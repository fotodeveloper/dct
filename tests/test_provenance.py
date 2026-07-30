"""Provenance columns on mutating ledgers (ADR #51/#52, CP #1236).

Four nullable columns — actor_type, actor_client, session_id, source —
record WHO created each ledger row. They are filled by SQLAlchemy Core
column defaults reading a ContextVar set once at each entry point (CLI,
MCP server), so no core call site changes. Creation provenance only:
updates never touch the columns (lifecycle auditing is deferred, ADR #51).
"""

import pytest
import sqlalchemy as sa

from dct.core import provenance
from dct.core.items import create_item
from dct.db import create_tables, get_engine

PROV_COLS = ("actor_type", "actor_client", "session_id", "source")
LEDGER_TABLES = (
    "items",
    "item_notes",
    "item_checkpoints",
    "changelog_entries",
    "session_handoffs",
    "sprints",
    "decisions",
    "plans",
)


@pytest.fixture(autouse=True)
def clean_provenance_context():
    provenance.set_provenance(provenance.Provenance())
    yield
    provenance.set_provenance(provenance.Provenance())


def test_provenance_columns_exist_on_every_ledger(engine):
    inspector = sa.inspect(engine)
    for table in LEDGER_TABLES:
        cols = {c["name"] for c in inspector.get_columns(table)}
        for col in PROV_COLS:
            assert col in cols, f"{table}.{col}"


def test_insert_carries_active_provenance(engine, seed_project):
    provenance.set_provenance(
        provenance.Provenance(
            actor_type="agent",
            actor_client="claude-code",
            session_id="sess-123",
            source="mcp",
        )
    )
    item = create_item(engine, project_slug="test", item_type="todo", title="t")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT actor_type, actor_client, session_id, source "
                "FROM items WHERE id = :id"
            ),
            {"id": item["id"]},
        ).one()
    assert tuple(row) == ("agent", "claude-code", "sess-123", "mcp")


def test_insert_without_context_leaves_nulls(engine, seed_project):
    item = create_item(engine, project_slug="test", item_type="todo", title="t")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT actor_type, actor_client, session_id, source "
                "FROM items WHERE id = :id"
            ),
            {"id": item["id"]},
        ).one()
    assert tuple(row) == (None, None, None, None)


def test_migration_adds_columns_to_pre_provenance_schema(tmp_path):
    """A db created before CP #1236 gains the columns on the next bootstrap —
    simulated by dropping them and re-running create_tables (the runner heals
    per-column, per the _apply_schema_updates docstring)."""
    eng = get_engine(f"sqlite:///{tmp_path / 'old.db'}")
    create_tables(eng)
    with eng.begin() as conn:
        for col in PROV_COLS:
            conn.execute(sa.text(f"ALTER TABLE items DROP COLUMN {col}"))
    create_tables(eng)  # idempotent healing bootstrap
    cols = {c["name"] for c in sa.inspect(eng).get_columns("items")}
    assert set(PROV_COLS) <= cols


def test_bootstrap_is_idempotent(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'twice.db'}")
    create_tables(eng)
    create_tables(eng)  # second run must be a clean no-op


def test_from_environment_detects_claude_code(monkeypatch):
    for var in ("DCT_ACTOR_CLIENT", "DCT_ACTOR_TYPE", "DCT_SESSION_ID",
                "CLAUDECODE", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "abc-42")
    p = provenance.from_environment("cli")
    assert p == provenance.Provenance(
        actor_type="agent", actor_client="claude-code",
        session_id="abc-42", source="cli",
    )


def test_from_environment_dct_vars_win(monkeypatch):
    """DCT_ACTOR_* are the neutral self-identification surface — a G2 client
    adapter (codex) sets them in its registration and must beat heuristics."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "abc-42")
    monkeypatch.setenv("DCT_ACTOR_CLIENT", "codex")
    monkeypatch.setenv("DCT_ACTOR_TYPE", "agent")
    monkeypatch.setenv("DCT_SESSION_ID", "codex-7")
    p = provenance.from_environment("mcp")
    assert p.actor_client == "codex"
    assert p.session_id == "codex-7"
    assert p.source == "mcp"


def test_from_environment_unknown_shell(monkeypatch):
    for var in ("DCT_ACTOR_CLIENT", "DCT_ACTOR_TYPE", "DCT_SESSION_ID",
                "CLAUDECODE", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    p = provenance.from_environment("cli")
    assert p == provenance.Provenance(source="cli")


def test_update_does_not_touch_creation_provenance(engine, seed_project):
    """Provenance is CREATION provenance: a later update under a different
    context must not overwrite who created the row."""
    from dct.core.items import update_item

    provenance.set_provenance(provenance.Provenance(
        actor_type="agent", actor_client="claude-code",
        session_id="s1", source="mcp",
    ))
    item = create_item(engine, project_slug="test", item_type="todo", title="t")
    provenance.set_provenance(provenance.Provenance(
        actor_type="agent", actor_client="codex", session_id="s2", source="cli",
    ))
    update_item(engine, item["id"], status="in_progress")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT actor_client, session_id FROM items WHERE id = :id"),
            {"id": item["id"]},
        ).one()
    assert tuple(row) == ("claude-code", "s1")
