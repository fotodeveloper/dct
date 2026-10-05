"""K-88 (+ K-74) — the error boundary of the release commands for a lost
serialization race.

A SerializationConflict / SQLSTATE 40001 (recognised ONLY by the shared
detector db.is_serialization_failure — type and SQLSTATE, never message
text) inside any release command becomes a stable JSON workflow response
with a non-zero exit code (75): the step whose write was rolled back, the
effects the effects layer itself observed (from its own step log — never an
empty report rebuilt in the CLI), and the record as RE-READ afterwards
("unknown" when that read fails — never "nothing written"). Nothing is
retried automatically: not the write, not the sync, commit, tag or push.

The CLI tests run the REAL command line in its own process (`python -m
dct.cli` through a tiny runner that injects the conflict) on a file database
— SQLite here; on the PG lane the same tests run against PostgreSQL, and the
`real_pg` variants produce a GENUINE 40001 (a writer outside the project
lock changes the row after the transaction's snapshot).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
import sqlalchemy as sa
from conftest import approved_plan_digest, approved_proposal_digest, git, push_argv
from test_release_effects import (
    SLUG,
    assert_released,
    make_release_repo,
    proven,
    sync_runs,
)

from dct.core import releases as ledger
from dct.core.changelog import list_entries
from dct.core.releases import get_pending_proposal, get_release
from dct.db import SerializationConflict
from dct.workflows.release import effects
from dct.workflows.release.effects import CONFLICT_EXIT_CODE, ReleaseConflict

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Commits are created by the effects layer."""


# The runner: the real CLI (`dct.cli.main`) with ONE injected conflict. It
# arms itself only inside the targeted write — the stamp (effects'
# release_entries) or the progress write of one status (advance_release) —
# and fires on that transaction's record lookup, i.e. INSIDE the write
# transaction (project_transaction then rolls it back):
#   raise    — the driver error a lost race produces (an OperationalError
#              whose orig carries SQLSTATE 40001), any dialect;
#   real_pg  — a writer OUTSIDE the project lock changes the row this
#              transaction will update, after its REPEATABLE READ snapshot:
#              PostgreSQL itself raises 40001 on the UPDATE.
RUNNER = r'''
import os, sys
import sqlalchemy as sa
from dct import cli
from dct.core import releases
from dct.db import get_engine, projects
from dct.workflows.release import effects

TARGET = os.environ["DCT_TEST_CONFLICT"]
MODE = os.environ.get("DCT_TEST_CONFLICT_MODE", "raise")
state = {"armed": False, "fired": False}


class Lost(Exception):
    sqlstate = "40001"


find = releases.unique_record  # the record lookup of every ledger WRITE


def find_then_conflict(conn, pid, slug, version):
    row = find(conn, pid, slug, version)
    if state["armed"] and not state["fired"]:
        state["fired"] = True
        if MODE == "raise":
            raise sa.exc.OperationalError("injected", {}, Lost("could not serialize access"))
        outsider = get_engine(conn.engine.url.render_as_string(hide_password=False))
        try:
            with outsider.begin() as other:
                if TARGET == "stamp":
                    other.execute(projects.update().where(projects.c.id == pid)
                                  .values(description="changed by a non-participant"))
                else:
                    other.execute(releases.releases.update()
                                  .where(releases.releases.c.id == row.id)
                                  .values(updated_at=row.updated_at))
        finally:
            outsider.dispose()
    return row


releases.unique_record = find_then_conflict

if TARGET == "stamp":
    stamp = effects.release_entries

    def armed_stamp(*args, **kwargs):
        state["armed"] = True
        try:
            return stamp(*args, **kwargs)
        finally:
            state["armed"] = False

    effects.release_entries = armed_stamp
else:
    advance = releases.advance_release

    def armed_advance(engine, slug, version, status, **kwargs):
        state["armed"] = status == TARGET
        try:
            return advance(engine, slug, version, status, **kwargs)
        finally:
            state["armed"] = False

    releases.advance_release = armed_advance

sys.argv = ["dct", *sys.argv[1:]]
cli.main()
'''


def run_dct(engine, repo, tmp_path, *argv: str, conflict: str | None = None,
            mode: str = "raise") -> subprocess.CompletedProcess:
    env = {**os.environ, "DCT_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    if conflict is None:
        cmd = [sys.executable, "-m", "dct.cli", *argv]
    else:
        runner = tmp_path / "conflict_runner.py"
        runner.write_text(RUNNER)
        env.update(DCT_TEST_CONFLICT=conflict, DCT_TEST_CONFLICT_MODE=mode)
        cmd = [sys.executable, str(runner), *argv]
    return subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True,
                          timeout=300, check=False)


@pytest.fixture
def fileproj(concurrent_engine, tmp_path):
    repo, log = make_release_repo(concurrent_engine, tmp_path)
    return concurrent_engine, repo, log


def apply_argv(engine, repo, a) -> list[str]:
    plan = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    proposal = approved_proposal_digest(engine, SLUG, repo, plan_digest=plan, version="1.1.0",
                                        entry_ids=[a])
    return ["release", "apply", "-P", SLUG, "--version", "1.1.0", "--entries", str(a),
            "--plan-digest", plan, "--proposal-digest", proposal, "--confirm", "--json",
            *push_argv(repo)]


MODES = ["raise", "real_pg"]


def _skip_unless(engine, mode):
    if mode == "real_pg" and engine.dialect.name != "postgresql":
        pytest.skip("a genuine 40001 needs PostgreSQL (SQLite's BEGIN IMMEDIATE excludes "
                    "every other writer) — runs on the PG lane")


@pytest.mark.parametrize("mode", MODES)
def test_cli_conflict_at_the_stamp_is_a_json_workflow_response(fileproj, tmp_path, mode):
    engine, repo, log = fileproj
    _skip_unless(engine, mode)
    a = proven(engine, repo, "feature a")
    argv = apply_argv(engine, repo, a)
    pending = get_pending_proposal(engine, SLUG)
    head = git(repo, "rev-parse", "HEAD")

    out = run_dct(engine, repo, tmp_path, *argv, conflict="stamp", mode=mode)

    assert out.returncode == CONFLICT_EXIT_CODE, out.stdout + out.stderr
    assert "Traceback" not in out.stderr
    doc = json.loads(out.stdout)
    assert set(doc) == set(effects.RESULT_KEYS)
    assert (doc["action"], doc["outcome"]) == ("apply", "conflict")
    assert doc["details"]["conflict"] == {"step": "stamp", "status": None}
    assert doc["details"]["observed_effects"] == []
    assert doc["details"]["record_state"] == "absent" and doc["release"] is None
    assert doc["steps"] == [{"step": "stamp", "result": "conflict"}]
    assert "rolled back" in doc["details"]["error"]
    # the rolled-back stamp wrote nothing; the approved proposal is still
    # pending — the same apply can simply run again (never automatically)
    assert get_release(engine, SLUG, "1.1.0") is None
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == a] == [None]
    assert get_pending_proposal(engine, SLUG) == pending
    assert git(repo, "rev-parse", "HEAD") == head and sync_runs(log) == ["1.1.0"]
    again = run_dct(engine, repo, tmp_path, *argv)
    assert again.returncode == 0, again.stdout + again.stderr
    assert_released(engine, repo, head, [a])


@pytest.mark.parametrize("mode", MODES)
def test_cli_conflict_after_the_files_before_their_progress_write(fileproj, tmp_path, mode):
    """The changelog file and the version file ARE written when the
    'files_written' write loses the race: the response lists them (from the
    effects layer's step log) and the re-read record ('stamped'); resume
    then finishes the release without rewriting them or re-running the sync."""
    engine, repo, log = fileproj
    _skip_unless(engine, mode)
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    argv = apply_argv(engine, repo, a)

    out = run_dct(engine, repo, tmp_path, *argv, conflict="files_written", mode=mode)

    assert out.returncode == CONFLICT_EXIT_CODE, out.stdout + out.stderr
    assert "Traceback" not in out.stderr
    doc = json.loads(out.stdout)
    assert doc["outcome"] == "conflict"
    assert doc["details"]["conflict"] == {"step": "files", "status": "files_written"}
    observed = {s["step"]: s for s in doc["details"]["observed_effects"]}
    assert set(observed) == {"stamp", "changelog", "version_files"}
    assert observed["changelog"]["result"] == "done"
    assert observed["version_files"]["written"] == ["pyproject.toml"]
    assert doc["details"]["record_state"] == "read"
    assert doc["release"]["status"] == "stamped" and doc["version"] == "1.1.0"
    assert doc["steps"][-1] == {"step": "files", "result": "conflict"}
    assert "STAY" in doc["details"]["error"]
    # reality matches the report: the files are written, nothing committed
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "stamped" and record["manifest"].keys() == {"untracked_baseline"}
    assert 'version = "1.1.0"' in (repo / "pyproject.toml").read_text()
    assert "## [1.1.0]" in (repo / "changelog.md").read_text()
    assert git(repo, "rev-parse", "HEAD") == candidate
    # no automatic retry; an explicit resume finishes it exactly once
    done = run_dct(engine, repo, tmp_path, "release", "resume", "-P", SLUG, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    steps = {s["step"]: s["result"] for s in json.loads(done.stdout)["steps"]}
    assert steps["changelog"] == "verified" and steps["version_files"] == "verified"
    assert sync_runs(log) == ["1.1.0"]
    assert_released(engine, repo, candidate, [a])


# ── the effects layer itself (in process): the shared detector, the re-read ──


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def _apply(engine, repo, a):
    from test_release_effects import apply

    return apply(engine, repo, version="1.1.0", entry_ids=[a])


class _Lost(Exception):
    sqlstate = "40001"


@pytest.mark.parametrize("error", [
    "domain",     # project_transaction's SerializationConflict
    "driver",     # a raw DBAPIError carrying SQLSTATE 40001 (psycopg 3 `sqlstate`)
])
def test_both_forms_of_a_lost_race_map_to_the_same_conflict_report(engine, proj, monkeypatch,
                                                                    error):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    real = ledger.advance_release

    def lose_committed(eng, slug, version, status, **kw):
        if status == "committed":
            if error == "domain":
                raise SerializationConflict("rolled back")
            raise sa.exc.OperationalError("UPDATE releases", {}, _Lost("serialize"))
        return real(eng, slug, version, status, **kw)

    monkeypatch.setattr(ledger, "advance_release", lose_committed)
    with pytest.raises(ReleaseConflict) as exc:
        _apply(engine, repo, a)
    err = exc.value
    assert err.exit_code == CONFLICT_EXIT_CODE
    assert err.details["conflict"] == {"step": "commit", "status": "committed"}
    observed = [s["step"] for s in err.details["observed_effects"]]
    assert observed == ["stamp", "changelog", "version_files", "commit", "index"]
    assert err.release["status"] == "files_written" and err.details["record_state"] == "read"
    # the commit EXISTS (an effect before the rolled-back write) — reported
    assert git(repo, "log", "-1", "--format=%s") == "release: v1.1.0"


def test_a_failing_re_read_reports_the_state_as_unknown(engine, proj, monkeypatch):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    real_advance, real_get = ledger.advance_release, ledger.get_release
    state = {"lost": False}

    def lose_tagged(eng, slug, version, status, **kw):
        if status == "tagged":
            state["lost"] = True
            raise SerializationConflict("rolled back")
        return real_advance(eng, slug, version, status, **kw)

    def unreadable(*args, **kwargs):
        if state["lost"]:
            raise sa.exc.OperationalError("SELECT", {}, Exception("connection lost"))
        return real_get(*args, **kwargs)

    monkeypatch.setattr(ledger, "advance_release", lose_tagged)
    monkeypatch.setattr(ledger, "get_release", unreadable)
    with pytest.raises(ReleaseConflict) as exc:
        _apply(engine, repo, a)
    err = exc.value
    assert err.details["record_state"] == "unknown" and err.release is None
    assert "UNKNOWN" in str(err) and "connection lost" in err.details["record_error"]
    assert [s["step"] for s in err.details["observed_effects"]][-1] == "tag"


def test_other_database_errors_are_not_reported_as_a_conflict(engine, proj, monkeypatch):
    """Only the shared detector decides — an operational error without
    SQLSTATE 40001 (and a message that merely SAYS "serialize") passes
    unchanged."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    real = ledger.advance_release

    def broken(eng, slug, version, status, **kw):
        if status == "files_written":
            raise sa.exc.OperationalError("UPDATE", {}, Exception("could not serialize access"))
        return real(eng, slug, version, status, **kw)

    monkeypatch.setattr(ledger, "advance_release", broken)
    with pytest.raises(sa.exc.OperationalError):
        _apply(engine, repo, a)


def test_a_conflict_outside_a_step_aware_write_reports_unknown_effects(engine, proj,
                                                                       monkeypatch):
    """The command-level boundary: a 40001 that escaped every step-aware
    write (here: the resume's entry-set read) is still a conflict response —
    with the effects UNKNOWN (null), never claimed as none."""
    from test_release_effects import Interrupted, apply, interrupt_at

    repo, _ = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_files_written"))

    def lost(*args, **kwargs):
        raise SerializationConflict("rolled back")

    monkeypatch.setattr(ledger, "verify_release_entry_set", lost)
    with pytest.raises(ReleaseConflict) as exc:
        effects.resume_release(engine, SLUG, cwd=str(repo))
    err = exc.value
    assert err.details["observed_effects"] is None
    assert err.details["conflict"] == {"step": "resume", "status": None}
    assert err.release["status"] == "files_written"
    assert "unknown" in str(err)
