"""Sprint CRUD + item_sprints link management.

First-class sprint entity (v0.2.0, migration 003). Since #216 this is the
SINGLE source of truth for sprint membership: the legacy
`items.in_current_sprint` boolean has been dropped, and `in_current_sprint`
is DERIVED on read from item_sprints + the project's active sprint. The
"current sprint" is that single active sprint (≤1 active per project,
enforced here). `db._migrate_legacy_sprint_flag` one-shot-seeds a
'Pre-0.1 legacy sprint' from the old boolean before the column is dropped.

Terminology:
  - **sprint**: a named time-boxed or scope-boxed unit of work. Subtypes
    via `kind`: sprint | phase | milestone | block. The subtype is
    informational; the lifecycle is identical (planned → active →
    [deferred] → completed/abandoned, with completed/abandoned → active as a
    reopen). `deferred` parks a started-but-paused sprint: it keeps its
    `started_at`, frees the ≤1-active-per-project slot, and is filtered out of
    the current-sprint views. Legal transitions are whitelisted in
    `ALLOWED_TRANSITIONS`.
  - **item_sprints**: many-to-many linkage between items and sprints.
    `removed_at` is a soft-delete marker — a removed link preserves the
    history of "this item was in this sprint from X to Y".
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from dct.db import item_sprints, items, row_to_dict, sprints
from dct.core.projects import resolve_project_id

VALID_KINDS = {"sprint", "phase", "milestone", "block"}
VALID_STATUSES = {"planned", "active", "deferred", "completed", "abandoned"}

# Legal status transitions. A move not listed here is rejected by update_sprint.
# Rationale for the notable exclusions:
#   - planned→deferred: a sprint that never started is already parked.
#   - active/deferred→planned: demoting a started sprint is semantically wrong
#     (started_at is already stamped) — the owner rule.
#   - completed/abandoned→active is the only reopen edge (clears ended_at).
ALLOWED_TRANSITIONS = {
    "planned":   {"active", "abandoned"},
    "active":    {"deferred", "completed", "abandoned"},
    "deferred":  {"active", "completed", "abandoned"},
    "completed": {"active"},   # reopen
    "abandoned": {"active"},   # reopen
}


def _validate(value: str, valid: set[str], field: str) -> str:
    if value not in valid:
        raise ValueError(f"Invalid {field}: '{value}'. Valid: {', '.join(sorted(valid))}")
    return value


def _active_sprint_id(conn: sa.Connection, project_id: int, exclude_id: int | None = None) -> int | None:
    """Return the id of the project's active sprint (the 'current sprint'), or
    None. `exclude_id` skips a given sprint so a sprint never blocks itself when
    being updated. Underpins the ≤1-active-sprint-per-project invariant (#216)."""
    q = sa.select(sprints.c.id).where(
        sprints.c.project_id == project_id,
        sprints.c.status == "active",
        sprints.c.deleted_at.is_(None),
    )
    if exclude_id is not None:
        q = q.where(sprints.c.id != exclude_id)
    return conn.execute(q).scalar()


# ── Sprint CRUD ───────────────────────────────────────────────────────────────

def create_sprint(
    engine: sa.Engine,
    project_slug: str,
    name: str,
    code: str | None = None,
    kind: str = "sprint",
    status: str = "planned",
    goal: str | None = None,
) -> dict:
    """Create a sprint under the given project.

    `code` (optional) is a short handle — "0.5", "M0", "sprint-v0.1-ship".
    If provided, uniqueness is enforced per project (partial-index in db.py).
    """
    if not name or not name.strip():
        raise ValueError("Sprint name cannot be empty")
    _validate(kind, VALID_KINDS, "kind")
    _validate(status, VALID_STATUSES, "status")
    if status == "deferred":
        raise ValueError(
            "A sprint cannot be created with status 'deferred' (it has no "
            "started_at yet); create it 'planned'/'active', then defer it."
        )

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        if code:
            existing = conn.execute(
                sa.select(sprints).where(
                    sprints.c.project_id == project_id,
                    sprints.c.code == code,
                    sprints.c.deleted_at.is_(None),
                )
            ).first()
            if existing:
                raise ValueError(f"Sprint with code '{code}' already exists in project")

        # ≤1 active sprint per project — the active one IS the "current sprint"
        # that /pickup and /handoff operate on (#216).
        if status == "active":
            other = _active_sprint_id(conn, project_id)
            if other is not None:
                raise ValueError(
                    f"Project already has an active sprint (#{other}); complete or "
                    f"abandon it before activating another (≤1 active sprint per project)."
                )

        started_at = datetime.now(timezone.utc) if status == "active" else None
        row = conn.execute(
            sprints.insert()
            .values(
                project_id=project_id,
                name=name.strip(),
                code=code,
                kind=kind,
                status=status,
                goal=goal,
                started_at=started_at,
            )
            .returning(sprints)
        ).first()
        assert row is not None, "INSERT RETURNING must return a row"
        return row_to_dict(row)


def list_sprints(
    engine: sa.Engine,
    project_slug: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(sprints).order_by(sprints.c.created_at.desc())
        if project_slug:
            pid = resolve_project_id(conn, project_slug)
            q = q.where(sprints.c.project_id == pid)
        if status:
            q = q.where(sprints.c.status == status)
        if kind:
            q = q.where(sprints.c.kind == kind)
        if not include_deleted:
            q = q.where(sprints.c.deleted_at.is_(None))
        return [row_to_dict(r) for r in conn.execute(q)]


def get_sprint(engine: sa.Engine, sprint_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(sprints).where(
                sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None)
            )
        ).first()
        return row_to_dict(row) if row else None


def get_current_sprint(engine: sa.Engine, project_slug: str) -> dict | None:
    """Return a project's single active sprint — the 'current sprint' that
    /pickup and /handoff act on — or None if there is none.

    Invariant: ≤1 active sprint per project (enforced by create_sprint /
    update_sprint). The order_by is purely defensive: should two ever slip
    through, the most recently started wins."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        row = conn.execute(
            sa.select(sprints)
            .where(
                sprints.c.project_id == pid,
                sprints.c.status == "active",
                sprints.c.deleted_at.is_(None),
            )
            .order_by(sprints.c.started_at.desc())
        ).first()
        return row_to_dict(row) if row else None


def get_sprint_by_code(engine: sa.Engine, project_slug: str, code: str) -> dict | None:
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        row = conn.execute(
            sa.select(sprints).where(
                sprints.c.project_id == pid,
                sprints.c.code == code,
                sprints.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def update_sprint(engine: sa.Engine, sprint_id: int, **kwargs) -> dict | None:
    """Update mutable sprint fields, enforcing the ALLOWED_TRANSITIONS whitelist.

    Status semantics:
      - a status equal to the current one is an idempotent no-op for the status
        field (timestamps untouched; other supplied fields still applied);
      - a transition not in ALLOWED_TRANSITIONS raises ValueError listing the
        legal targets for the current status;
      - activation (→active) is capped at ≤1 active sprint per project and
        stamps started_at only when it was NULL (reactivation preserves it);
      - reopen (completed/abandoned → active) clears the stale ended_at;
      - completed/abandoned stamp ended_at;
      - deferred touches neither started_at nor ended_at (parked, keeps both).

    `code` gets the same per-project uniqueness pre-check as create_sprint —
    a friendly ValueError instead of a raw IntegrityError. `name`, when
    supplied, is stripped and rejected if blank (same rule as create_sprint) so
    update can never store a name create_sprint would have refused.

    Returns the updated sprint dict, or None when the id is unknown / deleted.
    """
    allowed = {"name", "code", "kind", "status", "goal"}
    values: dict = {}
    for k, v in kwargs.items():
        if k not in allowed or v is None:
            continue
        if k == "kind":
            _validate(v, VALID_KINDS, "kind")
        elif k == "status":
            _validate(v, VALID_STATUSES, "status")
        elif k == "name":
            # Mirror create_sprint: reject blank names and strip padding so
            # update can never produce a name create_sprint would have refused.
            if not v.strip():
                raise ValueError("Sprint name cannot be empty")
            v = v.strip()
        values[k] = v
    if not values:
        raise ValueError("No valid fields to update")

    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        current = conn.execute(
            sa.select(sprints).where(
                sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None)
            )
        ).first()
        if current is None:
            return None

        project_id = current._mapping["project_id"]
        cur_status = current._mapping["status"]

        # Per-project `code` uniqueness pre-check (friendly ValueError instead of
        # the raw IntegrityError the partial UNIQUE index would otherwise raise).
        new_code = values.get("code")
        if new_code:
            dup = conn.execute(
                sa.select(sprints.c.id).where(
                    sprints.c.project_id == project_id,
                    sprints.c.code == new_code,
                    sprints.c.id != sprint_id,
                    sprints.c.deleted_at.is_(None),
                )
            ).first()
            if dup is not None:
                raise ValueError(
                    f"Sprint with code '{new_code}' already exists in project"
                )

        # Status transition validation + lifecycle timestamp maintenance.
        new_status = values.get("status")
        if new_status is not None and new_status == cur_status:
            values.pop("status")  # idempotent no-op for the status field
            new_status = None

        if new_status is not None:
            legal = ALLOWED_TRANSITIONS.get(cur_status, set())
            if new_status not in legal:
                targets = ", ".join(sorted(legal)) or "(none)"
                raise ValueError(
                    f"Illegal sprint status transition '{cur_status}' -> "
                    f"'{new_status}'. Allowed from '{cur_status}': {targets}."
                )
            if new_status == "active":
                other = _active_sprint_id(conn, project_id, exclude_id=sprint_id)
                if other is not None:
                    raise ValueError(
                        f"Project already has an active sprint (#{other}); complete or "
                        f"abandon it before activating another (≤1 active sprint per project)."
                    )
                if current._mapping["started_at"] is None:
                    values["started_at"] = now  # first time becoming active
                if current._mapping["ended_at"] is not None:
                    values["ended_at"] = None  # reopen — clear stale ended_at
            elif new_status in ("completed", "abandoned"):
                values["ended_at"] = now
            # 'deferred' intentionally touches neither started_at nor ended_at.

        if not values:
            # Only a same->same status was requested — nothing to write, and
            # timestamps must stay untouched.
            return get_sprint(engine, sprint_id)

        values["updated_at"] = now
        conn.execute(
            sprints.update()
            .where(sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None))
            .values(**values)
        )
    return get_sprint(engine, sprint_id)


def delete_sprint(engine: sa.Engine, sprint_id: int) -> bool:
    """Soft-delete a sprint. Does NOT cascade to item_sprints — links remain
    for historical query ('this item WAS in this sprint'). To hide those
    links, call `remove_item_from_sprint` per item before deleting."""
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            sprints.update()
            .where(sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        return result.rowcount > 0


# ── item_sprints link management ─────────────────────────────────────────────

def assign_item_to_sprint(engine: sa.Engine, item_id: int, sprint_id: int) -> dict:
    """Create (or reactivate) a link from item to sprint.

    Idempotent: if the link already exists and is active (removed_at IS NULL),
    returns it unchanged. If it exists but was removed, clears `removed_at`
    to reactivate while preserving `assigned_at` history via the row itself.
    """
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        # Verify both rows exist and aren't soft-deleted.
        item_row = conn.execute(
            sa.select(items.c.id).where(
                items.c.id == item_id, items.c.deleted_at.is_(None)
            )
        ).first()
        if not item_row:
            raise ValueError(f"Item #{item_id} not found")

        sprint_row = conn.execute(
            sa.select(sprints.c.id).where(
                sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None)
            )
        ).first()
        if not sprint_row:
            raise ValueError(f"Sprint #{sprint_id} not found")

        existing = conn.execute(
            sa.select(item_sprints).where(
                item_sprints.c.item_id == item_id,
                item_sprints.c.sprint_id == sprint_id,
            )
        ).first()
        if existing:
            if existing._mapping["removed_at"] is not None:
                conn.execute(
                    item_sprints.update()
                    .where(
                        item_sprints.c.item_id == item_id,
                        item_sprints.c.sprint_id == sprint_id,
                    )
                    .values(removed_at=None, assigned_at=now)
                )
            link_row = conn.execute(
                sa.select(item_sprints).where(
                    item_sprints.c.item_id == item_id,
                    item_sprints.c.sprint_id == sprint_id,
                )
            ).first()
            assert link_row is not None
            return row_to_dict(link_row)

        conn.execute(
            item_sprints.insert().values(
                item_id=item_id, sprint_id=sprint_id, assigned_at=now
            )
        )
        link_row = conn.execute(
            sa.select(item_sprints).where(
                item_sprints.c.item_id == item_id,
                item_sprints.c.sprint_id == sprint_id,
            )
        ).first()
        assert link_row is not None
        return row_to_dict(link_row)


def remove_item_from_sprint(
    engine: sa.Engine, item_id: int, sprint_id: int
) -> dict | None:
    """Soft-remove the link — sets removed_at. Returns the row, or None
    if the link never existed or was already removed."""
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            item_sprints.update()
            .where(
                item_sprints.c.item_id == item_id,
                item_sprints.c.sprint_id == sprint_id,
                item_sprints.c.removed_at.is_(None),
            )
            .values(removed_at=now)
        )
        if result.rowcount == 0:
            return None
        link_row = conn.execute(
            sa.select(item_sprints).where(
                item_sprints.c.item_id == item_id,
                item_sprints.c.sprint_id == sprint_id,
            )
        ).first()
        assert link_row is not None
        return row_to_dict(link_row)


def list_items_in_sprint(
    engine: sa.Engine, sprint_id: int, include_removed: bool = False
) -> list[dict]:
    """Return item rows currently (or historically) linked to a sprint."""
    with engine.begin() as conn:
        q = (
            sa.select(items)
            .join(item_sprints, items.c.id == item_sprints.c.item_id)
            .where(item_sprints.c.sprint_id == sprint_id, items.c.deleted_at.is_(None))
            .order_by(item_sprints.c.assigned_at.desc())
        )
        if not include_removed:
            q = q.where(item_sprints.c.removed_at.is_(None))
        return [row_to_dict(r) for r in conn.execute(q)]


def list_sprints_for_item(
    engine: sa.Engine, item_id: int, include_removed: bool = False
) -> list[dict]:
    """Return sprint rows an item is (or was) linked to."""
    with engine.begin() as conn:
        q = (
            sa.select(sprints)
            .join(item_sprints, sprints.c.id == item_sprints.c.sprint_id)
            .where(
                item_sprints.c.item_id == item_id, sprints.c.deleted_at.is_(None)
            )
            .order_by(item_sprints.c.assigned_at.desc())
        )
        if not include_removed:
            q = q.where(item_sprints.c.removed_at.is_(None))
        return [row_to_dict(r) for r in conn.execute(q)]


# ── current-sprint operations (the /handoff + /pickup surface) ─────────────────
# These replace the v0.1 boolean set_sprint/clear_sprint. "Current sprint" = the
# project's single active sprint (≤1 invariant); membership lives in item_sprints
# so there is exactly ONE source of truth (#216).

def ensure_current_sprint(
    engine: sa.Engine, project_slug: str, name: str | None = None
) -> dict:
    """Return the project's current (active) sprint, creating+activating one if
    none exists. Idempotent: a second call returns the same sprint."""
    existing = get_current_sprint(engine, project_slug)
    if existing:
        return existing
    return create_sprint(engine, project_slug, name or "Current sprint", status="active")


def set_current_sprint_items(
    engine: sa.Engine, project_slug: str, item_ids: list[int]
) -> dict:
    """Replace the current sprint's membership with exactly `item_ids`.

    Auto-creates the current sprint if none exists. Foreign / deleted item ids
    are silently ignored (only items belonging to this project are linked). The
    membership swap is atomic — the sprint is never left half-set.
    """
    sprint = ensure_current_sprint(engine, project_slug)
    sprint_id = sprint["id"]
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        valid = (
            set(
                conn.execute(
                    sa.select(items.c.id).where(
                        items.c.project_id == project_id,
                        items.c.id.in_(list(item_ids)),
                        items.c.deleted_at.is_(None),
                    )
                ).scalars()
            )
            if item_ids
            else set()
        )
        current_members = set(
            conn.execute(
                sa.select(item_sprints.c.item_id).where(
                    item_sprints.c.sprint_id == sprint_id,
                    item_sprints.c.removed_at.is_(None),
                )
            ).scalars()
        )

        # Add — create-or-reactivate by full PK (mirrors assign_item_to_sprint;
        # never collide with a soft-removed row — the #215 lesson).
        for iid in valid - current_members:
            has_row = conn.execute(
                sa.select(item_sprints.c.item_id).where(
                    item_sprints.c.item_id == iid,
                    item_sprints.c.sprint_id == sprint_id,
                )
            ).first()
            if has_row:
                conn.execute(
                    item_sprints.update()
                    .where(
                        item_sprints.c.item_id == iid,
                        item_sprints.c.sprint_id == sprint_id,
                    )
                    .values(removed_at=None, assigned_at=now)
                )
            else:
                conn.execute(
                    item_sprints.insert().values(
                        item_id=iid, sprint_id=sprint_id, assigned_at=now
                    )
                )

        # Remove — soft-remove dropped members.
        for iid in current_members - valid:
            conn.execute(
                item_sprints.update()
                .where(
                    item_sprints.c.item_id == iid,
                    item_sprints.c.sprint_id == sprint_id,
                    item_sprints.c.removed_at.is_(None),
                )
                .values(removed_at=now)
            )

    return {
        "project": project_slug,
        "size": len(valid),
        "items": sorted(valid),
        "sprint_id": sprint_id,
    }


def clear_current_sprint(engine: sa.Engine, project_slug: str) -> dict:
    """Soft-remove every member of the current sprint. The sprint itself stays
    active (empty), ready for fresh planning. No-op if there is no current
    sprint."""
    sprint = get_current_sprint(engine, project_slug)
    if not sprint:
        return {"project": project_slug, "cleared": 0}
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            item_sprints.update()
            .where(
                item_sprints.c.sprint_id == sprint["id"],
                item_sprints.c.removed_at.is_(None),
            )
            .values(removed_at=now)
        )
    return {"project": project_slug, "cleared": result.rowcount}


def add_item_to_current_sprint(engine: sa.Engine, project_slug: str, item_id: int) -> dict:
    """Add ONE item to the project's current sprint (auto-creating it). Additive
    — unlike set_current_sprint_items it does not touch other members. Idempotent
    (create-or-reactivate). Returns {project, sprint_id, item_id, active}."""
    sprint = ensure_current_sprint(engine, project_slug)
    link = assign_item_to_sprint(engine, item_id, sprint["id"])
    return {
        "project": project_slug,
        "sprint_id": sprint["id"],
        "item_id": item_id,
        "active": link["removed_at"] is None,
    }


def remove_item_from_current_sprint(engine: sa.Engine, project_slug: str, item_id: int) -> dict:
    """Remove ONE item from the project's current sprint. No-op (removed=False)
    if there is no current sprint or the item is not an active member."""
    sprint = get_current_sprint(engine, project_slug)
    if not sprint:
        return {"project": project_slug, "removed": False}
    res = remove_item_from_sprint(engine, item_id, sprint["id"])
    return {"project": project_slug, "removed": res is not None}
