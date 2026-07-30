"""Pure aggregation helpers over dct.core data.

Engine-first, JSON-native return dicts, no ORM. Every query is a single
GROUP BY (or a constant number of grouped queries merged in Python) — never a
per-project loop (no N+1). All aggregates exclude soft-deleted rows
(`deleted_at IS NULL`; `item_sprints` uses `removed_at IS NULL`).

Read-only: this module issues SELECTs only. Used by the web viewer
(dct/web) and exposable later via MCP/CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import sqlalchemy as sa

from dct.core.checkpoints import NON_GATE_KINDS
from dct.core.items import TERMINAL_STATUSES
from dct.db import (
    changelog_entries,
    item_checkpoints,
    item_sprints,
    items,
    projects,
    session_handoffs,
    sprints,
)

OPEN_STATUSES = ("open", "in_progress")


def _iso(dt) -> str | None:
    """datetime -> ISO 8601 string; None passthrough."""
    return dt.isoformat() if dt is not None else None


def _safe_tags(raw) -> list[str]:
    """Tolerant tag parse: JSON-TEXT | None | garbage -> list[str].

    Reuses items._parse_tags (returns [] on malformed JSON) and additionally
    guards against non-list / non-str payloads so an adversarial `tags` value
    can never crash an aggregate.
    """
    from dct.core.items import _parse_tags

    parsed = _parse_tags(raw)
    if not isinstance(parsed, list):
        return []
    return [t for t in parsed if isinstance(t, str) and t]


def radar(engine: sa.Engine) -> dict:
    """Cross-project operational rollup — one grouped query per table, merged.

    open_total counts status='open'; by_priority breaks down those open items;
    in_progress counts status='in_progress'; stale_Nd counts items with status
    in (open,in_progress) whose updated_at is older than N days.
    """
    now = datetime.now(UTC)
    anchor7 = now - timedelta(days=7)
    anchor30 = now - timedelta(days=30)

    def _open_pri(pri):
        return sa.func.sum(
            sa.case((sa.and_(items.c.status == "open", items.c.priority == pri), 1), else_=0)
        )

    items_q = (
        sa.select(
            items.c.project_id.label("project_id"),
            sa.func.sum(sa.case((items.c.status == "open", 1), else_=0)).label("open_total"),
            _open_pri("P1").label("p1"),
            _open_pri("P2").label("p2"),
            _open_pri("P3").label("p3"),
            _open_pri("future").label("pfuture"),
            sa.func.sum(sa.case((items.c.status == "in_progress", 1), else_=0)).label("in_progress"),
            sa.func.sum(sa.case(
                (sa.and_(items.c.status.in_(OPEN_STATUSES), items.c.updated_at < anchor7), 1), else_=0
            )).label("stale_7d"),
            sa.func.sum(sa.case(
                (sa.and_(items.c.status.in_(OPEN_STATUSES), items.c.updated_at < anchor30), 1), else_=0
            )).label("stale_30d"),
        )
        .where(items.c.deleted_at.is_(None))
        .group_by(items.c.project_id)
    )
    handoffs_q = (
        sa.select(session_handoffs.c.project_id.label("project_id"), sa.func.count().label("cnt"))
        .where(session_handoffs.c.consumed_at.is_(None), session_handoffs.c.deleted_at.is_(None))
        .group_by(session_handoffs.c.project_id)
    )
    clog_q = (
        sa.select(changelog_entries.c.project_id.label("project_id"), sa.func.count().label("cnt"))
        .where(changelog_entries.c.version.is_(None), changelog_entries.c.deleted_at.is_(None))
        .group_by(changelog_entries.c.project_id)
    )

    with engine.connect() as conn:
        proj_rows = conn.execute(
            sa.select(projects.c.id, projects.c.slug, projects.c.name)
            .where(projects.c.deleted_at.is_(None))
            .order_by(projects.c.slug)
        ).fetchall()
        items_by = {r.project_id: r for r in conn.execute(items_q)}
        handoffs_by = {r.project_id: int(r.cnt) for r in conn.execute(handoffs_q)}
        clog_by = {r.project_id: int(r.cnt) for r in conn.execute(clog_q)}

    out_projects = []
    tot_open = tot_ip = tot_stale7 = tot_handoff = 0
    for p in proj_rows:
        it = items_by.get(p.id)
        open_total = int(it.open_total) if it else 0
        in_progress = int(it.in_progress) if it else 0
        stale_7d = int(it.stale_7d) if it else 0
        stale_30d = int(it.stale_30d) if it else 0
        by_priority = {
            "P1": int(it.p1) if it else 0,
            "P2": int(it.p2) if it else 0,
            "P3": int(it.p3) if it else 0,
            "future": int(it.pfuture) if it else 0,
        }
        unconsumed = handoffs_by.get(p.id, 0)
        unreleased = clog_by.get(p.id, 0)
        out_projects.append({
            "slug": p.slug,
            "name": p.name,
            "open_total": open_total,
            "by_priority": by_priority,
            "in_progress": in_progress,
            "stale_7d": stale_7d,
            "stale_30d": stale_30d,
            "unconsumed_handoffs": unconsumed,
            "unreleased_changelog": unreleased,
        })
        tot_open += open_total
        tot_ip += in_progress
        tot_stale7 += stale_7d
        tot_handoff += unconsumed

    return {
        "projects": out_projects,
        "totals": {
            "open_total": tot_open,
            "in_progress": tot_ip,
            "stale_7d": tot_stale7,
            "unconsumed_handoffs": tot_handoff,
        },
    }


def backlog_health(engine: sa.Engine, project_slug: str) -> dict:
    """Single-project distributions, aging, hotspots, tags, checkpoint stats.

    "open backlog" (aging / hotspots / tags) = OPEN_STATUSES (open + in_progress).
    Distributions (by_type/priority/status/component) span all non-deleted items.
    """
    from dct.core.projects import resolve_project_id

    now = datetime.now(UTC)
    a1w = now - timedelta(days=7)
    a4w = now - timedelta(days=28)
    a3m = now - timedelta(days=90)

    with engine.connect() as conn:
        pid = resolve_project_id(conn, project_slug)
        base = sa.and_(items.c.project_id == pid, items.c.deleted_at.is_(None))
        open_base = sa.and_(base, items.c.status.in_(OPEN_STATUSES))

        def _group(col):
            return conn.execute(
                sa.select(col, sa.func.count().label("cnt")).where(base).group_by(col)
            ).fetchall()

        by_type = {r[0]: int(r.cnt) for r in _group(items.c.item_type)}
        by_priority = {r[0]: int(r.cnt) for r in _group(items.c.priority)}
        by_status = {r[0]: int(r.cnt) for r in _group(items.c.status)}

        comp_rows = conn.execute(
            sa.select(items.c.component, sa.func.count().label("cnt"))
            .where(base, items.c.component.isnot(None))
            .group_by(items.c.component)
            .order_by(sa.func.count().desc(), items.c.component)
        ).fetchall()
        by_component = [{"component": r.component, "count": int(r.cnt)} for r in comp_rows]

        aging_row = conn.execute(
            sa.select(
                sa.func.sum(sa.case((items.c.created_at > a1w, 1), else_=0)).label("lt_1w"),
                sa.func.sum(sa.case(
                    (sa.and_(items.c.created_at <= a1w, items.c.created_at > a4w), 1), else_=0
                )).label("w1_4"),
                sa.func.sum(sa.case(
                    (sa.and_(items.c.created_at <= a4w, items.c.created_at > a3m), 1), else_=0
                )).label("m1_3"),
                sa.func.sum(sa.case((items.c.created_at <= a3m, 1), else_=0)).label("gt_3m"),
            ).where(open_base)
        ).first()
        aging = {
            "lt_1w": int(aging_row.lt_1w or 0),
            "w1_4": int(aging_row.w1_4 or 0),
            "m1_3": int(aging_row.m1_3 or 0),
            "gt_3m": int(aging_row.gt_3m or 0),
        }

        def _hotspot(col):
            rows = conn.execute(
                sa.select(col, sa.func.count().label("cnt"))
                .where(open_base, col.isnot(None))
                .group_by(col)
                .having(sa.func.count() >= 2)
                .order_by(sa.func.count().desc(), col)
            ).fetchall()
            return [{"name": r[0], "count": int(r.cnt)} for r in rows]

        hotspots = {"components": _hotspot(items.c.component), "files": _hotspot(items.c.source_file)}

        tag_rows = conn.execute(sa.select(items.c.tags).where(open_base)).fetchall()

        cp_join = item_checkpoints.join(items, items.c.id == item_checkpoints.c.item_id)
        cp_filter = sa.and_(
            items.c.project_id == pid,
            items.c.deleted_at.is_(None),
            item_checkpoints.c.deleted_at.is_(None),
        )
        cp_status_rows = conn.execute(
            sa.select(item_checkpoints.c.status, sa.func.count().label("cnt"))
            .select_from(cp_join).where(cp_filter)
            .group_by(item_checkpoints.c.status)
        ).fetchall()
        cp_by = {r.status: int(r.cnt) for r in cp_status_rows}

        gate_rows = conn.execute(
            sa.select(item_checkpoints.c.kind, sa.func.count().label("cnt"))
            .select_from(cp_join)
            .where(cp_filter, item_checkpoints.c.kind.notin_(NON_GATE_KINDS))
            .group_by(item_checkpoints.c.kind)
        ).fetchall()
        gates_by_kind = {r.kind: int(r.cnt) for r in gate_rows}

    # tag frequency (parsed defensively outside the connection)
    tag_counts: dict[str, int] = {}
    for r in tag_rows:
        for t in _safe_tags(r[0]):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tags = [
        {"tag": t, "count": c}
        for t, c in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    done = cp_by.get("done", 0)
    pending = cp_by.get("pending", 0)
    rejected = cp_by.get("rejected", 0)
    denom = done + pending
    completion_rate = round(done / denom, 4) if denom else 0.0

    return {
        "by_type": by_type,
        "by_priority": by_priority,
        "by_status": by_status,
        "by_component": by_component,
        "aging": aging,
        "hotspots": hotspots,
        "tags": tags,
        "checkpoints": {
            "pending": pending,
            "done": done,
            "rejected": rejected,
            "gates_by_kind": gates_by_kind,
            "completion_rate": completion_rate,
        },
    }


def velocity(engine: sa.Engine, project_slug: str, weeks: int = 26) -> dict:
    """Single-project time-series. Weeks are bucketed by their Monday (ISO date
    string). Bucketing is done in Python (portable across SQLite/Postgres) after
    a constant number of windowed SELECTs — never a per-week query loop.
    """
    from dct.core.projects import resolve_project_id

    now = datetime.now(UTC)
    today = now.date()
    this_monday = today - timedelta(days=today.weekday())
    start_monday = this_monday - timedelta(days=7 * (weeks - 1))
    week_keys = [(start_monday + timedelta(days=7 * i)).isoformat() for i in range(weeks)]
    window_start = datetime.combine(start_monday, time(0, 0, 0))

    def _monday_key(dt) -> str:
        d = dt.date() if isinstance(dt, datetime) else dt
        return (d - timedelta(days=d.weekday())).isoformat()

    with engine.connect() as conn:
        pid = resolve_project_id(conn, project_slug)

        resolved_rows = conn.execute(
            sa.select(items.c.resolved_at).where(
                items.c.project_id == pid,
                items.c.deleted_at.is_(None),
                items.c.status.in_(TERMINAL_STATUSES),
                items.c.resolved_at.isnot(None),
                items.c.resolved_at >= window_start,
            )
        ).fetchall()
        created_rows = conn.execute(
            sa.select(items.c.created_at).where(
                items.c.project_id == pid,
                items.c.deleted_at.is_(None),
                items.c.created_at >= window_start,
            )
        ).fetchall()
        clog_rows = conn.execute(
            sa.select(changelog_entries.c.category, changelog_entries.c.created_at).where(
                changelog_entries.c.project_id == pid,
                changelog_entries.c.deleted_at.is_(None),
                changelog_entries.c.created_at >= window_start,
            )
        ).fetchall()
        release_rows = conn.execute(
            sa.select(
                changelog_entries.c.version,
                sa.func.min(changelog_entries.c.released_at).label("released_at"),
            )
            .where(
                changelog_entries.c.project_id == pid,
                changelog_entries.c.deleted_at.is_(None),
                changelog_entries.c.version.isnot(None),
                changelog_entries.c.released_at.isnot(None),
            )
            .group_by(changelog_entries.c.version)
            .order_by(sa.func.min(changelog_entries.c.released_at))
        ).fetchall()

        isp = item_sprints.alias("isp")
        live_link = sa.and_(isp.c.removed_at.is_(None), items.c.id.isnot(None))
        sprint_rows = conn.execute(
            sa.select(
                sprints.c.name,
                sprints.c.code,
                sa.func.sum(sa.case((live_link, 1), else_=0)).label("size"),
                sa.func.sum(sa.case(
                    (sa.and_(live_link, items.c.status.in_(TERMINAL_STATUSES)), 1), else_=0
                )).label("done"),
            )
            .select_from(
                sprints
                .outerjoin(isp, isp.c.sprint_id == sprints.c.id)
                .outerjoin(items, sa.and_(items.c.id == isp.c.item_id, items.c.deleted_at.is_(None)))
            )
            .where(sprints.c.project_id == pid, sprints.c.deleted_at.is_(None))
            .group_by(sprints.c.id, sprints.c.name, sprints.c.code, sprints.c.created_at)
            .order_by(sprints.c.created_at)
        ).fetchall()

    resolved_per = {k: 0 for k in week_keys}
    for r in resolved_rows:
        k = _monday_key(r[0])
        if k in resolved_per:
            resolved_per[k] += 1
    created_per = {k: 0 for k in week_keys}
    for r in created_rows:
        k = _monday_key(r[0])
        if k in created_per:
            created_per[k] += 1
    clog_per = {k: {"added": 0, "changed": 0, "fixed": 0, "removed": 0} for k in week_keys}
    for r in clog_rows:
        k = _monday_key(r.created_at)
        if k in clog_per and r.category in clog_per[k]:
            clog_per[k][r.category] += 1

    return {
        "resolved_per_week": [{"week": k, "count": resolved_per[k]} for k in week_keys],
        "created_per_week": [{"week": k, "count": created_per[k]} for k in week_keys],
        "backlog_growth": [
            {"week": k, "created": created_per[k], "resolved": resolved_per[k],
             "net": created_per[k] - resolved_per[k]}
            for k in week_keys
        ],
        "changelog_per_week": [{"week": k, **clog_per[k]} for k in week_keys],
        "release_cadence": [
            {"version": r.version, "released_at": _iso(r.released_at)} for r in release_rows
        ],
        "sprint_completion": [
            {"name": r.name, "code": r.code, "done": int(r.done or 0), "size": int(r.size or 0)}
            for r in sprint_rows
        ],
    }
