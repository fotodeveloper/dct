"""Checkpoint CRUD — acceptance criteria tied to items with ledger semantics."""

from datetime import datetime, timezone

import sqlalchemy as sa

from dct.db import item_checkpoints, items, row_to_dict

VALID_STATUSES = {"pending", "done", "rejected"}

# Known checkpoint kinds. 'task' is the default and does NOT block resolve_item.
# Any non-'task' value is treated as a gate — resolve_item refuses until done
# or rejected. The set is intentionally open (see `kind` column docstring in
# db.py) — OSS users may define custom gate kinds; the validator accepts any
# non-empty string, the whitelist is advisory (warning for typos in future).
KNOWN_KINDS = {"task", "dogfood", "security", "approval", "review", "cc-task"}


def _validate_status(status: str) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: '{status}'. Valid: {', '.join(sorted(VALID_STATUSES))}")
    return status


def _validate_kind(kind: str) -> str:
    if not kind or not kind.strip():
        raise ValueError("Checkpoint kind cannot be empty")
    return kind


def add_checkpoint(
    engine: sa.Engine,
    item_id: int,
    text: str,
    sort_order: int | None = None,
    kind: str = "task",
) -> dict:
    """Create a checkpoint on an item.

    Args:
        kind: 'task' (default, non-blocking) or a gate kind:
              - 'dogfood'  — user-facing feature awaiting fresh-session validation
              - 'security' — security review required
              - 'approval' — stakeholder sign-off
              - 'review'   — code review
              - 'cc-task'  — mirrored from Claude Code TaskCreate (non-blocking)
              Any non-'task' kind blocks `resolve_item` until the checkpoint is
              completed or rejected (reject = 'not applicable').
    """
    if not text or not text.strip():
        raise ValueError("Checkpoint text cannot be empty")
    _validate_kind(kind)

    with engine.begin() as conn:
        exists = conn.execute(
            sa.select(items.c.id).where(items.c.id == item_id, items.c.deleted_at.is_(None))
        ).first()
        if not exists:
            raise ValueError(f"Item #{item_id} not found")

        if sort_order is None:
            max_row = conn.execute(
                sa.select(sa.func.max(item_checkpoints.c.sort_order))
                .where(item_checkpoints.c.item_id == item_id, item_checkpoints.c.deleted_at.is_(None))
            ).first()
            max_order = (max_row[0] if max_row else None) or 0
            sort_order = max_order + 10

        row = conn.execute(
            item_checkpoints.insert()
            .values(item_id=item_id, text=text, sort_order=sort_order, kind=kind)
            .returning(item_checkpoints)
        ).first()
        assert row is not None
        return row_to_dict(row)


def list_pending_gates(engine: sa.Engine, item_id: int) -> list[dict]:
    """Return pending gate checkpoints (kind != 'task', status = 'pending') for item.
    Used by resolve_item to block resolution until gates close. cc-task is
    treated as non-gating even though kind != 'task' — it's a mirror class, not
    a gate. Use the explicit filter in resolve_item rather than hardcoding here
    so future gate kinds are captured by default.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(item_checkpoints)
            .where(
                item_checkpoints.c.item_id == item_id,
                item_checkpoints.c.kind.notin_(["task", "cc-task"]),
                item_checkpoints.c.status == "pending",
                item_checkpoints.c.deleted_at.is_(None),
            )
            .order_by(item_checkpoints.c.sort_order)
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def list_checkpoints(
    engine: sa.Engine,
    item_id: int,
    include_deleted: bool = False,
    include_rejected: bool = False,
) -> list[dict]:
    """List checkpoints for an item.

    Defaults hide rejected and deleted — the typical caller wants the active
    TODO list (pending + done). Pass include_rejected=True for full audit
    view (e.g. "what did we plan and abandon?"). Note: get_item() does NOT
    use this filter — a single-item view always returns all checkpoints,
    because the caller explicitly asked about that item.
    """
    with engine.begin() as conn:
        q = (
            sa.select(item_checkpoints)
            .where(item_checkpoints.c.item_id == item_id)
            .order_by(item_checkpoints.c.sort_order, item_checkpoints.c.created_at)
        )
        if not include_deleted:
            q = q.where(item_checkpoints.c.deleted_at.is_(None))
        if not include_rejected:
            q = q.where(item_checkpoints.c.status != "rejected")
        return [row_to_dict(r) for r in conn.execute(q)]


def get_checkpoint(engine: sa.Engine, checkpoint_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(item_checkpoints).where(
                item_checkpoints.c.id == checkpoint_id,
                item_checkpoints.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def set_status(engine: sa.Engine, checkpoint_id: int, status: str) -> dict | None:
    _validate_status(status)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            item_checkpoints.update()
            .where(item_checkpoints.c.id == checkpoint_id, item_checkpoints.c.deleted_at.is_(None))
            .values(status=status, updated_at=now)
        )
        if result.rowcount == 0:
            return None
    return get_checkpoint(engine, checkpoint_id)


def update_text(engine: sa.Engine, checkpoint_id: int, text: str) -> dict | None:
    if not text or not text.strip():
        raise ValueError("Checkpoint text cannot be empty")
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            item_checkpoints.update()
            .where(item_checkpoints.c.id == checkpoint_id, item_checkpoints.c.deleted_at.is_(None))
            .values(text=text, updated_at=now)
        )
        if result.rowcount == 0:
            return None
    return get_checkpoint(engine, checkpoint_id)


def delete_checkpoint(engine: sa.Engine, checkpoint_id: int) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            item_checkpoints.update()
            .where(item_checkpoints.c.id == checkpoint_id, item_checkpoints.c.deleted_at.is_(None))
            .values(deleted_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0


def get_progress(engine: sa.Engine, item_id: int) -> dict:
    """Compute checkpoint progress for an item.

    Returns: {total, done, rejected, pending, active, percent}
    - active = total - rejected (things that actually need to be done)
    - percent = done / active * 100 (0 if active == 0)
    """
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(item_checkpoints.c.status, sa.func.count())
            .where(item_checkpoints.c.item_id == item_id, item_checkpoints.c.deleted_at.is_(None))
            .group_by(item_checkpoints.c.status)
        ).fetchall()

    counts = {"pending": 0, "done": 0, "rejected": 0}
    for status, count in rows:
        counts[status] = count

    total = counts["pending"] + counts["done"] + counts["rejected"]
    active = counts["pending"] + counts["done"]
    percent = round(counts["done"] / active * 100) if active else 0

    return {
        "total": total,
        "done": counts["done"],
        "rejected": counts["rejected"],
        "pending": counts["pending"],
        "active": active,
        "percent": percent,
    }
