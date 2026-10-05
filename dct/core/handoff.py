"""Session handoff ledger — prompts left by one session for the next.

Ledger semantics: never hard-deleted, multiple unconsumed rows per project
allowed (parallel sessions, different scopes). Consumer marks consumed_at
(history preserved) or deleted_at (soft discard).
"""

from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.db import row_to_dict, session_handoffs


def _create_handoff_conn(
    conn: sa.Connection,
    project_id: int,
    prompt: str,
    created_by: str,
    scope: str | None = None,
    context_summary: str | None = None,
) -> dict:
    """Insert a handoff on an existing connection. Validation lives HERE so no
    caller can bypass it — and so a ValueError raised inside a composed
    transaction (handoff_work: sprint write first, handoff second) rolls the
    whole operation back, sprint write included."""
    if not prompt or not prompt.strip():
        raise ValueError("Handoff prompt cannot be empty")
    if not created_by or not created_by.strip():
        raise ValueError("created_by cannot be empty — identify the session (model + timestamp)")
    ensure_fits(session_handoffs, {"created_by": created_by, "scope": scope})

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


def create_handoff(
    engine: sa.Engine,
    project_slug: str,
    prompt: str,
    created_by: str,
    scope: str | None = None,
    context_summary: str | None = None,
) -> dict:
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        return _create_handoff_conn(
            conn, project_id, prompt, created_by, scope, context_summary
        )


def _query_unconsumed_handoffs(
    conn: sa.Connection, project_id: int, scope: str | None = None
) -> list[dict]:
    """Unconsumed, undeleted handoffs, newest first (id tie-break) — full rows.

    `scope` narrows the candidates to one workstream tag IN SQL. This is the
    ONE candidate reader behind every pickup_work selection path (K-37, #413):
    auto-consume and consume=true share it, so the scope contract cannot drift
    between them. Conn-level so composed ops (pickup_work, get_sprint_review)
    read it inside their own transaction.
    """
    q = sa.select(session_handoffs).where(
        session_handoffs.c.project_id == project_id,
        session_handoffs.c.consumed_at.is_(None),
        session_handoffs.c.deleted_at.is_(None),
    )
    if scope:
        q = q.where(session_handoffs.c.scope == scope)
    handoff_rows = conn.execute(
        q.order_by(session_handoffs.c.created_at.desc(), session_handoffs.c.id.desc())
    ).fetchall()
    return [row_to_dict(r) for r in handoff_rows]


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
        q = q.order_by(session_handoffs.c.created_at.desc(), session_handoffs.c.id.desc())
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


def _consume_handoff_conn(
    conn: sa.Connection,
    handoff_id: int,
    consumed_by: str | None = None,
) -> dict | None:
    """UPDATE + re-read on ONE connection. The pre-G1 version updated in one
    transaction and re-fetched in a second — a concurrent soft-delete between
    them made a SUCCESSFUL consume return None. Single-connection collapse
    closes that window and lets composed ops consume inside their own txn."""
    ensure_fits(session_handoffs, {"consumed_by": consumed_by})
    now = datetime.now(UTC)
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
    row = conn.execute(
        sa.select(session_handoffs).where(session_handoffs.c.id == handoff_id)
    ).first()
    return row_to_dict(row) if row else None


def consume_handoff(
    engine: sa.Engine,
    handoff_id: int,
    consumed_by: str | None = None,
) -> dict | None:
    with engine.begin() as conn:
        return _consume_handoff_conn(conn, handoff_id, consumed_by)


def delete_handoff(engine: sa.Engine, handoff_id: int) -> bool:
    """Soft delete — preserves row for audit but hides from list_handoffs."""
    with engine.begin() as conn:
        result = conn.execute(
            session_handoffs.update()
            .where(
                session_handoffs.c.id == handoff_id,
                session_handoffs.c.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC))
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
    ensure_fits(session_handoffs, values)

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
