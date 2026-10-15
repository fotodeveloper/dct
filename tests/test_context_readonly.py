"""Context reads refuse setup/old schemas without bootstrap, including CWD lookup.

File-backed SQLite checks compare bytes, mtime, schema, rows and journal mode.
Wire metadata is checked through the public SDK registry; annotations are not
host-approval guarantees. Writer pickup keeps its existing transaction contract.
"""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import run_cli
from test_readonly_engine import _rollback_journal_db, _state

from dct import cli, paths
from dct.db import UPGRADE_COMMAND
from dct.mcp import server

pytestmark = pytest.mark.slow
SURFACES = ("snapshot", "mcp-pickup", "cli-pickup")


@pytest.fixture
def actual_context(monkeypatch, tmp_path):
    db = tmp_path / "context.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("DCT_DATABASE_URL", paths.sqlite_url(db))
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setattr(server, "_readonly_engine", None, raising=False)
    yield db
    for name in ("_engine", "_readonly_engine"):
        engine = getattr(server, name, None)
        if engine is not None:
            engine.dispose()


def read_context(surface, project, monkeypatch, capsys):
    if surface == "snapshot":
        return server.get_context_snapshot(project=project)
    if surface == "mcp-pickup":
        return server.pickup_work(project=project, consume="false")
    argv = ["pickup", "--consume", "never", "--json"]
    if project:
        argv += ["--project", project]
    try:
        run_cli(monkeypatch, argv)
    except SystemExit as exc:
        assert exc.code == 1
        return {"error": capsys.readouterr().err}
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("project", ("proj-a", ""), ids=("explicit", "cwd"))
def test_missing_context_db_refuses_without_creating_file_or_parent(actual_context, monkeypatch, capsys, surface, project):
    db = actual_context.parent / "absent-parent" / "missing.db"
    monkeypatch.setenv("DCT_DATABASE_URL", paths.sqlite_url(db))
    result = read_context(surface, project, monkeypatch, capsys)
    assert "error" in result and UPGRADE_COMMAND in result["error"]
    assert not db.exists() and not db.parent.exists()


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("project", ("proj-a", ""), ids=("explicit", "cwd"))
def test_old_context_schema_refuses_without_ddl_rows_or_journal_change(actual_context, monkeypatch, capsys, surface, project):
    db = actual_context
    _rollback_journal_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE releases DROP COLUMN proof")
    before = _state(db)
    result = read_context(surface, project, monkeypatch, capsys)
    assert "error" in result and UPGRADE_COMMAND in result["error"]
    assert _state(db) == before
    assert not Path(f"{db}-journal").exists()


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("project", ("proj-a", ""), ids=("explicit", "cwd"))
def test_current_context_reads_preserve_full_state_and_never_open_writer(actual_context, monkeypatch, capsys, surface, project):
    db = actual_context
    _rollback_journal_db(db)
    before = _state(db)
    monkeypatch.setattr(server, "_get_engine", lambda: pytest.fail("context read opened writer"))
    monkeypatch.setattr(cli, "_get_engine", lambda: pytest.fail("CLI read opened writer"))
    result = read_context(surface, project, monkeypatch, capsys)
    assert result["project"] == "proj-a" and "summary" in result
    assert set(result["sections"]) == {"sprint", "handoff", "gates", "decisions", "recent_changes"}
    if surface != "snapshot":
        assert result["consumed"] is False and result["picked_up"] is None
    assert _state(db) == before


def test_context_refusal_then_explicit_db_upgrade_then_success(actual_context, monkeypatch, capsys):
    """The read never secretly migrates; the approved setup operation remains explicit."""
    db = actual_context
    _rollback_journal_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE releases DROP COLUMN proof")
    assert UPGRADE_COMMAND in server.get_context_snapshot(project="proj-a")["error"]
    run_cli(monkeypatch, ["db", "upgrade"])
    capsys.readouterr()
    assert server.get_context_snapshot(project="proj-a")["project"] == "proj-a"


def test_only_pure_snapshot_has_readonly_annotation_on_registered_wire():
    tools = {t.name: t.model_dump(by_alias=True, exclude_none=True)
             for t in asyncio.run(server.mcp.list_tools())}
    assert tools["get_context_snapshot"]["annotations"]["readOnlyHint"] is True
    for name in ("pickup_work", "handoff_work", "get_item", "list_items"):
        assert tools[name].get("annotations", {}).get("readOnlyHint") is not True


def test_writer_pickup_and_read_cache_are_separate(actual_context, monkeypatch, capsys):
    """AUTO still consumes atomically even after a cached context read."""
    from dct.core.handoff import create_handoff
    from dct.db import get_engine
    db = actual_context
    _rollback_journal_db(db)
    write_engine = get_engine(paths.sqlite_url(db))
    saved = create_handoff(write_engine, "proj-a", "Continue concrete work", "fixture")
    write_engine.dispose()
    assert server.get_context_snapshot(project="proj-a")["summary"]["handoffs_pending"] == 1
    result = server.pickup_work(project="proj-a", consumed_by="test-client", consume="")
    assert result["consumed"] is True and result["picked_up"]["id"] == saved["id"]
    assert server._readonly_engine is not server._engine
    assert server.get_context_snapshot(project="proj-a")["summary"]["handoffs_pending"] == 0


def test_spawned_mcp_wire_has_pure_annotation_and_missing_read_creates_nothing(tmp_path):
    """Actual stdio registration/refusal keeps the missing DB absent."""
    from test_server_wire import WireClient, isolated_wire_environment
    env = isolated_wire_environment(tmp_path)
    db = tmp_path / "wire.sqlite"
    env["DCT_DATABASE_URL"] = paths.sqlite_url(db)
    with WireClient(env, Path(__file__).resolve().parents[1]) as client:
        client.initialize()
        tools = {tool["name"]: tool for tool in client.request("tools/list")["result"]["tools"]}
        assert tools["get_context_snapshot"]["annotations"]["readOnlyHint"] is True
        assert tools["pickup_work"].get("annotations", {}).get("readOnlyHint") is not True
        assert UPGRADE_COMMAND in client.call("get_context_snapshot", project="proj-a")["error"]
        assert UPGRADE_COMMAND in client.call("pickup_work", project="proj-a", consume="false")["error"]
    assert not db.exists()


def test_pg_context_reads_use_real_readonly_transactions(engine, monkeypatch, tmp_path):
    """Guarded PG fixture: CWD reads work, while writes on the read engine refuse."""
    if engine.dialect.name != "postgresql":
        pytest.skip("requires the guarded PostgreSQL test lane")
    import sqlalchemy as sa

    from dct.core.projects import create_project
    project = create_project(engine, "test", "Read test", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # SQLAlchemy masks passwords in str(url), so render this isolated test URL explicitly.
    monkeypatch.setenv("DCT_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(server, "_readonly_engine", None)
    monkeypatch.setattr(server, "_get_engine", lambda: pytest.fail("PG read opened writer"))
    try:
        assert server.get_context_snapshot()["project"] == "test"
        assert server.pickup_work(consume="false")["consumed"] is False
        with pytest.raises(sa.exc.DBAPIError), server._get_readonly_engine().begin() as conn:
            conn.execute(sa.text("UPDATE projects SET name='unauthorized' WHERE id=:id"), {"id": project["id"]})
    finally:
        if server._readonly_engine is not None:
            server._readonly_engine.dispose()
