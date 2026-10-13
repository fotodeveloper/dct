"""Migration guarantees of the labels / branch / releases change (K-45, R5).

Fills the concrete gaps the per-column migration tests leave open:

- a database that misses EVERY piece of this change at once (items.short_title,
  changelog_entries.branch / worktree / branch_source and the `releases`
  table) heals on the first bootstrap, keeps its data, and the second and
  third bootstraps issue no DDL, no DML touching those columns / that table
  and print nothing (AGENTS.md three-bootstrap invariant);
- two processes bootstrapping the same old database at the same instant both
  succeed (the column / table ends up there exactly once);
- PostgreSQL only: `SET LOCAL lock_timeout` runs BEFORE the ALTER inside the
  SAME transaction, actually bounds the wait on a held lock, and never leaks
  into the pooled session.

Both-lane tests run on in-memory / file SQLite by default and on the throwaway
PostgreSQL database on the PG lane; PG-only tests skip on SQLite.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from dct import db
from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.items import create_item, get_item
from dct.core.projects import create_project
from dct.core.releases import get_release
from dct.db import create_tables, get_engine

_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
# Every identifier this change introduced.
_CHANGE = re.compile(r"\b(short_title|branch|worktree|branch_source|releases)\b", re.IGNORECASE)


def _record_all(engine) -> list[str]:
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _listener)
    return seen


def _cols(engine, table) -> list[str]:
    return [c["name"] for c in sa.inspect(engine).get_columns(table)]


def _drop_everything_of_this_change(engine) -> None:
    """Turn the current schema into the pre-change (v1.5.0) one."""
    statements = [
        "DROP TABLE releases",
        "ALTER TABLE items DROP COLUMN short_title",
        "ALTER TABLE changelog_entries DROP COLUMN branch",
        "ALTER TABLE changelog_entries DROP COLUMN worktree",
        "ALTER TABLE changelog_entries DROP COLUMN branch_source",
    ]
    for stmt in statements:
        with engine.begin() as conn:
            conn.execute(sa.text(stmt))


# ── old schema: everything missing at once ───────────────────────────────────


def test_pre_change_database_heals_once_and_keeps_its_data(engine, capsys):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    item = create_item(engine, "proj-a", "todo", "Written before the change")
    entry = add_entry(engine, "proj-a", "added", "Old entry", related_item_id=item["id"])
    _drop_everything_of_this_change(engine)
    assert "releases" not in sa.inspect(engine).get_table_names()

    seen = _record_all(engine)
    create_tables(engine)  # bootstrap 1 — heals every piece
    capsys.readouterr()
    assert "short_title" in _cols(engine, "items")
    assert {"branch", "worktree", "branch_source"} <= set(_cols(engine, "changelog_entries"))
    assert "releases" in sa.inspect(engine).get_table_names()
    # Pre-change rows survive untouched: no backfill invents a label or a branch.
    assert get_item(engine, item["id"])["short_title"] is None
    [row] = list_entries(engine, "proj-a")
    assert (row["id"], row["entry"], row["related_item_id"]) == (
        entry["id"], "Old entry", item["id"])
    assert (row["branch"], row["worktree"], row["branch_source"]) == (None, None, None)
    # The healed schema is usable end to end: a release writes its record.
    rec = release_entries(engine, "proj-a", "1.1.0")["release"]
    assert rec["entry_ids"] == [entry["id"]]

    for n in (2, 3):
        seen.clear()
        create_tables(engine)
        ddl = [s for s in seen if _DDL.match(s)]
        touching = [s for s in seen if _DML.match(s) and _CHANGE.search(s)]
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        assert touching == [], f"bootstrap {n} wrote this change's data: {touching}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        assert get_release(engine, "proj-a", "1.1.0") == rec


# ── concurrent bootstraps of the same old database ───────────────────────────


@pytest.fixture
def two_engines(engine, tmp_path):
    """Two independent engines on ONE database: the PG lane's throwaway
    database, or a file SQLite database (in-memory SQLite is one connection)."""
    if engine.dialect.name == "postgresql":
        url = engine.url.render_as_string(hide_password=False)
        engines = [get_engine(url), get_engine(url)]
    else:
        path = tmp_path / "race.db"
        first = get_engine(f"sqlite:///{path}")
        create_tables(first)
        engines = [first, get_engine(f"sqlite:///{path}")]
    try:
        yield engines
    finally:
        for eng in engines:
            eng.dispose()


def _hold_both_at(engines, needle: re.Pattern) -> threading.Barrier:
    """Make each engine wait at the first statement matching `needle` until
    the other one reached it too — both have passed their existence check."""
    barrier = threading.Barrier(len(engines), timeout=15)
    hit: set[int] = set()

    def make_listener(idx):
        def _listener(conn, cursor, statement, parameters, context, executemany):
            if idx not in hit and needle.search(statement):
                hit.add(idx)
                barrier.wait()
        return _listener

    for idx, eng in enumerate(engines):
        event.listen(eng, "before_cursor_execute", make_listener(idx))
    return barrier


def _bootstrap_concurrently(engines) -> list[BaseException | None]:
    def run(eng):
        try:
            create_tables(eng)
        except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
            return exc
        return None

    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        return [f.result(timeout=60) for f in [pool.submit(run, e) for e in engines]]


def test_two_concurrent_bootstraps_both_add_the_column_once(two_engines):
    first, _ = two_engines
    create_project(first, slug="proj-a", name="Project A", path="/tmp/proj-a")
    item = create_item(first, "proj-a", "todo", "Survives the race")
    with first.begin() as conn:
        conn.execute(sa.text("ALTER TABLE items DROP COLUMN short_title"))

    _hold_both_at(two_engines, re.compile(r"ADD COLUMN .*short_title", re.IGNORECASE))
    errors = _bootstrap_concurrently(two_engines)

    assert errors == [None, None], errors
    assert _cols(first, "items").count("short_title") == 1
    assert get_item(first, item["id"])["title"] == "Survives the race"


_RELEASES_INDEX = "ix_releases_project_version_live"


def _index_names(engine, table) -> list[str]:
    return [ix["name"] for ix in sa.inspect(engine).get_indexes(table)]


def _assert_bootstraps_are_quiet(engine, capsys, runs=(2, 3)) -> None:
    """Bootstraps 2..3 after the race: no DDL, no DML on this change, no output."""
    seen = _record_all(engine)
    for n in runs:
        seen.clear()
        create_tables(engine)
        ddl = [st for st in seen if _DDL.match(st)]
        touching = [st for st in seen if _DML.match(st) and _CHANGE.search(st)]
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        assert touching == [], f"bootstrap {n} wrote: {touching}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"


_AT_CREATE_TABLE = re.compile(r"CREATE TABLE releases\b", re.IGNORECASE)
_AT_CREATE_INDEX = re.compile(rf"CREATE UNIQUE INDEX {_RELEASES_INDEX}\b", re.IGNORECASE)


@pytest.mark.parametrize("needles", [
    [_AT_CREATE_TABLE],
    [_AT_CREATE_TABLE, _AT_CREATE_INDEX],
], ids=["at-create-table", "at-create-table-then-index"])
def test_two_concurrent_bootstraps_both_create_the_releases_table_and_index(
        two_engines, capsys, needles):
    """K-59: both bootstraps pass their existence check, then meet at the
    barrier right before CREATE TABLE (and, in the second case, again right
    before CREATE INDEX — the loser of the table race has re-checked and
    found the table WITHOUT its index); the loser recognises the
    concurrent-creation conflict, re-checks the WHOLE required schema of the
    table (a table that exists does not prove its unique index exists) and
    finishes it."""
    first, _ = two_engines
    if first.dialect.name == "postgresql" and _AT_CREATE_INDEX in needles:
        pytest.skip("PostgreSQL creates a table and its indexes in ONE transaction — "
                    "no other session can see the table without its index")
    with first.begin() as conn:
        conn.execute(sa.text("DROP TABLE releases"))

    for needle in needles:
        _hold_both_at(two_engines, needle)
    errors = _bootstrap_concurrently(two_engines)

    assert errors == [None, None], errors
    assert sa.inspect(first).get_table_names().count("releases") == 1
    assert _index_names(first, "releases").count(_RELEASES_INDEX) == 1
    capsys.readouterr()
    for eng in two_engines:
        _assert_bootstraps_are_quiet(eng, capsys)


def test_a_table_seen_without_its_index_after_a_conflict_is_finished(engine, capsys):
    """The conflicting creator has so far created only the bare table (on
    SQLite CREATE TABLE and CREATE INDEX commit separately): the retry after
    the conflict must not trust the table alone and creates the index."""
    from dct import db

    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE releases"))
    real = db._create_table_objects
    calls = []

    def lose_the_race_once(eng, table):
        if table.name == "releases" and not calls:
            calls.append(table.name)
            # The "other process" created the bare table only, then died.
            with eng.begin() as conn:
                conn.execute(sa.schema.CreateTable(table))
            raise _duplicate_table_error(eng)
        return real(eng, table)

    db._create_table_objects = lose_the_race_once
    try:
        create_tables(engine)
    finally:
        db._create_table_objects = real
    assert calls == ["releases"]
    assert _index_names(engine, "releases").count(_RELEASES_INDEX) == 1
    capsys.readouterr()
    _assert_bootstraps_are_quiet(engine, capsys)


def _duplicate_table_error(engine) -> Exception:
    """The error the dialect raises for CREATE TABLE of an existing table."""
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE releases (id INTEGER)"))
    except sa.exc.DBAPIError as exc:
        return exc
    raise AssertionError("CREATE TABLE of an existing table did not fail")


def test_a_conflict_that_never_resolves_gives_up_after_bounded_attempts(engine):
    from dct import db

    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE releases"))
    attempts = []

    def always_conflict(eng, table):
        attempts.append(table.name)
        raise _duplicate_table_error_from_items(eng)

    real = db._create_table_objects
    db._create_table_objects = always_conflict
    try:
        with pytest.raises(sa.exc.DBAPIError):
            create_tables(engine)
    finally:
        db._create_table_objects = real
    assert len(attempts) == db.SCHEMA_CREATE_ATTEMPTS


def _duplicate_table_error_from_items(engine) -> Exception:
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE items (id INTEGER)"))
    except sa.exc.DBAPIError as exc:
        return exc
    raise AssertionError("CREATE TABLE of an existing table did not fail")


def test_other_creation_errors_are_never_swallowed(engine):
    from dct import db

    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE releases"))
    attempts = []

    def broken(eng, table):
        attempts.append(table.name)
        raise sa.exc.OperationalError("CREATE TABLE releases", {}, Exception("disk I/O error"))

    real = db._create_table_objects
    db._create_table_objects = broken
    try:
        with pytest.raises(sa.exc.OperationalError, match="disk I/O error"):
            create_tables(engine)
    finally:
        db._create_table_objects = real
    assert attempts == ["releases"]   # no retry of an unrelated error


def test_tables_that_predate_the_bootstrap_are_not_retrofitted(engine):
    """Only objects of tables THIS bootstrap found missing are completed — an
    index missing on an old, already-present table is not created behind the
    owner's back (that stays the job of an explicit migration block)."""
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_items_project_status_deleted"))
    seen = _record_all(engine)
    create_tables(engine)
    assert [st for st in seen if _DDL.match(st)] == []
    assert "ix_items_project_status_deleted" not in _index_names(engine, "items")


# ── PostgreSQL: lock_timeout is scoped to the ALTER's transaction ────────────


@pytest.fixture
def pg_engine(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL-only guarantee (ADD COLUMN IF NOT EXISTS + SET LOCAL)")
    return engine


def _record_transactions(engine) -> list[tuple]:
    """(kind, connection id[, statement]) for begin / statement / commit / rollback."""
    events: list[tuple] = []
    event.listen(engine, "begin", lambda conn: events.append(("begin", id(conn))))
    event.listen(engine, "commit", lambda conn: events.append(("commit", id(conn))))
    event.listen(engine, "rollback", lambda conn: events.append(("rollback", id(conn))))

    def _stmt(conn, cursor, statement, parameters, context, executemany):
        events.append(("sql", id(conn), statement.strip()))

    event.listen(engine, "before_cursor_execute", _stmt)
    return events


@pytest.mark.parametrize("table,column,ddl_type", [
    ("items", "short_title", "VARCHAR(50)"),
    ("changelog_entries", "branch", "VARCHAR(255)"),
    ("changelog_entries", "worktree", "VARCHAR(500)"),
    ("changelog_entries", "branch_source", "VARCHAR(16)"),
])
def test_pg_set_local_lock_timeout_precedes_the_alter_in_one_transaction(
    pg_engine, table, column, ddl_type,
):
    with pg_engine.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    events = _record_transactions(pg_engine)

    assert db._add_column_if_missing(pg_engine, table, column, ddl_type) is True

    alter_at = next(i for i, e in enumerate(events)
                    if e[0] == "sql" and e[2].upper().startswith("ALTER TABLE"))
    conn_id = events[alter_at][1]
    alter = events[alter_at][2]
    assert f"ADD COLUMN IF NOT EXISTS {column} " in alter, alter
    mine = [(i, e) for i, e in enumerate(events) if e[1] == conn_id]
    set_at = next(i for i, e in mine if e[0] == "sql" and "lock_timeout" in e[2])
    begin_at = max(i for i, e in mine if e[0] == "begin" and i < set_at)
    commit_at = next(i for i, e in mine if e[0] == "commit" and i > alter_at)
    assert begin_at < set_at < alter_at < commit_at
    assert events[set_at][2].upper().startswith("SET LOCAL LOCK_TIMEOUT")
    between = [e for i, e in mine if set_at < i < alter_at]
    assert all(e[0] == "sql" for e in between), f"transaction boundary between: {between}"
    assert column in _cols(pg_engine, table)


def test_pg_lock_timeout_fails_fast_on_a_held_lock_then_heals(pg_engine):
    """A session holding a lock on `items` (a long transaction, a stuck hook)
    must not hang every bootstrap: the ALTER gives up within the bounded
    lock_timeout, nothing is half-applied, and the next bootstrap heals."""
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE items DROP COLUMN short_title"))
    url = pg_engine.url.render_as_string(hide_password=False)
    holder = get_engine(url)
    try:
        with holder.connect() as hold:
            hold.begin()
            hold.execute(sa.text("LOCK TABLE items IN ACCESS SHARE MODE"))
            started = time.monotonic()
            with pytest.raises(sa.exc.OperationalError, match="lock timeout"):
                db._add_column_if_missing(pg_engine, "items", "short_title", "VARCHAR(50)")
            waited = time.monotonic() - started
            hold.rollback()
    finally:
        holder.dispose()
    assert 3 <= waited < 15, f"waited {waited:.1f}s — expected the ~5s lock_timeout"
    assert "short_title" not in _cols(pg_engine, "items")
    assert db._add_column_if_missing(pg_engine, "items", "short_title", "VARCHAR(50)") is True
    assert "short_title" in _cols(pg_engine, "items")


def test_pg_lock_timeout_never_leaks_into_the_pooled_session(pg_engine):
    with pg_engine.connect() as conn:
        default = conn.execute(sa.text("SHOW lock_timeout")).scalar_one()
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE items DROP COLUMN short_title"))
    assert db._add_column_if_missing(pg_engine, "items", "short_title", "VARCHAR(50)") is True
    for _ in range(3):  # whichever pooled connection comes back
        with pg_engine.connect() as conn:
            assert conn.execute(sa.text("SHOW lock_timeout")).scalar_one() == default
