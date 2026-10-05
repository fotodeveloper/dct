"""`dct release plan` and `dct clog candidates` — the read-only proof layer
(K-72 layer 1). No side effects: no database write, no ref / index /
work-tree change (every git read runs with `--no-optional-locks` through
dct.core.gitinfo.git_read). Usable by every project — also one whose own
release tool performs the effects.

build_plan(engine, project, ...) answers, for the release candidate (the
full HEAD of a STRICTLY verified checkout, K-52):
  - range (K-71): from the nearest release tag that is an ancestor of the
    candidate — see resolve_range for the exact rule — or, without one, the
    approximate `--since` window;
  - proof per commit (proof.py rules), reverts (K-68), squash signals and
    the collective attestation proposals (K-69), provenance (K-70);
  - classification of every unreleased entry: proven | unproven with a
    reason; foreign citations; the index state (K-50, read part); the
    version-file contract (K-73).
commit_candidates(engine, project, ...) gives /commit its candidate lists
(K-53): confirmed (client provenance of THIS checkout) vs uncertain.

Both return JSON-native dicts whose top-level keys are PLAN_KEYS /
CANDIDATE_KEYS (pinned by a schema test; SCHEMA_VERSION moves when a key's
meaning changes). Schema: docs/release-workflow.md.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa

from dct.core import gitinfo
from dct.core.changelog import list_entries, normalize_worktree
from dct.core.projects import get_project
from dct.core.releases import get_release, get_version_files, validate_proof
from dct.core.version import (
    bump,
    normalize_version,
    same_version,
    storable_version,
    version_key,
)
from dct.db import changelog_entries, projects
from dct.workflows.release.proof import (
    TRAILER_FORMAT,
    Commit,
    RangeProof,
    lost_trailer_ids,
    merge_pr,
    proven_ids,
    range_proof,
    revert_target,
    squash_signals,
    suffix_pr,
)

SCHEMA_VERSION = 1
PLAN_KEYS = (
    "schema_version", "project", "kind", "version", "checkout", "unfinished_release",
    "range", "entries", "foreign_citations", "reverts", "squash_commits",
    "attestation_groups", "proof", "index", "version_files", "warnings",
)
CANDIDATE_KEYS = (
    "schema_version", "project", "checkout", "range", "confirmed", "uncertain",
    "other_branch", "already_proven", "staged_files", "warnings",
)
UNPROVEN_REASONS = ("no_trailer", "reverted", "below_range", "squash_suspected", "no_git")

_LOG_FORMAT = "%x1e%H%x1f%P%x1f" + TRAILER_FORMAT + "%x1f%B"
_FULL_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_UNSAFE_REF_CHARS = re.compile(r"[\s*?\[\\~^:]|\.\.|^-|@\{", re.ASCII)
_SINCE_MARGIN = timedelta(days=1)


def _jsonable(value):
    """datetime → ISO 8601 (naive = UTC), recursively; the rest unchanged."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ── git readers (strict, read-only) ──────────────────────────────────────────


def read_commits(cwd: str, *revs: str) -> list[Commit]:
    """The commits selected by `git log <revs>`, OLDEST first (topological).

    One git process; message, parents and git's Changelog trailer value per
    commit. An unparseable record raises GitReadError (never a partial list).
    """
    _, out = gitinfo.git_read(
        cwd, "log", "--topo-order", "--reverse", f"--format={_LOG_FORMAT}", *revs, "--")
    commits = []
    for record in out.split("\x1e")[1:]:
        parts = record.split("\x1f", 3)
        if len(parts) != 4 or not _FULL_SHA_RE.fullmatch(parts[0]):
            raise gitinfo.GitReadError(f"unparseable `git log` record in {cwd}: {record[:80]!r}")
        sha, parents, trailers, message = parts
        commits.append(Commit(
            sha=sha, parents=tuple(parents.split()), message=message.rstrip("\n"),
            trailer_value=trailers.strip(),
        ))
    return commits


def release_tags(cwd: str, candidate: str) -> list[dict]:
    """Tags that are ANCESTORS of `candidate` (or point at it) and whose name
    follows the version contract (`v1.5.0`, `1.5`, …): [{name, sha (peeled
    commit), version (normalized)}], sorted by name. Other tags are ignored."""
    _, out = gitinfo.git_read(
        cwd, "for-each-ref", f"--merged={candidate}",
        "--format=%(refname:strip=2)%1f%(objectname)%1f%(*objectname)", "refs/tags")
    tags = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        name, obj, peeled = parts
        try:
            version = normalize_version(name)
        except ValueError:
            continue
        if name != name.strip():
            continue
        tags.append({"name": name, "sha": peeled or obj, "version": version})
    return sorted(tags, key=lambda t: t["name"])


def _pick_base(tags: list[dict], current: str | None) -> dict | None:
    """The NEAREST release tag (K-71) — deterministic rule: the ancestor tag
    with the HIGHEST version under the version contract (the never-rewind
    order, so a maintenance branch cut from v1.5.0 with v1.5.1 picks
    v1.5.1). Ties (1.5 / v1.5.0 naming the same version): the exact name
    `v<current_version>` when that is the version, else a `v`-prefixed name,
    else the lowest name."""
    if not tags:
        return None
    top = max(version_key(t["version"]) for t in tags)
    tied = [t for t in tags if version_key(t["version"]) == top]
    expected = f"v{current}" if current else None

    def rank(tag: dict) -> tuple:
        return (tag["name"] != expected, not tag["name"].startswith(("v", "V")), tag["name"])

    return min(tied, key=rank)


def _since_value(created: datetime) -> str:
    moment = (created if created.tzinfo else created.replace(tzinfo=UTC)) - _SINCE_MARGIN
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def resolve_range(
    cwd: str, candidate: str, current_version: str | None,
    oldest_unreleased: datetime | None,
) -> dict:
    """The proof range of `candidate` (K-71).

    - base = _pick_base over the ancestor release tags; the range is
      `<base>..<candidate>`. `confirm_required` is True when the base is not
      the tag of the project's current_version (`v<current>`, compared under
      the version contract) — the plan shows it and the owner confirms.
    - No ancestor release tag: the approximate window `--since=<oldest
      unreleased entry's created_at − 1 day> <candidate>` with
      `approximate=True` (a warning is added by the caller); without
      unreleased entries the range is empty.
    Returns {candidate_sha, base_tag, base_sha, base_version, expected_tag
    ({name, exists, ancestor} or None without a current_version),
    confirm_required, approximate, since, revs (git log arguments, None =
    empty range), display}.
    """
    tags = release_tags(cwd, candidate)
    base = _pick_base(tags, current_version)
    expected = None
    if current_version:
        name = f"v{current_version}"
        code, out = gitinfo.git_read(
            cwd, "rev-parse", "-q", "--verify", f"refs/tags/{name}^{{commit}}", ok_codes=(0, 1))
        exists = code == 0
        ancestor = exists and gitinfo.git_is_ancestor(cwd, out.strip(), candidate)
        expected = {"name": name, "exists": exists, "ancestor": ancestor}
    rng = {
        "candidate_sha": candidate, "base_tag": None, "base_sha": None, "base_version": None,
        "expected_tag": expected, "confirm_required": False, "approximate": False,
        "since": None, "revs": None, "display": None,
    }
    if base is not None:
        rng.update(
            base_tag=base["name"], base_sha=base["sha"], base_version=base["version"],
            confirm_required=not same_version(base["version"], current_version),
            revs=[f"{base['sha']}..{candidate}"], display=f"{base['name']}..{candidate}",
        )
        return rng
    rng["approximate"] = True
    if oldest_unreleased is not None:
        since = _since_value(oldest_unreleased)
        rng.update(since=since, revs=[f"--since={since}", candidate],
                   display=f"--since='{since}' {candidate}")
    return rng


def first_parent_merges(cwd: str, revs: list[str]) -> tuple[list[Commit], dict[str, str]]:
    """The candidate's first-parent line within `revs` (oldest first) and
    commit SHA → the first-parent MERGE commit that brought it in (commits
    on the line itself are absent from the map)."""
    line = read_commits(cwd, "--first-parent", *revs)
    merged_by: dict[str, str] = {}
    for commit in line:
        if commit.is_merge:
            for sha in introduced_by(cwd, commit):
                merged_by.setdefault(sha, commit.sha)
    return line, merged_by


def introduced_by(cwd: str, merge: Commit) -> list[str]:
    """Commits a merge brought in: `git rev-list <M> ^<first parent>`, minus M."""
    _, out = gitinfo.git_read(cwd, "rev-list", merge.sha, f"^{merge.parents[0]}", "--")
    return [sha for sha in out.split() if sha != merge.sha]


def _branch_tips(cwd: str, branch: str) -> list[str]:
    """Tips of refs/heads/<branch> and refs/remotes/*/<branch> ([] when the
    name cannot be a safe ref pattern)."""
    if not branch or _UNSAFE_REF_CHARS.search(branch):
        return []
    _, out = gitinfo.git_read(
        cwd, "for-each-ref", "--format=%(objectname)",
        f"refs/heads/{branch}", f"refs/remotes/*/{branch}")
    return sorted(set(out.split()))


def _absent_branch_proof(cwd: str, candidate: str, branch: str) -> set[int]:
    """Ids proven by commits of `branch` (its local / remote-tracking refs)
    that are NOT in the candidate's ancestry — the branch's own proving
    commits are absent, so a squash plausibly carried its change (K-69)."""
    ids: set[int] = set()
    for tip in _branch_tips(cwd, branch):
        if gitinfo.git_is_ancestor(cwd, tip, candidate):
            continue
        for commit in read_commits(cwd, f"{candidate}..{tip}"):
            ids |= proven_ids(commit)
    return ids


def _branch_named(message: str, branch: str) -> bool:
    pattern = r"(?<![\w./-])" + re.escape(branch) + r"(?![\w./-])"
    return re.search(pattern, message) is not None


_CONFLICTS = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def index_state(repo_root: str, artifacts: list[dict]) -> dict:
    """Index / work-tree state for a release (K-50, read-only part).

    `git status --porcelain=v1 -z --untracked-files=all` (paths relative to
    the repository root): {clean (nothing staged, unstaged or conflicted —
    untracked files do not count), staged, unstaged, untracked, conflicted
    (sorted path lists), release_artifacts: per artifact {path, role,
    staged (the index differs from HEAD), unstaged, untracked,
    conflicted}}. A rename lists its new path."""
    _, out = gitinfo.git_read(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    staged, unstaged, untracked, conflicted = set(), set(), set(), set()
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        xy, path = token[:2], token[3:]
        if xy[0] in "RC":
            i += 1  # the original path follows a rename / copy
        if xy == "??":
            untracked.add(path)
        elif xy in _CONFLICTS:
            conflicted.add(path)
        else:
            if xy[0] not in " !":
                staged.add(path)
            if xy[1] not in " !":
                unstaged.add(path)
    return {
        "clean": not (staged or unstaged or conflicted),
        "staged": sorted(staged),
        "unstaged": sorted(unstaged),
        "untracked": sorted(untracked),
        "conflicted": sorted(conflicted),
        "release_artifacts": [
            {**artifact,
             "staged": artifact["path"] in staged,
             "unstaged": artifact["path"] in unstaged,
             "untracked": artifact["path"] in untracked,
             "conflicted": artifact["path"] in conflicted}
            for artifact in artifacts
        ],
    }


def staged_files(repo_root: str) -> list[str]:
    """`git diff --cached --name-only -z` (repo-root-relative, sorted)."""
    _, out = gitinfo.git_read(repo_root, "diff", "--cached", "--name-only", "-z")
    return sorted(p for p in out.split("\0") if p)


def _current_branch(cwd: str) -> str | None:
    code, out = gitinfo.git_read(cwd, "symbolic-ref", "-q", "--short", "HEAD", ok_codes=(0, 1))
    return (out.strip() or None) if code == 0 else None


# ── database readers ─────────────────────────────────────────────────────────


def _entry_view(entry: dict) -> dict:
    uncertain = entry.get("branch_source") != "client"
    return {
        "id": entry["id"],
        "category": entry["category"],
        "entry": entry["entry"],
        "component": entry.get("component"),
        "source_file": entry.get("source_file"),
        "branch": entry.get("branch"),
        "worktree": entry.get("worktree"),
        "branch_source": entry.get("branch_source"),
        "uncertain": uncertain,
        "created_at": _jsonable(entry.get("created_at")),
    }


def _foreign_citations(engine: sa.Engine, project_id: int, cited: dict[int, list[str]]) -> list[dict]:
    """Cited ids that are not unreleased entries of the project: {id,
    reason: unknown | other_project | released | deleted, version (for
    released), commits}. Reported, never stamped."""
    if not cited:
        return []
    c = changelog_entries.c
    with engine.begin() as conn:
        rows = {r.id: r for r in conn.execute(
            sa.select(c.id, c.project_id, c.version, c.deleted_at).where(c.id.in_(sorted(cited)))
        )}
    out = []
    for entry_id in sorted(cited):
        row = rows.get(entry_id)
        version = None
        if row is None:
            reason = "unknown"
        elif row.project_id != project_id:
            reason = "other_project"
        elif row.deleted_at is not None:
            reason = "deleted"
        else:
            reason, version = "released", row.version
        out.append({"id": entry_id, "reason": reason, "version": version,
                    "commits": cited[entry_id]})
    return out


def _project_id(engine: sa.Engine, slug: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            sa.select(projects.c.id).where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
        ).scalar_one()


def _target_version(current: str | None, version: str | None, bump_type: str | None) -> str | None:
    if version and bump_type:
        raise ValueError("Provide at most one of version or bump_type (patch/minor/major)")
    if version:
        return storable_version(version)
    if bump_type:
        if not current:
            raise ValueError("No current version to bump — provide an explicit version")
        return storable_version(bump(current, bump_type))
    return None


def _artifacts(project: dict, checkout: dict, version_files: list[str] | None) -> list[dict]:
    """Release artifacts as repo-relative paths: the changelog file (when
    dct manages it) and the version-file contract (repo-relative, K-73)."""
    out = []
    if project.get("manages_changelog", True) and project.get("changelog_path"):
        full = os.path.join(checkout["project_root"], project["changelog_path"])
        rel = os.path.relpath(full, checkout["repo_root"]).replace(os.sep, "/")
        out.append({"path": rel, "role": "changelog"})
    for path in version_files or []:
        out.append({"path": path, "role": "version_file"})
    return out


# ── the plan ─────────────────────────────────────────────────────────────────


def _provenance(proving: list[str], by_sha: dict[str, Commit], merged_by: dict[str, str]) -> dict:
    """{commits, merge_commit, pr} of a proven entry (K-70): the first
    proving commit (oldest first) brought in by a first-parent merge names
    the merge; the PR comes from that merge's `Merge pull request #N`
    subject, else from a proving commit's `… (#N)` subject."""
    merge_commit = pr = None
    for sha in proving:
        if sha in merged_by:
            merge_commit = merged_by[sha]
            pr = merge_pr(by_sha[merge_commit].subject) if merge_commit in by_sha else None
            break
    if pr is None:
        for sha in proving:
            pr = suffix_pr(by_sha[sha].subject)
            if pr is not None:
                break
    return {"commits": list(proving), "merge_commit": merge_commit, "pr": pr}


def _squash_report(commit: Commit, signals: list[str]) -> dict:
    return {
        "sha": commit.sha,
        "subject": commit.subject,
        "signals": signals,
        "pr": suffix_pr(commit.subject),
        "lost_ids": sorted(lost_trailer_ids(commit.message)),
        "proves": sorted(proven_ids(commit)),
    }


def _attestation_groups(
    cwd: str, candidate: str, pending: list[dict], squashes: list[dict],
) -> tuple[dict[int, int], list[dict]]:
    """Group unproven entries that plausibly came from a squash into ONE
    proposed collective attestation per source branch (K-69) — a proposal,
    never an assignment. Links (entry ↔ squash commit), strongest first:
      - indented_trailer: the squash body carries the id on an indented
        `Changelog:` line (local `git merge --squash`);
      - branch_named: the squash message names the entry's CLIENT-confirmed
        branch as a whole token;
      - branch_commits_absent: the entry's client branch still has a local
        or remote-tracking ref whose commits prove the id but are NOT in
        the candidate's ancestry (the branch was squashed, not merged) —
        linked to every squash commit of the range (unspecific).
    Entries with server / null provenance link only through
    indented_trailer (their branch is unconfirmed). Returns (entry id →
    group index, groups [{branch, entry_ids, squash_commits, signals,
    proposal}])."""
    if not squashes:
        return {}, []
    absent_cache: dict[str, set[int]] = {}
    groups: dict[str | None, dict] = {}
    membership: dict[int, str | None] = {}
    for entry in pending:
        branch = entry["branch"] if entry["branch_source"] == "client" else None
        links: list[tuple[str, str]] = []
        for squash in squashes:
            if entry["id"] in squash["lost_ids"]:
                links.append((squash["sha"], "indented_trailer"))
            elif branch and _branch_named(squash["message"], branch):
                links.append((squash["sha"], "branch_named"))
        if not links and branch:
            if branch not in absent_cache:
                absent_cache[branch] = _absent_branch_proof(cwd, candidate, branch)
            if entry["id"] in absent_cache[branch]:
                links = [(s["sha"], "branch_commits_absent") for s in squashes]
        if not links:
            continue
        group = groups.setdefault(branch, {"branch": branch, "entry_ids": [],
                                           "squash_commits": [], "signals": []})
        group["entry_ids"].append(entry["id"])
        for sha, signal in links:
            if sha not in group["squash_commits"]:
                group["squash_commits"].append(sha)
            if signal not in group["signals"]:
                group["signals"].append(signal)
        membership[entry["id"]] = branch
    ordered = sorted(groups.values(), key=lambda g: (g["branch"] is None, g["branch"] or ""))
    order = {sha: i for i, sha in enumerate(s["sha"] for s in squashes)}
    for group in ordered:
        group["entry_ids"].sort()
        group["squash_commits"].sort(key=order.__getitem__)
        group["signals"].sort()
        group["proposal"] = (
            "collective attestation of these entries after the owner confirms that "
            "their changes are in the candidate — never an automatic assignment")
    index = {g["branch"]: i for i, g in enumerate(ordered)}
    return {eid: index[b] for eid, b in membership.items()}, ordered


def build_plan(
    engine: sa.Engine,
    project_slug: str,
    *,
    version: str | None = None,
    bump_type: str | None = None,
    cwd: str | None = None,
) -> dict:
    """The release plan of `project_slug` for the checkout at `cwd` (default:
    the process cwd) — read-only (see module doc). Keys: PLAN_KEYS.

    Raises ValueError (unknown project, bad version / bump),
    gitinfo.NotACheckoutError / CheckoutVerificationError (the checkout
    cannot be CONFIRMED — K-52, never a fallback to the main checkout) and
    gitinfo.GitReadError (a history read failed — never a partial plan).
    """
    project = get_project(engine, project_slug)
    if project is None:
        raise ValueError(f"Project '{project_slug}' not found")
    current = project.get("current_version")
    target = _target_version(current, version, bump_type)
    warnings: list[str] = []
    unfinished = get_release(engine, project_slug)
    kind = "resume" if unfinished else "release"
    if unfinished and target and not same_version(target, unfinished["version"]):
        warnings.append(
            f"an unfinished release {unfinished['version']} exists — resume or abandon it "
            f"before planning {target}")
    if unfinished and not target:
        target = unfinished["version"]

    entries = sorted(list_entries(engine, project_slug, unreleased_only=True), key=lambda e: e["id"])
    views = [_entry_view(e) for e in entries]
    unreleased_ids = {e["id"] for e in entries}

    plan = {key: None for key in PLAN_KEYS}
    plan.update(
        schema_version=SCHEMA_VERSION, project=project_slug, kind=kind,
        version={"current": current, "target": target,
                 "bump": None if version else bump_type},
        unfinished_release=_jsonable(unfinished), foreign_citations=[], reverts=[],
        squash_commits=[], attestation_groups=[], proof={}, warnings=warnings,
        version_files=_jsonable(get_version_files(engine, project_slug)),
    )

    path = project.get("path")
    checkout = gitinfo.verify_release_checkout(path, cwd) if path else None
    if checkout is None:
        plan["checkout"] = {"git": False, "reason": "no_path" if not path else "not_a_repository",
                            "project_root": path}
        plan["entries"] = {"proven": [], "unproven": [
            {**v, "reason": "no_git"} for v in views]}
        warnings.append("no git checkout — entries cannot be proven by commits")
        return plan

    root, candidate = checkout["repo_root"], checkout["head"]
    branch = _current_branch(root)
    plan["checkout"] = {
        "git": True, "repo_root": root, "project_root": checkout["project_root"],
        "candidate_sha": candidate, "branch": branch, "detached": branch is None,
    }
    if unfinished:
        plan["checkout"]["matches_record"] = {
            "candidate_sha": unfinished.get("candidate_sha") == candidate,
            "checkout_root": unfinished.get("checkout_root") in (None, normalize_worktree(root)),
        }

    oldest = min((e["created_at"] for e in entries if e.get("created_at")), default=None)
    rng = resolve_range(root, candidate, current, oldest)
    if rng["confirm_required"]:
        warnings.append(
            f"the proof range starts at {rng['base_tag']}, not at v{current} — confirm it")
    if rng["approximate"]:
        warnings.append(
            "no release tag is an ancestor of the candidate — the range is the approximate "
            "--since window" if rng["since"] else
            "no release tag is an ancestor of the candidate and nothing is unreleased — "
            "empty range")

    commits = read_commits(root, *rng["revs"]) if rng["revs"] else []
    by_sha = {c.sha: c for c in commits}
    _, merged_by = first_parent_merges(root, rng["revs"]) if rng["revs"] else ([], {})
    introduced = {c.sha: introduced_by(root, c) for c in commits
                  if c.is_merge and any(_is_revert_of(o, c.sha) for o in commits)}
    proof: RangeProof = range_proof(commits, introduced)
    plan["reverts"] = proof.analysis.reverts
    for report in plan["reverts"]:
        if report["status"] != "in_range":
            warnings.append(
                f"revert {report['sha'][:12]} targets {report['target']} — "
                f"{report['status'].replace('_', ' ')}; not applied")

    below: dict[int, list[str]] = {}
    if rng["base_sha"] and oldest is not None:
        for commit in read_commits(root, f"--since={_since_value(oldest)}", rng["base_sha"]):
            for entry_id in proven_ids(commit):
                below.setdefault(entry_id, []).append(commit.sha)

    squashes = []
    for commit in commits:
        signals = squash_signals(commit)
        if signals:
            squashes.append({**_squash_report(commit, signals), "message": commit.message})

    proven, unproven, pending, proof_map = [], [], [], {}
    for view in views:
        entry_id = view["id"]
        proving = proof.proving.get(entry_id) or []
        if proving:
            prov = _provenance(proving, by_sha, merged_by)
            proof_map[entry_id] = prov
            proven.append({**view, **prov})
        elif entry_id in proof.reverted:
            reverted = proof.reverted[entry_id]
            unproven.append({**view, "reason": "reverted", "commits": sorted(reverted),
                             "reverted_by": sorted({r for rs in reverted.values() for r in rs})})
        elif entry_id in below:
            unproven.append({**view, "reason": "below_range", "commits": below[entry_id]})
        else:
            pending.append(view)

    groups_of, groups = _attestation_groups(root, candidate, pending, squashes)
    for view in pending:
        if view["id"] in groups_of:
            unproven.append({**view, "reason": "squash_suspected", "group": groups_of[view["id"]]})
        else:
            unproven.append({**view, "reason": "no_trailer"})
    unproven.sort(key=lambda e: e["id"])

    cited = {i: shas for i, shas in proof.proving.items() if i not in unreleased_ids and shas}
    for i, reverted in proof.reverted.items():
        if i not in unreleased_ids and i not in cited:
            cited[i] = sorted(reverted)
    plan.update(
        range=rng,
        entries={"proven": proven, "unproven": unproven},
        foreign_citations=_foreign_citations(engine, _project_id(engine, project_slug), cited),
        squash_commits=[{k: v for k, v in s.items() if k != "message"} for s in squashes],
        attestation_groups=groups,
        proof=validate_proof(proof_map) or {},
        index=index_state(root, _artifacts(
            project, checkout, (plan["version_files"] or {}).get("version_files"))),
    )
    plan["range"]["commit_count"] = len(commits)
    return plan


def _is_revert_of(commit: Commit, sha: str) -> bool:
    target = revert_target(commit.message)
    return bool(target) and sha.startswith(target)


# ── /commit candidates (K-53) ────────────────────────────────────────────────


def commit_candidates(engine: sa.Engine, project_slug: str, *, cwd: str | None = None) -> dict:
    """Changelog candidates for the commit being prepared in `cwd` (default:
    the process cwd) — read-only, best-effort location (resolve_checkout).

    - confirmed: unreleased entries with CLIENT provenance whose branch is
      the current branch and whose worktree is this checkout's toplevel (or
      not recorded);
    - uncertain: entries with server-detected or no provenance — whatever
      branch / worktree they store (the worktree filter never removes them,
      K-53); on a detached HEAD also every client entry of this checkout
      (no confirmed list, `checkout.detached` True). Never attached
      automatically: match them against `staged_files` and confirm them
      (update_changelog branch + worktree) first;
    - other_branch: client entries of THIS worktree recorded on another
      branch (offer to correct their branch);
    - already_proven: ids a commit of the range (resolve_range, target HEAD)
      already proves — removed from every list.
    Client entries of other checkouts are not listed. Keys: CANDIDATE_KEYS.
    """
    project = get_project(engine, project_slug)
    if project is None:
        raise ValueError(f"Project '{project_slug}' not found")
    warnings: list[str] = []
    entries = sorted(list_entries(engine, project_slug, unreleased_only=True), key=lambda e: e["id"])
    context = gitinfo.resolve_checkout(project.get("path"), cwd)
    result = {key: None for key in CANDIDATE_KEYS}
    result.update(schema_version=SCHEMA_VERSION, project=project_slug, confirmed=[],
                  uncertain=[], other_branch=[], already_proven=[], staged_files=[],
                  warnings=warnings)
    if context is None:
        result["checkout"] = {"git": False}
        result["uncertain"] = [
            {**_entry_view(e), "uncertain_reason": _uncertain_reason(e)}
            for e in entries if e.get("branch_source") != "client"]
        warnings.append("not inside a checkout of the project — no confirmed candidates")
        return result

    toplevel, branch = context["repo_root"], context["branch"]
    head = gitinfo.git_head(toplevel)
    detached = branch is None
    result["checkout"] = {"git": True, "toplevel": toplevel, "branch": branch,
                          "head": head, "detached": detached}
    if detached:
        warnings.append("detached HEAD — no confirmed candidates; every candidate is uncertain")

    proven: set[int] = set()
    if head:
        try:
            oldest = min((e["created_at"] for e in entries if e.get("created_at")), default=None)
            rng = resolve_range(toplevel, head, project.get("current_version"), oldest)
            result["range"] = rng
            commits = read_commits(toplevel, *rng["revs"]) if rng["revs"] else []
            introduced = {c.sha: introduced_by(toplevel, c) for c in commits
                          if c.is_merge and any(_is_revert_of(o, c.sha) for o in commits)}
            proven = range_proof(commits, introduced).proven()
        except gitinfo.CheckoutVerificationError as exc:
            warnings.append(f"the range could not be read ({exc}) — proven ids not excluded")
        try:
            result["staged_files"] = staged_files(toplevel)
        except gitinfo.CheckoutVerificationError as exc:
            warnings.append(f"the staged file list could not be read ({exc})")

    here = normalize_worktree(toplevel)
    for entry in entries:
        if entry["id"] in proven:
            result["already_proven"].append(entry["id"])
            continue
        view = _entry_view(entry)
        if entry.get("branch_source") != "client":
            result["uncertain"].append({**view, "uncertain_reason": _uncertain_reason(entry)})
        elif entry.get("worktree") not in (here, None):
            continue
        elif detached:
            result["uncertain"].append(
                {**view, "uncertain": True, "uncertain_reason": "detached_head"})
        elif entry.get("branch") == branch:
            result["confirmed"].append(view)
        else:
            result["other_branch"].append(view)
    return result


def _uncertain_reason(entry: dict) -> str:
    return "server_detected" if entry.get("branch_source") == "server" else "no_provenance"
