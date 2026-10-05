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


# ── K-35: separate toplevel / branch readers ──────────────────────────────────


def _failing(real_run, *fragments, exc=None):
    """subprocess.run that fails git calls containing any of `fragments`."""
    def run(cmd, *args, **kwargs):
        if any(f in cmd for f in fragments):
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(cmd, 129, "", "error: unknown option")
        return real_run(cmd, *args, **kwargs)
    return run


def test_git_toplevel_is_one_realpath_process(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    sub = tmp_path / "repo" / "pkg"
    sub.mkdir()
    calls = []
    real_run = subprocess.run

    def counting(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(gitinfo.subprocess, "run", counting)
    assert gitinfo.git_toplevel(sub) == root
    assert len(calls) == 1
    assert gitinfo.git_toplevel(tmp_path) is None


def test_branch_read_failure_keeps_the_known_worktree(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo", branch="feat/x")
    monkeypatch.setattr(gitinfo.subprocess, "run",
                        _failing(subprocess.run, "branch", "symbolic-ref"))
    assert git_context(root) == {"branch": None, "worktree": root}


def test_branch_read_timeout_keeps_the_known_worktree(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo", branch="feat/x")
    monkeypatch.setattr(gitinfo.subprocess, "run", _failing(
        subprocess.run, "branch", "symbolic-ref",
        exc=subprocess.TimeoutExpired("git", 2)))
    assert git_context(root) == {"branch": None, "worktree": root}


def test_branch_falls_back_to_symbolic_ref_when_show_current_is_unsupported(
    tmp_path, monkeypatch
):
    """git < 2.22 has no `branch --show-current`."""
    root = make_git_repo(tmp_path / "repo", branch="feat/old-git")
    monkeypatch.setattr(gitinfo.subprocess, "run", _failing(subprocess.run, "--show-current"))
    assert git_context(root) == {"branch": "feat/old-git", "worktree": root}


def test_symbolic_ref_fallback_on_detached_head_is_none(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    git(root, "checkout", "-q", "--detach")
    monkeypatch.setattr(gitinfo.subprocess, "run", _failing(subprocess.run, "--show-current"))
    assert git_context(root) == {"branch": None, "worktree": root}


def test_worktree_root_runs_exactly_one_git_process(tmp_path, monkeypatch):
    """Codex autoscan needs only the toplevel — no branch read, no 2nd process."""
    root = make_git_repo(tmp_path / "repo")
    calls = []
    real_run = subprocess.run

    def counting(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(gitinfo.subprocess, "run", counting)
    assert codex_hooks._worktree_root(Path(root)) == Path(root)
    assert len(calls) == 1
    assert calls[0][-2:] == ["rev-parse", "--show-toplevel"]


def test_worktree_root_survives_a_failing_branch_read(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(gitinfo.subprocess, "run",
                        _failing(subprocess.run, "branch", "symbolic-ref"))
    assert codex_hooks._worktree_root(Path(root)) == Path(root)


# ── K-34: one checkout resolver (same repository, repo root vs project root) ─


def _wt(main, path, branch):
    git(main, "worktree", "add", "-q", "-b", branch, str(path))
    return os.path.realpath(path)


def test_resolve_checkout_main_checkout_from_a_subdirectory(tmp_path):
    root = make_git_repo(tmp_path / "repo", branch="feat/x")
    sub = tmp_path / "repo" / "src"
    sub.mkdir()
    out = gitinfo.resolve_checkout(root, sub)
    assert out["repo_root"] == out["project_root"] == root
    assert out["branch"] == "feat/x"
    assert out["git_common_dir"] == os.path.realpath(tmp_path / "repo" / ".git")


def test_resolve_checkout_linked_worktree_inside_the_project(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    linked = _wt(root, tmp_path / "repo" / ".worktrees" / "wt", "feat/wt")
    out = gitinfo.resolve_checkout(root, linked)
    assert (out["repo_root"], out["project_root"], out["branch"]) == (linked, linked, "feat/wt")


def test_resolve_checkout_linked_worktree_outside_the_registered_path(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    linked = _wt(root, tmp_path / "elsewhere" / "wt", "feat/out")
    out = gitinfo.resolve_checkout(root, Path(linked) / ".")
    assert (out["repo_root"], out["project_root"], out["branch"]) == (linked, linked, "feat/out")


def test_resolve_checkout_monorepo_subproject_maps_the_relative_path(tmp_path):
    root = make_git_repo(tmp_path / "mono")
    app = tmp_path / "mono" / "packages" / "app"
    app.mkdir(parents=True)
    linked = _wt(root, tmp_path / "wt-mono", "feat/app")
    out = gitinfo.resolve_checkout(str(app), linked)
    assert out["repo_root"] == linked
    assert out["project_root"] == os.path.join(linked, "packages", "app")
    # From the main checkout the project root is the registered subdirectory.
    assert gitinfo.resolve_checkout(str(app), root)["project_root"] == str(app.resolve())


def test_resolve_checkout_rejects_a_nested_foreign_repo(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    nested = make_git_repo(tmp_path / "repo" / "vendor" / "lib")
    assert gitinfo.resolve_checkout(root, nested) is None


def test_resolve_checkout_rejects_a_submodule(tmp_path):
    lib = make_git_repo(tmp_path / "lib")
    root = make_git_repo(tmp_path / "repo")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", lib, "sub")
    sub = os.path.join(root, "sub")
    assert gitinfo.git_toplevel(sub) == sub
    assert gitinfo.resolve_checkout(root, sub) is None


def test_resolve_checkout_rejects_an_unrelated_repo(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    other = make_git_repo(tmp_path / "other")
    assert gitinfo.resolve_checkout(root, other) is None


@pytest.mark.parametrize("registered", [None, "", "missing", "plain"])
def test_resolve_checkout_without_a_git_project_path_is_none(tmp_path, registered):
    root = make_git_repo(tmp_path / "repo")
    (tmp_path / "plain").mkdir()
    path = str(tmp_path / registered) if registered else registered
    assert gitinfo.resolve_checkout(path, root) is None


def test_resolve_checkout_reads_getcwd_only_when_cwd_is_not_given(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")

    def boom():
        raise AssertionError("getcwd must not be read")

    monkeypatch.setattr(gitinfo.os, "getcwd", boom)
    assert gitinfo.resolve_checkout(root, root)["repo_root"] == root
    assert gitinfo.resolve_checkout(None) is None


def test_resolve_checkout_uses_the_process_cwd_by_default(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    assert gitinfo.resolve_checkout(root)["repo_root"] == root


def test_resolve_checkout_deleted_cwd_is_no_context(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    gone = tmp_path / "repo" / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    os.rmdir(gone)
    assert gitinfo.resolve_checkout(root) is None


def test_git_common_dir_is_absolute_from_a_subdirectory(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    sub = tmp_path / "repo" / "a" / "b"
    sub.mkdir(parents=True)
    linked = _wt(root, tmp_path / "wt", "feat/c")
    expected = os.path.realpath(tmp_path / "repo" / ".git")
    assert gitinfo.git_common_dir(sub) == expected
    assert gitinfo.git_common_dir(linked) == expected
    assert gitinfo.git_common_dir(tmp_path) is None
