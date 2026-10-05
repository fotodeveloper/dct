"""Git context of a directory — the ONE place dct asks git where it is (#801).

Readers (each one git process, never raising — failure means "unknown"):
  - git_toplevel(cwd)   — realpath of the checkout containing `cwd` (K-35);
  - git_branch(cwd)     — current branch, None on a detached HEAD or failure;
  - git_common_dir(cwd) — realpath of the repository's shared git dir.
Composites:
  - git_context(cwd)    — {branch, worktree}: changelog branch metadata;
  - resolve_checkout(project_path, cwd) — whether `cwd` is a checkout of the
    SAME repository as a registered project, with its repo root and project
    root (K-34). Used by changelog detection (MCP add_changelog, CLI clog add)
    and by the CLI release's version-sync step.

The Codex hook bridge (autoscan ownership bound to the current worktree) uses
git_toplevel alone — exactly one git process, no branch read.
"""

from __future__ import annotations

import os
import shutil
import subprocess

GIT_TIMEOUT_SECONDS = 2


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
    relative = os.path.relpath(os.path.realpath(project_path), project[0])
    project_root = repo_root if relative == "." else os.path.join(repo_root, relative)
    return {
        "repo_root": repo_root,
        "project_root": project_root,
        "branch": git_branch(target),
        "git_common_dir": common,
    }
