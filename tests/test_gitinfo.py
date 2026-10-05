"""git_context (#801, C2) against real throwaway repositories, plus the Codex
hook bridge's _worktree_root, which now delegates to it."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core import gitinfo
from dct.core.gitinfo import git_context
from dct.integrations import codex_hooks

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def test_normal_branch_reports_branch_and_realpath_toplevel(tmp_path):
    root = make_git_repo(tmp_path / "repo", branch="feat/x")
    sub = tmp_path / "repo" / "pkg" / "deep"
    sub.mkdir(parents=True)
    assert git_context(sub) == {"branch": "feat/x", "worktree": root}


def test_detached_head_keeps_worktree_and_nulls_branch(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    git(root, "checkout", "-q", "--detach")
    assert git_context(root) == {"branch": None, "worktree": root}


def test_linked_worktree_reports_its_own_toplevel_and_branch(tmp_path):
    main = make_git_repo(tmp_path / "repo")
    linked = tmp_path / "repo" / ".worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat/wt", str(linked))
    ctx = git_context(linked)
    assert ctx == {"branch": "feat/wt", "worktree": os.path.realpath(linked)}
    # The main checkout still answers for itself.
    assert git_context(main) == {"branch": "main", "worktree": main}


def test_symlinked_path_resolves_to_the_physical_toplevel(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    link = tmp_path / "alias"
    link.symlink_to(tmp_path / "repo")
    assert git_context(link)["worktree"] == root


def test_non_repo_directory_is_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_context(plain) is None


def test_missing_directory_is_none(tmp_path):
    assert git_context(tmp_path / "does-not-exist") is None


def test_git_missing_from_path_is_none(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert git_context(root) is None


def test_timeout_is_none(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(gitinfo.subprocess, "run", slow)
    assert git_context(root) is None


def test_git_env_of_an_enclosing_repo_is_ignored(tmp_path, monkeypatch):
    """A hook runs inside git's process tree with GIT_DIR / GIT_WORK_TREE set
    for the committing repo; git_context must still answer for `cwd`."""
    other = make_git_repo(tmp_path / "other", branch="other-branch")
    target = make_git_repo(tmp_path / "target", branch="feat/x")
    monkeypatch.setenv("GIT_DIR", str(Path(other) / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", other)
    assert git_context(target) == {"branch": "feat/x", "worktree": target}


# ── Codex hook bridge regression (autoscan ownership) ─────────────────────────


def test_worktree_root_is_the_linked_worktree_not_the_main_checkout(tmp_path):
    main = make_git_repo(tmp_path / "repo")
    linked = tmp_path / "repo" / ".worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat/wt", str(linked))
    sub = linked / "src"
    sub.mkdir()
    assert codex_hooks._worktree_root(sub) == Path(os.path.realpath(linked))


def test_worktree_root_on_detached_head_is_still_the_toplevel(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    git(root, "checkout", "-q", "--detach")
    assert codex_hooks._worktree_root(Path(root)) == Path(root)


def test_worktree_root_outside_git_falls_back_to_cwd(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert codex_hooks._worktree_root(plain) == plain.resolve()
