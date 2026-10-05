"""Strict checkout verification for the MUTATING release path (K-52).

`gitinfo.verify_release_checkout` must tell three outcomes apart:
  - a CONFIRMED repository checkout (full existing HEAD commit required);
  - a CONFIRMED non-repository (git positively says "not a git repository"
    AND no `.git` entry exists on the path or any parent);
  - a verification ERROR — git missing, a timeout, a permission error, a
    deleted directory, a broken repository, a failed HEAD / common-dir read,
    an exit 128 with any other message — which raises, never "no git".

The best-effort readers (git_toplevel & co.) are untouched: they still map
every failure to None (pinned in test_gitinfo.py).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core import gitinfo
from dct.core.gitinfo import (
    CheckoutVerificationError,
    NotACheckoutError,
    verify_release_checkout,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _fake_git(tmp_path: Path, body: str) -> str:
    """An executable stand-in for git (bash) — returns its path."""
    path = tmp_path / "fake-bin" / "git"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w") as f:
        f.write(f"#!/bin/bash\n{body}\n")
    return str(path)


def _use_git(monkeypatch, path: str | None) -> None:
    monkeypatch.setattr(gitinfo.shutil, "which", lambda name: path if name == "git" else None)


# ── confirmed outcomes ───────────────────────────────────────────────────────


@needs_git
def test_confirmed_non_repository_is_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert verify_release_checkout(str(plain), cwd=str(tmp_path)) is None


@needs_git
def test_checkout_with_a_commit_pins_the_full_head(tmp_path):
    main = make_git_repo(tmp_path / "repo")
    sub = Path(main) / "src"
    sub.mkdir()
    out = verify_release_checkout(main, cwd=str(sub))
    assert out["repo_root"] == main and out["project_root"] == main
    assert out["head"] == git(main, "rev-parse", "HEAD")
    assert len(out["head"]) in (40, 64)


@needs_git
def test_linked_worktree_pins_its_own_head(tmp_path):
    main = make_git_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    (wt / "f").write_text("x\n")
    git(wt, "add", "f")
    git(wt, "commit", "-q", "-m", "wt")
    out = verify_release_checkout(main, cwd=str(wt))
    assert out["repo_root"] == str(wt.resolve())
    assert out["head"] == git(wt, "rev-parse", "HEAD") != git(main, "rev-parse", "HEAD")


# ── verification errors — never "no git" ────────────────────────────────────


@needs_git
def test_repository_without_a_first_commit_is_an_error(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    with pytest.raises(CheckoutVerificationError, match="no commit"):
        verify_release_checkout(str(repo), cwd=str(repo))


def test_git_missing_is_an_error_not_a_non_repository(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _use_git(monkeypatch, None)
    with pytest.raises(CheckoutVerificationError, match="git is not on PATH"):
        verify_release_checkout(str(plain), cwd=str(plain))


def test_timeout_is_an_error(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _use_git(monkeypatch, "/usr/bin/git")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=gitinfo.GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(gitinfo.subprocess, "run", _timeout)
    with pytest.raises(CheckoutVerificationError, match="timed out"):
        verify_release_checkout(str(plain), cwd=str(plain))


def test_os_error_launching_git_is_an_error(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _use_git(monkeypatch, str(tmp_path / "no-such-git"))
    with pytest.raises(CheckoutVerificationError, match="could not run git"):
        verify_release_checkout(str(plain), cwd=str(plain))


@pytest.mark.parametrize("missing", ["project", "cwd"])
@needs_git
def test_deleted_directory_is_an_error(tmp_path, missing):
    main = make_git_repo(tmp_path / "repo")
    gone = str(tmp_path / "gone")
    project, cwd = (gone, main) if missing == "project" else (main, gone)
    with pytest.raises(CheckoutVerificationError, match="does not exist"):
        verify_release_checkout(project, cwd=cwd)


@needs_git
def test_deleted_process_cwd_is_an_error(tmp_path, monkeypatch):
    main = make_git_repo(tmp_path / "repo")
    gone = Path(main) / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    os.rmdir(gone)
    with pytest.raises(CheckoutVerificationError, match="no longer exists"):
        verify_release_checkout(main)


def test_exit_128_with_another_message_is_an_error(tmp_path, monkeypatch):
    """Exit code 128 alone never proves "no repository"."""
    plain = tmp_path / "plain"
    plain.mkdir()
    _use_git(monkeypatch, _fake_git(
        tmp_path, "echo 'fatal: detected dubious ownership in repository' >&2; exit 128"))
    with pytest.raises(CheckoutVerificationError, match="dubious ownership"):
        verify_release_checkout(str(plain), cwd=str(plain))


def test_not_a_repository_message_with_a_git_entry_on_the_path_is_an_error(
        tmp_path, monkeypatch):
    """git's own message is not enough while a `.git` exists above the path
    (a broken repository, a ceiling/filesystem boundary) — refuse."""
    root = tmp_path / "proj"
    sub = root / "pkg"
    sub.mkdir(parents=True)
    (root / ".git").write_text("gitdir: /nonexistent/broken\n")
    _use_git(monkeypatch, _fake_git(
        tmp_path, "echo 'fatal: not a git repository (or any of the parent directories): .git' >&2; "
                  "exit 128"))
    with pytest.raises(CheckoutVerificationError, match="not a git repository"):
        verify_release_checkout(str(sub), cwd=str(sub))


@needs_git
def test_broken_repository_is_an_error(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent/broken\n")
    with pytest.raises(CheckoutVerificationError):
        verify_release_checkout(str(broken), cwd=str(broken))


@needs_git
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="POSIX permissions, non-root only")
def test_permission_error_is_an_error(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        with pytest.raises(CheckoutVerificationError):
            verify_release_checkout(str(locked), cwd=str(tmp_path))
    finally:
        os.chmod(locked, 0o755)


def test_failed_head_read_is_an_error(tmp_path, monkeypatch):
    """rev-parse of the location succeeds, the HEAD read fails."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _use_git(monkeypatch, _fake_git(tmp_path, (
        'if [ "$3" = rev-parse ] && [ "$4" = --show-toplevel ]; then '
        f'echo {repo}; echo {repo}/.git; exit 0; fi; '
        "echo 'fatal: unable to read HEAD' >&2; exit 128")))
    with pytest.raises(CheckoutVerificationError, match="no commit|unable to read HEAD"):
        verify_release_checkout(str(repo), cwd=str(repo))


def test_malformed_location_output_is_an_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _use_git(monkeypatch, _fake_git(tmp_path, f"echo {repo}; exit 0"))
    with pytest.raises(CheckoutVerificationError, match="unexpected"):
        verify_release_checkout(str(repo), cwd=str(repo))


def test_abbreviated_head_is_an_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _use_git(monkeypatch, _fake_git(tmp_path, (
        'if [ "$4" = --show-toplevel ]; then '
        f'echo {repo}; echo {repo}/.git; exit 0; fi; echo abc1234; exit 0')))
    with pytest.raises(CheckoutVerificationError, match="full commit"):
        verify_release_checkout(str(repo), cwd=str(repo))


# ── not a checkout of the project's repository ───────────────────────────────


@needs_git
@pytest.mark.parametrize("where", ["other-repo", "plain-dir"])
def test_cwd_outside_the_projects_repository_is_not_a_checkout(tmp_path, where):
    main = make_git_repo(tmp_path / "repo")
    if where == "other-repo":
        cwd = make_git_repo(tmp_path / "other")
    else:
        (tmp_path / "plain").mkdir()
        cwd = str(tmp_path / "plain")
    with pytest.raises(NotACheckoutError):
        verify_release_checkout(main, cwd=cwd)


def test_not_a_checkout_is_a_verification_error():
    """Callers that refuse on any CheckoutVerificationError also refuse here."""
    assert issubclass(NotACheckoutError, CheckoutVerificationError)
