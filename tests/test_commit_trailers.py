"""Commit-trailer preferences stored with the project.

The owner answers ONCE, at the project's first configuration, whether /commit
adds a `Co-Authored-By` trailer and a session/artifact link; /commit then
applies the stored answer without asking again. Each preference is a
tri-state column on `projects`:

- NULL  — not answered yet (the question was never asked)
- False — a stored "no" (never asked again)
- True  — a stored "yes"

Surfaces: core getter/setter (dct.core.projects), `dct project trailers`,
and the MCP `list_projects` read path /commit already uses.

AGENTS.md invariant: the schema update is locked by a THREE-bootstrap test
(no DDL, no writes to the columns, no output after the first heal).
Runs on both lanes: the `engine` fixture is in-memory SQLite by default and
the throwaway PostgreSQL database on the PG lane.
"""

import re

import pytest
import sqlalchemy as sa
from conftest import run_cli
from sqlalchemy import event

from dct.core.projects import (
    create_project,
    get_commit_trailers,
    get_project,
    set_commit_trailers,
    update_project,
)
from dct.db import create_tables

COLUMNS = ("commit_co_authored_by", "commit_session_link")
NOT_ANSWERED = {"co_authored_by": None, "session_link": None}


def _project_columns(engine) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("projects")}


def _record(engine, pattern: re.Pattern) -> list[str]:
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if pattern.search(statement):
            seen.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _listener)
    return seen


_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML_ON_TRAILERS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE)\b(?=.*\bcommit_(co_authored_by|session_link)\b)",
    re.IGNORECASE | re.DOTALL,
)


# ── schema + migration ───────────────────────────────────────────────────────


def test_fresh_schema_has_both_columns_nullable(engine):
    cols = _project_columns(engine)
    for name in COLUMNS:
        assert name in cols, f"projects.{name} missing"
        assert cols[name]["nullable"] is True


def test_three_bootstraps_from_old_schema_heal_once_then_converge(engine, capsys):
    # A database from before the preferences existed, with a registered project.
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    with engine.begin() as conn:
        for name in COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE projects DROP COLUMN {name}"))
    assert not set(COLUMNS) & set(_project_columns(engine))

    ddl = _record(engine, _DDL)
    create_tables(engine)  # bootstrap 1 — heals
    assert set(COLUMNS) <= set(_project_columns(engine))
    assert all(any(name in s for s in ddl) for name in COLUMNS), ddl
    capsys.readouterr()  # bootstrap 1 output is not the claim

    # No backfill: a project registered earlier has NOT answered — never a
    # guessed "no".
    assert get_commit_trailers(engine, "proj-a") == NOT_ANSWERED

    set_commit_trailers(engine, "proj-a", co_authored_by=False)
    dml = _record(engine, _DML_ON_TRAILERS)
    for n in (2, 3):
        ddl.clear()
        dml.clear()
        create_tables(engine)
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        assert dml == [], f"bootstrap {n} wrote a trailer preference: {dml}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        assert get_commit_trailers(engine, "proj-a") == {
            "co_authored_by": False, "session_link": None,
        }


# ── core getter / setter ─────────────────────────────────────────────────────


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    return engine


def test_a_new_project_has_not_answered(proj):
    assert get_commit_trailers(proj, "proj-a") == NOT_ANSWERED
    row = get_project(proj, "proj-a")
    assert row["commit_co_authored_by"] is None
    assert row["commit_session_link"] is None


def test_a_stored_no_is_distinct_from_not_answered(proj):
    row = set_commit_trailers(proj, "proj-a", co_authored_by=False, session_link=False)
    assert row["commit_co_authored_by"] is False
    assert row["commit_session_link"] is False
    assert get_commit_trailers(proj, "proj-a") == {
        "co_authored_by": False, "session_link": False,
    }


def test_setting_one_preference_leaves_the_other_as_it_was(proj):
    set_commit_trailers(proj, "proj-a", co_authored_by=True)
    assert get_commit_trailers(proj, "proj-a") == {
        "co_authored_by": True, "session_link": None,
    }
    set_commit_trailers(proj, "proj-a", session_link=True)
    assert get_commit_trailers(proj, "proj-a") == {
        "co_authored_by": True, "session_link": True,
    }
    set_commit_trailers(proj, "proj-a", co_authored_by=False)
    assert get_commit_trailers(proj, "proj-a") == {
        "co_authored_by": False, "session_link": True,
    }


def test_setting_the_same_answer_twice_is_idempotent(proj):
    first = set_commit_trailers(proj, "proj-a", co_authored_by=True, session_link=False)
    second = set_commit_trailers(proj, "proj-a", co_authored_by=True, session_link=False)
    assert (first["commit_co_authored_by"], first["commit_session_link"]) == (True, False)
    assert (second["commit_co_authored_by"], second["commit_session_link"]) == (True, False)


def test_unknown_or_deleted_project_returns_none(proj):
    from dct.core.projects import delete_project

    assert set_commit_trailers(proj, "nope", co_authored_by=True) is None
    assert get_commit_trailers(proj, "nope") is None
    delete_project(proj, "proj-a")
    assert set_commit_trailers(proj, "proj-a", co_authored_by=True) is None
    assert get_commit_trailers(proj, "proj-a") is None


def test_no_preference_given_is_refused_and_writes_nothing(proj):
    before = get_project(proj, "proj-a")
    with pytest.raises(ValueError, match="co_authored_by|session_link"):
        set_commit_trailers(proj, "proj-a")
    assert get_project(proj, "proj-a") == before


@pytest.mark.parametrize("bad", ["off", "no", "", 0, 1, "true"])
@pytest.mark.parametrize("key", ["co_authored_by", "session_link"])
def test_a_non_bool_answer_is_refused_and_writes_nothing(proj, key, bad):
    """Only a real True/False is an answer — a truthy string such as "off"
    must never be stored as "yes", and 0/1 never as an answer at all."""
    set_commit_trailers(proj, "proj-a", co_authored_by=True, session_link=False)
    before = get_project(proj, "proj-a")
    with pytest.raises(TypeError, match=key):
        set_commit_trailers(proj, "proj-a", **{key: bad})
    assert get_project(proj, "proj-a") == before


def test_a_refused_pair_writes_neither_half(proj):
    with pytest.raises(TypeError, match="session_link"):
        set_commit_trailers(proj, "proj-a", co_authored_by=True, session_link="yes")
    assert get_commit_trailers(proj, "proj-a") == NOT_ANSWERED


def test_a_rename_keeps_the_stored_answer(proj):
    set_commit_trailers(proj, "proj-a", co_authored_by=False, session_link=True)
    update_project(proj, "proj-a", new_slug="proj-b")
    assert get_commit_trailers(proj, "proj-b") == {
        "co_authored_by": False, "session_link": True,
    }


# ── CLI: dct project trailers ────────────────────────────────────────────────


def test_cli_query_reports_not_answered(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["project", "trailers", "proj-a"])
    out = capsys.readouterr().out
    assert "proj-a" in out
    assert "Co-Authored-By not answered" in out
    assert "session link not answered" in out
    assert get_commit_trailers(cli_engine, "proj-a") == NOT_ANSWERED


def test_cli_sets_both_answers(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["project", "trailers", "proj-a",
                          "--co-authored-by", "on", "--session-link", "off"])
    out = capsys.readouterr().out
    assert "✓ proj-a" in out
    assert "Co-Authored-By → on" in out and "session link → off" in out
    assert get_commit_trailers(cli_engine, "proj-a") == {
        "co_authored_by": True, "session_link": False,
    }
    run_cli(monkeypatch, ["project", "trailers", "proj-a"])
    out = capsys.readouterr().out
    assert "Co-Authored-By on" in out and "session link off" in out


def test_cli_one_flag_leaves_the_other_unanswered(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["project", "trailers", "proj-a", "--session-link", "off"])
    out = capsys.readouterr().out
    assert "session link → off" in out
    assert "Co-Authored-By → not answered" in out
    assert get_commit_trailers(cli_engine, "proj-a") == {
        "co_authored_by": None, "session_link": False,
    }


def test_cli_unknown_project_exits_1(cli_engine, monkeypatch, capsys):
    for argv in (["project", "trailers", "nope"],
                 ["project", "trailers", "nope", "--co-authored-by", "off"]):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, argv)
        assert exc.value.code == 1
        assert "nope" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", "yes", "true", "1"])
def test_cli_rejects_a_value_other_than_on_or_off(cli_engine, monkeypatch, value):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["project", "trailers", "proj-a", "--co-authored-by", value])
    assert exc.value.code == 2
    assert get_commit_trailers(cli_engine, "proj-a") == NOT_ANSWERED


# ── MCP: list_projects is the read path /commit uses ─────────────────────────


def test_mcp_list_projects_exposes_the_stored_answers(srv, engine):
    for name in COLUMNS:
        assert name in srv.PROJECT_COLS
        assert name in (srv.list_projects.__doc__ or "")

    [row] = [r for r in srv.list_projects(format="dict") if r["slug"] == "test"]
    assert (row["commit_co_authored_by"], row["commit_session_link"]) == (None, None)

    set_commit_trailers(engine, "test", co_authored_by=False, session_link=True)
    [row] = [r for r in srv.list_projects(format="dict") if r["slug"] == "test"]
    assert (row["commit_co_authored_by"], row["commit_session_link"]) == (False, True)

    structured = srv.list_projects()
    cols = structured["cols"]
    [r] = [r for r in structured["rows"] if r[cols.index("slug")] == "test"]
    assert r[cols.index("commit_co_authored_by")] is False
    assert r[cols.index("commit_session_link")] is True
