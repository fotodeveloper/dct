"""Session handoff ledger — prompts left by one session for the next.

Ledger semantics: never hard-deleted, multiple unconsumed rows per project
allowed (parallel sessions, different scopes). Consumer marks consumed_at
(history preserved) or deleted_at (soft discard).
"""

from datetime import datetime, timezone

import sqlalchemy as sa

from dct.db import session_handoffs, row_to_dict
from dct.core.projects import resolve_project_id


def create_handoff(
    engine: sa.Engine,
    project_slug: str,
    prompt: str,
    created_by: str,
    scope: str | None = None,
    context_summary: str | None = None,
) -> dict:
    if not prompt or not prompt.strip():
        raise ValueError("Handoff prompt cannot be empty")
    if not created_by or not created_by.strip():
        raise ValueError("created_by cannot be empty — identify the session (model + timestamp)")

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        row = conn.execute(
            session_handoffs.insert()
            .values(
                project_id=project_id,
                prompt=prompt,
                context_summary=context_summary,
                scope=scope,
                created_by=created_by,
            )
            .returning(session_handoffs)
        ).first()
        assert row is not None
        return row_to_dict(row)


def list_handoffs(
    engine: sa.Engine,
    project_slug: str | None = None,
    unconsumed_only: bool = True,
) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(session_handoffs).where(session_handoffs.c.deleted_at.is_(None))
        if project_slug:
            project_id = resolve_project_id(conn, project_slug)
            q = q.where(session_handoffs.c.project_id == project_id)
        if unconsumed_only:
            q = q.where(session_handoffs.c.consumed_at.is_(None))
        q = q.order_by(session_handoffs.c.created_at.desc())
        return [row_to_dict(r) for r in conn.execute(q)]


def get_handoff(engine: sa.Engine, handoff_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(session_handoffs).where(
                session_handoffs.c.id == handoff_id,
                session_handoffs.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def consume_handoff(
    engine: sa.Engine,
    handoff_id: int,
    consumed_by: str | None = None,
) -> dict | None:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            session_handoffs.update()
            .where(
                session_handoffs.c.id == handoff_id,
                session_handoffs.c.deleted_at.is_(None),
                session_handoffs.c.consumed_at.is_(None),
            )
            .values(consumed_at=now, consumed_by=consumed_by)
        )
        if result.rowcount == 0:
            return None
    return get_handoff(engine, handoff_id)


def delete_handoff(engine: sa.Engine, handoff_id: int) -> bool:
    """Soft delete — preserves row for audit but hides from list_handoffs."""
    with engine.begin() as conn:
        result = conn.execute(
            session_handoffs.update()
            .where(
                session_handoffs.c.id == handoff_id,
                session_handoffs.c.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0


def update_handoff(
    engine: sa.Engine,
    handoff_id: int,
    prompt: str | None = None,
    scope: str | None = None,
    context_summary: str | None = None,
) -> dict | None:
    """Amend an UNCONSUMED handoff in place — preserves id + created_at.

    Only fields passed (not None) are changed; None means "leave unchanged".
    Consumed or soft-deleted handoffs are immutable ledger entries: returns
    None (no row matches the filter). Also returns None when the id does not
    exist. This is the in-place alternative to the supersede pattern
    (delete_handoff + create_handoff), so the next session sees the latest
    draft without the id churning.

    Raises ValueError if prompt is given but blank, or if no field is given.
    """
    if prompt is not None and not prompt.strip():
        raise ValueError("Handoff prompt cannot be empty")

    values: dict = {}
    if prompt is not None:
        values["prompt"] = prompt
    if scope is not None:
        values["scope"] = scope
    if context_summary is not None:
        values["context_summary"] = context_summary
    if not values:
        raise ValueError("Nothing to update — pass prompt, scope, or context_summary")

    with engine.begin() as conn:
        result = conn.execute(
            session_handoffs.update()
            .where(
                session_handoffs.c.id == handoff_id,
                session_handoffs.c.deleted_at.is_(None),
                session_handoffs.c.consumed_at.is_(None),
            )
            .values(**values)
        )
        if result.rowcount == 0:
            return None
    return get_handoff(engine, handoff_id)
