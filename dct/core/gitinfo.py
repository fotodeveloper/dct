"""Git context of a directory — the ONE place dct asks git where it is (#801).

Readers (each one git process, never raising — failure means "unknown"):
  - git_toplevel(cwd)   — realpath of the checkout containing `cwd` (K-35);
  - git_branch(cwd)     — current branch, None on a detached HEAD or failure;
  - git_common_dir(cwd) — realpath of the repository's shared git dir;
  - git_head(cwd)       — full commit id of HEAD (the release candidate, K-41).
Composites:
  - git_context(cwd)    — {branch, worktree}: changelog branch metadata;
  - resolve_checkout(project_path, cwd) — whether `cwd` is a checkout of the
    SAME repository as a registered project, with its repo root and project
    root (K-34). Best-effort: used by changelog detection (MCP add_changelog,
    CLI clog add).
STRICT (raises instead of guessing — the mutating release path only, K-52):
  - verify_release_checkout(project_path, cwd) — a CONFIRMED checkout (with
    its full HEAD commit), a CONFIRMED non-repository (None), or a
    CheckoutVerificationError. Used by the CLI release before it stamps
    and by the release plan (dct.workflows.release).
  - git_read(cwd, *args) / git_is_ancestor(cwd, a, b) / git_read_blob —
    read-only history reads of the proof layer (`--no-optional-locks`,
    bounded timeout); any failure raises GitReadError, never a partial
    answer.
  - git_read_lines(cwd, *args) — the same read STREAMED line by line, for
    output that grows with the history (K-116 H8); a failure or a timeout
    raises GitReadError, an early close stops git.
WRITER (the release effects layer only, dct.workflows.release.effects):
  - git_write(cwd, *args, index_file=None, network=False) — keeps the
    caller's identity / config-location variables (WRITE_ENV_PASSTHROUGH;
    with `network` also the transport ones), may target a temporary index;
    any failure raises GitWriteError.

PROCESSES (K-108, K-113): every STRICT command and both release-layer
helpers run through the supervised runner (dct.core.procs) — git in its own
process group (hooks, filters, transports and whatever they start end with
it: on a timeout, an interruption or an error the whole group is stopped
and reaped before the error is raised), stdin closed, no terminal, output
drained into bounded memory. What a command PRINTS is one of two things:
  - DATA the caller acts on (refs, object ids, history, diffs, blobs — also
    the proof layer's and the push verification's input): read WHOLE, or
    the command is an error once stdout exceeds GIT_OUTPUT_MAX_BYTES
    (64 MiB) — never a cut result; git_read_lines streams line by line
    instead (any size; one line over GIT_LINE_MAX_BYTES is an error);
  - DIAGNOSTIC text (stderr of every command; stdout of git_write(...,
    diagnostic=True) — `git commit` / `git push`, i.e. hook and transport
    chatter): the first and the last GIT_DIAGNOSTIC_MAX_BYTES / 2 bytes,
    the rest counted and dropped.
The best-effort readers at the top (_git: 2 s location probes for changelog
detection and hooks) are not part of the release workflow and keep
subprocess.run.

The Codex hook bridge (autoscan ownership bound to the current worktree) uses
git_toplevel alone — exactly one git process, no branch read.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

from dct.core import procs

GIT_TIMEOUT_SECONDS = 2
# stdout that is DATA is read whole up to this size — past it the command is
# an error (never a cut result). Streamed reads have no total limit, only
# one per line.
GIT_OUTPUT_MAX_BYTES = 64 * 1024 * 1024
GIT_LINE_MAX_BYTES = 1024 * 1024
# DIAGNOSTIC text kept per stream: half from the start, half from the end.
GIT_DIAGNOSTIC_MAX_BYTES = 65536

# A full object id: SHA-1 (40) or SHA-256 (64) lowercase hex.
_FULL_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
# git's own wording for "this directory is outside any repository" (C locale).
_NOT_A_REPOSITORY = "not a git repository"


class CheckoutVerificationError(RuntimeError):
    """The strict resolver could not CONFIRM where it is (K-52): git missing,
    a timeout, a permission error, a deleted directory, a broken repository,
    a failed HEAD / common-dir read, or a repository without a first commit.
    Never means "no git"."""


class NotACheckoutError(CheckoutVerificationError):
    """`cwd` is confirmed NOT to be a checkout of the project's repository
    (outside git, another repository, a nested repo or a submodule)."""


def _git(git: str, cwd: str, *args: str) -> str | None:
    """stdout of `git -C <cwd> <args>` without its trailing newline, or None.

    None on a non-zero exit, a timeout or any OS / subprocess error. The
    environment drops every GIT_* variable: a hook runs inside git's own
    process tree (GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE are set during a
    commit), and those would make git answer for the repository that invoked
    the hook instead of for `cwd`.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            [git, "-C", cwd, *args],
            env=env, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\r\n")


def _git_and_target(cwd: str | os.PathLike[str] | None) -> tuple[str, str] | None:
    git = shutil.which("git")
    if not git or cwd is None:
        return None
    return git, os.fspath(cwd)


def git_toplevel(cwd: str | os.PathLike[str]) -> str | None:
    """Realpath of `git rev-parse --show-toplevel` for `cwd`, or None.

    ONE git process. None when `cwd` is not inside a git work tree (or does
    not exist), git is not on PATH, or the call fails / exceeds
    GIT_TIMEOUT_SECONDS.
    """
    resolved = _git_and_target(cwd)
    if resolved is None:
        return None
    toplevel = _git(*resolved, "rev-parse", "--show-toplevel")
    return os.path.realpath(toplevel) if toplevel else None


def git_branch(cwd: str | os.PathLike[str]) -> str | None:
    """Current branch of the checkout containing `cwd`, or None.

    `git branch --show-current` (git >= 2.22); when that call FAILS (older
    git, unknown option) falls back to `git symbolic-ref -q --short HEAD`.
    A detached HEAD (empty output / symbolic-ref exit 1) and any failure give
    None — never an error.
    """
    resolved = _git_and_target(cwd)
    if resolved is None:
        return None
    branch = _git(*resolved, "branch", "--show-current")
    if branch is None:
        branch = _git(*resolved, "symbolic-ref", "-q", "--short", "HEAD")
    return branch or None


def _absolute_git_path(base: str, value: str) -> str:
    """git prints some paths relative to the `-C` directory — anchor them
    there, then realpath (no dependence on `--path-format`, git >= 2.31)."""
    return os.path.realpath(os.path.join(base, value))


def git_common_dir(cwd: str | os.PathLike[str]) -> str | None:
    """Realpath of `git rev-parse --git-common-dir` for `cwd`, or None.

    The common dir is shared by the main checkout and all its linked
    worktrees, and differs for a nested foreign repository or a submodule —
    the identity of "the same repository".
    """
    resolved = _git_and_target(cwd)
    if resolved is None:
        return None
    common = _git(*resolved, "rev-parse", "--git-common-dir")
    return _absolute_git_path(resolved[1], common) if common else None


def git_head(cwd: str | os.PathLike[str]) -> str | None:
    """`git rev-parse --verify -q HEAD` for `cwd` (full lowercase hex), or None.

    None when `cwd` is not in a work tree, the repository has no commit yet,
    git is missing, or the call fails / times out — never an error.
    """
    resolved = _git_and_target(cwd)
    if resolved is None:
        return None
    head = _git(*resolved, "rev-parse", "--verify", "-q", "HEAD")
    return head.lower() if head else None


def _toplevel_and_common_dir(cwd: str) -> tuple[str, str] | None:
    """(toplevel, common dir) realpaths for `cwd` in ONE git process, or None."""
    resolved = _git_and_target(cwd)
    if resolved is None:
        return None
    out = _git(*resolved, "rev-parse", "--show-toplevel", "--git-common-dir")
    lines = out.splitlines() if out else []
    if len(lines) != 2 or not all(lines):
        return None
    return os.path.realpath(lines[0]), _absolute_git_path(resolved[1], lines[1])


def git_context(cwd: str | os.PathLike[str]) -> dict | None:
    """`{"branch": str | None, "worktree": str}` for `cwd`, or None.

    - `worktree`: git_toplevel(cwd) — the checkout (main or linked worktree)
      that contains `cwd`.
    - `branch`: git_branch(cwd); a detached HEAD or a FAILED branch read gives
      `branch=None` while the known `worktree` is kept (K-35).

    None only when there is no toplevel: `cwd` is not inside a git work tree,
    git is not on PATH, or the toplevel read fails / times out — callers treat
    "no git context" as unknown, never as an error.
    """
    toplevel = git_toplevel(cwd)
    if toplevel is None:
        return None
    return {"branch": git_branch(cwd), "worktree": toplevel}


def _current_directory() -> str | None:
    """os.getcwd(), or None when the process cwd no longer exists (OSError)."""
    try:
        return os.getcwd()
    except OSError:
        return None


def _project_root(project_path: str, project_toplevel: str, repo_root: str) -> str:
    """The project's directory inside the checkout `repo_root`: the project's
    path relative to the toplevel of the checkout that holds it, re-anchored
    (a monorepo subproject maps to the same subdirectory of every worktree)."""
    relative = os.path.relpath(os.path.realpath(project_path), project_toplevel)
    return repo_root if relative == "." else os.path.join(repo_root, relative)


def resolve_checkout(
    project_path: str | None, cwd: str | os.PathLike[str] | None = None
) -> dict | None:
    """The checkout of the project's repository that contains `cwd` (K-34).

    `cwd` defaults to the process cwd, read ONLY here (after the cheap
    project-path check); a deleted cwd (OSError) means no context. `cwd` is a
    checkout of the project when both resolve to the same canonical
    `git rev-parse --git-common-dir` — so the main checkout and every linked
    worktree qualify (also a worktree OUTSIDE the registered path), while a
    nested foreign repository or a submodule inside the project directory
    does not.

    Returns None (no usable context) when `project_path` is empty, is not
    inside a git work tree, `cwd` is not inside one, or the repositories
    differ. Otherwise:
      - `repo_root`: realpath toplevel of the CURRENT checkout;
      - `project_root`: repo_root + the project's path relative to the
        toplevel of the checkout that holds `project_path` (a project
        registered in a monorepo subdirectory maps to the same subdirectory
        of the current worktree);
      - `branch`: git_branch(cwd) (None on a detached HEAD / failed read);
      - `git_common_dir`: the shared realpath.

    This decides "same repository" for an ALREADY KNOWN project; it does not
    identify the project of a worktree outside the registered path (that is
    CWD project auto-detection, a separate problem).
    """
    if not project_path:
        return None
    project = _toplevel_and_common_dir(os.fspath(project_path))
    if project is None:
        return None
    target = os.fspath(cwd) if cwd is not None else _current_directory()
    if target is None:
        return None
    current = _toplevel_and_common_dir(target)
    if current is None or current[1] != project[1]:
        return None
    repo_root, common = current
    return {
        "repo_root": repo_root,
        "project_root": _project_root(os.fspath(project_path), project[0], repo_root),
        "branch": git_branch(target),
        "git_common_dir": common,
    }


# ── strict resolver (K-52) ───────────────────────────────────────────────────


def _git_env(
    keep_env: frozenset[str] = frozenset(), extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The environment of every strict git process: GIT_* variables dropped
    (except the names in `keep_env`), LANGUAGE dropped, `extra_env` added,
    the C locale forced so git's messages can be matched."""
    env = {key: value for key, value in os.environ.items()
           if (not key.startswith("GIT_") or key in keep_env) and key != "LANGUAGE"}
    env.update(extra_env or {})
    env["LC_ALL"] = "C"
    return env


@dataclass(frozen=True)
class GitResult:
    """A finished strict git command: its exit code, stdout (text, or bytes
    with `binary`) and stderr (bounded diagnostic text)."""

    returncode: int
    stdout: str | bytes
    stderr: str


def _diagnostic() -> procs.Capture:
    return procs.bounded(GIT_DIAGNOSTIC_MAX_BYTES // 2, GIT_DIAGNOSTIC_MAX_BYTES // 2)


def _strict_git(
    cwd: str, *args: str, timeout: float = GIT_TIMEOUT_SECONDS,
    error: type[CheckoutVerificationError] = CheckoutVerificationError,
    global_args: tuple[str, ...] = (),
    decode_errors: str = "strict",
    keep_env: frozenset[str] = frozenset(),
    extra_env: dict[str, str] | None = None,
    binary: bool = False,
    diagnostic: bool = False,
) -> GitResult:
    """`git <global_args> -C <cwd> <args>` through the supervised runner
    (dct.core.procs.run — module docstring, PROCESSES) → its GitResult, or
    `error` (a CheckoutVerificationError) when git is missing, cannot be
    started, times out (the whole process group is stopped first), or
    prints more DATA than GIT_OUTPUT_MAX_BYTES. GIT_* variables are dropped
    (see _git) — except the names in `keep_env` — then `extra_env` is
    added, and the C locale is forced so git's messages can be matched.
    `decode_errors` is the stdout decoding policy (an undecodable byte
    under "strict" is reported as `error`); `binary` returns stdout as raw
    bytes; `diagnostic` marks stdout as diagnostic text (bounded, the
    middle dropped) instead of data. stderr is always bounded text."""
    git = shutil.which("git")
    if not git:
        raise error("git is not on PATH — the checkout cannot be verified")
    limit = GIT_OUTPUT_MAX_BYTES
    try:
        # stdin closed, no terminal: no git command dct runs reads stdin, and
        # a hook / credential helper must not wait on the caller's terminal.
        done = procs.run(
            [git, *global_args, "-C", cwd, *args], env=_git_env(keep_env, extra_env),
            timeout=timeout, stdout=_diagnostic() if diagnostic else procs.complete(limit),
            stderr=_diagnostic())
    except procs.TimedOut as exc:
        raise error(f"git timed out after {timeout}s in {cwd} (stopped with its whole "
                    f"process group)") from exc
    except procs.OutputLimitExceeded as exc:
        raise error(
            f"`git {' '.join(args)}` in {cwd} printed more than the limit of {limit} bytes "
            f"(GIT_OUTPUT_MAX_BYTES) — its output is data that is never used cut; "
            f"refused") from exc
    except procs.ProcessError as exc:
        raise error(f"could not run git in {cwd}: {exc}") from exc
    try:
        if binary:
            stdout: str | bytes = done.stdout.data
        elif diagnostic:
            stdout = done.stdout.text(decode_errors)
        else:
            stdout = done.stdout.data.decode("utf-8", decode_errors)
    except ValueError as exc:
        raise error(f"could not run git in {cwd}: {exc}") from exc
    return GitResult(done.returncode, stdout, done.stderr.text())


def _git_entry_on_path(path: str) -> str | None:
    """The first `.git` entry (file, directory or link) at `path` or above."""
    current = os.path.realpath(path)
    while True:
        candidate = os.path.join(current, ".git")
        if os.path.lexists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _strict_location(path: str, what: str) -> tuple[str, str] | None:
    """(toplevel, common dir) realpaths of the work tree holding `path`, None
    when `path` is CONFIRMED to be outside every repository, else
    CheckoutVerificationError.

    Confirmed non-repository = git fails with its own "not a git repository"
    message AND no `.git` entry exists on the path or any parent. An exit
    code (128) or a message alone proves nothing: a broken `.git`, a
    filesystem boundary, a permission or ownership problem all fail too.
    """
    if not os.path.isdir(path):
        raise CheckoutVerificationError(f"{what} {path} does not exist or is not a directory")
    result = _strict_git(path, "rev-parse", "--show-toplevel", "--git-common-dir")
    detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
    if result.returncode == 0:
        lines = result.stdout.splitlines()
        if len(lines) != 2 or not all(lines):
            raise CheckoutVerificationError(
                f"unexpected `git rev-parse` output for {what} {path}: {result.stdout!r}")
        return os.path.realpath(lines[0]), _absolute_git_path(path, lines[1])
    if _NOT_A_REPOSITORY in detail.lower():
        entry = _git_entry_on_path(path)
        if entry is None:
            return None
        raise CheckoutVerificationError(
            f"git reports {what} {path} as {detail!r}, but {entry} exists — "
            f"broken or unreachable repository")
    raise CheckoutVerificationError(f"git could not verify {what} {path}: {detail}")


def verify_release_checkout(
    project_path: str, cwd: str | os.PathLike[str] | None = None
) -> dict | None:
    """STRICT checkout resolution for a mutating release (K-52).

    - `project_path` CONFIRMED outside git (see _strict_location) → None: the
      documented explicit path of a project without git.
    - `project_path` inside a work tree: `cwd` (default: the process cwd)
      must be a checkout of the SAME repository (same git common dir — main
      checkout or any linked worktree), else NotACheckoutError; its HEAD
      must be a full existing commit id (a repository without a first
      commit is an error, never "no git"). Returns {repo_root, project_root,
      head, git_common_dir} (project_root as in resolve_checkout).
    - Anything that cannot be CONFIRMED — git missing, timeout, permission
      error, deleted directory (also a deleted process cwd), broken
      repository, failed common-dir / HEAD read — raises
      CheckoutVerificationError. Never falls back to the main checkout.
    """
    project = _strict_location(os.fspath(project_path), "project path")
    if project is None:
        return None
    target = os.fspath(cwd) if cwd is not None else _current_directory()
    if target is None:
        raise CheckoutVerificationError("the current directory no longer exists")
    current = _strict_location(target, "current directory")
    if current is None or current[1] != project[1]:
        raise NotACheckoutError(
            f"{target} is not a checkout of the repository at {project_path}")
    repo_root, common = current
    result = _strict_git(target, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    head = result.stdout.strip().lower() if result.returncode == 0 else ""
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise CheckoutVerificationError(
            f"the checkout {repo_root} has no commit to release from "
            f"(HEAD unreadable{': ' + detail if detail else ''})")
    if not _FULL_SHA_RE.fullmatch(head):
        raise CheckoutVerificationError(
            f"HEAD of {repo_root} is not a full commit id: {head!r}")
    return {
        "repo_root": repo_root,
        "project_root": _project_root(os.fspath(project_path), project[0], repo_root),
        "head": head,
        "git_common_dir": common,
    }


# ── strict read-only readers (the release proof layer, K-72 layer 1) ────────

# History reads (log over a release range, rev-list per merge) can take far
# longer than the 2 s location probes; still bounded so a plan never hangs.
GIT_READ_TIMEOUT_SECONDS = 60


class GitReadError(CheckoutVerificationError):
    """A read-only git command the release plan needs failed or timed out —
    the plan is refused instead of being computed on partial history."""


def git_read(
    cwd: str | os.PathLike[str], *args: str,
    ok_codes: tuple[int, ...] = (0,), timeout: float = GIT_READ_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """(exit code, stdout) of a READ-ONLY `git --no-optional-locks -C <cwd>
    <args>` (no shell, GIT_* stripped, C locale). An exit code outside
    `ok_codes`, git missing, or a timeout raises GitReadError with git's
    stderr. stdout is returned verbatim and WHOLE (NUL / control separators
    kept; bytes that are not UTF-8 — a legacy-encoded commit message —
    become U+FFFD instead of failing the read); output over
    GIT_OUTPUT_MAX_BYTES is a GitReadError, never a cut result (K-113).
    Callers pass only read commands — this helper never writes, and
    `--no-optional-locks` keeps git from refreshing the index on the way (a
    plain `git status` may rewrite it to update its stat cache)."""
    result = _strict_git(
        os.fspath(cwd), *args, timeout=timeout, error=GitReadError,
        global_args=("--no-optional-locks",), decode_errors="replace",
    )
    if result.returncode not in ok_codes:
        detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
        raise GitReadError(f"`git {' '.join(args)}` failed in {os.fspath(cwd)}: {detail}")
    return result.returncode, result.stdout


# How much of a failed streamed read's stderr is quoted in its error.
_STREAM_STDERR_MAX_BYTES = 8192


def git_read_lines(
    cwd: str | os.PathLike[str], *args: str, timeout: float = GIT_READ_TIMEOUT_SECONDS,
) -> Iterator[str]:
    """The stdout LINES of a READ-ONLY `git --no-optional-locks -C <cwd>
    <args>`, STREAMED (K-116 H8) — for output whose size grows with the
    history (`rev-list --parents`), which git_read would hold in memory
    whole. Same environment and decoding as git_read (GIT_* stripped, C
    locale, undecodable bytes → U+FFFD); lines come without their newline.

    Never a silently partial result: the generator finishes normally ONLY
    after git exited 0. A non-zero exit, git missing, `timeout` seconds
    since the start, or one line longer than GIT_LINE_MAX_BYTES raises
    GitReadError — after the lines already yielded, so a caller must not
    use them when the iteration raised. A consumer that stops early
    (`close()`, e.g. to refuse past its own cap) stops git. In every case
    git's whole process group is stopped and reaped (dct.core.procs.
    stream_lines — the ONE process seam of the streamed readers)."""
    cwd = os.fspath(cwd)
    git = shutil.which("git")
    if not git:
        raise GitReadError("git is not on PATH — the checkout cannot be verified")
    command = " ".join(args)
    lines = procs.stream_lines(
        [git, "--no-optional-locks", "-C", cwd, *args], env=_git_env(), timeout=timeout,
        max_line_bytes=GIT_LINE_MAX_BYTES, stderr=procs.bounded(_STREAM_STDERR_MAX_BYTES))
    try:
        for raw in lines:
            yield raw.decode("utf-8", "replace")
    except procs.TimedOut as exc:
        raise GitReadError(f"`git {command}` timed out after {timeout}s in {cwd}") from exc
    except procs.Failed as exc:
        raise GitReadError(f"`git {command}` failed in {cwd}: {exc}") from exc
    except procs.OutputLimitExceeded as exc:
        raise GitReadError(
            f"`git {command}` in {cwd} printed a line over the limit of {exc.limit} bytes "
            f"(GIT_LINE_MAX_BYTES) — refused, nothing of its output is used") from exc
    except procs.ProcessError as exc:
        raise GitReadError(f"could not run git in {cwd}: {exc}") from exc
    finally:
        lines.close()


def git_is_ancestor(cwd: str | os.PathLike[str], ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor`: True (exit 0) / False (exit 1); any
    other outcome raises GitReadError."""
    code, _ = git_read(cwd, "merge-base", "--is-ancestor", ancestor, descendant, ok_codes=(0, 1))
    return code == 0


def git_read_blob(cwd: str | os.PathLike[str], commit: str, path: str) -> bytes | None:
    """The EXACT bytes of `path` (repo-root-relative, forward slashes) in
    `commit`, or None when the commit's tree has no such file. Read-only
    (`--no-optional-locks`); any other failure raises GitReadError."""
    cwd = os.fspath(cwd)
    _, out = git_read(cwd, "ls-tree", "-z", commit, "--", path)
    entry = next((e for e in out.split("\0") if e), None)
    if entry is None:
        return None
    meta, _, name = entry.partition("\t")
    parts = meta.split()
    if name != path or len(parts) != 3 or parts[1] != "blob":
        raise GitReadError(f"{path} in {commit} is not a regular file ({meta!r})")
    result = _strict_git(cwd, "cat-file", "blob", parts[2], error=GitReadError,
                         timeout=GIT_READ_TIMEOUT_SECONDS,
                         global_args=("--no-optional-locks",), binary=True)
    if result.returncode != 0:
        raise GitReadError(
            f"`git cat-file blob {parts[2]}` failed in {cwd}: {result.stderr.strip()}")
    return result.stdout


def git_cat_blob(cwd: str | os.PathLike[str], blob: str, *, path: str | None = None) -> bytes:
    """The bytes of the blob object `blob` — raw (`git cat-file blob`), or,
    with `path` (repo-root-relative), as the WORK TREE holds them (`git
    cat-file --filters --path=<path>`: smudge / eol filters of that path
    applied, as a checkout would write it). Read-only; GitReadError when
    the object is missing or the read fails."""
    cwd = os.fspath(cwd)
    args = ("cat-file", "--filters", f"--path={path}", blob) if path else ("cat-file", "blob", blob)
    result = _strict_git(cwd, *args, error=GitReadError, timeout=GIT_READ_TIMEOUT_SECONDS,
                         global_args=("--no-optional-locks",), binary=True)
    if result.returncode != 0:
        raise GitReadError(f"`git {' '.join(args)}` failed in {cwd}: {result.stderr.strip()}")
    return result.stdout


# ── the release EFFECTS layer's writer (K-72 layer 3) ────────────────────────

# Identity / config-location variables a WRITE keeps: the commit the effects
# layer creates carries the caller's identity (GIT_AUTHOR_* / GIT_COMMITTER_*)
# and honours the caller's config location. Repository-location variables
# (GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE, …) are still dropped — a writer is
# told explicitly which index it works on (`index_file`).
WRITE_ENV_PASSTHROUGH = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
})


# A git command that talks to a REMOTE (the release push and its
# `ls-remote` verification, K-97 F2) additionally keeps the caller's
# transport configuration; GIT_TERMINAL_PROMPT=0 makes a missing credential
# a failure instead of a prompt nobody answers.
NETWORK_ENV_PASSTHROUGH = frozenset({
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_ASKPASS", "GIT_PROXY_COMMAND",
    "GIT_SSL_CAINFO", "GIT_SSL_NO_VERIFY", "GIT_HTTP_PROXY_AUTHMETHOD",
})


class GitWriteError(CheckoutVerificationError):
    """A git command of the release effects layer failed or timed out."""


def git_write(
    cwd: str | os.PathLike[str], *args: str,
    ok_codes: tuple[int, ...] = (0,), timeout: float = GIT_READ_TIMEOUT_SECONDS,
    index_file: str | None = None, network: bool = False, diagnostic: bool = False,
) -> tuple[int, str]:
    """(exit code, stdout) of a git command that MAY write (`git -C <cwd>
    <args>`, no shell, C locale, stdin closed, no terminal) — the release
    effects layer only (dct.workflows.release.effects). GIT_* variables are
    dropped except WRITE_ENV_PASSTHROUGH (+ NETWORK_ENV_PASSTHROUGH and
    GIT_TERMINAL_PROMPT=0 with `network`); `index_file` sets GIT_INDEX_FILE
    (a TEMPORARY index — the user's index is never touched through it; a
    `git commit` exports it to the repository's hooks). An exit code outside
    `ok_codes`, git missing, or a timeout raises GitWriteError with git's
    stderr (bounded: its start and its end).

    Supervised (K-108 / K-118 L10): hooks, filters and transports git
    starts are in its process group — a timeout or an interruption stops
    them all before GitWriteError / the interruption is raised. stdout is
    DATA by default (whole, or GitWriteError past GIT_OUTPUT_MAX_BYTES —
    `ls-remote`, `hash-object`, `write-tree`); `diagnostic=True` is for
    commands whose stdout only carries hook / transport chatter (`commit`,
    `push`): bounded, never an error for its size (K-113)."""
    extra = {"GIT_INDEX_FILE": index_file} if index_file else {}
    keep = WRITE_ENV_PASSTHROUGH
    if network:
        keep = keep | NETWORK_ENV_PASSTHROUGH
        extra["GIT_TERMINAL_PROMPT"] = "0"
    result = _strict_git(
        os.fspath(cwd), *args, timeout=timeout, error=GitWriteError,
        decode_errors="replace", keep_env=keep, extra_env=extra or None,
        diagnostic=diagnostic,
    )
    if result.returncode not in ok_codes:
        detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
        raise GitWriteError(f"`git {' '.join(args)}` failed in {os.fspath(cwd)}: {detail}")
    return result.returncode, result.stdout
