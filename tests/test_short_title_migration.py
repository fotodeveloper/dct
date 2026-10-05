"""items.short_title migration (#817, B1) — convergence and duplicate safety.

CLAUDE.md invariant: a "re-run is a no-op" claim about a schema update MUST be
locked by a test that bootstraps THREE times. The runner always issues a few
healing UPDATEs (legacy-sprint demote, K-42 restore), so "no writes" is not the
claim here — the claim is NO DDL and NO output after the first heal, and no
data loss across bootstraps.

Runs on both lanes: the `engine` fixture is in-memory SQLite by default and
the throwaway PostgreSQL database on the PG lane.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from dct import db
from dct.core.items import create_item, get_item
from dct.core.projects import create_project
from dct.db import create_tables

_DDL_PREFIXES = ("ALTER", "CREATE", "DROP")


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("items")}


def _record_ddl(engine) -> list[str]:
    """Attach a listener collecting every DDL statement sent to the driver."""
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(_DDL_PREFIXES):
            seen.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _listener)
    return seen


def test_three_bootstraps_from_old_schema_heal_once_then_converge(engine, capsys):
    # Simulate a pre-#817 database: the current schema minus the new column.
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE items DROP COLUMN short_title"))
    assert "short_title" not in _columns(engine)

    ddl = _record_ddl(engine)
    create_tables(engine)  # bootstrap 1 — heals
    assert "short_title" in _columns(engine)
    assert any("short_title" in s for s in ddl), ddl
    capsys.readouterr()  # bootstrap 1 output is not the claim

    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    item = create_item(engine, "proj-a", "todo", "A long descriptive title",
                       short_title="Label survives")

    for n in (2, 3):
        ddl.clear()
        create_tables(engine)
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        assert get_item(engine, item["id"])["short_title"] == "Label survives"


def test_helper_on_existing_column_returns_false_and_issues_no_ddl(engine):
    ddl = _record_ddl(engine)
    assert db._add_column_if_missing(engine, "items", "short_title", "VARCHAR(50)") is False
    assert ddl == []


def test_helper_adds_a_missing_column_and_reports_it(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE items DROP COLUMN short_title"))
    assert db._add_column_if_missing(engine, "items", "short_title", "VARCHAR(50)") is True
    assert "short_title" in _columns(engine)
    # And it is idempotent right away.
    assert db._add_column_if_missing(engine, "items", "short_title", "VARCHAR(50)") is False


def _sqlite_only(engine):
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite race path; PostgreSQL uses ADD COLUMN IF NOT EXISTS")


def test_sqlite_helper_survives_losing_the_race(engine, monkeypatch):
    """Another process adds the column between our check and our ALTER: the
    'duplicate column' error is re-checked and treated as success."""
    _sqlite_only(engine)
    real = db._column_exists
    calls = {"n": 0}

    def stale_first_check(eng, table, column):
        calls["n"] += 1
        return False if calls["n"] == 1 else real(eng, table, column)

    monkeypatch.setattr(db, "_column_exists", stale_first_check)
    assert "short_title" in _columns(engine)  # the "other process" already won
    assert db._add_column_if_missing(engine, "items", "short_title", "VARCHAR(50)") is True
    assert calls["n"] == 2  # the error path re-checked instead of trusting the error text


def test_sqlite_helper_reraises_errors_that_are_not_a_lost_race(engine, monkeypatch):
    """A duplicate-column message is only forgiven when the column really
    exists afterwards; any other OperationalError propagates."""
    _sqlite_only(engine)
    monkeypatch.setattr(db, "_column_exists", lambda *a: False)
    with pytest.raises(sa.exc.OperationalError):
        # Column exists, but the re-check (patched) denies it → not a lost race.
        db._add_column_if_missing(engine, "items", "short_title", "VARCHAR(50)")
    with pytest.raises(sa.exc.OperationalError):
        db._add_column_if_missing(engine, "no_such_table", "c", "VARCHAR(5)")
