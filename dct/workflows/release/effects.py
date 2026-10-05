"""The release EFFECTS layer (K-72 layer 3) — the default executable release
workflow behind `/release`: `dct release attest / prepare / apply / resume /
abandon / reconstruct` (and `dct clog release`, an alias of apply without
--plan-digest: it needs --confirm like apply, K-96 D1, and the approved
--proposal-digest — it never prepares or approves a proposal itself, K-117).

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
reconstruct --confirm and the old ledger commands (`clog release-advance` =
record_progress, `clog release-abandon` = abandon — K-96 D10). A second one
refuses at once; editors and other git processes are never blocked by it.
ONE lock identity for all of them (K-118 L8, _lock_file): a function of the
project's REGISTERED path only; a project without a path, or with a path
that cannot be verified, is refused — never locked on a substitute key.

PROCESSES (K-108, K-113, K-118 L10): the version-sync script and every git
command — so every hook, filter and push transport git starts — run through
the ONE supervised runner (dct.core.procs, via run_sync_script and
dct.core.gitinfo): each in its own process group, stopped and REAPED as a
whole on a timeout, on an interruption (Ctrl-C, SIGTERM, SIGHUP — turned
into exceptions while the operation lock is held) and on any error, BEFORE
the temporary worktree is removed and the lock released; output is drained
into bounded memory, and git output that is data (refs, history, blobs,
`ls-remote`) is read whole or refused, never cut. A dct that was KILLED
leaves its running process groups in the lock's record — the next mutating
command refuses while one of them lives (_operation, oplock's ownership
model).

The EXECUTOR is explicit (K-85): the project's release_executor ('dct' |
'own') is copied onto each record at its stamp, and the RECORD decides from
then on (a later project-setting change never takes over an unfinished
release). An 'own' project gets no effect from dct — prepare / apply / the
alias refuse before any write (_refuse_own_executor); resume performs only a
record with executor 'dct' AND the owner's consent (_check_record_executor —
an 'own' record, or one stamped outside apply, is refused, never finished
without a push). The proof layer, attest (a proof-layer commit), the ledger,
abandon and reconstruct are shared by both executors. Progress writes carry
the actor: this module advances as 'dct'; record_progress (the ledger path)
as 'ledger' (core.releases.check_progress_actor — always checked, K-119
M5). abandon (`dct release abandon`, under the operation lock) is the ONLY
path that ends a release dct executes (actor 'dct', K-119 M6 — MCP
abandon_release holds no lock and refuses such a record), so no effect ever
runs past an abandon. K-106: apply's consent names executor 'dct'; the stamp
compares it with the project's setting in its transaction, and the fresh
apply re-reads the record and verifies its scope and executor BEFORE the
first effect (K-102), exactly like a resume.

K-115 G4 — proofs, not statuses: 'tagged' is recorded WITH the tag identity
verified in the repository (`tag_proof`: name, object, commit, annotated),
'pushed' WITH the target as read back from the remote (`push_proof`); a
record that reached those statuses before the fields existed gets them
with the next progress write of a resume that re-verified them
(_Run.verified). The ledger refuses the status without the proof.

Every database write runs through _ledger_write (K-88): a lost
serialization race (db.is_serialization_failure — SQLSTATE 40001 or
SerializationConflict, never a message match) becomes ReleaseConflict with
the step whose write was rolled back, the effects THIS run observed (its own
step log) and the record re-read afterwards ("unknown" if that read fails);
_conflict_boundary does the same, with the effects unknown, for a race that
escaped every step-aware write. Nothing is retried automatically.

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
     (dct.paths.release_store_dir). K-117: each proposal has a stable `id`
     and its blobs are PINNED by the ref PIN_REF_PREFIX/<project>/<id>
     (a tree) until the release is completed or abandoned or the pending
     proposal is replaced; the store follows the same lifetime, keeping
     what another live proposal still names (release_proposal_resources).
     A failing script reports its environment contract (sync_environment,
     J2 / J3) — it is never re-run in the user's checkout. Nothing is
     stamped.
  1. pre-checks of apply — strict checkout (K-52; a registered path), no
     unfinished record, clean index and work tree (K-50); the plan
     re-derived FOR THE PINNED CANDIDATE must match the approved snapshot
     digest (K-80); the requested set ⊆ the plan's proven ids; git itself
     recent enough for the hook lifecycle (MIN_GIT_VERSION, K-103); the ONE
     push target (push_target, K-104: the branch's upstream, or the
     explicit --push-remote / --push-branch, or --no-push — no upstream is
     refused, never guessed; its URL is the remote's effective push
     address from the full `get-url --push --all`, --push-url choosing one
     of several — several without a choice are refused) and that URL read
     once (no tag of V there, its branch contained in the candidate — K-97
     F2); the approved proposal (--proposal-digest, REQUIRED for a new
     release — also for the alias, K-117) must be the pending one, bound
     to the same candidate, version and plan digest (K-116 H2), with the
     script and the baseline unchanged, its blobs available and pinned;
     HEAD unchanged right before the stamp;
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
  5. commit (K-103) — the expected tree (candidate tree + exactly the
     manifest blobs) is built in a TEMPORARY index; the real index is
     checked read-only for foreign paths / content (and a foreign
     index.lock). The commit is created by the EXPLICIT hook lifecycle of
     _hook_commit — never `git commit`, whose parent is whatever HEAD is
     when IT reads it: HEAD's commit and the ref HEAD names are PINNED;
     `git hook run` pre-commit → prepare-commit-msg → commit-msg (the
     repository's hooks, core.hooksPath, in the same work tree, with the
     temporary index, GIT_EDITOR and the author identity as `git commit`
     exports them — never skipped, K-96); then the tree the temporary
     index writes, the artifacts in the work tree and the cleaned message
     are verified; `git commit-tree <tree> -p <candidate>` (signed when
     commit.gpgSign says so) creates an UNPUBLISHED object, read back and
     checked (tree, parent, subject); only then the pinned ref moves by a
     real compare-and-swap inside one ref transaction in which HEAD must
     still name it; post-commit runs after that (a failure is a warning).
     A refusing hook, a hook that changed the content or the message, a
     HEAD that moved, a branch switch → refused with NOTHING published: no
     foreign commit is left on the branch, nothing is reset (K-118 L1–L3).
     Then the user's REAL index follows the commit under git's index.lock
     protocol, its own staging preserved (K-79, _sync_real_index) →
     'committed'. Needs git >= MIN_GIT_VERSION, checked before any effect;
  6. tag `v<V>` (lightweight) at the verified release commit → 'tagged'
     with its tag_proof (K-115 G4; an existing tag of V at that commit —
     also an annotated one — is verified and recorded instead);
  7. push (K-97 F2, K-104) — `git push --atomic <url> <commit>:refs/heads/
     <branch> <commit>:refs/tags/<tag>`: the release commit and the tag to
     the CONSENTED target's URL itself with explicit refspecs (a later
     change of the remote's configuration adds no recipient), verified
     with `git ls-remote <url>` — the same address → 'pushed' with its
     push_proof (K-115 G4), then 'completed' (the proposal's pin / store
     blobs are released); a failed push leaves 'tagged' and resume retries
     only the push. Without a push target (--no-push, outside git) the
     record goes 'tagged' → 'completed' (no push proof: the consent's
     push_target null is the explicit local release).

Reconstruction (gather_evidence, K-86 / K-96 D8): the authoritative
version spelling is resolved FIRST by the shared legacy resolver
(changelog.version_spelling); release commits on refs and the tag's commit
are candidates, each verified (one parent, the contract, the section, the
version files' fields — never a substring, J8); a tag → 'completed', a
single verified commit without a tag → 'committed'.

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
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime

import sqlalchemy as sa

from dct import paths as dct_paths
from dct.core import gitinfo, procs
from dct.core import releases as ledger
from dct.core.changelog import (
    list_entries,
    normalize_worktree,
    release_entries,
    version_spelling,
)
from dct.core.ids import new_ulid
from dct.core.projects import get_project
from dct.core.version import (
    compare_versions,
    ensure_version_fits,
    normalize_version,
    read_version_field,
    same_version,
    version_field_reader,
)
from dct.db import is_serialization_failure
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
    "prepared", "advanced", "refused", "failed", "conflict", "found", "none",
)
# K-88: the exit code of a lost serialization race (sysexits EX_TEMPFAIL —
# the failed write may succeed when the command is run again; never retried
# automatically).
CONFLICT_EXIT_CODE = 75
# Steps that are effects on the world (the stamp included) — what a conflict
# report lists as observed; `sync` ran in an isolated copy, `complete` is
# only a progress write.
EFFECT_STEPS = ("stamp", "changelog", "version_files", "commit", "index", "tag", "push",
                "attest")
# The step whose progress write records each status (advance_release).
_STATUS_STEP = {"files_written": "files", "committed": "commit", "tagged": "tag",
                "pushed": "push", "completed": "complete"}

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
    "before_commit",         # real index checked; no hook has run, no object created
    "before_publish",        # pre-commit / prepare-commit-msg / commit-msg ran; the commit
                             # object exists (on the pinned parent) and is verified; NO ref
                             # moved yet
    "publishing",            # the ref transaction is PREPARED (git holds the lock of the
                             # pinned ref — and of HEAD naming it); not committed
    "after_commit",          # the ref moved (compare-and-swap), post-commit ran, verified;
                             # the real index not yet updated; not 'committed'
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

# The version-sync script (prepare, K-96 D9): wall-clock limit and the cap of
# the captured output (stdout + stderr, ONE stream; the first bytes are kept,
# the rest is counted and dropped while it is drained — K-113). A stopped
# process group gets dct.core.procs.TERM_GRACE_SECONDS between SIGTERM and
# SIGKILL, like every supervised process (K-108).
SYNC_TIMEOUT_SECONDS = 300
SYNC_OUTPUT_MAX_BYTES = 65536
SYNC_TIMEOUT_EXIT = 124  # the exit code of a timed-out sync (as timeout(1))
# Creating / staging the temporary worktree of prepare (a checkout of the
# whole candidate) may take longer than the ordinary git reads.
PREPARE_GIT_TIMEOUT_SECONDS = 600
# A project outside git is prepared on a temporary COPY of its directory —
# bounded, a larger directory is refused instead of copied.
COPY_MAX_FILES = 20000
COPY_MAX_BYTES = 256 * 1024 * 1024
# `git hook run` runs the repository's hooks (a pre-commit test suite …) and
# `git push` the remote side's — both may take far longer than a git read.
# Past it git is stopped WITH its whole process group: the hook, the
# transport and everything they started (K-118 L10).
HOOK_GIT_TIMEOUT_SECONDS = 900
# K-103: the commit goes through the hook lifecycle EXPLICITLY — `git hook
# run` (git 2.36); the ref transaction protocol of `git update-ref --stdin`
# (start / prepare / commit) is older (2.27). Checked before any effect.
MIN_GIT_VERSION = (2, 36)

_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)")
_IDENT_RE = re.compile(r"(.*) <([^<>]*)> (\d+) ([+-]\d{4})")
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


class ReleaseConflict(ReleaseRefused):
    """K-88 / K-74: a database write of a release command lost a
    serialization race (db.is_serialization_failure — SQLSTATE 40001 or the
    domain SerializationConflict, never matched by message). THAT
    transaction was rolled back; effects performed before it (files, commit,
    tag, push) STAY and are listed. Never retried automatically — neither
    the write nor any effect. `details`: {error, conflict: {step, status},
    observed_effects (the run's own effect steps, or null = unknown),
    record_state ("read" | "absent" | "unknown")}; `release` = the record as
    re-read after the rollback (null when absent or unknown)."""

    exit_code = CONFLICT_EXIT_CODE


def _conflict(engine: sa.Engine, slug: str, version: str | None, exc: BaseException, *,
              step: str, status: str | None, steps: list[dict] | None) -> ReleaseConflict:
    """The conflict report (K-88), built where the step context is known:
    the step / status whose write was rolled back, the effects this run
    observed (`steps`; None = not known at this boundary) and the record
    RE-READ now — a failing re-read is reported as "unknown", never guessed."""
    try:
        record = ledger.get_release(engine, slug, version) if version else \
            ledger.get_release(engine, slug)
        state, read_error = ("read" if record is not None else "absent"), None
    except Exception as read_exc:  # noqa: BLE001 - the state is then unknown
        record, state, read_error = None, "unknown", str(read_exc)
    observed = None if steps is None else [
        dict(s) for s in steps if s["step"] in EFFECT_STEPS and s["result"] != "skipped"]
    if observed is None:
        effects_text = "the effects of this run are unknown at this point"
    elif observed:
        effects_text = "effects already performed STAY: " + ", ".join(
            f"{s['step']} ({s['result']})" for s in observed)
    else:
        effects_text = "this run performed no effect before it"
    if state == "read":
        record_text = f"the record is now '{record['status']}'"
    elif state == "absent":
        record_text = "no record exists"
    else:
        record_text = f"the record could not be re-read ({read_error}) — its state is UNKNOWN"
    what = f"the {step} write" + (f" ('{status}')" if status else "")
    error = (f"a concurrent database transaction conflicted with {what} of "
             f"{version or 'the release'} (serialization failure) — that write was rolled back "
             f"and is not recorded; {effects_text}; {record_text}. Nothing is retried "
             f"automatically: check the state (`dct release status`) and run the command "
             f"again (or `dct release resume`).")
    details = {"conflict": {"step": step, "status": status}, "observed_effects": observed,
               "record_state": state}
    if read_error is not None:
        details["record_error"] = read_error
    out = ReleaseConflict(error, details=details,
                          steps=[*(steps or []), {"step": step, "result": "conflict"}],
                          release=record)
    out.__cause__ = exc
    return out


def _ledger_write(engine: sa.Engine, slug: str, version: str | None, step: str,
                  write: Callable[[], dict], *, status: str | None = None,
                  steps: list[dict] | None = None) -> dict:
    """Run ONE ledger write; a lost serialization race becomes the
    ReleaseConflict report of THAT step (K-88) — any other error passes."""
    try:
        return write()
    except Exception as exc:
        if isinstance(exc, ReleaseConflict) or not is_serialization_failure(exc):
            raise
        raise _conflict(engine, slug, version, exc, step=step, status=status,
                        steps=steps) from exc


def _conflict_boundary(action: str):
    """The error boundary of a public effects command (K-88): a
    serialization failure that escaped every step-aware write (a read, an
    unexpected path) still becomes a ReleaseConflict — with its effects
    reported as UNKNOWN (null), never as "nothing written"."""
    def wrap(fn):
        import functools

        @functools.wraps(fn)
        def inner(engine, project_slug, *args, **kwargs):
            try:
                return fn(engine, project_slug, *args, **kwargs)
            except Exception as exc:
                if isinstance(exc, ReleaseConflict) or not is_serialization_failure(exc):
                    raise
                version = kwargs.get("version") or (args[0] if args and isinstance(
                    args[0], str) else None)
                try:
                    version = normalize_version(version) if version else None
                except ValueError:
                    version = None
                raise _conflict(engine, project_slug, version, exc, step=action,
                                status=None, steps=None) from exc
        return inner
    return wrap


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


def _check_git_version(repo: str) -> None:
    """K-103 — BEFORE any effect: the explicit hook lifecycle needs `git hook
    run` (MIN_GIT_VERSION). An older (or unrecognisable) git is refused with
    nothing done — never a fallback that would skip the hooks."""
    _, out = gitinfo.git_read(repo, "version")
    match = _GIT_VERSION_RE.match(out.strip())
    found = (int(match.group(1)), int(match.group(2))) if match else None
    if found is None or found < MIN_GIT_VERSION:
        needed = ".".join(str(part) for part in MIN_GIT_VERSION)
        raise ReleaseRefused(
            f"the release workflow needs git {needed} or newer (it runs the repository's commit "
            f"hooks explicitly with `git hook run` and publishes the commit with a ref "
            f"transaction); this is {out.strip()!r} — upgrade git; nothing done.",
            details={"git_version": out.strip(), "required_git_version": needed})


def _head_ref(repo: str) -> str | None:
    """The full name of the ref HEAD NAMES (`refs/heads/<b>`), or None for a
    detached HEAD — the symbolic HEAD, not its commit."""
    code, out = gitinfo.git_read(repo, "symbolic-ref", "-q", "HEAD", ok_codes=(0, 1))
    return (out.strip() or None) if code == 0 else None


def _commit_config(repo: str, key: str, *flags: str) -> str | None:
    """A config value as the COMMIT will see it — read through the writer
    (its config-location variables are the caller's), not the stripped
    read-only environment."""
    code, out = gitinfo.git_write(repo, "config", *flags, "--get", key, ok_codes=(0, 1))
    return (out.strip() or None) if code == 0 else None


def _commit_identity(run_refuse: Callable[..., ReleaseRefused], repo: str) -> dict[str, str]:
    """The author identity of the commit, resolved ONCE the way `git commit`
    does (`git var GIT_AUTHOR_IDENT`: the caller's GIT_AUTHOR_* variables,
    else user.name / user.email) and exported to every hook and to the
    commit itself as GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL / GIT_AUTHOR_DATE —
    what `git commit` exports to its hooks. The committer identity must
    resolve too. An unknown identity is refused before any hook runs."""
    try:
        _, author = gitinfo.git_write(repo, "var", "GIT_AUTHOR_IDENT")
        gitinfo.git_write(repo, "var", "GIT_COMMITTER_IDENT")
    except gitinfo.GitWriteError as exc:
        raise run_refuse(
            f"git cannot determine the identity to commit with ({exc}) — set user.name / "
            f"user.email; nothing committed.", git_error=str(exc)) from None
    match = _IDENT_RE.fullmatch(author.strip())
    if match is None:
        raise run_refuse(f"unexpected author identity from git: {author.strip()!r} — nothing "
                         f"committed.")
    name, email, when, zone = match.groups()
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": f"@{when} {zone}"}


def _cleaned_message(run_refuse: Callable[..., ReleaseRefused], repo: str, path: str) -> str:
    """The message file after the hooks, cleaned up as `git commit -m` does
    (commit.cleanup: default / whitespace / scissors → `git stripspace`,
    strip → `--strip-comments`, verbatim → as it is)."""
    mode = (_commit_config(repo, "commit.cleanup") or "default").lower()
    raw = _read_bytes(path)
    if raw is None:
        raise run_refuse(f"a hook removed the commit message file {path} — nothing committed.")
    if mode == "verbatim":
        text = raw.decode("utf-8", "replace")
    else:
        args = ("stripspace", "--strip-comments") if mode == "strip" else ("stripspace",)
        with open(path, "rb") as fh:
            _, text = gitinfo.git_write(repo, *args, stdin=fh.fileno())
    if "�" in text and "�".encode() not in raw:
        raise run_refuse("a hook left a commit message that is not UTF-8 — nothing committed.")
    return text


def _hook_commit(
    run_refuse: Callable[..., ReleaseRefused], repo: str, tree: str, parent: str, message: str,
    *, check: Callable[[dict, object], tuple[str, dict] | None],
    work_tree_check: Callable[[], None] | None = None,
    on_point: Callable[[str], None] | None = None,
) -> tuple[str, list[str]]:
    """Create the commit of EXACTLY `tree` on the PINNED `parent` and publish
    it on the ref HEAD names — K-103: the repository's hook lifecycle run
    EXPLICITLY, in `git commit`'s order and with what it gives the hooks,
    then a real compare-and-swap. Returns (commit, warnings).

    1. Pinned: HEAD must be `parent`; the ref HEAD NAMES (`refs/heads/<b>`,
       or none — detached) is resolved now and is the ONLY ref published to.
    2. Hooks, each through `git hook run --ignore-missing` (core.hooksPath
       honoured; supervised, HOOK_GIT_TIMEOUT_SECONDS): pre-commit →
       prepare-commit-msg <file> message → commit-msg <file>. They run in
       the SAME work tree (cwd = its top level) with GIT_INDEX_FILE = a
       TEMPORARY index holding `tree`, GIT_EDITOR=":" and the author
       identity (GIT_AUTHOR_NAME / EMAIL / DATE) exactly as `git commit -m`
       exports them; `<file>` is this checkout's COMMIT_EDITMSG. The user's
       index is never used.
    3. Verified BEFORE any commit object exists: the temporary index still
       writes `tree` (a hook that staged / reformatted content → refused),
       `work_tree_check()` (the caller's artifacts unchanged), the message
       cleaned as `git commit` cleans it and not empty.
    4. `git commit-tree <tree> -p <parent>` — an UNPUBLISHED object, with the
       identity of step 2 and the signing policy in force (commit-tree
       ignores commit.gpgSign: `-S` is passed when it is true; user.signingKey
       / gpg.* are git's). Read back and checked: tree, the single parent,
       and `check(facts, commit)` — the caller's message contract (the
       subject; for attest the EXACT id set, K-107), returning None or
       (reason, details). A hook that changed the message → refused; the
       object stays unreferenced.
    5. Publication — gitinfo.git_update_ref_cas: ONE ref transaction that
       moves the pinned ref (HEAD itself, not dereferenced, when detached)
       from `parent` to the commit; inside the prepared transaction (git
       holds the ref's lock, and HEAD's when HEAD names it) HEAD must still
       name the pinned ref. A HEAD that moved, a branch switch, an attach /
       detach → refused, NOTHING published, the other process's commits and
       staging untouched.
    6. post-commit — only after the publication; a failure is a WARNING: the
       commit stays, the hook is never run again (resume adopts the commit).

    Every refusal before step 5 leaves no ref changed (K-118 L1–L3); nothing
    is ever reset. Interruption points: before_publish (after step 4),
    publishing (inside step 5)."""
    ref = _head_ref(repo)
    head = _rev(repo, "HEAD^{commit}")
    if head != parent:
        raise run_refuse(
            f"HEAD is {head}, not {parent} — it moved since the last check; nothing "
            f"committed, nothing changed.", head=head, expected_parent=parent)
    identity = _commit_identity(run_refuse, repo)
    env = {"GIT_EDITOR": ":", **identity}
    _, shown = gitinfo.git_read(repo, "rev-parse", "--git-path", "COMMIT_EDITMSG")
    message_arg = shown.strip()  # as `git commit` passes it (relative to the top level)
    message_path = message_arg if os.path.isabs(message_arg) else os.path.join(repo, message_arg)
    subject = message.split("\n", 1)[0]
    warnings: list[str] = []

    def unchanged(what: str) -> str:
        now = _rev(repo, "HEAD^{commit}")
        return (f"{what}; nothing was published: no ref moved, HEAD is "
                + ("still " if now == parent else "now (moved by another process) ") + f"{now}")

    with tempfile.TemporaryDirectory(prefix="dct-release-commit-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", tree, index_file=index)

        def hook(name: str, *args: str) -> None:
            try:
                gitinfo.git_write(repo, "hook", "run", "--ignore-missing", name,
                                  *(("--", *args) if args else ()), index_file=index, env=env,
                                  timeout=HOOK_GIT_TIMEOUT_SECONDS, diagnostic=True)
            except gitinfo.GitWriteError as exc:
                raise run_refuse(
                    unchanged(f"the repository's {name} hook refused the commit ({exc})")
                    + " — nothing recorded; fix the cause and run the command again.",
                    git_error=str(exc), hook=name, head=_rev(repo, "HEAD^{commit}"),
                    expected_parent=parent) from None

        hook("pre-commit")
        with open(message_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(message if message.endswith("\n") else message + "\n")
        hook("prepare-commit-msg", message_arg, "message")
        hook("commit-msg", message_arg)
        # 3. — nothing exists yet that a refusal would have to take back.
        try:
            _, after = gitinfo.git_write(repo, "write-tree", index_file=index)
        except gitinfo.GitWriteError as exc:
            raise run_refuse(
                unchanged(f"a hook left the index it was given unusable ({exc})") + ".",
                git_error=str(exc), head=_rev(repo, "HEAD^{commit}"),
                expected_tree=tree) from None
        after = after.strip()
        if after != tree:
            changed = sorted(_diff_names(repo, tree, after))
            raise run_refuse(
                unchanged(f"a hook changed the content to be committed ({', '.join(changed)} — "
                          f"a formatter, restaged files): tree {after}, approved {tree}")
                + ". dct commits only the approved content and restores nothing: make the "
                  "hook leave these files alone (or approve content the hook accepts), put "
                  "the files back as approved, then run the command again.",
                head=_rev(repo, "HEAD^{commit}"), expected_tree=tree, tree=after,
                changed=changed)
        if work_tree_check is not None:
            work_tree_check()
        text = _cleaned_message(run_refuse, repo, message_path)
        if not text.strip():
            raise run_refuse(unchanged("a hook emptied the commit message") + ".",
                             head=_rev(repo, "HEAD^{commit}"))
        final = os.path.join(tmp, "message")
        with open(final, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        sign = ["-S"] if _commit_config(repo, "commit.gpgSign", "--type=bool") == "true" else []
        try:
            _, out = gitinfo.git_write(repo, "commit-tree", *sign, tree, "-p", parent, "-F",
                                       final, env=identity, timeout=HOOK_GIT_TIMEOUT_SECONDS)
        except gitinfo.GitWriteError as exc:
            what = ("git could not create the signed commit — signing (commit.gpgSign) failed"
                    if sign else "git could not create the commit object")
            raise run_refuse(unchanged(f"{what} ({exc})") + ".", git_error=str(exc),
                             head=_rev(repo, "HEAD^{commit}"), signed=bool(sign)) from None
        commit = out.strip().lower()
        # 4. — the object is unpublished: read it back with the proof layer's
        # own reader and hold it against the contract.
        facts = _commit_facts(repo, commit)
        problem = None
        if facts["tree"] != tree or facts["parents"] != [parent]:
            problem = (f"git created tree {facts['tree']} on parents {facts['parents']}", {})
        else:
            problem = check(facts, planning.read_commits(repo, "-1", commit)[0])
        if problem is not None:
            reason, extra = problem
            raise run_refuse(
                unchanged(f"a hook changed the commit message outside its contract: {reason}")
                + f". The commit object {commit} was created but is NOT published (it is "
                  f"referenced by nothing; git's garbage collection removes it). Fix the hook "
                  f"and run the command again.",
                **{**facts, "head": _rev(repo, "HEAD^{commit}"), "unpublished_commit": commit,
                   "expected_tree": tree, "expected_parent": parent, **extra})
        _point(on_point, "before_publish")

        def still_named() -> str | None:
            _point(on_point, "publishing")
            now = _head_ref(repo)
            if now != ref:
                return (f"HEAD names {now or 'no branch (detached)'} now, not "
                        f"{ref or 'a detached HEAD'} as at the pre-check")
            return None

        try:
            gitinfo.git_update_ref_cas(
                repo, ref or "HEAD", commit, parent, message=f"commit: {subject}",
                no_deref=ref is None, verify=still_named, timeout=HOOK_GIT_TIMEOUT_SECONDS)
        except gitinfo.GitRefVerifyError as exc:
            raise run_refuse(
                unchanged(f"{exc.reason} — the symbolic HEAD changed between the last check and "
                          f"the publication (a branch switch)")
                + ". Neither branch was changed; switch back and run the command again.",
                pinned_ref=ref, head_ref=_head_ref(repo), head=_rev(repo, "HEAD^{commit}"),
                expected_parent=parent, unpublished_commit=commit) from None
        except gitinfo.GitWriteError as exc:
            raise run_refuse(
                unchanged(f"{ref or 'HEAD'} could not be moved from {parent} to the new commit "
                          f"by compare-and-swap ({exc}) — it moved meanwhile, or another git "
                          f"process holds its lock")
                + ". The other process's commits and staging are untouched.",
                git_error=str(exc), pinned_ref=ref, head_ref=_head_ref(repo),
                head=_rev(repo, "HEAD^{commit}"), expected_parent=parent,
                unpublished_commit=commit) from None
        # 6. — published. post-commit cannot undo or repeat it.
        try:
            gitinfo.git_write(repo, "hook", "run", "--ignore-missing", "post-commit",
                              index_file=index, env=env, timeout=HOOK_GIT_TIMEOUT_SECONDS,
                              diagnostic=True)
        except gitinfo.GitWriteError as exc:
            warnings.append(
                f"the post-commit hook failed AFTER the commit {commit} was published ({exc}) — "
                f"the commit stays and the hook is not run again.")
    return commit, warnings


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


def _ls_remote(repo: str, url: str) -> dict[str, str]:
    """{ref: object} of the repository at `url` (`git ls-remote <url>`,
    network; an annotated tag maps to the commit it peels to). K-104: the
    caller passes the EFFECTIVE PUSH URL of the approved target — never a
    remote name, which git would resolve to the fetch URL."""
    try:
        _, out = gitinfo.git_write(repo, "ls-remote", url, network=True,
                                   timeout=HOOK_GIT_TIMEOUT_SECONDS)
    except gitinfo.GitWriteError as exc:
        raise ReleaseRefused(f"the push destination {url} cannot be read ({exc}) — nothing "
                             f"pushed.", details={"git_error": str(exc), "url": url}) from None
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
    [{name, commit (peeled), object (what the ref resolves to — the tag
    object of an annotated tag), annotated}]."""
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
        tags.append({"name": name, "commit": (peeled or obj).lower(), "object": obj.lower(),
                     "annotated": bool(peeled)})
    return sorted(tags, key=lambda t: t["name"])


def _tag_proof(tags: list[dict], version: str, commit: str) -> dict | None:
    """The identity of the release tag as VERIFIED in the repository now
    (K-115 G4 — core.releases.validate_tag_proof): of the tags of the
    version (`_version_tags`) that target `commit`, `v<version>` when it is
    one of them, else the first by name; None when no tag targets it."""
    here = [t for t in tags if t["commit"] == commit]
    if not here:
        return None
    tag = next((t for t in here if t["name"] == f"v{version}"), here[0])
    return {"name": tag["name"], "object": tag["object"], "commit": tag["commit"],
            "annotated": tag["annotated"]}


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


def _lock_file(project: dict, checkout: dict | None = None) -> str:
    """The ONE canonical lock identity of every local release mutation
    (K-96 D7, K-118 L8 — dct.workflows.release.oplock): a function of the
    project's REGISTERED PATH only — the git common dir of the repository
    holding it (the same for the main checkout, every linked worktree and
    every project in that repository), or, for a path CONFIRMED outside
    git, that directory. `checkout` (a verified checkout of the same
    repository, _locate) is only a shortcut to the same common dir.

    There is NO substitute key: a project without a registered path, or
    with a path that cannot be verified (a missing directory, a broken
    repository, git missing), is REFUSED — a lock derived from the slug or
    the path string would let the command run beside a release that holds
    the repository's lock."""
    slug, path = project["slug"], project.get("path")
    if not path:
        raise ReleaseRefused(
            f"project '{slug}' has no registered path — its checkout cannot be verified (a "
            f"project outside git must be CONFIRMED outside git) and the repository whose "
            f"release operation lock this command must hold cannot be determined; dct never "
            f"locks on a substitute key. Register the path first (`dct project rename {slug} "
            f"--path <dir>`) — a release dct does not execute can also be recorded through "
            f"the MCP ledger tools (advance_release / abandon_release); nothing done.",
            details={"lock": None, "reason": "no_path"})
    if checkout is not None:
        return oplock.lock_path(git_common_dir=checkout["git_common_dir"])
    try:
        common = gitinfo.release_lock_location(path)
    except gitinfo.CheckoutVerificationError as exc:
        raise ReleaseRefused(
            f"the checkout of project '{slug}' ({path}) cannot be verified ({exc}) — the "
            f"repository whose release operation lock this command must hold cannot be "
            f"determined, and dct never locks on a substitute key; run it where the "
            f"registered path exists (or correct the path: `dct project rename {slug} --path "
            f"<dir>`); nothing done.",
            details={"lock": None, "reason": "unverifiable_path", "path": path}) from None
    if common is not None:
        return oplock.lock_path(git_common_dir=common)
    return oplock.lock_path(project_path=path)


@contextlib.contextmanager
def _operation(project: dict, checkout: dict | None, action: str) -> Iterator[None]:
    """Hold the project operation lock for one MUTATING command (K-96 D7) —
    taken before its pre-checks / stamp, released when it returns; another
    holder → ReleaseRefused at once (nothing done, nothing waits). The lock
    is also the scope of the SUPERVISED operation (K-108, oplock's
    ownership model): SIGINT / SIGTERM / SIGHUP end the command as
    exceptions, every process it starts is reaped before the lock is
    released, and process groups a KILLED dct left running refuse the next
    command (`details.process_groups`) instead of running beside it."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(oplock.operation_lock(_lock_file(project, checkout), action))
        except oplock.OperationLocked as exc:
            details = {"lock": exc.path, "holder": exc.holder}
            if isinstance(exc, oplock.LeftoverProcesses):
                details["process_groups"] = exc.groups
            raise ReleaseRefused(str(exc), details=details) from None
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
        # K-115 G4: proofs this run verified for a record that lacks them
        # (reached 'tagged' / 'pushed' before the fields existed) — written
        # with the next progress write.
        self.verified: dict[str, dict] = {}
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
        """Record verified progress (actor 'dct', K-85); a lost
        serialization race → ReleaseConflict with this run's steps (K-88)."""
        version = self.record["version"]
        for field, proof in self.verified.items():
            if self.record.get(field) is None:
                kwargs.setdefault(field, proof)
        out = _ledger_write(
            self.engine, self.slug, version, _STATUS_STEP.get(status, status),
            lambda: ledger.advance_release(self.engine, self.slug, version, status,
                                           actor="dct", **kwargs),
            status=status, steps=self.steps)
        self.record = out["release"]
        self.verified.clear()

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


# ── the lifetime of a proposal's resources (K-117) ───────────────────────────
#
# A proposal names content that nothing else keeps alive: the expected blobs
# of a project in git are unreachable until the release commit exists (`git
# gc` may prune them), and the blobs of a project outside git live in dct's
# store. Both are kept for exactly as long as a proposal needs them:
#
#   - git: ONE ref per proposal, PIN_REF_PREFIX/<project key>/<proposal id>,
#     pointing at a TREE that holds every blob the proposal names
#     (`expected/<path>`, `base/<path>`, `changelog-base/<path>`). The name
#     distinguishes the project (several dct projects may share a
#     repository) and the proposal's STABLE identity (proposal.id — not the
#     version, which a second prepare reuses, and not a content hash, which
#     two proposals may share). A tree, not a commit: `git log --all` and
#     the proof layer never see it. Identical files of two proposals are
#     one blob reachable from two refs — removing one ref never takes it
#     from the other.
#   - outside git: the store is content-addressed and shared by every
#     project of this database; a blob is deleted only when no other LIVE
#     proposal (core.releases.live_proposals: every pending proposal and
#     every unfinished record) names it.
#
# Kept from prepare until the release is completed or abandoned, or the
# pending proposal is replaced by a new prepare. Cleaning is best effort
# (a warning, never a failed release). Limit: a blob another project is
# storing at this very moment (its prepare runs under ITS lock) can be
# removed before that proposal is recorded — its apply then refuses with
# "prepare again", never with wrong content (the store re-hashes on read).

PIN_REF_PREFIX = "refs/dct/release-proposals"
_REF_SAFE_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*", re.ASCII)


def _project_ref_key(slug: str) -> str:
    """The project's component of a pin ref: `s-<slug>` when the slug is
    safe in a ref name, else `h-<hash of the slug>`."""
    if _REF_SAFE_SLUG_RE.fullmatch(slug):
        return f"s-{slug}"
    return "h-" + _sha256(slug.encode("utf-8"))[:16]


def pin_ref(slug: str, proposal: dict) -> str | None:
    """The ref that pins `proposal`'s blobs, None for a proposal without an
    identity (stored before K-117)."""
    identity = proposal.get("id")
    return f"{PIN_REF_PREFIX}/{_project_ref_key(slug)}/{identity}" if identity else None


def _proposal_blobs(proposal: dict) -> dict[str, str]:
    """{pin path: blob id} of everything `proposal` names."""
    blobs = {f"expected/{rel}": blob for rel, blob in proposal["expected_blobs"].items()}
    blobs.update({f"base/{rel}": blob for rel, blob in proposal["base_blobs"].items() if blob})
    changelog = proposal.get("changelog") or {}
    if changelog.get("base_blob"):
        blobs[f"changelog-base/{changelog['path']}"] = changelog["base_blob"]
    return blobs


def _pin_proposal(repo: str, slug: str, proposal: dict) -> str | None:
    """Make `proposal`'s blobs reachable (git): build the tree of
    _proposal_blobs in a temporary index and point pin_ref at it — created
    only if absent or already this tree (idempotent). Returns the ref."""
    ref = pin_ref(slug, proposal)
    if ref is None:
        return None
    blobs = _proposal_blobs(proposal)
    with tempfile.TemporaryDirectory(prefix="dct-release-pin-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", "--empty", index_file=index)
        if blobs:
            entries = [arg for path in sorted(blobs)
                       for arg in ("--cacheinfo", f"100644,{blobs[path]},{path}")]
            gitinfo.git_write(repo, "update-index", "--add", *entries, index_file=index)
        _, tree = gitinfo.git_write(repo, "write-tree", index_file=index)
    tree = tree.strip()
    if _rev(repo, ref) != tree:
        gitinfo.git_write(repo, "update-ref", "-m", "dct release: pin proposal blobs", ref,
                          tree, "")
    return ref


def _store_digests(proposal: dict) -> set[str]:
    """The release blob store entries `proposal` names (outside git only)."""
    if proposal.get("release_mode") != ledger.WITHOUT_GIT:
        return set()
    digests = set(proposal["expected_blobs"].values())
    changelog = proposal.get("changelog") or {}
    if changelog.get("base_blob"):
        digests.add(changelog["base_blob"])
    return digests


def release_proposal_resources(engine: sa.Engine, slug: str, proposal: dict | None,
                               repo: str | None) -> list[str]:
    """Let go of what was kept for `proposal`, which is no longer live (its
    release completed or was abandoned, or a new prepare replaced it):
    its pin ref (git, in `repo`) and those of its store blobs that NO live
    proposal of any project still names. A proposal that is still live
    (ledger.live_proposals — by its id) is left alone. Best effort →
    warnings (never raises): a leftover ref / blob costs disk, not
    correctness."""
    if not proposal:
        return []
    warnings: list[str] = []
    try:
        live = [item["proposal"] for item in ledger.live_proposals(engine)]
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail the command
        return [("the resources of the finished proposal were not cleaned (the live "
                 f"proposals could not be read: {exc})")]
    identity = proposal.get("id")
    if identity and any(other.get("id") == identity for other in live):
        return []
    ref = pin_ref(slug, proposal)
    if ref and repo and os.path.isdir(repo):
        try:
            if _rev(repo, ref) is not None:
                gitinfo.git_write(repo, "update-ref", "-m", "dct release: unpin proposal blobs",
                                  "-d", ref)
        except gitinfo.CheckoutVerificationError as exc:
            warnings.append(f"the pin {ref} was not removed ({exc}) — remove it yourself: "
                            f"`git update-ref -d {ref}`")
    in_use = set().union(*(_store_digests(other) for other in live)) if live else set()
    for digest in sorted(_store_digests(proposal) - in_use):
        try:
            os.unlink(_store_path(digest))
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(f"the stored blob {digest} was not removed ({exc})")
    return warnings


# ── the version-sync script (prepare only, K-96 D9) ──────────────────────────


def _sync_env() -> dict[str, str]:
    """The caller's environment without repository-location variables (a
    GIT_DIR leaked from a hook must not redirect the script's git calls)."""
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_") or key in gitinfo.WRITE_ENV_PASSTHROUGH}


def run_sync_script(script: str, version: str, cwd: str) -> dict:
    """Run `bash <script> <version>` in `cwd` through the supervised runner
    (dct.core.procs.run, K-108): its own session / process group, stdin
    closed, stdout + stderr drained as ONE stream into bounded memory
    (K-113 — never a file). At most SYNC_TIMEOUT_SECONDS of wall clock; on
    a timeout, on an interruption of dct, and after a normal exit for
    anything the script left running, the whole group is stopped and
    reaped BEFORE this returns or raises. Returns {exit_code (None on
    timeout), timed_out, output (the first SYNC_OUTPUT_MAX_BYTES, cut on a
    character boundary, UTF-8 with replacement), output_bytes (total),
    output_dropped_bytes, stopped_processes, duration_seconds}. A group
    that cannot be confirmed dead is refused (it may still write)."""
    timed_out = False
    try:
        done = procs.run(["bash", script, version], cwd=cwd, env=_sync_env(),
                         timeout=SYNC_TIMEOUT_SECONDS, merge_stderr=True,
                         stdout=procs.bounded(SYNC_OUTPUT_MAX_BYTES))
    except procs.TimedOut as exc:
        done, timed_out = exc.result, True
    except procs.StartFailed as exc:
        raise ReleaseRefused(f"the version-sync script cannot be started: {exc}") from None
    except procs.GroupSurvived as exc:
        raise ReleaseRefused(
            f"the version-sync script could not be stopped: {exc} — its isolated copy is "
            f"left in place; nothing prepared.", details={"process_group": exc.pgid}) from None
    return {
        "exit_code": None if timed_out else done.returncode,
        "timed_out": timed_out,
        "output": done.stdout.head.decode("utf-8", "replace"),
        "output_bytes": done.stdout.total,
        "output_dropped_bytes": done.stdout.dropped,
        "stopped_processes": done.stopped,
        "duration_seconds": round(done.duration, 3),
    }


def sync_environment(run: _Run, cwd: str) -> dict:
    """The ENVIRONMENT CONTRACT of the version-sync script (K-117 J2 / J3) —
    what it can rely on, reported with every failure so a missing
    dependency reads as what it is. The script runs ISOLATED and only
    there: there is no fallback to a run in the user's checkout."""
    in_git = run.repo is not None
    return {
        "isolated": True,
        "in_place_fallback": False,
        "cwd": cwd,
        "files": ("only the files of the release candidate (a temporary detached worktree): "
                  "no untracked or ignored file of your checkout — virtualenvs, node_modules, "
                  "build output, local tools, .env files — and no submodule content"
                  if in_git else
                  "a temporary copy of the project directory as it is now"),
        "script": ("the script committed in the candidate (an untracked or modified one "
                   "never runs)" if in_git else "the script in the project directory"),
        "environment": "the caller's environment without GIT_* repository variables; PATH "
                       "as dct sees it",
        "stdin": "closed; no terminal",
        "hooks": "none (the temporary checkout runs no repository hook)" if in_git else "n/a",
        "timeout_seconds": SYNC_TIMEOUT_SECONDS,
        "output_max_bytes": SYNC_OUTPUT_MAX_BYTES,
    }


def _sync_failed(run: _Run, script: str, sync: dict, cwd: str) -> SyncFailed:
    environment = sync_environment(run, cwd)
    details = {"script": script, "exit_code": sync["exit_code"], "sync": sync,
               "environment": environment}
    contract = (f" It ran isolated, with {environment['files']}; dct never runs it in your "
                f"checkout instead. If it needs something that is not in the candidate, make "
                f"the script provide it (install / build it itself, or use a tool on PATH) "
                f"and commit that.")
    if sync["timed_out"]:
        return SyncFailed(
            f"version-sync script timed out after {SYNC_TIMEOUT_SECONDS}s and was stopped "
            f"(its whole process group): {script} — nothing stamped, no proposal stored; fix "
            f"the script and run `dct release prepare` again.{contract}", SYNC_TIMEOUT_EXIT,
            details=details, steps=run.steps)
    return SyncFailed(
        f"version-sync script failed (exit {sync['exit_code']}): {script} — nothing stamped, "
        f"no proposal stored; fix the script and run `dct release prepare` again.{contract}",
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
def _scratch_dir() -> Iterator[str]:
    """A temporary directory for the isolated sync, removed afterwards —
    unless a process group of this operation could not be confirmed dead
    (procs.survivors, K-108): then it stays, with whatever still writes
    into it."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dct-release-prepare-"))
    try:
        yield tmp
    finally:
        if not procs.survivors():
            shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def _isolated_worktree(repo: str, candidate: str) -> Iterator[str]:
    """A temporary linked worktree DETACHED at `candidate`, outside the
    user's checkout (under tempfile); the repository's hooks are disabled
    for this internal checkout (core.hooksPath = an empty directory — no
    post-checkout hook runs). Removed afterwards (`git worktree remove
    --force` + `git worktree prune`), also on failure and on an
    interruption — by then the supervised runner has reaped every process
    that ran in it (K-108). Only when a process group could NOT be
    confirmed dead (procs.survivors) is it left in place: nothing is
    removed under a process that may still write there."""
    with _scratch_dir() as tmp:
        wt, hooks = os.path.join(tmp, "worktree"), os.path.join(tmp, "no-hooks")
        os.mkdir(hooks)
        try:
            gitinfo.git_write(repo, "-c", f"core.hooksPath={hooks}", "worktree", "add",
                              "--detach", "--quiet", wt, candidate,
                              timeout=PREPARE_GIT_TIMEOUT_SECONDS)
            yield wt
        finally:
            if not procs.survivors():
                with contextlib.suppress(gitinfo.CheckoutVerificationError):
                    gitinfo.git_write(repo, "worktree", "remove", "--force", "--force", wt)
                with contextlib.suppress(gitinfo.CheckoutVerificationError):
                    gitinfo.git_write(repo, "worktree", "prune")


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
        sync_cwd = os.path.join(wt, *prefix.rstrip("/").split("/")) if prefix else wt
        sync = run_sync_script(os.path.join(wt, *script[0].split("/")), version, sync_cwd)
        changed = _state_diff(before, _git_state(repo))
        if changed:
            raise run.refuse(
                "the version-sync script changed the checkout outside its isolated worktree "
                f"({', '.join(changed)}) — refused; dct restores nothing: review and undo "
                "those changes yourself, then fix the script; nothing prepared.",
                checkout_changed=changed, sync=sync)
        if sync["exit_code"] != 0:
            raise _sync_failed(run, script[0], sync, sync_cwd)
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
    with _scratch_dir() as tmp:
        copy = os.path.join(tmp, "project")
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
            raise _sync_failed(run, script[0], sync, copy)
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
        # K-117: the STABLE identity of THIS proposal — names its pin.
        "id": new_ulid(),
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


def _record_commit(run: _Run, commit: str, paths: list[str]) -> None:
    """After a verified, published release commit: the real index follows it
    (K-79, _sync_real_index), then 'committed' with release_commit_sha."""
    updated = _sync_real_index(run, commit, paths)
    run.step("index", "done" if updated else "verified", paths=updated)
    _point(run.on_point, "after_index")
    run.advance("committed", release_commit_sha=commit)
    _point(run.on_point, "after_committed")


def _foreign_head(run: _Run, head: str) -> ReleaseRefused:
    """K-118 L1–L3 — HEAD is not the candidate and not the release commit
    (a commit of the owner, or the leftover of an older run whose `git
    commit` let a hook change the content): reported EXACTLY (commit,
    parents, tree against the approved one, subject); no ref, no staging
    and no file is touched; the repair hint is conditional — the decision
    is the owner's, dct never resets."""
    candidate = run.record["candidate_sha"]
    approved = (run.record["manifest"] or {}).get("tree")
    facts = _commit_facts(run.repo, head)
    leftover = (facts["parents"] == [candidate]
                and facts["subject"] == RELEASE_SUBJECT.format(version=run.version))
    return run.refuse(
        f"HEAD is {head}, not the release candidate {candidate} — the release commit is "
        f"created on its candidate only; nothing done. Found at HEAD: commit {head}, parents "
        f"{facts['parents']}, tree {facts['tree']} (approved {approved}), subject "
        f"{facts['subject']!r}"
        + (" — titled like this release and sitting on the candidate, but NOT the approved "
           "content: the leftover of an earlier run whose commit hook changed it, or a commit "
           "made by hand" if leftover else "")
        + f". dct never resets or rewrites a branch: no ref, no staged change and no file was "
          f"touched. IF that commit is yours to discard and nothing is based on it, move the "
          f"branch back to {candidate} yourself (e.g. `git reset --soft {candidate}` keeps "
          f"your index and files), put the artifacts back as approved and run `dct release "
          f"resume`; otherwise abandon the release (`dct release abandon`) and plan a new one "
          f"from the current HEAD.",
        head=head, candidate=candidate, expected_tree=approved, **facts)


def _step_commit(run: _Run) -> None:
    """'files_written' → 'committed' (see module doc, step 5)."""
    record, repo = run.record, run.repo
    candidate, manifest = record["candidate_sha"], record["manifest"] or {}
    subject = RELEASE_SUBJECT.format(version=run.version)
    paths = _manifest_paths(manifest)
    head = _head(repo)
    if head != candidate:
        # Interrupted between the publication and its progress write: adopt
        # HEAD only when it IS the release commit (parent, tree, subject) —
        # no hook runs again, post-commit included.
        if _is_release_commit(run, head):
            _verify_manifest_files(run)
            run.step("commit", "adopted", commit=head)
            _record_commit(run, head, paths)
            return
        raise _foreign_head(run, head)
    _verify_manifest_files(run)
    expected = expected_tree(repo, candidate, paths)
    if expected != manifest.get("tree"):
        raise run.refuse(f"the artifacts no longer build the recorded tree "
                         f"{manifest.get('tree')} (got {expected}) — nothing committed.")
    _check_index_for_commit(run, candidate, expected, paths)
    _point(run.on_point, "before_commit")

    def message_contract(facts: dict, _commit) -> tuple[str, dict] | None:
        if facts["subject"] != subject:
            return f"its subject is {facts['subject']!r}, not {subject!r}", {}
        return None

    def artifacts_unchanged() -> None:
        try:
            _verify_manifest_files(run)
        except ReleaseRefused as exc:
            changed = exc.details.get("changed", [])
            raise run.refuse(
                f"a commit hook changed the work tree ({', '.join(changed)}) — the approved "
                f"content is no longer what the files hold; nothing committed, nothing "
                f"published (HEAD is still {candidate}). dct restores nothing: put those "
                f"files back as approved, then resume.", head=candidate, changed=changed,
                expected_tree=expected) from None

    commit, warnings = _hook_commit(run.refuse, repo, expected, candidate, subject,
                                    check=message_contract,
                                    work_tree_check=artifacts_unchanged, on_point=run.on_point)
    run.warnings.extend(warnings)
    try:
        _verify_manifest_files(run)
    except ReleaseRefused as exc:
        raise run.refuse(
            f"the release commit {commit} was published with the approved tree, but the "
            f"post-commit hook changed the work tree afterwards "
            f"({', '.join(exc.details.get('changed', []))}) — not recorded; restore those files "
            f"to the commit's content yourself, then resume.",
            commit=commit, head=commit, changed=exc.details.get("changed", [])) from None
    run.step("commit", "done", commit=commit)
    _point(run.on_point, "after_commit")
    _record_commit(run, commit, paths)


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
    proof = _tag_proof(tags, run.version, commit)
    if proof is not None:
        run.step("tag", "verified", tag=proof["name"])
    else:
        gitinfo.git_write(run.repo, "tag", name, commit)
        proof = _tag_proof(_version_tags(run.repo, run.version), run.version, commit)
        if proof is None or proof["name"] != name:
            raise run.refuse(f"tag {name} does not point at {commit} after creating it")
        run.step("tag", "done", tag=name)
    _point(run.on_point, "after_tag")
    # K-115 G4: 'tagged' is recorded WITH the identity verified just now.
    run.advance("tagged", tag_proof=proof)
    _point(run.on_point, "after_tagged")


def _check_recorded_tag(run: _Run) -> None:
    """A record at 'tagged' / 'pushed' on resume: a tag of the version must
    still target the release commit; the recorded tag_proof (K-115 G4) must
    still be what the repository shows — and a record that has none (it
    reached the status before the field existed) gets the identity verified
    now with its next progress write. A status is never the proof."""
    commit = run.record["release_commit_sha"]
    tags = _version_tags(run.repo, run.version)
    found = _tag_proof(tags, run.version, commit)
    if found is None:
        raise run.refuse(f"release {run.version} is recorded as tagged but no tag of "
                         f"it points at {commit} — nothing done.")
    recorded = run.record.get("tag_proof")
    if recorded is None:
        run.verified["tag_proof"] = found
    else:
        now = next((t for t in tags if t["name"] == recorded["name"]), None)
        if now is None or (now["object"], now["commit"]) != (recorded["object"],
                                                             recorded["commit"]):
            raise run.refuse(
                f"release {run.version}: the recorded tag {recorded['name']} "
                f"({recorded['object']}) is not what the repository shows now "
                f"({now['object'] if now else 'no such tag'}) — the tag was moved or "
                f"deleted after it was verified; nothing done.",
                tag_proof=recorded, tag_now=now)
    run.step("tag", "verified")


# ── step 7: the push (K-97 F2) ───────────────────────────────────────────────

_PUSH_CHOICE = ("choose explicitly: `--push-remote <remote> [--push-branch <branch>]` (the "
                "remote and branch the release commit and its tag go to) or `--no-push` (a "
                "local release, nothing pushed)")


def _config(repo: str, key: str) -> str | None:
    code, out = gitinfo.git_read(repo, "config", "--get", key, ok_codes=(0, 1))
    return (out.strip() or None) if code == 0 else None


def _push_urls(repo: str, remote: str) -> list[str] | None:
    """EVERY address `git push <remote>` would send to — the full `git
    remote get-url --push --all` (K-104: `--push` alone prints only the
    first), in git's order without repeats; None when there is no such
    remote. Read with the writer's configuration view (the one the push
    itself runs with); url.*.insteadOf / pushInsteadOf already applied by
    git."""
    code, out = gitinfo.git_write(repo, "remote", "get-url", "--push", "--all", remote,
                                  ok_codes=(0, 1, 2, 128))
    if code != 0:
        return None
    return list(dict.fromkeys(line.strip() for line in out.splitlines() if line.strip()))


def _check_literal_url(run: _Run, url: str) -> None:
    """The approved URL goes on git's command line (`git push <url>`, `git
    ls-remote <url>`) — it must reach the repository it NAMES. A
    url.<base>.insteadOf / pushInsteadOf rule whose value is a prefix of it
    would make git send (or read) elsewhere, so such a URL is refused:
    what is pushed to is exactly what was approved, whatever rule is added
    later."""
    code, out = gitinfo.git_write(
        run.repo, "config", "-z", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$",
        ok_codes=(0, 1))
    if code != 0:
        return
    for item in (i for i in out.split("\0") if i):
        key, _, value = item.partition("\n")
        if value and url.startswith(value):
            raise run.refuse(
                f"the push destination {url} cannot be addressed literally: the git "
                f"configuration `{key} = {value}` (insteadOf / pushInsteadOf) would rewrite "
                f"it to another address when dct passes it to `git push` — dct pushes only to "
                f"the exact approved URL; remove that rule (or point the remote at the final "
                f"address); nothing pushed.", url=url, rewrite_rule=key)


def push_target(run: _Run, version: str, *, push_remote: str | None = None,
                push_branch: str | None = None, push_url: str | None = None,
                no_push: bool = False) -> dict | None:
    """The ONE effective push target the owner approves with apply (K-97
    F2, K-104) — None for a project outside git or with `no_push` (an
    explicit local release). Default: the current branch's UPSTREAM
    (`branch.<b>.remote` + `branch.<b>.merge`); no upstream (also a
    detached HEAD, or a local upstream `.`) → refused with the explicit
    choice, never guessed. `push_remote` names the remote; its branch is
    `push_branch`, else the upstream's branch when the upstream is on that
    remote, else refused.

    The URL is the remote's EFFECTIVE PUSH address — from the full `git
    remote get-url --push --all` (_push_urls), never the fetch URL (a fetch
    URL and a different push URL — https to read, ssh to write — is a valid
    layout: the push URL is the target). Exactly one address → that one;
    SEVERAL → `push_url` must choose ONE of them (refused otherwise, with
    the list: `git push <remote>` would send to all of them; dct has no
    mirror mode — the release goes to one destination, verified there).
    `push_url` must be one of the remote's push addresses. The URL must be
    literal (_check_literal_url).
    → {remote, url, branch, tag: "v<V>"} — what prepare shows, the consent
    stores, the pre-stamp read, the push, its verification and every resume
    use. Read-only, no network."""
    if run.repo is None:
        if push_remote or push_branch or push_url:
            raise run.refuse("the project is outside git — there is nothing to push; drop "
                             "--push-remote / --push-branch / --push-url.")
        return None
    if no_push:
        if push_remote or push_branch or push_url:
            raise run.refuse("--no-push contradicts --push-remote / --push-branch / "
                             "--push-url — choose one.")
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
    urls = _push_urls(run.repo, remote)
    if not urls:
        raise run.refuse(f"there is no remote named {remote!r} — {_PUSH_CHOICE}; nothing "
                         f"released.", remote=remote)
    if push_url is not None:
        if push_url not in urls:
            raise run.refuse(
                f"--push-url {push_url} is not a push URL of the remote {remote} (its push "
                f"URLs: {', '.join(urls)}) — dct pushes only to an address the remote is "
                f"configured to push to; nothing released.", remote=remote, push_urls=urls)
        url = push_url
    elif len(urls) > 1:
        raise run.refuse(
            f"the remote {remote} has {len(urls)} push URLs ({', '.join(urls)}) — `git push "
            f"{remote}` would send to all of them, but a release is pushed to ONE approved "
            f"destination and verified there (no mirror mode: an atomic push covers the branch "
            f"and the tag in one repository, never several repositories). Choose it: "
            f"`--push-url <one of them>` (push the others yourself afterwards), or `--no-push`; "
            f"nothing released.", remote=remote, push_urls=urls, push_target=None)
    else:
        url = urls[0]
    _check_literal_url(run, url)
    code, _ = gitinfo.git_read(run.repo, "check-ref-format", f"refs/heads/{target_branch}",
                               ok_codes=(0, 1))
    if code != 0:
        raise run.refuse(f"{target_branch!r} is not a valid branch name — nothing released.")
    return {"remote": remote, "url": url, "branch": target_branch, "tag": f"v{version}"}


def _check_push_precondition(run: _Run, target: dict, version: str, candidate: str) -> None:
    """Before the stamp (network, K-97 F2): the push destination (the
    target's URL, K-104) must not hold a tag of V already (any spelling —
    the release commit does not exist yet, so any such tag is elsewhere),
    and its branch must be absent or contained in the candidate (else the
    push could never be a fast-forward)."""
    refs = _ls_remote(run.repo, target["url"])
    tags = _remote_version_tags(refs, version)
    if tags:
        raise run.refuse(
            f"the remote {target['remote']} ({target['url']}) already has a tag of {version} "
            f"({', '.join(t['name'] for t in tags)}) — refused before the stamp; nothing "
            f"released.", remote_tags=tags, push_target=target)
    tip = refs.get(f"refs/heads/{target['branch']}")
    if tip is not None and not _contains(run.repo, candidate, tip):
        raise run.refuse(
            f"{target['remote']}/{target['branch']} ({target['url']}) is at {tip}, which the "
            f"candidate {candidate} does not contain — the push would be rejected; fetch and "
            f"integrate it first (then plan again); nothing released.", remote_branch=tip,
            push_target=target)


def _consented_target(run: _Run) -> dict | None:
    """The push target of the record's consent (K-97 F4, K-104) — what every
    resume uses, never re-derived from today's upstream. Its URL must STILL
    be one of the push addresses of its remote (a remote re-pointed
    elsewhere is outside the consent: refused) and still literal
    (_check_literal_url). Addresses ADDED to the remote since are not
    recipients: the push names the approved URL itself."""
    target = (run.record.get("consent") or {}).get("push_target")
    if target is None:
        return None
    urls = _push_urls(run.repo, target["remote"])
    if not urls or target["url"] not in urls:
        raise run.refuse(
            f"the approved push target {target['remote']} ({target['url']}) is not a push "
            f"address of this repository's remote any more (now "
            f"{', '.join(urls) if urls else 'missing'}) — outside the consent of "
            f"{run.version}: restore the remote, or abandon the release; nothing pushed.",
            push_target=target, push_urls=urls or [], url_now=urls[0] if urls else None)
    _check_literal_url(run, target["url"])
    return target


def _push_proof(refs: dict[str, str], target: dict) -> dict:
    """What the push target showed when it was read (K-115 G4 —
    core.releases.validate_push_proof): the approved target, the commit its
    tag resolved to and its branch tip."""
    return {"target": target, "tag_commit": refs.get(f"refs/tags/{target['tag']}"),
            "branch_tip": refs.get(f"refs/heads/{target['branch']}")}


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
    """'tagged' → 'pushed' (K-97 F2, K-104): `git push --atomic <url>
    <commit>:refs/heads/<branch> <commit>:refs/tags/<tag>` — the APPROVED
    URL itself and explicit refspecs, so nothing the remote's configuration
    gained since (another pushurl, a push refspec, a mirror flag) adds a
    recipient or a ref; the remote side's hooks and the local pre-push hook
    run (it receives the URL as the remote's name). Verified with `git
    ls-remote <url>` — the same address. Already there → verified, not
    pushed again. A failure leaves 'tagged' — resume retries only this
    step. (Pushing by URL does not move `refs/remotes/<remote>/<branch>`;
    the next fetch does.)"""
    target = _consented_target(run)
    if target is None:
        run.step("push", "skipped", reason="no push target approved")
        return
    commit = run.record["release_commit_sha"]
    where = {"remote": target["remote"], "url": target["url"], "branch": target["branch"],
             "tag": target["tag"]}
    refs = _ls_remote(run.repo, target["url"])
    if _pushed(run, refs, target, commit):
        run.step("push", "verified", **where)
    else:
        _point(run.on_point, "before_push")
        try:
            gitinfo.git_write(run.repo, "push", "--atomic", target["url"],
                              f"{commit}:refs/heads/{target['branch']}",
                              f"{commit}:refs/tags/{target['tag']}",
                              network=True, timeout=HOOK_GIT_TIMEOUT_SECONDS,
                              diagnostic=True)
        except gitinfo.GitWriteError as exc:
            raise run.refuse(
                f"the push of {run.version} to {target['remote']} ({target['url']}; "
                f"{target['branch']}, {target['tag']}) failed ({exc}) — the release stays "
                f"'tagged' (commit and tag exist locally); fix the cause and run `dct release "
                f"resume`: it retries ONLY the push.", git_error=str(exc),
                push_target=target) from None
        refs = _ls_remote(run.repo, target["url"])
        if not _pushed(run, refs, target, commit):
            raise run.refuse(
                f"after the push, {target['remote']} ({target['url']}) does not show "
                f"{target['branch']} and {target['tag']} at {commit} — not recorded as pushed.",
                push_target=target)
        run.step("push", "done", **where)
        _point(run.on_point, "after_push")
    # K-115 G4: 'pushed' is recorded WITH the target as verified just now.
    run.advance("pushed", push_proof=_push_proof(refs, target))
    _point(run.on_point, "after_pushed")


def _verify_push(run: _Run) -> None:
    """A 'pushed' record on resume: the approved destination must still show it."""
    target = _consented_target(run)
    if target is None:
        run.step("push", "verified", reason="no push target approved")
        return
    refs = _ls_remote(run.repo, target["url"])
    if not _pushed(run, refs, target, run.record["release_commit_sha"]):
        raise run.refuse(
            f"release {run.version} is recorded as pushed, but {target['remote']} "
            f"({target['url']}) does not show {target['branch']} and {target['tag']} at its "
            f"release commit — nothing done.", push_target=target)
    if run.record.get("push_proof") is None:  # an older record: proven now (K-115 G4)
        run.verified["push_proof"] = _push_proof(refs, target)
    run.step("push", "verified", remote=target["remote"], url=target["url"],
             branch=target["branch"], tag=target["tag"])


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
            _check_recorded_tag(run)
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
    # K-117: the release ended — its proposal's pin and store blobs go
    # (only what no other live proposal uses).
    run.warnings.extend(release_proposal_resources(
        run.engine, run.slug, run.record.get("proposal"), run.repo))


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


OWN_EXECUTOR_HINT = (
    "its effects belong to the project's own release tool: it uses the shared proof layer "
    "(`dct release plan --json`) and ledger (MCP release_changelog / get_release / "
    "advance_release / abandon_release) — see docs/release-workflow.md, \"Integrating a "
    "project's own release tool\"")


def _refuse_own_executor(project: dict) -> None:
    """K-85: a project whose release executor is 'own' gets NO effect from
    dct (prepare, apply, the `clog release` alias) — refused before any
    write, pointing to its own integration. The proof layer, attest and the
    ledger stay shared."""
    if (project.get("release_executor") or ledger.DEFAULT_EXECUTOR) == "own":
        raise ReleaseRefused(
            f"project '{project['slug']}' has release executor 'own' — dct performs none of "
            f"its release effects (no prepare, stamp, file, commit, tag or push); "
            f"{OWN_EXECUTOR_HINT}; nothing done. (`dct project release-executor "
            f"{project['slug']} dct` hands NEW releases to dct.)",
            details={"executor": "own"})


def _check_record_executor(run: _Run) -> None:
    """K-85 — resume executes only a release dct itself stamped: the
    RECORD's executor (never today's project setting) must be 'dct' and the
    record must carry the owner's consent (stamped by `dct release apply`;
    core.releases.progress_owner). Anything else — an 'own' record, one
    stamped outside the effects layer (MCP release_changelog, older
    records, a partial reconstruction: no approved proposal, no push
    target) — is refused before any effect, never finished without a push."""
    record = run.record
    executor = record.get("executor") or ledger.DEFAULT_EXECUTOR
    if executor == "own":
        raise run.refuse(
            f"release {record['version']} is executed by the project's own release tool "
            f"(executor 'own', recorded at its stamp — a later project-setting change does "
            f"not take it over) — dct performs and adopts none of its effects; finish it "
            f"with that tool, or abandon it; {OWN_EXECUTOR_HINT}; nothing done.",
            executor="own")
    if record.get("consent") is None:
        raise run.refuse(
            f"release {record['version']} was stamped outside `dct release apply` (no "
            f"recorded consent: no approved version-file proposal, no approved push target) — "
            f"dct does not perform or adopt its effects; finish them yourself and record the "
            f"verified progress (`dct clog release-advance`, MCP advance_release), or abandon "
            f"it; nothing done.", executor=executor, consent=None)


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
    _refuse_own_executor(project)
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
        _check_git_version(checkout["repo_root"])
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
    replaced = ledger.get_pending_proposal(run.engine, run.slug)
    # K-117: reachable BEFORE the proposal can be approved — its blobs are
    # pinned by the time anything refers to them.
    pin = _pin_proposal(run.repo, run.slug, proposal) if run.repo else None
    stored = _ledger_write(run.engine, run.slug, normalize_version(target), "proposal",
                           lambda: ledger.set_pending_proposal(run.engine, run.slug, proposal),
                           steps=run.steps)
    if replaced is not None:  # the earlier pending proposal can never be approved now
        run.warnings.extend(release_proposal_resources(
            run.engine, run.slug, replaced["proposal"], run.repo))
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    run.step("proposal", "done", proposal_sha256=stored["proposal_sha256"], pin=pin)
    return {
        "proposal": stored["proposal"],
        "proposal_sha256": stored["proposal_sha256"],
        "plan_digest": plan["snapshot_digest"],
        "contract": {"confirmed": contract, "proposed": stored["proposal"]["paths"],
                     "change": contract != stored["proposal"]["paths"]},
        "sync": report["sync"],
        "checks": report["checks"],
    }


@_conflict_boundary("prepare")
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
    or ReleaseRefused (a pre-check — also a project whose release executor
    is 'own', K-85 — a script that wrote outside its
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
    if run.repo:
        # K-117: the blobs stay reachable until the release ends — the pin of
        # prepare is verified (re-created when someone removed the ref).
        if not proposal.get("id"):
            raise run.refuse(
                f"the pending proposal has no identity (prepared by an older dct): its blobs "
                f"cannot be pinned for the release — {hint}; nothing released.")
        _pin_proposal(run.repo, run.slug, proposal)
    return proposal


@_conflict_boundary("apply")
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
    push_url: str | None = None,
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

    K-76 / K-117: `proposal_digest` is the `proposal_sha256` of `dct release
    prepare` for the same version, selection and plan digest — REQUIRED for
    a NEW release, with or without `strict` (a re-run of a recorded /
    historical release needs none); a missing one is refused before the
    stamp with the prepare procedure — nothing is prepared or approved
    inside this call. It must name the project's PENDING proposal, prepared for this
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
    refused instead of being confirmed; it takes no plan digest, so the
    approved proposal must carry the digest of the plan derived here —
    K-116 H2, _check_approved_proposal). `confirm=True` is required —
    also for the alias (K-96 D1: running it is no longer the confirmation).

    K-97 F2 / K-104: `push_remote` / `push_branch` / `push_url` / `no_push`
    choose the ONE push target (push_target(): default the branch's
    upstream; none → refused with the choice, never guessed; the URL is the
    remote's effective push address, `push_url` choosing one when it has
    several — refused without the choice; `no_push` = an explicit local
    release). Before the stamp that address is read (no tag of V there,
    its branch contained in the candidate); the target is recorded in the
    consent and is what the push, its verification — and every resume —
    address literally (K-97 F4).

    K-85: a project whose release_executor is 'own' is refused before any
    write (details.executor 'own').

    Returns result() with outcome 'completed' or 'noop' (a recorded /
    historical / legacy release — no side effect). Raises ReleaseRefused
    (nothing done beyond what the result's steps say), ReleaseConflict (a
    lost serialization race, K-88 — the effects observed so far listed),
    or gitinfo errors."""
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
                      push=(push_remote, push_branch, push_url, no_push), cwd=cwd,
                      on_point=on_point)


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
        if proposal_digest is None and _newer_than_current(run.project, target):
            # K-117 / K-116 H2: a NEW release needs the owner's approval of
            # ONE specific proposal — also through the alias. Equal paths and
            # a checked version field are no consent to the content.
            raise run.refuse(
                f"a new release needs the APPROVED version-file proposal: run `dct release "
                f"prepare --version {target} (--entries … | --no-entries) --plan-digest "
                f"<snapshot_digest of `dct release plan … --json`>`, review the files it "
                f"proposes and pass its proposal_sha256 as --proposal-digest"
                + (" (the `clog release` alias takes no --plan-digest: the approved proposal "
                   "must have been prepared for exactly the plan the alias derives — every "
                   "unreleased entry, or your --entries / --no-entries)" if strict else "")
                + "; nothing prepared, nothing released.",
                proposal_digest=None, version=target, plan_digest=plan["snapshot_digest"])
        push_remote, push_branch, push_url, no_push = push
        push_to = push_target(run, normalize_version(target), push_remote=push_remote,
                              push_branch=push_branch, push_url=push_url, no_push=no_push)
        if push_to is not None:
            _check_push_precondition(run, push_to, target, checkout["head"])
        if proposal_digest is not None:
            # … bound to THIS candidate, version and plan (H2: the alias has no
            # plan flag — proposal.plan_digest must be the digest derived here).
            _check_approved_proposal(run, plan, target, proposal_digest)
        consent = {"version": target, "entry_ids": ids,
                   "candidate_sha": checkout["head"] if checkout else None,
                   "proposal_sha256": proposal_digest,
                   "render": {"pr_refs": bool(run.project.get("changelog_pr_refs"))},
                   "push_target": push_to,
                   # K-106: the owner approved a release executed by dct —
                   # the stamp compares it with the project's setting.
                   "executor": ledger.DEFAULT_EXECUTOR}
    _point(on_point, "before_stamp")
    if checkout and _head(checkout["repo_root"]) != checkout["head"]:
        raise ReleaseRefused(
            f"HEAD moved since the plan was derived for the candidate {checkout['head']} — "
            f"nothing released; plan again.", details={"candidate": checkout["head"]})
    def stamp() -> dict:
        return release_entries(
            engine, project_slug, target, entry_ids=ids,
            candidate_sha=checkout["head"] if checkout else None,
            checkout_root=checkout["repo_root"] if checkout else None, proof=proof,
            # K-116 H7: the approved snapshot's PR numbers were recognised in
            # the repository by the plan — never a client declaration.
            pr_source="git",
            # K-94 B1: _locate verified the checkout strictly — None means the
            # project is CONFIRMED outside git (a detection error refuses).
            release_mode=run.mode,
            stampable_ids=stampable, expected_entries=snapshot["entries"],
            # K-76 / K-96 D3: the approved proposal, the consent, the learned
            # contract and the untracked baseline — atomically with the stamp.
            consent=consent, render=consent["render"] if consent else None,
            manifest={"untracked_baseline": sorted(pre_untracked)}
            if consent and checkout else None,
            # K-106: a NEW record is bound to executor 'dct' by the consent
            # (compared in the stamp transaction); a re-run of an existing
            # record passes no consent and stays a no-op for either executor.
            consume_proposal=consent is not None)

    try:
        stamped = _ledger_write(engine, project_slug, normalize_version(target), "stamp", stamp,
                                steps=run.steps)
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
    # K-102 / K-106: the record is RE-READ and verified like a resumed one
    # BEFORE the first effect — its full entry set / consent scope (an
    # entry that joined V since the stamp) and its executor (the second
    # barrier behind the stamp transaction's expected_executor).
    _check_record_scope(run)
    _check_record_executor(run)
    _check_record_consistency(run)
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


def _check_record_consistency(run: _Run) -> None:
    """K-115 G5 / G9 — before any effect: the whole record (consent,
    proposal, render, tag / push proof) must describe ONE release
    (core.releases.check_record_consistency); a contradiction is refused,
    never repaired."""
    try:
        ledger.check_record_consistency(run.record)
    except ValueError as exc:
        raise run.refuse(f"{str(exc).removesuffix('nothing written.')}nothing done.") from None


@_conflict_boundary("resume")
def resume_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """Finish the unfinished release record (`dct release resume`): only a
    record dct executes (K-85: executor 'dct' AND a recorded consent —
    _check_record_executor; anything else is refused before any effect);
    verify the record's FULL entry set first (K-94 B3) and that its consent names
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
        _check_record_executor(run)
        _check_record_scope(run)
        _check_record_consistency(run)
        _check_record_location(run)
        if run.repo:
            _check_git_version(run.repo)
            if _operation_in_progress(run.repo):
                raise run.refuse("a git operation is in progress — finish or abort it first")
        _continue(run, fresh=False)
        return _finish("resume", run, "completed")


@_conflict_boundary("abandon")
def abandon(engine: sa.Engine, project_slug: str, version: str, reason: str, *,
            confirm: bool = False) -> dict:
    """`dct release abandon` — core abandon_release (K-60): the record ends
    'abandoned', every effect stays. Under the project operation lock
    (K-96 D7): never while another release command of the repository runs —
    which is why it abandons as the actor 'dct' (K-119 M6): the ONLY path
    that ends a release dct's effects layer executes; MCP abandon_release
    (no lock) refuses such a record and names this command. After it no
    effect continues: resume finds no unfinished record."""
    project = get_project(engine, project_slug)
    if project is None:
        raise ReleaseRefused(f"Project '{project_slug}' not found")
    try:
        with _operation(project, None, "abandon"):
            out = _ledger_write(
                engine, project_slug, _normalized_or_none(version), "abandon",
                # K-119 M6: this path holds the operation lock — the only one
                # that may end a release dct's effects layer executes.
                lambda: ledger.abandon_release(engine, project_slug, version, reason,
                                               confirm=confirm, actor="dct"), steps=[])
            record = out["release"]
            # K-117: an abandoned release needs its proposal's pin / blobs no
            # longer (still under the lock; a repeat cleans what is left).
            repo = record.get("checkout_root") if record.get("candidate_sha") else None
            warnings = release_proposal_resources(engine, project_slug,
                                                  record.get("proposal"), repo)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    return result("abandon", project_slug, "abandoned" if out["updated"] else "noop",
                  version=record["version"], release=record, warnings=warnings,
                  details={"updated": out["updated"]})


def _normalized_or_none(version: str | None) -> str | None:
    try:
        return normalize_version(version) if version else None
    except ValueError:
        return None


@_conflict_boundary("advance")
def record_progress(engine: sa.Engine, project_slug: str, version: str, status: str, *,
                    release_commit_sha: str | None = None,
                    tag_proof: dict | None = None,
                    push_proof: dict | None = None) -> dict:
    """`dct clog release-advance` (K-96 D10) — the LEDGER path of a release
    dct does not execute (an 'own' record, or one stamped outside `dct
    release apply`): core advance_release with actor 'ledger' (its state
    machine, write-once and B1 rules; a record dct executes is refused —
    `dct release resume` records its progress), under the project operation
    lock like every other mutating release path. The caller reports what
    IT verified (a status is never proof for dct): `tag_proof` /
    `push_proof` (K-115 G4, core validate_tag_proof / validate_push_proof)
    are the identities it verified — required by the ledger for 'tagged' /
    'pushed' / 'completed' of a record with git. Returns result() with
    outcome 'advanced' or 'noop' and details {updated, previous_status}."""
    project = get_project(engine, project_slug)
    if project is None:
        raise ReleaseRefused(f"Project '{project_slug}' not found")
    try:
        with _operation(project, None, "advance"):
            before = ledger.get_release(engine, project_slug, version)
            out = _ledger_write(
                engine, project_slug, _normalized_or_none(version), "advance",
                lambda: ledger.advance_release(engine, project_slug, version, status,
                                               release_commit_sha=release_commit_sha,
                                               tag_proof=tag_proof, push_proof=push_proof,
                                               actor="ledger"),
                status=status, steps=[])
    except (ValueError, TypeError) as exc:
        raise ReleaseRefused(str(exc)) from None
    record = out["release"]
    return result("advance", project_slug, "advanced" if out["updated"] else "noop",
                  version=record["version"], release=record,
                  details={"updated": out["updated"],
                           "previous_status": before["status"] if before else None})


@_conflict_boundary("attest")
def attest(
    engine: sa.Engine,
    project_slug: str,
    entry_ids: Iterable[int],
    *,
    confirm: bool = False,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """`dct release attest` (K-16/K-69): the EMPTY attestation commit
    `chore: attest changelog entries` whose final paragraph is the trailer
    `Changelog: <ids ascending>` — on the current branch (or detached
    HEAD), only for ids the plan lists as unproven, only when nothing is
    staged and no operation is in progress, under the project operation
    lock (K-96 D7). Created by the same mechanism as the release commit
    (_hook_commit, K-103): the repository's hooks run explicitly on a
    temporary index holding HEAD's tree, the user's index and work tree are
    untouched, the commit object is created on the PINNED HEAD and checked
    BEFORE it is published — tree unchanged, subject, and (K-107) the ids
    the proof parser reads from it (trailer + body scan) EXACTLY the
    approved ones: a hook that adds, drops or duplicates an id, or stages
    content, is refused with nothing published (a hook's addition that
    leaves the set alone — a Signed-off-by — is fine). The ref HEAD names
    then moves by compare-and-swap; a HEAD that moved meanwhile loses
    nothing. Returns result() with details {previous_head, head,
    entry_ids} (warnings: a failed post-commit hook); the candidate changes
    (no record exists yet). `on_point` — the interruption points
    before_publish / publishing, for tests."""
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
        return _attest(engine, project_slug, checkout, ids, cwd=cwd, on_point=on_point)


def _attest(engine, project_slug: str, checkout: dict, ids: list[int], *, cwd,
            on_point=None) -> dict:
    _check_git_version(checkout["repo_root"])
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
        return ReleaseRefused(msg, details={**details, "previous_head": head})

    def message_contract(facts: dict, commit) -> tuple[str, dict] | None:
        # K-107: EXACT equality with the whole set the proof parser reads
        # from this commit (trailer + body scan) — a hook that adds, drops or
        # duplicates an id changes what the next plan would take as proven.
        proven = sorted(proven_ids(commit))
        if facts["subject"] != ATTEST_SUBJECT:
            return f"its subject is {facts['subject']!r}, not {ATTEST_SUBJECT!r}", {}
        if proven != ids:
            return (f"it would prove {proven}, not exactly the approved {ids}",
                    {"approved_ids": ids, "proven_ids": proven})
        return None

    commit, warnings = _hook_commit(refuse, repo, tree, head, message, check=message_contract,
                                    on_point=on_point)
    return result("attest", project_slug, "attested", warnings=warnings,
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


def _equivalent_pattern(version: str) -> re.Pattern:
    """V as a whole version token in ANY spelling equivalent to it under the
    version contract (1.1 ↔ 1.1.0, optional leading v) — for reading
    legacy artifacts written before one spelling was authoritative."""
    spellings = {version}
    if re.fullmatch(r"\d+\.\d+", version):
        spellings.add(version + ".0")
    match = re.fullmatch(r"(\d+\.\d+)\.0", version)
    if match:
        spellings.add(match.group(1))
    alternatives = "|".join(re.escape(v) for v in sorted(spellings, key=len, reverse=True))
    return re.compile(r"(?<![0-9A-Za-z.])v?(?:" + alternatives + r")(?![0-9A-Za-z]|\.[0-9])")


def _legacy_version_file(rel: str, base: bytes | None, data: bytes | None, version: str,
                         *, git: bool) -> dict:
    """K-96 D9 / J8 for a reconstruction — the version FIELD (a file dct has
    a reader for) must be V (same_version); any other file: in git the lines
    the release commit changed must each hold V (any equivalent spelling),
    outside git at least one line must. Never a substring search. →
    {path, check, …}; ValueError (the reason) otherwise."""
    if data is None:
        raise ValueError(f"{rel} is missing")
    if version_field_reader(rel) is not None:
        return check_version_file(rel, base, data, version)
    try:
        lines = data.decode("utf-8").splitlines()
        old = (base or b"").decode("utf-8").splitlines() if git else []
    except UnicodeDecodeError:
        raise ValueError(f"{rel} is not UTF-8 text and dct has no version reader for it") \
            from None
    pattern = _equivalent_pattern(version)
    if not git:
        if not any(pattern.search(line) for line in lines):
            raise ValueError(f"{rel} holds no line with {version}")
        return {"path": rel, "check": "lines_with_version", "changed_lines": None}
    changed, bad = 0, []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=lines,
                                                       autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if j1 == j2:
            bad.append(f"line(s) {i1 + 1}-{i2} removed")
        for line in lines[j1:j2]:
            changed += 1
            if not pattern.search(line):
                bad.append(line)
    if bad or not changed:
        raise ValueError(f"{rel}: every line the release commit changed must hold {version} — "
                         + ("; ".join(repr(b) for b in bad[:5]) if bad else "nothing changed"))
    return {"path": rel, "check": "lines_with_version", "changed_lines": changed}


def _legacy_section(run: _Run, version: str, ids: list[int], data: bytes | None) -> dict:
    """F holds the V section (any equivalent label) EXACTLY once, its body
    equal to the database render of the entries → the manifest's changelog
    {path, sha256, section_date}; ReleaseRefused otherwise."""
    try:
        text = data.decode("utf-8", "strict") if data is not None else None
    except UnicodeDecodeError:
        raise ReleaseRefused(f"{run.changelog_rel} is not UTF-8 text") from None
    blocks = _version_blocks(text, version) if text is not None else []
    if len(blocks) != 1:
        raise ReleaseRefused(f"{run.changelog_rel} holds {len(blocks)} {version} sections")
    block = text[blocks[0][1]:blocks[0][2]]
    header = block.split("\n", 1)[0].rstrip("\r")
    date = header.rsplit(" - ", 1)[-1].strip() if " - " in header else None
    stored = {"version": normalize_version(blocks[0][0]), "entry_ids": ids,
              "created_at": date or datetime.now(UTC).date().isoformat()}
    try:
        section, _ = render_section(run.engine, run.slug, stored)
    except ReleaseRefused as exc:
        raise ReleaseRefused(f"the section cannot be rendered: {exc}") from None
    if _norm_block(block).split("\n", 1)[-1] != _norm_block(section).split("\n", 1)[-1]:
        raise ReleaseRefused(f"the {version} section of {run.changelog_rel} differs from the "
                             f"database's entries")
    return {"path": run.changelog_rel, "sha256": _sha256(data), "section_date": date}


def _verify_release_candidate(run: _Run, repo: str, commit: str, version: str,
                              ids: list[int], contract: list[str] | None) -> dict:
    """D8 — ONE candidate release commit, verified on its own evidence:
    subject `release: v<V>` (any equivalent spelling), exactly one parent
    (the candidate), the paths it changed besides F = the version-file
    contract (when one exists), F at the commit holding the V section once
    (the database's entries), each changed version file passing the D9
    contract against the parent. → {candidate_sha, manifest};
    ReleaseRefused (the reason) otherwise."""
    match = _RELEASE_SUBJECT_RE.match(_subject(repo, commit))
    if not match or not _same_label(match.group(1), version):
        raise ReleaseRefused(f"its subject is not `release: v{version}`")
    parents = _parents(repo, commit)
    if len(parents) != 1:
        raise ReleaseRefused(f"it has {len(parents)} parents — the candidate is ambiguous")
    parent = parents[0]
    changed = sorted(_diff_names(repo, parent, commit) - {run.changelog_rel})
    if contract is not None and changed != contract:
        raise ReleaseRefused(f"it changed {changed} but the version-file contract is "
                             f"{contract}")
    manifest: dict = {"tree": _tree(repo, commit)}
    if run.changelog_rel:
        manifest["changelog"] = _legacy_section(
            run, version, ids, gitinfo.git_read_blob(repo, commit, run.changelog_rel))
    files = []
    for rel in changed:
        data = gitinfo.git_read_blob(repo, commit, rel)
        try:
            _legacy_version_file(rel, gitinfo.git_read_blob(repo, parent, rel), data, version,
                                 git=True)
        except ValueError as exc:
            raise ReleaseRefused(f"version file {rel}: {exc}") from None
        files.append({"path": rel, "sha256": _sha256(data)})
    manifest["version_files"] = files
    return {"candidate_sha": parent, "manifest": manifest}


def gather_evidence(engine: sa.Engine, project_slug: str, version: str, *,
                    cwd: str | None = None) -> dict:
    """Read-only evidence of a legacy release of `version` WITHOUT a record
    (K-60, K-86, K-96 D8). Refuses (ReleaseRefused) on ANY contradiction —
    never guesses.

    1. The AUTHORITATIVE spelling first (K-86): changelog.version_spelling —
       the shared resolver core's reconstruct_release uses (the first-stamped
       entry's spelling, else current_version); the requested V is only
       reported (`requested_version`), every comparison is same_version, and
       files, section, tag and record all refer to that resolved version. It
       must fit the schema (ensure_version_fits, K-87). No entry carries V
       and it is not current_version → refused.
    2. Git (D8): commits titled `release: v<V>` (any equivalent spelling, any
       local ref) and the commit the tags of V point at are CANDIDATES, each
       verified on its own (_verify_release_candidate: subject, one parent,
       the changed paths vs the contract, the section once with the
       database's entries, each version file's FIELD / changed lines — J8:
       never a substring). Tags of V must all point at ONE commit. With a
       tag: that commit must verify → status 'completed'. Without a tag:
       exactly one verified candidate → status 'committed' (the evidence
       ends at the commit); none or several → refused.
    3. Without git: F as found and the contract's files (field / a line
       holding V) → 'completed'.

    Returns {version (authoritative), requested_version, spellings (the
    equivalence map {stored spelling: [entry ids]}), entry_ids,
    candidate_sha, release_commit_sha, tag, tag_proof (K-115 G4: the
    verified identity {name, object, commit, annotated} of the tag at the
    release commit — what the record stores; null without a tag or without
    git), checkout_root, manifest, status, without_git, candidates
    ([{commit, verified, reason}], git only)}."""
    requested = normalize_version(version)
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    if ledger.get_release(engine, project_slug, requested) is not None:
        raise ReleaseRefused(f"version {requested} already has a release record — nothing to "
                             f"reconstruct")
    try:
        spelling = version_spelling(engine, project_slug, requested)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    if spelling["authoritative"] is None:
        raise ReleaseRefused(
            f"no entry carries {requested} and it is not the current version "
            f"({project.get('current_version')}) — the database does not prove this release")
    version = normalize_version(spelling["authoritative"])
    try:
        ensure_version_fits(version)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    ids = sorted(e["id"] for e in list_entries(engine, project_slug, version=version))
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=None)
    contract = ledger.get_version_files(engine, project_slug)["version_files"]
    evidence = {"version": version, "requested_version": requested,
                "spellings": spelling["spellings"], "entry_ids": ids, "candidate_sha": None,
                "release_commit_sha": None, "tag": None, "tag_proof": None,
                "checkout_root": None, "without_git": checkout is None, "status": "completed"}
    if checkout is None:
        if not project_root:
            raise ReleaseRefused("the project has no path — no artifact can be verified")
        manifest: dict = {}
        if run.changelog_rel:
            manifest["changelog"] = _legacy_section(
                run, version, ids, _read_bytes(run.abspath(run.changelog_rel)))
        files, bad = [], []
        for rel in contract or []:
            data = _read_bytes(run.abspath(rel))
            try:
                _legacy_version_file(rel, None, data, version, git=False)
            except ValueError as exc:
                bad.append(f"{rel}: {exc}")
                continue
            files.append({"path": rel, "sha256": _sha256(data)})
        if bad:
            raise ReleaseRefused(f"version files without {version}: {'; '.join(bad)} — refused.",
                                 details={"missing": bad})
        manifest["version_files"] = files
        evidence["manifest"] = manifest
        return evidence
    repo = checkout["repo_root"]
    tags = _version_tags(repo, version)
    tag_commits = sorted({t["commit"] for t in tags})
    if len(tag_commits) > 1:
        raise ReleaseRefused(
            f"the tags of {version} point at different commits "
            f"({', '.join(t['name'] + '→' + t['commit'] for t in tags)}) — a contradiction; "
            f"refused, never guessed.", details={"tags": tags})
    tagged = tag_commits[0] if tag_commits else None
    pool = sorted(set(_release_commits(repo, version)) | ({tagged} if tagged else set()))
    candidates, verified = [], {}
    for sha in pool:
        try:
            verified[sha] = _verify_release_candidate(run, repo, sha, version, ids, contract)
            candidates.append({"commit": sha, "verified": True, "reason": None})
        except ReleaseRefused as exc:
            candidates.append({"commit": sha, "verified": False, "reason": str(exc)})
    reasons = "; ".join(f"{c['commit'][:12]}: {c['reason']}" for c in candidates
                        if not c["verified"])
    if tagged is not None:
        if tagged not in verified:
            raise ReleaseRefused(
                f"the tag(s) {', '.join(t['name'] for t in tags)} of {version} point at "
                f"{tagged}, which is not a verifiable release commit ({reasons}) — refused.",
                details={"tags": tags, "candidates": candidates})
        commit, status = tagged, "completed"
    else:
        if len(verified) != 1:
            what = "none of the candidates verifies" if not verified else \
                f"{len(verified)} verified release commits — ambiguous"
            raise ReleaseRefused(
                f"no tag of {version}, and {what} ({len(pool)} candidate(s) titled `release: "
                f"v{version}`{'; ' + reasons if reasons else ''}) — refused, never guessed.",
                details={"candidates": candidates})
        commit, status = next(iter(verified)), "committed"
    found = verified[commit]
    # K-115 G4: the tag identity as verified now — what 'completed' records
    # (None without a tag: the evidence then ends at 'committed').
    tag_proof = _tag_proof(tags, version, commit) if tagged is not None else None
    evidence.update(
        candidate_sha=found["candidate_sha"], release_commit_sha=commit, checkout_root=repo,
        status=status, manifest=found["manifest"], candidates=candidates,
        tag=None if tag_proof is None else tag_proof["name"], tag_proof=tag_proof)
    return evidence


@_conflict_boundary("reconstruct")
def reconstruct(engine: sa.Engine, project_slug: str, version: str, *,
                confirm: bool = False, cwd: str | None = None) -> dict:
    """`dct release reconstruct` (K-60): gather_evidence (read-only) and,
    only with confirm=True, core reconstruct_release with exactly that
    evidence (no re-stamp) — gathered and recorded under the project
    operation lock (K-96 D7; the preview takes no lock). Usable by both
    executors (K-85: it performs no effect). Outcome 'preview' (nothing
    written) or 'reconstructed'."""
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
        record = _ledger_write(engine, project_slug, evidence["version"], "reconstruct",
                               lambda: ledger.reconstruct_release(
            engine, project_slug, evidence["version"], entry_ids=evidence["entry_ids"],
            candidate_sha=evidence["candidate_sha"],
            release_commit_sha=evidence["release_commit_sha"], manifest=evidence["manifest"],
            status=evidence["status"], confirm=True, checkout_root=evidence["checkout_root"],
            without_git=evidence["without_git"], tag_proof=evidence["tag_proof"]), steps=[])
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    return result("reconstruct", project_slug, "reconstructed", version=record["version"],
                  release=record, details={"evidence": evidence})
