"""Git context of a directory — the ONE place dct asks git where it is (#801).

Used by the changelog writers (branch / worktree an entry was written on), the
CLI release (which checkout's version-sync script to run) and the Codex hook
bridge (autoscan ownership bound to the current worktree).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 2


def _git(git: str, cwd: str, *args: str) -> str | None:
    """stdout of `git -C <cwd> <args>` without its trailing newline, or None.

    The environment drops every GIT_* variable: a hook runs inside git's own
    process tree (GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE are set during a
    commit), and those would make git answer for the repository that invoked
    the hook instead of for `cwd`.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        [git, "-C", cwd, *args],
        env=env, capture_output=True, text=True,
        timeout=GIT_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\r\n")


def git_context(cwd: str | os.PathLike[str]) -> dict | None:
    """`{"branch": str | None, "worktree": str}` for `cwd`, or None.

    - `worktree`: realpath of `git rev-parse --show-toplevel` — the checkout
      (main or linked worktree) that contains `cwd`.
    - `branch`: `git branch --show-current`; a detached HEAD prints nothing
      and yields `branch=None` while `worktree` is kept.

    None when `cwd` is not inside a git work tree, git is not on PATH, a git
    call exceeds GIT_TIMEOUT_SECONDS, or anything else fails — callers treat
    "no git context" as unknown, never as an error.
    """
    git = shutil.which("git")
    if not git:
        return None
    target = os.fspath(cwd)
    try:
        toplevel = _git(git, target, "rev-parse", "--show-toplevel")
        if not toplevel:
            return None
        branch = _git(git, target, "branch", "--show-current")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if branch is None:
        return None
    return {"branch": branch or None, "worktree": str(Path(toplevel).resolve())}
