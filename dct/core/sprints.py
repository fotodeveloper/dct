"""Sprint CRUD + item_sprints link management.

First-class sprint entity (v0.2.0, migration 003). Since #216 this is the
SINGLE source of truth for sprint membership: the legacy
`items.in_current_sprint` boolean has been dropped, and `in_current_sprint`
is DERIVED on read from item_sprints + the project's active sprint(s).
Since G1 (#413, owner decision 2026-07-28) a project may have MULTIPLE
active sprints in parallel; the "current sprint" is the PRIMARY one — the
most recently started active sprint (id as tie-break). Ops that need one
unambiguous target (e.g. handoff_work) must accept an explicit sprint
selector and raise a conflict error rather than guess when several are
active. `db._migrate_legacy_sprint_flag` one-shot-seeds a 'Pre-0.1 legacy
sprint' from the old boolean before the column is dropped.

Terminology:
  - **sprint**: a named time-boxed or scope-boxed unit of work. Subtypes
    via `kind`: sprint | phase | milestone | block. The subtype is
    informational; the lifecycle is identical (planned → active →
    [deferred] → completed/abandoned, with completed/abandoned → active as a
    reopen). `deferred` parks a started-but-paused sprint: it keeps its
    `started_at` and is filtered out of the active-sprint views. Legal
    transitions are whitelisted in `ALLOWED_TRANSITIONS`.
  - **item_sprints**: many-to-many linkage between items and sprints.
    `removed_at` is a soft-delete marker — a removed link preserves the
    history of "this item was in this sprint from X to Y".
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from dct.db import item_sprints, items, row_to_dict, sprints
from dct.core.items import TERMINAL_STATUSES, _membership_count_exprs
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


def _active_sprints_query(project_id: int) -> sa.Select:
    """Active sprints of a project, primary first (newest started_at, id
    tie-break) — the single ordering rule both readers below share."""
    return (
        sa.select(sprints)
        .where(
            sprints.c.project_id == project_id,
            sprints.c.status == "active",
            sprints.c.deleted_at.is_(None),
        )
        .order_by(sprints.c.started_at.desc(), sprints.c.id.desc())
    )


def _get_current_sprint_conn(conn: sa.Connection, project_id: int) -> dict | None:
    row = conn.execute(_active_sprints_query(project_id)).first()
    return row_to_dict(row) if row else None


def _list_active_sprints_conn(conn: sa.Connection, project_id: int) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute(_active_sprints_query(project_id))]


def _sprint_membership_stats_by_sprint(
    conn: sa.Connection, project_id: int, sprint_ids: list[int]
) -> dict[int, tuple[int, int]]:
    """{sprint_id: (total_members, completed_members)} for the given sprints —
    live links only (removed_at IS NULL), terminal item statuses count as
    completed. Counts only the project's own items (K-26 — legacy
    cross-project links are excluded). One grouped query (no N+1); ids absent
    from the result have no members.

    Shares the counting rule (_membership_count_exprs) with the project-wide
    _query_sprint_membership_stats, but NOT the query: an item in two active
    sprints is counted by each of them here, and only once there (K-38)."""
    if not sprint_ids:
        return {}
    rows = conn.execute(
        sa.select(item_sprints.c.sprint_id, *_membership_count_exprs())
        .select_from(item_sprints.join(items, items.c.id == item_sprints.c.item_id))
        .where(
            item_sprints.c.sprint_id.in_(sprint_ids),
            item_sprints.c.removed_at.is_(None),
            items.c.deleted_at.is_(None),
            items.c.project_id == project_id,
        )
        .group_by(item_sprints.c.sprint_id)
    ).fetchall()
    return {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in rows}


def get_current_sprint(engine: sa.Engine, project_slug: str) -> dict | None:
    """Return a project's PRIMARY active sprint — the default target /pickup
    and /handoff act on — or None if no sprint is active.

    Multiple active sprints are legal since G1 (#413): primary = the most
    recently started active sprint, id as tie-break. Use list_active_sprints
    for the full set; ops needing one unambiguous target should require an
    explicit selector when more than one is active."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        return _get_current_sprint_conn(conn, pid)


def list_active_sprints(engine: sa.Engine, project_slug: str) -> list[dict]:
    """All active sprints of a project, newest started first (id tie-break) —
    the first row is the primary sprint get_current_sprint returns."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        return _list_active_sprints_conn(conn, pid)


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
      - activation (→active) stamps started_at only when it was NULL
        (reactivation preserves it); parallel active sprints are legal (G1);
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

    Project isolation (K-26, #413): the item and the sprint MUST belong to the
    same project — a cross-project link would leak the item's title/status
    into the other project's snapshot at every session start.
    """
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        # Verify both rows exist, aren't soft-deleted, and share a project.
        item_row = conn.execute(
            sa.select(items.c.id, items.c.project_id).where(
                items.c.id == item_id, items.c.deleted_at.is_(None)
            )
        ).first()
        if not item_row:
            raise ValueError(f"Item #{item_id} not found")

        sprint_row = conn.execute(
            sa.select(sprints.c.id, sprints.c.project_id).where(
                sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None)
            )
        ).first()
        if not sprint_row:
            raise ValueError(f"Sprint #{sprint_id} not found")

        if item_row._mapping["project_id"] != sprint_row._mapping["project_id"]:
            raise ValueError(
                f"Item #{item_id} and sprint #{sprint_id} belong to different "
                f"projects — cross-project sprint membership is not allowed."
            )

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
# project's PRIMARY active sprint (newest started; several may be active since
# G1); membership lives in item_sprints so there is exactly ONE source of truth
# (#216).

def _ensure_current_sprint_conn(
    conn: sa.Connection, project_id: int, name: str | None = None
) -> dict:
    """Return the project's primary active sprint, creating+activating one
    (default name 'Current sprint') when none is active. Conn-level so composed
    ops run get-or-create inside their OWN transaction — a convenience (and an
    all-or-nothing rollback), NOT a concurrency guarantee: under PostgreSQL
    READ COMMITTED two sessions can still both miss the sprint and both create
    one. Serializing that (advisory lock / unique index) is tracked separately."""
    existing = _get_current_sprint_conn(conn, project_id)
    if existing:
        return existing
    sprint_name = (name or "Current sprint").strip()
    if not sprint_name:
        raise ValueError("Sprint name cannot be empty")
    row = conn.execute(
        sprints.insert()
        .values(
            project_id=project_id,
            name=sprint_name,
            kind="sprint",
            status="active",
            started_at=datetime.now(timezone.utc),
        )
        .returning(sprints)
    ).first()
    assert row is not None, "INSERT RETURNING must return a row"
    return row_to_dict(row)


def ensure_current_sprint(
    engine: sa.Engine, project_slug: str, name: str | None = None
) -> dict:
    """Return the project's current (primary active) sprint, creating+activating
    one if none exists. Idempotent: a second call returns the same sprint."""
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        return _ensure_current_sprint_conn(conn, project_id, name)


def _validate_project_items_conn(
    conn: sa.Connection, project_id: int, ids: list[int]
) -> set[int]:
    """STRICT id validation (K-8, #413): return the set of ids that are live
    items of this project; raise ValueError listing any that are not
    (foreign, unknown, soft-deleted)."""
    if not ids:
        return set()
    valid = set(
        conn.execute(
            sa.select(items.c.id).where(
                items.c.project_id == project_id,
                items.c.id.in_(ids),
                items.c.deleted_at.is_(None),
            )
        ).scalars()
    )
    unknown = set(ids) - valid
    if unknown:
        raise ValueError(
            f"Unknown item id(s) for this project: {sorted(unknown)} — "
            f"sprint membership unchanged. Pass only live items of the project."
        )
    return valid


def _validate_target_sprint_conn(
    conn: sa.Connection, project_id: int, sprint_id: int
) -> None:
    """The explicit membership target must be this project's live sprint
    (K-26 asymmetry fix — item ids were validated strictly, the sprint id
    was not)."""
    row = conn.execute(
        sa.select(sprints.c.project_id).where(
            sprints.c.id == sprint_id, sprints.c.deleted_at.is_(None)
        )
    ).first()
    if row is None or row[0] != project_id:
        raise ValueError(f"Sprint #{sprint_id} is not a live sprint of this project")


def _member_ids_conn(
    conn: sa.Connection, sprint_id: int, open_only: bool = False
) -> set[int]:
    """Item ids with a live link to the sprint; open_only filters out
    terminal members (the sprint's completion history)."""
    q = (
        sa.select(item_sprints.c.item_id)
        .select_from(item_sprints.join(items, items.c.id == item_sprints.c.item_id))
        .where(
            item_sprints.c.sprint_id == sprint_id,
            item_sprints.c.removed_at.is_(None),
            items.c.deleted_at.is_(None),
        )
    )
    if open_only:
        q = q.where(items.c.status.notin_(TERMINAL_STATUSES))
    return set(conn.execute(q).scalars())


def _add_members_conn(
    conn: sa.Connection, sprint_id: int, ids: set[int], now: datetime
) -> None:
    """Create-or-reactivate links by full PK (mirrors assign_item_to_sprint;
    never collide with a soft-removed row — the #215 lesson)."""
    for iid in sorted(ids):
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


def _remove_members_conn(
    conn: sa.Connection, sprint_id: int, ids: set[int], now: datetime
) -> None:
    """Soft-remove live links for the given item ids (idempotent — absent or
    already-removed links are untouched)."""
    for iid in sorted(ids):
        conn.execute(
            item_sprints.update()
            .where(
                item_sprints.c.item_id == iid,
                item_sprints.c.sprint_id == sprint_id,
                item_sprints.c.removed_at.is_(None),
            )
            .values(removed_at=now)
        )


def _require_unambiguous_current_conn(
    conn: sa.Connection, project_id: int
) -> dict | None:
    """The single active sprint, None when none is active, ValueError when
    SEVERAL are (K-33): every current-sprint WRITE refuses to guess among
    parallel active sprints — reads may use the primary, destructive ops may
    not. Callers with an explicit selector bypass this via sprint_id."""
    active = _list_active_sprints_conn(conn, project_id)
    if len(active) > 1:
        listing = ", ".join(
            f"#{s['id']} ({s.get('code') or s.get('name')})" for s in active
        )
        raise ValueError(
            f"{len(active)} active sprints — current-sprint writes refuse to "
            f"guess; target one explicitly (sprint id/code, or "
            f"assign_item_to_sprint / remove_item_from_sprint): {listing}"
        )
    return active[0] if active else None


def _set_current_sprint_items_conn(
    conn: sa.Connection,
    project_id: int,
    item_ids: list[int],
    sprint_id: int | None = None,
) -> dict:
    """Replace a sprint's OPEN membership with exactly `item_ids`.

    Target: explicit `sprint_id` when given (validated as this project's live
    sprint), otherwise the primary active sprint — auto-created when none is
    active.

    STRICT since G1 (K-8, #413): ids that are not live items of this project
    raise ValueError listing the offenders; on the shared connection the raise
    rolls the whole transaction back.

    TERMINAL MEMBERS ARE IMMUNE (K-24, ADR #57): resolved / wont_fix members
    are the sprint's completion history — a replace only swaps the OPEN
    members and never soft-removes terminal ones, so sprint_completion
    survives session-end replanning. Returns {"size", "items", "sprint_id"}
    where items/size describe the OPEN membership after the swap.
    """
    if sprint_id is None:
        current = _require_unambiguous_current_conn(conn, project_id)  # K-33
        sprint = current or _ensure_current_sprint_conn(conn, project_id)
        sprint_id = sprint["id"]
    else:
        _validate_target_sprint_conn(conn, project_id, sprint_id)
    now = datetime.now(timezone.utc)

    valid = _validate_project_items_conn(conn, project_id, list(item_ids))
    open_members = _member_ids_conn(conn, sprint_id, open_only=True)

    _add_members_conn(conn, sprint_id, valid - open_members, now)
    # Remove — soft-remove dropped OPEN members only (terminal stay, K-24).
    _remove_members_conn(conn, sprint_id, open_members - valid, now)

    return {
        "size": len(valid),
        "items": sorted(valid),
        "sprint_id": sprint_id,
    }


def _resolve_active_sprint_conn(
    conn: sa.Connection, project_id: int, selector: str | int
) -> dict:
    """Resolve an id-or-code selector against the ACTIVE sprints (K-25).

    Two-pass contract: an id match and a code match are computed
    independently; if BOTH exist and point at different sprints, that is a
    collision → ValueError listing both candidates (conflict over guessing —
    a numeric `code` must never silently shadow another sprint's id, and
    vice versa). Only ASCII decimal selectors are treated as ids
    ("²".isdigit() is True but int() rejects it; "٥" would silently parse).
    Raises ValueError with the active-sprint listing when nothing matches.
    """
    actives = _list_active_sprints_conn(conn, project_id)
    sel = str(selector).strip()
    listing = ", ".join(
        f"#{s['id']} ({s.get('code') or s.get('name')})" for s in actives
    ) or "(none)"

    id_match = None
    if sel.isascii() and sel.isdecimal():
        sel_id = int(sel)
        id_match = next((s for s in actives if s["id"] == sel_id), None)
    code_match = next((s for s in actives if s.get("code") == sel), None)

    if id_match and code_match and id_match["id"] != code_match["id"]:
        raise ValueError(
            f"Sprint selector '{sel}' is ambiguous: it matches sprint "
            f"#{id_match['id']} by id AND sprint #{code_match['id']} "
            f"(code '{sel}') — rename the code or pass an unambiguous selector."
        )
    resolved = id_match or code_match
    if resolved is None:
        raise ValueError(
            f"Sprint '{sel}' is not an active sprint of this project. "
            f"Active: {listing}. Activate it first or pick one of the above."
        )
    return resolved


def set_current_sprint_items(
    engine: sa.Engine, project_slug: str, item_ids: list[int]
) -> dict:
    """Replace the current sprint's membership with exactly `item_ids`.

    Auto-creates the current sprint if none exists. STRICT since G1 (K-8,
    #413): foreign / unknown / soft-deleted ids raise ValueError (listing the
    offenders) and the whole swap — including any auto-created sprint — rolls
    back. One transaction; the sprint is never left half-set.
    """
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        out = _set_current_sprint_items_conn(conn, project_id, item_ids)
    return {"project": project_slug, **out}


def clear_current_sprint(engine: sa.Engine, project_slug: str) -> dict:
    """Soft-remove the current sprint's OPEN members. The sprint itself stays
    active, ready for fresh planning; terminal members are its completion
    history and stay linked (ADR #57 — use remove_item_from_sprint for a
    manual override). No-op if there is no current sprint."""
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        sprint = _require_unambiguous_current_conn(conn, project_id)  # K-33
        if not sprint:
            return {"project": project_slug, "cleared": 0}
        open_members = _member_ids_conn(conn, sprint["id"], open_only=True)
        _remove_members_conn(conn, sprint["id"], open_members, now)
    return {"project": project_slug, "cleared": len(open_members)}


def add_item_to_current_sprint(engine: sa.Engine, project_slug: str, item_id: int) -> dict:
    """Add ONE item to the project's current sprint (auto-creating it). Additive
    — unlike set_current_sprint_items it does not touch other members. Idempotent
    (create-or-reactivate). With SEVERAL active sprints this refuses to guess
    (K-33) — use assign_item_to_sprint(item_id, sprint_id) instead.
    Returns {project, sprint_id, item_id, active}."""
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        sprint = _require_unambiguous_current_conn(conn, project_id)  # K-33
    if sprint is None:
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
    if there is no current sprint or the item is not an active member. With
    SEVERAL active sprints this refuses to guess (K-33) — use
    remove_item_from_sprint(item_id, sprint_id) instead."""
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        sprint = _require_unambiguous_current_conn(conn, project_id)  # K-33
    if not sprint:
        return {"project": project_slug, "removed": False}
    res = remove_item_from_sprint(engine, item_id, sprint["id"])
    return {"project": project_slug, "removed": res is not None}
