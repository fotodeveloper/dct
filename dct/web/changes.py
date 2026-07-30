"""Cheap composite change-token for SSE live refresh.

The token is two halves joined by ':':
  1. max(updated_at) across every table that carries an updated_at column
     — advances on any UPDATE (core stamps updated_at with microsecond precision).
  2. per-table live row counts (deleted_at IS NULL; item_sprints uses removed_at
     IS NULL) — advances on any INSERT or SOFT-DELETE, including child rows
     (notes, checkpoints, links) that never touch a parent's updated_at.

Stable when the DB is unchanged. Read-only: SELECTs only, never DDL.
"""

import sqlalchemy as sa

from dct.db import (
    changelog_entries,
    decisions,
    item_checkpoints,
    item_notes,
    item_sprints,
    items,
    plan_checkpoints,
    plan_sections,
    plans,
    projects,
    session_handoffs,
    sprints,
)

# Tables carrying an updated_at column — feed the freshness half of the token.
_UPDATED_AT_TABLES = (
    projects,
    items,
    item_checkpoints,
    changelog_entries,
    sprints,
    plans,
    plan_sections,
    plan_checkpoints,
    decisions,
)

# (table, live_predicate) — count of non-deleted rows feeds the count half.
# item_notes / session_handoffs / item_sprints have no updated_at, so their
# mutations are visible only through this row-count half.
_COUNT_TABLES = (
    (projects, projects.c.deleted_at.is_(None)),
    (items, items.c.deleted_at.is_(None)),
    (item_notes, item_notes.c.deleted_at.is_(None)),
    (item_checkpoints, item_checkpoints.c.deleted_at.is_(None)),
    (changelog_entries, changelog_entries.c.deleted_at.is_(None)),
    (session_handoffs, session_handoffs.c.deleted_at.is_(None)),
    (sprints, sprints.c.deleted_at.is_(None)),
    (plans, plans.c.deleted_at.is_(None)),
    (plan_sections, plan_sections.c.deleted_at.is_(None)),
    (plan_checkpoints, plan_checkpoints.c.deleted_at.is_(None)),
    (item_sprints, item_sprints.c.removed_at.is_(None)),
    (decisions, decisions.c.deleted_at.is_(None)),
)


def change_token(engine: sa.Engine) -> str:
    """Return a cheap composite token that changes on any tracked mutation."""
    parts: list[str] = []
    with engine.connect() as conn:
        newest = None
        for table in _UPDATED_AT_TABLES:
            value = conn.execute(sa.select(sa.func.max(table.c.updated_at))).scalar()
            if value is not None and (newest is None or value > newest):
                newest = value
        parts.append(str(newest) if newest is not None else "-")
        for table, predicate in _COUNT_TABLES:
            count = conn.execute(
                sa.select(sa.func.count()).select_from(table).where(predicate)
            ).scalar()
            parts.append(str(count or 0))
    return ":".join(parts)
