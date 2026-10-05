"""changelog_entries.branch / worktree / branch_source migration (#801, C1).

Same claim as test_short_title_migration: the first bootstrap of a pre-#801
database adds the three columns; the second and third issue NO DDL, print
nothing and keep the data. Runs on both lanes (SQLite default, PG lane).
"""

import sqlalchemy as sa
from sqlalchemy import event

from dct.core.changelog import add_entry, list_entries
from dct.core.projects import create_project
from dct.db import create_tables

_NEW = ("branch", "worktree", "branch_source")
_DDL_PREFIXES = ("ALTER", "CREATE", "DROP")


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("changelog_entries")}


def _record_ddl(engine) -> list[str]:
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(_DDL_PREFIXES):
            seen.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _listener)
    return seen


def _drop_new_columns(engine) -> None:
    for column in _NEW:
        with engine.begin() as conn:
            conn.execute(sa.text(f"ALTER TABLE changelog_entries DROP COLUMN {column}"))


def test_three_bootstraps_from_old_schema_heal_once_then_converge(engine, capsys):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    old = add_entry(engine, "proj-a", "added", "Written before #801")
    _drop_new_columns(engine)
    assert not (_columns(engine) & set(_NEW))

    ddl = _record_ddl(engine)
    create_tables(engine)  # bootstrap 1 — heals
    assert set(_NEW) <= _columns(engine)
    for column in _NEW:
        assert any(column in s for s in ddl), (column, ddl)
    capsys.readouterr()

    # The pre-#801 row survives as "unbranched" (no backfill).
    [row] = list_entries(engine, "proj-a")
    assert row["id"] == old["id"]
    assert row["branch"] is None and row["worktree"] is None and row["branch_source"] is None

    new = add_entry(engine, "proj-a", "fixed", "After", branch="feat/x", worktree="/tmp/proj-a")
    for n in (2, 3):
        ddl.clear()
        create_tables(engine)
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        rows = {r["id"]: r for r in list_entries(engine, "proj-a")}
        assert rows[new["id"]]["branch"] == "feat/x"
        assert rows[new["id"]]["branch_source"] == "client"
        assert rows[old["id"]]["branch"] is None


def test_partial_prior_run_heals_each_column_independently(engine):
    """A crash after the first ADD COLUMN leaves one column in place; the next
    bootstrap adds only the missing ones."""
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE changelog_entries DROP COLUMN worktree"))
        conn.execute(sa.text("ALTER TABLE changelog_entries DROP COLUMN branch_source"))
    ddl = _record_ddl(engine)
    create_tables(engine)
    assert set(_NEW) <= _columns(engine)
    assert not any("ADD COLUMN branch " in s or "ADD COLUMN IF NOT EXISTS branch " in s
                   for s in ddl), ddl
