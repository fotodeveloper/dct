"""CLI surface of the durable release record (K-41): `clog release` pins the
checkout's HEAD + root, `clog release-status`, `clog release-advance`."""

import shutil
from pathlib import Path

import pytest
from conftest import git, make_git_repo, run_cli

from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import advance_release, get_release

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _exit_code(monkeypatch, argv) -> int:
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, argv)
    return exc.value.code


@pytest.fixture
def gitproj(cli_engine, tmp_path):
    main = make_git_repo(tmp_path / "repo")
    create_project(cli_engine, slug="proj-g", name="G", path=main, current_version="1.0.0")
    return cli_engine, Path(main)


@needs_git
def test_release_pins_head_and_checkout_root(gitproj, monkeypatch, capsys):
    engine, main = gitproj
    add_entry(engine, "proj-g", "added", "a")
    sub = main / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"])
    rec = get_release(engine, "proj-g", "1.1.0")
    assert rec["candidate_sha"] == git(main, "rev-parse", "HEAD")
    assert rec["checkout_root"] == str(main)
    out = capsys.readouterr().out
    assert "release record 1.1.0: stamped" in out
    assert "not advanced" in out


@needs_git
def test_release_from_a_linked_worktree_pins_that_checkout(gitproj, monkeypatch, tmp_path):
    engine, main = gitproj
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    (wt / "f").write_text("x\n")
    git(wt, "add", "f")
    git(wt, "commit", "-q", "-m", "wt")
    add_entry(engine, "proj-g", "added", "a")
    monkeypatch.chdir(wt)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"])
    rec = get_release(engine, "proj-g", "1.1.0")
    assert rec["checkout_root"] == str(wt.resolve())
    assert rec["candidate_sha"] == git(wt, "rev-parse", "HEAD")
    assert rec["candidate_sha"] != git(main, "rev-parse", "HEAD")


def test_release_of_a_project_without_path_pins_nothing(cli_engine, monkeypatch):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    add_entry(cli_engine, "proj-v", "added", "a")
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "1.1.0"])
    rec = get_release(cli_engine, "proj-v", "1.1.0")
    assert rec["candidate_sha"] is None and rec["checkout_root"] is None


@needs_git
def test_release_in_a_repo_without_commits_is_refused_before_stamping(
        cli_engine, monkeypatch, tmp_path, capsys):
    """K-52: a repository without a first commit is not a project without
    git — there is no HEAD to pin, so nothing is stamped."""
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    create_project(cli_engine, slug="proj-e", name="E", path=str(repo), current_version="1.0.0")
    eid = add_entry(cli_engine, "proj-e", "added", "a")["id"]
    monkeypatch.chdir(repo)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-e", "1.1.0"]) == 1
    assert "no commit" in capsys.readouterr().err
    _assert_nothing_released(cli_engine, "proj-e", eid)


def _assert_nothing_released(engine, slug, eid):
    assert get_project(engine, slug)["current_version"] == "1.0.0"
    assert get_release(engine, slug, "1.1.0") is None
    assert [e["version"] for e in list_entries(engine, slug) if e["id"] == eid] == [None]


def test_release_with_git_missing_is_refused_before_stamping(
        cli_engine, monkeypatch, tmp_path, capsys):
    """K-52: without git a registered directory cannot be CONFIRMED to be
    outside a repository — never silently treated as "no git"."""
    from dct.core import gitinfo

    plain = tmp_path / "plain"
    plain.mkdir()
    create_project(cli_engine, slug="proj-p", name="P", path=str(plain), current_version="1.0.0")
    eid = add_entry(cli_engine, "proj-p", "added", "a")["id"]
    monkeypatch.setattr(gitinfo.shutil, "which", lambda name: None)
    monkeypatch.chdir(plain)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-p", "1.1.0"]) == 1
    err = capsys.readouterr().err
    assert "Cannot verify" in err and "git is not on PATH" in err and "nothing released" in err
    _assert_nothing_released(cli_engine, "proj-p", eid)


def test_release_with_a_failing_git_is_refused_before_stamping(
        cli_engine, monkeypatch, tmp_path, capsys):
    """Exit 128 with any message other than git's "not a git repository"
    (here: dubious ownership) proves nothing — refuse."""
    import os

    from dct.core import gitinfo

    plain = tmp_path / "plain"
    plain.mkdir()
    fake = tmp_path / "git"
    fd = os.open(fake, os.O_WRONLY | os.O_CREAT, 0o755)
    with os.fdopen(fd, "w") as f:
        f.write("#!/bin/bash\necho 'fatal: detected dubious ownership' >&2\nexit 128\n")
    create_project(cli_engine, slug="proj-p", name="P", path=str(plain), current_version="1.0.0")
    eid = add_entry(cli_engine, "proj-p", "added", "a")["id"]
    monkeypatch.setattr(gitinfo.shutil, "which", lambda name: str(fake))
    monkeypatch.chdir(plain)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-p", "1.1.0"]) == 1
    assert "dubious ownership" in capsys.readouterr().err
    _assert_nothing_released(cli_engine, "proj-p", eid)


def test_release_of_a_project_whose_directory_is_gone_is_refused(
        cli_engine, monkeypatch, tmp_path, capsys):
    create_project(cli_engine, slug="proj-p", name="P", path=str(tmp_path / "gone"),
                   current_version="1.0.0")
    eid = add_entry(cli_engine, "proj-p", "added", "a")["id"]
    monkeypatch.chdir(tmp_path)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-p", "1.1.0"]) == 1
    assert "does not exist" in capsys.readouterr().err
    _assert_nothing_released(cli_engine, "proj-p", eid)


def test_release_rerun_reports_the_existing_record(cli_engine, monkeypatch, capsys):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    add_entry(cli_engine, "proj-v", "added", "a")
    release_entries(cli_engine, "proj-v", "1.1.0")
    advance_release(cli_engine, "proj-v", "1.1.0", "committed")
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "1.1.0"])
    out = capsys.readouterr().out
    assert "already released" in out
    assert "release record 1.1.0: committed" in out


def test_release_rerun_with_a_different_set_exits_nonzero(cli_engine, monkeypatch, capsys):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    a = add_entry(cli_engine, "proj-v", "added", "a")["id"]
    add_entry(cli_engine, "proj-v", "added", "b")
    release_entries(cli_engine, "proj-v", "1.1.0", entry_ids=[a])
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-v", "1.1.0"]) == 1
    assert "different set" in capsys.readouterr().err


# ── clog release-status ──────────────────────────────────────────────────────


@pytest.fixture
def recorded(cli_engine):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    a = add_entry(cli_engine, "proj-v", "added", "a")["id"]
    release_entries(cli_engine, "proj-v", "1.1.0", candidate_sha="abcdef0",
                    checkout_root="/tmp/proj-v")
    return cli_engine, a


def test_release_status_by_version(recorded, monkeypatch, capsys):
    _, a = recorded
    run_cli(monkeypatch, ["clog", "release-status", "--version", "1.1.0", "-P", "proj-v"])
    out = capsys.readouterr().out
    assert "1.1.0" in out and "stamped" in out
    assert "abcdef0" in out and "/tmp/proj-v" in out
    assert f"#{a}" in out


def test_release_status_without_version_shows_the_unfinished_one(recorded, monkeypatch, capsys):
    run_cli(monkeypatch, ["clog", "release-status", "-P", "proj-v"])
    assert "1.1.0" in capsys.readouterr().out


def test_release_status_none_unfinished(recorded, monkeypatch, capsys):
    engine, _ = recorded
    advance_release(engine, "proj-v", "1.1.0", "completed")
    run_cli(monkeypatch, ["clog", "release-status", "-P", "proj-v"])
    assert "No unfinished release" in capsys.readouterr().out


def test_release_status_unknown_version(recorded, monkeypatch, capsys):
    run_cli(monkeypatch, ["clog", "release-status", "--version", "9.9.9", "-P", "proj-v"])
    assert "No release record for 9.9.9" in capsys.readouterr().out


def test_release_status_bad_version_exits_nonzero(recorded, monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["clog", "release-status", "--version", "latest",
                                    "-P", "proj-v"]) == 1
    assert "Unsupported version" in capsys.readouterr().err


# ── clog release-advance ─────────────────────────────────────────────────────


def test_release_advance_moves_forward_and_repeat_is_a_noop(recorded, monkeypatch, capsys):
    engine, _ = recorded
    run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", "files_written", "-P", "proj-v"])
    assert "1.1.0: stamped → files_written" in capsys.readouterr().out
    assert get_release(engine, "proj-v", "1.1.0")["status"] == "files_written"
    run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", "files_written", "-P", "proj-v"])
    assert "unchanged" in capsys.readouterr().out


def test_release_advance_repins_sha(recorded, monkeypatch):
    engine, _ = recorded
    run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", "committed", "-P", "proj-v",
                          "--candidate-sha", "1234567"])
    rec = get_release(engine, "proj-v", "1.1.0")
    assert (rec["status"], rec["candidate_sha"]) == ("committed", "1234567")


def test_release_advance_backwards_exits_nonzero(recorded, monkeypatch, capsys):
    engine, _ = recorded
    advance_release(engine, "proj-v", "1.1.0", "tagged")
    assert _exit_code(monkeypatch, ["clog", "release-advance", "1.1.0", "committed",
                                    "-P", "proj-v"]) == 1
    assert "backwards" in capsys.readouterr().err
    assert get_release(engine, "proj-v", "1.1.0")["status"] == "tagged"


def test_release_advance_rejects_unknown_status_at_the_parser(recorded, monkeypatch):
    assert _exit_code(monkeypatch, ["clog", "release-advance", "1.1.0", "shipped",
                                    "-P", "proj-v"]) == 2


def test_release_advance_unknown_version_exits_nonzero(recorded, monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["clog", "release-advance", "9.9.9", "tagged",
                                    "-P", "proj-v"]) == 1
    assert "No release record" in capsys.readouterr().err
