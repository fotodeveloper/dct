"""Current-sprint surface + the shared session-view readers.

Two roles, deliberately kept in one module:

1. The legacy, v0.1-compatible current-sprint API — set_sprint / clear_sprint /
   list_sprint_items / get_sprint_review. These delegate to the item_sprints
   model in core/sprints; the project's PRIMARY active sprint (newest started;
   several may be active since G1, #413) is the "current sprint" (#216). No
   boolean column is read or written here — in_current_sprint is derived.
   get_sprint_review is the narrower predecessor of get_context_snapshot and
   stays for callers (and skills) that still ask for it.
2. The conn-level readers behind BOTH surfaces — _parse_since_conn,
   _query_recent_activity, _query_total_open, _query_stale_in_progress,
   _query_sprint_items_with_cp, _query_sprint_membership_stats and the
   {cols,rows} / JSON-safety helpers. workflows/session.py composes exactly
   these inside its single transaction, which is why the legacy dashboard and
   the G1 snapshot can never drift apart on what "stale", "open" or
   "sprint completion" mean.
"""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

# Re-export: the implementation moved to core/handoff (which owns the
# session_handoffs table), but get_sprint_review below and the historical
# import path `from dct.core.sprint import _query_unconsumed_handoffs` still
# read it here.
from dct.core.handoff import _query_unconsumed_handoffs
from dct.core.items import (
    TERMINAL_STATUSES,
    _cp_progress_exprs,
    _current_sprint_exists,
    _membership_count_exprs,
    _priority_order_expr,
)
from dct.core.projects import resolve_project_id
from dct.db import changelog_entries, item_checkpoints, item_sprints, items, sprints


def set_sprint(engine: sa.Engine, project_slug: str, item_ids: list[int]) -> dict:
    """Replace the current sprint with exactly the given items (the /handoff
    entry point).

    v0.2+ (#216): the "current sprint" is the project's primary active sprint
    and membership lives in item_sprints. Delegates to set_current_sprint_items,
    which auto-creates the active sprint if none exists and swaps membership
    atomically. Ids are STRICT (K-8, #413): unknown / foreign / soft-deleted ids
    RAISE ValueError listing them and nothing is written — they are never
    silently dropped. `open_size` counts the OPEN membership after the swap
    (K-45), so it may still differ from len(ids) when terminal members are
    involved (K-24: resolved members are the sprint's history and survive a
    replace).
    """
    from dct.core.sprints import set_current_sprint_items
    result = set_current_sprint_items(engine, project_slug, list(item_ids))
    return {
        "project": project_slug,
        "open_size": result["open_size"],
        "items": result["items"],
    }


def clear_sprint(engine: sa.Engine, project_slug: str) -> dict:
    """Empty the current sprint (fresh planning). Delegates to
    clear_current_sprint — soft-removes every member of the active sprint; the
    sprint row stays active and empty. No-op if there is no current sprint."""
    from dct.core.sprints import clear_current_sprint
    return clear_current_sprint(engine, project_slug)


def list_sprint_items(engine: sa.Engine, project_slug: str) -> list[dict]:
    """OPEN items in the project's current (active) sprint, priority-ordered.

    v0.2+ (#216): membership comes from item_sprints via the active sprint.
    Terminal members (resolved / wont_fix) keep their link (K-18, #413) but are
    a completion statistic, not current work — this view filters them out.
    Rows inherit list_items_in_sprint's `tags` contract (K-21, #611): active
    item_tags names as list[str], name-ASC — never the frozen items.tags
    column. Returns [] when there is no current sprint."""
    from dct.core.sprints import get_current_sprint, list_items_in_sprint
    sprint = get_current_sprint(engine, project_slug)
    if not sprint:
        return []
    members = [
        m for m in list_items_in_sprint(engine, sprint["id"])
        if m.get("status") not in TERMINAL_STATUSES
    ]
    return sorted(members, key=lambda i: (i.get("priority") or "P2", i.get("created_at") or ""))


# ── since parsing ─────────────────────────────────────────────────────────────

# in_progress items untouched this long count as stale (get_sprint_review +
# get_context_snapshot share the constant).
STALE_DAYS = 7


def _last_release_anchor(conn: sa.Connection, project_id: int) -> datetime | None:
    """Most recent released_at for the project, or None if never released."""
    row = conn.execute(
        sa.select(sa.func.max(changelog_entries.c.released_at))
        .where(
            changelog_entries.c.project_id == project_id,
            changelog_entries.c.deleted_at.is_(None),
            changelog_entries.c.released_at.isnot(None),
        )
    ).first()
    return row[0] if row else None


def _parse_since_conn(
    conn: sa.Connection | None, project_id: int, since: str
) -> tuple[datetime | None, str]:
    """Conn-level body of parse_since (same accepted forms / return contract).

    `conn` is touched ONLY by the "last_release" branch — every other form is
    pure string parsing, so callers without a transaction may pass conn=None
    for those (parse_since relies on this to keep its no-DB fast paths).
    """
    if since in ("", "last_release"):
        if conn is None:
            # Explicit raise, not assert: `python -O` strips asserts and this
            # would degrade into an AttributeError deep in the query (K-41).
            raise ValueError("last_release parsing needs a connection")
        last_release = _last_release_anchor(conn, project_id)
        if last_release:
            return last_release, f"last_release ({last_release.isoformat()})"
        return None, "no releases — showing all time"

    if since == "all":
        return None, "all time"

    # Hour shortcuts
    if since.endswith("h") and since[:-1].isdigit():
        hours = int(since[:-1])
        anchor = datetime.now(UTC) - timedelta(hours=hours)
        return anchor, f"last {hours}h"

    # ISO date / datetime
    try:
        anchor = datetime.fromisoformat(since)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        return anchor, f"since {anchor.isoformat()}"
    except ValueError:
        raise ValueError(
            f"Invalid `since`: '{since}'. Accepted: last_release, all, <N>h (e.g. 24h), ISO date"
        )


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
            return _parse_since_conn(conn, project_id, since)
    return _parse_since_conn(None, 0, since)  # no-DB branches, project untouched


# ── review ────────────────────────────────────────────────────────────────────


def _json_safe(value):
    """Make one payload value JSON-serializable (K-36, #413).

    datetime/date → ISO 8601 string; str/int/float/bool/None pass through
    untouched (already-ISO strings are NEVER re-converted); list/tuple and dict
    are mapped element-wise so nested cells (cp=[done,total], handoff rows)
    are covered too. Anything else falls back to str() — the MCP layer must
    never hand the model a value json.dumps would reject.
    """
    from datetime import date, datetime

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _to_cols_rows(rows: list[dict], cols: list[str], cap: int | None = None) -> dict:
    """Render list of dicts as compact {cols, rows} table.

    Every cell passes through _json_safe, so the table is directly
    json.dumps-able (K-36). When `cap` is given the table carries at most `cap`
    rows plus "truncated": bool telling the reader whether anything was dropped
    (K-34) — an uncapped call keeps the historical two-key shape.
    """
    if cap is None:
        return {"cols": cols, "rows": [[_json_safe(r.get(c)) for c in cols] for r in rows]}
    kept = rows[:cap]
    return {
        "cols": cols,
        "rows": [[_json_safe(r.get(c)) for c in cols] for r in kept],
        "truncated": len(rows) > cap,
    }


def _query_recent_activity(
    conn: sa.Connection, project_id: int, anchor: datetime | None
) -> tuple[list[dict], int, int]:
    """(items_changed_rows, checkpoints_completed, changelog_added) since anchor.

    anchor=None means NO cutoff — "all time" / no-releases-yet must return the
    data their label promises, not empty activity (K-18, #413). The items list
    stays capped at 20 via _query_items_simple.
    """
    changed_rows = _query_items_simple(conn, project_id, changed_since=anchor)

    cp_q = (
        sa.select(sa.func.count())
        .select_from(item_checkpoints.join(items, items.c.id == item_checkpoints.c.item_id))
        .where(
            items.c.project_id == project_id,
            item_checkpoints.c.status == "done",
            item_checkpoints.c.deleted_at.is_(None),
        )
    )
    if anchor is not None:
        cp_q = cp_q.where(item_checkpoints.c.updated_at > anchor)
    checkpoints_done = conn.execute(cp_q).scalar() or 0

    cl_q = sa.select(sa.func.count()).where(
        changelog_entries.c.project_id == project_id,
        changelog_entries.c.deleted_at.is_(None),
    )
    if anchor is not None:
        cl_q = cl_q.where(changelog_entries.c.created_at > anchor)
    changelog_added = conn.execute(cl_q).scalar() or 0

    return changed_rows, checkpoints_done, changelog_added


def _query_total_open(conn: sa.Connection, project_id: int) -> int:
    """Open work = status open OR in_progress (K-18) — shared by the review
    dashboard and get_context_snapshot."""
    return conn.execute(
        sa.select(sa.func.count())
        .where(
            items.c.project_id == project_id,
            items.c.status.in_(["open", "in_progress"]),
            items.c.deleted_at.is_(None),
        )
    ).scalar() or 0


def _query_sprint_items_with_cp(conn: sa.Connection, project_id: int) -> list[dict]:
    """Open items of ALL active sprints with their sprint_id and cp progress —
    the multi-sprint union view for get_context_snapshot. An item linked to two
    active sprints appears once per sprint (sprint_id disambiguates). Terminal
    members are a completion statistic, not current work — excluded here.
    Ordered P1-first, created_at, id."""
    priority_order = _priority_order_expr()
    cp_done, cp_total = _cp_progress_exprs()

    q = (
        sa.select(items, item_sprints.c.sprint_id.label("sprint_id"), cp_done, cp_total)
        .select_from(
            item_sprints.join(sprints, sprints.c.id == item_sprints.c.sprint_id)
            .join(items, items.c.id == item_sprints.c.item_id)
            .outerjoin(
                item_checkpoints,
                sa.and_(
                    item_checkpoints.c.item_id == items.c.id,
                    item_checkpoints.c.deleted_at.is_(None),
                ),
            )
        )
        .where(
            sprints.c.project_id == project_id,
            sprints.c.status == "active",
            sprints.c.deleted_at.is_(None),
            item_sprints.c.removed_at.is_(None),
            items.c.deleted_at.is_(None),
            # K-26: legacy cross-project links must never leak another
            # project's items into this snapshot.
            items.c.project_id == project_id,
            items.c.status.notin_(TERMINAL_STATUSES),
        )
        .group_by(items.c.id, item_sprints.c.sprint_id)
        .order_by(priority_order, items.c.created_at, items.c.id)
    )
    out = []
    for r in conn.execute(q):
        m = r._mapping
        out.append({
            "id": m["id"],
            "sprint_id": m["sprint_id"],
            "priority": m["priority"],
            "status": m["status"],
            "cp": [int(m["cp_done"] or 0), int(m["cp_total"] or 0)],
            "short_title": m["short_title"],
            "title": m["title"],
        })
    return out


def _query_sprint_membership_stats(conn: sa.Connection, project_id: int) -> tuple[int, int]:
    """(total_members, completed_members) across ALL active sprints — DISTINCT
    items with a live link (removed_at IS NULL) to an active sprint. Terminal
    statuses (resolved / wont_fix) count as completed. Membership survives
    resolution since K-18 (#413), which is what makes completion computable.

    NOT derivable from _sprint_membership_stats_by_sprint: an item linked to
    two active sprints counts in EACH sprint's per-sprint stats but ONCE here
    (K-38) — summing the grouped result would double it. The two readers
    therefore share the counting rule (_membership_count_exprs) but keep
    separate queries."""
    base = (
        sa.select(*_membership_count_exprs())
        .select_from(
            item_sprints.join(sprints, sprints.c.id == item_sprints.c.sprint_id)
            .join(items, items.c.id == item_sprints.c.item_id)
        )
        .where(
            sprints.c.project_id == project_id,
            sprints.c.status == "active",
            sprints.c.deleted_at.is_(None),
            item_sprints.c.removed_at.is_(None),
            items.c.deleted_at.is_(None),
            # K-26: exclude legacy cross-project links from the counters too.
            items.c.project_id == project_id,
        )
    )
    row = conn.execute(base).first()
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def _query_stale_in_progress(
    conn: sa.Connection, project_id: int, days: int = STALE_DAYS
) -> list[dict]:
    """in_progress items untouched for `days` — updated_at ISO-formatted."""
    stale_anchor = datetime.now(UTC) - timedelta(days=days)
    stale_rows = conn.execute(
        sa.select(items.c.id, items.c.short_title, items.c.title, items.c.updated_at)
        .where(
            items.c.project_id == project_id,
            items.c.status == "in_progress",
            items.c.updated_at < stale_anchor,
            items.c.deleted_at.is_(None),
        )
        .order_by(items.c.updated_at, items.c.id)
    ).fetchall()
    return [
        {
            "id": r._mapping["id"],
            "updated_at": r._mapping["updated_at"].isoformat() if r._mapping["updated_at"] else None,
            "short_title": r._mapping["short_title"],
            "title": r._mapping["title"],
        }
        for r in stale_rows
    ]


def get_sprint_review(
    engine: sa.Engine,
    project_slug: str,
    since: str = "last_release",
) -> dict:
    """Dashboard payload for session start/end.

    Returns data only — interpretation (what to prioritize, what's stale, etc.)
    lives in the /track-review skill so it can be iterated without schema change.
    """
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        anchor, since_label = _parse_since_conn(conn, project_id, since)

        # in_sprint — items flagged
        in_sprint_rows = _query_items_with_cp(conn, project_id, where_in_sprint=True)

        changed_rows, checkpoints_done, changelog_added = _query_recent_activity(
            conn, project_id, anchor
        )
        stale = _query_stale_in_progress(conn, project_id)
        handoffs = _query_unconsumed_handoffs(conn, project_id)

        # totals — in-flight work IS open work (K-18: open + in_progress)
        total_open = _query_total_open(conn, project_id)

        # completion over MEMBERSHIP (retained through resolve since K-18),
        # not over the open-work view — otherwise it is forever 0/N.
        sprint_size, sprint_done = _query_sprint_membership_stats(conn, project_id)

    # cols ordered short-to-long: fixed-width fields left, free text (title) right.
    return {
        "project": project_slug,
        "since": since,
        "since_anchor": since_label,
        "in_sprint": _to_cols_rows(
            in_sprint_rows,
            ["id", "priority", "status", "cp", "short_title", "title"],
        ),
        "recent_activity": {
            "items_changed": _to_cols_rows(
                changed_rows,
                ["id", "status", "updated_at", "short_title", "title"],
            ),
            "checkpoints_completed": checkpoints_done,
            "changelog_added": changelog_added,
        },
        "stale_in_progress": _to_cols_rows(
            stale,
            ["id", "updated_at", "short_title", "title"],
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
    cp_done, cp_total = _cp_progress_exprs()

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
            "short_title": m["short_title"],
            "title": m["title"],
        })
    return out


def _query_items_simple(
    conn: sa.Connection,
    project_id: int,
    changed_since: datetime | None,
) -> list[dict]:
    """Recently-changed items, newest first, capped at 20. changed_since=None
    means no cutoff (the K-18 'all time' contract) — the limit still applies."""
    q = (
        sa.select(
            items.c.id, items.c.short_title, items.c.title, items.c.status, items.c.updated_at,
        )
        .where(
            items.c.project_id == project_id,
            items.c.deleted_at.is_(None),
        )
        .order_by(items.c.updated_at.desc(), items.c.id.desc())
        .limit(20)
    )
    if changed_since is not None:
        q = q.where(items.c.updated_at > changed_since)
    rows = conn.execute(q).fetchall()
    return [
        {
            "id": r._mapping["id"],
            "status": r._mapping["status"],
            "updated_at": r._mapping["updated_at"].isoformat() if r._mapping["updated_at"] else None,
            "short_title": r._mapping["short_title"],
            "title": r._mapping["title"],
        }
        for r in rows
    ]
