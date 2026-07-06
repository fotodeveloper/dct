"""Current-sprint operations + review dashboards (session start/end).

Thin, v0.1-compatible surface (set_sprint / clear_sprint / list_sprint_items /
get_sprint_review) that delegates to the item_sprints model in core/sprints —
the project's single active sprint IS the "current sprint" (#216). No boolean
column is read or written here; in_current_sprint is derived."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from dct.db import items, item_checkpoints, changelog_entries, session_handoffs, row_to_dict
from dct.core.projects import resolve_project_id


def set_sprint(engine: sa.Engine, project_slug: str, item_ids: list[int]) -> dict:
    """Replace the current sprint with exactly the given items (the /handoff
    entry point).

    v0.2+ (#216): the "current sprint" is the project's single active sprint and
    membership lives in item_sprints. Delegates to set_current_sprint_items,
    which auto-creates the active sprint if none exists and swaps membership
    atomically. Foreign/deleted ids are ignored, so `size` may be < len(ids).
    """
    from dct.core.sprints import set_current_sprint_items
    result = set_current_sprint_items(engine, project_slug, list(item_ids))
    return {"project": project_slug, "size": result["size"], "items": result["items"]}


def clear_sprint(engine: sa.Engine, project_slug: str) -> dict:
    """Empty the current sprint (fresh planning). Delegates to
    clear_current_sprint — soft-removes every member of the active sprint; the
    sprint row stays active and empty. No-op if there is no current sprint."""
    from dct.core.sprints import clear_current_sprint
    return clear_current_sprint(engine, project_slug)


def list_sprint_items(engine: sa.Engine, project_slug: str) -> list[dict]:
    """Items in the project's current (active) sprint, priority-ordered.

    v0.2+ (#216): membership comes from item_sprints via the active sprint.
    Returns [] when there is no current sprint."""
    from dct.core.sprints import get_current_sprint, list_items_in_sprint
    sprint = get_current_sprint(engine, project_slug)
    if not sprint:
        return []
    members = list_items_in_sprint(engine, sprint["id"])
    return sorted(members, key=lambda i: (i.get("priority") or "P2", i.get("created_at") or ""))


# ── since parsing ─────────────────────────────────────────────────────────────


def parse_since(since: str, engine: sa.Engine, project_slug: str) -> tuple[datetime | None, str]:
    """Convert a `since` specifier into an anchor datetime + human label.

    Accepted forms:
      "last_release" (default) — most recent release timestamp for project, or None if never released
      "12h", "24h", "48h" — hours relative to now
      "all" — no cutoff
      ISO date like "2026-04-10" or "2026-04-10T14:00" — absolute

    Returns (anchor_datetime, human_label). anchor_datetime=None means no cutoff.
    """
    if since in ("", "last_release"):
        with engine.begin() as conn:
            project_id = resolve_project_id(conn, project_slug)
            row = conn.execute(
                sa.select(sa.func.max(changelog_entries.c.released_at))
                .where(
                    changelog_entries.c.project_id == project_id,
                    changelog_entries.c.deleted_at.is_(None),
                    changelog_entries.c.released_at.isnot(None),
                )
            ).first()
            last_release = row[0] if row else None
        if last_release:
            return last_release, f"last_release ({last_release.isoformat()})"
        return None, "no releases — showing all time"

    if since == "all":
        return None, "all time"

    # Hour shortcuts
    if since.endswith("h") and since[:-1].isdigit():
        hours = int(since[:-1])
        anchor = datetime.now(timezone.utc) - timedelta(hours=hours)
        return anchor, f"last {hours}h"

    # ISO date / datetime
    try:
        anchor = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        return anchor, f"since {anchor.isoformat()}"
    except ValueError:
        raise ValueError(
            f"Invalid `since`: '{since}'. Accepted: last_release, all, <N>h (e.g. 24h), ISO date"
        )


# ── review ────────────────────────────────────────────────────────────────────


def _to_cols_rows(rows: list[dict], cols: list[str]) -> dict:
    """Render list of dicts as compact {cols, rows} table."""
    return {"cols": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


def get_sprint_review(
    engine: sa.Engine,
    project_slug: str,
    since: str = "last_release",
) -> dict:
    """Dashboard payload for session start/end.

    Returns data only — interpretation (what to prioritize, what's stale, etc.)
    lives in the /track-review skill so it can be iterated without schema change.
    """
    anchor, since_label = parse_since(since, engine, project_slug)

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)

        # in_sprint — items flagged
        in_sprint_rows = _query_items_with_cp(conn, project_id, where_in_sprint=True)

        # recent changes — items whose updated_at > anchor
        if anchor:
            changed_rows = _query_items_simple(conn, project_id, changed_since=anchor)
            checkpoints_done = conn.execute(
                sa.select(sa.func.count())
                .select_from(item_checkpoints.join(items, items.c.id == item_checkpoints.c.item_id))
                .where(
                    items.c.project_id == project_id,
                    item_checkpoints.c.status == "done",
                    item_checkpoints.c.updated_at > anchor,
                    item_checkpoints.c.deleted_at.is_(None),
                )
            ).scalar() or 0
            changelog_added = conn.execute(
                sa.select(sa.func.count())
                .where(
                    changelog_entries.c.project_id == project_id,
                    changelog_entries.c.created_at > anchor,
                    changelog_entries.c.deleted_at.is_(None),
                )
            ).scalar() or 0
        else:
            changed_rows = []
            checkpoints_done = 0
            changelog_added = 0

        # stale in_progress — items in progress > 7 days since last update
        stale_anchor = datetime.now(timezone.utc) - timedelta(days=7)
        stale_rows = conn.execute(
            sa.select(items.c.id, items.c.title, items.c.updated_at)
            .where(
                items.c.project_id == project_id,
                items.c.status == "in_progress",
                items.c.updated_at < stale_anchor,
                items.c.deleted_at.is_(None),
            )
            .order_by(items.c.updated_at)
        ).fetchall()
        stale = [
            {
                "id": r._mapping["id"],
                "updated_at": r._mapping["updated_at"].isoformat() if r._mapping["updated_at"] else None,
                "title": r._mapping["title"],
            }
            for r in stale_rows
        ]

        # unconsumed handoffs
        handoff_rows = conn.execute(
            sa.select(session_handoffs)
            .where(
                session_handoffs.c.project_id == project_id,
                session_handoffs.c.consumed_at.is_(None),
                session_handoffs.c.deleted_at.is_(None),
            )
            .order_by(session_handoffs.c.created_at.desc())
        ).fetchall()
        handoffs = [row_to_dict(r) for r in handoff_rows]

        # totals
        total_open = conn.execute(
            sa.select(sa.func.count())
            .where(
                items.c.project_id == project_id,
                items.c.status == "open",
                items.c.deleted_at.is_(None),
            )
        ).scalar() or 0

    sprint_size = len(in_sprint_rows)
    sprint_done = sum(1 for r in in_sprint_rows if r.get("status") in ("resolved", "wont_fix"))

    # cols ordered short-to-long: fixed-width fields left, free text (title) right.
    return {
        "project": project_slug,
        "since": since,
        "since_anchor": since_label,
        "in_sprint": _to_cols_rows(
            in_sprint_rows,
            ["id", "priority", "status", "cp", "title"],
        ),
        "recent_activity": {
            "items_changed": _to_cols_rows(
                changed_rows,
                ["id", "status", "updated_at", "title"],
            ),
            "checkpoints_completed": checkpoints_done,
            "changelog_added": changelog_added,
        },
        "stale_in_progress": _to_cols_rows(
            stale,
            ["id", "updated_at", "title"],
        ),
        "unconsumed_handoffs": _to_cols_rows(
            [
                {
                    "id": h["id"],
                    "scope": h["scope"],
                    "created_by": h["created_by"],
                    "created_at": h["created_at"].isoformat() if h["created_at"] else None,
                    "prompt": h["prompt"],
                }
                for h in handoffs
            ],
            ["id", "scope", "created_by", "created_at", "prompt"],
        ),
        "summary": {
            "sprint_size": sprint_size,
            "sprint_completion": f"{sprint_done}/{sprint_size}" if sprint_size else "0/0",
            "total_open_in_project": total_open,
            "handoffs_pending": len(handoffs),
        },
    }


def _query_items_with_cp(
    conn: sa.Connection,
    project_id: int,
    where_in_sprint: bool = False,
) -> list[dict]:
    """Fetch items enriched with cp=[done,total] checkpoint progress via subquery."""
    cp_done = sa.func.coalesce(
        sa.func.sum(sa.case((item_checkpoints.c.status == "done", 1), else_=0)),
        0,
    ).label("cp_done")
    cp_total = sa.func.coalesce(
        sa.func.sum(sa.case((item_checkpoints.c.status != "rejected", 1), else_=0)),
        0,
    ).label("cp_total")

    q = (
        sa.select(items, cp_done, cp_total)
        .select_from(
            items.outerjoin(
                item_checkpoints,
                sa.and_(
                    item_checkpoints.c.item_id == items.c.id,
                    item_checkpoints.c.deleted_at.is_(None),
                ),
            )
        )
        .where(items.c.project_id == project_id, items.c.deleted_at.is_(None))
        .group_by(items.c.id)
    )

    if where_in_sprint:
        from dct.core.items import _current_sprint_exists
        q = q.where(_current_sprint_exists())

    q = q.order_by(items.c.priority, items.c.created_at)

    out = []
    for r in conn.execute(q):
        m = r._mapping
        out.append({
            "id": m["id"],
            "priority": m["priority"],
            "status": m["status"],
            "cp": [int(m["cp_done"] or 0), int(m["cp_total"] or 0)],
            "title": m["title"],
        })
    return out


def _query_items_simple(
    conn: sa.Connection,
    project_id: int,
    changed_since: datetime,
) -> list[dict]:
    rows = conn.execute(
        sa.select(items.c.id, items.c.title, items.c.status, items.c.updated_at)
        .where(
            items.c.project_id == project_id,
            items.c.updated_at > changed_since,
            items.c.deleted_at.is_(None),
        )
        .order_by(items.c.updated_at.desc())
        .limit(20)
    ).fetchall()
    return [
        {
            "id": r._mapping["id"],
            "status": r._mapping["status"],
            "updated_at": r._mapping["updated_at"].isoformat() if r._mapping["updated_at"] else None,
            "title": r._mapping["title"],
        }
        for r in rows
    ]
