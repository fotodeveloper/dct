"""Checkpoint CRUD — acceptance criteria tied to items with ledger semantics."""

from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.text import item_label
from dct.db import item_checkpoints, items, lock_item_row, row_to_dict

VALID_STATUSES = {"pending", "done", "rejected"}

# Known checkpoint kinds. 'task' is the default and does NOT block resolve_item.
# Every kind outside NON_GATE_KINDS is a gate — resolve_item refuses until done
# or rejected. The set is intentionally OPEN (see the `kind` column comment in
# db.py) — users may define custom gate kinds. add_checkpoint refuses only a
# blank kind and one longer than the column (ensure_fits); KNOWN_KINDS is a
# list of the documented kinds, never a whitelist.
KNOWN_KINDS = {"task", "dogfood", "security", "approval", "review", "cc-task"}

# The kinds that do NOT gate resolve_item: 'task' (plain step) and 'cc-task'
# (mirrored Claude Code todo — a mirror class, not a gate). EVERY other kind
# is a gate. Single source of truth for the gate filter, shared by the core
# readers below, analytics and the web layer; a TUPLE so it drops straight
# into `kind.notin_(...)`.
NON_GATE_KINDS = ("task", "cc-task")


def _validate_status(status: str) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: '{status}'. Valid: {', '.join(sorted(VALID_STATUSES))}")
    return status


_CHECKPOINT_LABELS = {"kind": "Checkpoint kind", "text": "Checkpoint text"}


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
              Every kind but 'task' / 'cc-task' blocks `resolve_item` until
              the checkpoint is completed or rejected (reject = 'not
              applicable'). Any non-blank kind is accepted; a blank one, or
              one longer than the `kind` column, raises ValueError (refused,
              never truncated). The text has no length limit.
    """
    if not text or not text.strip():
        raise ValueError("Checkpoint text cannot be empty")
    _validate_kind(kind)
    # K-65: widths come from the schema; the text column is unbounded, so
    # only the kind can be refused here.
    ensure_fits(item_checkpoints, {"kind": kind, "text": text}, _CHECKPOINT_LABELS)

    with engine.begin() as conn:
        # The existence check is the item row lock (#910): a gate created here
        # lands before or after a concurrent close, never inside its guard.
        if not lock_item_row(conn, item_id):
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


def pending_gates_conn(conn: sa.Connection, item_id: int) -> list[dict]:
    """Pending gate checkpoints (kind outside NON_GATE_KINDS, status =
    'pending') of the item, read on the CALLER's connection — so the gate
    guard of a terminal transition (items._refuse_pending_gates_conn) runs in
    the same transaction as the status write. cc-task is treated as
    non-gating even though kind != 'task' — it's a mirror class, not a gate.
    The filter is NON_GATE_KINDS-driven (exclusion, not a whitelist), so a
    future gate kind is captured by default.
    """
    rows = conn.execute(
        sa.select(item_checkpoints)
        .where(
            item_checkpoints.c.item_id == item_id,
            item_checkpoints.c.kind.notin_(NON_GATE_KINDS),
            item_checkpoints.c.status == "pending",
            item_checkpoints.c.deleted_at.is_(None),
        )
        .order_by(item_checkpoints.c.sort_order)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def list_pending_gates(engine: sa.Engine, item_id: int) -> list[dict]:
    """Return pending gate checkpoints for item — the engine-level reader over
    pending_gates_conn (same filter, own transaction)."""
    with engine.begin() as conn:
        return pending_gates_conn(conn, item_id)


def _sprint_checkpoints_base(project_id: int, *columns: sa.ColumnElement) -> sa.Select:
    """SELECT skeleton of the sprint-scoped checkpoint reader:
    LIVE checkpoints of LIVE items that are in ANY active sprint of the
    project (membership via _current_sprint_exists, so the multi-sprint and
    terminal-member rules are defined in exactly one place).

    `columns` are the projected columns; callers add their own status/kind
    filters and ORDER BY. No ordering is applied here — an unordered base
    would be a bug in a paged reader, so every caller must state its own."""
    from dct.core.items import (
        _current_sprint_exists,  # local: items imports checkpoints
    )

    return (
        sa.select(*columns)
        .select_from(item_checkpoints.join(items, items.c.id == item_checkpoints.c.item_id))
        .where(
            items.c.project_id == project_id,
            items.c.deleted_at.is_(None),
            item_checkpoints.c.deleted_at.is_(None),
            _current_sprint_exists(),
        )
    )


def _with_item_label(mapping, *, title_key: str, keep_title: bool = True) -> dict:
    """Row dict for a row that REFERENCES an item: the raw short_title and
    title columns become `item_short_title` via the one rule
    (dct.core.text.item_label); the title stays under `title_key` only when
    `keep_title`."""
    d = dict(mapping)
    short_title = d.pop("short_title", None)
    title = d[title_key] if keep_title else d.pop(title_key)
    d["item_short_title"] = item_label(short_title, title)
    return d


def _query_pending_gates(conn: sa.Connection, project_id: int) -> list[dict]:
    """Pending gate checkpoints on EVERY open item of the project, in a sprint
    or not (#947: an out-of-sprint item awaiting dogfood was invisible) — the
    project-wide companion to list_pending_gates (which stays per-item as
    resolve_item's guard). One query, no N+1. Ordered P1-first, then
    item id, checkpoint sort_order, id tie-break.

    Row shape: {item_id, checkpoint_id, kind, priority, in_current_sprint,
    item_short_title, item_title, text} — in_current_sprint is the derived
    flag (dct.core.items._current_sprint_exists); item_short_title is
    dct.core.text.item_label (the item's short_title, else its title
    compacted to 50 chars).
    """
    from dct.core.items import (  # local: items imports checkpoints
        TERMINAL_STATUSES,
        _current_sprint_exists,
        _priority_order_expr,
    )

    rows = conn.execute(
        sa.select(
            item_checkpoints.c.item_id,
            item_checkpoints.c.id.label("checkpoint_id"),
            item_checkpoints.c.kind,
            items.c.priority,
            _current_sprint_exists().label("in_current_sprint"),
            items.c.short_title,
            items.c.title.label("item_title"),
            item_checkpoints.c.text,
        )
        .select_from(item_checkpoints.join(items, items.c.id == item_checkpoints.c.item_id))
        .where(
            items.c.project_id == project_id,
            items.c.deleted_at.is_(None),
            items.c.status.notin_(TERMINAL_STATUSES),
            item_checkpoints.c.deleted_at.is_(None),
            item_checkpoints.c.kind.notin_(NON_GATE_KINDS),
            item_checkpoints.c.status == "pending",
        )
        .order_by(_priority_order_expr(), item_checkpoints.c.item_id,
                  item_checkpoints.c.sort_order, item_checkpoints.c.id)
    ).fetchall()
    out = []
    for r in rows:
        d = _with_item_label(r._mapping, title_key="item_title")
        d["in_current_sprint"] = bool(d["in_current_sprint"])
        out.append(d)
    return out


def _query_sprint_item_checkpoints(conn: sa.Connection, project_id: int) -> list[dict]:
    """All live, non-rejected checkpoints of items currently in an active
    sprint — the snapshot's TODO map (a resuming agent reads the next pending
    step straight from here, #413 K-9). Rejected checkpoints are hidden (same
    rule as list_checkpoints' default). Ordered item_id, sort_order, id.

    Row shape: {item_id, id, status, kind, sort_order, item_short_title, text}
    — item_short_title is dct.core.text.item_label of the owning item.
    """
    rows = conn.execute(
        _sprint_checkpoints_base(
            project_id,
            item_checkpoints.c.item_id,
            item_checkpoints.c.id,
            item_checkpoints.c.status,
            item_checkpoints.c.kind,
            item_checkpoints.c.sort_order,
            items.c.short_title,
            items.c.title.label("item_title"),
            item_checkpoints.c.text,
        )
        .where(item_checkpoints.c.status != "rejected")
        .order_by(item_checkpoints.c.item_id, item_checkpoints.c.sort_order, item_checkpoints.c.id)
    ).fetchall()
    return [
        _with_item_label(r._mapping, title_key="item_title", keep_title=False)
        for r in rows
    ]


def list_project_pending_gates(engine: sa.Engine, project_slug: str) -> list[dict]:
    """Public wrapper over _query_pending_gates — resolves the slug and runs
    in its own transaction. See the conn-level docstring for the shape."""
    from dct.core.projects import resolve_project_id

    with engine.begin() as conn:
        return _query_pending_gates(conn, resolve_project_id(conn, project_slug))


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
    """Set a checkpoint's status → its dict, or None for an unknown /
    soft-deleted id. Reopening a gate re-blocks the close of its item, so the
    write queues on the item row (#910) like add_checkpoint and the close."""
    _validate_status(status)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        owner = conn.execute(
            sa.select(item_checkpoints.c.item_id)
            .where(item_checkpoints.c.id == checkpoint_id, item_checkpoints.c.deleted_at.is_(None))
        ).first()
        if owner is None or not lock_item_row(conn, owner.item_id):
            return None
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
    now = datetime.now(UTC)
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
            .values(deleted_at=datetime.now(UTC))
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
