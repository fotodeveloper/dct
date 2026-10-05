"""`releases` table on an existing database (K-41).

The table is created by metadata.create_all (checkfirst) like the v1.5 tags
tables: the first bootstrap of a database without it creates the table and
its partial unique index; the second and third issue NO DDL, no DML touching
`releases` and print nothing. Runs on both lanes (SQLite default, PG lane).
"""

import re

import sqlalchemy as sa
from sqlalchemy import event

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project
from dct.core.releases import get_release
from dct.db import create_tables

_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_RELEASES = re.compile(r"\breleases\b", re.IGNORECASE)


def _record(engine) -> list[str]:
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _listener)
    return seen


def _ddl(statements):
    return [s for s in statements if _DDL.match(s)]


def _release_dml(statements):
    return [s for s in statements if _DML.match(s) and _RELEASES.search(s)]


def test_three_bootstraps_create_the_table_once_then_converge(engine, capsys):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    add_entry(engine, "proj-a", "added", "Written before K-41")
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE releases"))
    assert "releases" not in sa.inspect(engine).get_table_names()

    seen = _record(engine)
    create_tables(engine)  # bootstrap 1 — creates the table + index
    assert "releases" in sa.inspect(engine).get_table_names()
    ddl = _ddl(seen)
    assert any("CREATE TABLE releases" in s for s in ddl), ddl
    assert any("ix_releases_project_version_live" in s for s in ddl), ddl
    capsys.readouterr()

    rec = release_entries(engine, "proj-a", "1.1.0")["release"]
    for n in (2, 3):
        seen.clear()
        create_tables(engine)
        assert _ddl(seen) == [], f"bootstrap {n} issued DDL: {_ddl(seen)}"
        assert _release_dml(seen) == [], f"bootstrap {n} wrote releases: {_release_dml(seen)}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        assert get_release(engine, "proj-a", "1.1.0") == rec


def test_partial_unique_index_exists_after_bootstrap(engine):
    names = {ix["name"] for ix in sa.inspect(engine).get_indexes("releases")}
    assert "ix_releases_project_version_live" in names
