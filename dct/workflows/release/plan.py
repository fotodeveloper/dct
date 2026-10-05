"""`dct release plan` and `dct clog candidates` — the read-only proof layer
(K-72 layer 1). No side effects: no database write, no ref / index /
work-tree change (every git read runs with `--no-optional-locks` through
dct.core.gitinfo.git_read). Usable by every project — also one whose own
release tool performs the effects.

build_plan(engine, project, ...) answers, for the release candidate (the
full HEAD of a STRICTLY verified checkout, K-52):
  - who performs it (K-119): the project's executor and, for an
    unfinished record, the record's stored executor, progress owner and
    consent — ONE read (core.releases.release_routing), so a dialog routes
    from this single result;
  - range (K-71, K-83): from the graph-nearest release tag that is an
    ancestor of the candidate — see resolve_range for the exact rule — or,
    without one, the approximate `--since` window; the ancestry graph is
    streamed and read in full or REFUSED past ANCESTRY_MAX_COMMITS (K-116
    H8 — never a cut graph);
  - proof per commit (proof.py rules), reverts with the merge mainline
    their message names (K-68, K-82 — revert_scopes; a revert is FULL only
    when its change equals the inverse of the reverted one in the ONE
    representation of change_between, which keeps blob identity, file type
    and mode where git prints no textual patch — K-105), squash signals and
    the collective attestation proposals (K-69), provenance from ONE
    recognition per proving commit (K-70, K-89) — a recognised PR number is
    marked `pr_source: "git"`, a heuristic one is never a `pr` (K-116 H7);
  - classification of every unreleased entry: proven | unproven with a
    reason; foreign citations; the index state (K-50, read part); the
    version-file contract (K-73);
  - the SNAPSHOT (K-80) — the immutable approved context (candidate, base,
    V, selection, proof, entry states) and its digest, which `dct release
    apply --plan-digest` re-derives for the candidate it pins. A resume
    plan (K-95 C5) shows the recorded release instead and computes nothing
    on today's HEAD.
commit_candidates(engine, project, ...) gives /commit its candidate lists
(K-53): confirmed (client provenance of THIS checkout) vs uncertain.

Both return JSON-native dicts whose top-level keys are PLAN_KEYS /
CANDIDATE_KEYS (pinned by a schema test; SCHEMA_VERSION moves when a key's
meaning changes). Schema: docs/release-workflow.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import deque
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa

from dct.core import gitinfo
from dct.core.changelog import entry_state_digest, list_entries, normalize_worktree
from dct.core.projects import get_project
from dct.core.releases import get_version_files, release_routing, validate_proof
from dct.core.version import (
    bump,
    normalize_version,
    same_version,
    storable_version,
)
from dct.db import changelog_entries, projects
from dct.workflows.release.proof import (
    TRAILER_FORMAT,
    Commit,
    RangeProof,
    lost_trailer_ids,
    merge_pr,
    parse_revert,
    proven_ids,
    range_proof,
    squash_signals,
    suffix_pr,
)

SCHEMA_VERSION = 3
PLAN_KEYS = (
    "schema_version", "project", "kind", "version", "checkout", "executor",
    "unfinished_release", "range", "entries", "foreign_citations", "reverts",
    "squash_commits", "attestation_groups", "proof", "index", "version_files", "warnings",
    "resume", "snapshot", "snapshot_digest",
)
# K-116 H8 — the most commits the range resolution reads from the ancestry
# graph of a candidate (read_ancestry). Past it the plan REFUSES; it never
# works on a cut graph. Documented in docs/release-workflow.md.
ANCESTRY_MAX_COMMITS = 1_000_000
CANDIDATE_KEYS = (
    "schema_version", "project", "checkout", "range", "confirmed", "uncertain",
    "other_branch", "already_proven", "staged_files", "warnings",
)
UNPROVEN_REASONS = (
    "no_trailer", "reverted", "revert_unconfirmed", "below_range", "squash_suspected",
    "no_git", "no_path",
)

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


def read_ancestry(cwd: str, candidate: str) -> Iterator[tuple[str, list[str]]]:
    """(commit, [its parents]) for EVERY commit reachable from `candidate` —
    `git rev-list --parents <candidate>`, STREAMED (K-116 H8;
    gitinfo.git_read_lines is the process layer: its timeout applies, and a
    failed read raises GitReadError). The graph is read in full or not at
    all: more than ANCESTRY_MAX_COMMITS commits raises GitReadError — a
    REFUSAL, never a graph cut at the cap (distances computed on a prefix
    could name the wrong range base)."""
    lines = gitinfo.git_read_lines(cwd, "rev-list", "--parents", candidate, "--")
    try:
        for count, line in enumerate(lines, 1):
            if count > ANCESTRY_MAX_COMMITS:
                raise gitinfo.GitReadError(
                    f"the history of {candidate} in {cwd} holds more than "
                    f"{ANCESTRY_MAX_COMMITS} commits — the release plan reads the ancestry "
                    f"graph in full to find the nearest release tag and refuses past that "
                    f"cap instead of computing a range on part of it")
            shas = line.split()
            if shas:
                yield shas[0], shas[1:]
    finally:
        lines.close()


def ancestor_distances(cwd: str, candidate: str, targets: set[str]) -> dict[str, int]:
    """The minimum number of PARENT EDGES from `candidate` to each commit of
    `targets` that is an ancestor of it (or the candidate itself, 0) — a
    breadth-first search over the full ancestry graph (read_ancestry: read
    with a cap that refuses, K-116 H8) (K-83).
    Not `rev-list --count TAG..candidate`: in a history with merges that
    counts commits, not the graph distance. No target → no read."""
    if not targets:
        return {}
    parents: dict[str, tuple[str, ...]] = {}
    for sha, above in read_ancestry(cwd, candidate):
        # One str object per commit id (each id appears as a key and as a
        # parent of its children) — the map is the plan's largest structure.
        parents[sys.intern(sha)] = tuple(sys.intern(parent) for parent in above)
    found: dict[str, int] = {}
    seen = {candidate}
    queue = deque([(candidate, 0)])
    while queue and len(found) < len(targets):
        sha, distance = queue.popleft()
        if sha in targets:
            found[sha] = distance
        for parent in parents.get(sha, ()):
            if parent not in seen:
                seen.add(parent)
                queue.append((parent, distance + 1))
    return found


def _pick_base(
    tags: list[dict], current: str | None, distances: dict[str, int],
) -> tuple[dict | None, list[dict]]:
    """(base, alternatives) — the NEAREST release tag (K-71/K-83): the
    ancestor tag with the fewest parent edges from the candidate
    (`distances`). Ties at that distance (several names on one commit, or
    tags on different commits equally far) — one deterministic choice: the
    exact name `v<current_version>`, then a tag naming the current version
    in another spelling, then a `v`-prefixed name, then the lowest name;
    every other tag at the same distance is returned in `alternatives`
    (sorted by name). Semantic versions are only compared, never ranked
    here (the highest version is NOT preferred)."""
    placed = [{**t, "distance": distances[t["sha"]]} for t in tags if t["sha"] in distances]
    if not placed:
        return None, []
    nearest = min(t["distance"] for t in placed)
    tied = [t for t in placed if t["distance"] == nearest]
    expected = f"v{current}" if current else None

    def rank(tag: dict) -> tuple:
        return (tag["name"] != expected, not same_version(tag["version"], current),
                not tag["name"].startswith(("v", "V")), tag["name"])

    base = min(tied, key=rank)
    alternatives = sorted((t for t in tied if t is not base), key=lambda t: t["name"])
    return base, alternatives


def _since_value(created: datetime) -> str:
    moment = (created if created.tzinfo else created.replace(tzinfo=UTC)) - _SINCE_MARGIN
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def resolve_range(
    cwd: str, candidate: str, current_version: str | None,
    oldest_unreleased: datetime | None,
) -> dict:
    """The proof range of `candidate` (K-71, K-83).

    - base = _pick_base over the ancestor release tags (graph distance from
      the candidate); the range is `<base>..<candidate>`.
      `confirm_required` is True when the base is not the tag of the
      project's current_version (compared under the version contract) or
      when another tag on a DIFFERENT commit is equally near — the plan
      shows it (`alternatives`) and the owner confirms.
    - No ancestor release tag: the approximate window `--since=<oldest
      unreleased entry's created_at − 1 day> <candidate>` with
      `approximate=True` (a warning is added by the caller); without
      unreleased entries the range is empty.
    Returns {candidate_sha, base_tag, base_sha, base_version, base_distance,
    alternatives ([{name, sha, version, distance}]), expected_tag ({name,
    exists, ancestor} or None without a current_version), confirm_required,
    approximate, since, revs (git log arguments, None = empty range),
    display, below_scan (set by build_plan)}.
    """
    tags = release_tags(cwd, candidate)
    distances = ancestor_distances(cwd, candidate, {t["sha"] for t in tags})
    base, alternatives = _pick_base(tags, current_version, distances)
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
        "base_distance": None, "alternatives": [],
        "expected_tag": expected, "confirm_required": False, "approximate": False,
        "since": None, "revs": None, "display": None, "below_scan": None,
    }
    if base is not None:
        rng.update(
            base_tag=base["name"], base_sha=base["sha"], base_version=base["version"],
            base_distance=base["distance"],
            alternatives=[{k: t[k] for k in ("name", "sha", "version", "distance")}
                          for t in alternatives],
            confirm_required=not same_version(base["version"], current_version)
            or any(t["sha"] != base["sha"] for t in alternatives),
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


def introduced_by(cwd: str, merge: Commit, parent: str | None = None) -> list[str]:
    """Commits a merge brought in relative to `parent` (default: its first
    parent): `git rev-list <M> ^<parent>`, minus M."""
    base = parent or merge.parents[0]
    _, out = gitinfo.git_read(cwd, "rev-list", merge.sha, f"^{base}", "--")
    return [sha for sha in out.split() if sha != merge.sha]


_DIFF_SECTION = "diff --git "


def change_between(cwd: str, old: str, new: str) -> tuple[tuple[str, ...], ...]:
    """The change from tree-ish `old` to `new` — the ONE representation the
    revert comparison uses (K-82, K-105): one tuple of lines per changed
    path, from `git diff-tree -p -U0 --no-renames --full-index` (a rename is
    a deletion plus an addition).

    Every section keeps git's header lines — the path, `new file mode` /
    `deleted file mode` (the file TYPE is in the mode: regular, executable,
    symlink, gitlink) and `old mode` / `new mode`.
    - A section WITH a textual patch: the removed and added lines are the
      identity of the change; the `@@` positions and the `index` line are
      dropped, so a revert equals the inverse of what it reverts even when
      later commits moved the lines or touched the file elsewhere (the blob
      ids legitimately differ then).
    - A section WITHOUT a textual patch (binary content, an empty file, a
      change git cannot print): the `index <old>..<new> [mode]` line is
      KEPT with the full input and output blob ids — two `Binary files …
      differ` lines are never the same change by themselves.
    """
    _, out = gitinfo.git_read(cwd, "diff-tree", "-p", "-U0", "--no-renames", "--no-color",
                              "--full-index", old, new, "--")
    sections: list[list[str]] = []
    for line in out.splitlines():
        if line.startswith(_DIFF_SECTION) or not sections:
            sections.append([])
        sections[-1].append(line)
    change = []
    for section in sections:
        textual = any(line.startswith("@@") for line in section)
        change.append(tuple(
            line for line in section
            if not line.startswith("@@") and not (textual and line.startswith("index "))))
    return tuple(change)


def revert_scopes(cwd: str, commits: list[Commit]) -> dict[str, dict]:
    """K-82 — for every standard revert of the range whose target is a
    range commit: {affected, confirmed, reason, mainline} for
    proof.analyze_reverts.

    - Target an ordinary commit T: affected [T]; the message must not use
      git's merge form (reason 'mainline_on_non_merge').
    - Target a merge M: the message must name the mainline P (`…, reversing
      changes made to <P>.`) and P must match EXACTLY ONE real parent of M
      (reasons 'no_mainline', 'mainline_not_a_parent',
      'mainline_ambiguous'); affected = M + `rev-list M ^P` — the commits
      the revert removes, never a fixed parent 1. Without a usable mainline
      every commit M brought in relative to ANY parent is affected (which
      side was reverted is unknown).
    - The revert must be FULL — every revert, an ordinary one included
      (K-116 H6): its own change equals the inverse of the reverted one (T →
      T's parent, or M → P) in the representation of change_between (K-105:
      blob identity, type and mode kept where git prints no textual patch);
      else reason 'partial' (a conflict resolved differently, an edited
      revert, a binary file reverted to another blob). A revert of a ROOT
      commit has nothing to be compared with: 'partial' too.
    A revert that is not confirmed neutralizes nothing; its affected
    entries get no automatic proof (reason 'revert_unconfirmed')."""
    shas = [c.sha for c in commits]
    by_sha = {c.sha: c for c in commits}
    scopes: dict[str, dict] = {}
    for commit in commits:
        ref = parse_revert(commit.message)
        if ref is None:
            continue
        matches = [sha for sha in shas if sha.startswith(ref.target)]
        if len(matches) != 1:
            continue
        target = by_sha[matches[0]]
        scope = {"affected": [target.sha], "confirmed": False, "reason": None, "mainline": None}
        inverse_of = None
        if target.is_merge:
            mains = [p for p in target.parents if ref.mainline and p.startswith(ref.mainline)]
            if len(mains) == 1:
                scope["mainline"] = inverse_of = mains[0]
                scope["affected"] += introduced_by(cwd, target, mains[0])
            else:
                scope["reason"] = ("no_mainline" if not ref.mainline else
                                   "mainline_ambiguous" if mains else "mainline_not_a_parent")
                for parent in target.parents:
                    scope["affected"] += introduced_by(cwd, target, parent)
        elif ref.merge_form:
            scope["reason"] = "mainline_on_non_merge"
        elif len(target.parents) == 1:
            inverse_of = target.parents[0]
        else:
            scope["reason"] = "partial"  # a root commit: no parent to compare with
        if inverse_of is not None:
            full = len(commit.parents) == 1 and \
                change_between(cwd, commit.parents[0], commit.sha) == change_between(cwd, target.sha, inverse_of)
            scope["confirmed"] = full
            scope["reason"] = None if full else "partial"
        scope["affected"] = sorted(set(scope["affected"]))
        scopes[commit.sha] = scope
    return scopes


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
    """{commits, merge_commit, pr, pr_source, source, ambiguous,
    associations} of a proven entry (K-70, K-89). Per proving commit ONE
    recognition: a proving commit that is itself a real merge names ITSELF
    (`own_merge`); else the first-parent merge that brought it in
    (`introducing_merge`). The PR is the `Merge pull request #N` subject of
    THAT merge — never borrowed from another commit; `pr_source` is then
    'git' (K-116 H7: recognised in the repository — the plan never emits
    'client', a declaration only a caller can make), else null. A `… (#N)`
    subject suffix may be an issue number: it is listed as an uncertain
    association (`subject_suffix`, certain False), never the entry's `pr`
    (K-95 C4). Several different merge associations → `ambiguous`,
    merge_commit / pr null — never one picked."""
    associations: list[dict] = []

    def add(item: dict) -> None:
        if item not in associations:
            associations.append(item)

    for sha in proving:
        commit = by_sha[sha]
        if commit.is_merge:
            add({"merge_commit": sha, "pr": merge_pr(commit.subject), "source": "own_merge",
                 "certain": True})
        elif sha in merged_by:
            merge = merged_by[sha]
            subject = by_sha[merge].subject if merge in by_sha else ""
            add({"merge_commit": merge, "pr": merge_pr(subject), "source": "introducing_merge",
                 "certain": True})
        suffix = None if commit.is_merge else suffix_pr(commit.subject)
        if suffix is not None:
            add({"merge_commit": None, "pr": suffix, "source": "subject_suffix",
                 "certain": False})
    merges = [a for a in associations if a["source"] != "subject_suffix"]
    distinct = {(a["merge_commit"], a["pr"]) for a in merges}
    chosen = merges[0] if len(distinct) == 1 else None
    return {
        "commits": list(proving),
        "merge_commit": chosen["merge_commit"] if chosen else None,
        "pr": chosen["pr"] if chosen else None,
        "pr_source": "git" if chosen and chosen["pr"] is not None else None,
        "source": chosen["source"] if chosen else None,
        "ambiguous": len(distinct) > 1,
        "associations": associations,
    }


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


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_digest(snapshot: dict) -> str:
    """SHA-256 (hex) of the snapshot's canonical JSON (sorted keys, compact
    separators, UTF-8) — the value `dct release apply --plan-digest` takes."""
    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()


def _selection(plan: dict, entry_ids, no_entries: bool) -> list[int]:
    """The ids the snapshot binds: [] for version-only, the given ids, or —
    by default — every id the plan proves (plus, for a project CONFIRMED
    outside git, every unreleased entry: K-63, the owner's choice)."""
    if no_entries:
        return []
    if entry_ids is not None:
        return sorted(set(entry_ids))
    ids = {e["id"] for e in plan["entries"]["proven"]}
    ids |= {e["id"] for e in plan["entries"]["unproven"] if e["reason"] == "no_git"}
    return sorted(ids)


def _snapshot(plan: dict, entries: list[dict], selected: list[int]) -> dict:
    """The IMMUTABLE approved context of one release (K-80): the checkout
    (repository, checkout root, project root), the full candidate SHA, the
    range base, the current and the target version, the selected ids, their
    proof (the map the stamp stores) and the state digest of EVERY
    unreleased entry the plan saw (core.changelog.entry_state_digest — an
    entry added, edited or removed after the plan changes it)."""
    checkout = plan["checkout"]
    rng = plan["range"]
    return {
        "project": plan["project"],
        "version": plan["version"]["target"],
        "current_version": plan["version"]["current"],
        "git": checkout["git"],
        "repo_root": checkout.get("repo_root"),
        "checkout_root": normalize_worktree(checkout.get("repo_root")),
        "project_root": checkout.get("project_root"),
        "candidate_sha": checkout.get("candidate_sha"),
        "range": None if rng is None else {
            k: rng[k] for k in ("base_tag", "base_sha", "approximate", "since", "revs")},
        "selected_ids": selected,
        "proof": {str(i): plan["proof"][str(i)] for i in selected if str(i) in plan["proof"]},
        "entries": {str(e["id"]): entry_state_digest(e) for e in entries},
    }


_EFFECT_ORDER = ("stamped", "files_written", "committed", "tagged", "pushed", "completed")


def _resume_section(record: dict, head: str | None, views: list[dict]) -> dict:
    """K-95 C5 — what a resume continues: the RECORDED candidate, entry
    set, proof and effect state (by status), never recomputed on today's
    HEAD; the current HEAD and the unreleased entries added since are shown
    beside it, separately."""
    rank = _EFFECT_ORDER.index(record["status"]) if record["status"] in _EFFECT_ORDER else -1
    candidate = record.get("candidate_sha")
    return {
        "version": record["version"],
        "status": record["status"],
        "candidate_sha": candidate,
        "checkout_root": record.get("checkout_root"),
        "release_mode": record.get("release_mode"),
        "entry_ids": list(record.get("entry_ids") or []),
        "proof": record.get("proof") or {},
        "release_commit_sha": record.get("release_commit_sha"),
        "manifest": record.get("manifest"),
        "effects": {status: rank >= i for i, status in enumerate(_EFFECT_ORDER)},
        "head": head,
        "head_is_candidate": None if head is None or not candidate else head == candidate,
        "new_unreleased": views,
    }


def build_plan(
    engine: sa.Engine,
    project_slug: str,
    *,
    version: str | None = None,
    bump_type: str | None = None,
    cwd: str | None = None,
    entry_ids=None,
    no_entries: bool = False,
    pinned_checkout: dict | None = None,
) -> dict:
    """The release plan of `project_slug` for the checkout at `cwd` (default:
    the process cwd) — read-only (see module doc). Keys: PLAN_KEYS.

    `entry_ids` / `no_entries` choose the selection the SNAPSHOT binds
    (default: every proven id); `snapshot` + `snapshot_digest` are the
    approved context `dct release apply --plan-digest` re-derives (K-80).
    `pinned_checkout` (the effects layer): the checkout dict the caller
    already verified (verify_release_checkout, its `head` = THE candidate)
    — the plan then never reads HEAD again. A resume plan (an unfinished
    record exists, K-95 C5) shows the record in `resume` and computes no
    range / proof on today's HEAD (`snapshot` null). `executor` (K-119):
    {project, record} of core.releases.release_routing — the project's
    setting and, for that unfinished record, ITS stored executor, progress
    owner and consent, read together with `unfinished_release`.

    Raises ValueError (unknown project, bad version / bump),
    gitinfo.NotACheckoutError / CheckoutVerificationError (the checkout
    cannot be CONFIRMED — K-52, never a fallback to the main checkout) and
    gitinfo.GitReadError (a history read failed, timed out, or the ancestry
    graph exceeds ANCESTRY_MAX_COMMITS — never a partial plan); ValueError
    also when one entry has more than core.releases.ASSOCIATIONS_MAX
    associations (K-116 H8: refused, never cut).
    """
    project = get_project(engine, project_slug)
    if project is None:
        raise ValueError(f"Project '{project_slug}' not found")
    if entry_ids is not None and no_entries:
        raise ValueError("entry_ids and no_entries are mutually exclusive")
    current = project.get("current_version")
    target = _target_version(current, version, bump_type)
    warnings: list[str] = []
    # K-119: the project's executor and the unfinished record (with ITS
    # stored executor / progress owner / consent) — one read, one result.
    routing, unfinished = release_routing(engine, project_slug)
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
        executor=routing,
        unfinished_release=_jsonable(unfinished), foreign_citations=[], reverts=[],
        squash_commits=[], attestation_groups=[], proof={}, warnings=warnings,
        version_files=_jsonable(get_version_files(engine, project_slug)),
        entries={"proven": [], "unproven": []},
    )

    path = project.get("path")
    if pinned_checkout is not None:
        checkout = pinned_checkout
    else:
        # K-95 C1: no_git only for a path CONFIRMED outside every repository
        # (verify_release_checkout → None); a detection error raises.
        checkout = gitinfo.verify_release_checkout(path, cwd) if path else None
    if checkout is None:
        reason = "no_path" if not path else "no_git"
        plan["checkout"] = {"git": False,
                            "reason": "no_path" if not path else "not_a_repository",
                            "project_root": path}
        warnings.append("the project has no registered path — its checkout cannot be verified"
                        if not path else "no git checkout — entries cannot be proven by commits")
        if unfinished:
            plan["resume"] = _jsonable(_resume_section(unfinished, None, views))
            return plan
        plan["entries"] = {"proven": [], "unproven": [{**v, "reason": reason} for v in views]}
        if path:
            plan["snapshot"] = _snapshot(plan, entries, _selection(plan, entry_ids, no_entries))
            plan["snapshot_digest"] = snapshot_digest(plan["snapshot"])
        return plan

    root, head = checkout["repo_root"], checkout["head"]
    branch = _current_branch(root)
    plan["checkout"] = {
        "git": True, "repo_root": root, "project_root": checkout["project_root"],
        "candidate_sha": head, "head": head, "branch": branch, "detached": branch is None,
    }
    index_artifacts = _artifacts(project, checkout, (plan["version_files"] or {}).get("version_files"))
    if unfinished:
        recorded = unfinished.get("candidate_sha")
        plan["checkout"]["candidate_sha"] = recorded
        plan["checkout"]["matches_record"] = {
            "candidate_sha": recorded == head,
            "checkout_root": unfinished.get("checkout_root") in (None, normalize_worktree(root)),
        }
        if recorded != head:
            warnings.append(
                f"HEAD {head[:12]} is not the recorded candidate "
                f"{(recorded or '-')[:12]} — the resume continues the RECORDED release; "
                f"nothing is recomputed on the current HEAD")
        plan["resume"] = _jsonable(_resume_section(unfinished, head, views))
        plan["index"] = index_state(root, index_artifacts)
        return plan

    candidate = head
    oldest = min((e["created_at"] for e in entries if e.get("created_at")), default=None)
    rng = resolve_range(root, candidate, current, oldest)
    if rng["confirm_required"]:
        tied = [a["name"] for a in rng["alternatives"] if a["sha"] != rng["base_sha"]]
        warnings.append(
            f"the proof range starts at {rng['base_tag']}"
            + (f", equally near: {', '.join(tied)}" if tied else "")
            + (f", not at v{current}" if not same_version(rng["base_version"], current) else "")
            + " — confirm it")
    if rng["approximate"]:
        warnings.append(
            "no release tag is an ancestor of the candidate — the range is the approximate "
            "--since window" if rng["since"] else
            "no release tag is an ancestor of the candidate and nothing is unreleased — "
            "empty range")

    commits = read_commits(root, *rng["revs"]) if rng["revs"] else []
    by_sha = {c.sha: c for c in commits}
    _, merged_by = first_parent_merges(root, rng["revs"]) if rng["revs"] else ([], {})
    proof: RangeProof = range_proof(commits, revert_scopes(root, commits))
    plan["reverts"] = proof.analysis.reverts
    for report in plan["reverts"]:
        if report["status"] == "unconfirmed":
            warnings.append(
                f"revert {report['sha'][:12]} of {report['target']} is not a confirmed full "
                f"revert ({report['reason']}) — the entries it may touch have no automatic "
                f"proof; confirm them (attest) or correct the history")
        elif report["status"] != "in_range":
            warnings.append(
                f"revert {report['sha'][:12]} targets {report['target']} — "
                f"{report['status'].replace('_', ' ')}; not applied")

    below: dict[int, list[str]] = {}
    if rng["base_sha"] and oldest is not None:
        # K-95 C3: a BOUNDED diagnostic scan below the base — "not found in
        # the scanned window" never means "not in history".
        since = _since_value(oldest)
        rng["below_scan"] = {"since": since, "until": rng["base_sha"], "bounded": True}
        for commit in read_commits(root, f"--since={since}", rng["base_sha"]):
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
        elif entry_id in proof.unconfirmed:
            doubted = proof.unconfirmed[entry_id]
            unproven.append({**view, "reason": "revert_unconfirmed", "commits": sorted(doubted),
                             "reverted_by": sorted({r for rs in doubted.values() for r in rs})})
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
    for doubted in (proof.reverted, proof.unconfirmed):
        for i, shas in doubted.items():
            if i not in unreleased_ids and i not in cited:
                cited[i] = sorted(shas)
    plan.update(
        range=rng,
        entries={"proven": proven, "unproven": unproven},
        foreign_citations=_foreign_citations(engine, _project_id(engine, project_slug), cited),
        squash_commits=[{k: v for k, v in s.items() if k != "message"} for s in squashes],
        attestation_groups=groups,
        proof=validate_proof(proof_map, pr_source="git") or {},
        index=index_state(root, index_artifacts),
    )
    plan["range"]["commit_count"] = len(commits)
    selected = _selection(plan, entry_ids, no_entries)
    outside = [i for i in selected if str(i) not in plan["proof"]]
    if outside:
        warnings.append(
            f"the selection holds ids the plan does not prove: "
            f"{', '.join(f'#{i}' for i in outside)} — apply refuses them")
    plan["snapshot"] = _snapshot(plan, entries, selected)
    plan["snapshot_digest"] = snapshot_digest(plan["snapshot"])
    return plan


# ── /commit candidates (K-53) ────────────────────────────────────────────────


def commit_candidates(engine: sa.Engine, project_slug: str, *, cwd: str | None = None) -> dict:
    """Changelog candidates for the commit being prepared in `cwd` (default:
    the process cwd) — read-only, best-effort location (resolve_checkout).

    - confirmed: unreleased entries with CLIENT provenance whose branch is
      the current branch and whose worktree is this checkout's toplevel;
    - uncertain: entries with server-detected or no provenance — whatever
      branch / worktree they store (the worktree filter never removes them,
      K-53); on a detached HEAD also every client entry of this checkout
      (no confirmed list, `checkout.detached` True); a client entry of the
      current branch whose worktree is NOT recorded (`no_worktree`, K-95
      C2 — a candidate to compare with the diff). Never attached
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
            proven = range_proof(commits, revert_scopes(toplevel, commits)).proven()
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
        elif entry.get("branch") == branch and entry.get("worktree") is None:
            # K-95 C2: the branch matches but the checkout is not recorded —
            # a candidate to compare with the diff, never confirmed.
            result["uncertain"].append(
                {**view, "uncertain": True, "uncertain_reason": "no_worktree"})
        elif entry.get("branch") == branch:
            result["confirmed"].append(view)
        else:
            result["other_branch"].append(view)
    return result


def _uncertain_reason(entry: dict) -> str:
    return "server_detected" if entry.get("branch_source") == "server" else "no_provenance"
