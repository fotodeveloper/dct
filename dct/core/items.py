"""Item CRUD operations with soft deletes and validation."""

import json
from datetime import datetime, timezone

import sqlalchemy as sa

from dct.db import items, item_notes, item_checkpoints, item_sprints, sprints, row_to_dict
from dct.core.projects import resolve_project_id

VALID_TYPES = {"issue", "todo", "feature", "idea", "improvement"}
VALID_PRIORITIES = {"P1", "P2", "P3", "future"}
VALID_STATUSES = {"open", "in_progress", "resolved", "wont_fix"}
TERMINAL_STATUSES = {"resolved", "wont_fix"}
OPEN_STATUSES = {"open", "in_progress"}


def _validate_enum(value: str, valid: set[str], field_name: str) -> str:
    if value not in valid:
        raise ValueError(f"Invalid {field_name}: '{value}'. Valid: {', '.join(sorted(valid))}")
    return value


def _serialize_tags(tags: list[str] | None) -> str:
    """Serialize tags for DB storage. Accepts list[str] only — callers that
    receive CSV/string input (CLI `--tags "a,b"`) MUST split into list before
    calling core. Historical bug: accepting `str` here meant MCP clients that
    couldn't send array types ended up passing the JSON-stringified list,
    which was then naively split on `,` into mangled fragments. Fix lifts
    normalization to the boundary (MCP = list[str] schema; CLI = split in
    `cmd_add`); core stays pure.
    """
    if tags is None:
        return "[]"
    assert isinstance(tags, list), f"tags must be list[str], got {type(tags).__name__}"
    return json.dumps([t for t in tags if t])


def _parse_tags(tags_str: str | None) -> list[str]:
    if not tags_str:
        return []
    try:
        return json.loads(tags_str)
    except (json.JSONDecodeError, TypeError):
        return []


def _current_sprint_exists():
    """Correlated EXISTS: the item has an active item_sprints link to its
    project's active sprint. This IS `in_current_sprint` (#216) — derived on
    read, never stored. Use as a labeled column in SELECT or directly in WHERE."""
    return (
        sa.select(sa.literal(1))
        .select_from(item_sprints.join(sprints, sprints.c.id == item_sprints.c.sprint_id))
        .where(
            item_sprints.c.item_id == items.c.id,
            item_sprints.c.removed_at.is_(None),
            sprints.c.status == "active",
            sprints.c.deleted_at.is_(None),
        )
        .exists()
    )


def _item_to_dict(row: sa.Row) -> dict:
    d = row_to_dict(row)
    d["tags"] = _parse_tags(d.get("tags"))
    if "in_current_sprint" in d:  # present only when the derived column was selected
        d["in_current_sprint"] = bool(d["in_current_sprint"])
    return d


def _item_with_cp_to_dict(row_mapping) -> dict:
    """Convert an items row enriched with cp_done/cp_total aggregates into a dict
    with a compact `cp: [done, total]` field replacing the two raw columns.
    """
    d = dict(row_mapping)
    d["tags"] = _parse_tags(d.get("tags"))
    if "in_current_sprint" in d:
        d["in_current_sprint"] = bool(d["in_current_sprint"])
    done = int(d.pop("cp_done", 0) or 0)
    total = int(d.pop("cp_total", 0) or 0)
    d["cp"] = [done, total]
    return d


def create_item(
    engine: sa.Engine,
    project_slug: str,
    item_type: str,
    title: str,
    description: str | None = None,
    priority: str = "P2",
    status: str = "open",
    component: str | None = None,
    source_file: str | None = None,
    source_lines: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    if not title or not title.strip():
        raise ValueError("Item title cannot be empty")
    _validate_enum(item_type, VALID_TYPES, "item_type")
    _validate_enum(priority, VALID_PRIORITIES, "priority")
    _validate_enum(status, VALID_STATUSES, "status")

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        row = conn.execute(
            items.insert()
            .values(
                project_id=project_id,
                item_type=item_type,
                title=title,
                description=description,
                priority=priority,
                status=status,
                component=component,
                source_file=source_file,
                source_lines=source_lines,
                tags=_serialize_tags(tags),
            )
            .returning(items)
        ).first()
        assert row is not None
        d = _item_to_dict(row)
        d["in_current_sprint"] = False  # a brand-new item belongs to no sprint
        return d


def list_items(
    engine: sa.Engine,
    project_slug: str | None = None,
    status: str | None = None,
    item_type: str | None = None,
    priority: str | None = None,
    component: str | None = None,
    search: str | None = None,
    in_current_sprint: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    # Priority ordering: P1 first, future last
    priority_order = sa.case(
        (items.c.priority == "P1", 1),
        (items.c.priority == "P2", 2),
        (items.c.priority == "P3", 3),
        (items.c.priority == "future", 4),
        else_=5,
    )

    # Checkpoint progress aggregated in a single query — avoids N+1.
    # cp_done = count of done, cp_total = done + pending (rejected excluded from "real" total).
    cp_done = sa.func.coalesce(
        sa.func.sum(sa.case((item_checkpoints.c.status == "done", 1), else_=0)),
        0,
    ).label("cp_done")
    cp_total = sa.func.coalesce(
        sa.func.sum(sa.case((item_checkpoints.c.status != "rejected", 1), else_=0)),
        0,
    ).label("cp_total")

    q = (
        sa.select(items, cp_done, cp_total, _current_sprint_exists().label("in_current_sprint"))
        .select_from(
            items.outerjoin(
                item_checkpoints,
                sa.and_(
                    item_checkpoints.c.item_id == items.c.id,
                    item_checkpoints.c.deleted_at.is_(None),
                ),
            )
        )
        .where(items.c.deleted_at.is_(None))
        .group_by(items.c.id)
        .order_by(priority_order, items.c.created_at.desc())
    )

    if status:
        q = q.where(items.c.status == status)
    if item_type:
        q = q.where(items.c.item_type == item_type)
    if priority:
        q = q.where(items.c.priority == priority)
    if component:
        q = q.where(items.c.component == component)
    if in_current_sprint is not None:
        exists_active = _current_sprint_exists()
        q = q.where(exists_active if in_current_sprint else ~exists_active)
    if search:
        pattern = f"%{search}%"
        q = q.where(sa.or_(items.c.title.ilike(pattern), items.c.description.ilike(pattern)))
    if limit:
        q = q.limit(limit)

    with engine.begin() as conn:
        if project_slug:
            pid = resolve_project_id(conn, project_slug)
            q = q.where(items.c.project_id == pid)
        return [_item_with_cp_to_dict(r._mapping) for r in conn.execute(q)]


def get_item(engine: sa.Engine, item_id: int) -> dict | None:
    from dct.db import item_checkpoints
    from dct.core.checkpoints import get_progress

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(items, _current_sprint_exists().label("in_current_sprint"))
            .where(items.c.id == item_id, items.c.deleted_at.is_(None))
        ).first()
        if not row:
            return None
        d = _item_to_dict(row)
        notes = conn.execute(
            sa.select(item_notes)
            .where(item_notes.c.item_id == item_id, item_notes.c.deleted_at.is_(None))
            .order_by(item_notes.c.created_at)
        ).fetchall()
        d["notes"] = [row_to_dict(n) for n in notes]

        checkpoints = conn.execute(
            sa.select(item_checkpoints)
            .where(item_checkpoints.c.item_id == item_id, item_checkpoints.c.deleted_at.is_(None))
            .order_by(item_checkpoints.c.sort_order, item_checkpoints.c.created_at)
        ).fetchall()
        d["checkpoints"] = [row_to_dict(c) for c in checkpoints]

    d["progress"] = get_progress(engine, item_id)
    return d


def update_item(engine: sa.Engine, item_id: int, **kwargs) -> dict | None:
    # v0.2+ guard: direct writes to `in_current_sprint` are forbidden.
    # The boolean is now derived from item_sprints; callers must use
    # assign_item_to_sprint / remove_item_from_sprint instead. Internal
    # auto-clear on terminal status transitions remains (see below).
    if "in_current_sprint" in kwargs:
        raise ValueError(
            "items.in_current_sprint is deprecated in v0.2.0 — use "
            "assign_item_to_sprint(item_id, sprint_id) or "
            "remove_item_from_sprint(item_id, sprint_id) instead. "
            "See docs/superpowers/specs/2026-04-17-plans-roadmaps-decisions-design.md §5.2."
        )

    allowed = {"title", "description", "priority", "status", "component", "source_file", "source_lines", "tags", "resolution", "item_type"}
    values = {}
    for k, v in kwargs.items():
        if k not in allowed or v is None:
            continue
        if k == "priority":
            _validate_enum(v, VALID_PRIORITIES, "priority")
        elif k == "status":
            _validate_enum(v, VALID_STATUSES, "status")
        elif k == "item_type":
            _validate_enum(v, VALID_TYPES, "item_type")
        elif k == "tags":
            v = _serialize_tags(v)
        if v == "" and k in {"component", "source_file", "source_lines", "resolution", "description"}:
            v = None
        values[k] = v

    if not values:
        raise ValueError("No valid fields to update")

    now = datetime.now(timezone.utc)
    values["updated_at"] = now

    # Invariant: terminal status ↔ resolved_at set, AND terminal status ⇒ the
    # item leaves the current sprint. Membership lives in item_sprints now, so
    # "leave the sprint" = soft-remove its active links (no boolean to flip).
    leaving_sprint = False
    if "status" in values:
        if values["status"] in TERMINAL_STATUSES:
            values.setdefault("resolved_at", now)
            leaving_sprint = True
        elif values["status"] in OPEN_STATUSES:
            values["resolved_at"] = None

    with engine.begin() as conn:
        result = conn.execute(items.update().where(items.c.id == item_id, items.c.deleted_at.is_(None)).values(**values))
        if result.rowcount == 0:
            return None
        if leaving_sprint:
            conn.execute(
                item_sprints.update()
                .where(item_sprints.c.item_id == item_id, item_sprints.c.removed_at.is_(None))
                .values(removed_at=now)
            )

    return get_item(engine, item_id)


def resolve_item(
    engine: sa.Engine,
    item_id: int,
    status: str = "resolved",
    resolution: str | None = None,
) -> dict | None:
    """Transition item to terminal status (resolved / wont_fix).

    Gate check: if any pending gate checkpoint exists for this item
    (kind not in {'task','cc-task'}, status='pending'), raises ValueError.
    Gates must be explicitly completed (done) or rejected (not applicable).

    Dogfood gate rationale: user-facing features (new skills, CLI commands,
    MCP tools) carry a `kind='dogfood'` checkpoint as their last one — this
    enforces that the feature was validated by the user in a fresh session,
    not just self-reviewed by the implementer. See CLAUDE.md "Gate
    checkpoints" section.
    """
    from dct.core.checkpoints import list_pending_gates

    _validate_enum(status, {"resolved", "wont_fix"}, "resolve status")

    pending_gates = list_pending_gates(engine, item_id)
    if pending_gates:
        gate_summary = ", ".join(f"#{g['id']} [{g['kind']}]" for g in pending_gates[:3])
        more = f" (+{len(pending_gates) - 3} more)" if len(pending_gates) > 3 else ""
        raise ValueError(
            f"Cannot resolve item #{item_id}: {len(pending_gates)} gate checkpoint(s) "
            f"pending — {gate_summary}{more}. Complete them (done) or reject them "
            f"(not applicable) before resolving. See CLAUDE.md 'Gate checkpoints'."
        )

    now = datetime.now(timezone.utc)
    values: dict = {
        "status": status,
        "resolved_at": now,
        "updated_at": now,
    }
    if resolution:
        values["resolution"] = resolution

    with engine.begin() as conn:
        result = conn.execute(items.update().where(items.c.id == item_id, items.c.deleted_at.is_(None)).values(**values))
        if result.rowcount == 0:
            return None
        # Terminal status ⇒ leave the current sprint: soft-remove active links.
        conn.execute(
            item_sprints.update()
            .where(item_sprints.c.item_id == item_id, item_sprints.c.removed_at.is_(None))
            .values(removed_at=now)
        )

    return get_item(engine, item_id)


def add_note(engine: sa.Engine, item_id: int, note: str) -> dict:
    if not note or not note.strip():
        raise ValueError("Note cannot be empty")
    with engine.begin() as conn:
        # Verify item exists
        row = conn.execute(
            sa.select(items.c.id).where(items.c.id == item_id, items.c.deleted_at.is_(None))
        ).first()
        if not row:
            raise ValueError(f"Item #{item_id} not found")

        row = conn.execute(
            item_notes.insert().values(item_id=item_id, note=note).returning(item_notes)
        ).first()
        assert row is not None
        return row_to_dict(row)


def list_notes(engine: sa.Engine, item_id: int) -> list[dict]:
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(item_notes)
            .where(item_notes.c.item_id == item_id, item_notes.c.deleted_at.is_(None))
            .order_by(item_notes.c.created_at)
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def delete_item(engine: sa.Engine, item_id: int) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            items.update()
            .where(items.c.id == item_id, items.c.deleted_at.is_(None))
            .values(deleted_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0
