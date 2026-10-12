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


# ── projects.release_preview (the preview slot of `prepare --preview`) ────────

_PREVIEW = re.compile(r"\brelease_preview\b", re.IGNORECASE)
_PROJECTS = re.compile(r"\bprojects\b", re.IGNORECASE)


def _project_columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("projects")}


def _slot_and_proposal(engine) -> tuple:
    from dct.db import projects

    with engine.begin() as conn:
        return conn.execute(sa.select(projects.c.release_preview, projects.c.release_proposal)
                            .where(projects.c.slug == "proj-a")).one()


def test_release_preview_column_is_added_once_then_bootstraps_converge(engine, capsys):
    """Additive, nullable, no backfill: the first bootstrap of a database
    without the column adds it (existing rows read NULL, nothing else
    written); the second and third issue no DDL, write nothing to
    `projects`, print nothing and keep a slot stored in between. Valid on
    SQLite and on the PG lane."""
    from dct.db import projects

    assert "release_preview" in _project_columns(engine), "projects has no release_preview"
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == "proj-a")
                     .values(release_proposal='{"paths": []}'))
        conn.execute(sa.text("ALTER TABLE projects DROP COLUMN release_preview"))
    assert "release_preview" not in _project_columns(engine)

    seen = _record(engine)
    create_tables(engine)  # bootstrap 1 — heals
    assert "release_preview" in _project_columns(engine)
    ddl = _ddl(seen)
    assert any(_PREVIEW.search(s) for s in ddl), ddl
    preview_dml = [s for s in seen if _DML.match(s) and _PREVIEW.search(s)]
    assert preview_dml == [], f"bootstrap 1 back-filled the slot: {preview_dml}"
    assert _slot_and_proposal(engine) == (None, '{"paths": []}')
    capsys.readouterr()

    with engine.begin() as conn:  # a slot stored by the new code
        conn.execute(projects.update().where(projects.c.slug == "proj-a")
                     .values(release_preview='{"id": "01SLOT"}'))
    for n in (2, 3):
        seen.clear()
        create_tables(engine)
        assert _ddl(seen) == [], f"bootstrap {n} issued DDL: {_ddl(seen)}"
        writes = [s for s in seen if _DML.match(s) and (_PROJECTS.search(s)
                                                        or _PREVIEW.search(s))]
        assert writes == [], f"bootstrap {n} wrote projects: {writes}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        assert _slot_and_proposal(engine) == ('{"id": "01SLOT"}', '{"paths": []}')
