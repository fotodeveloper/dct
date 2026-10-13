"""K-84: the read-only commands never change the database.

`dct release plan`, `dct clog candidates`, `dct release status` /
`dct clog release-status` and the `dct release reconstruct` preview open the
database through ONE genuinely read-only engine path (db.get_readonly_engine +
db.check_schema_ready): no database file or directory is created, no table or
index, no journal-mode change, no row. A missing or older schema is a clear
refusal naming the explicit upgrade command.

The CLI tests run the REAL entry point in a subprocess (no `_get_engine`
substitution) against a throwaway SQLite file and compare the file's bytes,
mtime, schema dump and journal mode before and after.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from dct import paths
from dct.core.projects import create_project
from dct.db import (
    UPGRADE_COMMAND,
    SchemaNotReady,
    check_schema_ready,
    create_tables,
    get_engine,
    get_readonly_engine,
)

pytestmark = pytest.mark.slow

READ_ONLY_COMMANDS = [
    ["release", "plan", "--project", "proj-a"],
    ["release", "plan", "--project", "proj-a", "--json"],
    ["clog", "candidates", "--project", "proj-a"],
    ["release", "status", "--project", "proj-a"],
    ["release", "status", "--project", "proj-a", "--version", "1.0.0"],
    ["clog", "release-status", "--project", "proj-a"],
    ["release", "reconstruct", "--project", "proj-a", "--version", "1.0.0"],
    # R8d: the previews of the takeover and of the version-file contract.
    ["release", "takeover", "--project", "proj-a", "--version", "1.0.0", "--no-push"],
    ["release", "version-files", "--project", "proj-a"],
    ["release", "version-files", "--project", "proj-a", "--none"],
]


def _cli(db_path: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "DCT_DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run(
        [sys.executable, "-m", "dct.cli", *args],
        capture_output=True, text=True, timeout=60, cwd=cwd, env=env, check=False,
    )


def _state(db_path: Path) -> dict:
    """Everything a read-only command must leave untouched."""
    data = db_path.read_bytes()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        rows = {
            name: conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
            for (kind, name, _tbl, _sql) in schema if kind == "table"
        }
    conn.close()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "mtime_ns": db_path.stat().st_mtime_ns,
        "schema": schema,
        "journal": journal,
        "rows": rows,
    }


def _rollback_journal_db(path: Path) -> None:
    """A current-schema database in rollback-journal mode — so a stray
    `PRAGMA journal_mode=WAL` would be visible in the state dump."""
    eng = get_engine(f"sqlite:///{path}")
    create_tables(eng)
    create_project(eng, "proj-a", "Project A", path=str(path.parent / "repo"))
    eng.dispose()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    conn.close()


@pytest.mark.parametrize("args", READ_ONLY_COMMANDS, ids=lambda a: " ".join(a[:2]))
def test_missing_database_is_refused_without_creating_anything(tmp_path, args):
    db_dir = tmp_path / "never-created"
    db_path = db_dir / "dct.db"
    out = _cli(db_path, args, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert UPGRADE_COMMAND in out.stderr
    assert "does not exist" in out.stderr
    assert not db_path.exists()
    assert not db_dir.exists()


@pytest.mark.parametrize("args", READ_ONLY_COMMANDS, ids=lambda a: " ".join(a[:2]))
def test_older_schema_is_refused_and_left_unchanged(tmp_path, args):
    db_path = tmp_path / "old.db"
    _rollback_journal_db(db_path)
    with sqlite3.connect(db_path) as conn:
        # An older release ledger: no proof column, no live-version index.
        conn.execute("DROP INDEX ix_releases_project_version_live")
        conn.execute("ALTER TABLE releases DROP COLUMN proof")
    conn.close()
    before = _state(db_path)
    out = _cli(db_path, args, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert UPGRADE_COMMAND in out.stderr
    assert "releases.proof" in out.stderr
    assert _state(db_path) == before
    assert not Path(f"{db_path}-journal").exists()


@pytest.mark.parametrize("args, shown", [
    (["release", "status", "--project", "proj-a"], "No "),
    (["clog", "release-status", "--project", "proj-a", "--version", "1.0.0"], "No "),
    # K-110: showing the contract, and a proposal for it, writes nothing.
    (["release", "version-files", "--project", "proj-a"], "UNKNOWN"),
    (["release", "version-files", "--project", "proj-a", "--none"], "preview"),
], ids=lambda a: " ".join(a[:2]) if isinstance(a, list) else a)
def test_current_schema_reads_and_changes_nothing(tmp_path, args, shown):
    db_path = tmp_path / "current.db"
    _rollback_journal_db(db_path)
    before = _state(db_path)
    out = _cli(db_path, args, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert shown in out.stdout
    assert _state(db_path) == before


def test_db_upgrade_bootstraps_explicitly(tmp_path):
    """The command the refusal names really creates/upgrades the schema."""
    db_path = tmp_path / "fresh" / "dct.db"
    out = _cli(db_path, ["db", "upgrade"], tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    eng = get_readonly_engine(f"sqlite:///{db_path}")
    try:
        check_schema_ready(eng)
    finally:
        eng.dispose()
    again = _cli(db_path, ["release", "status", "--project", "proj-a"], tmp_path)
    assert again.returncode == 1  # the schema is there; the project is not
    assert UPGRADE_COMMAND not in again.stderr


# ── the engine itself ────────────────────────────────────────────────────────


def test_sqlite_readonly_engine_refuses_writes(tmp_path):
    db_path = tmp_path / "ro.db"
    _rollback_journal_db(db_path)
    before = _state(db_path)
    eng = get_readonly_engine(f"sqlite:///{db_path}")
    try:
        check_schema_ready(eng)
        with eng.connect() as conn:
            assert conn.execute(sa.text("SELECT slug FROM projects")).scalar_one() == "proj-a"
            with pytest.raises(sa.exc.OperationalError, match="readonly|read-only"):
                conn.execute(sa.text("UPDATE projects SET name = 'x'"))
    finally:
        eng.dispose()
    assert _state(db_path) == before


def test_readonly_engine_opens_a_path_with_uri_metacharacters(tmp_path):
    """'#' and '%' in the file name must not be read as a URI fragment or an
    escape — the engine opens exactly that file (and creates no other)."""
    plain = tmp_path / "plain.db"
    _rollback_journal_db(plain)
    db_path = tmp_path / "we#ird%20name.db"
    plain.rename(db_path)
    # K-99: the URL comes from the ONE builder (paths.sqlite_url), so the
    # test holds on every supported SQLAlchemy (2.1 percent-decodes the
    # database part of a URL, 2.0 does not).
    eng = get_readonly_engine(paths.sqlite_url(db_path))
    try:
        check_schema_ready(eng)
    finally:
        eng.dispose()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["we#ird%20name.db"]


def test_in_memory_database_is_not_ready():
    with pytest.raises(SchemaNotReady, match="in-memory"):
        get_readonly_engine("sqlite://")


def test_postgres_readonly_engine_uses_read_only_transactions(engine):
    """PG lane: every transaction of the read-only engine is READ ONLY."""
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL read-only transaction mode")
    create_tables(engine)
    ro = get_readonly_engine(engine.url.render_as_string(hide_password=False))
    try:
        check_schema_ready(ro)
        with ro.connect() as conn:
            assert conn.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on"
            with pytest.raises(sa.exc.DBAPIError, match="read-only transaction"):
                conn.execute(sa.text(
                    "INSERT INTO projects (slug, name) VALUES ('ro-probe', 'x')"
                ))
    finally:
        ro.dispose()


def test_schema_check_names_missing_tables_and_columns(engine):
    """Missing table and missing column are both reported (both lanes)."""
    create_tables(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE item_tags"))
    with pytest.raises(SchemaNotReady) as raised:
        check_schema_ready(engine)
    assert "item_tags" in str(raised.value)
    assert UPGRADE_COMMAND in str(raised.value)
