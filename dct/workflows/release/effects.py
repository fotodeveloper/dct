"""The release EFFECTS layer (K-72 layer 3) — the default executable release
workflow behind `/release`: `dct release attest / prepare / apply / resume /
abandon / reconstruct` (and `dct clog release`, an alias of prepare + apply
that needs --confirm like apply, K-96 D1).

ONE implementation for the CLI and both clients' skills (they only run the
dialog, K-49): every effect is performed by this module and VERIFIED against
reality before the ledger (dct.core.releases) records the progress — a
status is never taken as proof that an effect happened. The MCP process
never runs this code (K-72: it never verifies files).

Every MUTATING command holds the project OPERATION LOCK (K-96 D7,
dct.workflows.release.oplock — one per repository: its git common dir, so
all worktrees and every project of that repository share it; a project
outside git locks its directory) from before its pre-checks / stamp until
it returns: prepare, apply (and the alias), resume, attest, abandon,
reconstruct --confirm. A second one refuses at once; editors and other git
processes are never blocked by it.

Stages of one release:

  0. prepare (`dct release prepare`, K-76) — an explicit EFFECTS stage
     BEFORE the stamp, not part of the read-only plan: the same pre-checks
     and candidate pinning as apply (the plan re-derived for the pinned
     checkout must match --plan-digest); the project's version-sync script
     runs ONCE with V, ISOLATED — in a temporary git worktree detached at
     the candidate (hooks disabled for that internal checkout), or, for a
     project CONFIRMED outside git, in a temporary copy of its directory —
     never in the user's checkout, under a timeout with bounded captured
     output and the whole process group stopped before it returns (K-96
     D9). The user's checkout must be unchanged afterwards (HEAD, refs,
     index, status, the bytes of untracked files; outside git every file) —
     a script that wrote outside its isolated copy is refused. From the
     isolated result it builds the PROPOSAL (core.releases.validate_proposal:
     exact changed paths, base / expected blobs, script identity + sha256,
     candidate, version, plan digest, the changelog baseline), checks each
     file's version FIELD (core.version readers; a file without a reader may
     change only lines holding V), refuses a script that touched the
     changelog file (K-77 / D2) or deleted / retyped a file, and stores it
     as the project's PENDING proposal (core.releases.set_pending_proposal).
     Expected blobs live in the repository's object database (`git add` in
     the temporary worktree) or, outside git, in dct's release blob store
     (dct.paths.release_store_dir). Nothing is stamped.
  1. pre-checks of apply — strict checkout (K-52; a registered path), no
     unfinished record, clean index and work tree (K-50); the plan
     re-derived FOR THE PINNED CANDIDATE must match the approved snapshot
     digest (K-80); the requested set ⊆ the plan's proven ids; the push
     target (push_target: the branch's upstream, or the explicit
     --push-remote / --push-branch, or --no-push — no upstream is refused,
     never guessed) and the remote read once (no tag of V there, its branch
     contained in the candidate — K-97 F2); the approved proposal
     (--proposal-digest) must be the pending one, bound to the same
     candidate, version and plan digest, with the script and the baseline
     unchanged; HEAD unchanged right before the stamp;
  2. stamp — core release_entries (the record, status 'stamped'), bound to
     the plan inside its transaction (K-81: stampable_ids, expected_entries)
     and consuming the approved proposal in the SAME transaction
     (consume_proposal: the record's proposal + consent — with the push
     target, K-97 F4: what every resume executes — the learned
     version-file contract, the untracked baseline — K-76, K-96 D3/D4);
     from here the record's `version` is the spelling of every artifact
     (K-55);
  3. changelog file F — the script is NEVER run again after the stamp; F's
     expected content is DETERMINISTIC: F's baseline (the candidate's blob;
     outside git the copy persisted at prepare, K-78) + the V section
     rendered from the database and the record (date = the record's
     created_at, K-32);
  4. version files — the approved expected blobs are written; every
     artifact (F included) is classified BEFORE anything is written (K-75:
     the baseline → written, the approved content → kept, anything else →
     refused with NOTHING written and the index untouched — never restored
     automatically); afterwards the full artifact set is read ONCE and
     every hash and the expected tree are computed from that one read and
     checked against the approved content (K-77) before the manifest
     {changelog, version_files, tree} is recorded with 'files_written';
  5. commit — the expected tree (candidate tree + exactly the manifest
     blobs) is built in a TEMPORARY index; the real index is checked
     read-only for foreign paths / content (and a foreign index.lock);
     `git commit` from the temporary index creates the commit THROUGH THE
     REPOSITORY'S HOOKS (K-96: pre-commit, prepare-commit-msg, commit-msg,
     post-commit; never --no-verify, never commit-tree), HEAD moving by
     git's own compare-and-swap; parent, tree and subject are re-validated
     (a refusing hook → nothing committed; a hook that changed the content
     → refused with the observed commit, never adopted); then the user's
     REAL index follows the commit under git's index.lock protocol, its
     own staging preserved (K-79, _sync_real_index) → 'committed';
  6. tag `v<V>` (lightweight) at the verified release commit → 'tagged';
  7. push (K-97 F2) — `git push --atomic` of the release commit and the tag
     to the CONSENTED target, verified with `git ls-remote` → 'pushed',
     then 'completed'; a failed push leaves 'tagged' and resume retries
     only the push. Without a push target (--no-push, outside git) the
     record goes 'tagged' → 'completed'.

Every effect is checked against reality on resume (after the full entry set
of the record, K-94 B3): present and matching → skipped; missing → performed;
contradicting → ReleaseRefused, nothing overwritten. Interruption points
(INTERRUPTION_POINTS) let tests stop the workflow before each effect and
between each effect and its progress write. Schema of the results and the
interruption / resume table: docs/release-workflow.md.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime

import sqlalchemy as sa

from dct import paths as dct_paths
from dct.core import gitinfo
from dct.core import releases as ledger
from dct.core.changelog import list_entries, normalize_worktree, release_entries
from dct.core.projects import get_project
from dct.core.version import (
    compare_versions,
    normalize_version,
    read_version_field,
    same_version,
    version_field_reader,
)
from dct.export import export_changelog_md
from dct.workflows.release import oplock
from dct.workflows.release import plan as planning
from dct.workflows.release.proof import proven_ids

SCHEMA_VERSION = 1
RESULT_KEYS = (
    "schema_version", "action", "project", "outcome", "version", "release",
    "steps", "warnings", "details",
)
OUTCOMES = (
    "completed", "noop", "attested", "abandoned", "preview", "reconstructed",
    "prepared", "refused", "failed", "found", "none",
)

# Where a test (or a debugger) may stop the workflow: before each effect and
# between each effect and the progress write that records it.
INTERRUPTION_POINTS = (
    "before_stamp",          # nothing done yet
    "after_stamp",           # record 'stamped' (the stamp IS its progress write)
    "before_changelog",      # every artifact classified, nothing written
    "after_changelog",       # F written; no version file written yet
    "before_version_files",
    "after_version_file",    # ONE version file written (fires after each write)
    "after_version_files",   # every artifact written; not verified, not 'files_written'
    "after_files_written",   # manifest recorded; before the commit
    "before_commit",         # real index checked, temporary index built; `git commit` not run
    "after_commit",          # commit created (hooks ran) + HEAD moved + verified; the real
                             # index not yet updated; not 'committed'
    "after_index",           # real index entries of the artifacts = the commit's; not 'committed'
    "after_committed",       # before the tag
    "after_tag",             # tag created + verified; not 'tagged'
    "after_tagged",          # before the push
    "before_push",           # remote checked (tag not elsewhere); `git push` not run
    "after_push",            # pushed + verified with ls-remote; not 'pushed'
    "after_pushed",          # before 'completed'
)

VERSION_SYNC_SCRIPTS = ("scripts/dct-version-sync.sh", "scripts/version-sync.sh")
VERSION_ONLY_LINE = "- Version-only release — no changelog entries."
ATTEST_SUBJECT = "chore: attest changelog entries"
RELEASE_SUBJECT = "release: v{version}"

# The version-sync script (prepare, K-96 D9): wall-clock limit, the grace a
# stopped process group gets between SIGTERM and SIGKILL, and the cap of the
# captured output (stdout + stderr; the rest is counted and dropped).
SYNC_TIMEOUT_SECONDS = 300
SYNC_KILL_GRACE_SECONDS = 3
SYNC_OUTPUT_MAX_BYTES = 65536
SYNC_TIMEOUT_EXIT = 124  # the exit code of a timed-out sync (as timeout(1))
# Creating / staging the temporary worktree of prepare (a checkout of the
# whole candidate) may take longer than the ordinary git reads.
PREPARE_GIT_TIMEOUT_SECONDS = 600
# A project outside git is prepared on a temporary COPY of its directory —
# bounded, a larger directory is refused instead of copied.
COPY_MAX_FILES = 20000
COPY_MAX_BYTES = 256 * 1024 * 1024
# `git commit` runs the repository's hooks (a pre-commit test suite …) and
# `git push` the remote side's — both may take far longer than a git read.
HOOK_GIT_TIMEOUT_SECONDS = 900

_MARKER_RE = re.compile(r"^## \[([^\]\r\n]*)\]")
_RELEASE_SUBJECT_RE = re.compile(r"^release: (\S+)$")
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_NOT_A_FILE = "<not a regular file>"


class ReleaseRefused(RuntimeError):
    """A controlled refusal: the workflow stopped WITHOUT the effect it was
    about to perform (and without overwriting anything). `details` is a
    JSON-native dict for the --json output; `exit_code` the CLI's code."""

    exit_code = 1

    def __init__(self, message: str, *, details: dict | None = None,
                 steps: list[dict] | None = None, release: dict | None = None):
        super().__init__(message)
        self.details = details or {}
        self.steps = steps or []
        self.release = release


class SyncFailed(ReleaseRefused):
    """The version-sync script exited non-zero or timed out during prepare
    — nothing was stamped, no proposal stored."""

    def __init__(self, message: str, code: int, **kwargs):
        super().__init__(message, **kwargs)
        self.exit_code = code if 0 < code < 256 else 1


def result(action: str, project: str, outcome: str, **values) -> dict:
    """The ONE result shape of every subcommand (RESULT_KEYS)."""
    out = {key: None for key in RESULT_KEYS}
    out.update(schema_version=SCHEMA_VERSION, action=action, project=project,
               outcome=outcome, steps=[], warnings=[], details={})
    out.update(values)
    return planning._jsonable(out)


# ── small helpers ────────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _write_atomic(path: str, data: bytes) -> None:
    """Replace `path` with `data` (temp file in the same directory + rename),
    keeping an existing file's mode (a new file: the default mode); read
    back and compared."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:  # a new file gets the default mode (0666 & ~umask)
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(prefix=".dct-release-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if _read_bytes(path) != data:
        raise ReleaseRefused(f"{path} does not read back what was written")


def _date_of(moment) -> str:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.date().isoformat()


def _rel(path: str) -> str:
    return path.replace(os.sep, "/")


def _point(on_point: Callable[[str], None] | None, name: str) -> None:
    assert name in INTERRUPTION_POINTS, name
    if on_point is not None:
        on_point(name)


# ── git ──────────────────────────────────────────────────────────────────────


def _rev(repo: str, spec: str) -> str | None:
    code, out = gitinfo.git_read(repo, "rev-parse", "-q", "--verify", spec, ok_codes=(0, 1))
    return out.strip().lower() if code == 0 and out.strip() else None


def _head(repo: str) -> str:
    head = _rev(repo, "HEAD^{commit}")
    if head is None:
        raise ReleaseRefused(f"HEAD of {repo} cannot be read")
    return head


def _tree(repo: str, commit: str) -> str:
    tree = _rev(repo, f"{commit}^{{tree}}")
    if tree is None:
        raise ReleaseRefused(f"commit {commit} has no readable tree")
    return tree


def _parents(repo: str, commit: str) -> list[str]:
    _, out = gitinfo.git_read(repo, "rev-list", "--parents", "-n", "1", commit, "--")
    return out.split()[1:]


def _subject(repo: str, commit: str) -> str:
    _, out = gitinfo.git_read(repo, "log", "-1", "--format=%s", commit, "--")
    return out.strip()


def _diff_names(repo: str, a: str, b: str) -> set[str]:
    """Paths whose entries differ between trees / commits `a` and `b`."""
    _, out = gitinfo.git_read(repo, "diff-tree", "-r", "--name-only", "--no-renames", "-z", a, b)
    return {p for p in out.split("\0") if p}


def _git_path(repo: str, name: str) -> str:
    """The absolute path of `name` inside THIS checkout's git dir (`git
    rev-parse --git-path`: a linked worktree has its own index)."""
    _, out = gitinfo.git_read(repo, "rev-parse", "--git-path", name)
    path = out.strip()
    return path if os.path.isabs(path) else os.path.join(repo, path)


def _staged_against(repo: str, commit: str, *, index_file: str | None = None) -> set[str]:
    """Paths whose index entry differs from `commit`'s tree (`git diff-index
    --cached`, READ-ONLY — never `write-tree`, which would lock and rewrite
    the user's index); unmerged paths included."""
    args = ("diff-index", "--cached", "--no-renames", "--name-only", "-z", commit, "--")
    if index_file is None:
        _, out = gitinfo.git_read(repo, *args)
    else:
        _, out = gitinfo.git_write(repo, *args, index_file=index_file)
    return {p for p in out.split("\0") if p}


def _index_entries(repo: str, paths: Iterable[str], *,
                   index_file: str | None = None) -> dict[str, tuple[str, str, str]]:
    """{path: (mode, blob, stage)} of `paths` in the user's index (read-only)
    or in `index_file`; an unmerged path has several stages — the highest
    is kept (never equal to a stage-0 expectation)."""
    paths = sorted(paths)
    if not paths:
        return {}
    args = ("ls-files", "-s", "-z", "--", *paths)
    if index_file is None:
        _, out = gitinfo.git_read(repo, *args)
    else:
        _, out = gitinfo.git_write(repo, *args, index_file=index_file)
    found: dict[str, tuple[str, str, str]] = {}
    for entry in (e for e in out.split("\0") if e):
        meta, _, rel = entry.partition("\t")
        mode, blob, stage = meta.split()
        if rel in found and found[rel][2] >= stage:
            continue
        found[rel] = (mode, blob.lower(), stage)
    return found


def _tree_entries(repo: str, commit: str, paths: Iterable[str]) -> dict[str, tuple[str, str, str]]:
    """{path: (mode, blob, "0")} of `paths` in `commit` (absent paths left out)
    — comparable with _index_entries."""
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


def _commit_facts(repo: str, commit: str) -> dict:
    return {"commit": commit, "tree": _tree(repo, commit), "parents": _parents(repo, commit),
            "subject": _subject(repo, commit)}


def _hook_commit(run_refuse: Callable[..., ReleaseRefused], repo: str, tree: str, parent: str,
                 message: str) -> str:
    """Create a commit of EXACTLY `tree` on `parent` (= HEAD) through `git
    commit` — the repository's hook lifecycle runs (pre-commit,
    prepare-commit-msg, commit-msg, post-commit; commit.gpgSign and the
    like as git applies them; never --no-verify, K-96). The commit is built
    from a TEMPORARY index holding `tree` (GIT_INDEX_FILE, exported to the
    hooks — they see exactly what is committed); the user's index is not
    touched. HEAD is moved by git commit's own ref transaction, a
    compare-and-swap against the HEAD it read (`parent`): a concurrent HEAD
    move fails the commit. Returns HEAD afterwards — UNVERIFIED: the caller
    re-validates parent, tree and message (a hook may have changed them). A
    failed `git commit` (a refusing hook, a lost ref race, a signing
    failure) → refusal naming what HEAD is now."""
    with tempfile.TemporaryDirectory(prefix="dct-release-commit-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", tree, index_file=index)
        try:
            gitinfo.git_write(repo, "commit", "--quiet", "--allow-empty", "-m", message,
                              index_file=index, timeout=HOOK_GIT_TIMEOUT_SECONDS)
        except gitinfo.GitWriteError as exc:
            head = _rev(repo, "HEAD^{commit}")
            moved = "unchanged" if head == parent else f"now {head} (moved by another process)"
            raise run_refuse(
                f"`git commit` refused the commit — a repository hook rejected it, HEAD moved "
                f"meanwhile or signing failed ({exc}); dct created no commit, HEAD is {moved}; "
                f"nothing recorded — fix the cause and run the command again.",
                git_error=str(exc), head=head, expected_parent=parent) from None
    return _head(repo)


@contextlib.contextmanager
def _index_lock(run_refuse: Callable[..., ReleaseRefused], index_path: str) -> Iterator[int]:
    """Git's own index lock protocol (K-79): `<index>.lock` created with
    O_CREAT | O_EXCL — an existing lock belongs to another git process and
    is refused, NEVER deleted or written. The holder writes the new index
    into the lock and renames it over the index (commit_index); on any
    failure ITS OWN lock is removed and the index is left as it was."""
    lock = index_path + ".lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        raise run_refuse(
            f"{lock} exists — another git process is using the index (or crashed and left "
            f"it: remove it yourself only if no git process runs); dct never removes a "
            f"foreign index.lock; nothing changed in the index — run `dct release resume` "
            f"once it is gone.", index_lock=lock) from None
    state = {"fd": fd, "committed": False}
    try:
        yield fd
        os.fsync(fd)
        os.close(fd)
        state["fd"] = None
        os.replace(lock, index_path)
        state["committed"] = True
    finally:
        if state["fd"] is not None:
            os.close(state["fd"])
        if not state["committed"]:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _sync_real_index(run: _Run, commit: str, paths: list[str]) -> list[str]:
    """Bring the user's REAL index in line with the verified release commit
    for the artifact `paths` — K-79, under git's index.lock (_index_lock),
    never by running `git add` / `git commit` on the real index. Under the
    lock the WHOLE relevant state is re-checked: HEAD must still be the
    release commit, and every artifact's index entry must be the
    candidate's (→ updated to the commit's entry) or already the commit's
    (→ kept); anything else is the user's own staging → refused, PRESERVED
    (nothing written). Every other index entry (foreign staging elsewhere)
    is copied unchanged. The new index is prepared on a COPY (a temporary
    GIT_INDEX_FILE), written into the lock and renamed into place. Returns
    the updated paths."""
    repo, candidate = run.repo, run.record["candidate_sha"]
    base = _tree_entries(repo, candidate, paths)
    target = _tree_entries(repo, commit, paths)
    index_path = _git_path(repo, "index")
    with _index_lock(run.refuse, index_path) as fd:
        current = _read_bytes(index_path)
        if current is None:
            raise run.refuse(f"the index {index_path} is missing — nothing changed.",
                             commit=commit)
        head = _rev(repo, "HEAD^{commit}")
        if head != commit:
            raise run.refuse(
                f"HEAD moved to {head} after the release commit {commit} was created — the "
                f"index is not updated and the commit is not recorded; nothing else done.",
                commit=commit, head=head)
        with tempfile.TemporaryDirectory(prefix="dct-release-index-") as tmp:
            copy = os.path.join(tmp, "index")
            with open(copy, "wb") as fh:
                fh.write(current)
            entries = _index_entries(repo, paths, index_file=copy)
            todo, foreign = [], []
            for rel in paths:
                entry = entries.get(rel)
                if entry == target.get(rel):
                    continue
                if entry == base.get(rel):
                    todo.append(rel)
                else:
                    foreign.append(rel)
            if foreign:
                raise run.refuse(
                    f"{', '.join(foreign)}: your index holds a staged version that is neither the "
                    f"candidate's nor the release commit's — PRESERVED, the index is not "
                    f"updated. The release commit {commit} exists (HEAD); it is not recorded "
                    f"yet: unstage or commit your change yourself, then run `dct release "
                    f"resume`.", foreign_staged=foreign, commit=commit, head=commit)
            for rel in todo:
                mode, blob, _ = target[rel]
                gitinfo.git_write(repo, "update-index", "--add", "--cacheinfo",
                                  f"{mode},{blob},{rel}", index_file=copy)
            if todo:  # stat data for the rewritten entries (content compared)
                gitinfo.git_write(repo, "update-index", "-q", "--refresh", index_file=copy,
                                  ok_codes=(0, 1))
            with open(copy, "rb") as fh:
                _write_all(fd, fh.read())
    return todo


def _ls_remote(repo: str, remote: str) -> dict[str, str]:
    """{ref: object} of `remote` (`git ls-remote`, network; an annotated tag
    maps to the commit it peels to)."""
    try:
        _, out = gitinfo.git_write(repo, "ls-remote", remote, network=True,
                                   timeout=HOOK_GIT_TIMEOUT_SECONDS)
    except gitinfo.GitWriteError as exc:
        raise ReleaseRefused(f"the remote {remote} cannot be read ({exc}) — nothing pushed.",
                             details={"git_error": str(exc), "remote": remote}) from None
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}"):
            refs[ref[:-3]] = sha.lower()
        else:
            refs.setdefault(ref, sha.lower())
    return refs


def _remote_version_tags(refs: dict[str, str], version: str) -> list[dict]:
    tags = []
    for ref, sha in sorted(refs.items()):
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        try:
            if same_version(normalize_version(name), version):
                tags.append({"name": name, "commit": sha})
        except ValueError:
            continue
    return tags


def _contains(repo: str, tip: str | None, commit: str) -> bool:
    """`tip` (a remote branch's object) is `commit` or a descendant of it
    that this repository knows."""
    if tip is None:
        return False
    if tip == commit:
        return True
    for obj in (tip, commit):
        code, _ = gitinfo.git_read(repo, "cat-file", "-e", f"{obj}^{{commit}}",
                                   ok_codes=(0, 1, 128))
        if code != 0:
            return False
    return gitinfo.git_is_ancestor(repo, commit, tip)


def _operation_in_progress(repo: str) -> list[str]:
    found = []
    for marker in _IN_PROGRESS:
        _, out = gitinfo.git_read(repo, "rev-parse", "--git-path", marker)
        path = out.strip()
        if path and os.path.exists(path if os.path.isabs(path) else os.path.join(repo, path)):
            found.append(marker)
    return found


def _file_mode(repo: str, commit: str, rel: str, absolute: str) -> str:
    _, out = gitinfo.git_read(repo, "ls-tree", "-z", commit, "--", rel)
    entry = next((e for e in out.split("\0") if e), None)
    if entry:
        return entry.split()[0]
    return "100755" if os.stat(absolute).st_mode & 0o111 else "100644"


def expected_tree(repo: str, candidate: str, paths: Iterable[str]) -> str:
    """The tree of the release commit: the candidate's tree with EXACTLY the
    work-tree content of `paths` (repo-relative) — blobs written with `git
    hash-object -w` (path filters apply, as for `git add`), the tree built
    by _tree_of_blobs (the user's index is never touched)."""
    blobs = {}
    for rel in sorted(paths):
        _, blob = gitinfo.git_write(repo, "hash-object", "-w", "--", rel)
        blobs[rel] = blob.strip()
    return _tree_of_blobs(repo, candidate, blobs)


def _tree_of_blobs(repo: str, candidate: str, blobs: dict[str, str]) -> str:
    """The candidate's tree with each path of `blobs` replaced by / added as
    that blob — computed in a TEMPORARY index (GIT_INDEX_FILE). The mode is
    the candidate's, or the work-tree file's for a new path."""
    with tempfile.TemporaryDirectory(prefix="dct-release-index-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", f"{candidate}^{{tree}}", index_file=index)
        for rel in sorted(blobs):
            mode = _file_mode(repo, candidate, rel, os.path.join(repo, rel))
            gitinfo.git_write(repo, "update-index", "--add", "--cacheinfo",
                              f"{mode},{blobs[rel]},{rel}", index_file=index)
        _, tree = gitinfo.git_write(repo, "write-tree", index_file=index)
    return tree.strip()


def _version_tags(repo: str, version: str) -> list[dict]:
    """Tags whose name is the same version as `version` (any spelling):
    [{name, commit (peeled)}]."""
    _, out = gitinfo.git_read(
        repo, "for-each-ref", "--format=%(refname:strip=2)%1f%(objectname)%1f%(*objectname)",
        "refs/tags")
    tags = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        name, obj, peeled = parts
        try:
            if not same_version(normalize_version(name), version):
                continue
        except ValueError:
            continue
        tags.append({"name": name, "commit": (peeled or obj).lower()})
    return sorted(tags, key=lambda t: t["name"])


# ── location ─────────────────────────────────────────────────────────────────


def _locate(engine: sa.Engine, slug: str, cwd: str | None) -> tuple[dict, dict | None, str | None]:
    """(project, checkout | None, project_root | None) — STRICT (K-52)."""
    project = get_project(engine, slug)
    if project is None:
        raise ReleaseRefused(f"Project '{slug}' not found")
    path = project.get("path")
    if not path:
        return project, None, None
    try:
        checkout = gitinfo.verify_release_checkout(path, cwd)
    except gitinfo.NotACheckoutError:
        raise ReleaseRefused(
            f"The current directory is not a checkout of project '{slug}' ({path}) — run "
            f"the release from the project's main checkout or one of its linked worktrees; "
            f"nothing released.") from None
    except gitinfo.CheckoutVerificationError as exc:
        raise ReleaseRefused(
            f"Cannot verify the release checkout of project '{slug}' ({path}): {exc} — "
            f"nothing released.") from None
    if checkout is None:
        return project, None, path
    return project, checkout, checkout["project_root"]


def _lock_file(project: dict, checkout: dict | None) -> str:
    """The operation lock of the project's REPOSITORY (K-96 D7,
    dct.workflows.release.oplock): its git common dir — the same for the
    main checkout, every linked worktree and every project in that
    repository — or, for a project CONFIRMED outside git, its directory.
    Without a verified checkout (abandon) the registered path is resolved
    the same way; a path that cannot be verified locks on the path itself."""
    if checkout is not None:
        return oplock.lock_path(git_common_dir=checkout["git_common_dir"])
    path = project.get("path")
    if not path:
        return oplock.lock_path(project_path=os.path.join(
            str(dct_paths.release_lock_dir()), f"project-{project['slug']}"))
    try:
        found = gitinfo.verify_release_checkout(path, path)
    except gitinfo.CheckoutVerificationError:
        found = None
    if found is not None:
        return oplock.lock_path(git_common_dir=found["git_common_dir"])
    return oplock.lock_path(project_path=path)


@contextlib.contextmanager
def _operation(project: dict, checkout: dict | None, action: str) -> Iterator[None]:
    """Hold the project operation lock for one MUTATING command (K-96 D7) —
    taken before its pre-checks / stamp, released when it returns; another
    holder → ReleaseRefused at once (nothing done, nothing waits)."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(oplock.operation_lock(_lock_file(project, checkout), action))
        except oplock.OperationLocked as exc:
            raise ReleaseRefused(str(exc), details={"lock": exc.path,
                                                    "holder": exc.holder}) from None
        yield


class _Run:
    """State of one prepare / apply / resume run."""

    def __init__(self, engine, slug, project, checkout, project_root, *, on_point):
        self.engine = engine
        self.slug = slug
        self.project = project
        self.checkout = checkout
        self.repo = checkout["repo_root"] if checkout else None
        self.project_root = project_root
        self.on_point = on_point
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.record: dict | None = None
        self.changelog_rel = self._changelog_rel()

    def _changelog_rel(self) -> str | None:
        if not self.project_root:
            return None
        if self.checkout:
            found = [a["path"] for a in planning._artifacts(self.project, self.checkout, None)
                     if a["role"] == "changelog"]
            return found[0] if found else None
        if not self.project.get("manages_changelog", True):
            return None
        return _rel(os.path.normpath(self.project.get("changelog_path") or "changelog.md"))

    @property
    def base(self) -> str:
        """Paths in the manifest are relative to this directory."""
        return self.repo or self.project_root

    @property
    def mode(self) -> str:
        return "git" if self.repo else ledger.WITHOUT_GIT

    def abspath(self, rel: str) -> str:
        return os.path.join(self.base, *rel.split("/"))

    def step(self, name: str, outcome: str, **detail) -> None:
        self.steps.append({"step": name, "result": outcome, **detail})

    def refuse(self, message: str, **details) -> ReleaseRefused:
        return ReleaseRefused(message, details=details, steps=self.steps, release=self.record)

    def advance(self, status: str, **kwargs) -> None:
        out = ledger.advance_release(self.engine, self.slug, self.record["version"], status,
                                     **kwargs)
        self.record = out["release"]

    @property
    def version(self) -> str:
        return self.record["version"]


# ── pre-checks ───────────────────────────────────────────────────────────────


def _clean_tree(run: _Run, artifacts: list[dict]) -> dict:
    """K-50 for a NEW release: nothing staged, unstaged or conflicted, no
    operation in progress, no release artifact untracked. Returns the index
    state (its untracked list is the pre-sync snapshot)."""
    busy = _operation_in_progress(run.repo)
    if busy:
        raise run.refuse(f"a git operation is in progress ({', '.join(busy)}) — finish or "
                         f"abort it first; nothing released.", in_progress=busy)
    state = planning.index_state(run.repo, artifacts)
    dirty = {"staged": state["staged"], "unstaged": state["unstaged"],
             "conflicted": state["conflicted"]}
    if not state["clean"]:
        raise run.refuse(
            "the index / work tree is not clean — commit or clean it yourself (dct never "
            "resets, stashes or cleans your changes); nothing released.", **dirty)
    untracked_artifacts = [a["path"] for a in state["release_artifacts"] if a["untracked"]]
    if untracked_artifacts:
        raise run.refuse(
            f"release artifacts are untracked: {', '.join(untracked_artifacts)} — commit or "
            f"remove them first; nothing released.", untracked=untracked_artifacts)
    return state


def _check_record_location(run: _Run) -> None:
    """K-51: the record belongs to THIS checkout — never re-pinned."""
    record = run.record
    if run.checkout is None:
        if record["candidate_sha"] or record["checkout_root"]:
            raise run.refuse(
                f"release {record['version']} was cut from a git checkout "
                f"({record['checkout_root'] or '?'}) but the project is not in one now — "
                f"nothing done.")
        return
    if not record["candidate_sha"] or not record["checkout_root"]:
        raise run.refuse(
            f"release {record['version']} has no recorded candidate / checkout — it cannot "
            f"be verified in a git checkout; nothing done (abandon it).",
            candidate_sha=record["candidate_sha"], checkout_root=record["checkout_root"])
    here = normalize_worktree(run.repo)
    if record["checkout_root"] != here:
        raise run.refuse(
            f"release {record['version']} belongs to the checkout {record['checkout_root']}, "
            f"not to {here} — resume it there; the record is never re-pinned; nothing done.",
            recorded=record["checkout_root"], here=here)


# ── the changelog file: its deterministic expected content ───────────────────


def _blocks(text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """(preamble, [(version label, start, end)]) — character offsets of each
    `## [label]` section (from its marker line to the next marker / EOF)."""
    offsets, pos = [], 0
    for line in text.splitlines(keepends=True):
        match = _MARKER_RE.match(line)
        if match:
            offsets.append((match.group(1), pos))
        pos += len(line)
    blocks = []
    for i, (label, start) in enumerate(offsets):
        end = offsets[i + 1][1] if i + 1 < len(offsets) else len(text)
        blocks.append((label, start, end))
    preamble = text[:offsets[0][1]] if offsets else text
    return preamble, blocks


def _version_blocks(text: str, version: str) -> list[tuple[str, int, int]]:
    return [b for b in _blocks(text)[1] if _same_label(b[0], version)]


def _same_label(label: str, version: str) -> bool:
    try:
        return same_version(normalize_version(label), version)
    except ValueError:
        return False


def _norm_block(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def render_section(engine: sa.Engine, slug: str, record: dict) -> tuple[str, str]:
    """(section, date) of the release — deterministic, from the database
    and the record only (K-32/K-49): the export's block of the record's
    version (every equivalent stored spelling, K-55) headed `## [<record
    version>] - <record created_at date>`; a version-only release gets
    VERSION_ONLY_LINE. Ends with exactly one newline."""
    version, date = record["version"], _date_of(record["created_at"])
    block = export_changelog_md(engine, slug, version=version)
    if record["entry_ids"]:
        lines = block.split("\n") if block else []
        match = _MARKER_RE.match(lines[0]) if lines else None
        if match is None or not _same_label(match.group(1), version):
            raise ReleaseRefused(
                f"the export of {version} does not render its section although the record "
                f"holds entries {record['entry_ids']} — nothing written.")
        lines[0] = f"## [{version}] - {date}"
        section = "\n".join(lines)
    else:
        if block:
            raise ReleaseRefused(
                f"entries carry {version} but its record is version-only — contradiction; "
                f"nothing written.")
        section = f"## [{version}] - {date}\n\n{VERSION_ONLY_LINE}\n"
    return section.rstrip("\n") + "\n", date


def _insert_section(base: str, section: str, version: str) -> str:
    """`base` with `section` inserted as the TOP version section (directly
    above the first `## [` marker line, a blank line between), or appended
    after the existing text when there is no marker. Verified: V exactly
    once, one marker more, every earlier byte unchanged."""
    newline = "\r\n" if "\r\n" in base else "\n"
    section = section.replace("\n", newline)
    _, blocks = _blocks(base)
    if blocks:
        pos = blocks[0][1]
        inserted = section + newline
    elif not base:
        pos, inserted = 0, f"# Changelog{newline}{newline}" + section
    else:
        pos = len(base)
        lead = "" if base.endswith(newline) else newline
        lead += "" if base.endswith(newline * 2) else newline
        inserted = lead + section
    new = base[:pos] + inserted + base[pos:]
    if not (new[:pos] == base[:pos] and new[pos + len(inserted):] == base[pos:]):
        raise ReleaseRefused("the changelog insertion would change earlier history")
    if len(_version_blocks(new, version)) != 1:
        raise ReleaseRefused(f"the changelog would not hold the {version} section exactly once")
    if len(_blocks(new)[1]) != len(blocks) + 1:
        raise ReleaseRefused("the changelog insertion did not add exactly one section")
    return new


def expected_changelog(engine: sa.Engine, slug: str, record: dict, base: bytes | None) -> bytes:
    """The expected content of F after the release (K-32) from its content
    BEFORE the release (`base`; None = F does not exist — bootstrap):
      - bootstrap: the full export (no Unreleased) with the release's block
        replaced by / completed with the rendered section — V exactly once;
      - existing F without a V section: the section inserted on top, the
        rest byte-identical, one section more;
      - existing F already holding the IDENTICAL V section once: unchanged;
        any other V section (different text, several) → ReleaseRefused.
    """
    version = record["version"]
    section, _ = render_section(engine, slug, record)
    if base is None:
        full = export_changelog_md(engine, slug, include_unreleased=False)
        for _, start, end in reversed(_version_blocks(full, version)):
            full = full[:start] + full[end:]
        return _insert_section(full, section, version).encode("utf-8")
    try:
        text = base.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseRefused("the changelog file is not UTF-8 — not touched") from None
    existing = _version_blocks(text, version)
    if len(existing) == 1 and _norm_block(text[existing[0][1]:existing[0][2]]) == \
            _norm_block(section):
        return base
    if existing:
        raise ReleaseRefused(
            f"the changelog file already holds {len(existing)} {version} section(s) that "
            f"differ from the expected one — not overwritten.",
            details={"sections": len(existing)})
    return _insert_section(text, section, version).encode("utf-8")

# ── content identities and approved blobs ────────────────────────────────────
#
# A version file's content is named by a git blob id (projects in git: the
# repository's object database, path filters applied as for `git add`) or by
# the SHA-256 of its bytes (projects CONFIRMED outside git: dct's release
# blob store, dct.paths.release_store_dir — content-addressed, read back and
# re-hashed before use).


def _read_regular(path: str) -> bytes | str | None:
    """The bytes of a regular file, None when nothing exists at `path`, or
    _NOT_A_FILE (a directory, a symlink, a device …) — never equal to any
    expected content."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        return _NOT_A_FILE
    return _read_bytes(path)


def _store_path(digest: str) -> str:
    return os.path.join(dct_paths.release_store_dir(), digest[:2], digest)


def _store_put(data: bytes) -> str:
    """Keep `data` in dct's release blob store; returns its SHA-256 (the id)."""
    digest = _sha256(data)
    path = _store_path(digest)
    if _read_bytes(path) != data:
        _write_atomic(path, data)
    return digest


def _store_get(digest: str) -> bytes:
    path = _store_path(digest)
    data = _read_bytes(path)
    if data is None or _sha256(data) != digest:
        raise ReleaseRefused(
            f"the approved content {digest} is missing from dct's release blob store "
            f"({os.path.dirname(os.path.dirname(path))}) — prepare again (or abandon the "
            f"release); nothing written.", details={"missing_blob": digest})
    return data


def _blob_at(repo: str, commit: str, rel: str) -> str | None:
    """The blob id of the REGULAR file `rel` in `commit`, or None (absent);
    a symlink / submodule / tree there is refused."""
    _, out = gitinfo.git_read(repo, "ls-tree", "-z", commit, "--", rel)
    entry = next((e for e in out.split("\0") if e), None)
    if entry is None:
        return None
    meta, _, name = entry.partition("\t")
    parts = meta.split()
    if name != rel or len(parts) != 3 or parts[1] != "blob" or parts[0] not in ("100644",
                                                                               "100755"):
        raise ReleaseRefused(f"{rel} in {commit} is not a regular file ({meta}) — refused.")
    return parts[2].lower()


def _content_id(run: _Run, rel: str) -> str | None:
    """The id of the CURRENT content of artifact `rel` (git: `git
    hash-object --path`, nothing written; outside git: SHA-256), None when
    absent, _NOT_A_FILE when not a regular file."""
    path = run.abspath(rel)
    data = _read_regular(path)
    if not isinstance(data, bytes):
        return data
    if run.repo is None:
        return _sha256(data)
    _, out = gitinfo.git_read(run.repo, "hash-object", f"--path={rel}", "--", path)
    return out.strip().lower()


def _id_of_bytes(run: _Run, rel: str, data: bytes) -> str:
    """The id of `data` as the content of `rel` — computed from THESE bytes
    (K-77: from the one read), never from a second read of the file. In git
    the blob is written to the object database (the expected tree needs
    it)."""
    if run.repo is None:
        return _sha256(data)
    with tempfile.TemporaryDirectory(prefix="dct-release-blob-") as tmp:
        scratch = os.path.join(tmp, "content")
        with open(scratch, "wb") as fh:
            fh.write(data)
        _, out = gitinfo.git_write(run.repo, "hash-object", "-w", f"--path={rel}", "--", scratch)
    return out.strip().lower()


def _approved_bytes(run: _Run, rel: str, blob: str) -> bytes:
    """The work-tree bytes of the approved blob of `rel`."""
    if run.repo is None:
        return _store_get(blob)
    try:
        return gitinfo.git_cat_blob(run.repo, blob, path=rel)
    except gitinfo.GitReadError as exc:
        raise run.refuse(
            f"the approved blob {blob} of {rel} cannot be read ({exc}) — prepare again (or "
            f"abandon the release); nothing written.", missing_blob=blob) from None


def _blob_available(run: _Run, blob: str) -> bool:
    if run.repo is None:
        data = _read_bytes(_store_path(blob))
        return data is not None and _sha256(data) == blob
    code, _ = gitinfo.git_read(run.repo, "cat-file", "-e", blob, ok_codes=(0, 1, 128))
    return code == 0


# ── the version-sync script (prepare only, K-96 D9) ──────────────────────────


def _sync_env() -> dict[str, str]:
    """The caller's environment without repository-location variables (a
    GIT_DIR leaked from a hook must not redirect the script's git calls)."""
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_") or key in gitinfo.WRITE_ENV_PASSTHROUGH}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_group(proc: subprocess.Popen) -> bool:
    """Stop the script's whole process group (it runs in its own session):
    SIGTERM, then SIGKILL after SYNC_KILL_GRACE_SECONDS; the script itself
    is reaped. Returns True when something had to be stopped. Without
    process groups (Windows) only the script is killed."""
    if not hasattr(os, "killpg"):
        stopped = proc.poll() is None
        if stopped:
            proc.kill()
        proc.wait()
        return stopped
    pgid = proc.pid
    stopped = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None and not _group_alive(pgid):
            break
        stopped = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
        deadline = time.monotonic() + SYNC_KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None and not _group_alive(pgid):
                break
            time.sleep(0.02)
    proc.wait()
    return stopped


def run_sync_script(script: str, version: str, cwd: str) -> dict:
    """Run `bash <script> <version>` in `cwd` — its own session / process
    group, stdin closed, stdout + stderr captured into ONE temporary file.
    At most SYNC_TIMEOUT_SECONDS of wall clock; on timeout, and after a
    normal exit for anything it left running, the whole group is stopped
    (_stop_group) BEFORE this returns. Returns {exit_code (None on
    timeout), timed_out, output (the first SYNC_OUTPUT_MAX_BYTES, UTF-8
    with replacement), output_bytes (total), output_dropped_bytes,
    stopped_processes, duration_seconds}."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(
                ["bash", script, version], cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, env=_sync_env(), start_new_session=True)
        except OSError as exc:
            raise ReleaseRefused(f"the version-sync script cannot be started: {exc}") from None
        timed_out = False
        try:
            proc.wait(timeout=SYNC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
        stopped = _stop_group(proc)
        total = out.seek(0, os.SEEK_END)
        out.seek(0)
        head = out.read(SYNC_OUTPUT_MAX_BYTES)
    return {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "output": head.decode("utf-8", "replace"),
        "output_bytes": total,
        "output_dropped_bytes": total - len(head),
        "stopped_processes": stopped,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _sync_failed(run: _Run, script: str, sync: dict) -> SyncFailed:
    details = {"script": script, "exit_code": sync["exit_code"], "sync": sync}
    if sync["timed_out"]:
        return SyncFailed(
            f"version-sync script timed out after {SYNC_TIMEOUT_SECONDS}s and was stopped "
            f"(its whole process group): {script} — nothing stamped, no proposal stored; fix "
            f"the script and run `dct release prepare` again.", SYNC_TIMEOUT_EXIT,
            details=details, steps=run.steps)
    return SyncFailed(
        f"version-sync script failed (exit {sync['exit_code']}): {script} — nothing stamped, "
        f"no proposal stored; fix the script and run `dct release prepare` again.",
        sync["exit_code"], details=details, steps=run.steps)


# ── the version-file contract (K-96 D9) ──────────────────────────────────────


def _version_pattern(version: str) -> re.Pattern:
    """V as a whole version token (optionally v-prefixed): not inside a
    longer number or word (1.1.0 does not match 1.1.01 or 21.1.0)."""
    return re.compile(r"(?<![0-9A-Za-z.])v?" + re.escape(version) + r"(?![0-9A-Za-z]|\.[0-9])")


def check_version_file(path: str, base: bytes | None, expected: bytes, version: str) -> dict:
    """The content contract of ONE version file after the sync (K-96 D9) —
    never a substring search over the whole file:
      - a file dct has a version reader for (core.version
        VERSION_FIELD_READERS, by name: VERSION, pyproject.toml,
        package.json, Cargo.toml, tauri.conf.json): its version FIELD must
        be V (same_version) → {path, check: "version_field", value};
      - any other file (UTF-8 text): every line the sync added or changed
        (base → expected, line diff) must hold V as a whole version token,
        at least one such line, no line only removed → {path, check:
        "lines_with_version", changed_lines}.
    ValueError (the reason) otherwise."""
    if version_field_reader(path) is not None:
        value = read_version_field(path, expected)
        if value is None:
            raise ValueError(f"{path} has no version field after the sync")
        try:
            matches = same_version(normalize_version(value), version)
        except ValueError:
            matches = False
        if not matches:
            raise ValueError(f"the version field of {path} is {value!r}, not {version}")
        return {"path": path, "check": "version_field", "value": value}
    try:
        old = (base or b"").decode("utf-8").splitlines()
        new = expected.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not UTF-8 text and dct has no version reader for it") \
            from None
    pattern, changed, bad = _version_pattern(version), 0, []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new,
                                                       autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if j1 == j2:
            bad.append(f"line(s) {i1 + 1}-{i2} removed")
            continue
        for line in new[j1:j2]:
            changed += 1
            if not pattern.search(line):
                bad.append(line)
    if bad or not changed:
        raise ValueError(
            f"{path} (no version-field reader): every line the sync changes must hold "
            f"{version} — " + ("; ".join(repr(b) for b in bad[:5]) if bad else "nothing changed"))
    return {"path": path, "check": "lines_with_version", "changed_lines": changed}


# ── prepare: the isolated run and the proposal (K-76) ────────────────────────


def _git_state(repo: str) -> dict:
    """What the user's checkout must still be after an isolated sync: HEAD,
    every ref, the index, the status and the bytes of every untracked file."""
    _, refs = gitinfo.git_read(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    _, index = gitinfo.git_read(repo, "ls-files", "-s", "-z")
    _, status = gitinfo.git_read(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    untracked = {}
    for token in status.split("\0"):
        if token.startswith("?? "):
            data = _read_regular(os.path.join(repo, token[3:]))
            untracked[token[3:]] = _sha256(data) if isinstance(data, bytes) else str(data)
    return {"head": _rev(repo, "HEAD"), "refs": refs, "index": index,
            "status": sorted(t for t in status.split("\0") if t), "untracked": untracked}


def _state_diff(before: dict, after: dict) -> dict:
    out = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            out[key] = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        elif isinstance(old, list) and isinstance(new, list):
            out[key] = sorted(set(old) ^ set(new))
        else:
            out[key] = {"before": old, "after": new}
    return out


def _walk(root: str) -> dict[str, tuple]:
    """Every entry under `root` (a project CONFIRMED outside git): {rel:
    ("file", sha256, executable) | ("link", target) | ("other",)} —
    bounded by COPY_MAX_FILES / COPY_MAX_BYTES (refused beyond)."""
    entries: dict[str, tuple] = {}
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            rel = _rel(os.path.relpath(full, root))
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                continue
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ("link", os.readlink(full))
            elif stat.S_ISREG(st.st_mode):
                files += 1
                size += st.st_size
                if files > COPY_MAX_FILES or size > COPY_MAX_BYTES:
                    raise ReleaseRefused(
                        f"the project directory {root} holds more than {COPY_MAX_FILES} files / "
                        f"{COPY_MAX_BYTES} bytes — it cannot be copied for an isolated version "
                        f"sync; nothing prepared.", details={"files": files, "bytes": size})
                entries[rel] = ("file", _sha256(_read_bytes(full)), bool(st.st_mode & 0o111))
            else:
                entries[rel] = ("other",)
    return entries


@contextlib.contextmanager
def _isolated_worktree(repo: str, candidate: str) -> Iterator[str]:
    """A temporary linked worktree DETACHED at `candidate`, outside the
    user's checkout (under tempfile); the repository's hooks are disabled
    for this internal checkout (core.hooksPath = an empty directory — no
    post-checkout hook runs). Removed afterwards (`git worktree remove
    --force` + `git worktree prune`), also on failure."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dct-release-prepare-"))
    wt, hooks = os.path.join(tmp, "worktree"), os.path.join(tmp, "no-hooks")
    os.mkdir(hooks)
    try:
        gitinfo.git_write(repo, "-c", f"core.hooksPath={hooks}", "worktree", "add", "--detach",
                          "--quiet", wt, candidate, timeout=PREPARE_GIT_TIMEOUT_SECONDS)
        yield wt
    finally:
        with contextlib.suppress(gitinfo.CheckoutVerificationError):
            gitinfo.git_write(repo, "worktree", "remove", "--force", "--force", wt)
        with contextlib.suppress(gitinfo.CheckoutVerificationError):
            gitinfo.git_write(repo, "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


def _worktree_changes(wt: str, candidate: str) -> dict[str, tuple[str, str, str]]:
    """{repo-relative path: (status, mode, blob)} of everything the sync
    changed in the temporary worktree against the candidate — staged there
    with `git add -A` (the TEMPORARY worktree's own index; ignored files
    stay out; every blob lands in the shared object database)."""
    gitinfo.git_write(wt, "add", "-A", timeout=PREPARE_GIT_TIMEOUT_SECONDS)
    _, out = gitinfo.git_read(wt, "diff", "--cached", "--no-renames", "--name-status", "-z",
                              candidate)
    tokens = [t for t in out.split("\0") if t]
    changes = dict(zip(tokens[1::2], tokens[0::2], strict=True))
    found: dict[str, tuple[str, str, str]] = {}
    if changes:
        _, listing = gitinfo.git_read(wt, "ls-files", "-s", "-z", "--", *sorted(changes))
        for entry in (e for e in listing.split("\0") if e):
            meta, _, rel = entry.partition("\t")
            mode, blob, _stage = meta.split()
            found[rel] = (changes[rel], mode, blob.lower())
    for rel, status in changes.items():
        found.setdefault(rel, (status, "", ""))
    return found


def _project_prefix(run: _Run) -> str:
    rel = _rel(os.path.relpath(run.project_root, run.repo))
    return "" if rel == "." else rel + "/"


def _current_script(run: _Run) -> tuple[str, bytes] | None:
    """(identity, bytes) of the project's version-sync script: in git the
    CANDIDATE's file (repo-relative identity — the release runs only what
    the candidate holds; an untracked script is refused), outside git the
    file under the project root."""
    if run.repo is None:
        for rel in VERSION_SYNC_SCRIPTS:
            data = _read_regular(os.path.join(run.project_root, rel))
            if isinstance(data, bytes):
                return rel, data
        return None
    candidate = run.checkout["head"] if run.record is None else run.record["candidate_sha"]
    prefix = _project_prefix(run)
    for rel in VERSION_SYNC_SCRIPTS:
        full = prefix + rel
        data = gitinfo.git_read_blob(run.repo, candidate, full)
        if data is not None:
            return full, data
        if os.path.lexists(run.abspath(full)):
            raise run.refuse(
                f"the version-sync script {full} is not part of the candidate {candidate} "
                f"(untracked?) — commit it first: a release runs only what its candidate "
                f"holds; nothing prepared.", script=full)
    return None


def _prepare_git(run: _Run, version: str, script: tuple[str, bytes] | None) -> tuple:
    """The isolated sync of a project in git → (files, sync, changelog)."""
    repo, candidate = run.repo, run.checkout["head"]
    changelog = None
    if run.changelog_rel:
        blob = _blob_at(repo, candidate, run.changelog_rel)
        base = gitinfo.git_read_blob(repo, candidate, run.changelog_rel) if blob else None
        changelog = {"path": run.changelog_rel, "base_blob": blob,
                     "base_sha256": _sha256(base) if base is not None else None}
    if script is None:
        return {}, None, changelog
    before = _git_state(repo)
    prefix = _project_prefix(run)
    with _isolated_worktree(repo, candidate) as wt:
        sync = run_sync_script(os.path.join(wt, *script[0].split("/")), version,
                               os.path.join(wt, *prefix.split("/")) if prefix else wt)
        changed = _state_diff(before, _git_state(repo))
        if changed:
            raise run.refuse(
                "the version-sync script changed the checkout outside its isolated worktree "
                f"({', '.join(changed)}) — refused; dct restores nothing: review and undo "
                "those changes yourself, then fix the script; nothing prepared.",
                checkout_changed=changed, sync=sync)
        if sync["exit_code"] != 0:
            raise _sync_failed(run, script[0], sync)
        changes = _worktree_changes(wt, candidate)
    if run.changelog_rel in changes:
        raise run.refuse(
            f"the version-sync script modified the changelog file {run.changelog_rel} — dct "
            f"writes it (manages_changelog); fix the script; nothing prepared.",
            changelog=run.changelog_rel, sync=sync)
    files = {}
    for rel, (status, mode, blob) in sorted(changes.items()):
        if status not in ("A", "M") or mode not in ("100644", "100755"):
            raise run.refuse(
                f"the version-sync script {'deleted' if status == 'D' else 'retyped'} {rel} — "
                f"only regular files may be added or changed; nothing prepared.",
                path=rel, status=status, mode=mode)
        base = _blob_at(repo, candidate, rel)
        if base == blob:
            raise run.refuse(f"the version-sync script changed only the mode of {rel} — "
                             f"refused; nothing prepared.", path=rel)
        files[rel] = {
            "base": base, "expected": blob,
            "base_bytes": gitinfo.git_read_blob(repo, candidate, rel) if base else None,
            "expected_bytes": gitinfo.git_cat_blob(repo, blob),
        }
    return files, sync, changelog


def _prepare_plain(run: _Run, version: str, script: tuple[str, bytes] | None) -> tuple:
    """The isolated sync of a project CONFIRMED outside git — on a temporary
    COPY of its directory → (files, sync, changelog); every content kept in
    dct's release blob store."""
    root = run.project_root
    changelog = None
    if run.changelog_rel:
        data = _read_regular(run.abspath(run.changelog_rel))
        if data == _NOT_A_FILE:
            raise run.refuse(f"{run.changelog_rel} is not a regular file — nothing prepared.")
        digest = _store_put(data) if data is not None else None
        changelog = {"path": run.changelog_rel, "base_blob": digest, "base_sha256": digest}
    if script is None:
        return {}, None, changelog
    before = _walk(root)
    others = sorted(rel for rel, entry in before.items() if entry[0] == "other")
    if others:
        raise run.refuse(f"special files under the project directory ({', '.join(others[:5])}) "
                         f"— it cannot be copied for an isolated sync; nothing prepared.")
    files = {}
    with tempfile.TemporaryDirectory(prefix="dct-release-prepare-") as tmp:
        copy = os.path.join(os.path.realpath(tmp), "project")
        shutil.copytree(root, copy, symlinks=True)
        sync = run_sync_script(os.path.join(copy, *script[0].split("/")), version, copy)
        after = _walk(root)
        if after != before:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            raise run.refuse(
                "the version-sync script changed the project directory outside its isolated "
                f"copy ({', '.join(changed[:10])}) — refused; dct restores nothing: review and "
                "undo those changes yourself, then fix the script; nothing prepared.",
                checkout_changed=changed, sync=sync)
        if sync["exit_code"] != 0:
            raise _sync_failed(run, script[0], sync)
        result_entries = _walk(copy)
        deleted = sorted(set(before) - set(result_entries))
        changed = sorted(rel for rel, entry in result_entries.items() if before.get(rel) != entry)
        if run.changelog_rel in changed or run.changelog_rel in deleted:
            raise run.refuse(
                f"the version-sync script modified the changelog file {run.changelog_rel} — dct "
                f"writes it (manages_changelog); fix the script; nothing prepared.",
                changelog=run.changelog_rel, sync=sync)
        bad = deleted + [rel for rel in changed if result_entries[rel][0] != "file"
                         or (rel in before and before[rel][0] != "file")]
        if bad:
            raise run.refuse(
                f"the version-sync script deleted or retyped {', '.join(bad)} — only regular "
                f"files may be added or changed; nothing prepared.", paths=bad)
        for rel in changed:
            expected = _read_bytes(os.path.join(copy, *rel.split("/")))
            base = _read_bytes(os.path.join(root, *rel.split("/"))) if rel in before else None
            files[rel] = {"base": before[rel][1] if rel in before else None,
                          "expected": _store_put(expected), "base_bytes": base,
                          "expected_bytes": expected}
    return files, sync, changelog


def _build_proposal(run: _Run, version: str, plan_digest: str) -> tuple[dict, dict]:
    """Run the sync isolated and build the proposal (validate_proposal) —
    (proposal, report {sync, checks, script})."""
    script = _current_script(run)
    if script is None:
        run.step("sync", "skipped", reason="no version-sync script")
    prepare = _prepare_git if run.repo else _prepare_plain
    files, sync, changelog = prepare(run, version, script)
    if sync is not None:
        run.step("sync", "done", script=script[0], isolated=True)
    checks = []
    for rel, item in sorted(files.items()):
        try:
            checks.append(check_version_file(rel, item["base_bytes"], item["expected_bytes"],
                                             version))
        except ValueError as exc:
            raise run.refuse(f"{exc} — the version sync's result breaks the version-file "
                             f"contract; nothing prepared.", path=rel, sync=sync) from None
    proposal = ledger.validate_proposal({
        "paths": sorted(files),
        "base_blobs": {rel: item["base"] for rel, item in files.items()},
        "expected_blobs": {rel: item["expected"] for rel, item in files.items()},
        "script": None if script is None else {"identity": script[0],
                                               "sha256": _sha256(script[1])},
        "candidate_sha": run.checkout["head"] if run.checkout else None,
        "version": version,
        "plan_digest": plan_digest,
        "release_mode": run.mode,
        "changelog": changelog,
    })
    return proposal, {"sync": sync, "checks": checks}


# ── steps 3-4: the artifacts — approved content only, the script never runs ──


def _record_proposal(run: _Run) -> dict:
    """The approved proposal of the record, consistent with this run."""
    proposal = run.record.get("proposal")
    if proposal is None:
        raise run.refuse(
            f"release {run.version} has no approved version-file proposal (stamped outside "
            f"`dct release apply`, or before K-76) — its files cannot be written without "
            f"re-running the version sync, which dct never does after the stamp; nothing "
            f"done (abandon it with `dct release abandon`).")
    if (proposal.get("release_mode") or run.mode) != run.mode:
        raise run.refuse(f"the approved proposal of {run.version} is for a "
                         f"{proposal['release_mode']} release, this checkout is {run.mode} — "
                         f"nothing done.")
    if (proposal.get("changelog") or {}).get("path") != run.changelog_rel:
        raise run.refuse(
            f"the changelog file of the approved proposal "
            f"({(proposal.get('changelog') or {}).get('path')}) is not the project's "
            f"({run.changelog_rel}) — the changelog setting changed since the approval; "
            f"nothing done.")
    return proposal


def _changelog_baseline(run: _Run, proposal: dict) -> bytes | None:
    """F's content BEFORE the release (K-78: never the current file): the
    candidate's blob in git (it must be the approved baseline), outside git
    the persisted copy in dct's release blob store; None = absent."""
    approved = proposal["changelog"]
    if run.repo:
        candidate = run.record["candidate_sha"]
        blob = _blob_at(run.repo, candidate, run.changelog_rel)
        if blob != approved["base_blob"]:
            raise run.refuse(f"the candidate's {run.changelog_rel} is not the approved baseline "
                             f"— nothing done.")
        return gitinfo.git_read_blob(run.repo, candidate, run.changelog_rel) if blob else None
    if approved["base_blob"] is None:
        return None
    return _store_get(approved["base_blob"])


def _check_resume_tree(run: _Run) -> None:
    """Before the file effects of a 'stamped' record are retried: nothing
    staged or conflicted; unstaged paths only release artifacts (F and the
    approved proposal's files — their CONTENT is checked by _step_files)."""
    proposal = run.record.get("proposal") or {}
    state = planning.index_state(run.repo, [])
    if state["staged"] or state["conflicted"]:
        raise run.refuse(
            "staged or conflicted paths before the release files were recorded — unstage "
            "them yourself; nothing done.", staged=state["staged"],
            conflicted=state["conflicted"])
    allowed = set(proposal.get("paths") or ()) | {run.changelog_rel}
    foreign = sorted(p for p in state["unstaged"] if p not in allowed)
    if foreign:
        raise run.refuse(f"foreign changes in the work tree: {', '.join(foreign)} — "
                         f"nothing done.", foreign=foreign)


def _verified_manifest(run: _Run, proposal: dict, expected_f: bytes | None) -> dict:
    """K-77: read the FULL artifact set ONCE and compute every hash and the
    expected tree from that one read; every artifact must be exactly the
    approved content (F: its baseline + the rendered section) — a mismatch
    refuses and records nothing."""
    rels = ([run.changelog_rel] if run.changelog_rel else []) + list(proposal["paths"])
    data = {rel: _read_regular(run.abspath(rel)) for rel in rels}
    ids, bad = {}, []
    if run.changelog_rel and data[run.changelog_rel] != expected_f:
        bad.append(run.changelog_rel)
    for rel in proposal["paths"]:
        if not isinstance(data[rel], bytes):
            bad.append(rel)
            continue
        ids[rel] = _id_of_bytes(run, rel, data[rel])
        if ids[rel] != proposal["expected_blobs"][rel]:
            bad.append(rel)
    if bad:
        raise run.refuse(
            f"after writing, {', '.join(bad)} differ(s) from the approved content (changed "
            f"meanwhile?) — 'files_written' is NOT recorded and nothing is overwritten; "
            f"restore the file(s) as approved and resume, or abandon the release.", changed=bad)
    manifest: dict = {"version_files": [{"path": rel, "sha256": _sha256(data[rel])}
                                        for rel in sorted(proposal["paths"])]}
    if run.changelog_rel:
        _, date = render_section(run.engine, run.slug, run.record)
        manifest["changelog"] = {"path": run.changelog_rel,
                                 "sha256": _sha256(data[run.changelog_rel]),
                                 "section_date": date}
    if run.repo:
        if run.changelog_rel:
            ids[run.changelog_rel] = _id_of_bytes(run, run.changelog_rel,
                                                  data[run.changelog_rel])
        manifest["tree"] = _tree_of_blobs(run.repo, run.record["candidate_sha"], ids)
    return manifest


def _step_files(run: _Run) -> None:
    """Steps 3-4 for a 'stamped' record → 'files_written' with the manifest.

    K-75: every artifact is CLASSIFIED before anything is written — its
    current content is the baseline (→ written), the approved content (→
    kept) or anything else (→ refused: nothing written, the index
    untouched, never restored automatically). Then F and the approved
    blobs are written (the script never runs), and the result is verified
    from one read (K-77)."""
    proposal = _record_proposal(run)
    actions: list[tuple[str, str]] = []
    foreign: list[str] = []
    expected_f = None
    if run.changelog_rel:
        base = _changelog_baseline(run, proposal)
        try:
            expected_f = expected_changelog(run.engine, run.slug, run.record, base)
        except ReleaseRefused as exc:
            raise run.refuse(str(exc), path=run.changelog_rel, **exc.details) from None
        current = _read_regular(run.abspath(run.changelog_rel))
        if current == expected_f:
            actions.append((run.changelog_rel, "verified"))
        elif current == base:
            actions.append((run.changelog_rel, "write"))
        else:
            foreign.append(run.changelog_rel)
    for rel in proposal["paths"]:
        current = _content_id(run, rel)
        if current == proposal["expected_blobs"][rel]:
            actions.append((rel, "verified"))
        elif current == proposal["base_blobs"][rel]:
            actions.append((rel, "write"))
        else:
            foreign.append(rel)
    if foreign:
        raise run.refuse(
            f"{', '.join(foreign)} differ(s) from both the content before the release and the "
            f"approved content (a partial or foreign edit?) — not overwritten: nothing "
            f"written, the index untouched; restore the file(s) yourself (or finish them "
            f"exactly as approved) and resume, or abandon the release.", foreign=foreign)
    todo = dict(actions)
    _point(run.on_point, "before_changelog")
    if run.changelog_rel:
        if todo[run.changelog_rel] == "write":
            _write_atomic(run.abspath(run.changelog_rel), expected_f)
            run.step("changelog", "done", path=run.changelog_rel)
        else:
            run.step("changelog", "verified", path=run.changelog_rel)
    else:
        run.step("changelog", "skipped", reason="no changelog file managed")
    _point(run.on_point, "after_changelog")
    _point(run.on_point, "before_version_files")
    written = []
    for rel in proposal["paths"]:
        if todo[rel] != "write":
            continue
        _write_atomic(run.abspath(rel), _approved_bytes(run, rel, proposal["expected_blobs"][rel]))
        written.append(rel)
        _point(run.on_point, "after_version_file")
    run.step("version_files", "done" if written else "verified", paths=list(proposal["paths"]),
             written=written)
    _point(run.on_point, "after_version_files")
    manifest = _verified_manifest(run, proposal, expected_f)
    run.advance("files_written", manifest=manifest)
    _point(run.on_point, "after_files_written")


def _manifest_paths(manifest: dict) -> list[str]:
    paths = [m["path"] for m in manifest.get("version_files") or []]
    if manifest.get("changelog"):
        paths.append(manifest["changelog"]["path"])
    return sorted(paths)


def _verify_manifest_files(run: _Run) -> None:
    """Every artifact of the manifest holds its recorded content."""
    manifest = run.record["manifest"] or {}
    entries = list(manifest.get("version_files") or [])
    if manifest.get("changelog"):
        entries.append(manifest["changelog"])
    bad = []
    for item in entries:
        data = _read_regular(run.abspath(item["path"]))
        if not isinstance(data, bytes) or _sha256(data) != item["sha256"]:
            bad.append(item["path"])
    if bad:
        raise run.refuse(
            f"release artifacts differ from the recorded manifest: {', '.join(bad)} — "
            f"not overwritten; restore them (or abandon the release).", changed=bad)


# ── step 5: the release commit ───────────────────────────────────────────────


def _verify_release_commit(run: _Run, commit: str) -> None:
    manifest = run.record["manifest"] or {}
    tree, parents = _tree(run.repo, commit), _parents(run.repo, commit)
    if tree != manifest.get("tree") or parents != [run.record["candidate_sha"]]:
        raise run.refuse(
            f"commit {commit} is not the release commit of {run.version}: tree {tree} "
            f"(expected {manifest.get('tree')}), parents {parents} (expected "
            f"[{run.record['candidate_sha']}]) — nothing done.",
            commit=commit, tree=tree, parents=parents)


def _is_release_commit(run: _Run, commit: str) -> bool:
    """`commit` is THE release commit of the record: its only parent is the
    candidate, its tree the manifest's, its subject `release: v<V>` (a
    trailer a commit-msg hook appended is fine)."""
    facts = _commit_facts(run.repo, commit)
    return (facts["parents"] == [run.record["candidate_sha"]]
            and facts["tree"] == (run.record["manifest"] or {}).get("tree")
            and facts["subject"] == RELEASE_SUBJECT.format(version=run.version))


def _check_index_for_commit(run: _Run, candidate: str, expected: str, paths: list[str]) -> None:
    """K-50 / K-79, read-only, before the commit: the user's index may hold
    nothing but release artifacts already staged as their EXPECTED blobs —
    a foreign staged path, foreign content inside a release file or an
    unmerged path refuses with the staging untouched; an existing
    index.lock (another git process) refuses before anything is created."""
    lock = _git_path(run.repo, "index") + ".lock"
    if os.path.lexists(lock):
        raise run.refuse(
            f"{lock} exists — another git process is using the index; dct never removes a "
            f"foreign index.lock; nothing committed — run `dct release resume` once it is gone.",
            index_lock=lock)
    staged = _staged_against(run.repo, candidate)
    expected_entries = _tree_entries(run.repo, expected, paths)
    index_entries = _index_entries(run.repo, staged & set(paths))
    foreign_paths = sorted(staged - set(paths))
    foreign_content = sorted(p for p in staged & set(paths)
                             if index_entries.get(p) != expected_entries.get(p))
    if foreign_paths or foreign_content:
        raise run.refuse(
            "the index holds changes that are not the release artifacts "
            f"({', '.join(foreign_paths + foreign_content)}) — your staging is left "
            "untouched; unstage them yourself and resume.",
            foreign_paths=foreign_paths, foreign_content=foreign_content)


def _publish_commit(run: _Run, commit: str, paths: list[str]) -> None:
    """After a verified release commit: the real index follows it (K-79,
    _sync_real_index), then 'committed' with release_commit_sha."""
    updated = _sync_real_index(run, commit, paths)
    run.step("index", "done" if updated else "verified", paths=updated)
    _point(run.on_point, "after_index")
    run.advance("committed", release_commit_sha=commit)
    _point(run.on_point, "after_committed")


def _step_commit(run: _Run) -> None:
    """'files_written' → 'committed' (see module doc, step 5)."""
    record, repo = run.record, run.repo
    candidate, manifest = record["candidate_sha"], record["manifest"] or {}
    subject = RELEASE_SUBJECT.format(version=run.version)
    paths = _manifest_paths(manifest)
    head = _head(repo)
    if head != candidate:
        # Interrupted between the commit and its progress write: adopt HEAD
        # only when it IS the release commit (parent, tree, subject).
        if _is_release_commit(run, head):
            _verify_manifest_files(run)
            run.step("commit", "adopted", commit=head)
            _publish_commit(run, head, paths)
            return
        raise run.refuse(
            f"HEAD is {head}, not the release candidate {candidate} — the release commit "
            f"is created on its candidate only; nothing done.", head=head, candidate=candidate)
    _verify_manifest_files(run)
    expected = expected_tree(repo, candidate, paths)
    if expected != manifest.get("tree"):
        raise run.refuse(f"the artifacts no longer build the recorded tree "
                         f"{manifest.get('tree')} (got {expected}) — nothing committed.")
    _check_index_for_commit(run, candidate, expected, paths)
    _point(run.on_point, "before_commit")
    commit = _hook_commit(run.refuse, repo, expected, candidate, subject)
    if not _is_release_commit(run, commit):
        facts = _commit_facts(repo, commit)
        raise run.refuse(
            f"the commit `git commit` created ({commit}; the repository's hooks ran) is not the "
            f"release commit of {run.version}: tree {facts['tree']} (approved "
            f"{manifest.get('tree')}), parents {facts['parents']}, subject {facts['subject']!r} "
            f"— a hook changed it (a formatter, restaged files) or HEAD moved. It is NOT "
            f"adopted: HEAD is {commit}, the record stays 'files_written', nothing tagged or "
            f"pushed; dct restores nothing — reset HEAD to {candidate} and the artifacts as "
            f"approved yourself (or abandon the release), then resume.",
            head=commit, expected_tree=manifest.get("tree"), **facts)
    try:
        _verify_manifest_files(run)
    except ReleaseRefused as exc:
        raise run.refuse(
            f"the release commit {commit} was created with the approved tree, but a hook "
            f"changed the work tree afterwards ({', '.join(exc.details.get('changed', []))}) — "
            f"not recorded; restore those files to the commit's content yourself, then resume.",
            commit=commit, head=commit, changed=exc.details.get("changed", [])) from None
    run.step("commit", "done", commit=commit)
    _point(run.on_point, "after_commit")
    _publish_commit(run, commit, paths)


def _check_committed_head(run: _Run) -> None:
    """From 'committed' on HEAD must be the release commit or a descendant —
    no file effect remains, the sync is never re-run on a newer HEAD."""
    commit, head = run.record["release_commit_sha"], _head(run.repo)
    if head != commit and not gitinfo.git_is_ancestor(run.repo, commit, head):
        raise run.refuse(
            f"HEAD {head} does not contain the release commit {commit} — nothing done.",
            head=head, release_commit=commit)


# ── step 6: the tag ──────────────────────────────────────────────────────────


def _step_tag(run: _Run) -> None:
    """'committed' → 'tagged': `v<V>` (lightweight) at the verified commit."""
    commit = run.record["release_commit_sha"]
    _verify_release_commit(run, commit)
    name = f"v{run.version}"
    tags = _version_tags(run.repo, run.version)
    elsewhere = [t for t in tags if t["commit"] != commit]
    if elsewhere:
        raise run.refuse(
            f"tag(s) {', '.join(t['name'] for t in elsewhere)} of {run.version} point at "
            f"another commit — nothing tagged.", tags=elsewhere)
    here = [t["name"] for t in tags]
    if here:
        run.step("tag", "verified", tag=here[0] if name not in here else name)
    else:
        gitinfo.git_write(run.repo, "tag", name, commit)
        if _rev(run.repo, f"refs/tags/{name}^{{commit}}") != commit:
            raise run.refuse(f"tag {name} does not point at {commit} after creating it")
        run.step("tag", "done", tag=name)
    _point(run.on_point, "after_tag")
    run.advance("tagged")
    _point(run.on_point, "after_tagged")


# ── step 7: the push (K-97 F2) ───────────────────────────────────────────────

_PUSH_CHOICE = ("choose explicitly: `--push-remote <remote> [--push-branch <branch>]` (the "
                "remote and branch the release commit and its tag go to) or `--no-push` (a "
                "local release, nothing pushed)")


def _config(repo: str, key: str) -> str | None:
    code, out = gitinfo.git_read(repo, "config", "--get", key, ok_codes=(0, 1))
    return (out.strip() or None) if code == 0 else None


def _remote_url(repo: str, remote: str) -> str | None:
    code, out = gitinfo.git_read(repo, "remote", "get-url", "--push", remote,
                                 ok_codes=(0, 1, 2, 128))
    return (out.strip() or None) if code == 0 else None


def push_target(run: _Run, version: str, *, push_remote: str | None = None,
                push_branch: str | None = None, no_push: bool = False) -> dict | None:
    """The push target the owner approves with apply (K-97 F2) — None for a
    project outside git or with `no_push` (an explicit local release).
    Default: the current branch's UPSTREAM (`branch.<b>.remote` +
    `branch.<b>.merge`); no upstream (also a detached HEAD, or a local
    upstream `.`) → refused with the explicit choice, never guessed.
    `push_remote` names the remote; its branch is `push_branch`, else the
    upstream's branch when the upstream is on that remote, else refused.
    → {remote, url (its push URL now), branch, tag: "v<V>"}. Read-only, no
    network."""
    if run.repo is None:
        if push_remote or push_branch:
            raise run.refuse("the project is outside git — there is nothing to push; drop "
                             "--push-remote / --push-branch.")
        return None
    if no_push:
        if push_remote or push_branch:
            raise run.refuse("--no-push contradicts --push-remote / --push-branch — choose one.")
        return None
    branch = planning._current_branch(run.repo)
    up_remote = _config(run.repo, f"branch.{branch}.remote") if branch else None
    up_merge = _config(run.repo, f"branch.{branch}.merge") if branch else None
    upstream = up_merge[len("refs/heads/"):] if up_remote and up_remote != "." and up_merge \
        and up_merge.startswith("refs/heads/") else None
    if push_remote is None:
        if push_branch:
            raise run.refuse("--push-branch needs --push-remote.")
        if upstream is None:
            where = f"the branch {branch}" if branch else "a detached HEAD"
            raise run.refuse(
                f"{where} has no upstream (a remote branch it tracks) — dct pushes the release "
                f"commit and its tag but never guesses where; {_PUSH_CHOICE}; nothing released.",
                branch=branch, push_target=None)
        remote, target_branch = up_remote, upstream
    else:
        remote = push_remote
        target_branch = push_branch or (upstream if up_remote == push_remote else None)
        if not target_branch:
            raise run.refuse(f"name the branch of {push_remote} to push to: --push-branch "
                             f"<branch>; nothing released.", remote=push_remote)
    url = _remote_url(run.repo, remote)
    if url is None:
        raise run.refuse(f"there is no remote named {remote!r} — {_PUSH_CHOICE}; nothing "
                         f"released.", remote=remote)
    code, _ = gitinfo.git_read(run.repo, "check-ref-format", f"refs/heads/{target_branch}",
                               ok_codes=(0, 1))
    if code != 0:
        raise run.refuse(f"{target_branch!r} is not a valid branch name — nothing released.")
    return {"remote": remote, "url": url, "branch": target_branch, "tag": f"v{version}"}


def _check_push_precondition(run: _Run, target: dict, version: str, candidate: str) -> None:
    """Before the stamp (network, K-97 F2): the remote must not hold a tag of
    V already (any spelling — the release commit does not exist yet, so any
    such tag is elsewhere), and its branch must be absent or contained in
    the candidate (else the push could never be a fast-forward)."""
    refs = _ls_remote(run.repo, target["remote"])
    tags = _remote_version_tags(refs, version)
    if tags:
        raise run.refuse(
            f"the remote {target['remote']} already has a tag of {version} "
            f"({', '.join(t['name'] for t in tags)}) — refused before the stamp; nothing "
            f"released.", remote_tags=tags, push_target=target)
    tip = refs.get(f"refs/heads/{target['branch']}")
    if tip is not None and not _contains(run.repo, candidate, tip):
        raise run.refuse(
            f"{target['remote']}/{target['branch']} is at {tip}, which the candidate "
            f"{candidate} does not contain — the push would be rejected; fetch and integrate "
            f"it first (then plan again); nothing released.", remote_branch=tip,
            push_target=target)


def _consented_target(run: _Run) -> dict | None:
    """The push target of the record's consent (K-97 F4) — the remote must
    still have the approved push URL (anything else is outside the consent:
    refused, never re-derived from today's upstream)."""
    target = (run.record.get("consent") or {}).get("push_target")
    if target is None:
        return None
    url = _remote_url(run.repo, target["remote"])
    if url != target["url"]:
        raise run.refuse(
            f"the approved push target {target['remote']} ({target['url']}) is not this "
            f"repository's remote any more (now {url or 'missing'}) — outside the consent of "
            f"{run.version}: restore the remote, or abandon the release; nothing pushed.",
            push_target=target, url_now=url)
    return target


def _pushed(run: _Run, refs: dict[str, str], target: dict, commit: str) -> bool:
    """The remote holds the release: its branch is the release commit (or a
    known descendant) and the tag points exactly at it. A tag of V at
    another commit → refused."""
    elsewhere = [t for t in _remote_version_tags(refs, run.version) if t["commit"] != commit]
    if elsewhere:
        raise run.refuse(
            f"tag(s) {', '.join(t['name'] for t in elsewhere)} of {run.version} on the remote "
            f"{target['remote']} point at another commit — nothing pushed.",
            remote_tags=elsewhere, push_target=target)
    return (refs.get(f"refs/tags/{target['tag']}") == commit
            and _contains(run.repo, refs.get(f"refs/heads/{target['branch']}"), commit))


def _step_push(run: _Run) -> None:
    """'tagged' → 'pushed' (K-97 F2): `git push --atomic <remote>
    <commit>:refs/heads/<branch> <commit>:refs/tags/<tag>` to the CONSENTED
    target (the remote's hooks and the local pre-push hook run), verified
    with `git ls-remote`. Already there → verified, not pushed again. A
    failure leaves 'tagged' — resume retries only this step."""
    target = _consented_target(run)
    if target is None:
        run.step("push", "skipped", reason="no push target approved")
        return
    commit = run.record["release_commit_sha"]
    where = {"remote": target["remote"], "branch": target["branch"], "tag": target["tag"]}
    if _pushed(run, _ls_remote(run.repo, target["remote"]), target, commit):
        run.step("push", "verified", **where)
    else:
        _point(run.on_point, "before_push")
        try:
            gitinfo.git_write(run.repo, "push", "--atomic", target["remote"],
                              f"{commit}:refs/heads/{target['branch']}",
                              f"{commit}:refs/tags/{target['tag']}",
                              network=True, timeout=HOOK_GIT_TIMEOUT_SECONDS)
        except gitinfo.GitWriteError as exc:
            raise run.refuse(
                f"the push of {run.version} to {target['remote']} ({target['branch']}, "
                f"{target['tag']}) failed ({exc}) — the release stays 'tagged' (commit and tag "
                f"exist locally); fix the cause and run `dct release resume`: it retries ONLY "
                f"the push.", git_error=str(exc), push_target=target) from None
        if not _pushed(run, _ls_remote(run.repo, target["remote"]), target, commit):
            raise run.refuse(
                f"after the push, {target['remote']} does not show {target['branch']} and "
                f"{target['tag']} at {commit} — not recorded as pushed.", push_target=target)
        run.step("push", "done", **where)
        _point(run.on_point, "after_push")
    run.advance("pushed")
    _point(run.on_point, "after_pushed")


def _verify_push(run: _Run) -> None:
    """A 'pushed' record on resume: the remote must still show it."""
    target = _consented_target(run)
    if target is None:
        run.step("push", "verified", reason="no push target approved")
        return
    if not _pushed(run, _ls_remote(run.repo, target["remote"]), target,
                   run.record["release_commit_sha"]):
        raise run.refuse(
            f"release {run.version} is recorded as pushed, but {target['remote']} does not "
            f"show {target['branch']} and {target['tag']} at its release commit — nothing "
            f"done.", push_target=target)
    run.step("push", "verified", remote=target["remote"], branch=target["branch"],
             tag=target["tag"])


# ── the pipeline (apply after the stamp, and resume) ─────────────────────────


def _continue(run: _Run, *, fresh: bool) -> None:
    """Perform / verify every effect still missing for run.record (`fresh`:
    straight after the stamp of a pre-checked apply; else a resume)."""
    status = run.record["status"]
    if run.repo:
        if status == "stamped":
            head = _head(run.repo)
            if head != run.record["candidate_sha"]:
                raise run.refuse(
                    f"HEAD is {head}, not the release candidate {run.record['candidate_sha']} "
                    f"— artifacts are created on the candidate only; nothing done.",
                    head=head, candidate=run.record["candidate_sha"])
            if not fresh:
                _check_resume_tree(run)
            _step_files(run)
        elif status == "files_written":
            run.step("files", "verified")
        if run.record["status"] == "files_written":
            _step_commit(run)
        elif run.record["status"] in ("committed", "tagged", "pushed"):
            _check_committed_head(run)
            _verify_release_commit(run, run.record["release_commit_sha"])
            run.step("commit", "verified", commit=run.record["release_commit_sha"])
        if run.record["status"] == "committed":
            _step_tag(run)
        elif run.record["status"] in ("tagged", "pushed"):
            commit = run.record["release_commit_sha"]
            if not any(t["commit"] == commit for t in _version_tags(run.repo, run.version)):
                raise run.refuse(f"release {run.version} is recorded as tagged but no tag of "
                                 f"it points at {commit} — nothing done.")
            run.step("tag", "verified")
        if run.record["status"] == "tagged":
            _step_push(run)
        elif run.record["status"] == "pushed":
            _verify_push(run)
    else:
        if status == "stamped":
            _step_files(run)
        else:
            _verify_manifest_files(run)
            run.step("files", "verified")
    run.advance("completed")
    run.step("complete", "done")


def _finish(action: str, run: _Run, outcome: str, **details) -> dict:
    return result(action, run.slug, outcome, version=run.record["version"] if run.record else None,
                  release=run.record, steps=run.steps, warnings=run.warnings, details=details)


# ── public API ───────────────────────────────────────────────────────────────


class NothingToPrepare(ReleaseRefused):
    """prepare has nothing to prepare: the version already has a record, or
    it is lower than the current version (releases never rewind)."""


def _entry_selection(slug: str, plan: dict, target: str, entry_ids, no_entries: bool,
                     strict: bool) -> list[int]:
    proven = {e["id"] for e in plan["entries"]["proven"]}
    unproven = {e["id"]: e["reason"] for e in plan["entries"]["unproven"]}
    # A confirmed project without git (K-63) has no commits to prove with:
    # its entries are the owner's explicit choice, as before the proof layer.
    proven |= {i for i, reason in unproven.items() if reason == "no_git"}
    unproven = {i: r for i, r in unproven.items() if i not in proven}
    if no_entries:
        return []
    if entry_ids is None:
        if not strict:
            raise ReleaseRefused("choose the entries explicitly: --entries <ids> or --no-entries")
        if unproven:
            listing = ", ".join(f"#{i} ({r})" for i, r in sorted(unproven.items()))
            raise ReleaseRefused(
                f"unreleased entries without proof: {listing} — decide them first "
                f"(`dct release attest --entries … --confirm`, correct, withdraw, or release "
                f"a selection with `dct release apply --entries …`); nothing released.",
                details={"unproven": sorted(unproven)})
        if not proven:
            raise ReleaseRefused(
                f"No unreleased entries for '{slug}' — a version-only release of {target} "
                f"must be explicit (--no-entries); nothing released.")
        return sorted(proven)
    ids = sorted(set(entry_ids))
    if not ids:
        raise ReleaseRefused("--entries is empty — use --no-entries for a version-only release")
    # Ids the plan does not list at all (unknown, foreign, deleted, released)
    # go to core's STRICT validation — the stamp transaction admits a NEW
    # stamp only for the plan's proven ids (stampable_ids, K-81); the one
    # exception is an entry already carrying V (the legacy set, K-61).
    bad = [i for i in ids if i in unproven]
    if bad:
        listing = ", ".join(f"#{i} ({unproven[i]})" for i in bad)
        raise ReleaseRefused(
            f"entries the plan does not prove: {listing} — attest them first "
            f"(`dct release attest`) or leave them out; nothing released.",
            details={"unproven": bad})
    return ids


def _check_selection_args(action: str, entry_ids, no_entries: bool, plan_digest: str | None,
                          strict: bool) -> None:
    if entry_ids is not None and no_entries:
        raise ReleaseRefused("--entries and --no-entries are mutually exclusive")
    if plan_digest is None and not strict:
        raise ReleaseRefused(
            f"{action} needs --plan-digest <snapshot_digest> of the plan the owner approved "
            f"(`dct release plan --version V --entries … --json`); nothing released.")
    if plan_digest is not None and not _DIGEST_RE.fullmatch(plan_digest):
        raise ReleaseRefused(f"--plan-digest must be a lowercase hex SHA-256, got {plan_digest!r}")


def _approved_context(engine: sa.Engine, project_slug: str, located: tuple, *, version,
                      bump_type, entry_ids, no_entries: bool, plan_digest: str | None,
                      strict: bool, cwd: str | None,
                      on_point) -> tuple[_Run, dict, str, list[int], set[str]]:
    """The pre-checks shared by prepare and apply (module doc, stage 1) —
    run UNDER the operation lock (`located` = _locate's result): the strict
    checkout, no unfinished record, a clean index / work tree, the plan
    re-derived FOR THE PINNED CANDIDATE and compared with the approved
    digest, the entry selection. Returns (run, plan, target version,
    selected ids, the untracked files present now)."""
    project, checkout, project_root = located
    if not project.get("path"):
        # K-95 C1: without a path the absence of a repository is NOT confirmed.
        raise ReleaseRefused(
            f"project '{project_slug}' has no registered path — its checkout cannot be "
            f"verified (a project outside git must be CONFIRMED outside git); register the "
            f"path first; nothing released.")
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=on_point)
    unfinished = ledger.get_release(engine, project_slug)
    if unfinished:
        raise ReleaseRefused(
            f"release {unfinished['version']} is unfinished ({unfinished['status']}) — run "
            f"`dct release resume` (or abandon it) first; nothing released.",
            details={"unfinished": unfinished["version"]}, release=unfinished)
    pre_untracked: set[str] = set()
    if checkout:
        state = _clean_tree(run, planning._artifacts(
            project, checkout, ledger.get_version_files(engine, project_slug)["version_files"]))
        pre_untracked = set(state["untracked"])
    try:
        plan = planning.build_plan(
            engine, project_slug, version=version, bump_type=bump_type, cwd=cwd,
            entry_ids=None if entry_ids is None else list(entry_ids), no_entries=no_entries,
            pinned_checkout=checkout)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    target = plan["version"]["target"]
    if not target:
        raise ReleaseRefused("Provide a version or --patch/--minor/--major")
    if plan_digest is not None and plan["snapshot_digest"] != plan_digest:
        raise ReleaseRefused(
            "the release context differs from the approved plan (HEAD, range base, "
            "unreleased entries, selection, version or proof changed) — run `dct release "
            "plan … --json` again, review it and approve its snapshot_digest; nothing "
            "released.",
            details={"approved_digest": plan_digest, "derived_digest": plan["snapshot_digest"],
                     "snapshot": plan["snapshot"]})
    if strict and plan["range"] and plan["range"]["confirm_required"]:
        raise ReleaseRefused(
            f"the proof range starts at {plan['range']['base_tag']}, not at "
            f"v{plan['version']['current']} — confirm it with `dct release apply … --confirm` "
            f"after reviewing `dct release plan`; nothing released.")
    run.warnings.extend(plan["warnings"])
    ids = _entry_selection(project_slug, plan, target, entry_ids, no_entries, strict)
    return run, plan, target, ids, pre_untracked


def _newer_than_current(project: dict, target: str) -> bool:
    current = project.get("current_version")
    if not current:
        return True
    try:
        return compare_versions(target, current) > 0
    except ValueError:
        return False  # an unparseable current version: the stamp refuses it (K-54)


def _prepared(run: _Run, target: str, plan: dict) -> dict:
    """Build the proposal (isolated sync) and store it as PENDING → the
    prepare result's details."""
    proposal, report = _build_proposal(run, target, plan["snapshot_digest"])
    stored = ledger.set_pending_proposal(run.engine, run.slug, proposal)
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    run.step("proposal", "done", proposal_sha256=stored["proposal_sha256"])
    return {
        "proposal": stored["proposal"],
        "proposal_sha256": stored["proposal_sha256"],
        "plan_digest": plan["snapshot_digest"],
        "contract": {"confirmed": contract, "proposed": stored["proposal"]["paths"],
                     "change": contract != stored["proposal"]["paths"]},
        "sync": report["sync"],
        "checks": report["checks"],
    }


def prepare_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    version: str | None = None,
    bump_type: str | None = None,
    entry_ids: Iterable[int] | None = None,
    no_entries: bool = False,
    plan_digest: str | None = None,
    strict: bool = False,
    cwd: str | None = None,
) -> dict:
    """The PREPARE stage (`dct release prepare`, K-76) — see module doc,
    stage 0. The same pre-checks, candidate pinning and approved-plan
    comparison as apply (`plan_digest` required unless `strict`); then the
    version-sync script runs ONCE with V, isolated (never in the user's
    checkout; the checkout is verified unchanged afterwards), and the
    proposal built from its result is stored as the project's PENDING
    proposal, replacing an earlier one. Nothing is stamped.

    Returns result() with outcome 'prepared' and details {proposal (core
    validate_proposal), proposal_sha256 (what `apply --proposal-digest`
    takes), plan_digest, contract {confirmed (the current contract or
    null), proposed (the proposal's paths), change (bool)}, sync (null
    without a script, else run_sync_script's report), checks (one
    check_version_file result per file)}. Raises NothingToPrepare (V is
    already recorded, or lower than the current version), SyncFailed (the
    script failed / timed out — exit code = the script's, 124 on timeout)
    or ReleaseRefused (a pre-check, a script that wrote outside its
    isolated copy, touched the changelog, deleted / retyped a file, or a
    version file whose version field is not V)."""
    _check_selection_args("prepare", entry_ids, no_entries, plan_digest, strict)
    located = _locate(engine, project_slug, cwd)
    with _operation(located[0], located[1], "prepare"):
        return _prepare(engine, project_slug, located, version=version, bump_type=bump_type,
                        entry_ids=entry_ids, no_entries=no_entries, plan_digest=plan_digest,
                        strict=strict, cwd=cwd)


def _prepare(engine, project_slug, located, *, version, bump_type, entry_ids, no_entries,
             plan_digest, strict, cwd) -> dict:
    run, plan, target, ids, _ = _approved_context(
        engine, project_slug, located, version=version, bump_type=bump_type,
        entry_ids=entry_ids, no_entries=no_entries, plan_digest=plan_digest, strict=strict,
        cwd=cwd, on_point=None)
    recorded = ledger.get_release(engine, project_slug, target)
    if recorded is not None:
        raise NothingToPrepare(f"{target} already has a release record ({recorded['status']}) "
                               f"— nothing to prepare.", release=recorded)
    if run.project.get("current_version") and not _newer_than_current(run.project, target) \
            and not same_version(run.project["current_version"], target):
        raise NothingToPrepare(f"{target} is lower than the current version "
                               f"{run.project['current_version']} — releases never rewind; "
                               f"nothing to prepare.")
    details = _prepared(run, target, plan)
    try:  # what apply would push to by default — shown for the approval (read-only)
        default_push = push_target(run, target)
    except ReleaseRefused as exc:
        default_push = None
        run.warnings.append(str(exc))
    return result("prepare", project_slug, "prepared", version=target, steps=run.steps,
                  warnings=run.warnings,
                  details={**details, "entry_ids": ids, "push_target": default_push})


def _check_approved_proposal(run: _Run, plan: dict, target: str, approval: str) -> dict:
    """K-76 — before the stamp: the approved digest names the PENDING
    proposal, bound to THIS candidate, version, plan digest and mode; the
    script and the baseline (every proposal path, outside git also the
    changelog) are unchanged; every expected blob is still available."""
    hint = "run `dct release prepare …` again, review it and approve its proposal_sha256"
    pending = ledger.get_pending_proposal(run.engine, run.slug)
    if pending is None:
        raise run.refuse(f"no pending version-file proposal for '{run.slug}' — {hint}; nothing "
                         f"released.", approved=approval)
    if pending["proposal_sha256"] != approval:
        raise run.refuse(
            f"--proposal-digest {approval} is not the pending proposal "
            f"({pending['proposal_sha256']}) — a newer prepare replaced it, or the digest is "
            f"wrong; {hint}; nothing released.",
            approved=approval, pending=pending["proposal_sha256"])
    proposal = pending["proposal"]
    now = {"candidate_sha": run.checkout["head"] if run.checkout else None,
           "version": target, "plan_digest": plan["snapshot_digest"], "release_mode": run.mode}
    mismatch = {key: {"approved": proposal.get(key), "now": value}
                for key, value in now.items() if proposal.get(key) != value}
    if mismatch:
        raise run.refuse(
            f"the approved proposal was prepared for another release context "
            f"({', '.join(mismatch)}) — a changed candidate, version or plan voids the "
            f"approval; {hint}; nothing released.", mismatch=mismatch)
    script = _current_script(run)
    approved_script = proposal.get("script")
    if (script is None) != (approved_script is None) or (script is not None and (
            approved_script["identity"] != script[0]
            or approved_script["sha256"] != _sha256(script[1]))):
        raise run.refuse(
            f"the version-sync script changed since the proposal was prepared — the approval "
            f"is void; {hint}; nothing released.",
            approved_script=approved_script,
            script=None if script is None else {"identity": script[0],
                                                "sha256": _sha256(script[1])})
    changed = [rel for rel in proposal["paths"]
               if _content_id(run, rel) != proposal["base_blobs"][rel]]
    approved_f = proposal.get("changelog")
    if (approved_f or {}).get("path") != run.changelog_rel:
        changed.append(f"changelog setting ({run.changelog_rel})")
    elif approved_f and run.repo is None:
        current = _read_regular(run.abspath(run.changelog_rel))
        digest = _sha256(current) if isinstance(current, bytes) else current
        if digest != approved_f["base_sha256"]:
            changed.append(run.changelog_rel)
    if changed:
        raise run.refuse(
            f"{', '.join(changed)} changed since the proposal was prepared — the approval is "
            f"void; {hint}; nothing released.", changed=changed)
    blobs = list(proposal["expected_blobs"].values())
    if approved_f and approved_f["base_blob"] and run.repo is None:
        blobs.append(approved_f["base_blob"])
    missing = [blob for blob in blobs if not _blob_available(run, blob)]
    if missing:
        raise run.refuse(
            f"approved content is no longer available ({', '.join(missing)}; e.g. pruned "
            f"by `git gc`) — {hint}; nothing released.", missing_blobs=missing)
    return proposal


def _alias_approval(run: _Run, target: str, plan: dict) -> str:
    """The `clog release` alias without --proposal-digest: prepare inside
    the call; the fresh proposal is approved only when its paths EQUAL the
    project's confirmed contract (the content is checked by the D9 rules
    and bound to this candidate). A first release or a contract change is
    refused with the proposal (now pending) — approve it with
    --proposal-digest."""
    details = _prepared(run, target, plan)
    contract = details["contract"]["confirmed"]
    digest = details["proposal_sha256"]
    if contract is not None and not details["contract"]["change"]:
        run.step("proposal", "approved", reason="equals the confirmed version-file contract")
        return digest
    what = "first release" if contract is None else "the version-file contract changes"
    raise run.refuse(
        f"{what} of '{run.slug}': the version sync changes "
        f"{', '.join(details['proposal']['paths']) or 'no files'} (confirmed contract: "
        f"{contract}) — review the proposal and approve it with `--proposal-digest {digest}`; "
        f"nothing released.", **details)


def apply_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    version: str | None = None,
    bump_type: str | None = None,
    entry_ids: Iterable[int] | None = None,
    no_entries: bool = False,
    confirm: bool = False,
    strict: bool = False,
    plan_digest: str | None = None,
    proposal_digest: str | None = None,
    push_remote: str | None = None,
    push_branch: str | None = None,
    no_push: bool = False,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """One release from the APPROVED plan and the APPROVED proposal (`dct
    release apply`), see module doc. Holds the project operation lock
    (K-96 D7) from before the pre-checks until it returns.

    K-80: `plan_digest` is the `snapshot_digest` of the plan the owner
    approved (`dct release plan --version V --entries … --json`; required
    unless `strict`). The candidate is read ONCE (the strict checkout's
    HEAD) and the plan is re-derived FOR THAT CANDIDATE with the same
    version and selection — never on a HEAD read later; a snapshot that
    differs from the approved digest (another HEAD, range base, entry set
    or entry state, selection, version or proof) is refused before any
    database write. HEAD is checked again right before the stamp; the stamp
    itself binds the plan (core release_entries `stampable_ids` /
    `expected_entries`, K-81).

    K-76: `proposal_digest` is the `proposal_sha256` of `dct release
    prepare` for the same version, selection and plan digest — required for
    a NEW release (a re-run of a recorded / historical release needs none).
    It must name the project's PENDING proposal, prepared for this
    candidate, version and plan, with the script and the baseline unchanged
    (_check_approved_proposal); the stamp consumes it in its transaction
    (the record's proposal and consent {version, entry_ids, candidate_sha,
    proposal_sha256, render, push_target: null}, the learned version-file
    contract, the untracked baseline). After the stamp the script never
    runs: the approved blobs are written, content-verified (K-75 / K-77).

    `entry_ids` (owner-chosen ids, each proven by the plan or already
    carrying V) or `no_entries=True` (version-only) — one is required unless
    `strict` (the `clog release` alias: no ids = every unreleased entry, all
    of which must be proven; a range base other than v<current_version> is
    refused instead of being confirmed; without `proposal_digest` it
    prepares inside the call and proceeds only when the proposal equals the
    confirmed contract — _alias_approval). `confirm=True` is required —
    also for the alias (K-96 D1: running it is no longer the confirmation).

    K-97 F2: `push_remote` / `push_branch` / `no_push` choose the push
    target (push_target(): default the branch's upstream; none → refused
    with the choice, never guessed; `no_push` = an explicit local release).
    Before the stamp the remote is read (no tag of V there, its branch
    contained in the candidate); the target is recorded in the consent and
    is what the push — and every resume — uses (K-97 F4).

    Returns result() with outcome 'completed' or 'noop' (a recorded /
    historical / legacy release — no side effect). Raises ReleaseRefused
    (nothing done beyond what the result's steps say), SyncFailed (the
    alias's internal prepare), or gitinfo errors."""
    if confirm is not True:
        raise ReleaseRefused("apply needs --confirm (it stamps, writes, commits, tags and "
                             "pushes)")
    if proposal_digest is not None and not _DIGEST_RE.fullmatch(proposal_digest):
        raise ReleaseRefused(
            f"--proposal-digest must be a lowercase hex SHA-256, got {proposal_digest!r}")
    _check_selection_args("apply", entry_ids, no_entries, plan_digest, strict)
    located = _locate(engine, project_slug, cwd)
    with _operation(located[0], located[1], "apply"):
        return _apply(engine, project_slug, located, version=version, bump_type=bump_type,
                      entry_ids=entry_ids, no_entries=no_entries, strict=strict,
                      plan_digest=plan_digest, proposal_digest=proposal_digest,
                      push=(push_remote, push_branch, no_push), cwd=cwd, on_point=on_point)


def _apply(engine, project_slug, located, *, version, bump_type, entry_ids, no_entries, strict,
           plan_digest, proposal_digest, push, cwd, on_point) -> dict:
    run, plan, target, ids, pre_untracked = _approved_context(
        engine, project_slug, located, version=version, bump_type=bump_type,
        entry_ids=entry_ids, no_entries=no_entries, plan_digest=plan_digest, strict=strict,
        cwd=cwd, on_point=on_point)
    checkout = run.checkout
    snapshot = plan["snapshot"]
    proof = {str(i): snapshot["proof"][str(i)] for i in ids if str(i) in snapshot["proof"]} \
        or None
    stampable = [i for i in ids if str(i) in snapshot["proof"]]
    if not checkout:  # K-63: a project CONFIRMED outside git — the owner's selection
        stampable = list(ids)
    before = ledger.get_release(engine, project_slug, target)
    consent = None
    push_to = None
    if before is None:
        push_remote, push_branch, no_push = push
        push_to = push_target(run, normalize_version(target), push_remote=push_remote,
                              push_branch=push_branch, no_push=no_push)
        if push_to is not None:
            _check_push_precondition(run, push_to, target, checkout["head"])
        if proposal_digest is None and strict and _newer_than_current(run.project, target):
            proposal_digest = _alias_approval(run, target, plan)
        if proposal_digest is not None:
            _check_approved_proposal(run, plan, target, proposal_digest)
        consent = {"version": target, "entry_ids": ids,
                   "candidate_sha": checkout["head"] if checkout else None,
                   "proposal_sha256": proposal_digest,
                   "render": {"pr_refs": bool(run.project.get("changelog_pr_refs"))},
                   "push_target": push_to}
    _point(on_point, "before_stamp")
    if checkout and _head(checkout["repo_root"]) != checkout["head"]:
        raise ReleaseRefused(
            f"HEAD moved since the plan was derived for the candidate {checkout['head']} — "
            f"nothing released; plan again.", details={"candidate": checkout["head"]})
    try:
        stamped = release_entries(
            engine, project_slug, target, entry_ids=ids,
            candidate_sha=checkout["head"] if checkout else None,
            checkout_root=checkout["repo_root"] if checkout else None, proof=proof,
            # K-94 B1: _locate verified the checkout strictly — None means the
            # project is CONFIRMED outside git (a detection error refuses).
            release_mode=run.mode,
            stampable_ids=stampable, expected_entries=snapshot["entries"],
            # K-76 / K-96 D3: the approved proposal, the consent, the learned
            # contract and the untracked baseline — atomically with the stamp.
            consent=consent, render=consent["render"] if consent else None,
            manifest={"untracked_baseline": sorted(pre_untracked)}
            if consent and checkout else None,
            consume_proposal=consent is not None)
    except ValueError as exc:
        raise run.refuse(str(exc)) from None
    details = {"requested_version": target, "previous_version": plan["version"]["current"],
               "bump": plan["version"]["bump"], "entry_ids": ids, "mode": stamped["mode"],
               "released": stamped["released"], "current_version": stamped["current_version"],
               "push_target": push_to}
    run.record = stamped["release"]
    if run.record is None:
        run.warnings.append(
            f"{target} is a legacy release without a record — nothing done "
            f"(`dct release reconstruct --version {target}` records it)")
        return _finish("apply", run, "noop", **details)
    if before is not None:
        run.step("stamp", "verified", status=run.record["status"])
        if run.record["status"] not in ledger.TERMINAL_STATUSES:
            raise run.refuse(f"release {run.version} is unfinished — run `dct release resume`")
        return _finish("apply", run, "noop", **details)
    run.step("stamp", "done", released=stamped["released"])
    _point(on_point, "after_stamp")
    _check_record_location(run)
    _continue(run, fresh=True)
    return _finish("apply", run, "completed", **details)


def _check_record_scope(run: _Run) -> None:
    """K-94 B3 + K-76 — before any effect of a resume: the record's entry
    set equals EXACTLY the live entries carrying its version (never
    repaired by widening the record), and its consent names its proposal."""
    try:
        run.record = ledger.verify_release_entry_set(run.engine, run.slug, run.record["version"])
    except ValueError as exc:
        raise run.refuse(str(exc)) from None
    consent, proposal = run.record.get("consent") or {}, run.record.get("proposal")
    approved = consent.get("proposal_sha256")
    if approved and (proposal is None or ledger.proposal_digest(proposal) != approved):
        raise run.refuse(f"release {run.version}: the recorded consent does not name the "
                         f"recorded proposal — nothing done.")
    # K-97 F4: resume executes the STORED consent — no new approval while
    # the record's scope is the consented one; anything outside it refuses.
    if consent:
        outside = [key for key, ok in (
            ("version", same_version(consent["version"], run.record["version"])),
            ("entry_ids", consent["entry_ids"] == run.record["entry_ids"]),
            ("candidate_sha", consent.get("candidate_sha") == run.record["candidate_sha"]),
        ) if not ok]
        if outside:
            raise run.refuse(
                f"release {run.version}: the record's {', '.join(outside)} is outside the "
                f"stored consent — resume needs a new apply (or abandon it); nothing done.",
                outside_consent=outside)


def resume_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """Finish the unfinished release record (`dct release resume`): verify
    the record's FULL entry set first (K-94 B3) and that its consent names
    its proposal, then the checkout against the record (same checkout
    root; HEAD == candidate before the commit, the release commit or a
    descendant after it — K-51; never re-pinned), then check each effect
    against reality and the manifest — present & matching → skipped,
    missing → performed (the artifacts of a 'stamped' record are the
    APPROVED blobs, content-verified — the script never runs, K-75),
    contradicting → ReleaseRefused without overwriting. No unfinished
    record → outcome 'noop' (completed / abandoned releases have no side
    effects)."""
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    with _operation(project, checkout, "resume"):
        run = _Run(engine, project_slug, project, checkout, project_root, on_point=on_point)
        run.record = ledger.get_release(engine, project_slug)
        if run.record is None:
            run.warnings.append("no unfinished release — nothing to resume")
            return _finish("resume", run, "noop")
        _check_record_scope(run)
        _check_record_location(run)
        if run.repo and _operation_in_progress(run.repo):
            raise run.refuse("a git operation is in progress — finish or abort it first")
        _continue(run, fresh=False)
        return _finish("resume", run, "completed")


def abandon(engine: sa.Engine, project_slug: str, version: str, reason: str, *,
            confirm: bool = False) -> dict:
    """`dct release abandon` — core abandon_release (K-60): the record ends
    'abandoned', every effect stays. Under the project operation lock
    (K-96 D7): never while another release command of the repository runs."""
    project = get_project(engine, project_slug)
    if project is None:
        raise ReleaseRefused(f"Project '{project_slug}' not found")
    try:
        with _operation(project, None, "abandon"):
            out = ledger.abandon_release(engine, project_slug, version, reason, confirm=confirm)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    record = out["release"]
    return result("abandon", project_slug, "abandoned" if out["updated"] else "noop",
                  version=record["version"], release=record, details={"updated": out["updated"]})


def attest(
    engine: sa.Engine,
    project_slug: str,
    entry_ids: Iterable[int],
    *,
    confirm: bool = False,
    cwd: str | None = None,
) -> dict:
    """`dct release attest` (K-16/K-69): the EMPTY attestation commit
    `chore: attest changelog entries` whose final paragraph is the trailer
    `Changelog: <ids ascending>` — on the current branch (or detached
    HEAD), only for ids the plan lists as unproven, only when nothing is
    staged and no operation is in progress, under the project operation
    lock (K-96 D7). Created by `git commit --allow-empty` from a temporary
    index holding HEAD's tree (_hook_commit): the repository's hooks run
    (K-96), the user's index and work tree are untouched, HEAD moves by
    git's own compare-and-swap. Re-validated afterwards: parent = the old
    HEAD, tree unchanged, subject, the trailer proves the ids — a hook that
    changed any of it → refused with the observed effects (the commit
    exists, HEAD moved), never accepted. Returns result() with details
    {previous_head, head, entry_ids}; the candidate changes (no record
    exists yet)."""
    if confirm is not True:
        raise ReleaseRefused("attest needs --confirm (it creates a commit)")
    ids = sorted(set(entry_ids or ()))
    if not ids:
        raise ReleaseRefused("attest needs --entries <ids>")
    project, checkout, _ = _locate(engine, project_slug, cwd)
    if checkout is None:
        raise ReleaseRefused(f"project '{project_slug}' is not in a git checkout — nothing to "
                             f"attest in")
    with _operation(project, checkout, "attest"):
        return _attest(engine, project_slug, checkout, ids, cwd=cwd)


def _attest(engine, project_slug: str, checkout: dict, ids: list[int], *, cwd) -> dict:
    unfinished = ledger.get_release(engine, project_slug)
    if unfinished:
        raise ReleaseRefused(f"release {unfinished['version']} is unfinished — resume or "
                             f"abandon it before attesting (its candidate is pinned)")
    repo = checkout["repo_root"]
    plan = planning.build_plan(engine, project_slug, cwd=cwd, pinned_checkout=checkout)
    unproven = {e["id"] for e in plan["entries"]["unproven"]}
    bad = [i for i in ids if i not in unproven]
    if bad:
        raise ReleaseRefused(
            f"the plan does not list {', '.join(f'#{i}' for i in bad)} as unproven (already "
            f"proven, released or unknown) — nothing attested.", details={"not_unproven": bad})
    busy = _operation_in_progress(repo)
    if busy:
        raise ReleaseRefused(f"a git operation is in progress ({', '.join(busy)})")
    head = checkout["head"]
    tree = _tree(repo, head)
    if _staged_against(repo, head):
        raise ReleaseRefused("the index holds staged changes — the attestation commit is "
                             "empty; unstage them first (nothing is reset for you).",
                             details={"staged": planning.staged_files(repo)})
    message = f"{ATTEST_SUBJECT}\n\nChangelog: {', '.join(str(i) for i in ids)}\n"

    def refuse(msg: str, **details) -> ReleaseRefused:
        return ReleaseRefused(msg, details=details)

    commit = _hook_commit(refuse, repo, tree, head, message)
    facts = _commit_facts(repo, commit)
    commits = planning.read_commits(repo, "-1", commit)
    if facts["parents"] != [head] or facts["tree"] != tree or facts["subject"] != ATTEST_SUBJECT \
            or not set(ids) <= proven_ids(commits[0]):
        raise ReleaseRefused(
            f"the commit `git commit` created ({commit}; the repository's hooks ran) is not the "
            f"empty attestation commit proving {ids}: parents {facts['parents']}, tree "
            f"{facts['tree']} (expected {tree}), subject {facts['subject']!r} — a hook changed "
            f"it. It is NOT accepted: HEAD is {commit}; dct restores nothing — reset HEAD to "
            f"{head} yourself and attest again.",
            details={**facts, "head": commit, "expected_tree": tree, "previous_head": head})
    return result("attest", project_slug, "attested",
                  steps=[{"step": "attest", "result": "done", "commit": commit}],
                  details={"previous_head": head, "head": commit, "entry_ids": ids})


# ── reconstruct (K-60) ───────────────────────────────────────────────────────


def _release_commits(repo: str, version: str) -> list[str]:
    """Commits (any ref) whose subject is `release: v<same version>`."""
    _, out = gitinfo.git_read(repo, "log", "--all", "--format=%H%x1f%s")
    found = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x1f")
        match = _RELEASE_SUBJECT_RE.match(subject.strip())
        if match and _same_label(match.group(1), version):
            found.append(sha.lower())
    return sorted(set(found))


def gather_evidence(engine: sa.Engine, project_slug: str, version: str, *,
                    cwd: str | None = None) -> dict:
    """Read-only evidence of a legacy release of `version` WITHOUT a record
    (K-60). Refuses (ReleaseRefused) on ANY ambiguity — never guesses:
    a record exists; no entry carries V and it is not current_version;
    several release commits or tags pointing elsewhere; a release commit
    without a tag or a tag without the release commit; a release commit
    that is a merge; the changelog section missing / doubled / different
    from the database render; version files that do not contain V or
    differ from the project's contract. Returns {version, entry_ids,
    candidate_sha, release_commit_sha, tag, checkout_root, manifest,
    status, without_git}."""
    version = normalize_version(version)
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    if ledger.get_release(engine, project_slug, version) is not None:
        raise ReleaseRefused(f"version {version} already has a release record — nothing to "
                             f"reconstruct")
    entries = list_entries(engine, project_slug, version=version)
    ids = sorted(e["id"] for e in entries)
    current = project.get("current_version")
    if not ids and not same_version(current, version):
        raise ReleaseRefused(f"no entry carries {version} and it is not the current version "
                             f"({current}) — the database does not prove this release")
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=None)
    contract = ledger.get_version_files(engine, project_slug)["version_files"]
    evidence = {"version": version, "entry_ids": ids, "candidate_sha": None,
                "release_commit_sha": None, "tag": None, "checkout_root": None,
                "without_git": checkout is None, "status": "completed"}
    manifest: dict = {}
    if checkout is None:
        if not project_root:
            raise ReleaseRefused("the project has no path — no artifact can be verified")
        def read(rel: str) -> bytes | None:
            return _read_bytes(run.abspath(rel))
        files = contract or []
    else:
        repo = checkout["repo_root"]
        commits = _release_commits(repo, version)
        tags = _version_tags(repo, version)
        tag_commits = sorted({t["commit"] for t in tags})
        if len(commits) != 1:
            raise ReleaseRefused(
                f"{len(commits)} commits titled `release: v{version}` — "
                f"{'none' if not commits else 'ambiguous'}; refused, never guessed.",
                details={"release_commits": commits})
        commit = commits[0]
        if tag_commits != [commit]:
            raise ReleaseRefused(
                f"the tag(s) of {version} ({', '.join(t['name'] for t in tags) or 'none'}) do "
                f"not point exactly at the release commit {commit} — refused.",
                details={"tags": tags, "release_commit": commit})
        parents = _parents(repo, commit)
        if len(parents) != 1:
            raise ReleaseRefused(f"the release commit {commit} has {len(parents)} parents — "
                                 f"the candidate is ambiguous; refused.")
        evidence.update(candidate_sha=parents[0], release_commit_sha=commit,
                        tag=next((t["name"] for t in tags if t["name"] == f"v{version}"),
                                 tags[0]["name"]),
                        checkout_root=repo)
        manifest["tree"] = _tree(repo, commit)
        def read(rel: str, _commit: str = commit) -> bytes | None:
            return gitinfo.git_read_blob(repo, _commit, rel)
        changed = sorted(_diff_names(repo, parents[0], commit) - {run.changelog_rel})
        if contract is not None and changed != contract:
            raise ReleaseRefused(
                f"the release commit changed {changed} but the version-file contract is "
                f"{contract} — refused.", details={"changed": changed, "contract": contract})
        files = changed
    if run.changelog_rel:
        data = read(run.changelog_rel)
        text = data.decode("utf-8", "strict") if data is not None else None
        blocks = _version_blocks(text, version) if text is not None else []
        if len(blocks) != 1:
            raise ReleaseRefused(f"{run.changelog_rel} holds {len(blocks)} {version} sections — "
                                 f"refused.")
        block = text[blocks[0][1]:blocks[0][2]]
        header = block.split("\n", 1)[0].rstrip("\r")
        date = header.rsplit(" - ", 1)[-1].strip() if " - " in header else None
        stored = {"version": blocks[0][0], "entry_ids": ids,
                  "created_at": date or datetime.now(UTC).date().isoformat()}
        try:
            section, _ = render_section(engine, project_slug, {
                **stored, "version": normalize_version(blocks[0][0])})
        except ReleaseRefused as exc:
            raise ReleaseRefused(f"the section cannot be rendered: {exc}") from None
        body = _norm_block(block).split("\n", 1)[-1]
        if body != _norm_block(section).split("\n", 1)[-1]:
            raise ReleaseRefused(f"the {version} section of {run.changelog_rel} differs from the "
                                 f"database's entries — refused.")
        manifest["changelog"] = {"path": run.changelog_rel, "sha256": _sha256(data),
                                 "section_date": date}
    missing, version_files = [], []
    for rel in files:
        data = read(rel)
        if data is None or version.encode("utf-8") not in data:
            missing.append(rel)
        else:
            version_files.append({"path": rel, "sha256": _sha256(data)})
    if missing:
        raise ReleaseRefused(f"version files without {version}: {', '.join(missing)} — refused.",
                             details={"missing": missing})
    manifest["version_files"] = version_files
    evidence["manifest"] = manifest
    return evidence


def reconstruct(engine: sa.Engine, project_slug: str, version: str, *,
                confirm: bool = False, cwd: str | None = None) -> dict:
    """`dct release reconstruct` (K-60): gather_evidence (read-only) and,
    only with confirm=True, core reconstruct_release with exactly that
    evidence (no re-stamp) — gathered and recorded under the project
    operation lock (K-96 D7; the preview takes no lock). Outcome 'preview'
    (nothing written) or 'reconstructed'."""
    if not confirm:
        evidence = gather_evidence(engine, project_slug, version, cwd=cwd)
        return result("reconstruct", project_slug, "preview", version=evidence["version"],
                      details={"evidence": evidence})
    project, checkout, _ = _locate(engine, project_slug, cwd)
    with _operation(project, checkout, "reconstruct"):
        return _reconstruct(engine, project_slug, version, cwd=cwd)


def _reconstruct(engine, project_slug: str, version: str, *, cwd) -> dict:
    evidence = gather_evidence(engine, project_slug, version, cwd=cwd)
    try:
        record = ledger.reconstruct_release(
            engine, project_slug, evidence["version"], entry_ids=evidence["entry_ids"],
            candidate_sha=evidence["candidate_sha"],
            release_commit_sha=evidence["release_commit_sha"], manifest=evidence["manifest"],
            status=evidence["status"], confirm=True, checkout_root=evidence["checkout_root"],
            without_git=evidence["without_git"])
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    return result("reconstruct", project_slug, "reconstructed", version=record["version"],
                  release=record, details={"evidence": evidence})
