"""The release EFFECTS layer (K-72 layer 3) — the default executable release
workflow behind `/release`: `dct release attest / prepare / apply / resume /
abandon / reconstruct / takeover / version-files` (and `dct clog release`, an alias of apply without
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
reconstruct --confirm, takeover --confirm, version-files --confirm and the
old ledger commands (`clog release-advance` =
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
without a push — and the refusal names the two ways to finish it: the
ledger path, or the controlled TAKEOVER, K-119 M3 / K-117 J4: `dct release
takeover` verifies the release's existing artifacts, shows the consent and
the proposal derived from them, and records them once the owner confirms
their digest — no re-stamp, no recorded value changed; resume then performs
only the missing effects). The proof layer, attest (a proof-layer commit),
the ledger, abandon and reconstruct are shared by both executors — the
reconstruction through the executor's ADAPTER: dct's own rules
(gather_evidence) for a 'dct' project, the evidence its tool supplies
(gather_own_evidence) for an 'own' one. Progress writes carry
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
     stamped. `prepare --preview` (preview_release) is the one-dialog
     form: the same pre-checks and isolated sync on SYNTHETIC commits of
     the uncommitted work (and of an attestation), the consent view of each
     variant and its digest C stored in the project's preview slot — see
     the preview section below and docs/release-workflow.md. `apply
     --consent C` (apply_consent) releases one of those variants under ONE
     lock: the view re-derived on synthetic commits must be C (nothing
     made), then the work-in-progress commit and the attestation commit
     through _hook_commit, the view at the real candidate must still be C,
     and the stamp consumes the preview (core release_entries
     consume_preview); a re-run adopts what an interrupted one made.
  1. pre-checks of apply — strict checkout (K-52; a registered path), no
     unfinished record, clean index and work tree (K-50); the plan
     re-derived FOR THE PINNED CANDIDATE must match the approved snapshot
     digest (K-80); the requested set ⊆ the plan's proven ids; git itself
     recent enough for the hook lifecycle (MIN_GIT_VERSION, K-103); the ONE
     push target (push_target, K-104 / K-123: a new release names it
     COMPLETELY — --push-remote, --push-branch and --push-url, what
     prepare showed and the owner approved — or --no-push; apply never
     selects the upstream, a missing part is refused; its URL must be one
     of the remote's push addresses from the full `get-url --push --all`
     and addressable literally — no insteadOf rewrite, not a remote's
     name, K-124) and that URL read
     once (no tag of V there, its branch contained in the candidate — K-97
     F2); the approved proposal (--proposal-digest, REQUIRED for a new
     release — also for the alias, K-117) must be the pending one, bound
     to the same candidate, version and plan digest (K-116 H2), with the
     script and the baseline unchanged, its blobs available and pinned;
     for a NEW release what a later step would otherwise find first
     (_new_release_prechecks, prepare too): the WHOLE changelog composition
     on the candidate's content (#876 K-9 — expected_changelog, the write's
     own function, `provisional`: never a trial stamp) and its git filter
     path (K-20 / K-26, _composed_forms), no local tag of V (K-8), and the commit
     identity git resolves (apply); HEAD unchanged and no foreign
     index.lock right before the stamp (K-25 — a lock, a refusing hook or
     a failing signature that only APPEARS later stops the release after
     the stamp; resume finishes it, no trial commit);
  2. stamp — core release_entries (the record, status 'stamped'), bound to
     the plan inside its transaction (K-81: stampable_ids, expected_entries)
     and consuming the approved proposal in the SAME transaction
     (consume_proposal: the record's proposal + consent — with the push
     target, K-97 F4: what every resume executes — the learned
     version-file contract, the untracked baseline — K-76, K-96 D3/D4);
     from here the record's `version` is the spelling of every artifact
     (K-55);
  3. changelog file F — WHICH file is the ONE resolver's answer
     (core.changelog_file, #876: detected among the candidate's tracked
     files; every problem — several Markdown changelogs, a stored
     case-variant, a non-Markdown changelog, a symlink / file / submodule
     on its path in the candidate or on disk (K-19), a work-tree file that
     is not the candidate's, a content the composition or its filters
     refuse — is refused by the pre-checks BEFORE the stamp,
     _check_changelog_file / _check_changelog_content; the write guards
     the project boundary again, _guard_changelog_write; written in the
     checkout form, committed as what git stores for it, K-20); the script is NEVER run again after the stamp; F's
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
  7. push (K-97 F2, K-104) — `git push --atomic --no-follow-tags
     --recurse-submodules=no <url> <commit>:refs/heads/<branch>
     <commit>:refs/tags/<tag>`: the release commit and the tag to
     the CONSENTED target's URL itself with explicit refspecs (a later
     change of the remote's configuration adds no recipient; no followed
     tag, no submodule push — K-126, K-134), verified
     with `git ls-remote <url>` — the same address → 'pushed' with its
     push_proof (K-115 G4) and the remote-tracking ref refreshed to the
     observed tip when the fetch configuration confirms the mapping
     (K-35, a warning otherwise), then 'completed' (the proposal's pin / store
     blobs are released); a failed push leaves 'tagged' and resume retries
     only the push. Without a push target (--no-push, outside git) the
     record goes 'tagged' → 'completed' (no push proof: the consent's
     push_target null is the explicit local release).

Reconstruction (gather_evidence, K-86 / K-96 D8 / K-109 / K-110): the
authoritative version spelling is resolved FIRST by the shared legacy
resolver (changelog.version_spelling); release commits on refs and the
tag's commit are candidates, each verified (one parent, the contract, the
section, the WHOLE older changelog history against the parent — a changed
older section, also a typo fix, is a deviation that is shown as a diff and
never verified —, the version files' fields — never a substring, J8); a
tag → 'completed', a single verified commit without a tag → 'committed',
several → all listed, the choice explicit. Each candidate's changelog file
is its own (K-12 / K-21, _candidate_changelog: a takeover's recorded
verified manifest, else the one Markdown file the commit changed that holds
the V section — several refuse) — never today's path or detection mode. An UNKNOWN version-file contract
(None) is never read as a confirmed "none" ([]): outside git it refuses
with the procedure that shows and confirms it (version_files_contract);
the scope of the verification is recorded (manifest.verification). Nothing
in the repository is ever modified by a reconstruction.

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
import shlex
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
    check_release_selection,
    list_entries,
    normalize_worktree,
    release_entries,
    version_spelling,
)
from dct.core.changelog_file import (
    EXPLICIT_MARKER,
    MARKDOWN_EXTS,
    check_work_tree,
    choice_settings,
    content_problem,
    explain,
    resolve_changelog,
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
    version_key,
)
from dct.db import is_serialization_failure
from dct.export import export_changelog_md
from dct.workflows.release import oplock
from dct.workflows.release import plan as planning
from dct.workflows.release.proof import (
    ATTEST_SUBJECT,
    attest_message,
    message_subject,
    proven_ids,
)

SCHEMA_VERSION = 1
RESULT_KEYS = (
    "schema_version", "action", "project", "outcome", "version", "release",
    "steps", "warnings", "details",
)
OUTCOMES = (
    "completed", "noop", "attested", "abandoned", "preview", "reconstructed",
    "prepared", "advanced", "refused", "failed", "conflict", "found", "none",
    "taken_over", "confirmed",
)
# K-88: the exit code of a lost serialization race (sysexits EX_TEMPFAIL —
# the failed write may succeed when the command is run again; never retried
# automatically).
CONFLICT_EXIT_CODE = 75
# Steps that are effects on the world (the stamp included) — what a conflict
# report lists as observed; `sync` ran in an isolated copy, `complete` is
# only a progress write.
EFFECT_STEPS = ("stamp", "changelog", "version_files", "commit", "index", "tag", "push",
                "attest", "work_commit")
# The step whose progress write records each status (advance_release).
_STATUS_STEP = {"files_written": "files", "committed": "commit", "tagged": "tag",
                "pushed": "push", "completed": "complete"}

# Where a test (or a debugger) may stop the workflow: before each effect and
# between each effect and the progress write that records it.
INTERRUPTION_POINTS = (
    # apply --consent only (the one-dialog release, before the stamp):
    "before_work_commit",    # every pre-commit check passed; no hook has run, nothing made
    "after_work_commit",     # the work-in-progress commit W published + recorded in the
                             # preview slot's progress; the real index NOT yet synced
    "after_work_index",      # the real index entries of Pₛ = W's
    "after_attest_commit",   # the attestation commit A published + recorded in the progress
    "before_stamp",          # nothing stamped yet (apply --consent: W / A may exist)
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
# The points only `apply --consent` reaches (before its stamp; a re-run of
# the same command resumes them — `dct release resume` has no record yet).
CONSENT_INTERRUPTION_POINTS = ("before_work_commit", "after_work_commit", "after_work_index",
                               "after_attest_commit")

VERSION_SYNC_SCRIPTS = ("scripts/dct-version-sync.sh", "scripts/version-sync.sh")
VERSION_ONLY_LINE = "- Version-only release — no changelog entries."
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
    """The full name of the ref HEAD NAMES, None for a detached HEAD
    (gitinfo.head_ref — the one reader the plan's adoption predicate uses too)."""
    return gitinfo.head_ref(repo)


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


def _identity_before_stamp(run, repo: str, *, nothing: str = "nothing released.") -> None:
    """K-8 audit: the identity the release commit needs, checked BEFORE the
    stamp (otherwise first resolved by the commit step after it, leaving an
    unfinished record) — _commit_identity, its refusal worded "refused
    before the stamp; <nothing>"."""
    _commit_identity(
        lambda message, **details: run.refuse(
            message.replace("nothing committed.", f"refused before the stamp; {nothing}"),
            **details),
        repo)


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
    on_published: Callable[[str], None] | None = None,
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
       writes `tree` (a hook that staged / reformatted content → refused,
       `details.hook_changed_tree: {paths}`),
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
       staging untouched. ANY failure of the transaction (an error, a
       timeout, an interruption — e.g. a `reference-transaction` hook that
       fails or hangs in its "committed" state, after the ref moved) re-reads
       the pinned ref: when it IS the new commit, the commit is published —
       `on_published(commit)` runs, then the error propagates (a git error
       as a refusal with `published_commit`); post-commit is not run.
    6. `on_published(commit)` — the caller records the published commit
       (apply --consent: the slot's progress) BEFORE any user code runs
       again; a refusal it raises propagates (post-commit is then not run).
    7. post-commit — only after the publication and on_published; a failure
       is a WARNING: the commit stays, the hook is never run again (resume
       adopts the commit).

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
                changed=changed, hook_changed_tree={"paths": changed})
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
            try:
                gitinfo.git_update_ref_cas(
                    repo, ref or "HEAD", commit, parent, message=f"commit: {subject}",
                    no_deref=ref is None, verify=still_named, timeout=HOOK_GIT_TIMEOUT_SECONDS)
            except BaseException as exc:
                # The `reference-transaction` hook's "committed" state runs
                # AFTER the ref moved and before update-ref confirms: a hook
                # that fails, hangs (timeout) or is interrupted there leaves
                # a PUBLISHED commit behind an error. The pinned ref decides.
                if _rev(repo, ref or "HEAD") != commit:
                    raise
                if on_published is not None:
                    on_published(commit)
                if not isinstance(exc, gitinfo.GitWriteError):
                    raise
                raise run_refuse(
                    f"the commit {commit} WAS published on {ref or 'the detached HEAD'} and is "
                    f"recorded, but git did not confirm the ref update ({exc}) — a "
                    f"reference-transaction hook that failed or hung after the update? The "
                    f"post-commit hook was not run; fix the hook and run the command again "
                    f"(it adopts the commit).", git_error=str(exc), pinned_ref=ref,
                    head=_rev(repo, "HEAD^{commit}"), published_commit=commit) from None
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
        # 6. — published: recorded before the user's post-commit hook runs
        # (a kill inside it must find the commit recorded).
        if on_published is not None:
            on_published(commit)
        # 7. — post-commit cannot undo or repeat it.
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


def _sync_real_index(run: _Run, commit: str, paths: list[str], *, base: str,
                     allowed_before: dict[str, tuple[str, str, str] | None] | None = None,
                     what: str = "the release commit",
                     next_step: str = "run `dct release resume`") -> list[str]:
    """Bring the user's REAL index in line with the verified commit `commit`
    (HEAD) for `paths` — K-79, under git's index.lock (_index_lock), never
    by running `git add` / `git commit` on the real index. Under the lock
    the WHOLE relevant state is re-checked: HEAD must still be `commit`, and
    every path's index entry must be `base`'s (the commit's parent side: the
    release candidate, or H0 of a work-in-progress commit) or one of
    `allowed_before` ({path: entry | None} — the entries the owner's index
    held when the content was approved, apply --consent) (→ updated to the
    commit's entry), or already the commit's (→ kept); anything else is the
    user's own staging → refused, PRESERVED (nothing written,
    `details.foreign_staged`). Every other index entry (foreign staging
    elsewhere) is copied unchanged. The new index is prepared on a COPY (a
    temporary GIT_INDEX_FILE), written into the lock and renamed into
    place. `what` / `next_step` word the refusals. Returns the updated
    paths."""
    repo = run.repo
    before = planning.tree_entries(repo, base, paths)
    target = planning.tree_entries(repo, commit, paths)
    allowed = allowed_before or {}
    index_path = _git_path(repo, "index")
    with _index_lock(run.refuse, index_path) as fd:
        current = _read_bytes(index_path)
        if current is None:
            raise run.refuse(f"the index {index_path} is missing — nothing changed.",
                             commit=commit)
        head = _rev(repo, "HEAD^{commit}")
        if head != commit:
            raise run.refuse(
                f"HEAD moved to {head} after {what} {commit} was created — the index is not "
                f"updated and the commit is not recorded; nothing else done.",
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
                if entry == before.get(rel) or (rel in allowed and entry == allowed[rel]):
                    todo.append(rel)
                else:
                    foreign.append(rel)
            if foreign:
                raise run.refuse(
                    f"{', '.join(foreign)}: your index holds a staged version that is neither "
                    f"the one before {what} nor its own — PRESERVED, the index is not updated. "
                    f"{what[0].upper() + what[1:]} {commit} exists (HEAD); it is not recorded "
                    f"yet: unstage or commit your change yourself, then {next_step}.",
                    foreign_staged=foreign, commit=commit, head=commit)
            for rel in todo:
                if rel not in target:  # deleted by the commit
                    gitinfo.git_write(repo, "update-index", "--force-remove", "--", rel,
                                      index_file=copy)
                    continue
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


def _tag_proof(tags: list[dict], version: str, commit: str) -> dict | None:
    """The identity of the release tag as VERIFIED in the repository now
    (K-115 G4 — core.releases.validate_tag_proof): of the tags of the
    version (`gitinfo.version_tags`) that target `commit`, `v<version>` when
    it is one of them, else the first by name; None when no tag targets
    it."""
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
        self.changelog: dict | None = None
        self.changelog_rel: str | None = None
        # K-35: the remote-tracking ref refreshed after a verified push
        # (_refresh_tracking_ref) — reported as details.tracking_ref.
        self.tracking_ref: dict | None = None
        self.bind_changelog(checkout["head"] if checkout else None)

    def bind_changelog(self, commit: str | None) -> None:
        """#876 — the project's changelog file F from the ONE resolver
        (core.changelog_file.resolve_changelog): in git among the tracked
        files of `commit` (the candidate), outside git in the project
        directory. `changelog_rel` is its base-relative path — None when dct
        manages no changelog file, or when the resolution has a problem,
        which every path refuses before any effect (_check_changelog_file
        before the stamp, _record_proposal after it)."""
        if not self.project_root:
            self.changelog, self.changelog_rel = None, None
            return
        self.changelog = resolve_changelog(self.project, project_root=self.project_root,
                                           repo_root=self.repo,
                                           commit=commit if self.repo else None)
        self.changelog_rel = self.changelog["path"]

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


def _check_changelog_file(run: _Run, *, work_tree: bool = True) -> None:
    """#876 — every condition of the changelog file's RESOLUTION that can
    refuse, checked BEFORE the stamp (prepare, apply's pre-checks; a
    reconstruction outside git before it records anything — in git a
    historical release is judged at its own commit, _candidate_changelog;
    the content: _check_changelog_content): no file guessed among several Markdown
    changelogs, no stored path whose case-variant is tracked, no Markdown
    written into (or beside) a non-Markdown changelog and — in git, with
    `work_tree` — the work tree's file at the path is the candidate's
    content (core.changelog_file.check_work_tree: an ignored / untracked
    file, or a tracked case-variant on a case-insensitive file system, is
    never overwritten after a stamp). Refused with details.changelog_file
    (the resolution) and the true next steps."""
    found = run.changelog
    if found is None:
        return
    if work_tree and run.repo:
        found = check_work_tree(found, repo_root=run.repo, commit=run.checkout["head"],
                                slug=run.slug)
    if found["problem"]:
        raise run.refuse(f"changelog file: {explain(found)}; nothing done.",
                         changelog_file=found)


def _stamp_proof(plan: dict, ids: list[int]) -> dict | None:
    """The proof map the stamp stores for `ids` — the approved snapshot's."""
    proof = plan["snapshot"]["proof"]
    return {str(i): proof[str(i)] for i in ids if str(i) in proof} or None


def _provisional_record(run: _Run, target: str, ids: list[int], plan: dict) -> dict:
    """What the stamp of a new release WOULD record and the section render
    reads (version, created_at — now —, entry_ids, render, proof) — data
    only, nothing is written (render_section `provisional`)."""
    try:
        proof = ledger.validate_proof(_stamp_proof(plan, ids), "git")
        check_release_selection(run.engine, run.slug, target, ids)
    except ValueError as exc:  # the stamp would refuse it the same way
        raise run.refuse(f"{exc}") from None
    return {"version": normalize_version(target), "created_at": datetime.now(UTC),
            "entry_ids": list(ids),
            "render": {"pr_refs": bool(run.project.get("changelog_pr_refs"))},
            "proof": proof}


def _check_changelog_content(run: _Run, target: str, ids: list[int], plan: dict, *,
                             record: dict | None = None) -> bytes | None:
    """#876 K-9 — BEFORE the stamp of a new release: the WHOLE changelog
    composition (expected_changelog — the function the write calls after
    the stamp) runs on F's content before the release (in git the
    candidate's blob, outside git the file on disk) for the release about
    to be stamped, and in git its render + filter path (_composed_forms,
    K-20: what git stores for the composition keeps its text). Every
    refusal it raises over that content (not UTF-8, a different V section,
    an insertion that fails its own verification, filters that rewrite the
    text) refuses here with details.changelog_file (problem `content`) and
    true next steps — never a trial stamp. Returns the composition in its
    work-tree form (None when dct manages no changelog file); `record` —
    the provisional record the caller already built (the preview renders
    its section from the same one)."""
    rel = run.changelog_rel
    if not rel:
        return None
    if run.repo:
        base = gitinfo.git_read_blob(run.repo, run.checkout["head"], rel)
    else:
        base = _read_regular(run.abspath(rel))
        if base == _NOT_A_FILE:
            raise run.refuse(f"{rel} is not a regular file — nothing released.")
    if record is None:
        record = _provisional_record(run, target, ids, plan)
    try:
        composed = expected_changelog(run.engine, run.slug, record, base, provisional=True)
        # K-20: the whole render + filter path — what git stores for the
        # composition keeps its text (line endings aside).
        work, _ = _composed_forms(run, rel, composed)
    except ReleaseRefused as exc:
        reason = exc.details.get("content")
        if reason is None:
            raise run.refuse(f"{exc} — refused before the stamp.", **exc.details) from None
        detail = str(exc).removesuffix(" — not touched").removesuffix(" — not overwritten.") \
            .removesuffix(" — not written")
        found = content_problem(run.changelog, slug=run.slug, reason=reason, detail=detail,
                                version=record["version"], in_git=bool(run.repo))
        raise run.refuse(f"changelog file: {explain(found)}; nothing done.",
                         changelog_file=found) from None
    return work


def _check_local_tags(run: _Run, target: str) -> None:
    """K-8 — BEFORE the stamp of a new release in git: no tag of V (any
    spelling, gitinfo.version_tags) may exist locally — the release commit does
    not exist yet, so no such tag could ever be adopted by the tag step."""
    tags = gitinfo.version_tags(run.repo, normalize_version(target))
    if tags:
        names = ", ".join(f"{t['name']} → {t['commit']}" for t in tags)
        deletes = " ".join(f"`git tag -d {t['name']}`" for t in tags)
        raise run.refuse(
            f"tag(s) of {target} already exist in this repository ({names}) — the release "
            f"tags its OWN release commit, which does not exist yet, so the tag step could "
            f"never adopt them; refused before the stamp. Delete a leftover tag yourself "
            f"({deletes}) if it is no release, or choose another version, then plan again; "
            f"nothing released.", tags=tags)


def _new_release_prechecks(run: _Run, target: str, ids: list[int], plan: dict) -> None:
    """The conditions of a NEW release (no record of V, V above the current
    version) known before the stamp but otherwise checked only by a step
    after it — prepare and apply both run them, before anything is written:
    the changelog composition (K-9) and a local tag of V (K-8)."""
    if ledger.get_release(run.engine, run.slug, target) is not None \
            or not _newer_than_current(run.project, target):
        return
    _check_changelog_content(run, target, ids, plan)
    if run.repo:
        _check_local_tags(run, target)


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
            f"be verified in a git checkout; nothing done (`dct release takeover` records "
            f"the verified ones of a release whose commit exists; else abandon it).",
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


def render_section(engine: sa.Engine, slug: str, record: dict, *,
                   provisional: bool = False) -> tuple[str, str]:
    """(section, date) of the release — deterministic, from the database
    and the record only (K-32/K-49): the export's block of the record's
    version (every equivalent stored spelling, K-55) headed `## [<record
    version>] - <record created_at date>`; a version-only release gets
    VERSION_ONLY_LINE. Ends with exactly one newline. `provisional` (#876
    K-9): `record` is the release ABOUT TO BE stamped ({version, created_at,
    entry_ids, render, proof} — _provisional_record) and the export renders
    its entries as if they carried the version (export_changelog_md
    `provisional`) — the same composition, before the stamp."""
    version, date = record["version"], _date_of(record["created_at"])
    block = export_changelog_md(engine, slug, version=version,
                                provisional=record if provisional else None)
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


def expected_changelog(engine: sa.Engine, slug: str, record: dict, base: bytes | None, *,
                       provisional: bool = False) -> bytes:
    """The expected content of F after the release (K-32) from its content
    BEFORE the release (`base`; None = F does not exist — bootstrap):
      - bootstrap: the full export (no Unreleased) with the release's block
        replaced by / completed with the rendered section — V exactly once;
      - existing F without a V section: the section inserted on top, the
        rest byte-identical, one section more;
      - existing F already holding the IDENTICAL V section once: unchanged;
        any other V section (different text, several) → ReleaseRefused.
    A refusal caused by an existing F's CONTENT carries details.content
    ('not_utf8' | 'section_differs' | 'insertion'). The ONE composition: the write
    (_step_files, after the stamp) and the pre-check of a new release
    (_check_changelog_content, BEFORE it — `provisional`, see
    render_section) both call it (#876 K-9)."""
    version = record["version"]
    section, _ = render_section(engine, slug, record, provisional=provisional)
    if base is None:
        full = export_changelog_md(engine, slug, include_unreleased=False,
                                   provisional=record if provisional else None)
        for _, start, end in reversed(_version_blocks(full, version)):
            full = full[:start] + full[end:]
        return _insert_section(full, section, version).encode("utf-8")
    try:
        text = base.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseRefused("the changelog file is not UTF-8 — not touched",
                             details={"content": "not_utf8"}) from None
    existing = _version_blocks(text, version)
    if len(existing) == 1 and _norm_block(text[existing[0][1]:existing[0][2]]) == \
            _norm_block(section):
        return base
    if existing:
        labels = ", ".join(f"[{label}]" for label, _, _ in existing)
        raise ReleaseRefused(
            f"the changelog file already holds {len(existing)} {version} section(s) ({labels}) "
            f"that differ from the expected one — not overwritten.",
            details={"sections": len(existing), "content": "section_differs"})
    try:
        return _insert_section(text, section, version).encode("utf-8")
    except ReleaseRefused as exc:  # its own verification of the result
        raise ReleaseRefused(str(exc), details={"content": "insertion"}) from None

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
    it). #876 K-29: with `core.safecrlf=false` — git's round-trip guard
    would die on a CRLF changelog under `text=auto` ("CRLF would be
    replaced by LF") where the owner's own `git add` succeeds; dct checks
    the preservation of the changelog's text itself (_composed_forms), and
    the guard changes no id."""
    if run.repo is None:
        return _sha256(data)
    with tempfile.TemporaryDirectory(prefix="dct-release-blob-") as tmp:
        scratch = os.path.join(tmp, "content")
        with open(scratch, "wb") as fh:
            fh.write(data)
        _, out = gitinfo.git_write(run.repo, "-c", "core.safecrlf=false", "hash-object", "-w",
                                   f"--path={rel}", "--", scratch)
    return out.strip().lower()


def _raw_blob(run: _Run, data: bytes) -> str:
    """The blob id of `data` AS IS (`git hash-object -w --no-filters`) —
    written to the object database (a checkout form is read from it)."""
    with tempfile.TemporaryDirectory(prefix="dct-release-blob-") as tmp:
        scratch = os.path.join(tmp, "content")
        with open(scratch, "wb") as fh:
            fh.write(data)
        _, out = gitinfo.git_write(run.repo, "hash-object", "-w", "--no-filters", "--", scratch)
    return out.strip().lower()


def _checkout_form(run: _Run, rel: str, data: bytes | None) -> bytes | None:
    """The work-tree bytes of `data` as the content of `rel` (K-11): in git
    `data` is BLOB content and the bytes are what a checkout of it writes
    (`git cat-file --filters`: eol / smudge filters of the path applied —
    the form the work tree holds and the release compares and writes);
    outside git the bytes themselves. None for None."""
    if data is None or run.repo is None:
        return data
    return gitinfo.git_cat_blob(run.repo, _raw_blob(run, data), path=rel)


def _eol(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _composed_forms(run: _Run, rel: str, composed: bytes) -> tuple[bytes, str]:
    """#876 K-20 / K-26 — the two forms of F's COMPOSED content
    (expected_changelog: blob content), kept apart: (expected WORK-TREE
    bytes, id of the content git STORES — what the release commit's tree
    holds). Outside git: (the bytes, their SHA-256).

    The stored id is what git's clean / eol filters make of the
    composition as a checkout writes it (_checkout_form, then `git
    hash-object --path`, _id_of_bytes). The work-tree bytes are the
    checkout form of THAT STORED content (K-26: exactly what a fresh
    checkout of the release commit writes — under `* text=auto` a CRLF
    history is stored LF and checked out LF, so the work tree holds LF, as
    a fresh clone does; the next release's work-tree check then passes),
    and they must be a FIXED POINT of the filters: `hash-object --path` of
    them gives the stored id again.

    The filter path must KEEP the composed text: the stored content is the
    composition itself or the composition with its line endings normalised
    (CRLF → LF — e.g. a CRLF changelog under `text=auto`); anything else (a
    clean filter rewriting the text) or a checkout form that is no fixed
    point refuses with details.content 'filtered' — called BEFORE the stamp
    too (_check_changelog_content), so such a filter path never stamps."""
    if run.repo is None:
        return composed, _sha256(composed)
    stored = _id_of_bytes(run, rel, _checkout_form(run, rel, composed))
    if stored not in {_raw_blob(run, composed), _raw_blob(run, _eol(composed))}:
        raise ReleaseRefused(
            f"the git filters of {rel} do not keep the release's text: what git stores for "
            f"the composed changelog ({stored}) is neither that text nor that text with its "
            f"line endings normalised — not written", details={"content": "filtered"})
    work = gitinfo.git_cat_blob(run.repo, stored, path=rel)
    again = _id_of_bytes(run, rel, work)
    if again != stored:
        raise ReleaseRefused(
            f"the git filters of {rel} are not stable: what a checkout of the stored changelog "
            f"({stored}) writes is stored as {again} again — the release could not leave the "
            f"work tree a checkout of its own commit; not written",
            details={"content": "filtered"})
    return work, stored


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
    every ref, the index, the status, the bytes of every untracked file and
    — `tracked` — of every tracked path that already has a change
    (_work_tree_dirt: content, with its executable bit; a deleted or retyped
    file reads otherwise), so a rewrite of a file that was " M" before and
    is " M" after is seen too. `refs` ("<refname> <object>") and `index`
    ("<mode> <blob> <stage>\\t<path>") are LISTS of lines, so _state_diff
    reports the lines that differ — the refs and the files by name."""
    _, refs = gitinfo.git_read(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    _, index = gitinfo.git_read(repo, "ls-files", "-s", "-z")
    _, status = gitinfo.git_read(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    untracked = {}
    for token in status.split("\0"):
        if token.startswith("?? "):
            data = _read_regular(os.path.join(repo, token[3:]))
            untracked[token[3:]] = _sha256(data) if isinstance(data, bytes) else str(data)
    tracked = {rel: [digest, _executable(os.path.join(repo, *rel.split("/")))]
               for rel, digest in _work_tree_dirt(repo, ()).items()}
    return {"head": _rev(repo, "HEAD"),
            "refs": sorted(line for line in refs.splitlines() if line),
            "index": sorted(entry for entry in index.split("\0") if entry),
            "status": sorted(t for t in status.split("\0") if t), "untracked": untracked,
            "tracked": tracked}


def _executable(path: str) -> bool:
    """The executable bit of a regular file in the work tree (git's only
    file mode besides 644); False for anything else or nothing there."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)


def _state_diff(before: dict, after: dict) -> dict:
    """{key: what differs}: a dict → its differing keys, a list → the
    entries in only one of the two (symmetric difference), anything else
    → {"before", "after"}."""
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


def _describe_checkout_change(changed: dict, limit: int = 10) -> str:
    """K-37 — what an isolated sync changed in the user's checkout
    (_state_diff of _git_state), for the owner: the FILES (work tree,
    index, untracked) by name, then HEAD / refs — never the internal state
    keys alone. `refs` / `index` are the differing LINES of _git_state's
    lists (a moved ref appears twice — once per object — and is named once)."""
    files: list[str] = []
    for token in changed.get("status") or ():
        # porcelain v1 -z: "XY path"; a rename's source is a bare token.
        files.append(token[3:] if len(token) > 3 and token[2] == " " else token)
    files += list(changed.get("untracked") or ())
    files += list(changed.get("tracked") or ())
    files += [line.partition("\t")[2] for line in changed.get("index") or ()]
    files = sorted(dict.fromkeys(f for f in files if f))
    parts = []
    if files:
        shown = ", ".join(files[:limit])
        more = f" and {len(files) - limit} more" if len(files) > limit else ""
        parts.append(f"file(s) {shown}{more}")
    if "head" in changed:
        parts.append("HEAD")
    refs = sorted(dict.fromkeys(r.partition(" ")[0] for r in changed.get("refs") or ()))
    if refs:
        parts.append(f"ref(s) {', '.join(refs[:limit])}")
    return "; ".join(parts) or ", ".join(changed)


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
                "the version-sync script changed the checkout outside its isolated worktree: "
                f"{_describe_checkout_change(changed)} — refused; dct restores nothing: "
                "review and undo those changes yourself, then fix the script (it may change "
                "ONLY the version files, in its working directory — never publish or release); "
                "nothing prepared.",
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
                f"copy: file(s) {', '.join(changed[:10])}"
                f"{f' and {len(changed) - 10} more' if len(changed) > 10 else ''} — refused; "
                "dct restores nothing: review and undo those changes yourself, then fix the "
                "script (it may change ONLY the version files, in its working directory — "
                "never publish or release); nothing prepared.",
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
            f"`dct release apply`, or before version-file approval existed) — its files cannot be written without "
            f"re-running the version sync, which dct never does after the stamp; nothing "
            f"done (finish it through the ledger path, take over its verified artifacts with "
            f"`dct release takeover`, or abandon it with `dct release abandon`).")
    if (proposal.get("release_mode") or run.mode) != run.mode:
        raise run.refuse(f"the approved proposal of {run.version} is for a "
                         f"{proposal['release_mode']} release, this checkout is {run.mode} — "
                         f"nothing done.")
    # K-34: the ONE predicate the plan reports as `resume.blocked` — the
    # approved changelog file is not the one the resolver finds in the
    # candidate now; resume writes neither file (K-97 F4).
    blocked = planning.resume_block(run.project, run.record, run.changelog,
                                    project_root=run.project_root, repo_root=run.repo)
    if blocked is not None:
        raise run.refuse(blocked["message"], changelog_file=run.changelog,
                         approved=blocked["approved"], restore=blocked["restore"])
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


def _verified_manifest(run: _Run, proposal: dict, expected_f: tuple) -> dict:
    """K-77: read the FULL artifact set ONCE and compute every hash and the
    expected tree from that one read; every artifact must be exactly the
    approved content (F: its baseline + the rendered section — `expected_f`
    is (expected work-tree bytes, id of the content git stores for them)
    from _composed_forms, K-11 / K-20: the bytes compared as written, the
    id the tree holds) — a mismatch refuses and records nothing."""
    rels = ([run.changelog_rel] if run.changelog_rel else []) + list(proposal["paths"])
    data = {rel: _read_regular(run.abspath(rel)) for rel in rels}
    ids, bad = {}, []
    if run.changelog_rel:
        expected_bytes, expected_id = expected_f
        if data[run.changelog_rel] != expected_bytes:
            bad.append(run.changelog_rel)
        elif run.repo:
            ids[run.changelog_rel] = _id_of_bytes(run, run.changelog_rel,
                                                  data[run.changelog_rel])
            if ids[run.changelog_rel] != expected_id:
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
        manifest["tree"] = _tree_of_blobs(run.repo, run.record["candidate_sha"], ids)
    return manifest


def _foreign_message(run: _Run, foreign: list[str], changelog_base: bytes | None) -> str:
    """The refusal of _step_files for files that are neither the baseline
    nor the approved content. #876: a changelog file the candidate does NOT
    hold, found on disk, was never touched by dct — the text names what is
    there and never asks to "restore" it."""
    parts = []
    edited = [rel for rel in foreign if rel != run.changelog_rel or changelog_base is not None]
    if edited:
        parts.append(
            f"{', '.join(edited)} differ(s) from both the content before the release and the "
            f"approved content (a partial or foreign edit?) — not overwritten; restore the "
            f"file(s) yourself (or finish them exactly as approved)")
    if run.changelog_rel in foreign and changelog_base is None:
        rel = run.changelog_rel
        directory = os.path.dirname(run.abspath(rel))
        try:
            here = sorted(n for n in os.listdir(directory)
                          if n.casefold() == os.path.basename(rel).casefold())
        except OSError:
            here = []
        parts.append(
            f"{rel} did not exist before the release (the approved baseline: absent), and "
            f"what is there now ({', '.join(here) or rel}) is not the approved content — an "
            f"ignored or untracked file, a tracked file whose name differs only in case on a "
            f"case-insensitive file system, or an edit of the file this release wrote — not "
            f"overwritten; move it away (resume then writes it)")
    return (f"{'; '.join(parts)} — nothing written, the index untouched; then resume, or "
            f"abandon the release.")


def _guard_changelog_write(run: _Run) -> None:
    """#876 K-19 — at WRITE time, again: F's path never leads out of the
    project. Every directory of F below the base must be a real directory
    (or not exist yet — the write creates it), F itself a regular file or
    absent, and F inside the project root (also when the project is a
    subdirectory of the repository). The pre-checks refused every such path
    in the candidate and on disk before the stamp; this catches one that
    changed since (a directory swapped for a link) — refused, nothing
    written through it; the record stays as it is (resume once restored)."""
    rel = run.changelog_rel
    parts = rel.split("/")
    path, bad = run.base, None
    for i, name in enumerate(parts):
        path = os.path.join(path, name)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode) or (i < len(parts) - 1 and not stat.S_ISDIR(mode)) \
                or (i == len(parts) - 1 and not stat.S_ISREG(mode)):
            bad = "/".join(parts[:i + 1])
            break
    root = os.path.realpath(run.project_root)
    full = os.path.realpath(run.abspath(rel))
    if bad is None and os.path.commonpath([root, full]) == root:
        return
    where = f"{bad} is a symbolic link or not a directory / regular file" if bad else \
        f"{rel} resolves outside the project root ({full})"
    raise run.refuse(
        f"the changelog file's path changed since the pre-checks: {where} — dct never writes "
        f"the changelog through it (it could leave the project); nothing written. Put the "
        f"directory back as the candidate holds it, then run `dct release resume` (or "
        f"abandon the release).", path=rel, component=bad)


def _check_approved_composition(run: _Run, composed: bytes) -> None:
    """The changelog file this release would write (its work-tree form,
    recomposed after the stamp — on the first write and on EVERY resume) is
    the composition the owner approved on the one-dialog screen
    (consent.changelog_sha256 — the view's composed_sha256). Released
    history written into the database between the stamp and the write (at
    the first changelog file the whole history is exported) would change it
    — that window lasts until the resume, however long the interruption —
    so another composition refuses BEFORE anything is written:
    `changelog_changed` {path, approved, now}. A record whose consent binds
    none (older records, the plan / prepare path, a release without a
    changelog file) is not checked."""
    approved = (run.record.get("consent") or {}).get("changelog_sha256")
    now = _sha256(composed)
    if approved is None or now == approved:
        return
    raise run.refuse(
        f"the changelog file {run.changelog_rel} this release would write is not the one the "
        f"owner approved — the released history it is composed from changed since the "
        f"approval (an entry of an earlier version imported or edited); nothing written. "
        f"Restore that history and run `dct release resume`, or end the release with `dct "
        f"release abandon`.",
        changelog_changed={"path": run.changelog_rel, "approved": approved, "now": now})


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
    expected_f, base = (None, None), None
    if run.changelog_rel:
        base = _changelog_baseline(run, proposal)
        try:
            raw = expected_changelog(run.engine, run.slug, run.record, base)
        except ReleaseRefused as exc:
            raise run.refuse(str(exc), path=run.changelog_rel, **exc.details) from None
        # K-11 / K-20: compared and written in the form a checkout writes
        # (eol / smudge filters); committed as what git stores for those
        # bytes (its clean / eol filters — a line-ending normalisation).
        try:
            expected_f = _composed_forms(run, run.changelog_rel, raw)
        except ReleaseRefused as exc:
            raise run.refuse(f"{exc}; nothing done.", path=run.changelog_rel,
                             **exc.details) from None
        _check_approved_composition(run, expected_f[0])
        base_form = _checkout_form(run, run.changelog_rel, base)
        _guard_changelog_write(run)
        current = _read_regular(run.abspath(run.changelog_rel))
        if current == expected_f[0]:
            actions.append((run.changelog_rel, "verified"))
        elif current == base_form:
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
        raise run.refuse(_foreign_message(run, foreign, base), foreign=foreign)
    todo = dict(actions)
    _point(run.on_point, "before_changelog")
    if run.changelog_rel:
        if todo[run.changelog_rel] == "write":
            _guard_changelog_write(run)
            _write_atomic(run.abspath(run.changelog_rel), expected_f[0])
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


def _foreign_index_lock(repo: str) -> str | None:
    """The path of THIS checkout's `index.lock` when it exists (another git
    process is using the index — dct never removes it), else None."""
    lock = _git_path(repo, "index") + ".lock"
    return lock if os.path.lexists(lock) else None


def _check_index_for_commit(run: _Run, candidate: str, expected: str, paths: list[str]) -> None:
    """K-50 / K-79, read-only, before the commit: the user's index may hold
    nothing but release artifacts already staged as their EXPECTED blobs —
    a foreign staged path, foreign content inside a release file or an
    unmerged path refuses with the staging untouched; an existing
    index.lock (another git process) refuses before anything is created."""
    lock = _foreign_index_lock(run.repo)
    if lock:
        raise run.refuse(
            f"{lock} exists — another git process is using the index; dct never removes a "
            f"foreign index.lock; nothing committed — run `dct release resume` once it is gone.",
            index_lock=lock)
    staged = _staged_against(run.repo, candidate)
    expected_entries = planning.tree_entries(run.repo, expected, paths)
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
    updated = _sync_real_index(run, commit, paths, base=run.record["candidate_sha"])
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
    tags = gitinfo.version_tags(run.repo, run.version)
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
        proof = _tag_proof(gitinfo.version_tags(run.repo, run.version), run.version, commit)
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
    tags = gitinfo.version_tags(run.repo, run.version)
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

_PUSH_CHOICE = ("choose explicitly: `--push-remote <remote> --push-branch <branch>` (the "
                "remote and branch the release commit and its tag go to; an apply also "
                "names `--push-url <url>`, that remote's push address) or `--no-push` (a "
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
    ls-remote <url>`) — it must reach the repository it NAMES. Refused when
    git would read it as something else:

    - a url.<base>.insteadOf / pushInsteadOf rule whose value is a prefix
      of it would make git send (or read) elsewhere;
    - K-124: the string is the NAME of a remote — git then uses that
      remote's own url for `ls-remote` and its pushurl(s) for the push,
      not the path the string names. Every name git itself would resolve
      counts: a `remote.<name>.*` key in ANY configuration scope (system,
      global, local, worktree, includes — read with the writer's view, the
      one the push runs with) and, for a name without a directory
      separator, the legacy `<git common dir>/remotes/<name>` and
      `branches/<name>` files. No spelling proves a literal address:
      `group/publish`, `./publish` and even a URL-like string are legal
      remote names, and git reads a string as a remote whenever a remote
      of exactly that name is configured — so the WHOLE string is compared
      with every configured name.

    What is pushed to is exactly what was approved, whatever rule or remote
    is added later: the check runs before the stamp (push_target), in the
    takeover and before every push / control read (_consented_target)."""
    code, out = gitinfo.git_write(
        run.repo, "config", "-z", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$",
        ok_codes=(0, 1))
    for item in (i for i in out.split("\0") if i) if code == 0 else ():
        key, _, value = item.partition("\n")
        if value and url.startswith(value):
            raise run.refuse(
                f"the push destination {url} cannot be addressed literally: the git "
                f"configuration `{key} = {value}` (insteadOf / pushInsteadOf) would rewrite "
                f"it to another address when dct passes it to `git push` — dct pushes only to "
                f"the exact approved URL; remove that rule (or point the remote at the final "
                f"address); nothing pushed.", url=url, rewrite_rule=key)
    code, out = gitinfo.git_write(run.repo, "config", "-z", "--name-only", "--get-regexp",
                                  r"^remote\.", ok_codes=(0, 1))
    # `remote.<name>.<variable>` — the name may itself contain dots and slashes.
    named = code == 0 and any(key[len("remote."):].rpartition(".")[0] == url
                              for key in out.split("\0"))
    if not named and url not in (".", "..") and not any(
            sep in url for sep in (os.sep, os.altsep, "/") if sep):
        named = any(os.path.lexists(_git_path(run.repo, f"{kind}/{url}"))
                    for kind in ("remotes", "branches"))
    if named:
        raise run.refuse(
            f"the push destination {url} cannot be addressed literally: {url!r} is also the "
            f"name of a git remote here (configuration `remote.{url}.*` in any scope, or a "
            f"legacy remotes/ or branches/ file), and git resolves such a string as that "
            f"REMOTE — its own url and pushurl — not as the repository the path names. dct "
            f"pushes only to the exact approved URL: rename or remove the remote {url!r}; "
            f"for a release that is not stamped yet you may instead write the push URL "
            f"explicitly as `./{url}`, an absolute path or a real URL (scheme://…) and "
            f"prepare again — an unfinished release keeps the approved string, so there "
            f"only the remote's name can change; nothing pushed.", url=url, remote_name=url)


def push_target(run: _Run, version: str, *, push_remote: str | None = None,
                push_branch: str | None = None, push_url: str | None = None,
                no_push: bool = False) -> dict | None:
    """The ONE effective push target the owner approves with apply (K-97
    F2, K-104) — None for a project outside git or with `no_push` (an
    explicit local release). Three callers:

    - apply (a new release, K-123) passes ALL of `push_remote`,
      `push_branch`, `push_url` — it refuses an incomplete set itself, so
      nothing below is derived there: the values are only CHECKED;
    - prepare passes none: the default it SHOWS for the approval;
    - a takeover passes the owner's choice, bound by its digest.

    Default (what is not given): the current branch's UPSTREAM
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
    `push_url` must be one of the remote's push addresses (a remote
    re-pointed since the approval is refused, `push_target: null`). The URL
    must be literal (_check_literal_url: no rewrite rule, not a remote's
    name).
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
                         f"released.", remote=remote, push_target=None)
    if push_url is not None:
        if push_url not in urls:
            raise run.refuse(
                f"--push-url {push_url} is not a push URL of the remote {remote} (its push "
                f"URLs: {', '.join(urls)}) — dct pushes only to an address the remote is "
                f"configured to push to; nothing released.", remote=remote, push_urls=urls,
                push_target=None)
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
            push_target=target, push_urls=urls or [])
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


# The ONE push command of apply and resume sends the refspecs it names to the
# URL it names — git configuration cannot widen it: `--no-follow-tags`
# (K-126: push.followTags would add every annotated tag reachable from the
# release commit that the destination lacks — an unapproved ref),
# `--recurse-submodules=no` (K-134: push.recurseSubmodules / submodule.recurse
# would publish the submodules' commits to THEIR remotes — repositories
# outside the consent).
_PUSH_SCOPE = ("--atomic", "--no-follow-tags", "--recurse-submodules=no")


def _step_push(run: _Run) -> None:
    """'tagged' → 'pushed' (K-97 F2, K-104): `git push --atomic
    --no-follow-tags --recurse-submodules=no <url>
    <commit>:refs/heads/<branch> <commit>:refs/tags/<tag>` (_PUSH_SCOPE) —
    the APPROVED URL itself and explicit refspecs, so nothing the
    configuration holds or gained since (another pushurl, a push refspec, a
    mirror flag, push.followTags, push.recurseSubmodules / submodule.recurse)
    adds a recipient or a ref. Submodules are therefore NEVER pushed: a
    gitlink of the release may name a commit its submodule's remote does not
    have yet — publishing it is the owner's, dct does not check it. The
    remote side's hooks and the local pre-push hook
    run (it receives the URL as the remote's name). Verified with `git
    ls-remote <url>` — the same address. Already there → verified, not
    pushed again; PARTLY there → only the missing refspec is pushed (a
    remote branch that already contains the release commit is not pushed
    to: the release of an old commit must not move it back). A failure leaves 'tagged' — resume retries only this
    step. Pushing by URL does not move the remote-tracking ref; after the
    verified push _refresh_tracking_ref sets it to the tip the destination
    showed, when the fetch configuration confirms the mapping (K-35)."""
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
        # Only what the destination still LACKS: a branch that already
        # contains the release commit (work went on after an old release —
        # a takeover, K-119 M3 — or the branch arrived there earlier) is
        # never moved back to it, which git would reject; the tag likewise.
        refspecs = []
        if not _contains(run.repo, refs.get(f"refs/heads/{target['branch']}"), commit):
            refspecs.append(f"{commit}:refs/heads/{target['branch']}")
        if refs.get(f"refs/tags/{target['tag']}") != commit:
            refspecs.append(f"{commit}:refs/tags/{target['tag']}")
        try:
            gitinfo.git_write(run.repo, "push", *_PUSH_SCOPE, target["url"], *refspecs,
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
    _refresh_tracking_ref(run, refs, target)


def _tracking_refs(refspecs: list[str], source: str) -> tuple[list[tuple[str, bool]], bool]:
    """The local refs `git fetch` would write for the remote ref `source`
    under the remote's fetch `refspecs` → ([(destination, forced)],
    excluded): a refspec maps it when its source is `source` itself or a
    pattern with ONE `*` that matches it (the matched part substituted into
    the destination); a negative refspec (`^<src>` — no destination, exact
    or a ONE-`*` pattern) matching it excludes it. `forced` (the `+`) is
    reported only: the refresh never forces (_write_tracking_ref)."""

    def match(src: str) -> str | None:
        """The part of `source` the pattern's `*` matches ("" for an exact
        match), or None when `src` does not match."""
        if "*" not in src:
            return "" if src == source else None
        head, _, tail = src.partition("*")
        if source.startswith(head) and source.endswith(tail) \
                and len(source) >= len(head) + len(tail):
            return source[len(head):len(source) - len(tail)]
        return None

    out: list[tuple[str, bool]] = []
    excluded = False
    for spec in refspecs:
        if spec.startswith("^"):
            src = spec[1:]
            if ":" not in src and src.count("*") <= 1 and match(src) is not None:
                excluded = True
            continue
        forced = spec.startswith("+")
        src, _, dst = spec.removeprefix("+").partition(":")
        if src.count("*") > 1 or dst.count("*") != src.count("*"):
            continue
        middle = match(src)
        if middle is None:
            continue
        if dst:
            out.append((dst.replace("*", middle), forced))
    return list(dict.fromkeys(out)), excluded


def _refresh_tracking_ref(run: _Run, refs: dict[str, str], target: dict) -> None:
    """K-35 — after the VERIFIED push (by URL, which git does not record),
    the remote-tracking ref of the pushed branch is set to the branch tip
    the destination SHOWED in the push proof (`refs`, the ls-remote read
    after the push — never the release SHA: the branch may have moved on),
    so `git status` does not report the released commits as unpushed.
    Only when the mapping is CONFIRMED by the remote's FETCH configuration:
    the approved URL is the remote's only fetch URL (the repository the
    tracking ref describes), the fetch refspecs map the branch to exactly
    one local ref, that ref lies under `refs/remotes/` (a mirror refspec
    `+refs/*:refs/*` maps it onto a LOCAL branch — never written) and is
    not a SYMBOLIC ref (written through, it would move its target), the
    update is a FAST-FORWARD (no ref yet, or the ref is an ancestor of the
    tip — whatever the refspec's `+`: a `git fetch` between the push proof
    and the write may have moved it further) and the tip is an object this
    repository has. Written by ONE ref transaction (gitinfo.
    git_update_ref_cas): compare-and-swap, `no-deref`, and the symbolic-ref
    check repeated while git holds the ref's lock — never forced past a
    concurrent change, never through (nor over) a symbolic ref made
    between the check and the write. Anything else —
    a git failure (read or write) included — is a warning plus
    `details.tracking_ref` with `updated: false`, never a failed or
    unfinished release (the next `git fetch` settles it)."""
    remote, branch = target["remote"], target["branch"]
    source = f"refs/heads/{branch}"
    tip = refs.get(source)
    report: dict = {"ref": None, "updated": False, "current": False, "remote": remote,
                    "tip": tip}
    run.tracking_ref = report

    def skip(reason: str, *, warn: bool = True) -> None:
        report["reason"] = reason
        if warn:
            where = report["ref"] or f"the remote-tracking ref of {remote}/{branch}"
            run.warnings.append(f"{where} was not updated after the push: {reason} — "
                                f"`git fetch {shlex.quote(remote)}` updates it")

    try:
        _write_tracking_ref(run, target, source, tip, report, skip)
    except gitinfo.CheckoutVerificationError as exc:  # GitReadError and GitWriteError
        skip(f"git failed ({exc})")


def _write_tracking_ref(run: _Run, target: dict, source: str, tip: str | None, report: dict,
                        skip: Callable[..., None]) -> None:
    """_refresh_tracking_ref's checks and the compare-and-swap write."""
    remote = target["remote"]
    code, out = gitinfo.git_write(run.repo, "config", "--get-all", f"remote.{remote}.fetch",
                                  ok_codes=(0, 1))
    mapped, excluded = _tracking_refs(out.split() if code == 0 else [], source)
    if excluded or not mapped:
        skip("the remote's fetch refspecs track no ref for this branch", warn=False)
        return
    if len(mapped) > 1:
        skip(f"the fetch refspecs map {source} to several refs "
             f"({', '.join(d for d, _ in mapped)})")
        return
    ref = mapped[0][0]
    report["ref"] = ref
    if not ref.startswith("refs/remotes/"):
        skip(f"the fetch refspecs map {source} to {ref}, which is not a remote-tracking ref "
             f"(refs/remotes/) — dct does not write it")
        return
    code, target_ref = gitinfo.git_write(run.repo, "symbolic-ref", "-q", ref, ok_codes=(0, 1))
    if code == 0:  # update-ref / rev-parse follow a symref: never write through it
        skip(f"{ref} is a symbolic ref to {target_ref.strip()} — dct does not write "
             f"through it")
        return
    code, out = gitinfo.git_write(run.repo, "remote", "get-url", "--all", remote,
                                  ok_codes=(0, 1, 2, 128))
    fetch_urls = list(dict.fromkeys(u.strip() for u in out.splitlines() if u.strip())) \
        if code == 0 else []
    if fetch_urls != [target["url"]]:
        skip(f"the push went to {target['url']}, which is not the fetch URL of {remote} "
             f"({', '.join(fetch_urls) or 'none'}) — the ref describes that repository")
        return
    if tip is None:
        skip(f"the destination showed no {source}")
        return
    if gitinfo.git_write(run.repo, "cat-file", "-e", f"{tip}^{{commit}}",
                         ok_codes=(0, 1, 128))[0] != 0:
        skip(f"the branch tip {tip} the destination shows is not in this repository")
        return
    code, old = gitinfo.git_write(run.repo, "rev-parse", "--verify", "-q", ref,
                                  ok_codes=(0, 1))
    old = old.strip() if code == 0 else ""
    if old == tip:
        report.update(current=True, previous=old)
        return
    if old and not _contains(run.repo, tip, old):
        skip(f"{ref} ({old}) is not an ancestor of {tip} — dct only fast-forwards it, "
             f"never forces")
        return
    def still_direct() -> str | None:
        # Under the lock of the prepared transaction: a symbolic ref made
        # since the check above cannot appear any more (git holds ref.lock).
        code, now = gitinfo.git_read(run.repo, "symbolic-ref", "-q", ref, ok_codes=(0, 1))
        return (f"{ref} became a symbolic ref to {now.strip()} meanwhile — dct does not write "
                f"through it, nor replace it") if code == 0 else None

    try:
        # ONE ref transaction: compare-and-swap, `no-deref` (a symbolic ref
        # made between the check and the write is locked itself — its
        # target never moves) and the check repeated under the lock (the
        # symbolic ref is not replaced by a direct one either).
        gitinfo.git_update_ref_cas(run.repo, ref, tip, old or "0" * len(tip),
                                   message=f"dct release: push {run.version}", no_deref=True,
                                   verify=still_direct)
    except gitinfo.GitRefVerifyError as exc:
        skip(exc.reason)
        return
    except gitinfo.GitWriteError as exc:  # GitRefRaceError included
        skip(f"`git update-ref` refused ({exc}) — the ref changed meanwhile?")
        return
    report.update(updated=True, current=True, previous=old or None)


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
    _refresh_tracking_ref(run, refs, target)


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
    if run.tracking_ref is not None:
        details["tracking_ref"] = run.tracking_ref
    return result(action, run.slug, outcome, version=run.record["version"] if run.record else None,
                  release=run.record, steps=run.steps, warnings=run.warnings, details=details)


# ── public API ───────────────────────────────────────────────────────────────


class NothingToPrepare(ReleaseRefused):
    """prepare has nothing to prepare: the version already has a record, or
    it is lower than the current version (releases never rewind)."""


def _check_version_available(run: _Run, target: str) -> None:
    """A version prepare / the preview may still release: no release record
    of it (`version_taken: {version, status}`, the record as `release`) and
    not lower than the current version (`version_lower: {version,
    current}`) — NothingToPrepare with the machine key a dialog routes on
    ("pick another version"), the data its sentence needs in it."""
    recorded = ledger.get_release(run.engine, run.slug, target)
    if recorded is not None:
        raise NothingToPrepare(
            f"{target} already has a release record ({recorded['status']}) — choose another "
            f"version; nothing to prepare.", release=recorded,
            details={"version_taken": {"version": target, "status": recorded["status"]}})
    current = run.project.get("current_version")
    if current and not _newer_than_current(run.project, target) \
            and not same_version(current, target):
        raise NothingToPrepare(
            f"{target} is lower than the current version {current} — releases never rewind; "
            f"choose a higher version; nothing to prepare.",
            details={"version_lower": {"version": target, "current": current}})


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
    record must carry the owner's consent (stamped by `dct release apply`,
    or recorded by the takeover; core.releases.progress_owner). Anything
    else — an 'own' record, one stamped outside the effects layer (MCP
    release_changelog, older records, a partial reconstruction: no approved
    proposal, no push target) — is refused before any effect, never
    finished without a push; a 'dct' record without a consent is told both
    ways to finish it (K-119 M3: `dct release takeover`, or the ledger
    path) — never just "do it by hand or abandon"."""
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
            f"dct performs no effect AUTOMATICALLY on it. Two ways to finish it: "
            f"(1) hand it to dct — `dct release takeover --version {record['version']} "
            f"[--push-remote … | --no-push]` verifies its artifacts, shows the consent it "
            f"would record and, once you confirm its digest, lets `dct release resume` "
            f"perform only the missing effects (no re-stamp); (2) the ledger path — perform "
            f"them yourself (or with the project's own tool) and record the verified progress "
            f"(`dct clog release-advance`, MCP advance_release). Or abandon it; nothing done.",
            executor=executor, consent=None)


def _new_release_run(engine: sa.Engine, project_slug: str, located: tuple, *,
                     on_point=None) -> _Run:
    """The first pre-checks of a NEW release (prepare, the preview, apply)
    → its run: an executor that is not 'own', a registered path, no
    unfinished record."""
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
    return run


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
    run = _new_release_run(engine, project_slug, located, on_point=on_point)
    checkout = run.checkout
    pre_untracked: set[str] = set()
    if checkout:
        _check_git_version(checkout["repo_root"])
        state = _clean_tree(run, planning._artifacts(
            run.changelog_rel, ledger.get_version_files(engine, project_slug)["version_files"]))
        pre_untracked = set(state["untracked"])
    _check_changelog_file(run)
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
    # K-36: the plan's request to confirm the range base is answered by the
    # digest that binds it — prepare and apply report the base as information.
    asks = planning.range_notice(plan["range"], plan["version"]["current"])
    run.warnings.extend(
        planning.range_notice(plan["range"], plan["version"]["current"], bound=True)
        if asks is not None and warning == asks else warning for warning in plan["warnings"])
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
    if stored.get("replaced_preview") is not None:  # a prepare replaces the preview slot
        run.warnings.extend(release_preview_resources(
            run.engine, run.slug, stored["replaced_preview"], run.repo))
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
    already recorded — `version_taken: {version, status}` —, or lower than
    the current version — `version_lower: {version, current}`), SyncFailed (the
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
    _check_version_available(run, target)
    _new_release_prechecks(run, target, ids, plan)
    details = _prepared(run, target, plan)
    try:  # what apply would push to by default — shown for the approval (read-only)
        default_push = push_target(run, target)
    except ReleaseRefused as exc:
        default_push = None
        run.warnings.append(str(exc))
    return result("prepare", project_slug, "prepared", version=target, steps=run.steps,
                  warnings=run.warnings,
                  details={**details, "entry_ids": ids, "push_target": default_push})


# ── the preview: one screen, consent bound to the release content ────────────
#
# `dct release prepare --preview` (the one-dialog release) computes what a
# release WOULD be — on SYNTHETIC commits, never referenced by a ref: C* =
# the work-in-progress commit of the uncommitted changes (its tree T1 built
# in a TEMPORARY index, the user's index never opened for writing), A* = an
# attestation commit on it — and, for each variant it offers ("shown", and
# "all": every waiting entry attested too), the CONSENT VIEW (consent_view)
# whose digest C the owner approves with `apply --consent C`. The variants
# are stored in the project's preview slot (core.releases.set_preview); the
# blobs they name are pinned by PREVIEW_PIN_PREFIX/<project>/<preview id>
# (a tree, like a proposal's pin). Nothing is committed, staged or tagged.

PREVIEW_PIN_PREFIX = "refs/dct/release-previews"
# The "with all N" variant is offered for 1..ALL_VARIANT_MAX_WAITING waiting
# entries, none of them reverted.
ALL_VARIANT_MAX_WAITING = 4
# The identity of the synthetic commits (never published).
PREVIEW_IDENTITY = ("dct preview", "preview@dct.invalid")
_REVERT_REASONS = ("reverted", "revert_unconfirmed")


def preview_pin_ref(slug: str, preview_id: str) -> str:
    """The ref that pins the blobs of the preview `preview_id`."""
    return f"{PREVIEW_PIN_PREFIX}/{_project_ref_key(slug)}/{preview_id}"


def _pin_preview(repo: str, ref: str, contents: dict[str, dict], work_tree: str | None) -> None:
    """Make every blob the preview's variants name reachable — the tree of
    their _proposal_blobs, each variant under its OWN name (`contents`:
    {variant name: proposal content} → `<name>/expected/<path>`,
    `<name>/base/<path>`, `<name>/changelog-base/<path>`: two variants with
    other versions hold other blobs for the same path, and neither may push
    the other's out) and, under `work/`, the future tree T1 — and point
    `ref` at it (a TREE: `git log --all` never shows it)."""
    blobs = {f"{name}/{path}": blob for name, content in contents.items()
             for path, blob in _proposal_blobs(content).items()}
    with tempfile.TemporaryDirectory(prefix="dct-release-pin-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", "--empty", index_file=index)
        if work_tree:
            gitinfo.git_write(repo, "read-tree", "--prefix=work/", work_tree, index_file=index)
        if blobs:
            entries = [arg for path in sorted(blobs)
                       for arg in ("--cacheinfo", f"100644,{blobs[path]},{path}")]
            gitinfo.git_write(repo, "update-index", "--add", *entries, index_file=index)
        _, tree = gitinfo.git_write(repo, "write-tree", index_file=index)
    gitinfo.git_write(repo, "update-ref", "-m", "dct release: pin preview blobs", ref,
                      tree.strip())


def _unpin(repo: str | None, ref: str | None, what: str) -> list[str]:
    if not ref or not repo or not os.path.isdir(repo):
        return []
    try:
        if _rev(repo, ref) is not None:
            gitinfo.git_write(repo, "update-ref", "-m", f"dct release: unpin {what}", "-d", ref)
    except gitinfo.CheckoutVerificationError as exc:
        return [(f"the pin {ref} was not removed ({exc}) — remove it yourself: "
                 f"`git update-ref -d {ref}`")]
    return []


def release_preview_resources(engine: sa.Engine, slug: str, preview: dict | None,
                              repo: str | None) -> list[str]:
    """Let go of what was kept for a preview slot that is no longer live
    (replaced, discarded): its pin ref (git) and the store blobs of its
    variants that no live proposal or preview still names. Best effort →
    warnings (never raises)."""
    if not preview:
        return []
    warnings = _unpin(repo, preview.get("pin"), "preview blobs")
    for item in preview["variants"].values():
        warnings.extend(release_proposal_resources(engine, slug, item["proposal_content"], repo))
    return warnings


def _ls_tree_entries(repo: str, tree: str) -> dict[str, tuple[str, str]]:
    """{path: (mode, object)} of every entry of `tree`, recursively."""
    _, out = gitinfo.git_read(repo, "ls-tree", "-r", "-z", tree)
    found = {}
    for entry in (e for e in out.split("\0") if e):
        meta, _, rel = entry.partition("\t")
        mode, _kind, obj = meta.split()
        found[rel] = (mode, obj.lower())
    return found


def _work_file_digest(path: str) -> str | None:
    """sha256 of the work-tree bytes at `path` (a symlink: of its target),
    None when nothing is there."""
    if os.path.islink(path):
        return _sha256(os.readlink(path).encode("utf-8", "surrogateescape"))
    data = _read_bytes(path) if os.path.isfile(path) else None
    return None if data is None else _sha256(data)


def _work_tree_dirt(repo: str, artifacts: Iterable[str]) -> dict[str, str | None]:
    """What the work-tree checks of `apply --consent` watch (read-only):
    every tracked path with a change (index or work tree), every conflicted
    path and every untracked RELEASE ARTIFACT (`artifacts`, repo-relative)
    → {path: _work_file_digest | None}. Untracked files that are no release
    artifact (a hook's log, build output) are not watched."""
    status = planning.status_v2(repo)
    paths = set(status["tracked"]) | set(status["conflicted"]) \
        | (set(status["untracked"]) & set(artifacts))
    return {rel: _work_file_digest(os.path.join(repo, *rel.split("/"))) for rel in sorted(paths)}


_CLEAN = object()  # a path absent from _work_tree_dirt: as the commit has it


def _dirt_changed(before: dict, after: dict, skip: Iterable[str] = ()) -> list[str]:
    """The paths whose _work_tree_dirt entry differs between two readings
    (dirty in only one, or other content), minus `skip`."""
    skipped = set(skip)
    return sorted(rel for rel in set(before) | set(after)
                  if rel not in skipped and before.get(rel, _CLEAN) != after.get(rel, _CLEAN))


def _synthetic_commit(repo: str, tree: str, parent: str, message: str) -> str:
    """An UNREFERENCED commit object of `tree` on `parent` with `message`
    verbatim — the preview identity, dated NOW (an approximate `--since`
    range includes it), never signed, no hook."""
    now = f"@{int(datetime.now(UTC).timestamp())} +0000"
    name, email = PREVIEW_IDENTITY
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": now,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": now}
    with tempfile.TemporaryDirectory(prefix="dct-release-preview-") as tmp:
        path = os.path.join(tmp, "message")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(message)
        _, out = gitinfo.git_write(repo, "-c", "commit.gpgSign=false", "commit-tree",
                                   "--no-gpg-sign", tree, "-p", parent, "-F", path, env=env)
    return out.strip().lower()


def _alpha(proof: dict, synthetic: dict[str, str]) -> dict:
    """α — the proof map with every full or 12-character id of a SYNTHETIC
    commit replaced by its placeholder (`@work`, `@attest`): the real
    commits of apply have other ids (dates, identity, signature) but the
    same role."""
    def sub(value):
        if isinstance(value, str):
            for sha, name in synthetic.items():
                if value == sha or value == sha[:12]:
                    return name
        return value

    out = {}
    for key, item in proof.items():
        item = dict(item)
        item["commits"] = [sub(c) for c in item.get("commits") or []]
        if "merge_commit" in item:
            item["merge_commit"] = sub(item["merge_commit"])
        if item.get("associations"):
            item["associations"] = [{**a, "merge_commit": sub(a.get("merge_commit"))}
                                    for a in item["associations"]]
        out[key] = item
    return out


def _repo_relative(repo: str, cwd: str | None, path: str) -> str | None:
    """`path` (relative to `cwd` — the caller's directory, as `git status`
    shows paths — or absolute) as a repository-relative path; None when it
    lies outside the repository. The parent directory is resolved (symlinked
    temp dirs), the last component never (an untracked symlink stays one)."""
    full = os.path.normpath(os.path.join(os.path.realpath(cwd or os.getcwd()), path))
    full = os.path.join(os.path.realpath(os.path.dirname(full)), os.path.basename(full))
    rel = os.path.relpath(full, os.path.realpath(repo))
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep) \
            or os.path.isabs(rel):
        return None
    return _rel(rel)


def _preview_bytes(work: dict, variants: int) -> int:
    """A lower bound of the work part of a preview slot in canonical JSON:
    the index entries and work-tree digests once, the bound paths once per
    variant — what PREVIEW_MAX_BYTES must hold before anything is synced."""
    def size(value) -> int:
        return len(ledger._canonical(value).encode("utf-8"))

    return (size(work["index_before"]) + size(work["paths_state"]) + size(work["message"])
            + variants * size(work["view_paths"]))


def _work_state(run: _Run, include_untracked: list[str], message_file: str | None,
                head: str, *, cwd: str | None = None) -> dict | None:
    """S0 of the preview (read-only) and the work-in-progress content: the
    staging scope Pₛ (planning.work_scope: every tracked changed path, the
    untracked release artifacts, `include_untracked` — relative to `cwd`),
    its refusals (a `filter` attribute: planning.filter_attributed; a
    secret-like path: planning.secret_like), the future tree T1 built in a
    TEMPORARY index (planning.future_tree — the ONE builder, also the
    plan's changelog check, planning.work_changelog; T1 == HEAD's
    tree — the index differs, the files do not — is refused with
    `index_only: {paths}`), the cleaned message and the synthetic commit
    C*. None for a clean tree (nothing to commit)."""
    repo = run.repo
    busy = _operation_in_progress(repo)
    if busy:
        raise run.refuse(f"a git operation is in progress ({', '.join(busy)}) — finish or "
                         f"abort it first; nothing prepared.", in_progress=busy)
    status = planning.status_v2(repo)
    if status["conflicted"]:
        raise run.refuse(f"conflicted path(s): {', '.join(status['conflicted'])} — resolve "
                         f"them first; nothing prepared.", conflicted=status["conflicted"])
    tracked = status["tracked"]
    planned = sorted(p for p, e in tracked.items() if e["xy"] == ".A")
    if planned:
        raise run.refuse(
            f"intent-to-add entries ({', '.join(planned)}: `git add -N`) — stage them fully "
            f"(`git add`) or drop them (`git rm --cached`) first; nothing prepared.",
            intent_to_add=planned)
    subs = sorted(p for p, e in tracked.items() if e["sub"] != "N...")
    if subs:
        raise run.refuse(
            f"submodule change(s): {', '.join(subs)} — a changed gitlink or a dirty submodule "
            f"is never committed by dct; commit or reset it yourself first; nothing prepared.",
            submodule=subs)
    untracked = set(status["untracked"])
    bad = []
    resolved = set()
    for given in include_untracked:
        rel = _repo_relative(repo, cwd, given)
        if rel is None:
            bad.append({"path": given, "reason": "outside"})
        else:
            resolved.add(rel)
    included = sorted(resolved)
    for rel in included:
        if rel in untracked:
            continue
        full = os.path.join(repo, *rel.split("/"))
        if not os.path.lexists(full):
            reason = "missing"
        elif gitinfo.git_read(repo, "--literal-pathspecs", "ls-files", "--error-unmatch", "--",
                              rel, ok_codes=(0, 1))[0] == 0:
            reason = "tracked"
        else:
            reason = "ignored"
        bad.append({"path": rel, "reason": reason})
    if bad:
        listing = ", ".join(f"{b['path']} ({b['reason']})" for b in bad)
        raise run.refuse(
            f"--include-untracked names files that are not untracked: {listing} — only an "
            f"untracked, not ignored file inside the repository can be added (paths are "
            f"relative to the current directory); nothing prepared.", bad_untracked=bad)
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    scope, auto = planning.work_scope(status, run.changelog_rel, contract, included)
    if scope:
        _, flags = gitinfo.git_read(repo, "--literal-pathspecs", "ls-files", "-v", "-z", "--",
                                    *scope)
        hidden = sorted({entry[2:] for entry in flags.split("\0") if entry
                         and (entry[0] == "S" or entry[0].islower())} & set(scope))
        if hidden:
            raise run.refuse(
                f"{', '.join(hidden)}: skip-worktree / assume-unchanged is set in your index — "
                f"what git would commit there is not what the work tree shows; clear the flag "
                f"(`git update-index --no-skip-worktree / --no-assume-unchanged`) first; "
                f"nothing prepared.", hidden_index_flags=hidden)
        filtered = planning.filter_attributed(repo, scope)
        if filtered:
            raise run.refuse(
                f"{', '.join(filtered)}: a `filter` attribute (LFS, clean / smudge) — "
                f"what such a filter stores cannot be previewed safely; commit these files "
                f"yourself first; nothing prepared.", filtered_paths=filtered)
        secrets = [p for p in scope if planning.secret_like(p)]
        if secrets:
            raise run.refuse(
                f"{', '.join(secrets)} look(s) like secrets (.env, keys, credentials) — a "
                f"work-in-progress commit never takes them along; move them out of the "
                f"change (or commit them yourself), then preview again; nothing prepared.",
                secret_like=secrets)
    if scope:
        # Before asking for a message: an index-only difference has nothing
        # to commit, so a message would only be asked for and then refused.
        h0_entries = planning.tree_entries(repo, head, scope)
        index_before = _index_entries(repo, scope)
        tree = planning.future_tree(repo, head, scope, timeout=PREPARE_GIT_TIMEOUT_SECONDS)
        if tree == _tree(repo, head):
            # The index differs from HEAD, the work tree does not: a work commit
            # would be EMPTY (and released with the message's proven ids).
            staged = sorted(p for p in scope if index_before.get(p) != h0_entries.get(p)) or scope
            raise run.refuse(
                f"your index differs from HEAD but the files are unchanged ({', '.join(staged)}) "
                f"— there is nothing to commit: unstage them (`git restore --staged <path>`) or "
                f"make the change; nothing prepared.", index_only={"paths": staged})
    if scope and message_file is None:
        raise run.refuse(
            "the work tree has uncommitted changes — the release commits them first and needs "
            "their commit message: --message-file <file>; nothing prepared.",
            message_required={"paths": sorted(set(tracked) | set(auto)),
                              "untracked": sorted(untracked)})
    if not scope:
        if message_file is not None:
            raise run.refuse("--message-file was given but there is nothing to commit (the "
                             "work tree is clean); nothing prepared.", nothing_to_commit=True)
        return None
    raw = _read_bytes(message_file) if os.path.isfile(message_file) else None
    if raw is None:
        raise run.refuse(f"the message file {message_file} cannot be read; nothing prepared.",
                         message_file=message_file)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise run.refuse(f"the message file {message_file} is not UTF-8; nothing prepared.",
                         message_file=message_file) from None
    with tempfile.TemporaryDirectory(prefix="dct-release-preview-") as tmp:
        msg = os.path.join(tmp, "message")
        with open(msg, "wb") as fh:
            fh.write(raw)
        message = _cleaned_message(run.refuse, repo, msg)
    # git's own subject (%s: the first paragraph, joined) — the one the
    # real commit shows, R8 checks and adoption compares.
    subject = message_subject(message)
    if not subject.strip():
        raise run.refuse("the commit message is empty after git's cleanup (commit.cleanup) — "
                         "write a subject line; nothing prepared.", message_empty=True)
    commit = _synthetic_commit(repo, tree, head, message)
    proves = sorted(proven_ids(planning.read_commits(repo, "-1", commit)[0]))
    unreleased = {e["id"] for e in list_entries(run.engine, run.slug, unreleased_only=True)}
    foreign = [i for i in proves if i not in unreleased]
    if foreign:
        cited = planning._foreign_citations(run.engine, planning._project_id(run.engine, run.slug),
                                            {i: [] for i in foreign})
        raise run.refuse(
            f"the message names {', '.join(f'#{i}' for i in foreign)}, which are not "
            f"unreleased changelog entries of '{run.slug}' — a commit may prove only this "
            f"project's unreleased entries; fix the message; nothing prepared.",
            message_foreign_ids=cited)
    candidates = planning.commit_candidates(run.engine, run.slug, cwd=repo)
    uncertain = sorted({e["id"] for e in candidates["uncertain"]} - set(proves))
    t1 = planning.tree_entries(repo, tree, scope)
    paths = []
    for rel in scope:
        old, new = h0_entries.get(rel), t1.get(rel)
        if old == new:
            continue  # the index holds a change the work tree takes back
        change = ("added" if old is None else "deleted" if new is None
                  else "mode" if old[1] == new[1] else "modified")
        before = index_before.get(rel)
        staged = "all" if before == new else "none" if before == old else "part"
        paths.append({"path": rel, "change": change, "staged": staged, "auto": rel in auto})
    return {
        "scope": scope, "auto": auto, "included": sorted(set(included) | set(auto)),
        "left": sorted(untracked - set(included) - set(auto)), "tree": tree,
        "message": message, "subject": subject, "proves": proves, "uncertain": uncertain,
        "commit": commit, "paths": paths,
        "view_paths": {rel: None if rel not in t1 else {"mode": t1[rel][0], "blob": t1[rel][1]}
                       for rel in scope},
        "index_before": {rel: None if rel not in index_before else dict(
            zip(("mode", "blob", "stage"), index_before[rel], strict=True)) for rel in scope},
        "paths_state": {rel: _work_file_digest(os.path.join(repo, *rel.split("/")))
                        for rel in scope},
    }


def consent_view(run: _Run, *, plan: dict, target: str, ids: list[int], base: dict,
                 work: dict | None, attest: list[int] | None, candidate_tree: str | None,
                 composition: dict, content: dict, push: dict | None,
                 synthetic: dict[str, str]) -> dict:
    """The CONSENT VIEW of one variant (core.releases.validate_consent_view)
    — the ONE builder of the content the owner approves, from what the
    caller derived: the plan at the variant's candidate, the selection, the
    base, the work-in-progress content, the attested ids, the composed
    changelog, the proposal content and the push target. Ids of synthetic
    commits in the proof map are replaced by `@work` / `@attest` (α)."""
    snapshot = plan["snapshot"]
    rng = snapshot["range"]
    notice = None
    if rng is not None:
        notice = ("base_not_current" if (plan["range"] or {}).get("confirm_required")
                  else "approximate" if rng["approximate"] else None)
    unproven = {e["id"]: e["reason"] for e in plan["entries"]["unproven"]}
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    # The section, its UTC date and the render are bound whether or not dct
    # writes the changelog file: the screen shows the dated section either
    # way, and the record and the export carry them.
    managed = composition["composed"] is not None
    changelog = {
        "managed": managed, "path": run.changelog_rel if managed else None,
        "base_blob": (content.get("changelog") or {}).get("base_blob") if managed else None,
        "composed_sha256": _sha256(composition["composed"]) if managed else None,
        "section_sha256": _sha256(composition["section"].encode("utf-8")),
        "date": composition["date"], "pr_refs": composition["pr_refs"],
    }
    view = {
        "format": ledger.CONSENT_VIEW_FORMAT, "project": run.slug,
        "executor": ledger.DEFAULT_EXECUTOR, "release_mode": run.mode,
        "checkout_root": snapshot["checkout_root"], "project_root": snapshot["project_root"],
        "version": {"current": plan["version"]["current"], "target": target},
        "base": base,
        "work": None if work is None else {
            "tree": work["tree"], "subject": work["subject"],
            "message_sha256": _sha256(work["message"].encode("utf-8")),
            "proves": work["proves"], "paths": work["view_paths"]},
        "attest": {"ids": attest} if attest else None,
        "candidate_tree": candidate_tree,
        "range": None if rng is None else {
            "base_tag": rng["base_tag"], "base_sha": rng["base_sha"],
            "approximate": rng["approximate"], "since": rng["since"], "notice_code": notice},
        "entries": {"selected": ids, "state": snapshot["entries"],
                    "waiting": {str(i): r for i, r in sorted(unproven.items()) if i not in ids},
                    "proof": _alpha(snapshot["proof"], synthetic)},
        "changelog": changelog,
        "version_files": {
            "paths": content["paths"], "base_blobs": content["base_blobs"],
            "expected_blobs": content["expected_blobs"], "script": content["script"],
            "contract": {"confirmed": contract, "change": contract != content["paths"]}},
        "push": push,
    }
    return ledger.validate_consent_view(planning._jsonable(view))


def _local_push_reason(repo: str, exc: ReleaseRefused, branch: str | None) -> str:
    """Why the default push target was refused — the view's local reason
    (core.releases.LOCAL_PUSH_REASONS): `upstream_invalid` when the branch
    HAS an upstream (`branch.<b>.remote`, not `.`) that cannot be pushed to
    (its remote is missing, its branch name invalid) — never shown as "no
    upstream"."""
    details = exc.details
    if "rewrite_rule" in details or "remote_name" in details:
        return "url_not_literal"
    if len(details.get("push_urls") or ()) > 1:
        return "several_urls"
    if branch is None:
        return "detached"
    remote = _config(repo, f"branch.{branch}.remote")
    return "upstream_invalid" if remote and remote != "." else "no_upstream"


class _Preview:
    """What every variant of one preview shares."""

    def __init__(self, run: _Run, *, version, bump_type, explicit: str | None, taken,
                 base: dict, work: dict | None, push: dict | None, local: str | None,
                 cwd: str | None):
        self.run, self.version, self.bump_type, self.explicit = run, version, bump_type, explicit
        self.cwd = cwd
        self.taken, self.base, self.work, self.push, self.local = taken, base, work, push, local
        self.checkout = run.checkout
        self.c_star = work["commit"] if work else (base["head"])
        self.tree = work["tree"] if work else (_tree(run.repo, base["head"]) if run.repo
                                                else None)

    def plan_at(self, head: str | None, entry_ids, no_entries: bool) -> dict:
        run = self.run
        if run.repo:
            run.checkout = {**self.checkout, "head": head}
            run.bind_changelog(head)
            _check_changelog_file(run)
        try:
            return planning.build_plan(
                run.engine, run.slug, version=self.version, bump_type=self.bump_type,
                cwd=self.cwd,
                entry_ids=entry_ids, no_entries=no_entries, pinned_checkout=run.checkout)
        except ValueError as exc:
            raise run.refuse(str(exc)) from None

    def range_text(self, text: str | None, head: str | None, attest) -> str | None:
        """The range as the screen shows it: a synthetic candidate is on no
        branch and never exists — its id is replaced by what it stands for."""
        if not text or head is None or head == self.base["head"]:
            return text
        end = "HEAD" + (" + your uncommitted changes" if self.work is not None else "") \
            + (" + the attestation commit" if attest else "")
        return text.replace(head, end)

    def variant(self, name: str, *, head: str | None, attest: list[int] | None,
                entry_ids, no_entries: bool, reuse: dict | None = None) -> dict:
        """One variant at the synthetic candidate `head` → {name, consent,
        consent_digest, content, suggested, display, plan, ids, target}."""
        run = self.run
        plan = self.plan_at(head, entry_ids, no_entries)
        proven = {e["id"] for e in plan["entries"]["proven"]}
        proven |= {e["id"] for e in plan["entries"]["unproven"] if e["reason"] == "no_git"}
        if no_entries:
            ids: list[int] = []
        elif entry_ids is None:
            ids = sorted(proven)
        else:
            ids = _entry_selection(run.slug, plan, "", entry_ids, False, False)
        views = {e["id"]: e for e in plan["entries"]["proven"] + plan["entries"]["unproven"]}
        suggested = planning.suggest_version(
            plan["version"]["current"], {views[i]["category"] for i in ids if i in views},
            (plan["range"] or {}).get("base_version"), self.taken)
        target = self.explicit or suggested["target"]
        if target is None:
            raise run.refuse(
                "no version can be suggested (the current version "
                f"{plan['version']['current']!r} is not a version dct reads, or every "
                "candidate is taken) — pass --version V; nothing prepared.",
                version_unresolved=suggested)
        record = _provisional_record(run, target, ids, plan)
        section, date = render_section(run.engine, run.slug, record, provisional=True)
        composed = _check_changelog_content(run, target, ids, plan, record=record)
        if run.repo:
            _check_local_tags(run, target)
        if reuse is not None and same_version(reuse["target"], target):
            content, report = reuse["content"], reuse["report"]
        else:
            proposal, report = _build_proposal(run, target, plan["snapshot_digest"])
            content = ledger.proposal_content(proposal)
        push = None
        if run.repo:
            push = ({"mode": "local", "reason": self.local} if self.push is None
                    else {"mode": "push", **self.push, "tag": f"v{target}"})
        synthetic = {}
        if self.work is not None:
            synthetic[self.work["commit"]] = "@work"
        if attest:
            synthetic[head] = "@attest"
        view = consent_view(
            run, plan=plan, target=target, ids=ids, base=self.base, work=self.work,
            attest=attest, candidate_tree=self.tree, push=push, synthetic=synthetic,
            composition={"composed": composed, "section": section, "date": date,
                         "pr_refs": record["render"]["pr_refs"]},
            content=content)
        contract = view["version_files"]["contract"]["confirmed"]
        rng = plan["range"]
        waiting = [{"id": e["id"], "category": e["category"], "entry": e["entry"],
                    "reason": e["reason"],
                    **({"reverted_by": e["reverted_by"]} if "reverted_by" in e else {})}
                   for e in plan["entries"]["unproven"] if e["id"] not in ids]
        display = {
            "entries": {"selected": [{"id": i, "category": views[i]["category"],
                                      "entry": views[i]["entry"],
                                      "component": views[i].get("component")}
                                     for i in ids if i in views],
                        "waiting": waiting},
            "section": section, "checks": report["checks"],
            "contract": {"confirmed": contract, "proposed": content["paths"],
                         "change": contract != content["paths"]},
            "sync": report["sync"],
            "range": {"text": None if rng is None else self.range_text(rng["display"], head,
                                                                         attest),
                      "approximate": bool(rng and rng["approximate"]),
                      "notice": planning.range_notice(rng, plan["version"]["current"],
                                                      bound=True)},
            "delta": None, "warnings": list(plan["warnings"]),
        }
        return {"name": name, "consent": view, "consent_digest": ledger.consent_view_digest(view),
                "content": content, "report": report, "suggested": suggested,
                "display": display, "plan": plan, "ids": ids, "target": target,
                "version_only": not ids}


@_conflict_boundary("prepare")
def preview_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    version: str | None = None,
    bump_type: str | None = None,
    entry_ids: Iterable[int] | None = None,
    no_entries: bool = False,
    attest_ids: Iterable[int] | None = None,
    message_file: str | None = None,
    include_untracked: Iterable[str] = (),
    push_remote: str | None = None,
    push_branch: str | None = None,
    push_url: str | None = None,
    no_push: bool = False,
    variants: bool = True,
    cwd: str | None = None,
) -> dict:
    """`dct release prepare --preview` — the one-dialog release's screen
    (docs/release-workflow.md, "The preview"). Under the operation lock
    (action 'prepare'): the pre-checks of a new release; the work-in-progress
    content of a dirty tree (_work_state — `message_file` REQUIRED iff the
    tree is dirty; `include_untracked` adds untracked files); synthetic
    commits C* (the work) and A* (`attest_ids` attested), never referenced;
    the plan at the candidate, the suggested version (`version` /
    `bump_type` choose one instead; a remote tag of a SUGGESTED version
    moves it, one of an explicit version refuses), the changelog
    composition, the isolated version sync, the push target (`push_*` /
    `no_push`; default: the upstream — read-only, an unreachable remote is
    a warning); then the "with all N" variant (unless `variants` is False).
    Each variant's consent view and digest C; the variants are stored in
    the preview slot (core.releases.set_preview — clearing the pending
    proposal) and their blobs pinned (preview_pin_ref). Nothing is
    committed, staged, tagged or pushed.

    Returns result() with outcome 'preview', version = the shown variant's
    and details {preview_id, base, work, remote, left_out, variants,
    all_unavailable}; `left_out` — display only, in no consent view — lists
    every untracked, not ignored file the release does not commit, also
    when `work` is null: [{path, secret_like (the secret-path label; such a
    file is never offered)}]. Refusals: ReleaseRefused with the details keys
    of the contract (nothing stored, the earlier slot kept) — among them
    `version_invalid: {version}` (an unparseable `version`) and
    `attest_without_entries: {attest}` (`attest_ids` with `no_entries`:
    attesting entries no release takes has no screen) —, NothingToPrepare
    (`version_taken: {version, status}` — V already has a record;
    `version_lower: {version, current}`), SyncFailed."""
    attest = sorted(set(attest_ids or ()))
    located = _locate(engine, project_slug, cwd)
    with _operation(located[0], located[1], "prepare"):
        return _preview(engine, project_slug, located, version=version, bump_type=bump_type,
                        entry_ids=None if entry_ids is None else sorted(set(entry_ids)),
                        no_entries=no_entries, attest=attest, message_file=message_file,
                        include_untracked=list(include_untracked), push_remote=push_remote,
                        push_branch=push_branch, push_url=push_url, no_push=no_push,
                        variants=variants, cwd=cwd)


def _preview(engine, project_slug, located, *, version, bump_type, entry_ids, no_entries,
             attest, message_file, include_untracked, push_remote, push_branch, push_url,
             no_push, variants, cwd) -> dict:
    if entry_ids is not None and no_entries:
        raise ReleaseRefused("--entries and --no-entries are mutually exclusive")
    run = _new_release_run(engine, project_slug, located)
    repo = run.repo
    if attest and no_entries:
        # An attestation commit marks waiting entries as done SO THAT they
        # are released; a release of no entry has no such screen.
        raise run.refuse(
            f"--attest {', '.join(f'#{i}' for i in attest)} marks entries as done so they are "
            f"released, but --no-entries releases none — pass the entries to release with "
            f"--entries (or drop --attest); nothing prepared.",
            attest_without_entries={"attest": list(attest)})
    if repo is None and (attest or message_file or include_untracked):
        raise run.refuse(
            "the project is outside git — there is nothing to attest or commit "
            "(--attest / --message-file / --include-untracked); nothing prepared.",
            no_git=True)
    if repo:
        _check_git_version(repo)
    explicit = None
    if version or bump_type:
        try:
            explicit = planning._target_version(run.project.get("current_version"), version,
                                                bump_type)
        except ValueError as exc:
            if version:
                raise run.refuse(f"{exc}; nothing prepared.",
                                 version_invalid={"version": version}) from None
            raise run.refuse(str(exc)) from None
        _check_version_available(run, explicit)
        if repo:
            _check_local_tags(run, explicit)
    checkout = run.checkout
    head = checkout["head"] if checkout else None
    ref = _head_ref(repo) if repo else None
    before = _git_state(repo) if repo else None
    if repo and (before["head"] != head or _head_ref(repo) != ref):
        # HEAD (read by _locate, before the lock) moved or the branch was
        # switched since: H0 would be stale — nothing is built on it.
        raise run.refuse(
            f"your checkout changed while the preview started: HEAD is {before['head']}, not "
            f"{head}, or the branch HEAD names changed — preview again once it is settled; "
            f"nothing stored.", checkout_changed={"head": {"before": head,
                                                           "after": before["head"]}})
    work = _work_state(run, include_untracked, message_file, head, cwd=cwd) if repo else None
    if work is not None:
        needed = _preview_bytes(work, 2 if variants else 1)
        if needed > ledger.PREVIEW_MAX_BYTES:
            raise run.refuse(
                f"the uncommitted change is too large for one preview ({len(work['scope'])} "
                f"paths need at least {needed} bytes of the preview's "
                f"{ledger.PREVIEW_MAX_BYTES}) — commit part of it yourself first (or preview "
                f"with --no-variants); nothing synced, nothing stored.",
                preview_too_large={"bytes": needed, "max": ledger.PREVIEW_MAX_BYTES,
                                   "paths": len(work["scope"])})
    if repo:
        run.step("preview_tree", "done", tree=work["tree"] if work else _tree(repo, head))
    else:
        run.step("preview_tree", "skipped", reason="outside git")
    # Display only, outside every consent view: the untracked (not ignored)
    # files the release leaves out — also when there is no work to commit —
    # labelled by the secret-path policy; never added to anything.
    left = (work["left"] if work is not None
            else sorted(planning.status_v2(repo)["untracked"]) if repo else [])
    left_out = [{"path": rel, "secret_like": planning.secret_like(rel)} for rel in left]

    # The push target and the remote (read-only; one ls-remote).
    remote = {"checked": False, "url": None, "tags": [], "branch_ahead": False, "error": None}
    push, local, remote_versions = None, None, {}
    if repo and no_push:
        push_target(run, "0", no_push=True, push_remote=push_remote, push_branch=push_branch,
                    push_url=push_url)
        local = "requested"
    elif repo:
        chosen = bool(push_remote or push_branch or push_url)
        try:
            found = push_target(run, "0", push_remote=push_remote, push_branch=push_branch,
                                push_url=push_url)
        except ReleaseRefused as exc:
            if chosen:
                raise
            local = _local_push_reason(repo, exc, planning._current_branch(repo))
            run.warnings.append(f"not pushed by default: {exc}")
        else:
            push = {k: found[k] for k in ("remote", "branch", "url")}
            remote["url"] = push["url"]
            try:
                refs = _ls_remote(repo, push["url"])
            except ReleaseRefused as exc:
                remote["error"] = str(exc)
                run.warnings.append(
                    f"the remote {push['remote']} ({push['url']}) could not be read now "
                    f"({exc}) — its tags and branch are checked again when the release is "
                    f"applied.")
            else:
                remote["checked"] = True
                tip = refs.get(f"refs/heads/{push['branch']}")
                if tip is not None and not _contains(repo, head, tip):
                    raise run.refuse(
                        f"{push['remote']}/{push['branch']} ({push['url']}) is at {tip}, which "
                        f"your branch does not contain — the push would be rejected; fetch "
                        f"and integrate it first, then preview again; nothing prepared.",
                        remote_ahead={**push, "tip": tip})
                for name, sha in sorted(refs.items()):
                    if name.startswith("refs/tags/"):
                        try:
                            key = version_key(name[len("refs/tags/"):])
                        except ValueError:
                            continue
                        remote_versions.setdefault(key, []).append(
                            {"name": name[len("refs/tags/"):], "commit": sha})
    if explicit and remote_versions.get(version_key(explicit)):
        tags = remote_versions[version_key(explicit)]
        raise run.refuse(
            f"the remote {push['remote']} ({push['url']}) already has a tag of {explicit} "
            f"({', '.join(t['name'] for t in tags)}) — choose another version; nothing "
            f"prepared.", remote_tag_exists=tags)
    local_taken = planning._taken_versions(engine, project_slug, repo)

    def taken(candidate: str) -> bool:
        return local_taken(candidate) or version_key(candidate) in remote_versions

    base = {"head": head, "ref": ref}
    shared = _Preview(run, version=version, bump_type=bump_type, explicit=explicit,
                      taken=taken, base=base, work=work, push=push, local=local, cwd=cwd)
    c_star = shared.c_star
    if attest:
        at_c = shared.plan_at(c_star, None, False)
        unproven = {e["id"]: e["reason"] for e in at_c["entries"]["unproven"]}
        bad = [i for i in attest if i not in unproven]
        if bad:
            raise run.refuse(
                f"--attest {', '.join(f'#{i}' for i in bad)}: not unproven at the candidate "
                f"(already proven, released or unknown) — nothing prepared.", not_unproven=bad)
        doubted = [{"id": i, "reason": unproven[i]} for i in attest
                   if unproven[i] in _REVERT_REASONS]
        if doubted:
            listing = ", ".join(f"#{d['id']}" for d in doubted)
            raise run.refuse(
                f"--attest {listing}: reverted (or a revert "
                f"dct cannot confirm) — attesting would claim a change the history takes back; "
                f"nothing prepared.", not_attestable=doubted)
    shown_head = _synthetic_commit(repo, shared.tree, c_star, attest_message(attest)) \
        if attest else c_star
    shown = shared.variant("shown", head=shown_head, attest=attest or None,
                           entry_ids=entry_ids, no_entries=no_entries)
    built = [shown]
    waiting = shown["plan"]["entries"]["unproven"]
    if repo is None:
        unavailable = "no_git"
    elif not variants:
        unavailable = "not_requested"
    elif not waiting:
        unavailable = "none_waiting"
    elif any(e["reason"] in _REVERT_REASONS for e in waiting):
        unavailable = "reverted"
    elif len(waiting) > ALL_VARIANT_MAX_WAITING:
        unavailable = "too_many"
    else:
        unavailable = None
        adds = sorted(e["id"] for e in waiting)
        every = sorted(set(adds) | set(attest))
        all_head = _synthetic_commit(repo, shared.tree, c_star, attest_message(every))
        full = shared.variant("all", head=all_head, attest=every,
                              entry_ids=sorted(set(shown["ids"]) | set(adds)), no_entries=False,
                              reuse=shown)
        full["display"]["delta"] = {"version": full["target"], "adds": adds,
                                    "files": full["report"]["checks"]}
        built.append(full)
    if repo:
        changed = _state_diff(before, _git_state(repo))
        if changed:
            raise run.refuse(
                f"your checkout changed while the preview ran: "
                f"{_describe_checkout_change(changed)} — preview again once it is settled; "
                f"nothing stored.", checkout_changed=changed)
        skipped = {key for v in built for key in map(version_key, v["suggested"]["taken"])}
        remote["tags"] = sorted(t["name"] for key in skipped & set(remote_versions)
                                for t in remote_versions[key])
    preview_id = new_ulid()
    pin = preview_pin_ref(project_slug, preview_id) if repo else None
    slot = {
        "id": preview_id, "created_at": datetime.now(UTC).isoformat(),
        "base": {**base, "checkout_root": normalize_worktree(repo) if repo else None},
        "work": None if work is None else {
            "index_before": work["index_before"], "paths_state": work["paths_state"],
            "untracked_included": work["included"], "message": work["message"]},
        "variants": {v["consent_digest"]: {"name": v["name"], "consent": v["consent"],
                                           "proposal_content": v["content"]} for v in built},
        "pin": pin, "progress": {"work_commit": None, "attest_commit": None, "consent": None},
    }
    if repo:  # reachable BEFORE anything refers to them
        _pin_preview(repo, pin, {v["name"]: v["content"] for v in built}, shared.tree)
    try:
        stored = _ledger_write(engine, project_slug, None, "preview",
                               lambda: ledger.set_preview(engine, project_slug, slot),
                               steps=run.steps)
    except ValueError as exc:
        _unpin(repo, pin, "preview blobs")
        if "too long" not in str(exc):
            raise
        size = len(ledger._canonical(slot).encode("utf-8"))
        raise run.refuse(
            f"the preview is too large to store ({exc}) — commit part of the change yourself "
            f"first (or preview with --no-variants); nothing stored.",
            preview_too_large={"bytes": size, "max": ledger.PREVIEW_MAX_BYTES,
                               "paths": len(work["scope"]) if work else 0}) from None
    except BaseException:
        _unpin(repo, pin, "preview blobs")
        raise
    run.warnings.extend(release_preview_resources(engine, project_slug,
                                                  stored["replaced_preview"], repo))
    run.warnings.extend(release_proposal_resources(engine, project_slug,
                                                   stored["replaced_proposal"], repo))
    branch = planning._current_branch(repo) if repo else None
    details = {
        "preview_id": preview_id,
        "base": {**base, "branch": branch, "detached": bool(repo) and branch is None},
        "work": None if work is None else {
            "paths": work["paths"], "untracked_included": work["included"],
            "untracked_left": work["left"], "message": work["message"],
            "subject": work["subject"], "proves": work["proves"],
            "uncertain": work["uncertain"], "tree": work["tree"]},
        "remote": remote,
        "left_out": left_out,
        "variants": [{"name": v["name"], "consent_digest": v["consent_digest"],
                      "consent": v["consent"], "version_only": v["version_only"],
                      "suggested": v["suggested"], "display": v["display"]} for v in built],
        "all_unavailable": unavailable,
    }
    return result("prepare", project_slug, "preview", version=shown["target"], steps=run.steps,
                  warnings=run.warnings, details=details)


@_conflict_boundary("prepare")
def discard_preview(engine: sa.Engine, project_slug: str, *, cwd: str | None = None) -> dict:
    """`dct release prepare --discard-preview` — the owner declined the
    screen: clear the project's preview slot (under the operation lock),
    remove its pin and release its store blobs. Returns result() with
    outcome 'noop' and details {discarded: the preview id | null (the slot
    was empty)}."""
    project, checkout, _ = _locate(engine, project_slug, cwd)
    with _operation(project, checkout, "prepare"):
        found = _ledger_write(engine, project_slug, None, "preview",
                              lambda: ledger.discard_preview(engine, project_slug))
        repo = checkout["repo_root"] if checkout else None
        warnings = release_preview_resources(engine, project_slug, found, repo)
    return result("prepare", project_slug, "noop", warnings=warnings,
                  details={"discarded": found["id"] if found else None})


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

    K-97 F2 / K-104 / K-123: `push_remote` + `push_branch` + `push_url`
    name the ONE push target — for a NEW release in git ALL THREE are
    required (the `push_target` {remote, branch, url} prepare showed and
    the owner approved), or `no_push=True` (an explicit local release): a
    missing one is refused before the stamp (details.push_target null),
    never completed from the upstream — the remote's URL, the upstream or
    the current branch may have changed since the approval. Each value is
    checked (push_target(): the remote exists, the URL is one of its push
    addresses and literal — no rewrite rule, not a remote's name; a
    mismatch is refused). Outside git no push flag is accepted; a re-run
    of a recorded release reads none. Before the stamp that address is read (no tag of V there,
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
    proof = _stamp_proof(plan, ids)
    stampable = [i for i in ids if str(i) in snapshot["proof"]]
    if not checkout:  # K-63: a project CONFIRMED outside git — the owner's selection
        stampable = list(ids)
    before = ledger.get_release(engine, project_slug, target)
    consent = None
    push_to = None
    if before is None:
        _new_release_prechecks(run, target, ids, plan)
        if checkout and _newer_than_current(run.project, target):
            _identity_before_stamp(run, checkout["repo_root"])
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
        missing = [flag for flag, value in (("--push-remote", push_remote),
                                            ("--push-branch", push_branch),
                                            ("--push-url", push_url)) if not value]
        if run.repo is not None and not no_push and missing:
            # K-123: what prepare showed is bound to nothing after the
            # approval (the upstream, the remote's URL and the current branch
            # can all change with HEAD and both digests intact) — so apply
            # never derives the target: the caller names the approved one.
            # Every version WITHOUT a record is covered (whether the stamp
            # will write is only known inside it), a legacy no-op included.
            raise run.refuse(
                f"a new release (a version without a record) that pushes needs the COMPLETE "
                f"approved push target — "
                f"`--push-remote <remote> --push-branch <branch> --push-url <url>`, exactly "
                f"the `push_target` (remote, branch, url) that `dct release prepare` showed "
                f"and the owner approved (missing: {', '.join(missing)}). apply never selects "
                f"the upstream itself: it may have changed since the approval. For another "
                f"destination the owner names all three (the URL is one of `git remote "
                f"get-url --push --all <remote>`); `--no-push` is an explicit local release. "
                f"If {target} was already released before dct kept release records, record "
                f"it with `dct release reconstruct --version {target}` instead of apply; "
                f"nothing released.", push_target=None)
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
    if checkout and before is None:
        # K-25: an index.lock that EXISTS now would stop the release commit
        # after the stamp — refused before it. (One that appears later — like
        # a refusing hook or a signature failure — stops the release after
        # the stamp; `dct release resume` finishes it. No trial commit.)
        lock = _foreign_index_lock(checkout["repo_root"])
        if lock:
            raise run.refuse(
                f"{lock} exists — another git process is using the index; dct never removes "
                f"a foreign index.lock; refused before the stamp, nothing released — run the "
                f"same `dct release apply` again once it is gone.", index_lock=lock)
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


# ── apply --consent: the one-dialog release ──────────────────────────────────
#
# `dct release apply --consent C --confirm` releases EXACTLY the content of
# the consent view C a preview stored (prepare --preview), under ONE
# operation lock: it re-derives the view on synthetic commits and refuses
# when it differs (nothing made), makes the work-in-progress commit W and /
# or the attestation commit A through the repository's hooks (_hook_commit,
# a temporary index), re-derives the view at the real candidate K and stamps
# only when it is still C (docs/release-workflow.md, "Apply --consent").
# A re-run of the same command ADOPTS what an interrupted one made
# (planning.adoptable_chain) — never a second W or A.

NOTHING_MADE = {"work_commit": None, "attest_commit": None}
# The largest value one leaf of a consent_mismatch diff shows (characters of
# canonical JSON; longer ones are cut and marked).
CONSENT_DIFF_MAX_CHARS = 500


def _narrow(approved, now) -> tuple:
    """The differing part of two values: for two objects only their
    differing keys, recursively; anything else as it is."""
    if isinstance(approved, dict) and isinstance(now, dict):
        keys = sorted(k for k in set(approved) | set(now) if approved.get(k) != now.get(k))
        pairs = {k: _narrow(approved.get(k), now.get(k)) for k in keys}
        return {k: p[0] for k, p in pairs.items()}, {k: p[1] for k, p in pairs.items()}
    return approved, now


def _clip(value):
    text = ledger._canonical(value)
    return value if len(text) <= CONSENT_DIFF_MAX_CHARS \
        else text[:CONSENT_DIFF_MAX_CHARS - 1] + "…"


def view_mismatch(approved: dict, now: dict) -> dict | None:
    """None when the derived consent view `now` IS the approved one, else
    `consent_mismatch`: {changed: [top-level keys], diff: {key: {approved,
    now}}} — each side narrowed to the differing sub-keys and cut to
    CONSENT_DIFF_MAX_CHARS of canonical JSON."""
    if approved == now:
        return None
    changed = sorted(k for k in set(approved) | set(now) if approved.get(k) != now.get(k))
    diff = {}
    for key in changed:
        old, new = _narrow(approved.get(key), now.get(key))
        diff[key] = {"approved": _clip(old), "now": _clip(new)}
    return {"changed": changed, "diff": diff}


def _with_made(exc: ReleaseRefused, made: dict) -> ReleaseRefused:
    """Every refusal of apply --consent names what it made (`details.made`);
    a commit made (or adopted) before a refusal that stamped nothing is
    said in the message too — the commit STAYS, nothing is reset."""
    exc.details = {**exc.details, "made": dict(made)}
    if any(made.values()) and exc.release is None:
        kept = ", ".join(f"{name.removesuffix('_commit')} {sha[:12]}"
                         for name, sha in made.items() if sha)
        message = (f"{exc} — commit made, release not started ({kept} stays; run the same "
                   f"`dct release apply --consent` again, or preview again)")
        exc.args = (message,)
    return exc


@_conflict_boundary("apply")
def apply_consent(
    engine: sa.Engine,
    project_slug: str,
    consent: str,
    *,
    confirm: bool = False,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """`dct release apply --consent C --confirm` — release EXACTLY the
    consent view C of the project's preview slot (prepare --preview), see
    docs/release-workflow.md "Apply --consent". Under the operation lock
    (action 'apply'), in order: the slot holds C (`no_preview`,
    `consent_unknown`) and no commit was made under its other screen
    (`consent_in_progress`); the pre-checks of a new release (executor,
    unfinished record, git ≥ 2.36); the base — planning.consent_adoption,
    the predicate the plan's pending_consent uses: HEAD names the preview's
    ref (else `base_moved` `why: "branch_switched"`), HEAD is H0 or the
    W / A this consent approves (adopted, planning.adoptable_chain; else
    `why: "head_moved"`), and every commit the slot's progress records is
    in that chain (else `why: "progress_lost"` — never a second W / A) —
    checked again after W, after A and right before the stamp (a switch on
    the same commit included; dct switches nothing back); the
    approved blobs are still available (`missing_blobs`); the work tree
    and the index still hold the approved work, or — a screen without work
    whose attestation is still to make — the tree is still clean
    (`work_changed`, `index_changed`); the view re-derived on synthetic
    commits equals C (`consent_mismatch` — nothing made); the commit
    identity, ALWAYS in git (refused before the stamp); W through the
    repository's hooks (`hook_changed_tree`, `message_contract`, a refusing
    hook — nothing published), recorded in the slot's progress right after
    its publication (before the post-commit hook), the real index synced
    (`foreign_staged`); A (the "all" variant; on a W the tree still clean —
    `work_changed`; `hook_changed_tree` as for W; recorded like W); the view at the real
    candidate K equals C (`consent_mismatch` with `made`); the remote
    (`remote_tags`, `remote_branch`) — every derivation reads the
    project's settings again; then the stamp consumes the preview (core
    release_entries consume_preview, in ONE transaction — `preview_replaced`,
    `date_changed` (the view's date, bound with or without a changelog
    file), `consent_mismatch` `changed: ["entries"]` (an entry changed since
    the last derivation) or `["changelog"]` (the render or the settings that
    choose the changelog file); a refusal there keeps the W / A made, named
    in `made`) with a consent that binds the approved changelog composition
    (`changelog_sha256` — the file write and every resume refuse another,
    `changelog_changed`), and the release continues as apply's steps 3-7.
    The script never runs.

    `confirm=True` is required. Returns result() with outcome 'completed'
    and details {requested_version, previous_version, bump, entry_ids,
    mode, released, current_version, push_target, consent_digest, variant,
    made: {work_commit, attest_commit}, candidate_sha, plan_digest,
    proposal_sha256}; steps `work_commit` / `attest` each {step, result:
    "done" | "adopted", commit}. Every ReleaseRefused carries
    `details.made`; the binding flags of the D/P path are refused by the
    CLI (`consent_flag_conflict`). `on_point` — the interruption points,
    for tests."""
    made = dict(NOTHING_MADE)
    try:
        if confirm is not True:
            raise ReleaseRefused("apply --consent needs --confirm (it commits your changes, "
                                 "stamps, writes, commits, tags and pushes); nothing done.")
        if not isinstance(consent, str) or not _DIGEST_RE.fullmatch(consent):
            raise ReleaseRefused(
                f"--consent must be the lowercase hex SHA-256 consent_digest of the screen the "
                f"owner approved, got {consent!r}; nothing done.")
        located = _locate(engine, project_slug, cwd)
        with _operation(located[0], located[1], "apply"):
            return _apply_consent(engine, project_slug, located, consent, made=made, cwd=cwd,
                                  on_point=on_point)
    except ReleaseRefused as exc:
        raise _with_made(exc, made) from exc.__cause__


def _release_artifact_paths(run: _Run) -> set[str]:
    """The repo-relative paths of the release artifacts in force now (the
    changelog dct manages, the version-file contract)."""
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    return {a["path"] for a in planning._artifacts(run.changelog_rel, contract)}


def _check_work_unchanged(run: _Run, h0: str, work: dict | None, stored: dict | None, *,
                          nothing: str = "nothing committed.") -> None:
    """R5 / R6 — before the work-in-progress commit (or, for a screen that
    showed a clean tree — `work` None, an empty staging set — before the
    attestation commit) is made: the work tree still gives T1 for Pₛ
    (rebuilt as the preview built it, content equality — a touched file is
    fine), no tracked path outside Pₛ (and no conflicted path, no new
    untracked release artifact) is dirty, every included untracked file is
    there; the user's index entry of each path of Pₛ is the one it held at
    the preview, H0's or T1's. Also before A on a W made (or adopted)
    earlier — `h0` = W, `work` None: the tree must still be clean on W."""
    repo = run.repo
    scope = sorted(work["paths"]) if work else []
    expected = work["tree"] if work else _tree(repo, h0)
    new_dirty = sorted(set(_work_tree_dirt(repo, _release_artifact_paths(run))) - set(scope))
    missing = sorted(p for p in (stored or {}).get("untracked_included", [])
                     if not os.path.lexists(os.path.join(repo, *p.split("/"))))
    tree_now = planning.future_tree(repo, h0, scope, timeout=PREPARE_GIT_TIMEOUT_SECONDS) if scope else expected
    if tree_now != expected or new_dirty or missing:
        changed = sorted(_diff_names(repo, expected, tree_now)) if tree_now != expected else []
        parts = [f"changed: {', '.join(changed)}" if changed else "",
                 f"newly dirty: {', '.join(new_dirty)}" if new_dirty else "",
                 f"gone: {', '.join(missing)}" if missing else ""]
        shown = "your uncommitted work is not" if work else "the work tree is no longer clean as"
        raise run.refuse(
            f"{shown} what the screen showed ({'; '.join(p for p in parts if p)}) — preview "
            f"again; {nothing}",
            work_changed={"tree_expected": expected, "tree_now": tree_now,
                          "paths_changed": changed, "new_dirty": new_dirty,
                          "missing_untracked": missing})
    if not scope:
        return
    current = _index_entries(repo, scope)
    in_h0 = planning.tree_entries(repo, h0, scope)
    in_t1 = planning.tree_entries(repo, expected, scope)
    before = {p: None if e is None else (e["mode"], e["blob"], e["stage"])
              for p, e in stored["index_before"].items()}
    moved = [p for p in scope
             if current.get(p) not in (before.get(p), in_h0.get(p), in_t1.get(p))]
    if moved:
        raise run.refuse(
            f"{', '.join(moved)}: your index holds a version that is neither what it held at "
            f"the preview nor the base's nor the approved one — someone staged something "
            f"else; preview again; nothing committed.", index_changed={"paths": moved})


def _base_id(run: _Run, head: str | None, rel: str) -> str | None:
    """The id of `rel` at the candidate `head` (git: its blob) or, outside
    git, of the file now (SHA-256) — None when absent."""
    if run.repo:
        return _blob_at(run.repo, head, rel)
    found = _content_id(run, rel)
    if found == _NOT_A_FILE:
        raise run.refuse(f"{rel} is not a regular file — nothing released.", path=rel)
    return found


def _derive_view(run: _Run, slot: dict, item: dict, *, head: str | None,
                 synthetic: dict[str, str], cwd: str | None) -> dict:
    """The consent view of the approved variant `item` derived NOW for the
    candidate `head` (synthetic or real) — the preview's own builder
    (consent_view) on fresh inputs: the plan pinned to `head` with the
    approved version and selection, the changelog composed now, the script
    and the base blobs at `head`, the contract, the push target re-validated
    (push_target with the approved remote, branch and URL — never derived
    again); the work and the attestation are the approved ones (the caller
    verified the commits / the work tree). The version sync never runs: the
    expected blobs are the stored ones. → {view, plan, ids, target, push_to,
    push_error}."""
    approved, content = item["consent"], item["proposal_content"]
    target, ids = approved["version"]["target"], list(approved["entries"]["selected"])
    _refresh_project(run)
    if run.repo:
        run.checkout = {**run.checkout, "head": head}
    # The file is chosen again from the refreshed settings in git AND outside
    # it (K-44): the view, the stamp's expected settings and the write all
    # follow the settings in force now — a change refuses as a mismatch.
    run.bind_changelog(head)
    _check_changelog_file(run)
    try:
        plan = planning.build_plan(run.engine, run.slug, version=target, cwd=cwd,
                                   entry_ids=ids or None, no_entries=not ids,
                                   pinned_checkout=run.checkout)
    except ValueError as exc:
        raise run.refuse(str(exc)) from None
    if plan["snapshot"] is None:
        raise run.refuse("the plan has no snapshot (an unfinished release?) — nothing done.")
    record = _provisional_record(run, target, ids, plan)
    section, date = render_section(run.engine, run.slug, record, provisional=True)
    composed = _check_changelog_content(run, target, ids, plan, record=record)
    if run.repo:
        _check_local_tags(run, target)
    script = _current_script(run)
    fresh = {**content,
             "script": None if script is None else {"identity": script[0],
                                                    "sha256": _sha256(script[1])},
             "base_blobs": {rel: _base_id(run, head, rel) for rel in content["paths"]},
             "changelog": None if not run.changelog_rel else {
                 "path": run.changelog_rel,
                 "base_blob": _base_id(run, head, run.changelog_rel)}}
    push, push_to, push_error = None, None, None
    if run.repo:
        consented = approved["push"]
        if consented is None or consented["mode"] == "local":
            push = consented
        else:
            try:
                push_to = push_target(run, target, push_remote=consented["remote"],
                                      push_branch=consented["branch"],
                                      push_url=consented["url"])
            except ReleaseRefused as exc:
                push_error = str(exc)
            else:
                push = {"mode": "push", **{k: push_to[k] for k in ("remote", "branch", "url",
                                                                  "tag")}}
    stored_work = slot["work"]
    work = approved["work"]
    view = consent_view(
        run, plan=plan, target=target, ids=ids,
        base={"head": slot["base"]["head"], "ref": slot["base"]["ref"]},
        work=None if work is None else {
            "tree": work["tree"], "subject": work["subject"], "proves": work["proves"],
            "message": stored_work["message"], "view_paths": work["paths"]},
        attest=(approved["attest"] or {}).get("ids"),
        candidate_tree=_tree(run.repo, head) if run.repo else None,
        composition={"composed": composed, "section": section, "date": date,
                     "pr_refs": record["render"]["pr_refs"]},
        content=fresh, push=push, synthetic=synthetic)
    return {"view": view, "plan": plan, "ids": ids, "target": target, "push_to": push_to,
            "push_error": push_error}


def _refresh_project(run: _Run) -> None:
    """The project settings in force NOW (the changelog file, its render,
    the current version) — read again before every derivation of the
    consent view and the step-7 checks: a setting changed while the
    repository's hooks ran is seen (R9), never the copy read before. The
    caller re-resolves the changelog file from it (run.bind_changelog — in
    git and outside it, K-44)."""
    run.project = get_project(run.engine, run.slug) or run.project


def _refuse_mismatch(run: _Run, approved: dict, derived: dict, *, when: str) -> None:
    """R9a / R9: the derived view must BE the approved one."""
    mismatch = view_mismatch(approved, derived["view"])
    if mismatch is None:
        return
    extra = {"push_error": derived["push_error"]} if derived["push_error"] else {}
    raise run.refuse(
        f"the release is no longer what the owner approved ({', '.join(mismatch['changed'])} "
        f"changed {when}) — preview again and show the new screen; nothing released.",
        consent_mismatch=mismatch, **extra)


def _record_made(run: _Run, slot: dict, **made) -> None:
    """record_preview_progress — the commit apply made (or adopted) and the
    consent C it was made under, written BEFORE it goes on (a re-run adopts
    it; the plan's pending_consent names C); a slot replaced meanwhile →
    refused."""
    try:
        _ledger_write(run.engine, run.slug, None, "preview",
                      lambda: ledger.record_preview_progress(run.engine, run.slug, slot["id"],
                                                             **made), steps=run.steps)
    except ValueError as exc:
        raise run.refuse(f"{exc}", preview_replaced={"preview_id": slot["id"]}) from None


def _check_adoptable(run: _Run, slot: dict, approved: dict, made: dict, *,
                     candidate: str | None = None, nothing: str) -> dict:
    """planning.consent_adoption on the checkout NOW — the same predicate
    the plan's pending_consent uses — before any commit (`made` = the slot's
    recorded progress), after the work-in-progress commit, after the
    attestation commit and right before the stamp (`made` = what this run
    made or adopted; `candidate` = the release candidate K HEAD must be).
    A refusal is `base_moved` with apply's `why`: `branch_switched` (HEAD
    names another ref than at the preview — a switch on the same commit, a
    detach or an attach included; dct switches nothing back; the message
    names planning.switch_back_command — for a detached preview the
    verified progress commit, K-42),
    `head_moved`, or `progress_lost` (never a second W / A under the same
    approval: the recorded commit is reset away — and maybe pruned; discard
    the preview). Returns the adopted chain."""
    repo, h0 = run.repo, slot["base"]["head"]
    head, ref = _head(repo), _head_ref(repo)
    found = planning.consent_adoption(repo, slot["base"], approved, head, ref, made)
    why = found["why"]
    if why is None and candidate is not None and head != candidate:
        why = "head_moved"
    if why is None:
        return found["chain"]
    moved = {"expected_parent": h0, "head": head, "why": why}
    if why == "branch_switched":
        expected = slot["base"]["ref"]
        back = planning.switch_back_command(repo, slot["base"], approved, made)
        way = (f"switch back (`{back}`) and run the same `dct release apply --consent` "
               f"again, or preview again" if back else
               f"the commit made under this approval can no longer be reached: "
               f"{planning.DISCARD_PREVIEW_STEP}")
        raise run.refuse(
            f"HEAD names {ref or 'no branch (detached)'}, not "
            f"{expected or 'a detached HEAD'} as at the preview — {way}; {nothing}",
            base_moved=moved)
    if why == "progress_lost":
        gone = [sha for _, sha in found["lost"] if not planning.commit_exists(repo, sha)]
        named = ", ".join(f"{'work-in-progress' if field == 'work_commit' else 'attestation'} "
                          f"commit {sha[:12]}" for field, sha in found["lost"])
        where = ("no longer exists (reset away and pruned?)" if gone
                 else f"is no longer what HEAD ({head[:12]}) stands on (reset away?)")
        raise run.refuse(
            f"the {named} made under this approval {where} — dct never makes a second one "
            f"under the same approval; discard the preview (`dct release prepare "
            f"--discard-preview`) and preview again; {nothing}", base_moved=moved)
    if candidate is not None:
        raise run.refuse(
            f"HEAD is {head}, not the release candidate {candidate} — it moved after the "
            f"commits were made; {nothing}", base_moved=moved)
    raise run.refuse(
        f"HEAD is {head}, not {h0} (the preview's base) nor a commit this consent approved — "
        f"the history moved since the screen; preview again; {nothing}", base_moved=moved)


def _work_commit(run: _Run, h0: str, work: dict, message: str, scope: list[str],
                 on_point, on_published: Callable[[str], None] | None = None) -> str:
    """W — the approved tree T1 on H0 with the approved message, through the
    repository's hook lifecycle (_hook_commit, a temporary index): a hook
    that changes the content (the temporary index, the work tree's files of
    Pₛ, or — against the reading taken before the hooks — a tracked path
    outside Pₛ, a conflicted path or an untracked release artifact) →
    `hook_changed_tree`; one that changes the subject or the proven ids →
    `message_contract` — both before anything is published. `on_published`
    (the progress write) runs right after the publication, before the
    post-commit hook."""
    artifacts = _release_artifact_paths(run)
    before = _work_tree_dirt(run.repo, artifacts)

    def contract(facts: dict, commit) -> tuple[str, dict] | None:
        proven = sorted(proven_ids(commit))
        if facts["subject"] == work["subject"] and proven == work["proves"]:
            return None
        reason = (f"its subject is {facts['subject']!r} proving {proven}; approved "
                  f"{work['subject']!r} proving {work['proves']}")
        return (reason,
                {"message_contract": {"subject": facts["subject"],
                                      "approved_ids": work["proves"], "proven_ids": proven}})

    def files_unchanged() -> None:
        now = planning.future_tree(run.repo, h0, scope,
                                     timeout=PREPARE_GIT_TIMEOUT_SECONDS)
        changed = set(_diff_names(run.repo, work["tree"], now)) if now != work["tree"] else set()
        changed.update(_dirt_changed(before, _work_tree_dirt(run.repo, artifacts), skip=scope))
        if changed:
            changed = sorted(changed)
            raise run.refuse(
                f"a hook changed your files ({', '.join(changed)} — a formatter?) while the "
                f"work-in-progress commit was made; nothing was published and dct restores "
                f"nothing: make the hook leave them alone (or commit them yourself), then "
                f"preview again.", hook_changed_tree={"paths": changed},
                expected_tree=work["tree"], tree=now)

    commit, warnings = _hook_commit(run.refuse, run.repo, work["tree"], h0, message,
                                    check=contract, work_tree_check=files_unchanged,
                                    on_point=on_point, on_published=on_published)
    run.warnings.extend(warnings)
    return commit


def _apply_consent(engine, project_slug, located, consent_digest, *, made, cwd,
                   on_point) -> dict:
    slot = ledger.get_preview(engine, project_slug)
    if slot is None:
        raise ReleaseRefused(
            f"'{project_slug}' has no preview to release — it was released already, discarded "
            f"(Stop) or replaced by `dct release prepare`; preview again (`dct release prepare "
            f"--preview`) and approve the new screen; nothing done.",
            details={"no_preview": True})
    item = slot["variants"].get(consent_digest)
    if item is None:
        raise ReleaseRefused(
            f"{consent_digest} is not a screen of the current preview {slot['id']} (a newer "
            f"preview replaced the one the owner approved, or the digest is wrong) — show the "
            f"current screen and approve it; nothing done.",
            details={"consent_unknown": {
                "preview_id": slot["id"],
                "variants": [{"name": v["name"], "version": v["consent"]["version"]["target"]}
                             for v in slot["variants"].values()]}})
    applied = slot["progress"].get("consent")
    if applied is not None and applied != consent_digest:
        other = slot["variants"][applied]
        raise ReleaseRefused(
            f"a commit was already made under the other screen of this preview "
            f"({other['name']}, {other['consent']['version']['target']}) — finish that release "
            f"(the plan's pending_consent names it) or preview again; nothing done.",
            details={"consent_in_progress": {
                "preview_id": slot["id"], "variant": other["name"],
                "version": other["consent"]["version"]["target"],
                "progress": {k: slot["progress"][k] for k in NOTHING_MADE}}})
    project, checkout, project_root = located
    # The settings the derivation reads are the ones in force NOW (under the
    # lock), not those _locate read.
    project = get_project(engine, project_slug) or project
    run = _new_release_run(engine, project_slug, (project, checkout, project_root),
                           on_point=on_point)
    approved, content = item["consent"], item["proposal_content"]
    repo = run.repo
    here = normalize_worktree(repo) if repo else None
    h0, work, attest = slot["base"]["head"], approved["work"], approved["attest"]
    if approved["release_mode"] != run.mode or approved["checkout_root"] != here:
        there = approved["checkout_root"] or "outside git"
        raise run.refuse(
            f"the preview was made in another checkout ({there}), not here "
            f"({here or 'outside git'}) — release it there, or preview here; nothing done.",
            base_moved={"expected_parent": h0, "head": checkout["head"] if checkout else None,
                        "why": "other_checkout"})
    if repo:
        _check_git_version(repo)
        busy = _operation_in_progress(repo)
        if busy:
            raise run.refuse(f"a git operation is in progress ({', '.join(busy)}) — finish or "
                             f"abort it first; nothing done.", in_progress=busy)
        # 2. The base and adoption: HEAD is H0, or the W / A this consent
        # approves (an interrupted run, or a plain `git commit` of T1), on
        # the ref HEAD named at the preview.
        made.update(_check_adoptable(run, slot, approved, slot["progress"],
                                     nothing="nothing done."))
    # R10 — the approved content (pinned by the preview) is still there.
    blobs = list(content["expected_blobs"].values())
    if repo and work is not None:
        blobs.append(work["tree"])
    if not repo and (content.get("changelog") or {}).get("base_blob"):
        blobs.append(content["changelog"]["base_blob"])
    missing = [blob for blob in blobs if not _blob_available(run, blob)]
    if missing:
        raise run.refuse(
            f"approved content is no longer available ({', '.join(missing)}; e.g. pruned by "
            f"`git gc` after its pin was lost) — preview again; nothing done.",
            missing_blobs=missing)
    if repo and slot["pin"] and _rev(repo, slot["pin"]) is None:
        _pin_preview(repo, slot["pin"],
                     {v["name"]: v["proposal_content"] for v in slot["variants"].values()},
                     work["tree"] if work else None)
    # 3. The work tree and the index still hold the approved work — for a
    # screen that showed a clean tree (no work) and an attestation still to
    # make: the tree is still clean (an empty staging set), before A.
    if repo and made["work_commit"] is None and (
            work is not None or (attest is not None and made["attest_commit"] is None)):
        _check_work_unchanged(run, h0, work, slot["work"])
    # 4. The pre-commit derivation on synthetic commits (R9a — nothing made).
    k, synthetic = (h0 if repo else None), {}
    if repo and work is not None:
        k = made["work_commit"] or _synthetic_commit(repo, work["tree"], h0,
                                                     slot["work"]["message"])
        synthetic[k] = "@work"
    if repo and attest is not None:
        k = made["attest_commit"] or _synthetic_commit(repo, _tree(repo, k), k,
                                                       attest_message(attest["ids"]))
        synthetic[k] = "@attest"
    derived = _derive_view(run, slot, item, head=k, synthetic=synthetic, cwd=cwd)
    _refuse_mismatch(run, approved, derived, when="since the screen")
    target = derived["target"]
    pending = repo and ((work is not None and made["work_commit"] is None)
                        or (attest is not None and made["attest_commit"] is None))
    if pending and derived["push_to"] is not None:  # no commit for a release the remote refuses
        _check_push_precondition(run, derived["push_to"], target, h0)
    if repo:  # also with nothing left to commit: the release commit needs it
        _identity_before_stamp(run, repo, nothing="nothing committed, nothing released."
                               if pending else "nothing released.")

    def published(field: str) -> Callable[[str], None]:
        # The commit and C become the slot's progress BEFORE the user's
        # post-commit hook runs (a kill there leaves them recorded).
        def record(commit: str) -> None:
            made[field] = commit
            _record_made(run, slot, **{field: commit}, consent=consent_digest)
        return record

    # 5. The work-in-progress commit W and the user's index.
    if repo and work is not None:
        scope = sorted(work["paths"])
        if made["work_commit"] is None:
            _point(on_point, "before_work_commit")
            made["work_commit"] = _work_commit(run, h0, work, slot["work"]["message"], scope,
                                               on_point, on_published=published("work_commit"))
            run.step("work_commit", "done", commit=made["work_commit"])
        else:
            run.step("work_commit", "adopted", commit=made["work_commit"])
            if slot["progress"]["work_commit"] is None:
                _record_made(run, slot, work_commit=made["work_commit"], consent=consent_digest)
        _point(on_point, "after_work_commit")
        # The ref HEAD names is the preview's still (a post-commit hook, an
        # IDE) — before the index sync and before A lands anywhere.
        _check_adoptable(run, slot, approved, made,
                         candidate=made["attest_commit"] or made["work_commit"],
                         nothing="nothing released.")
        allowed = {p: None if e is None else (e["mode"], e["blob"], e["stage"])
                   for p, e in slot["work"]["index_before"].items()}
        _sync_real_index(run, made["attest_commit"] or made["work_commit"], scope, base=h0,
                         allowed_before=allowed, what="the work-in-progress commit",
                         next_step="run the same `dct release apply --consent` again")
        _point(on_point, "after_work_index")
    # 6. The attestation commit A (a "release with all N" variant).
    if repo and attest is not None:
        if made["attest_commit"] is None:
            if made["work_commit"] is not None:
                # The tree must still be clean on W (an edit made after an
                # interrupted run, a post-commit hook) — before A, not at the
                # clean-tree check after it was published.
                _check_work_unchanged(run, made["work_commit"], None, None,
                                      nothing="nothing more committed.")
            commit, warnings = _attest_commit(run.refuse, repo, made["work_commit"] or h0,
                                              attest["ids"], artifacts=_release_artifact_paths(run),
                                              on_point=on_point,
                                              on_published=published("attest_commit"))
            run.warnings.extend(warnings)
            made["attest_commit"] = commit
            run.step("attest", "done", commit=commit)
        else:
            run.step("attest", "adopted", commit=made["attest_commit"])
            if slot["progress"]["attest_commit"] is None:
                _record_made(run, slot, attest_commit=made["attest_commit"],
                             consent=consent_digest)
        _point(on_point, "after_attest_commit")
    # 7. The post-commit derivation at the real candidate K (R9).
    pre_untracked: list[str] = []
    k = (made["attest_commit"] or made["work_commit"] or h0) if repo else None
    if repo:
        _check_adoptable(run, slot, approved, made, candidate=k, nothing="nothing released.")
        _refresh_project(run)
        run.checkout = {**run.checkout, "head": k}
        run.bind_changelog(k)
        state = _clean_tree(run, planning._artifacts(
            run.changelog_rel, ledger.get_version_files(engine, project_slug)["version_files"]))
        pre_untracked = state["untracked"]
        synthetic = {sha: name for sha, name in ((made["work_commit"], "@work"),
                                                 (made["attest_commit"], "@attest")) if sha}
    final = _derive_view(run, slot, item, head=k, synthetic=synthetic, cwd=cwd)
    _refuse_mismatch(run, approved, final, when="while the commits were made")
    push_to, plan, ids = final["push_to"], final["plan"], final["ids"]
    if push_to is not None:
        _check_push_precondition(run, push_to, target, k)
    # 8. Rebind the stored content to K and stamp — consuming the preview.
    proposal = ledger.validate_proposal({**content, "id": slot["id"], "candidate_sha": k,
                                         "plan_digest": plan["snapshot_digest"]})
    proposal_sha256 = ledger.proposal_digest(proposal)
    proposal_pin = _pin_proposal(repo, project_slug, proposal) if repo else None
    # The render and the date are the screen's (bound with or without a
    # changelog file); the composition the owner approved is bound to the
    # record (changelog_sha256 — the file write and every resume check it);
    # the settings that chose the file are the ones the derivation above
    # used, compared again inside the stamp transaction.
    bound = approved["changelog"]
    render = {"pr_refs": bound["pr_refs"]}
    consent = {"version": target, "entry_ids": ids, "candidate_sha": k,
               "proposal_sha256": proposal_sha256, "render": render, "push_target": push_to,
               "executor": ledger.DEFAULT_EXECUTOR, "content_sha256": consent_digest,
               "changelog_sha256": bound["composed_sha256"]}
    settings = choice_settings(run.project)
    stampable = [i for i in ids if str(i) in plan["snapshot"]["proof"]] if repo else list(ids)
    _point(on_point, "before_stamp")
    if repo:
        _check_adoptable(run, slot, approved, made, candidate=k, nothing="nothing released.")
    lock = _foreign_index_lock(repo) if repo else None
    if lock:
        raise run.refuse(
            f"{lock} exists — another git process is using the index; dct never removes a "
            f"foreign index.lock; refused before the stamp, nothing released — run the same "
            f"`dct release apply --consent` again once it is gone.", index_lock=lock)

    def stamp() -> dict:
        return release_entries(
            engine, project_slug, target, entry_ids=ids, candidate_sha=k,
            checkout_root=repo, proof=_stamp_proof(plan, ids), pr_source="git",
            release_mode=run.mode, stampable_ids=stampable,
            expected_entries=plan["snapshot"]["entries"], consent=consent, render=render,
            manifest={"untracked_baseline": sorted(pre_untracked)} if repo else None,
            consume_preview={"preview_id": slot["id"], "consent_sha256": consent_digest,
                             "proposal": proposal},
            expected_release_date=bound["date"], expected_changelog_settings=settings)

    try:
        stamped = _ledger_write(engine, project_slug, normalize_version(target), "stamp", stamp,
                                steps=run.steps)
    except BaseException as exc:
        run.warnings.extend(_unpin(repo, proposal_pin, "proposal blobs"))
        if isinstance(exc, ValueError):
            raise run.refuse(str(exc), **getattr(exc, "details", {})) from None
        raise
    run.record = stamped["release"]
    if run.record is None:  # cannot happen for a version above the current one
        raise run.refuse(f"{target} was not stamped — nothing released.")
    run.step("stamp", "done", released=stamped["released"])
    # The preview is consumed: its pin and the store blobs no live proposal
    # names go (the record's proposal keeps its own pin).
    run.warnings.extend(release_preview_resources(engine, project_slug, slot, repo))
    _point(on_point, "after_stamp")
    _check_record_scope(run)
    _check_record_executor(run)
    _check_record_consistency(run)
    _check_record_location(run)
    _continue(run, fresh=True)
    return _finish(
        "apply", run, "completed", requested_version=target,
        previous_version=plan["version"]["current"], bump=plan["version"]["bump"],
        entry_ids=ids, mode=stamped["mode"], released=stamped["released"],
        current_version=stamped["current_version"], push_target=push_to,
        consent_digest=consent_digest, variant=item["name"], made=dict(made),
        candidate_sha=k, plan_digest=plan["snapshot_digest"],
        proposal_sha256=proposal_sha256)


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
        _bind_record_changelog(run)
        _continue(run, fresh=False)
        return _finish("resume", run, "completed")


def _bind_record_changelog(run: _Run) -> None:
    """#876 — the changelog file of a resumed record: resolved again at the
    RECORDED candidate (deterministic after HEAD moved; a 'stamped' record's
    approved file is compared with it by _record_proposal); past 'stamped'
    the file is the one its verified manifest records."""
    if run.repo and run.record.get("candidate_sha"):
        run.bind_changelog(run.record["candidate_sha"])
    recorded = (run.record.get("manifest") or {}).get("changelog")
    if run.record["status"] != "stamped" and recorded:
        run.changelog_rel = recorded["path"]


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
    approved ones: a hook that adds, drops or duplicates an id, stages
    content or changes a file in the work tree (`hook_changed_tree`) is
    refused with nothing published (a hook's addition that
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

    def refuse(msg: str, **details) -> ReleaseRefused:
        return ReleaseRefused(msg, details={**details, "previous_head": head})

    commit, warnings = _attest_commit(refuse, repo, head, ids, on_point=on_point)
    return result("attest", project_slug, "attested", warnings=warnings,
                  steps=[{"step": "attest", "result": "done", "commit": commit}],
                  details={"previous_head": head, "head": commit, "entry_ids": ids})


def _attest_commit(run_refuse: Callable[..., ReleaseRefused], repo: str, head: str,
                   ids: list[int], *, artifacts: Iterable[str] = (),
                   on_point: Callable[[str], None] | None = None,
                   on_published: Callable[[str], None] | None = None) -> tuple[str, list[str]]:
    """The attestation commit's internals, shared by `dct release attest`
    and `apply --consent` (a "release with all N" variant): nothing staged
    against `head` (the commit is EMPTY — refused with `staged`, nothing
    reset), then _hook_commit of `head`'s tree on `head` with attest_message
    and the K-107 contract — the subject ATTEST_SUBJECT and EXACTLY `ids`
    as the proven set (trailer + body scan; `approved_ids` / `proven_ids`).
    A hook that changes the work tree (a tracked path, a conflicted path or
    an untracked release artifact among `artifacts`, against the reading
    taken before the hooks) → `hook_changed_tree`, before anything is
    published. `on_published` (apply --consent's progress write) runs right
    after the publication, before the post-commit hook. The caller checked
    that `ids` are unproven. → (commit, warnings)."""
    tree = _tree(repo, head)
    if _staged_against(repo, head):
        raise run_refuse("the index holds staged changes — the attestation commit is empty; "
                         "unstage them first (nothing is reset for you).",
                         staged=planning.staged_files(repo))
    artifacts = set(artifacts)
    before = _work_tree_dirt(repo, artifacts)

    def files_unchanged() -> None:
        changed = _dirt_changed(before, _work_tree_dirt(repo, artifacts))
        if changed:
            raise run_refuse(
                f"a hook changed your files ({', '.join(changed)} — a formatter?) while the "
                f"attestation commit was made; nothing was published and dct restores nothing: "
                f"make the hook leave them alone (or commit them yourself), then run the "
                f"command again.", hook_changed_tree={"paths": changed})

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

    return _hook_commit(run_refuse, repo, tree, head, attest_message(ids),
                        check=message_contract, work_tree_check=files_unchanged,
                        on_point=on_point, on_published=on_published)


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


def _legacy_section(run: _Run, rel: str, version: str, ids: list[int],
                    data: bytes | None) -> dict:
    """F (`rel`, its content `data`) holds the V section (any equivalent
    label) EXACTLY once, its body equal to the database render of the
    entries → the manifest's changelog {path, sha256, section_date};
    ReleaseRefused otherwise."""
    try:
        text = data.decode("utf-8", "strict") if data is not None else None
    except UnicodeDecodeError:
        raise ReleaseRefused(f"{rel} is not UTF-8 text") from None
    blocks = _version_blocks(text, version) if text is not None else []
    if len(blocks) != 1:
        raise ReleaseRefused(f"{rel} holds {len(blocks)} {version} sections")
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
        raise ReleaseRefused(f"the {version} section of {rel} differs from the "
                             f"database's entries")
    return {"path": rel, "sha256": _sha256(data), "section_date": date}


# K-109: the lines of a history diff kept in a result (the digest covers
# the whole diff).
HISTORY_DIFF_MAX_LINES = 400


def _visible(line: str) -> str:
    """One diff line for display: its line ending made visible when it is
    not a plain newline (a change of line endings is a change)."""
    body = line[:-1] if line.endswith("\n") else line + "\\ (no newline at end of file)"
    return body.replace("\r", "\\r")


def _history_diff(old: str, new: str, old_label: str, new_label: str) -> dict:
    """{state: 'changed', diff (unified, at most HISTORY_DIFF_MAX_LINES
    lines), diff_lines (the full count), diff_sha256 (of the full diff)}."""
    lines = [_visible(line) for line in difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), old_label, new_label,
        lineterm="\n")]
    return {"state": "changed", "diff": lines[:HISTORY_DIFF_MAX_LINES],
            "diff_lines": len(lines),
            "diff_sha256": _sha256("\n".join(lines).encode("utf-8"))}


def _changelog_history(rel: str, repo: str, parent: str, commit: str, version: str) -> dict:
    """K-109 — the WHOLE changelog history against the release commit's
    parent, read-only (neither the commit nor the file is ever touched):

      - F absent at the parent → {state: 'bootstrap'} (the release commit
        created the file: there is no earlier content to preserve — an
        explicit exception, never reported as preserved);
      - F identical at both → {state: 'unchanged'} (the parent already held
        the section);
      - F at the commit with its V section TAKEN OUT equal, byte for byte,
        to F at the parent (or the commit equal to the parent with that
        section inserted by _insert_section — a file that had no version
        section before) → {state: 'preserved'};
      - anything else — a removed, reordered or edited older section, a
        changed preamble, also a typo fix or a whitespace change — →
        {state: 'changed', diff, diff_lines, diff_sha256}: a deviation the
        caller refuses. Only the V block is ever normalised; the older
        content is compared as bytes.

    The caller has verified that F at the commit holds the V section
    exactly once (_legacy_section)."""
    new, old = (gitinfo.git_read_blob(repo, sha, rel) for sha in (commit, parent))
    if old is None:
        return {"state": "bootstrap"}
    if old == new:
        return {"state": "unchanged"}
    try:
        old_text, new_text = old.decode("utf-8", "strict"), new.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ReleaseRefused(f"{rel} at the parent {parent} is not UTF-8 text — its history "
                             f"cannot be compared") from None
    _, start, end = _version_blocks(new_text, version)[0]
    rest = new_text[:start] + new_text[end:]
    if rest == old_text:
        return {"state": "preserved"}
    try:
        inserted = _insert_section(old_text, _norm_block(new_text[start:end]) + "\n", version)
    except ReleaseRefused:
        inserted = None
    if inserted == new_text:
        return {"state": "preserved"}
    return _history_diff(old_text, rest, f"{rel} at the parent {parent[:12]}",
                         f"{rel} at {commit[:12]} without its {version} section")


def _holds_section(repo: str, commit: str, rel: str, version: str) -> bool:
    """`rel` at `commit` is UTF-8 text holding at least one V section (any
    equivalent label) — read-only."""
    data = gitinfo.git_read_blob(repo, commit, rel)
    try:
        return data is not None and bool(_version_blocks(data.decode("utf-8", "strict"),
                                                         version))
    except UnicodeDecodeError:
        return False


def _candidate_changelog(run: _Run, repo: str, parent: str, commit: str, version: str,
                         contract: list[str] | None, recorded: dict | None) -> str | None:
    """#876 K-12 / K-21 — the changelog file of a HISTORICAL release, from its
    own evidence, never from today's path or detection mode:

      1. the record's VERIFIED manifest (`recorded`, it holds a `tree` — the
         takeover): its changelog path (none when it records none);
      2. dct's changelog file off → None (nothing judged);
      3. else the release COMMIT's evidence: among the files it changed
         inside the project (the confirmed version-file contract's left
         out), the regular Markdown files that hold a V section at the
         commit — exactly ONE is the release's changelog; SEVERAL refuse
         (never guessed);
      4. none changed holds one (the candidate already held the section):
         the files the resolver settles at the parent — with the stored
         path and with detection, whichever the release was made with —
         that hold a V section at the commit; again exactly one, several
         refuse.
    The caller still verifies the section against the database's entries
    and the untouched earlier history (_legacy_section, _changelog_history)
    — finding a section is never enough. ReleaseRefused when no file is
    found."""
    if recorded and recorded.get("tree"):
        return (recorded.get("changelog") or {}).get("path")
    if not run.project.get("manages_changelog", True) or not run.project_root:
        return None
    prefix = _project_prefix(run)
    changed = sorted(p for p in _diff_names(repo, parent, commit)
                     if p.startswith(prefix) and p not in (contract or ())
                     and os.path.splitext(p)[1].casefold() in MARKDOWN_EXTS)
    entries = planning.tree_entries(repo, commit, changed)
    holding = [p for p in changed if entries.get(p, ("",))[0] in ("100644", "100755")
               and _holds_section(repo, commit, p, version)]
    source = "changed by the release commit"
    if not holding:
        settled = set()
        for marker in (bool(run.project.get(EXPLICIT_MARKER)), False):
            found = resolve_changelog({**run.project, EXPLICIT_MARKER: marker},
                                      project_root=run.project_root, repo_root=repo,
                                      commit=parent)
            if found["path"]:
                settled.add(found["path"])
        holding = sorted(p for p in settled if _holds_section(repo, commit, p, version))
        source = "unchanged by the release commit"
    if len(holding) > 1:
        raise ReleaseRefused(
            f"{len(holding)} changelog files {source} hold a {version} section "
            f"({', '.join(holding)}) — which one is the release's changelog is not decided by "
            f"its evidence; refused, never guessed")
    if not holding:
        raise ReleaseRefused(
            f"no Markdown file of the project holds a {version} section at {commit[:12]} — "
            f"dct manages the changelog file, so its release commit must hold the section")
    return holding[0]


def _verify_release_candidate(run: _Run, repo: str, commit: str, version: str,
                              ids: list[int], contract: list[str] | None, *,
                              recorded_manifest: dict | None = None) -> dict:
    """D8 — ONE candidate release commit, verified on its own evidence:
    subject `release: v<V>` (any equivalent spelling), exactly one parent
    (the candidate), the paths it changed besides F = the version-file
    contract (when one is CONFIRMED — `contract` None is an unknown
    contract, K-110: the changed paths are then checked one by one, and
    reported as learned from the commit), F at the commit holding the V
    section once (the database's entries), the REST of F preserved from
    the parent (K-109, _changelog_history — a changed history refuses with
    details.history, its diff), each changed version file passing the D9
    contract against the parent. F is the file of THIS release
    (_candidate_changelog: the `recorded_manifest`'s, else the one the
    commit's own evidence names — K-12 / K-21). → {candidate_sha, manifest, history (None without a
    managed changelog)}; ReleaseRefused (the reason) otherwise."""
    match = _RELEASE_SUBJECT_RE.match(_subject(repo, commit))
    if not match or not _same_label(match.group(1), version):
        raise ReleaseRefused(f"its subject is not `release: v{version}`")
    parents = _parents(repo, commit)
    if len(parents) != 1:
        raise ReleaseRefused(f"it has {len(parents)} parents — the candidate is ambiguous")
    parent = parents[0]
    rel = _candidate_changelog(run, repo, parent, commit, version, contract, recorded_manifest)
    changed = sorted(_diff_names(repo, parent, commit) - {rel})
    if contract is not None and changed != contract:
        raise ReleaseRefused(f"it changed {changed} but the version-file contract is "
                             f"{contract}")
    manifest: dict = {"tree": _tree(repo, commit)}
    history = None
    if rel:
        manifest["changelog"] = _legacy_section(
            run, rel, version, ids, gitinfo.git_read_blob(repo, commit, rel))
        history = _changelog_history(rel, repo, parent, commit, version)
        if history["state"] == "changed":
            raise ReleaseRefused(
                f"it changes earlier changelog history: {rel} at its parent is "
                f"not what remains of {rel} at the commit once the {version} "
                f"section is taken out ({history['diff_lines']} diff line(s)) — a deviation "
                f"(also a typo fix): dct does not attest that the history was preserved",
                details={"history": history})
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
    return {"candidate_sha": parent, "manifest": manifest, "history": history}


# ── the scope of a reconstruction (K-110) ────────────────────────────────────

CONTRACT_PROCEDURE = (
    "show it with `dct release version-files`, then confirm it: `dct release version-files "
    "--set <path>[,<path>…] --confirm` (the files every release changes) or `dct release "
    "version-files --none --confirm` (the project has NO version files)")


def contract_view(contract: dict, *, with_time: bool = False) -> dict:
    """The version-file contract (core get_version_files) as every surface
    SHOWS it (K-110): {version_files (list | null), state} — 'unknown' for
    None (nothing was ever confirmed) and 'confirmed' for a list, also the
    EMPTY one (the owner confirmed that the project has no version files).
    The two are never merged. `with_time` adds confirmed_at (ISO | null)."""
    files = contract["version_files"]
    view = {"version_files": files, "state": "unknown" if files is None else "confirmed"}
    if with_time:
        confirmed = contract.get("confirmed_at")
        view["confirmed_at"] = confirmed.isoformat() if confirmed is not None else None
    return view


def _scope(*, contract: list[str] | None, changelog: str, history: str, version_files: str,
           commit: bool, tag: bool) -> dict:
    """The recorded SCOPE of a reconstruction by dct's own rules (K-110) —
    what was verified, stored as manifest.verification: `contract` (null =
    UNKNOWN, a list = the confirmed contract, [] included), `changelog`
    ('section_verified' | 'not_managed'), `history` ('preserved' |
    'unchanged' | 'bootstrap' | 'not_verifiable' without git | 'not_managed'),
    `version_files` ('verified' against the confirmed contract |
    'from_commit' — the contract is unknown, the files are the ones the
    release commit changed | 'none'), `commit` / `tag` (bool: verified in
    the repository) and `limited` — True when NO artifact content was
    verified (no changelog section, no version file): the database's stamp
    and, in git, the commit are then all the evidence there is."""
    return {"adapter": "dct", "contract": contract, "changelog": changelog,
            "history": history, "version_files": version_files, "commit": commit, "tag": tag,
            "limited": changelog != "section_verified" and version_files == "none"}


def _legacy_identity(engine: sa.Engine, project_slug: str, project: dict,
                     version: str) -> tuple[str, str, dict, list[int]]:
    """(authoritative version, requested version, spelling map, entry ids) of
    a legacy release WITHOUT a record — step 1 of every reconstruction
    (K-86), shared by dct's parser and the own adapter. ReleaseRefused when
    the version already has a record or the database does not prove it."""
    requested = normalize_version(version)
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
    final = normalize_version(spelling["authoritative"])
    try:
        ensure_version_fits(final)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    ids = sorted(e["id"] for e in list_entries(engine, project_slug, version=final))
    return final, requested, spelling["spellings"], ids


def _git_evidence(run: _Run, version: str, ids: list[int], contract: list[str] | None, *,
                  choose: str | None = None, recorded_manifest: dict | None = None) -> dict:
    """The git evidence of a release of `version` (D8, K-109, K-119 M3) —
    used by the reconstruction and by the takeover of an existing record.

    Commits titled `release: v<V>` (any equivalent spelling, any local ref)
    and the commit the tags of V point at are CANDIDATES, each verified on
    its own (_verify_release_candidate). The tags of V must all point at
    ONE commit. With a tag: that commit must verify → status 'completed'.
    Without a tag: ONE verified candidate → 'committed' — the evidence ends
    at the commit; SEVERAL → every verified alternative is listed and the
    choice is explicit (`choose`, a commit id: it must be one of the
    verified candidates, and the tagged one when there is a tag); none →
    refused. Each candidate's changelog file is its own (K-12,
    _candidate_changelog; `recorded_manifest` — the takeover's recorded
    manifest). → {candidate_sha, release_commit_sha, status,
    manifest (tree, changelog, version_files), history, tag, tag_proof,
    candidates
    ([{commit, verified, reason, history}])}; ReleaseRefused with
    details.candidates (and details.history: the candidates whose history
    changed) otherwise."""
    repo = run.repo
    tags = gitinfo.version_tags(repo, version)
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
            verified[sha] = _verify_release_candidate(
                run, repo, sha, version, ids, contract, recorded_manifest=recorded_manifest)
            candidates.append({"commit": sha, "verified": True, "reason": None,
                               "history": verified[sha]["history"]})
        except ReleaseRefused as exc:
            candidates.append({"commit": sha, "verified": False, "reason": str(exc),
                               "history": exc.details.get("history")})
    reasons = "; ".join(f"{c['commit'][:12]}: {c['reason']}" for c in candidates
                        if not c["verified"])
    details: dict = {"candidates": candidates}
    changed = [{"commit": c["commit"], **c["history"]} for c in candidates
               if (c["history"] or {}).get("state") == "changed"]
    if changed:
        details["history"] = changed
    if choose is not None:
        try:
            chosen = _rev(repo, f"{ledger.validate_sha(choose, 'release commit')}^{{commit}}")
        except ValueError as exc:
            raise ReleaseRefused(f"{exc} — refused.", details=details) from None
        if chosen not in verified:
            raise ReleaseRefused(
                f"{choose} is not a verified release commit of {version} (verified: "
                f"{', '.join(sorted(verified)) or 'none'}{'; ' + reasons if reasons else ''}) — "
                f"refused.", details=details)
        if tagged is not None and chosen != tagged:
            raise ReleaseRefused(
                f"the tag(s) {', '.join(t['name'] for t in tags)} of {version} point at "
                f"{tagged}, not at the chosen {chosen} — a contradiction; refused.",
                details={**details, "tags": tags})
        choose = chosen
    if tagged is not None:
        if tagged not in verified:
            raise ReleaseRefused(
                f"the tag(s) {', '.join(t['name'] for t in tags)} of {version} point at "
                f"{tagged}, which is not a verifiable release commit ({reasons}) — refused.",
                details={**details, "tags": tags})
        commit, status = tagged, "completed"
    elif choose is not None:
        commit, status = choose, "committed"
    elif len(verified) == 1:
        commit, status = next(iter(verified)), "committed"
    elif not verified:
        raise ReleaseRefused(
            f"no tag of {version}, and none of the candidates verifies ({len(pool)} "
            f"candidate(s) titled `release: v{version}`{'; ' + reasons if reasons else ''}) — "
            f"refused, never guessed.", details=details)
    else:
        raise ReleaseRefused(
            f"no tag of {version}, and {len(verified)} verified release commits — ambiguous: "
            f"{', '.join(sorted(verified))}. Every one of them is a verified alternative; "
            f"dct never picks one — choose it explicitly: `--release-commit <sha>`"
            f"{'; not verified: ' + reasons if reasons else ''}.", details=details)
    found = verified[commit]
    # K-12: from here on the changelog file is the verified release's own.
    run.changelog_rel = (found["manifest"].get("changelog") or {}).get("path")
    # K-115 G4: the tag identity as verified now — what 'completed' records
    # (None without a tag: the evidence then ends at 'committed').
    tag_proof = _tag_proof(tags, version, commit) if tagged is not None else None
    return {"candidate_sha": found["candidate_sha"], "release_commit_sha": commit,
            "status": status, "manifest": found["manifest"], "history": found["history"],
            "candidates": candidates, "tag_proof": tag_proof,
            "tag": None if tag_proof is None else tag_proof["name"]}


def gather_evidence(engine: sa.Engine, project_slug: str, version: str, *,
                    cwd: str | None = None, release_commit: str | None = None) -> dict:
    """Read-only evidence of a legacy release of `version` WITHOUT a record,
    by dct's OWN rules (K-60, K-86, K-96 D8, K-109, K-110). Refuses
    (ReleaseRefused) on ANY contradiction — never guesses, never modifies a
    commit or a file.

    0. A project whose release executor is 'own' is refused (K-119):
       dct's parser assumes the subject `release: v<V>` and dct's changelog
       layout — its releases go through the own adapter
       (gather_own_evidence, `dct release reconstruct --evidence FILE`).
    1. The AUTHORITATIVE spelling first (K-86, _legacy_identity):
       changelog.version_spelling — the shared resolver core's
       reconstruct_release uses; the requested V is only reported
       (`requested_version`), every comparison is same_version. It must fit
       the schema (K-87). No entry carries V and it is not current_version
       → refused.
    2. The version-file contract is read AS IT IS (K-110): None = UNKNOWN,
       a list = confirmed ([] = confirmed "no version files") — `contract`
       ({version_files, state}).
    3. Git (_git_evidence): the candidates, each verified — subject, one
       parent, F named by that commit's own evidence (K-12 / K-21, never
       today's path or detection mode), the changed paths vs the confirmed contract (an unknown one:
       the changed paths, each checked), the section once with the
       database's entries, the WHOLE older history preserved from the
       parent (K-109), each version file's FIELD / changed lines (J8). A
       tag → 'completed'; one verified commit without a tag → 'committed';
       several → all listed, the choice explicit (`release_commit`).
    4. Without git: an UNKNOWN contract refuses with the procedure that
       shows and confirms it (CONTRACT_PROCEDURE) — nothing is verified
       against a contract nobody confirmed. Then F as found (the section)
       and the confirmed contract's files (field / a line holding V) →
       'completed'; no commit, no tag and no history check exist there.

    Returns {version (authoritative), requested_version, spellings (the
    equivalence map {stored spelling: [entry ids]}), entry_ids,
    candidate_sha, release_commit_sha, tag, tag_proof (K-115 G4: the
    verified identity {name, object, commit, annotated} of the tag at the
    release commit — what the record stores; null without a tag or without
    git), checkout_root, manifest (with `verification` — the recorded
    scope, _scope), verification (the same scope), contract, status,
    without_git, candidates ([{commit, verified, reason, history}], git
    only)}."""
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    if (project.get("release_executor") or ledger.DEFAULT_EXECUTOR) == "own":
        raise ReleaseRefused(
            f"project '{project_slug}' has release executor 'own' — dct's default parser "
            f"assumes dct's own artifact format (a commit titled `release: v<V>`, dct's "
            f"changelog layout) and is not used for it. Its tool supplies the evidence in ITS "
            f"format: `dct release reconstruct --version {version} --evidence <file.json> "
            f"[--confirm]` ({{release_commit_sha, candidate_sha, status, tag, artifacts: "
            f"[{{path, sha256}}]}} — dct verifies each of them in the repository; see "
            f"docs/release-workflow.md, \"Reconstruct\"); nothing recorded.",
            details={"executor": "own"})
    version, requested, spellings, ids = _legacy_identity(engine, project_slug, project,
                                                          version)
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=None)
    stored = ledger.get_version_files(engine, project_slug)
    contract = stored["version_files"]
    evidence = {"version": version, "requested_version": requested,
                "spellings": spellings, "entry_ids": ids, "candidate_sha": None,
                "release_commit_sha": None, "tag": None, "tag_proof": None,
                "checkout_root": None, "without_git": checkout is None, "status": "completed",
                "contract": contract_view(stored)}
    if checkout is None:
        if release_commit is not None:
            raise ReleaseRefused("the project is outside git — there is no release commit to "
                                 "choose; drop --release-commit.")
        if not project_root:
            raise ReleaseRefused("the project has no path — no artifact can be verified")
        # Outside git the files on disk are the only evidence there is.
        _check_changelog_file(run, work_tree=False)
        if contract is None:
            # K-110: unknown is not "none" — nothing is checked against a
            # contract nobody confirmed.
            raise ReleaseRefused(
                f"the version-file contract of '{project_slug}' is UNKNOWN (never confirmed) "
                f"— without git the declared files are all there is to verify, and an unknown "
                f"contract is not a confirmed \"no version files\": {CONTRACT_PROCEDURE}; then "
                f"run the reconstruction again; nothing recorded.",
                details={"contract": evidence["contract"]})
        manifest: dict = {}
        if run.changelog_rel:
            manifest["changelog"] = _legacy_section(
                run, run.changelog_rel, version, ids, _read_bytes(run.abspath(run.changelog_rel)))
        files, bad = [], []
        for rel in contract:
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
        scope = _scope(
            contract=contract,
            changelog="section_verified" if run.changelog_rel else "not_managed",
            history="not_verifiable" if run.changelog_rel else "not_managed",
            version_files="verified" if files else "none", commit=False, tag=False)
        manifest["verification"] = scope
        evidence.update(manifest=manifest, verification=scope)
        return evidence
    found = _git_evidence(run, version, ids, contract, choose=release_commit)
    manifest = found["manifest"]
    if not manifest["version_files"]:
        files_scope = "none"
    else:
        files_scope = "verified" if contract is not None else "from_commit"
    scope = _scope(
        contract=contract,
        changelog="section_verified" if run.changelog_rel else "not_managed",
        history=found["history"]["state"] if found["history"] else "not_managed",
        version_files=files_scope, commit=True, tag=found["tag_proof"] is not None)
    manifest = {**manifest, "verification": scope}
    evidence.update(
        candidate_sha=found["candidate_sha"], release_commit_sha=found["release_commit_sha"],
        checkout_root=run.repo, status=found["status"], manifest=manifest,
        verification=scope, candidates=found["candidates"], tag=found["tag"],
        tag_proof=found["tag_proof"])
    return evidence


# ── the own adapter (K-119) ──────────────────────────────────────────────────

OWN_EVIDENCE_KEYS = frozenset({"release_commit_sha", "candidate_sha", "status", "tag",
                               "artifacts"})
# The stages an own tool may declare; 'pushed' is recorded afterwards
# through the ledger with its push proof (`dct clog release-advance`).
OWN_EVIDENCE_STATUSES = ("committed", "tagged", "completed")


def _tag_identity(repo: str, name: str) -> dict | None:
    """{name, object, commit, annotated} of the tag `name` (ANY name — an
    own tool's tag format is its own), or None when there is no such tag."""
    _, out = gitinfo.git_read(
        repo, "for-each-ref", "--format=%(refname:strip=2)%1f%(objectname)%1f%(*objectname)",
        f"refs/tags/{name}")
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and parts[0] == name:
            return {"name": name, "object": parts[1].lower(),
                    "commit": (parts[2] or parts[1]).lower(), "annotated": bool(parts[2])}
    return None


def _own_artifacts(supplied) -> list[dict]:
    if isinstance(supplied, (str, bytes)) or not isinstance(supplied, (list, tuple)):
        raise ReleaseRefused("evidence.artifacts must be a list of {path, sha256}")
    out = {}
    for item in supplied:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ReleaseRefused(f"evidence.artifacts items must be {{path, sha256}}, got "
                                 f"{item!r}")
        try:
            [path] = ledger.normalize_version_files([item["path"]])
        except (ValueError, TypeError) as exc:
            raise ReleaseRefused(f"evidence.artifacts: {exc}") from None
        digest = item["sha256"]
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ReleaseRefused(f"evidence.artifacts: {path}: sha256 must be a lowercase hex "
                                 f"SHA-256, got {digest!r}")
        if out.setdefault(path, digest) != digest:
            raise ReleaseRefused(f"evidence.artifacts names {path} twice with different hashes")
    return [{"path": path, "sha256": out[path]} for path in sorted(out)]


def gather_own_evidence(engine: sa.Engine, project_slug: str, version: str, supplied: dict, *,
                        cwd: str | None = None) -> dict:
    """The OWN ADAPTER (K-119): the evidence of a legacy release of a
    project whose release executor is 'own', SUPPLIED by its tool in its
    own artifact format and verified here — never parsed by dct's default
    rules (no `release: v<V>` subject, no changelog layout, no version
    field is assumed). Read-only; any contradiction refuses.

    `supplied` (JSON object, unknown keys refused):
      {"release_commit_sha": <sha> (git: required), "candidate_sha": <sha>
      (git: required — the commit the release was cut from), "status":
      'committed' | 'tagged' | 'completed' (git; the stage the tool
      vouches for — without git only 'completed'), "tag": "<tag name>" |
      null (required for 'tagged' / 'completed', refused for 'committed'),
      "artifacts": [{"path": <repo-relative>, "sha256": <hex>}] (the files
      the tool's release produced — its changelog, its version files)}.

    What dct verifies (the format stays the tool's): the version and its
    entry set against the database (_legacy_identity); in git — the
    release commit exists, the candidate is one of its STRICT ancestors,
    every artifact exists AT THE RELEASE COMMIT with exactly that hash,
    the tag exists and points at the release commit (its identity is read
    from the repository — the supplied data never becomes a proof, K-115);
    without git — every artifact on disk with that hash. A push is never
    part of it (recorded later through the ledger with its push proof).

    Returns the shape of gather_evidence (without `contract` /
    `candidates`): manifest {tree (git), artifacts, verification {adapter:
    'own', contract (the declared paths), artifacts ('hash_verified' |
    'none'), commit, tag, limited}}."""
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    if (project.get("release_executor") or ledger.DEFAULT_EXECUTOR) != "own":
        raise ReleaseRefused(
            f"supplied evidence is the adapter of a project whose release executor is 'own' — "
            f"'{project_slug}' is executed by dct, whose releases are verified by dct's own "
            f"rules (drop --evidence); nothing recorded.", details={"executor": "dct"})
    if not isinstance(supplied, dict):
        raise ReleaseRefused("the evidence must be a JSON object")
    unknown = sorted(set(supplied) - OWN_EVIDENCE_KEYS)
    if unknown:
        raise ReleaseRefused(f"the evidence has unknown keys: {', '.join(map(str, unknown))} "
                             f"(known: {', '.join(sorted(OWN_EVIDENCE_KEYS))})")
    version, requested, spellings, ids = _legacy_identity(engine, project_slug, project,
                                                          version)
    artifacts = _own_artifacts(supplied.get("artifacts") or [])
    status = supplied.get("status") or "completed"
    tag_name = supplied.get("tag")
    evidence = {"version": version, "requested_version": requested, "spellings": spellings,
                "entry_ids": ids, "candidate_sha": None, "release_commit_sha": None,
                "tag": None, "tag_proof": None, "checkout_root": None,
                "without_git": checkout is None, "status": status}
    if checkout is None:
        if not project_root:
            raise ReleaseRefused("the project has no path — no artifact can be verified")
        if status != "completed" or tag_name or supplied.get("release_commit_sha") \
                or supplied.get("candidate_sha"):
            raise ReleaseRefused("the project is outside git — the evidence has status "
                                 "'completed' and no commit, candidate or tag")
        bad = []
        for item in artifacts:
            data = _read_regular(os.path.join(project_root, *item["path"].split("/")))
            if not isinstance(data, bytes):
                bad.append(f"{item['path']} is missing")
            elif _sha256(data) != item["sha256"]:
                bad.append(f"{item['path']} has sha256 {_sha256(data)}, not {item['sha256']}")
        if bad:
            raise ReleaseRefused(f"the supplied artifacts are not what the project holds: "
                                 f"{'; '.join(bad)} — refused.", details={"artifacts": bad})
        manifest: dict = {"artifacts": artifacts}
        tagged, tag_proof = False, None
    else:
        repo = checkout["repo_root"]
        if status not in OWN_EVIDENCE_STATUSES:
            raise ReleaseRefused(
                f"evidence.status must be one of {', '.join(OWN_EVIDENCE_STATUSES)}, got "
                f"{status!r} — a push is recorded afterwards through the ledger with its "
                f"proof (`dct clog release-advance … --push-proof`)")
        shas = {}
        for key, what in (("release_commit_sha", "release commit"), ("candidate_sha",
                                                                      "candidate")):
            given = supplied.get(key)
            try:
                valid = ledger.validate_sha(given, key) if isinstance(given, str) else None
            except ValueError:
                valid = None
            resolved = _rev(repo, f"{valid}^{{commit}}") if valid else None
            if resolved is None:
                raise ReleaseRefused(f"the {what} {given!r} (evidence.{key}) is not a commit "
                                     f"of this repository — refused.")
            shas[key] = resolved
        commit, candidate = shas["release_commit_sha"], shas["candidate_sha"]
        if candidate == commit or not gitinfo.git_is_ancestor(repo, candidate, commit):
            raise ReleaseRefused(f"the candidate {candidate} is not an ancestor of the release "
                                 f"commit {commit} — refused.")
        bad = []
        for item in artifacts:
            try:
                data = gitinfo.git_read_blob(repo, commit, item["path"])
            except gitinfo.GitReadError as exc:
                bad.append(f"{item['path']}: {exc}")
                continue
            if data is None:
                bad.append(f"{item['path']} is missing at the release commit")
            elif _sha256(data) != item["sha256"]:
                bad.append(f"{item['path']} has sha256 {_sha256(data)} at the release commit, "
                           f"not {item['sha256']}")
        if bad:
            raise ReleaseRefused(f"the supplied artifacts are not what the release commit "
                                 f"{commit} holds: {'; '.join(bad)} — refused.",
                                 details={"artifacts": bad})
        tag_proof = None
        if status == "committed":
            if tag_name:
                raise ReleaseRefused("evidence.status 'committed' takes no tag — a release "
                                     "with its tag is 'tagged' or 'completed'")
        else:
            if not isinstance(tag_name, str) or not tag_name:
                raise ReleaseRefused(f"evidence.status {status!r} needs the tag of the release "
                                     f"(evidence.tag) — a status is never proof of a tag")
            tag_proof = _tag_identity(repo, tag_name)
            if tag_proof is None:
                raise ReleaseRefused(f"the tag {tag_name} does not exist in this repository — "
                                     f"refused.")
            if tag_proof["commit"] != commit:
                raise ReleaseRefused(
                    f"the tag {tag_name} points at {tag_proof['commit']}, not at the release "
                    f"commit {commit} — refused.", details={"tag": tag_proof})
        manifest = {"tree": _tree(repo, commit), "artifacts": artifacts}
        tagged = tag_proof is not None
        evidence.update(candidate_sha=candidate, release_commit_sha=commit, checkout_root=repo,
                        tag=tag_name if tagged else None, tag_proof=tag_proof)
    scope = {"adapter": "own", "contract": [item["path"] for item in artifacts],
             "artifacts": "hash_verified" if artifacts else "none",
             "commit": checkout is not None, "tag": tagged, "limited": not artifacts}
    manifest["verification"] = scope
    evidence.update(manifest=manifest, verification=scope)
    return evidence


# ── reconstruct: the command ─────────────────────────────────────────────────


def _ways_to_finish(version: str, status: str, executor: str, without_git: bool) -> list[dict]:
    """K-119 M3 — the CONCRETE ways to finish a release whose evidence ends
    before 'completed' (never "do it by hand or abandon"): the controlled
    takeover by dct (a record dct may execute, in git) and the ledger path
    of whoever performs the effects. [] for a completed release."""
    if status == "completed":
        return []
    ways = []
    if executor == "dct" and not without_git:
        ways.append({
            "way": "takeover",
            "command": f"dct release takeover --version {version} [--push-remote R "
                       f"[--push-branch B] [--push-url U] | --no-push]",
            "what": "dct verifies the artifacts again and shows the consent it would record "
                    "(version, entries, candidate, release commit, artifact hashes, push target "
                    "or no push) with its takeover_digest; `… --takeover-digest D --confirm` "
                    "records it — no new bump, no re-stamp, no recorded proof changed — and "
                    "`dct release resume` then performs only the missing effects (the tag, "
                    "the push)"})
    ways.append({
        "way": "ledger",
        "command": f"dct clog release-advance {version} <tagged|pushed|completed> "
                   f"[--tag-proof JSON] [--push-proof JSON]",
        "what": "the project's own tool (or you) performs the missing effects and reports what "
                "it verified; dct performs none of them"})
    return ways


def _reconstruction(action_outcome: str, project_slug: str, project: dict, evidence: dict,
                    record: dict | None) -> dict:
    """The result of a reconstruct call: the evidence, the scope warning
    (K-110), the ways to finish (K-119 M3) and the executor the record gets
    — an ASSIGNED responsibility, not provenance (M9)."""
    executor = project.get("release_executor") or ledger.DEFAULT_EXECUTOR
    warnings = []
    if evidence["verification"]["limited"]:
        proven = "the release commit and the database's stamp" if \
            evidence["release_commit_sha"] else "the database's stamp"
        warnings.append(
            f"limited scope of evidence for {evidence['version']}: no changelog section and "
            f"no version file was verified — only {proven} prove(s) this release; nothing "
            f"else is claimed (see details.evidence.verification)")
    return result(
        "reconstruct", project_slug, action_outcome, version=evidence["version"],
        release=record, warnings=warnings,
        details={"evidence": evidence,
                 "executor": {"assigned": executor, "provenance": False},
                 "finish": _ways_to_finish(evidence["version"], evidence["status"], executor,
                                           evidence["without_git"])})


def _gather(engine, project_slug: str, version: str, *, cwd, release_commit, supplied) -> dict:
    if supplied is not None:
        if release_commit is not None:
            raise ReleaseRefused("--release-commit chooses among dct's own candidates — the "
                                 "supplied evidence names its release commit itself.")
        return gather_own_evidence(engine, project_slug, version, supplied, cwd=cwd)
    return gather_evidence(engine, project_slug, version, cwd=cwd, release_commit=release_commit)


@_conflict_boundary("reconstruct")
def reconstruct(engine: sa.Engine, project_slug: str, version: str, *,
                confirm: bool = False, cwd: str | None = None,
                release_commit: str | None = None, evidence: dict | None = None) -> dict:
    """`dct release reconstruct` (K-60): the evidence of a legacy release
    (read-only) and, only with confirm=True, core reconstruct_release with
    exactly that evidence (no re-stamp, nothing in the repository touched)
    — gathered and recorded under the project operation lock (K-96 D7; the
    preview takes no lock). It performs no effect.

    The ADAPTER is chosen by the project's release executor (K-119): 'dct'
    → dct's own rules (gather_evidence; `release_commit` chooses among
    several verified candidates); 'own' → the evidence its tool supplies
    (`evidence`, gather_own_evidence) — each refuses the other's project.

    Outcome 'preview' (nothing written) or 'reconstructed'; details
    {evidence, executor {assigned, provenance: false} — the executor the
    record gets is an ASSIGNED responsibility for its further handling,
    not who made the historical release (M9) —, finish ([{way, command,
    what}]: how a release whose evidence ends before 'completed' is
    finished — the takeover and the ledger path, K-119 M3; [] when
    completed)}; a `warnings` line for a limited scope (K-110)."""
    project, checkout, _ = _locate(engine, project_slug, cwd)
    if not confirm:
        gathered = _gather(engine, project_slug, version, cwd=cwd,
                           release_commit=release_commit, supplied=evidence)
        return _reconstruction("preview", project_slug, project, gathered, None)
    with _operation(project, checkout, "reconstruct"):
        gathered = _gather(engine, project_slug, version, cwd=cwd,
                           release_commit=release_commit, supplied=evidence)
        try:
            record = _ledger_write(
                engine, project_slug, gathered["version"], "reconstruct",
                lambda: ledger.reconstruct_release(
                    engine, project_slug, gathered["version"],
                    entry_ids=gathered["entry_ids"], candidate_sha=gathered["candidate_sha"],
                    release_commit_sha=gathered["release_commit_sha"],
                    manifest=gathered["manifest"], status=gathered["status"], confirm=True,
                    checkout_root=gathered["checkout_root"],
                    without_git=gathered["without_git"], tag_proof=gathered["tag_proof"]),
                steps=[])
        except (ValueError, TypeError) as exc:
            raise ReleaseRefused(str(exc)) from None
        return _reconstruction("reconstructed", project_slug, project, gathered, record)


# ── the version-file contract: show and confirm (K-110) ──────────────────────


@_conflict_boundary("version-files")
def version_files_contract(engine: sa.Engine, project_slug: str, *,
                           paths: Iterable[str] | None = None, confirm: bool = False) -> dict:
    """`dct release version-files` (K-110) — the procedure that SHOWS and
    CONFIRMS the project's version-file contract (core get_version_files /
    set_version_files): which repo-relative files every release changes.

    `paths` None → show it: outcome 'none' (UNKNOWN — never confirmed) or
    'found' (confirmed, also the EMPTY contract), details {contract
    {version_files, state, confirmed_at}}. `paths` (a list, [] = "the
    project has no version files") without `confirm` → outcome 'preview'
    (nothing written; details add `proposed` and `change`); with
    confirm=True → written under the project operation lock (the earlier
    contract goes to the history ledger): outcome 'confirmed', or 'noop'
    when it already was exactly that. A release approved through `dct
    release prepare` / `apply` learns its contract by itself — this command
    is for a project that needs one WITHOUT such a release (a
    reconstruction outside git)."""
    project = get_project(engine, project_slug)
    if project is None:
        raise ReleaseRefused(f"Project '{project_slug}' not found")
    current = ledger.get_version_files(engine, project_slug)
    if paths is None:
        return result("version-files", project_slug,
                      "none" if current["version_files"] is None else "found",
                      details={"contract": contract_view(current, with_time=True)})
    try:
        proposed = ledger.normalize_version_files(list(paths))
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    if not confirm:
        return result("version-files", project_slug, "preview",
                      details={"contract": contract_view(current, with_time=True),
                               "proposed": proposed,
                               "change": proposed != current["version_files"]})
    with _operation(project, None, "version-files"):
        try:
            out = _ledger_write(
                engine, project_slug, None, "version-files",
                lambda: ledger.set_version_files(engine, project_slug, proposed, confirm=True),
                steps=[])
        except ValueError as exc:
            raise ReleaseRefused(str(exc)) from None
    return result("version-files", project_slug, "confirmed" if out["updated"] else "noop",
                  details={"contract": contract_view(out, with_time=True),
                           "updated": out["updated"]})


# ── the takeover of an existing release (K-119 M3, K-117 J4) ─────────────────

LEDGER_PATH_HINT = (
    "the ledger path stays open: whoever performs the missing effects records what was "
    "verified with `dct clog release-advance <V> <status> [--tag-proof JSON] [--push-proof "
    "JSON]` (MCP advance_release)")


def _proposal_from_artifacts(run: _Run, version: str, candidate: str, commit: str,
                             manifest: dict) -> dict:
    """The version-file proposal of a release that ALREADY exists, derived
    from its verified artifacts (no script ran, nothing is prepared): the
    paths of the manifest's version files, their blobs at the candidate
    (base) and at the release commit (expected), the changelog's baseline
    at the candidate. Without `id` (given when it is recorded)."""
    repo = run.repo
    paths = sorted(item["path"] for item in manifest["version_files"])
    expected = {rel: _blob_at(repo, commit, rel) for rel in paths}
    missing = sorted(rel for rel, blob in expected.items() if blob is None)
    if missing:
        raise run.refuse(f"{', '.join(missing)} are not files of the release commit {commit}")
    changelog = None
    if run.changelog_rel:
        base = gitinfo.git_read_blob(repo, candidate, run.changelog_rel)
        changelog = {"path": run.changelog_rel,
                     "base_blob": _blob_at(repo, candidate, run.changelog_rel),
                     "base_sha256": None if base is None else _sha256(base)}
    return {"paths": paths,
            "base_blobs": {rel: _blob_at(repo, candidate, rel) for rel in paths},
            "expected_blobs": expected, "script": None, "candidate_sha": candidate,
            "version": version, "plan_digest": None, "release_mode": "git",
            "changelog": changelog}


@_conflict_boundary("takeover")
def takeover(engine: sa.Engine, project_slug: str, version: str, *, confirm: bool = False,
             takeover_digest: str | None = None, push_remote: str | None = None,
             push_branch: str | None = None, push_url: str | None = None,
             no_push: bool = False, cwd: str | None = None) -> dict:
    """`dct release takeover` (K-119 M3, K-117 J4) — dct takes over an
    EXISTING release record that has no consent (a reconstruction that
    ended before 'completed', a record stamped through MCP
    release_changelog, an older record), so that `dct release resume` can
    perform the effects that are still missing — and only those.

    What it does NOT do: no new bump, no re-stamp (no entry, no
    current_version), no file written, no commit, tag or push, no change
    of anything the record already holds.

    1. The record: of `version`, not terminal, executor 'dct' (an 'own'
       record stays with its tool — refused), without a consent, its entry
       set exactly the live entries carrying the version, in a git
       checkout (the record's own, when it names one).
    2. The artifacts are verified AGAIN by the rules of the reconstruction
       (_git_evidence: the release commit — the recorded one, when the
       record names it —, one parent, the contract, the section, the whole
       older changelog history, the version files) and compared with the
       record: its candidate, release commit and manifest (tree, changelog,
       version_files) must be what was verified. Nothing verified → nothing
       to take over (the ledger path stays).
    3. The push target is an explicit choice, as for apply (push_target():
       the branch's upstream, `push_remote` / `push_branch` / `push_url`,
       or `no_push` = finish locally); a tag of the version that already
       exists at the release commit is the tag that is pushed.
    4. What the owner approves is SHOWN (confirm=False → outcome 'preview',
       nothing written, no lock): the consent {version, entry_ids,
       candidate_sha, render, push_target, executor 'dct', takeover
       {release_commit_sha, tree, artifacts_sha256}}, the proposal derived
       from the artifacts, the stage the evidence proves, the effects still
       missing, and `takeover_digest` — the SHA-256 of all of it.
    5. confirm=True needs that `takeover_digest`: under the operation lock
       everything is derived again and must give the same digest; then the
       proposal gets its identity, its blobs are pinned (K-117) and core
       take_over_release records consent, proposal and whatever of
       candidate / checkout / release commit / manifest the record lacked,
       in one transaction → outcome 'taken_over'. A record below
       'committed' moves to 'committed' (where the verified evidence
       starts); the tag, the push and the completion are resume's.

    Raises ReleaseRefused (nothing written), ReleaseConflict."""
    if takeover_digest is not None and not _DIGEST_RE.fullmatch(takeover_digest):
        raise ReleaseRefused(
            f"--takeover-digest must be a lowercase hex SHA-256, got {takeover_digest!r}")
    if confirm and takeover_digest is None:
        raise ReleaseRefused(
            "takeover --confirm needs --takeover-digest <takeover_digest of the preview> — run "
            "it without --confirm first and review what dct would record; nothing written.")
    located = _locate(engine, project_slug, cwd)
    push = {"push_remote": push_remote, "push_branch": push_branch, "push_url": push_url,
            "no_push": no_push}
    if not confirm:
        return _takeover(engine, project_slug, located, version, push, None)
    with _operation(located[0], located[1], "takeover"):
        return _takeover(engine, project_slug, located, version, push, takeover_digest)


def _takeover(engine, project_slug: str, located: tuple, version: str, push: dict,
              approved: str | None) -> dict:
    project, checkout, project_root = located
    try:
        requested = normalize_version(version)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=None)
    record = ledger.get_release(engine, project_slug, requested)
    if record is None:
        raise ReleaseRefused(
            f"version {requested} has no release record — nothing to take over (a legacy "
            f"release is recorded first: `dct release reconstruct --version {requested}`).")
    run.record = record
    hint = LEDGER_PATH_HINT.replace("<V>", record["version"])
    if record["status"] in ledger.TERMINAL_STATUSES:
        raise run.refuse(f"release {record['version']} is {record['status']} — nothing is left "
                         f"to take over.")
    executor = record.get("executor") or ledger.DEFAULT_EXECUTOR
    if executor == "own":
        raise run.refuse(
            f"release {record['version']} is executed by the project's own release tool "
            f"(executor 'own', recorded at its stamp) — dct never takes it over; {hint}.",
            executor="own")
    if record.get("consent") is not None:
        raise run.refuse(
            f"release {record['version']} already carries the owner's consent — dct executes "
            f"it: run `dct release resume`; nothing to take over.")
    if checkout is None:
        raise run.refuse(
            f"the project is not in a git checkout — a takeover covers the tag and the push of "
            f"a release in git; {hint}.")
    _check_git_version(run.repo)
    try:
        run.record = record = ledger.verify_release_entry_set(engine, project_slug,
                                                              record["version"])
    except ValueError as exc:
        raise run.refuse(str(exc)) from None
    _check_record_consistency(run)
    here = normalize_worktree(run.repo)
    if record["checkout_root"] and record["checkout_root"] != here:
        raise run.refuse(
            f"release {record['version']} belongs to the checkout {record['checkout_root']}, "
            f"not to {here} — take it over there; the record is never re-pinned.",
            recorded=record["checkout_root"], here=here)
    final = record["version"]
    contract = ledger.get_version_files(engine, project_slug)["version_files"]
    try:
        # K-12: judged at the release's own commit — the recorded manifest's
        # changelog file when the record holds one, never today's HEAD.
        found = _git_evidence(run, final, record["entry_ids"], contract,
                              choose=record["release_commit_sha"],
                              recorded_manifest=record["manifest"])
    except ReleaseRefused as exc:
        raise run.refuse(
            f"release {final} cannot be taken over — no verified release commit: {exc} "
            f"A takeover is derived from VERIFIED artifacts only; {hint}.",
            **exc.details) from None
    candidate, commit = found["candidate_sha"], found["release_commit_sha"]
    artifacts = found["manifest"]
    contradictions = []
    if record["candidate_sha"] and record["candidate_sha"] != candidate:
        contradictions.append(f"the record's candidate is {record['candidate_sha']}, the "
                              f"verified release commit {commit} was cut from {candidate}")
    for key in ledger.ARTIFACT_KEYS:
        recorded = (record["manifest"] or {}).get(key)
        if recorded is not None and recorded != artifacts.get(key):
            contradictions.append(f"the recorded manifest's `{key}` is not what the release "
                                  f"commit {commit} holds")
    if contradictions:
        raise run.refuse(
            f"release {final} cannot be taken over — the verified artifacts contradict the "
            f"record: {'; '.join(contradictions)}. Nothing recorded is ever changed; {hint}.",
            contradictions=contradictions)
    target = push_target(run, final, **push)
    if target is not None and found["tag_proof"] is not None:
        target = {**target, "tag": found["tag_proof"]["name"]}
    proposal = record["proposal"] or _proposal_from_artifacts(run, final, candidate, commit,
                                                              artifacts)
    consent = {
        "version": final, "entry_ids": record["entry_ids"], "candidate_sha": candidate,
        "proposal_sha256": None, "render": record["render"], "push_target": target,
        "executor": ledger.DEFAULT_EXECUTOR,
        "takeover": {"release_commit_sha": commit, "tree": artifacts["tree"],
                     "artifacts_sha256": ledger.artifacts_digest(artifacts)}}
    stage = "tagged" if found["tag_proof"] is not None else "committed"
    missing = ([] if found["tag_proof"] is not None else ["tag"]) + (
        [] if target is None or record["status"] == "pushed" else ["push"])
    shown = {"consent": consent, "proposal": proposal, "stage": stage,
             "tag_proof": found["tag_proof"]}
    digest = _sha256(ledger._canonical(planning._jsonable(shown)).encode("utf-8"))
    details = {
        "consent": consent, "proposal": proposal, "stage": stage,
        "record_status": record["status"],
        "status_after": record["status"] if ledger.RELEASE_FLOW.index(record["status"])
        >= ledger.RELEASE_FLOW.index("committed") else "committed",
        "missing_effects": missing, "takeover_digest": digest,
        "evidence": {"candidate_sha": candidate, "release_commit_sha": commit,
                     "manifest": artifacts, "tag_proof": found["tag_proof"],
                     "candidates": found["candidates"]},
        "executor": {"assigned": ledger.DEFAULT_EXECUTOR, "provenance": False},
    }
    if approved is None:
        return result("takeover", project_slug, "preview", version=final, release=record,
                      details=details)
    if approved != digest:
        raise run.refuse(
            f"--takeover-digest {approved} is not the digest of what dct verifies now "
            f"({digest}) — the record, the artifacts, the tag or the push choice differ from "
            f"the preview that was approved; run `dct release takeover --version {final} …` "
            f"again without --confirm and review it; nothing written.",
            approved_digest=approved, derived_digest=digest)
    recorded_proposal = None
    if record["proposal"] is None:
        # K-117: a stable identity, and its blobs pinned BEFORE the record
        # refers to them.
        recorded_proposal = proposal = {**proposal, "id": new_ulid()}
    consent = {**consent, "proposal_sha256": ledger.proposal_digest(proposal)}
    pin = None
    try:
        if recorded_proposal is not None:
            pin = _pin_proposal(run.repo, project_slug, recorded_proposal)
        out = _ledger_write(
            engine, project_slug, final, "takeover",
            lambda: ledger.take_over_release(
                engine, project_slug, final, consent=consent, proposal=recorded_proposal,
                release_commit_sha=commit, manifest=artifacts, candidate_sha=candidate,
                checkout_root=run.repo, confirm=True), steps=run.steps)
    except Exception as exc:
        # Nothing was recorded (or the write's state is reported by the
        # conflict): the pin of a proposal no record names is let go.
        release_proposal_resources(engine, project_slug, recorded_proposal, run.repo)
        if isinstance(exc, ReleaseRefused) or not isinstance(exc, (ValueError, TypeError)):
            raise
        raise run.refuse(f"{exc} {hint[0].upper()}{hint[1:]}.") from None
    run.record = out["release"]
    run.step("takeover", "done" if out["updated"] else "verified", pin=pin)
    return result("takeover", project_slug, "taken_over", version=final, release=run.record,
                  steps=run.steps,
                  details={**details, "consent": run.record["consent"],
                           "proposal": run.record["proposal"],
                           "status_after": run.record["status"],
                           "next": "dct release resume"})
