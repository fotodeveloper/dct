"""`dct release plan` and `dct clog candidates` — the read-only proof layer
(K-72 layer 1). No side effects: no database write, no ref / index /
work-tree change (every git read runs with `--no-optional-locks` through
dct.core.gitinfo.git_read). The one git write is future_tree's — the tree
of a work-in-progress commit, built in a TEMPORARY index for the plan's
changelog check (work_changelog, K-51) and the preview: it only adds
unreferenced objects. Usable by every project — also one whose own
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
    version-file contract (K-73); the changelog file (`changelog_file`,
    #876 — core.changelog_file: detected among the candidate's tracked
    files, with the problem prepare / apply refuse before the stamp;
    never stored; with uncommitted work checked at the future tree of the
    work-in-progress commit, as the preview checks it — work_changelog);
  - the SNAPSHOT (K-80) — the immutable approved context (candidate, base,
    V, selection, proof, entry states) and its digest, which `dct release
    apply --plan-digest` re-derives for the candidate it pins. A resume
    plan (K-95 C5) shows the recorded release instead and computes nothing
    on today's HEAD;
  - the version suggestion (`version.suggested`, suggest_version) for the
    snapshot's selection — OUTSIDE the snapshot, so the digest never
    depends on it;
  - `pending_consent` (pending_consent): the project's preview slot as a
    dialog routes it, with the ONE "can it still be finished here"
    predicate of `apply --consent` (consent_adoption: the ref, then
    adoptable_chain, then the recorded progress) and, when it cannot,
    `blocked` (why, and the step that works instead).
commit_candidates(engine, project, ...) gives /commit its candidate lists
(K-53): confirmed (client provenance of THIS checkout) vs uncertain.

Both return JSON-native dicts whose top-level keys are PLAN_KEYS /
CANDIDATE_KEYS (pinned by a schema test; SCHEMA_VERSION moves when a key's
meaning changes). Schema: docs/release-workflow.md.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa

from dct.core import gitinfo
from dct.core.changelog import entry_state_digest, list_entries, normalize_worktree
from dct.core.changelog_file import (
    EXPLICIT_MARKER,
    check_work_tree,
    explain,
    resolve_changelog,
)
from dct.core.projects import get_project
from dct.core.releases import (
    PREVIEW_VARIANTS,
    get_preview,
    get_version_files,
    record_versions,
    release_routing,
    validate_proof,
)
from dct.core.version import (
    bump,
    parse_version,
    same_version,
    storable_version,
    version_key,
)
from dct.db import changelog_entries, projects
from dct.workflows.release.proof import (
    ATTEST_SUBJECT,
    TRAILER_FORMAT,
    Commit,
    RangeProof,
    attest_message,
    lost_trailer_ids,
    merge_pr,
    message_subject,
    parse_revert,
    proven_ids,
    range_proof,
    squash_signals,
    suffix_pr,
)

SCHEMA_VERSION = 5
PLAN_KEYS = (
    "schema_version", "project", "kind", "version", "checkout", "executor",
    "unfinished_release", "range", "entries", "foreign_citations", "reverts",
    "squash_commits", "attestation_groups", "proof", "index", "version_files",
    "changelog_file", "warnings", "resume", "snapshot", "snapshot_digest",
    "pending_consent",
)
# K-116 H8 — the most commits the range resolution reads from the ancestry
# graph of a candidate (read_ancestry). Past it the plan REFUSES; it never
# works on a cut graph. Documented in docs/release-workflow.md.
ANCESTRY_MAX_COMMITS = 1_000_000
CANDIDATE_KEYS = (
    "schema_version", "project", "checkout", "range", "confirmed", "uncertain",
    "other_branch", "already_proven", "staged_files", "secret_like", "warnings",
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
    commit), version (normalized)}], sorted by name. Other tags are ignored.
    Built on gitinfo.all_version_tags (`merged=candidate`) — the ONE reader
    of version tags."""
    return [{"name": t["name"], "sha": t["commit"], "version": t["version"]}
            for t in gitinfo.all_version_tags(cwd, merged=candidate)
            if t["name"] == t["name"].strip()]


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


# ── secret-like paths (shared by /commit and the release preview) ────────────

# Paths a work-in-progress commit never takes along and /commit never offers
# for staging — matched on the file NAME; `.env.*` templates (*.example,
# *.sample) are not secrets.
SECRET_LIKE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12",
                        "credentials*.json")
SECRET_LIKE_TEMPLATES = ("*.example", "*.sample")


def secret_like(path: str) -> bool:
    """Whether the repository path `path` looks like a secret by its file
    NAME (SECRET_LIKE_PATTERNS; an `.env.*` template — SECRET_LIKE_TEMPLATES
    — is not one). The ONE predicate of the release preview (refusal
    `secret_like`, details `left_out`) and of /commit's candidates
    (commit_candidates `secret_like`). A path policy, never a content
    scanner: a file whose name it does not match may still hold a secret."""
    name = path.rsplit("/", 1)[-1]
    if fnmatch.fnmatchcase(name, ".env.*") and any(
            fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_LIKE_TEMPLATES):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_LIKE_PATTERNS)


def _git_quote(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def secret_like_changes(repo_root: str, state: dict, head: str | None) -> list[dict]:
    """The changed files of `state` (index_state: staged, unstaged and
    untracked) whose path is secret_like → [{path, staged, unstage}]
    sorted by path. `staged` — the index already holds the change (a commit
    would take it along); `unstage` — for a staged one, the quoted command
    that takes it out of the index again without touching the file (`git
    restore --staged -- <path>`; `git rm --cached -- <path>` before the
    first commit, `head` None), else None. Nothing is ever staged or
    unstaged here."""
    staged = set(state["staged"])
    changed = staged | set(state["unstaged"]) | set(state["untracked"])
    out = []
    for path in sorted(p for p in changed if secret_like(p)):
        command = None
        if path in staged:
            command = (_git_quote("git", "restore", "--staged", "--", path) if head
                       else _git_quote("git", "rm", "--cached", "-q", "--", path))
        out.append({"path": path, "staged": path in staged, "unstage": command})
    return out


# ── the work-in-progress commit's future tree (shared with the preview) ──────
# ONE implementation of the tree T1 a work-in-progress commit would have:
# the preview builds its synthetic commit C* on it (effects._work_state) and
# the plan checks the changelog file against it (work_changelog, K-51).

# The `git add` into the temporary index of future_tree hashes every path of
# the scope (a large change takes a while).
FUTURE_TREE_TIMEOUT_SECONDS = 600


def status_v2(repo: str) -> dict:
    """`git status --porcelain=v2 -z --untracked-files=all --no-renames`,
    read-only (no index refresh): {"tracked": {path: {"xy", "sub"}},
    "untracked": [path], "conflicted": [path]}."""
    _, out = gitinfo.git_read(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all",
                              "--no-renames")
    tracked: dict[str, dict] = {}
    untracked, conflicted = [], []
    for record in (r for r in out.split("\0") if r):
        kind = record[0]
        if kind == "1":
            fields = record.split(" ", 8)
            tracked[fields[8]] = {"xy": fields[1], "sub": fields[2]}
        elif kind == "u":
            conflicted.append(record.split(" ", 10)[10])
        elif kind == "?":
            untracked.append(record[2:])
    return {"tracked": tracked, "untracked": sorted(untracked), "conflicted": sorted(conflicted)}


def tree_entries(repo: str, commit: str, paths: Iterable[str]) -> dict[str, tuple[str, str, str]]:
    """{path: (mode, blob, "0")} of `paths` in `commit` (absent paths left out)
    — comparable with the index entries of `git ls-files -s`."""
    paths = sorted(paths)
    if not paths:
        return {}
    _, out = gitinfo.git_read(repo, "ls-tree", "-z", commit, "--", *paths)
    found = {}
    for entry in (e for e in out.split("\0") if e):
        meta, _, rel = entry.partition("\t")
        mode, _kind, blob = meta.split()
        found[rel] = (mode, blob.lower(), "0")
    return found


def work_scope(status: dict, changelog: str | None, version_files: list[str] | None,
               included: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """(scope, auto) of the work-in-progress commit: the scope Pₛ is every
    tracked changed path of `status` (status_v2), the untracked RELEASE
    ARTIFACTS (`auto` — the changelog file `changelog` and the version
    files `version_files`, _artifacts) and `included` (repo-relative
    untracked paths the caller already checked). Both sorted."""
    untracked = set(status["untracked"])
    auto = sorted(a["path"] for a in _artifacts(changelog, version_files)
                  if a["path"] in untracked)
    return sorted(set(status["tracked"]) | set(auto) | set(included)), auto


def filter_attributed(repo: str, paths: Iterable[str]) -> list[str]:
    """The paths of `paths` present in the work tree whose `filter`
    attribute is set (LFS, clean / smudge) — what `git add` would run a
    filter program on, so no future tree is built over them; sorted."""
    present = [p for p in paths if os.path.lexists(os.path.join(repo, *p.split("/")))]
    if not present:
        return []
    _, attrs = gitinfo.git_read(repo, "check-attr", "-z", "filter", "--", *present)
    tokens = attrs.split("\0")
    return sorted(tokens[i] for i in range(0, len(tokens) - 2, 3)
                  if tokens[i + 2] not in ("unspecified", "unset"))


def future_tree(repo: str, head: str, scope: list[str],
                present: Callable[[str], bool] | None = None, *,
                timeout: float = FUTURE_TREE_TIMEOUT_SECONDS) -> str:
    """T1 — the tree a commit of `scope` would have: in a TEMPORARY index,
    `read-tree <head>`, `git add -A -- <scope>` (literal pathspecs; the
    paths in `head` or present in the work tree), `write-tree`. The user's
    index, refs and work tree are never written; the only trace is the
    unreferenced objects `git add` / `write-tree` store."""
    exists = present or (lambda rel: os.path.lexists(os.path.join(repo, *rel.split("/"))))
    in_head = tree_entries(repo, head, scope)
    addable = [p for p in scope if p in in_head or exists(p)]
    with tempfile.TemporaryDirectory(prefix="dct-release-preview-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", head, index_file=index)
        if addable:
            gitinfo.git_write(repo, "--literal-pathspecs", "add", "-A", "--", *addable,
                              index_file=index, timeout=timeout)
        _, tree = gitinfo.git_write(repo, "write-tree", index_file=index)
    return tree.strip().lower()


def work_changelog(project: dict, found: dict, *, project_root: str, repo_root: str,
                   head: str, version_files: list[str] | None, warnings: list[str]) -> dict:
    """K-51 — the changelog file of a NEW release as the preview will check
    it: `found` (resolve_changelog at `head`) when the work tree is clean,
    else the resolution AT THE FUTURE TREE T1 of the work-in-progress commit
    (work_scope + future_tree — the preview's own scope and tree, its
    candidate C* has exactly this tree), each through check_work_tree. A
    difference the work-in-progress commit settles (an uncommitted edit of
    the changelog, a conversion to Markdown, a link replaced by a file) is
    then no problem; one it does not settle (an ignored file at the path,
    skip-worktree, a file outside the scope) still is. When a path of the
    scope carries a `filter` attribute the preview commits nothing (refusal
    `filtered_paths`), so nothing settles the difference: the check is
    against `head`. So it is when git cannot build T1 (a read-only object
    store): a warning in `warnings` says so. Raises
    gitinfo.CheckoutVerificationError when a git read fails."""
    slug = project["slug"]
    if not found.get("managed"):
        return found
    scope, _ = work_scope(status_v2(repo_root), found["path"], version_files)
    if not scope or filter_attributed(repo_root, scope):
        return check_work_tree(found, repo_root=repo_root, commit=head, slug=slug)
    try:
        tree = future_tree(repo_root, head, scope)
    except gitinfo.GitWriteError as exc:
        warnings.append(f"the tree of the work-in-progress commit could not be built ({exc}) "
                        f"— the changelog file is checked against HEAD")
        return check_work_tree(found, repo_root=repo_root, commit=head, slug=slug)
    at_work = resolve_changelog(project, project_root=project_root, repo_root=repo_root,
                                commit=tree)
    return check_work_tree(at_work, repo_root=repo_root, commit=tree, slug=slug)


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


# ── the version suggestion (plan schema 5) ───────────────────────────────────

# The version of a project's first release (no current version, no ancestor
# release tag).
FIRST_VERSION = "0.1.0"
# How many taken versions a suggestion skips (one patch each) before it
# gives up: past it `target` is null and the owner types a version.
SUGGEST_MAX_STEPS = 100
_BUMPS = ("patch", "minor", "major")


def _category_rule(categories: set[str], zero: bool) -> tuple[str, str, bool]:
    """(bump, reason, safe) of the selected categories — precedence
    `removed` > `added` = `changed` > `fixed`; a 0.x project's breaking
    change is a minor bump (never 1.0.0)."""
    if "removed" in categories:
        return ("minor", "breaking_0x", True) if zero else ("major", "breaking", False)
    if categories & {"added", "changed"}:
        return "minor", "features", True
    if "fixed" in categories:
        return "patch", "fixes_only", True
    return "patch", "version_only", True


def suggest_version(current: str | None, categories: Iterable[str], base_tag: str | None,
                    taken: Callable[[str], bool]) -> dict:
    """The version dct proposes for a release of `categories` (the
    categories of the SELECTED entries) — pure rules, no git, no database.

    `current` = the project's current_version (None = none stored);
    `base_tag` = the version of the plan's range base tag (the nearest
    ancestor release tag, normalized), used only without a current version;
    `taken(V)` = True when V exists already (a local tag of V in any
    spelling, a release record of V in any status).

    Returns {bump, target, reason, safe, taken: [V], alternatives: [{bump,
    target}]}:
      - no current version, no base tag → 0.1.0, bump null, reason
        `first_version`, safe, no alternatives;
      - no current version, a base tag → the category rule applied to the
        tag's version, reason `after_tag`, NOT safe;
      - a current version outside the version contract → target and bump
        null, reason `current_unparseable`, not safe (`taken` never asked);
      - else the category rule (_category_rule): `version_only`,
        `fixes_only`, `features` (safe), `breaking` (major, NOT safe) or
        `breaking_0x` (minor, safe).
    A taken target moves to the next patch above it, up to
    SUGGEST_MAX_STEPS times (each skipped version listed in `taken`, reason
    `tag_taken`, not safe, `bump` unchanged); past that `target` is null.
    `alternatives`: the other bumps of the base, ascending — for a 0.x base
    only patch / minor, so neither the target nor an alternative of a 0.x
    project is ever 1.0.0 or above (the owner types such a version) — taken
    ones left out."""
    categories = set(categories)
    result = {"bump": None, "target": None, "reason": "current_unparseable", "safe": False,
              "taken": [], "alternatives": []}
    if current is None and base_tag is None:
        result.update(target=FIRST_VERSION, reason="first_version", safe=True)
        return _skip_taken(result, taken)
    base = current if current is not None else base_tag
    try:
        zero = parse_version(base)[0] == 0
    except ValueError:
        return result
    kind, reason, safe = _category_rule(categories, zero)
    if current is None:
        reason, safe = "after_tag", False
    result.update(bump=kind, target=bump(base, kind), reason=reason, safe=safe)
    _skip_taken(result, taken)
    for other in _BUMPS:
        if other == kind or (zero and other == "major"):
            continue
        version = bump(base, other)
        if not taken(version) and not same_version(version, result["target"]):
            result["alternatives"].append({"bump": other, "target": version})
    return result


def _skip_taken(result: dict, taken: Callable[[str], bool]) -> dict:
    """Move `result["target"]` past the taken versions (one patch each, at
    most SUGGEST_MAX_STEPS; then null) — see suggest_version."""
    target, skipped = result["target"], []
    while taken(target):
        if len(skipped) == SUGGEST_MAX_STEPS:
            target = None
            break
        skipped.append(target)
        target = bump(target, "patch")
    if skipped:
        result.update(target=target, reason="tag_taken", safe=False, taken=skipped)
    return result


def _taken_versions(engine: sa.Engine, project_slug: str,
                    repo_root: str | None) -> Callable[[str], bool]:
    """`taken` of suggest_version for the plan: the local version tags of
    the repository (gitinfo.all_version_tags — ONE read, whatever commit
    they point at) and the versions of every live release record of the
    project (any status, abandoned included) and the versions its live
    RELEASED changelog entries carry (imported history without a record or
    a tag — the stamp refuses those versions too), compared under the
    version contract (1.5 == 1.5.0 == v1.5)."""
    known = [t["version"] for t in gitinfo.all_version_tags(repo_root)] if repo_root else []
    known += record_versions(engine, project_slug)
    known += _released_entry_versions(engine, project_slug)
    keys, legacy = set(), set()
    for value in known:
        try:
            keys.add(version_key(value))
        except ValueError:
            legacy.add(value)  # a stored version from before the contract
    return lambda version: version_key(version) in keys or version in legacy


def _released_entry_versions(engine: sa.Engine, project_slug: str) -> list[str]:
    """The distinct versions of the project's live released changelog
    entries, as stored (one SELECT DISTINCT)."""
    c = changelog_entries.c
    with engine.begin() as conn:
        return list(conn.execute(
            sa.select(c.version).distinct()
            .select_from(changelog_entries.join(projects, projects.c.id == c.project_id))
            .where(projects.c.slug == project_slug, c.deleted_at.is_(None),
                   c.version.is_not(None))
        ).scalars())


def _suggestion(engine: sa.Engine, plan: dict, entries: list[dict],
                repo_root: str | None) -> dict | None:
    """`version.suggested` of a NEW release plan — computed for the
    snapshot's selection (the categories of its selected ids), OUTSIDE the
    snapshot, so the plan digest does not depend on it. None for a project
    whose own release tool picks the version (executor `own`)."""
    if plan["executor"]["project"] == "own":
        return None
    selected = set(plan["snapshot"]["selected_ids"])
    categories = {e["category"] for e in entries if e["id"] in selected}
    base_tag = (plan["range"] or {}).get("base_version")
    return suggest_version(plan["version"]["current"], categories, base_tag,
                           _taken_versions(engine, plan["project"], repo_root))


# ── the consent of the one-dialog release (`apply --consent`, §E2 / §F) ─────


def _commit_view(repo: str, commit: str) -> dict:
    """{parents, tree, subject (git's %s, message_subject), proves} of
    `commit` — the facts adoption compares (read-only)."""
    [found] = read_commits(repo, "-1", commit)
    _, tree = gitinfo.git_read(repo, "rev-parse", f"{commit}^{{tree}}")
    return {"parents": list(found.parents), "tree": tree.strip().lower(),
            "subject": message_subject(found.message), "proves": sorted(proven_ids(found))}


def _tree_of(repo: str, commit: str) -> str:
    _, tree = gitinfo.git_read(repo, "rev-parse", f"{commit}^{{tree}}")
    return tree.strip().lower()


def _stripspace(text: str) -> str:
    """`git stripspace` (no comment stripping): trailing whitespace of each
    line dropped, runs of blank lines collapsed to one, leading and
    trailing blank lines dropped, one final newline."""
    out: list[str] = []
    blank = False
    for line in text.split("\n"):
        line = line.rstrip(" \t\n\v\f\r")
        if not line:
            blank = bool(out)
            continue
        if blank:
            out.append("")
        out.append(line)
        blank = False
    return "\n".join(out) + "\n" if out else ""


def _message_digests(repo: str, commit: str) -> set[str]:
    """sha256 of `commit`'s message as stored and as `git stripspace`
    cleans it — what a work commit's message_sha256 is compared with (the
    preview stored the message cleaned by the repository's commit.cleanup:
    default → stripspace, verbatim → as it is). Read-only."""
    _, raw = gitinfo.git_read(repo, "cat-file", "commit", commit)
    message = raw.partition("\n\n")[2]
    return {hashlib.sha256(text.encode("utf-8")).hexdigest()
            for text in (message, _stripspace(message))}


def commit_exists(repo: str, commit: str) -> bool:
    """`commit` resolves to a commit object here (`git cat-file -e`) —
    False for one reset away and pruned by `git gc`. Read-only."""
    code, _ = gitinfo.git_read(repo, "cat-file", "-e", f"{commit}^{{commit}}", ok_codes=(0, 1, 128))
    return code == 0


def adoptable_chain(repo: str, base_head: str, view: dict, head: str,
                    made: dict | None = None) -> dict | None:
    """The ONE adoption predicate of `apply --consent` (contract §E2 step
    2) — which of the commits the consent view `view` approves `head`
    already IS, decided by the commits themselves: {"work_commit": W |
    None, "attest_commit": A | None} when `head` is the base H0 ({None,
    None}), an adoptable W or an adoptable A (on W, or on H0 without work:
    an empty tree delta, ATTEST_SUBJECT, proven ids == attest.ids exactly,
    K-107); None for any other `head`. A W is adoptable with parents ==
    [H0], tree == work.tree, subject == work.subject and proven ids ==
    work.proves; when it is NOT the work commit dct made (`made` — the
    slot's recorded progress, or the commits a run made — names another or
    none: a plain `git commit`), its message must also be the approved one
    (message_sha256 of the message as stored or as `git stripspace` cleans
    it) — a body the screen never showed is not adopted. The same holds for
    an A dct did not make (not `made`'s attestation commit): its message
    must be attest_message(attest.ids), as stored or as `git stripspace`
    cleans it. A hook's addition to the message of the W or A dct made (a
    Signed-off-by) stays adoptable. Read-only."""
    if head == base_head:
        return {"work_commit": None, "attest_commit": None}
    work, attest = view.get("work"), view.get("attest")
    ours = (made or {}).get("work_commit")
    ours_attest = (made or {}).get("attest_commit")

    def is_work(commit: str) -> bool:
        facts = _commit_view(repo, commit)
        if not (facts["parents"] == [base_head] and facts["tree"] == work["tree"]
                and facts["subject"] == work["subject"] and facts["proves"] == work["proves"]):
            return False
        return commit == ours or work["message_sha256"] in _message_digests(repo, commit)

    facts = _commit_view(repo, head)
    if attest is not None and facts["subject"] == ATTEST_SUBJECT \
            and facts["proves"] == attest["ids"] and len(facts["parents"]) == 1:
        parent = facts["parents"][0]
        if facts["tree"] != _tree_of(repo, parent):
            return None
        approved = hashlib.sha256(attest_message(attest["ids"]).encode("utf-8")).hexdigest()
        if head != ours_attest and approved not in _message_digests(repo, head):
            return None
        if work is None:
            return {"work_commit": None, "attest_commit": head} if parent == base_head else None
        return {"work_commit": parent, "attest_commit": head} if is_work(parent) else None
    if work is not None and is_work(head):
        return {"work_commit": head, "attest_commit": None}
    return None


ADOPTION_FIELDS = ("work_commit", "attest_commit")


def consent_adoption(repo: str, base: dict, view: dict, head: str, ref: str | None,
                     made: dict | None) -> dict:
    """The ONE "can this approved release still be finished here" predicate
    of a preview slot's consent view `view` — used by the plan's
    pending_consent and by `apply --consent` (before any commit, after the
    work-in-progress commit, after the attestation commit and right before
    the stamp), so the plan offers "Finish" exactly when apply finishes.
    `base` is the slot's base {head (H0), ref}, `head` / `ref` the commit and
    the ref HEAD names NOW (gitinfo.head_ref — None when detached), `made`
    the commits recorded (the slot's progress, or what the run made).

    → {"chain": adoptable_chain's answer or None, "why": None |
    "branch_switched" (HEAD names another ref than at the preview — also a
    detached HEAD where a branch was, or a branch where HEAD was detached,
    on the same commit: another context) | "head_moved" (HEAD is neither H0
    nor a commit this consent approves) | "progress_lost" (a recorded
    commit is not the one HEAD's chain names — reset away, maybe pruned),
    "lost": [(field, sha)] of the recorded commits not in the chain}. In
    that order: the ref first. Read-only."""
    out: dict = {"chain": None, "why": None, "lost": []}
    if ref != base["ref"]:
        out["why"] = "branch_switched"
        return out
    chain = adoptable_chain(repo, base["head"], view, head, made)
    if chain is None:
        out["why"] = "head_moved"
        return out
    lost = [(field, sha) for field in ADOPTION_FIELDS
            if (sha := (made or {}).get(field)) and chain.get(field) != sha]
    if lost:
        out.update(why="progress_lost", lost=lost)
        return out
    out["chain"] = chain
    return out


def _ref_label(ref: str | None) -> str:
    return ref.removeprefix("refs/heads/") if ref else "a detached HEAD"


DISCARD_PREVIEW_STEP = ("discard the preview (`dct release prepare --discard-preview`) and "
                        "preview again")


def switch_back_command(repo: str, base: dict, view: dict | None,
                        made: dict | None) -> str | None:
    """The ONE `git switch` command that puts HEAD back where an approved
    release continues after a branch switch (K-42) — for the plan's
    `blocked.next_step` and for apply's `branch_switched` refusal. A preview
    made on a branch: `git switch <branch>`. One made on a DETACHED HEAD:
    `git switch --detach <the verified progress commit>` — the recorded A,
    else the recorded W, else the base H0 — only when HEAD there passes
    consent_adoption for `view` with every recorded commit in its chain
    (never H0 once a commit was made: that drops it from the chain and the
    re-run refuses `progress_lost`). None when no such commit exists (reset
    away, pruned): switching back cannot help. Read-only."""
    if base["ref"]:
        return f"git switch {shlex.quote(_ref_label(base['ref']))}"
    made = made or {}
    commit = made.get("attest_commit") or made.get("work_commit") or base["head"]
    if view is None or not commit or not commit_exists(repo, commit):
        return None
    if consent_adoption(repo, base, view, commit, None, made)["why"] is not None:
        return None
    return f"git switch --detach {commit[:12]}"


def _adoption_next_step(why: str, slot: dict, head_ref: str | None,
                        back: str | None = None) -> str:
    """The step that works when a release approved earlier cannot be
    finished here — never "finish it" (apply would refuse). `back` is
    switch_back_command's answer for a `branch_switched` slot."""
    expected = slot["base"]["ref"]
    if why == "other_checkout":
        return (f"release it in {slot['base']['checkout_root']}, where it was previewed — "
                f"or preview again here (the approval made there does not apply here)")
    if why == "branch_switched":
        if back is None:
            return (f"HEAD is on {_ref_label(head_ref)}, the release was approved on "
                    f"{_ref_label(expected)}, and the commit made under the approval can no "
                    f"longer be reached: {DISCARD_PREVIEW_STEP}")
        return (f"HEAD is on {_ref_label(head_ref)}, the release was approved on "
                f"{_ref_label(expected)}: switch back (`{back}`), then the approved release "
                f"can be finished — or preview again on {_ref_label(head_ref)}")
    if why == "progress_lost":
        return (f"the commit made under the approval is no longer what HEAD stands on: "
                f"{DISCARD_PREVIEW_STEP}")
    return ("HEAD moved past what the approval covers: preview again (`dct release prepare "
            "--preview`) — the commits already made stay")


def pending_consent(engine: sa.Engine, project_slug: str, checkout: dict | None,
                    warnings: list[str] | None = None) -> dict | None:
    """`pending_consent` of the plan — the project's preview slot as a
    dialog routes it (no digest of a screen nobody approved): null without
    a slot, else {preview_id, created_at, base_head, checkout_root (the
    checkout the preview was made in), variants: [{name, version}] (in the
    preview's order), progress: {work_commit, attest_commit}, applied
    (null, or — once a commit was recorded under the owner's Release — the
    variant it was made under: {name, version, entries: [the selected
    ids], attest: [the ids its empty commit marks as done]}, so the Finish
    question can say what it releases; no digest), adoptable, consent_digest,
    blocked}.

    adoptable — apply's own predicate (consent_adoption: the ref HEAD names,
    a detached HEAD included, then the adoptable chain, then the recorded
    progress): without recorded progress, HEAD is a commit one variant
    approves (a plain `git commit` of T1) on the preview's ref; with it,
    HEAD's chain under the applied C names EVERY recorded commit on the
    preview's ref (a replacement such as W amended --no-edit, a reset to the
    base, a switched branch are not adoptable). consent_digest — the C the
    progress was made under (progress.consent) ONLY when a commit was
    recorded and adoptable is true; else null. blocked — null when adoptable
    or when no commit was recorded (a draft: the next preview replaces it);
    else why the release approved earlier cannot be finished HERE and what
    works instead: {why: "other_checkout" | "branch_switched" | "head_moved"
    | "progress_lost" (apply's base_moved.why), head, head_ref (None when
    detached), expected_ref (the preview's; None when it was detached),
    next_step (one plain sentence — never "finish"; for `branch_switched`
    the switch_back_command that works: the preview's branch, or for a
    detached preview the verified progress commit)}. A recorded commit that
    no longer resolves (reset away, pruned by `git gc`) is not adoptable:
    no digest, `progress_lost`, and a warning naming it is appended to
    `warnings`. Never raises for it."""
    slot = get_preview(engine, project_slug)
    if slot is None:
        return None
    order = {name: i for i, name in enumerate(PREVIEW_VARIANTS)}
    variants = sorted(slot["variants"].items(), key=lambda kv: order[kv[1]["name"]])
    progress = slot["progress"]
    made = {"work_commit": progress["work_commit"], "attest_commit": progress["attest_commit"]}
    recorded = {field: sha for field, sha in made.items() if sha}
    applied = progress.get("consent")
    base_head = slot["base"]["head"]
    adoptable, digest, why = False, None, None
    head = head_ref = None
    here = checkout is not None and slot["base"]["checkout_root"] == normalize_worktree(
        checkout["repo_root"])
    if not here:
        why = "other_checkout"
    elif base_head:
        repo, head = checkout["repo_root"], checkout["head"]
        head_ref = gitinfo.head_ref(repo)
        gone = [sha for sha in made.values() if sha and not commit_exists(repo, sha)]
        for sha in gone:
            if warnings is not None:
                warnings.append(
                    f"the commit {sha[:12]} recorded as made under the preview "
                    f"{slot['id']} no longer exists (reset away and pruned?) — it cannot be "
                    f"adopted; preview again, or discard the preview "
                    f"(`dct release prepare --discard-preview`)")
        if gone:
            why = "progress_lost"
        elif not recorded:
            adoptable = any(
                (found := consent_adoption(repo, slot["base"], item["consent"], head, head_ref,
                                           made))["why"] is None
                and any(found["chain"].values())
                for _, item in variants)
        elif applied in slot["variants"]:
            found = consent_adoption(repo, slot["base"], slot["variants"][applied]["consent"],
                                     head, head_ref, made)
            adoptable, why = found["why"] is None, found["why"]
            digest = applied if adoptable else None
    chosen = slot["variants"].get(applied) if any(made.values()) else None
    blocked = None
    if recorded and not adoptable:
        why = why or "head_moved"
        back = None
        if why == "branch_switched":
            view = (slot["variants"].get(applied) or {}).get("consent")
            back = switch_back_command(checkout["repo_root"], slot["base"], view, made)
        blocked = {"why": why, "head": head, "head_ref": head_ref,
                   "expected_ref": slot["base"]["ref"],
                   "next_step": _adoption_next_step(why, slot, head_ref, back)}
    return {
        "preview_id": slot["id"], "created_at": slot["created_at"], "base_head": base_head,
        "checkout_root": slot["base"]["checkout_root"],
        "variants": [{"name": item["name"], "version": item["consent"]["version"]["target"]}
                     for _, item in variants],
        "progress": made,
        "applied": None if chosen is None else {
            "name": chosen["name"], "version": chosen["consent"]["version"]["target"],
            "entries": list(chosen["consent"]["entries"]["selected"]),
            "attest": list((chosen["consent"]["attest"] or {}).get("ids") or [])},
        "adoptable": adoptable, "consent_digest": digest, "blocked": blocked,
    }


def _artifacts(changelog: str | None, version_files: list[str] | None) -> list[dict]:
    """Release artifacts as repo-relative paths: the changelog file
    (`changelog` — the path core.changelog_file.resolve_changelog resolved;
    None when dct manages none or the resolution has a problem) and the
    version-file contract (repo-relative, K-73)."""
    out = []
    if changelog:
        out.append({"path": changelog, "role": "changelog"})
    for path in version_files or []:
        out.append({"path": path, "role": "version_file"})
    return out


def _changelog_file(found: dict, warnings: list[str]) -> dict:
    """The plan's `changelog_file` (the resolution, #876) — a problem (what
    prepare / apply refuse before the stamp) or other changelog-like files
    beside the chosen one add a warning."""
    if found["problem"]:
        warnings.append(f"changelog file: {explain(found)} — prepare and apply refuse until "
                        f"it is settled")
    elif found["managed"] and len(found["candidates"]) > 1:
        others = [c["path"] for c in found["candidates"] if c["path"] != found["path"]]
        warnings.append(f"the changelog file is {found['path']} ({found['source']}); the other "
                        f"changelog-like file(s) {', '.join(others)} are not written")
    return found


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


def restore_commands(project: dict, approved: str | None, *, project_root: str | None,
                     repo_root: str | None, commit: str | None) -> list[str]:
    """K-16 / K-23 — the `dct project …` commands after which the ONE
    resolver yields the APPROVED changelog file (`approved`, base-relative;
    None = dct's changelog file was off) at the record's candidate `commit`
    again, derived from the stored proposal. The proposal does not record
    WHICH setting produced the path (detection or a manual choice), so this
    is a setting CONSISTENT with the approved path, never "the setting of
    the approval": the current setting when it already yields it (only the
    switch flipped back — `on` / `off`), else the MANUAL choice of the
    approved file (project-relative) — never a switch to detection, which
    would silently drop a manual mode. Each command is offered only when
    the resolution it leads to verifiably gives `approved`. [] when none
    does — e.g. a record stamped by the pre-fix code whose approved file the
    resolver can never yield (K-18): then abandon is the only way, never a
    false promise."""
    if not project_root:
        return []

    def yields(**fields) -> bool:
        found = resolve_changelog({**project, **fields}, project_root=project_root,
                                  repo_root=repo_root, commit=commit if repo_root else None)
        return found["path"] == approved if found["managed"] else approved is None

    slug = shlex.quote(project["slug"])
    if approved is None:
        return [f"dct project changelog {slug} off"] if yields(manages_changelog=False) else []
    switch = [] if project.get("manages_changelog", True) else [
        f"dct project changelog {slug} on"]
    if yields(manages_changelog=True):
        return switch
    prefix = ""
    if repo_root:
        rel = os.path.relpath(project_root, repo_root).replace(os.sep, "/")
        prefix = "" if rel == "." else rel + "/"
    if not approved.startswith(prefix):
        return []
    rel = approved[len(prefix):]
    if yields(manages_changelog=True, changelog_path=rel, **{EXPLICIT_MARKER: True}):
        return switch + [f"dct project changelog-path {slug} {shlex.quote(rel)}"]
    return []


def resume_block(project: dict, record: dict, found: dict | None, *,
                 project_root: str | None, repo_root: str | None) -> dict | None:
    """K-34 — the ONE read-only predicate of whether `dct release resume`
    can write the changelog file of the unfinished `record`: used by the
    plan's resume section (`resume.blocked`) AND by resume itself
    (effects._record_proposal refuses with exactly this block), so the two
    never disagree. Stage-aware: only while the record is `stamped` does
    its APPROVED proposal bind the changelog file (resume writes it from
    that proposal); past `stamped` the verified manifest binds it and a
    setting changed since blocks nothing. `found` is the resolution
    (core.changelog_file.resolve_changelog) at the record's candidate — None
    without a project root. None when resume can continue; else {approved
    (the path the proposal binds; None = off), now (what the candidate
    resolves today), restore (the `dct project …` commands that make the
    candidate resolve the approved file again — [] when none verifiably
    does), resume, abandon (the commands of the other ways), message (the
    refusal resume raises)}. A record without a proposal is not reported
    here (resume refuses it on its own, naming the ledger path / takeover)."""
    proposal = record.get("proposal")
    if record.get("status") != "stamped" or proposal is None:
        return None
    approved = (proposal.get("changelog") or {}).get("path")
    if approved == (found or {}).get("path"):
        return None
    # #876 / K-16: the approved file is not the one the resolver finds in the
    # candidate now — a changelog setting changed since the approval (or a
    # record stamped before dct detected the changelog file, K-18). Resume
    # executes ONLY the approved proposal (K-97 F4), so it writes neither
    # file; the way back is a setting that resolves the approved file again
    # — offered only when it verifiably does.
    if found is None or not found["managed"]:
        now = "none (dct's changelog file is off)"
    elif found["path"]:
        now = f"{found['path']} ({found['source']})"
    else:
        now = f"not settled — {explain(found)}"
    version = record["version"]
    restore = restore_commands(project, approved, project_root=project_root,
                               repo_root=repo_root, commit=record.get("candidate_sha"))
    resume = "dct release resume"
    abandon = f"dct release abandon --version {shlex.quote(version)} --reason … --confirm"
    ways = []
    if restore:
        ways.append(
            f"(1) set a changelog setting consistent with the approved path "
            f"{approved or 'none (off)'}: "
            + ", then ".join(f"`{c}`" for c in restore)
            + f" — the candidate then resolves {approved or 'no changelog file'} again "
            f"(which setting the approval was made under is not recorded) — and run "
            f"`{resume}`")
    ways.append(
        f"({len(ways) + 1}) {'as the last resort, ' if restore else ''}"
        f"`{abandon}` ends the record — its stamps and current_version stay, the version "
        f"stays taken; its section is then not in the changelog unless you add it yourself")
    message = (
        f"the approved proposal of {version} binds the changelog file "
        f"{approved or 'none (off)'}, but the candidate's changelog file is {now}. Resume "
        f"executes only the approved proposal, so it writes neither file (both are "
        f"untouched); nothing done. Next step: {'; '.join(ways)}.")
    return {"approved": approved, "now": now, "restore": restore,
            "resume": resume if restore else None, "abandon": abandon, "message": message}


def _resume_section(record: dict, head: str | None, views: list[dict],
                    blocked: dict | None) -> dict:
    """K-95 C5 — what a resume continues: the RECORDED candidate, entry
    set, proof and effect state (by status), never recomputed on today's
    HEAD; the current HEAD and the unreleased entries added since are shown
    beside it, separately. `blocked` (K-34): resume_block — None when
    resume can continue."""
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
        "blocked": blocked,
    }


def range_notice(rng: dict | None, current: str | None, *, bound: bool = False) -> str | None:
    """K-36 — THE notice of a proof range whose base needs the owner's
    confirmation (`range.confirm_required`): the base tag, the equally near
    alternatives and the current version it differs from. In the PLAN
    (`bound` False) it asks to confirm the range — the consent screen is the
    one place that asks; prepare and apply, whose plan digest already binds
    the range, replace exactly this notice with the `bound` form, which is
    information only (never another request to confirm). None when the
    range needs no confirmation."""
    if not rng or not rng.get("confirm_required"):
        return None
    tied = [a["name"] for a in rng["alternatives"] if a["sha"] != rng["base_sha"]]
    text = (f"the proof range starts at {rng['base_tag']}"
            + (f", equally near: {', '.join(tied)}" if tied else "")
            + (f", not at v{current}" if not same_version(rng["base_version"], current) else ""))
    return text + (" — the base the approved plan digest binds" if bound else " — confirm it")


def _resume_plan(plan: dict, record: dict, head: str | None, views: list[dict],
                 blocked: dict | None) -> None:
    """The resume section of `plan`; a known block (K-34) is also a warning:
    the dialog must not offer finishing the release as doable."""
    if blocked is not None:
        plan["warnings"].append(f"`dct release resume` refuses this record: "
                                f"{blocked['message']}")
    plan["resume"] = _jsonable(_resume_section(record, head, views, blocked))


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
    `pending_consent` (pending_consent) is computed only for an unpinned
    plan — the effects layer's own pinned plans route no dialog (null).

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
                 "bump": None if version else bump_type, "suggested": None},
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
    if pinned_checkout is None:  # the effects layer's own plans never route a dialog
        plan["pending_consent"] = _jsonable(pending_consent(engine, project_slug, checkout,
                                                            warnings))
    if checkout is None:
        reason = "no_path" if not path else "no_git"
        plan["checkout"] = {"git": False,
                            "reason": "no_path" if not path else "not_a_repository",
                            "project_root": path}
        warnings.append("the project has no registered path — its checkout cannot be verified"
                        if not path else "no git checkout — entries cannot be proven by commits")
        found = resolve_changelog(project, project_root=path) if path else None
        if found is not None:
            plan["changelog_file"] = _changelog_file(found, warnings)
        if unfinished:
            _resume_plan(plan, unfinished, None, views,
                         resume_block(project, unfinished, found, project_root=path,
                                      repo_root=None))
            return plan
        plan["entries"] = {"proven": [], "unproven": [{**v, "reason": reason} for v in views]}
        if path:
            plan["snapshot"] = _snapshot(plan, entries, _selection(plan, entry_ids, no_entries))
            plan["snapshot_digest"] = snapshot_digest(plan["snapshot"])
            plan["version"]["suggested"] = _suggestion(engine, plan, entries, None)
        return plan

    root, head = checkout["repo_root"], checkout["head"]
    branch = _current_branch(root)
    plan["checkout"] = {
        "git": True, "repo_root": root, "project_root": checkout["project_root"],
        "candidate_sha": head, "head": head, "branch": branch, "detached": branch is None,
    }
    # #876: the changelog file of the CANDIDATE (a resume: the recorded one)
    # — detected among its tracked files, never assumed; read-only, nothing
    # stored. A new release also compares the work tree's file with it.
    found = resolve_changelog(project, project_root=checkout["project_root"], repo_root=root,
                              commit=(unfinished or {}).get("candidate_sha") or head)
    index_artifacts = _artifacts(found["path"],
                                 (plan["version_files"] or {}).get("version_files"))
    if not unfinished and pinned_checkout is None:
        # K-51: checked as the preview will check it — against the tree the
        # approved work-in-progress commit would have, not against HEAD.
        found = work_changelog(project, found, project_root=checkout["project_root"],
                               repo_root=root, head=head,
                               version_files=(plan["version_files"] or {}).get("version_files"),
                               warnings=warnings)
    elif not unfinished:
        found = check_work_tree(found, repo_root=root, commit=head, slug=project["slug"])
    plan["changelog_file"] = _changelog_file(found, warnings)
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
        _resume_plan(plan, unfinished, head, views,
                     resume_block(project, unfinished, found,
                                  project_root=checkout["project_root"], repo_root=root))
        plan["index"] = index_state(root, index_artifacts)
        return plan

    candidate = head
    oldest = min((e["created_at"] for e in entries if e.get("created_at")), default=None)
    rng = resolve_range(root, candidate, current, oldest)
    if rng["confirm_required"]:
        warnings.append(range_notice(rng, current))
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
    plan["version"]["suggested"] = _suggestion(engine, plan, entries, root)
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
      already proves — removed from every list;
    - secret_like: the changed files (staged, unstaged, untracked) whose
      path looks like a secret (secret_like — the release preview's ONE
      predicate, a name heuristic, never a content scanner) →
      secret_like_changes [{path, staged, unstage}]: an unstaged one is
      never offered for staging; a staged one stops the commit, `unstage`
      names the quoted command (dct never changes the index itself).
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
                  secret_like=[], warnings=warnings)
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
    try:
        result["secret_like"] = secret_like_changes(toplevel, index_state(toplevel, []), head)
    except gitinfo.CheckoutVerificationError as exc:
        warnings.append(f"the changed file list could not be read ({exc}) — secret-like paths "
                        f"are not flagged")

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
