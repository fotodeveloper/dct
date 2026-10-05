"""Round 4 — ONE canonical lock identity for every local release mutation
(K-118 L8): apply, the `clog release` alias, prepare, resume, attest,
abandon, reconstruct --confirm, `clog release-advance`, `clog
release-abandon` all lock the repository of the project's REGISTERED path
(its git common dir; the directory of a project CONFIRMED outside git).
Nothing falls back to another key: a path that cannot be verified, or a
project without a path, is REFUSED — it never proceeds beside a release
that holds the repository's lock.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import git, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    _exit,
    apply,
    interrupt_at,
    make_release_repo,
    proven,
    use_cli,
)

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project
from dct.core.releases import get_release
from dct.workflows.release import effects, oplock
from dct.workflows.release.effects import ReleaseRefused

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

LOCKED = "another dct release operation"


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Every test here may create commits through the effects layer."""


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def repo_lock(repo: Path) -> str:
    return oplock.lock_path(git_common_dir=git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def stamped_by_dct(engine, repo) -> int:
    """An unfinished release dct executes (interrupted right after the stamp)."""
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at("after_stamp"))
    return a


def test_every_mutating_command_takes_the_same_repository_lock(engine, proj, monkeypatch,
                                                               capsys):
    repo, _ = proj
    a = stamped_by_dct(engine, repo)
    b = add_entry(engine, SLUG, "added", "not proven")["id"]
    use_cli((repo, None), engine, monkeypatch)
    calls = {
        "apply": lambda: effects.apply_release(
            engine, SLUG, version="1.2.0", no_entries=True, confirm=True,
            plan_digest="0" * 64, proposal_digest="0" * 64, cwd=str(repo)),
        "alias": lambda: effects.apply_release(
            engine, SLUG, version="1.2.0", strict=True, confirm=True,
            proposal_digest="0" * 64, cwd=str(repo)),
        "prepare": lambda: effects.prepare_release(
            engine, SLUG, version="1.2.0", no_entries=True, plan_digest="0" * 64,
            cwd=str(repo)),
        "resume": lambda: effects.resume_release(engine, SLUG, cwd=str(repo)),
        "attest": lambda: effects.attest(engine, SLUG, [b], confirm=True, cwd=str(repo)),
        "abandon": lambda: effects.abandon(engine, SLUG, "1.1.0", "x", confirm=True),
        "reconstruct --confirm": lambda: effects.reconstruct(
            engine, SLUG, "1.0.0", confirm=True, cwd=str(repo)),
        "release-advance": lambda: effects.record_progress(
            engine, SLUG, "1.1.0", "files_written"),
    }
    cli = {
        "clog release-advance": ["clog", "release-advance", "1.1.0", "files_written", "-P", SLUG],
        "clog release-abandon": ["clog", "release-abandon", "1.1.0", "--reason", "x",
                                 "--confirm", "-P", SLUG],
        "release abandon": ["release", "abandon", "--version", "1.1.0", "--reason", "x",
                            "--confirm", "-P", SLUG],
    }
    with oplock.operation_lock(repo_lock(repo), "someone-else"):
        for what, call in calls.items():
            with pytest.raises(ReleaseRefused, match=LOCKED):
                call()
            assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped", what
        for what, argv in cli.items():
            assert _exit(monkeypatch, argv) == 1, what
            assert LOCKED in capsys.readouterr().err, what
            assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped", what
    assert a in get_release(engine, SLUG, "1.1.0")["entry_ids"]


@pytest.mark.parametrize("command", ["abandon", "release-advance"])
def test_an_unverifiable_checkout_is_refused_never_locked_on_another_key(engine, proj,
                                                                         command):
    """The repository cannot be verified (its HEAD file is garbage) while a
    release holds the repository's lock: abandon / release-advance must not
    run on a key derived from the path string."""
    repo, _ = proj
    stamped_by_dct(engine, repo)
    lock = repo_lock(repo)
    head = repo / ".git" / "HEAD"
    saved = head.read_bytes()
    head.write_bytes(b"garbage\n")
    try:
        with oplock.operation_lock(lock, "apply"), \
                pytest.raises(ReleaseRefused, match="cannot be verified") as exc:
            if command == "abandon":
                effects.abandon(engine, SLUG, "1.1.0", "x", confirm=True)
            else:
                effects.record_progress(engine, SLUG, "1.1.0", "files_written")
        assert "lock" in str(exc.value)
    finally:
        head.write_bytes(saved)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    # Verifiable again: the same command takes the repository's lock and works.
    out = effects.abandon(engine, SLUG, "1.1.0", "x", confirm=True)
    assert out["outcome"] == "abandoned"


@pytest.mark.parametrize("path", [None, "missing"])
def test_a_project_without_a_resolvable_path_is_refused_by_the_ledger_commands(
        engine, tmp_path, path):
    """No path (or a path that does not exist): there is no repository whose
    lock could be taken — refused, never a slug-derived lock."""
    registered = None if path is None else str(tmp_path / "gone")
    create_project(engine, slug="proj-n", name="N", path=registered, current_version="1.0.0")
    add_entry(engine, "proj-n", "added", "a")
    release_entries(engine, "proj-n", "1.1.0")
    for call in (
        lambda: effects.record_progress(engine, "proj-n", "1.1.0", "files_written"),
        lambda: effects.abandon(engine, "proj-n", "1.1.0", "x", confirm=True),
    ):
        with pytest.raises(ReleaseRefused, match="lock") as exc:
            call()
        assert exc.value.details["lock"] is None
    assert get_release(engine, "proj-n", "1.1.0")["status"] == "stamped"
    assert not (tmp_path / "data").exists()


def test_a_project_confirmed_outside_git_locks_its_directory_for_every_command(
        engine, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    create_project(engine, slug="proj-n", name="N", path=str(plain), current_version="1.0.0")
    add_entry(engine, "proj-n", "added", "a")
    release_entries(engine, "proj-n", "1.1.0")
    with oplock.operation_lock(oplock.lock_path(project_path=str(plain)), "someone-else"):
        for call in (
            lambda: effects.record_progress(engine, "proj-n", "1.1.0", "files_written"),
            lambda: effects.abandon(engine, "proj-n", "1.1.0", "x", confirm=True),
            lambda: effects.resume_release(engine, "proj-n", cwd=str(plain)),
            lambda: effects.prepare_release(engine, "proj-n", version="1.2.0", no_entries=True,
                                            plan_digest="0" * 64, cwd=str(plain)),
        ):
            with pytest.raises(ReleaseRefused, match=LOCKED):
                call()
    assert get_release(engine, "proj-n", "1.1.0")["status"] == "stamped"
    out = effects.record_progress(engine, "proj-n", "1.1.0", "files_written")
    assert out["outcome"] == "advanced"


def test_cli_ledger_commands_refuse_a_pathless_project(engine, tmp_path, monkeypatch, capsys):
    create_project(engine, slug="proj-n", name="N", current_version="1.0.0")
    add_entry(engine, "proj-n", "added", "a")
    release_entries(engine, "proj-n", "1.1.0")
    from test_release_effects import use_cli as _use

    _use((tmp_path, None), engine, monkeypatch)
    for argv in (["clog", "release-advance", "1.1.0", "files_written", "-P", "proj-n"],
                 ["clog", "release-abandon", "1.1.0", "--reason", "x", "--confirm", "-P",
                  "proj-n"]):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, argv)
        assert exc.value.code == 1
        assert "no registered path" in capsys.readouterr().err
    assert get_release(engine, "proj-n", "1.1.0")["status"] == "stamped"
