"""SQLAlchemy Core database layer for dct.

Defines all tables, engine creation, and connection helpers.
Uses Core (not ORM) for portability between PostgreSQL and SQLite.
"""

import contextlib
import json
import sys
from collections import Counter
from datetime import UTC
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event

from dct.core.provenance import get_provenance

metadata = sa.MetaData()

# Names of the creation-provenance columns added to every mutating ledger
# table (CP #1236). Single source of truth for the table defs, the
# _apply_schema_updates migration, and tests.
PROVENANCE_COLUMNS = ("actor_type", "actor_client", "session_id", "source")
PROVENANCE_TABLES = (
    "items",
    "item_notes",
    "item_checkpoints",
    "changelog_entries",
    "session_handoffs",
    "sprints",
    "decisions",
    "plans",
    "tags",
)


def _provenance_columns() -> list:
    """Fresh Column objects per table (Columns cannot be shared). The python
    defaults read the entry-point ContextVar, so every Core INSERT records
    who created the row with zero call-site changes. Nullable by design —
    honest absence beats guessed attribution."""
    return [
        sa.Column("actor_type", sa.String(16),
                  default=lambda: get_provenance().actor_type),
        sa.Column("actor_client", sa.String(64),
                  default=lambda: get_provenance().actor_client),
        sa.Column("session_id", sa.String(128),
                  default=lambda: get_provenance().session_id),
        sa.Column("source", sa.String(16),
                  default=lambda: get_provenance().source),
    ]

projects = sa.Table(
    "projects",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("slug", sa.String(50), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("path", sa.String(500)),
    sa.Column("description", sa.Text),
    sa.Column("current_version", sa.String(50)),
    # #222: per-project opt-in for the dct changelog hook (default TRUE; the
    # migration backfills existing projects from their actual dct changelog
    # history). Opt out with `dct project changelog <slug> off`.
    sa.Column("manages_changelog", sa.Boolean, nullable=False, server_default=sa.true()),
    # #225: changelog file location relative to the project root (default repo
    # root). A project keeping its changelog in a subdir points it there and
    # /release resolves {path}/{changelog_path}. Constrained to the project root
    # by set_changelog_path (leading '/' stripped, '..' escape rejected).
    sa.Column("changelog_path", sa.String(500), nullable=False, server_default="changelog.md"),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
)

items = sa.Table(
    "items",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("item_type", sa.String(20), nullable=False),
    sa.Column("title", sa.String(500), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("priority", sa.String(10), nullable=False, server_default="P2"),
    sa.Column("status", sa.String(20), nullable=False, server_default="open"),
    sa.Column("component", sa.String(100)),
    sa.Column("source_file", sa.String(500)),
    sa.Column("source_lines", sa.String(100)),
    # DEPRECATED — frozen, DROP at 2.0.0. Tags live in tags / item_tags since
    # #611; _backfill_item_tags is the ONLY sanctioned reader of this column —
    # and its ONLY writer, in exactly one case (K-22): after reporting an item
    # whose JSON yields no usable name, it blanks that item's value to '[]' so
    # the un-migratable row stops being re-selected on every bootstrap.
    sa.Column("tags", sa.Text, server_default="[]"),
    sa.Column("resolution", sa.Text),
    # NB: sprint membership is NOT a column. `in_current_sprint` is derived at
    # read time from item_sprints + the project's active sprint (single source
    # of truth, #216). The legacy boolean column is dropped by the migration in
    # _apply_schema_updates.
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("resolved_at", sa.DateTime),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

item_notes = sa.Table(
    "item_notes",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("note", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

item_checkpoints = sa.Table(
    "item_checkpoints",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
    sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    # `kind` classifies the checkpoint as a 'task' (default, closeable by
    # anyone) or a 'gate' (kind != 'task'). Pending gates block resolve_item.
    # Open string column (not ENUM) so OSS users can define custom gate
    # kinds; core validates against a whitelist in checkpoints.py but
    # unknown kinds are treated as gates (conservative default).
    sa.Column("kind", sa.String(32), nullable=False, server_default="task"),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

changelog_entries = sa.Table(
    "changelog_entries",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("version", sa.String(50)),
    sa.Column("category", sa.String(20), nullable=False),
    sa.Column("entry", sa.Text, nullable=False),
    sa.Column("component", sa.String(100)),
    sa.Column("source_file", sa.String(500)),
    sa.Column("source_lines", sa.String(100)),
    sa.Column("related_item_id", sa.Integer, sa.ForeignKey("items.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("released_at", sa.DateTime),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

session_handoffs = sa.Table(
    "session_handoffs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("prompt", sa.Text, nullable=False),
    sa.Column("context_summary", sa.Text),
    sa.Column("scope", sa.String(100)),
    sa.Column("created_by", sa.String(200), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("consumed_at", sa.DateTime),
    sa.Column("consumed_by", sa.String(200)),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

# ── v0.2.0 schema: plans / sprints / decisions ────────────────────────────────

sprints = sa.Table(
    "sprints",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("code", sa.String(50)),  # short code: "0.5", "M0", "sprint-v0.1-ship"
    sa.Column("kind", sa.String(20), nullable=False, server_default="sprint"),  # sprint|phase|milestone|block
    sa.Column("status", sa.String(20), nullable=False, server_default="planned"),  # planned|active|deferred|completed|abandoned
    sa.Column("goal", sa.Text),
    sa.Column("started_at", sa.DateTime),
    sa.Column("ended_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

plans = sa.Table(
    "plans",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("slug", sa.String(200), nullable=False),
    sa.Column("title", sa.String(500), nullable=False),
    sa.Column("kind", sa.String(20), nullable=False),  # plan|spec|roadmap|adr|retro|backlog
    sa.Column("source_file", sa.String(1000)),        # NULL for DB-only roadmap
    sa.Column("source_hash", sa.String(64)),          # sha256 of file at last scan
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),  # draft|active|completed|archived
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id")),
    sa.Column("imported_at", sa.DateTime),            # NULL = never imported (first-import gate)
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("archived_at", sa.DateTime),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

plan_sections = sa.Table(
    "plan_sections",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id"), nullable=False),
    sa.Column("parent_section_id", sa.Integer, sa.ForeignKey("plan_sections.id")),
    sa.Column("heading", sa.Text, nullable=False),
    sa.Column("depth", sa.Integer, nullable=False),  # 1=H1, 2=H2, ..., 6=H6
    sa.Column("slug", sa.String(200)),
    sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    sa.Column("section_type", sa.String(30), nullable=False, server_default="generic"),
    sa.Column("section_type_confidence", sa.String(10), server_default="high"),  # high|low|uncertain
    sa.Column("needs_llm_review", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("source_lines", sa.String(50)),
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id")),
    sa.Column("converted_to_item_id", sa.Integer, sa.ForeignKey("items.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
)

plan_checkpoints = sa.Table(
    "plan_checkpoints",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("plan_section_id", sa.Integer, sa.ForeignKey("plan_sections.id"), nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("status", sa.String(10), nullable=False, server_default="pending"),  # pending|done|rejected
    sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    sa.Column("converted_to_item_id", sa.Integer, sa.ForeignKey("items.id")),
    sa.Column("source_lines", sa.String(50)),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
)

item_sprints = sa.Table(
    "item_sprints",
    metadata,
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), primary_key=True),
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id"), primary_key=True),
    sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("removed_at", sa.DateTime),
)

# ── v1.5 schema: tags ─────────────────────────────────────────────────────────

tags = sa.Table(
    "tags",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    # GLOBAL / user-scoped: deliberately NO project_id (owner decision, #611) —
    # one name means the same thing in every project.
    sa.Column("name", sa.Text, nullable=False),  # no DB length limit: the
    # 50-char rule is an API rule for NEW names only, so legacy names migrated
    # from the frozen items.tags column always fit (K-7).
    sa.Column("description", sa.Text),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

item_tags = sa.Table(  # mirror of item_sprints: composite PK, soft-removed links
    "item_tags",
    metadata,
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), primary_key=True),
    sa.Column("tag_id", sa.Integer, sa.ForeignKey("tags.id"), primary_key=True),
    sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("removed_at", sa.DateTime),
)

decisions = sa.Table(
    "decisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("kind", sa.String(30), nullable=False),  # adr|sprint_amendment|inline_plan|open_question
    sa.Column("ref_code", sa.String(50)),
    sa.Column("title", sa.String(500), nullable=False),
    sa.Column("context", sa.Text),
    sa.Column("decision", sa.Text),
    sa.Column("consequences", sa.Text),
    sa.Column("alternatives", sa.Text),
    sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),  # proposed|accepted|superseded|rejected
    sa.Column("superseded_by", sa.Integer, sa.ForeignKey("decisions.id")),
    sa.Column("source_file", sa.String(1000)),
    sa.Column("source_lines", sa.String(50)),
    sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id")),
    sa.Column("plan_section_id", sa.Integer, sa.ForeignKey("plan_sections.id")),
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)


sa.Index(
    "ix_projects_slug_active",
    projects.c.slug,
    unique=True,
    sqlite_where=projects.c.deleted_at.is_(None),
    postgresql_where=projects.c.deleted_at.is_(None),
)

sa.Index("ix_items_project_status_deleted", items.c.project_id, items.c.status, items.c.deleted_at)
# (ix_items_sprint removed with the in_current_sprint column — membership now
#  lives in item_sprints; the current-sprint EXISTS lookup is served by the
#  item_sprints PK on item_id + ix_sprints_project_status.)
sa.Index("ix_changelog_project_version_deleted", changelog_entries.c.project_id, changelog_entries.c.version, changelog_entries.c.deleted_at)
sa.Index("ix_changelog_related_item", changelog_entries.c.related_item_id)
sa.Index("ix_item_notes_item_deleted", item_notes.c.item_id, item_notes.c.deleted_at)
sa.Index("ix_checkpoints_item_status_deleted", item_checkpoints.c.item_id, item_checkpoints.c.status, item_checkpoints.c.deleted_at)
# Partial index: fast lookup of pending gates per item (hot path in resolve_item).
# Condition must match the predicate in resolve_item exactly for planner to use it.
sa.Index(
    "ix_checkpoints_pending_gate",
    item_checkpoints.c.item_id,
    sqlite_where=sa.and_(
        item_checkpoints.c.kind != "task",
        item_checkpoints.c.status == "pending",
        item_checkpoints.c.deleted_at.is_(None),
    ),
    postgresql_where=sa.and_(
        item_checkpoints.c.kind != "task",
        item_checkpoints.c.status == "pending",
        item_checkpoints.c.deleted_at.is_(None),
    ),
)
sa.Index(
    "ix_handoffs_unconsumed",
    session_handoffs.c.project_id,
    session_handoffs.c.created_at.desc(),
    sqlite_where=sa.and_(session_handoffs.c.consumed_at.is_(None), session_handoffs.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(session_handoffs.c.consumed_at.is_(None), session_handoffs.c.deleted_at.is_(None)),
)

# v0.2.0 indexes
sa.Index(
    "ix_plans_project_slug_active",
    plans.c.project_id, plans.c.slug,
    unique=True,
    sqlite_where=plans.c.deleted_at.is_(None),
    postgresql_where=plans.c.deleted_at.is_(None),
)
sa.Index("ix_plan_sections_plan", plan_sections.c.plan_id, plan_sections.c.sort_order)
sa.Index("ix_plan_sections_parent", plan_sections.c.parent_section_id)
sa.Index(
    "ix_plan_sections_uncertain",
    plan_sections.c.plan_id,
    sqlite_where=sa.and_(plan_sections.c.needs_llm_review.is_(True), plan_sections.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(plan_sections.c.needs_llm_review.is_(True), plan_sections.c.deleted_at.is_(None)),
)
sa.Index("ix_plan_checkpoints_section_status", plan_checkpoints.c.plan_section_id, plan_checkpoints.c.status, plan_checkpoints.c.deleted_at)
sa.Index(
    "ix_sprints_project_code_active",
    sprints.c.project_id, sprints.c.code,
    unique=True,
    sqlite_where=sa.and_(sprints.c.code.isnot(None), sprints.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(sprints.c.code.isnot(None), sprints.c.deleted_at.is_(None)),
)
sa.Index("ix_sprints_project_status", sprints.c.project_id, sprints.c.status, sprints.c.deleted_at)
sa.Index(
    "ix_item_sprints_sprint_active",
    item_sprints.c.sprint_id,
    sqlite_where=item_sprints.c.removed_at.is_(None),
    postgresql_where=item_sprints.c.removed_at.is_(None),
)
sa.Index(
    "ix_decisions_project_refcode_active",
    decisions.c.project_id, decisions.c.ref_code,
    unique=True,
    sqlite_where=sa.and_(decisions.c.ref_code.isnot(None), decisions.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(decisions.c.ref_code.isnot(None), decisions.c.deleted_at.is_(None)),
)
sa.Index("ix_decisions_project_kind_status", decisions.c.project_id, decisions.c.kind, decisions.c.status, decisions.c.deleted_at)

# v1.5 tag indexes — the dictionary's only hard constraint (the similarity
# guard is advisory) plus the reverse lookup behind the list_items tag filter.
sa.Index(
    "ix_tags_name_active",
    tags.c.name,
    unique=True,
    sqlite_where=tags.c.deleted_at.is_(None),
    postgresql_where=tags.c.deleted_at.is_(None),
)
sa.Index(
    "ix_item_tags_tag_active",
    item_tags.c.tag_id,
    sqlite_where=item_tags.c.removed_at.is_(None),
    postgresql_where=item_tags.c.removed_at.is_(None),
)


def _set_sqlite_wal(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL may be refused (e.g. network filesystems) — plain journal is fine.
    with contextlib.suppress(Exception):
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory of a file-backed sqlite URL if missing.

    First run on a pristine machine: the default sqlite path lives under a
    directory that may not exist yet, and sqlite fails to open the database
    file with a raw OperationalError instead of creating it (#360). No-op for
    non-sqlite dialects and for memory URLs ('sqlite://', 'sqlite:///:memory:').
    """
    url = sa.engine.make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return
    if not url.database or url.database == ":memory:":
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def get_engine(database_url: str) -> sa.Engine:
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            # In-memory sqlite must share ONE connection (StaticPool): the
            # default per-thread pool hands every new thread its own empty
            # database, and the MCP SDK (2.0) runs sync tools in worker
            # threads — tools would see "no such table" (#429).
            kwargs["poolclass"] = sa.pool.StaticPool

    engine = sa.create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_wal)

    return engine


def create_tables(engine: sa.Engine) -> None:
    metadata.create_all(engine)
    _apply_schema_updates(engine)


def _apply_schema_updates(engine: sa.Engine) -> None:
    """Idempotent schema updates for existing DBs (lightweight migration runner).

    Each schema change (column add, index add, ...) is checked and applied
    INDEPENDENTLY. We do NOT treat a logical group (e.g. "sprint migration")
    as atomic — a partial prior run could leave the column in place but the
    index missing, and we must heal each piece on the next call. This mirrors
    the invariant-per-field rule learned from the update_item resolved_at bug
    (core/items.py TERMINAL_STATUSES).

    Dialect note: PostgreSQL is strict about BOOLEAN literals — `0` (INTEGER)
    does NOT coerce to FALSE, the ALTER fails with DatatypeMismatch; same for
    `1` vs TRUE in index predicates. SQLite is permissive. We emit FALSE/TRUE
    for PostgreSQL and 0/1 for SQLite. Keep this in sync with the
    server_default on the SQLAlchemy column definition (which uses sa.false()
    to portably emit the correct literal at CREATE TABLE time).
    """
    inspector = sa.inspect(engine)
    if "items" not in inspector.get_table_names():
        return

    col_names = {c["name"] for c in inspector.get_columns("items")}

    # #222: opt-in changelog enforcement. Add projects.manages_changelog and,
    # on first add, backfill — a project KEEPS enforcement only if it already
    # has dct changelog history; one with none opts out (so projects registered
    # for item-tracking but keeping their own changelog aren't wrongly blocked).
    # New rows default TRUE via the column server_default; this block is a
    # one-shot (guarded by column absence) so re-runs never override a later
    # explicit `dct project changelog` toggle.
    proj_cols = {c["name"] for c in inspector.get_columns("projects")}
    if "manages_changelog" not in proj_cols:
        is_pg = engine.dialect.name == "postgresql"
        true_lit, false_lit = ("TRUE", "FALSE") if is_pg else ("1", "0")
        with engine.begin() as conn:
            conn.execute(sa.text(
                f"ALTER TABLE projects ADD COLUMN manages_changelog "
                f"BOOLEAN NOT NULL DEFAULT {true_lit}"
            ))
            conn.execute(sa.text(
                f"UPDATE projects SET manages_changelog = {false_lit} "
                "WHERE deleted_at IS NULL AND id NOT IN "
                "(SELECT DISTINCT project_id FROM changelog_entries "
                "WHERE deleted_at IS NULL AND project_id IS NOT NULL)"
            ))

    # #225: per-project changelog location. The ADD COLUMN DEFAULT backfills
    # existing rows to 'changelog.md' (repo root) — no conditional UPDATE needed.
    # Guarded by column absence so re-runs never clobber an explicit value set
    # later via `dct project changelog-path`. VARCHAR default is dialect-portable
    # (unlike the BOOLEAN literal dance above).
    if "changelog_path" not in proj_cols:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "ALTER TABLE projects ADD COLUMN changelog_path "
                "VARCHAR(500) NOT NULL DEFAULT 'changelog.md'"
            ))

    # v0.4.0: add item_checkpoints.kind column + partial index for gate lookup.
    # See item #33 (gate checkpoints). Pre-v0.4 checkpoints default to kind='task'
    # via server_default — existing resolve_item behaviour preserved for rows
    # without explicit gate assignment.
    if "item_checkpoints" in inspector.get_table_names():
        cp_cols = {c["name"] for c in inspector.get_columns("item_checkpoints")}
        cp_idx = {ix["name"] for ix in inspector.get_indexes("item_checkpoints")}
        with engine.begin() as conn:
            if "kind" not in cp_cols:
                conn.execute(sa.text(
                    "ALTER TABLE item_checkpoints ADD COLUMN kind "
                    "VARCHAR(32) NOT NULL DEFAULT 'task'"
                ))
            if "ix_checkpoints_pending_gate" not in cp_idx:
                conn.execute(sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_checkpoints_pending_gate "
                    "ON item_checkpoints(item_id) "
                    "WHERE kind != 'task' AND status = 'pending' AND deleted_at IS NULL"
                ))

    # CP #1236 (ADR #52): creation-provenance columns on every mutating
    # ledger. Purely additive and nullable — no backfill, no literal dance,
    # portable ALTERs; each (table, column) pair heals independently, per
    # this runner's docstring.
    _PROV_SIZES = {"actor_type": 16, "actor_client": 64,
                   "session_id": 128, "source": 16}
    for prov_table in PROVENANCE_TABLES:
        if prov_table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(prov_table)}
        missing = [c for c in PROVENANCE_COLUMNS if c not in existing]
        if not missing:
            continue
        with engine.begin() as conn:
            for col in missing:
                conn.execute(sa.text(
                    f"ALTER TABLE {prov_table} ADD COLUMN {col} "
                    f"VARCHAR({_PROV_SIZES[col]})"
                ))

    # v0.2.0 + #216: migrate the legacy `in_current_sprint` boolean into the
    # item_sprints model, THEN drop the column. Sprint membership now has ONE
    # source of truth (item_sprints + the active sprint) and is derived on read.
    # The seed reads the column via raw SQL (it is intentionally absent from the
    # table metadata), so this whole block self-disables once the column is gone.
    # Order matters: seed BEFORE dropping, and drop the dependent index first
    # (SQLite refuses to drop a column an index still references).
    tables_after = set(inspector.get_table_names())

    # Legacy sprints are HISTORY, not the current sprint — keep them 'completed'.
    # INDEPENDENT, idempotent invariant (deliberately NOT nested under the column
    # check): on a DB where the column was dropped before this demote existed,
    # the legacy sprint would otherwise stay 'active' and wrongly resolve as the
    # current sprint. Heal per-piece (the rule from this runner's docstring).
    if "sprints" in tables_after:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE sprints SET status = 'completed', "
                "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE code = 'legacy' AND status = 'active' AND deleted_at IS NULL"
            ))

    # v0.2.0 + #216: migrate the legacy `in_current_sprint` boolean into the
    # item_sprints model, THEN drop the column. Sprint membership now has ONE
    # source of truth (item_sprints + the active sprint) and is derived on read.
    # The seed reads the column via raw SQL (intentionally absent from the table
    # metadata), so this block self-disables once the column is gone. Order
    # matters: seed BEFORE dropping, drop the dependent index first (SQLite).
    if "in_current_sprint" in col_names and {"sprints", "item_sprints"}.issubset(tables_after):
        _migrate_legacy_sprint_flag(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP INDEX IF EXISTS ix_items_sprint"))
            conn.execute(sa.text("ALTER TABLE items DROP COLUMN in_current_sprint"))

    # K-42 (#413): restore membership links that pre-G1 resolve_item/update_item
    # soft-removed on the terminal transition. Those writes stamped
    # item_sprints.removed_at with the SAME `now` value they wrote to
    # items.resolved_at, so an EXACT equality of the two timestamps identifies a
    # machine-made removal with no false positives worth worrying about — a
    # manual remove_item_from_sprint used a different `now` and MUST stay
    # removed. Without this, historical sprints read done=0 forever (G1 retains
    # membership; the old rows are still there to heal).
    # Idempotent by construction: a restored row has removed_at IS NULL and can
    # never match again.
    if {"items", "item_sprints", "sprints"}.issubset(tables_after):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE item_sprints SET removed_at = NULL "
                "WHERE removed_at IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM items i WHERE i.id = item_sprints.item_id "
                "AND i.resolved_at IS NOT NULL "
                "AND i.resolved_at = item_sprints.removed_at)"
            ))

            # K-26a: cross-project links are legacy damage (an item linked to
            # another project's sprint). REPORT ONLY — deleting them would
            # destroy history we cannot reconstruct, and completion counts
            # already exclude them (_sprint_membership_stats_by_sprint filters
            # on the sprint's project). One stderr line, no mutation.
            stray = conn.execute(sa.text(
                "SELECT COUNT(*) FROM item_sprints ls "
                "JOIN items i ON i.id = ls.item_id "
                "JOIN sprints s ON s.id = ls.sprint_id "
                "WHERE ls.removed_at IS NULL "
                "AND i.deleted_at IS NULL AND s.deleted_at IS NULL "
                "AND i.project_id != s.project_id"
            )).scalar() or 0
        if stray:
            print(
                f"WARNING: dct found {stray} live cross-project sprint "
                "link(s) (item and sprint belong to different projects). "
                "Nothing was changed — review them manually and drop the "
                "bogus ones with remove_item_from_sprint.",
                file=sys.stderr,
            )

    # #611: seed tags / item_tags from the frozen items.tags JSON column. Runs
    # on EVERY bootstrap (no marker row — the facts are the truth) so an item
    # written by a still-running pre-#611 process is adopted next time (K-8).
    _backfill_item_tags(engine)


def _parse_legacy_tags_json(raw) -> tuple[list[str], list]:
    """Tolerant parse of one frozen `items.tags` value -> (names, dropped).

    NEVER raises: this feeds a block that runs on every bootstrap, so a raise
    here would brick every dct command. Bad JSON, a non-list payload and
    non-str / empty elements are dropped and REPORTED by the caller.

    K-7: legacy names are HISTORY, not input to the API — only lowercase/trim
    is applied. The comma and MAX_TAG_LENGTH rules are deliberately NOT
    enforced, so nothing is ever silently lost by length or punctuation.
    """
    if raw is None or raw == "":
        return [], []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [], [raw]
    if not isinstance(parsed, list):
        return [], [raw]

    names: list[str] = []
    dropped: list = []
    for element in parsed:
        if not isinstance(element, str):
            dropped.append(element)
            continue
        normalized = " ".join(element.split()).lower()
        if not normalized:
            dropped.append(element)
            continue
        names.append(normalized)
    return list(dict.fromkeys(names)), dropped


def _backfill_item_tags(engine: sa.Engine) -> None:
    """Seed tags / item_tags from the frozen items.tags JSON column (#611).

    Self-healing, not one-shot (the _migrate_legacy_sprint_flag pattern):
    candidates are items whose JSON column is non-empty and which have NO
    item_tags row at all. The anti-join guard is per-ITEM on purpose — once an
    item has spoken through the join table it owns its tag set, so clearing or
    re-tagging is never undone by the next bootstrap (a per-PAIR guard would
    resurrect removed tags forever, since the frozen column keeps the old JSON).

    K-22 — the ONE write to the frozen column: an item whose JSON yields no
    usable name never gets an item_tags row, so the per-item anti-join would
    re-select it on every bootstrap forever (a write transaction and a repeated
    warning per dct command). After the drop has been REPORTED (stderr, item id
    + the raw values), this function — the column's only sanctioned writer —
    overwrites that item's `items.tags` with '[]', which removes it from the
    candidate set. Scoped to ZERO-yield items: an item that migrated at least
    one name keeps its column as history. `updated_at` is deliberately left
    alone — this is a migration bookkeeping write, not an edit of the item.

    Self-disables once the column is dropped at 2.0.0.
    """
    from dct.core.tags import _active_names_conn, _add_tag_conn, _ensure_link_conn

    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    # NB two different scopes: `tables` is about the tags/item_tags TABLES,
    # `item_cols` about the frozen COLUMN on items. Both must be present.
    if not {"items", "tags", "item_tags"}.issubset(tables):
        return
    item_cols = {c["name"] for c in inspector.get_columns("items")}
    if "tags" not in item_cols:
        return

    no_links = ~(
        sa.select(sa.literal(1))
        .select_from(item_tags)
        .where(item_tags.c.item_id == items.c.id)
        .exists()
    )
    candidates_q = sa.select(items.c.id, items.c.tags, items.c.updated_at).where(
        items.c.tags.isnot(None),
        items.c.tags.notin_(("", "[]")),
        no_links,
    )

    # Steady state is ONE cheap anti-join probe on a read connection — no write
    # transaction is opened when there is nothing to migrate.
    with engine.connect() as conn:
        if conn.execute(candidates_q.limit(1)).first() is None:
            return

    migrated_names: set[str] = set()
    dictionary: list[str] = []
    with engine.begin() as conn:
        # K-9: the candidate set is re-SELECTed INSIDE the write transaction, so
        # the anti-join and the inserts see one consistent snapshot — no window
        # for a concurrent re-tagging to be overwritten.
        for row in conn.execute(candidates_q).fetchall():
            names, dropped = _parse_legacy_tags_json(row.tags)
            if dropped:
                # Printed BEFORE the converging write below is committed: a drop
                # the user never saw is unauditable data loss, so the report can
                # only ever run ahead of the blanking, never behind it.
                print(
                    f"WARNING: dct tag backfill — item #{row.id}: dropped "
                    f"{len(dropped)} unusable tag value(s) {dropped!r}",
                    file=sys.stderr,
                )
            for name in names:
                # No similarity guard here: historical names are not typos to
                # be corrected (the post-run report suggests consolidation).
                tag_id = _add_tag_conn(conn, name, force=True)["id"]
                _ensure_link_conn(conn, row.id, tag_id, row.updated_at)
                migrated_names.add(name)
            if not names:
                # K-22: nothing usable in there and no link to fall back on —
                # converge by emptying the frozen column (see the docstring).
                conn.execute(
                    sa.update(items).where(items.c.id == row.id).values(tags="[]")
                )
        if migrated_names:
            # K-30: read the FULL active dictionary (not just this batch) while
            # the write transaction is still open — the get-or-create above has
            # already populated it, so a legacy 'tests' meets an existing 'test'.
            dictionary = _active_names_conn(conn)

    if migrated_names:
        _report_possible_duplicates(sorted(migrated_names), dictionary)


# K-34: past this many active tag names the post-backfill scan degrades to a
# single pointer line. The scan is O(migrated x dictionary) and migrated names
# are themselves in the dictionary, so this one number bounds the whole report.
_REPORT_SCAN_LIMIT = 300


def _similarity_shortlist(candidate: str, names, threshold: float) -> list[str]:
    """Names that COULD reach `threshold` — a cheap SUPERSET of `_similar`'s hits.

    K-34: SequenceMatcher with autojunk=False is quadratic in the name lengths,
    so it must not run on every pair. Both bounds below are real upper bounds on
    ratio = 2*M/(len(a)+len(b)), because the matched blocks are a common
    subsequence: M <= min(len(a), len(b)) and M <= |multiset(a) & multiset(b)|.
    Anything the shortlist drops therefore could not have been flagged.

    Bucketing by first character was rejected: it drops auth/oauth (.889), a
    pair the guard's own vector table flags. The plural rule (a == b + "s") is
    kept unconditionally — it fires below the ratio threshold (ui/uis at .800).
    """
    counts = Counter(candidate)
    length = len(candidate)
    keep: list[str] = []
    for other in names:
        if other == candidate:
            continue
        if candidate == other + "s" or other == candidate + "s":
            keep.append(other)
            continue
        total = length + len(other)
        if 2 * min(length, len(other)) < threshold * total:
            continue
        if 2 * sum((counts & Counter(other)).values()) < threshold * total:
            continue
        keep.append(other)
    return keep


def _report_possible_duplicates(migrated: list[str], dictionary: list[str]) -> None:
    """Post-backfill consolidation SUGGESTION on stderr — never a block, never
    an auto-merge (that is the user's call, via `dct tags merge` / merge_tag).

    `migrated` is compared against the whole active `dictionary` (K-30), through
    the cheap prefilter and under the hard scan limit (K-34).
    """
    from dct.core.tags import SIMILARITY_THRESHOLD, _similar

    if len(dictionary) > _REPORT_SCAN_LIMIT:
        print(
            "NOTE: dct tag backfill — possible duplicates not scanned "
            f"({len(dictionary)} names); run `dct tags list` to review.",
            file=sys.stderr,
        )
        return

    pairs = set()
    for name in migrated:
        shortlist = _similarity_shortlist(name, dictionary, SIMILARITY_THRESHOLD)
        for other in _similar(name, shortlist):
            pairs.add(tuple(sorted((name, other))))
    for left, right in sorted(pairs):
        print(
            f"NOTE: dct tag backfill — possible duplicates: {left}/{right} — "
            "consider consolidating with `dct tags merge`.",
            file=sys.stderr,
        )


def _migrate_legacy_sprint_flag(engine: sa.Engine) -> None:
    """Seed a 'legacy' sprint per project with flagged items and link them.

    Idempotent — for each project with any item where `in_current_sprint=TRUE`,
    we ensure:
      1. A sprints row with code='legacy' exists (created if missing).
      2. Every flagged item has an item_sprints link ROW to that sprint,
         created once if absent. A link later soft-removed via the v0.2 API
         (removed_at set) is left untouched — this backfill is a one-shot seed,
         NOT an authority that overrides v0.2 sprint-membership decisions.
    Repeat invocations make no further writes once the invariant holds — the
    existence probe spans the FULL (item_id, sprint_id) primary key regardless
    of removed_at, so a re-run never collides with a soft-removed row.

    One-shot seed (#216): reads the legacy `in_current_sprint` column via raw
    SQL because the column is absent from the table metadata and is DROPPED
    right after this seed runs (see _apply_schema_updates). No-op once the
    column is gone, so it is always safe to call. The full-PK existence probe
    means a re-run never collides with a soft-removed link (the #215 lesson).
    """
    from datetime import datetime

    # The column only exists in pre-#216 DBs. Self-disable cleanly otherwise.
    if "in_current_sprint" not in {c["name"] for c in sa.inspect(engine).get_columns("items")}:
        return

    with engine.begin() as conn:
        # Read the legacy column via literal_column (it is absent from the table
        # metadata) while keeping updated_at a TYPED column, so the value stays a
        # datetime and can be reused as item_sprints.assigned_at.
        flagged_rows = conn.execute(
            sa.select(items.c.id, items.c.project_id, items.c.updated_at).where(
                sa.literal_column("in_current_sprint") == sa.true(),
                items.c.deleted_at.is_(None),
            )
        ).fetchall()
        if not flagged_rows:
            return

        by_project: dict[int, list[tuple[int, datetime]]] = {}
        for row in flagged_rows:
            by_project.setdefault(row.project_id, []).append((row.id, row.updated_at))

        for project_id, flagged in by_project.items():
            existing = conn.execute(
                sa.select(sprints.c.id).where(
                    sprints.c.project_id == project_id,
                    sprints.c.code == "legacy",
                    sprints.c.deleted_at.is_(None),
                )
            ).first()
            if existing:
                sprint_id = existing.id
            else:
                row = conn.execute(
                    sprints.insert()
                    .values(
                        project_id=project_id,
                        name="Pre-0.1 legacy sprint",
                        code="legacy",
                        kind="sprint",
                        # Legacy sprint is HISTORY, not the current sprint — it is
                        # 'completed' so it never resolves as the active/current
                        # sprint after #216. Items keep their membership for the
                        # record; the user sets a fresh current sprint via /handoff.
                        status="completed",
                        started_at=datetime.now(UTC),
                        ended_at=datetime.now(UTC),
                    )
                    .returning(sprints.c.id)
                ).first()
                assert row is not None, "INSERT RETURNING must return a row"
                sprint_id = row.id

            # Probe by the FULL primary key (item_id, sprint_id) — deliberately
            # NOT filtered by removed_at. item_sprints_pkey spans both columns
            # irrespective of removed_at, so a soft-removed link still occupies
            # the slot. Filtering removed_at IS NULL here (the original bug)
            # would miss a flagged item whose link was later soft-removed via
            # remove_item_from_sprint, then re-INSERT it -> UniqueViolation.
            already_linked = {
                r.item_id for r in conn.execute(
                    sa.select(item_sprints.c.item_id).where(
                        item_sprints.c.sprint_id == sprint_id,
                    )
                )
            }
            for item_id, updated_at in flagged:
                if item_id in already_linked:
                    continue
                conn.execute(
                    item_sprints.insert().values(
                        item_id=item_id,
                        sprint_id=sprint_id,
                        assigned_at=updated_at,
                    )
                )


def row_to_dict(row: sa.Row) -> dict:
    return dict(row._mapping)
