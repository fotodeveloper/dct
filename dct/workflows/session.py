"""Session-level workflow orchestration — the G1 composed ops (#413).

get_context_snapshot / pickup_work / handoff_work: ONE implementation consumed
by the MCP tools, the CLI (`dct pickup` / `dct handoff create`) and the
packaged /pickup + /handoff skills. Each op composes conn-level core helpers
inside a SINGLE transaction, so a resuming agent reads a consistent view and a
failed write leaves no debris (a handoff validation error rolls the sprint
write back too).

Contract: STRUCTURED DATA ONLY. The snapshot carries facts from the DB —
sprint state, handoff, gates, decisions, recent changes; interpretation
("what should happen next") lives in the consuming skill/assistant so the
schema can evolve without baking in guesses. A future interpretive layer
arrives ADDITIVELY as annotate_snapshot(snapshot) plus an opt-in flag on the
tools — never as changes to the base shape.

Multi-sprint semantics (cap lifted in G1): several sprints may be active; the
snapshot lists all of them (primary first), and handoff_work demands an
explicit `sprint` selector when the target is ambiguous — conflict error over
guessing (plan #12 §14).

JSON contract (K-36): every value returned by get_context_snapshot /
pickup_work is JSON-serializable — datetimes are ISO 8601 strings, never
`datetime` objects. `json.dumps(snapshot)` must succeed without a `default=`
hook on any path, consumed handoff included.

Size contract (K-34): the unbounded tables are capped (see SNAPSHOT_CAPS) and
each capped {cols,rows} table carries "truncated": bool. handoff.latest is
DELIBERATELY exempt — the full prompt is the no-transcript criterion (K-11).
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from dct.core import handoff as handoff_core
from dct.core import sprints as sprints_core
from dct.core.checkpoints import (
    _query_sprint_item_checkpoints,
    _query_sprint_pending_gates,
)
from dct.core.decisions import _query_recent_decisions
from dct.core.handoff import _query_unconsumed_handoffs
from dct.core.projects import resolve_project_id
from dct.core.sprint import (
    _json_safe,
    _parse_since_conn,
    _query_recent_activity,
    _query_sprint_items_with_cp,
    _query_sprint_membership_stats,
    _query_stale_in_progress,
    _query_total_open,
    _to_cols_rows,
)

SNAPSHOT_SECTIONS = ("sprint", "handoff", "gates", "decisions", "recent_changes")

# Row caps for the unbounded snapshot tables (K-34, #413) — a runaway project
# must not blow the consumer's context. Each capped table reports "truncated".
# handoff.latest is NOT capped by design (K-11: full prompt, no transcript).
SNAPSHOT_CAPS = {
    "sprint_items": 100,
    "sprint_checkpoints": 500,
    "gates": 50,
    "handoff_others": 20,
    "stale_in_progress": 50,
}

PREVIEW_CHARS = 200


def _preview(text: str | None, limit: int = PREVIEW_CHARS) -> str | None:
    """Compact free text to at most `limit` chars, ELLIPSIS INCLUDED.

    The documented "200-char preview" is a hard ceiling: a string of exactly
    `limit` chars comes back untouched (no marker), a longer one becomes
    `limit - 1` chars plus '…' — so len(result) <= limit always. Non-str input
    (None) passes through. Single implementation, shared by the snapshot
    (handoff.others, decision summaries) and the MCP list views (K-35).
    """
    if not isinstance(text, str):
        return text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_sections(include: str | list[str] | None) -> list[str] | None:
    """Normalize an `include` argument from ANY surface into the list form the
    workflow ops take.

    Accepts a comma-separated string ("sprint,gates" — the MCP and CLI wire
    format), an already-split list, or None. Blank fragments are dropped and
    each name is stripped; input that carries no name at all (None, "", ",,")
    collapses to None, i.e. "all sections". VOCABULARY IS NOT CHECKED HERE —
    validation (unknown names, the 'none' sentinel) stays in _resolve_sections
    so every surface reports the same errors.
    """
    if include is None:
        return None
    parts = include.split(",") if isinstance(include, str) else list(include)
    cleaned = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    return cleaned or None


def _resolve_sections(include: list[str] | None) -> tuple[str, ...]:
    """Normalize the `include` argument. None/[] → all sections; the single
    sentinel "none" → zero sections (cheap confirm calls); unknown names raise
    a ValueError naming the valid vocabulary. 'none' mixed with real section
    names is a contradiction and raises too — never silently one or the other."""
    if not include:
        return SNAPSHOT_SECTIONS
    parts = [p.strip() for p in include if p and p.strip()]
    if not parts:
        return SNAPSHOT_SECTIONS
    if parts == ["none"]:
        return ()
    if "none" in parts:
        raise ValueError(
            f"'none' must be used alone — got {parts}. Pass include=['none'] "
            f"for a summary-only snapshot, or list only real section names "
            f"({', '.join(SNAPSHOT_SECTIONS)})."
        )
    unknown = [p for p in parts if p not in SNAPSHOT_SECTIONS]
    if unknown:
        raise ValueError(
            f"Unknown snapshot section(s): {unknown}. "
            f"Valid: {', '.join(SNAPSHOT_SECTIONS)}, or the single sentinel 'none'."
        )
    # Preserve canonical order regardless of caller order.
    return tuple(s for s in SNAPSHOT_SECTIONS if s in parts)


def _handoff_brief(row: dict) -> dict:
    """Preview row for handoff.others — everything but the full prompt."""
    return {
        "id": row["id"],
        "scope": row.get("scope"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "prompt": _preview(row.get("prompt")),
    }


def _build_snapshot(
    conn: sa.Connection,
    project_id: int,
    project_slug: str,
    sections: tuple[str, ...],
    since: str,
    scope: str | None = None,
) -> dict:
    """Assemble the snapshot dict on an existing connection.

    Always present: project, sections, summary. Skipped sections are OMITTED
    (probe with `"gates" in snapshot`), never None. When `scope` is given the
    handoff section is scope-coherent: `latest` is the newest unconsumed
    handoff OF THAT SCOPE (K-3) — `others` still lists every remaining
    unconsumed handoff so nothing is hidden.

    Counting rule (K-38): an item linked to TWO active sprints counts once in
    EACH sprint's active_sprints[].size/done (per-sprint membership) but only
    ONCE in summary.sprint_size / sprint_completion (DISTINCT items across all
    active sprints) — the two numbers are not expected to add up.

    Caps (K-34): sprint.items, sprint.checkpoints, sprint.stale_in_progress,
    gates and handoff.others are truncated per SNAPSHOT_CAPS and each carries
    "truncated": bool. The summary counters (stale_count, gates_pending,
    handoffs_pending, sprint_size) are computed over the FULL sets, so a
    truncated table never hides the real magnitude. handoff.latest is never
    truncated. Every value is JSON-serializable (K-36).
    """
    anchor, since_label = _parse_since_conn(conn, project_id, since)
    snap: dict = {
        "project": project_slug,
        "since": since,
        "since_anchor": since_label,
        "sections": list(sections),
    }

    active = sprints_core._list_active_sprints_conn(conn, project_id)
    # ONE stale query — the section table and summary.stale_count read the same
    # list instead of hitting the DB twice with a moving `now` anchor (K-41).
    stale = _query_stale_in_progress(conn, project_id)

    if "sprint" in sections:
        stats = sprints_core._sprint_membership_stats_by_sprint(
            conn, project_id, [s["id"] for s in active]
        )
        snap["sprint"] = {
            "primary_sprint_id": active[0]["id"] if active else None,
            "active_sprints": [
                {
                    "id": s["id"],
                    "code": s.get("code"),
                    "name": s.get("name"),
                    "goal": s.get("goal"),
                    "started_at": _json_safe(s.get("started_at")),
                    "size": stats.get(s["id"], (0, 0))[0],
                    "done": stats.get(s["id"], (0, 0))[1],
                }
                for s in active
            ],
            "items": _to_cols_rows(
                _query_sprint_items_with_cp(conn, project_id),
                ["id", "sprint_id", "priority", "status", "cp", "title"],
                cap=SNAPSHOT_CAPS["sprint_items"],
            ),
            "checkpoints": _to_cols_rows(
                _query_sprint_item_checkpoints(conn, project_id),
                ["item_id", "id", "status", "kind", "sort_order", "text"],
                cap=SNAPSHOT_CAPS["sprint_checkpoints"],
            ),
            "stale_in_progress": _to_cols_rows(
                stale,
                ["id", "updated_at", "title"],
                cap=SNAPSHOT_CAPS["stale_in_progress"],
            ),
        }

    unconsumed = _query_unconsumed_handoffs(conn, project_id)
    if "handoff" in sections:
        if scope:
            in_scope = [h for h in unconsumed if h.get("scope") == scope]
            latest = in_scope[0] if in_scope else None
        else:
            latest = unconsumed[0] if unconsumed else None
        others = [h for h in unconsumed if latest is None or h["id"] != latest["id"]]
        snap["handoff"] = {
            # FULL prompt, never truncated — the G2 no-transcript criterion.
            "latest": _json_safe(latest),
            "others": _to_cols_rows(
                [_handoff_brief(h) for h in others],
                ["id", "scope", "created_by", "created_at", "prompt"],
                cap=SNAPSHOT_CAPS["handoff_others"],
            ),
            "pending_count": len(unconsumed),
        }

    gates = _query_sprint_pending_gates(conn, project_id)
    if "gates" in sections:
        snap["gates"] = _to_cols_rows(
            gates,
            ["item_id", "checkpoint_id", "kind", "priority", "item_title", "text"],
            cap=SNAPSHOT_CAPS["gates"],
        )

    if "decisions" in sections:
        decisions = _query_recent_decisions(conn, project_id)
        snap["decisions"] = _to_cols_rows(
            [
                {
                    "id": d["id"],
                    "kind": d["kind"],
                    "status": d["status"],
                    "ref_code": d.get("ref_code"),
                    "created_at": d.get("created_at"),
                    "title": d.get("title"),
                    "decision_preview": _preview(d.get("decision")),
                }
                for d in decisions
            ],
            ["id", "kind", "status", "ref_code", "created_at", "title", "decision_preview"],
        )

    if "recent_changes" in sections:
        changed_rows, checkpoints_done, changelog_added = _query_recent_activity(
            conn, project_id, anchor
        )
        snap["recent_changes"] = {
            "items_changed": _to_cols_rows(
                changed_rows, ["id", "status", "updated_at", "title"]
            ),
            "checkpoints_completed": checkpoints_done,
            "changelog_added": changelog_added,
        }

    total_size, total_done = _query_sprint_membership_stats(conn, project_id)
    snap["summary"] = {
        "active_sprint_count": len(active),
        "sprint_size": total_size,
        "sprint_completion": f"{total_done}/{total_size}" if total_size else "0/0",
        "total_open_in_project": _query_total_open(conn, project_id),
        "handoffs_pending": len(unconsumed),
        "gates_pending": len(gates),
        "stale_count": len(stale),
    }
    return snap


def get_context_snapshot(
    engine: sa.Engine,
    project_slug: str,
    *,
    include: list[str] | None = None,
    since: str = "last_release",
) -> dict:
    """One composed read of a project's execution state (#413 CP1240).

    Sections (SNAPSHOT_SECTIONS, skippable via `include`): sprint (all active
    sprints, primary first + open member items with cp + their checkpoints +
    stale in_progress), handoff (latest full / others previewed / pending
    count), gates (pending gate checkpoints of in-sprint items), decisions
    (accepted+proposed, newest 10, decision text previewed), recent_changes.
    Always present: project, since, since_anchor, sections, summary.
    Runs in ONE transaction — a consistent view, no torn reads.

    The big tables are row-capped (SNAPSHOT_CAPS) and say so via "truncated";
    summary counters stay computed over the full sets. The return is
    json.dumps-able as-is (K-36).
    """
    sections = _resolve_sections(include)
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        return _build_snapshot(conn, project_id, project_slug, sections, since)


_HANDOFF_NOT_FOUND = "Handoff #{id} not found or already consumed"


def pickup_work(
    engine: sa.Engine,
    project_slug: str,
    *,
    consume: bool | None = None,
    handoff_id: int | None = None,
    scope: str | None = None,
    consumed_by: str | None = None,
    since: str = "last_release",
    include: list[str] | None = None,
) -> dict:
    """Session-start op: snapshot + (possibly) consume ONE handoff (CP1241).

    consume semantics (owner decision 2026-07-28):
      - None ("auto", default): handoff_id given → consume it; otherwise
        consume ONLY when exactly one unconsumed candidate matches (after the
        scope filter) — several candidates mean the human picks, so nothing is
        consumed and the candidates ride back in the handoff section.
      - True: consume handoff_id or the newest matching candidate; ValueError
        when there is nothing to consume.
      - False: pure read. Combining consume=False with handoff_id raises —
        pass get_handoff-style reads through get_context_snapshot instead.

    consumed_by is REQUIRED whenever consumption is possible (auto/True) —
    mirrors create_handoff's created_by discipline (plan #12 §14 actor
    identity); ValueError otherwise, never a silent downgrade to "never".

    handoff_id wins over scope: scope is then ignored EVERYWHERE, the returned
    handoff.latest included (K-41) — an explicit id means "this one", not "this
    one, and now show me a different workstream". A handoff_id belonging to
    another project raises the same "not found or already consumed" error as a
    missing id (K-1 — no cross-project consume).

    Returns {**snapshot(POST-consume), "picked_up": full row | None,
    "consumed": bool}. The consumed handoff lives ONLY in picked_up; the
    handoff section and handoffs_pending reflect the state AFTER the write
    (K-3/K-4). When `scope` was given (and not overridden by handoff_id) the
    handoff section also carries "scope_matched": <int> — how many unconsumed
    handoffs of that scope remain after the write, so 0 ("nothing for this
    workstream") is distinguishable from "several pending, pick one". The key
    is OMITTED when no scope was given. Snapshot read + consume share one
    transaction; the whole return is json.dumps-able (K-36).
    """
    if consume is False and handoff_id is not None:
        raise ValueError(
            "handoff_id together with consume=false is contradictory — "
            "use get_context_snapshot (or get_handoff) for pure reads."
        )
    may_consume = consume is not False
    if may_consume and (consumed_by is None or not consumed_by.strip()):
        raise ValueError(
            "consumed_by cannot be empty when consumption is possible "
            "(consume auto/true) — identify the session (model + timestamp), "
            "or pass consume=false for a pure read."
        )

    sections = _resolve_sections(include)
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)

        # ONE candidate list for BOTH consume paths (K-37): the scope filter
        # lives in SQL, so auto and consume=true can never disagree about what
        # "matching" means. Skipped when an explicit id was given — that path
        # ignores scope entirely.
        candidates: list[dict] = []
        effective_scope = None if handoff_id is not None else scope
        if handoff_id is None:
            candidates = _query_unconsumed_handoffs(conn, project_id, effective_scope)

        target: dict | None = None
        if handoff_id is not None:
            row = conn.execute(
                sa.select(handoff_core.session_handoffs).where(
                    handoff_core.session_handoffs.c.id == handoff_id,
                    handoff_core.session_handoffs.c.project_id == project_id,
                    handoff_core.session_handoffs.c.deleted_at.is_(None),
                    handoff_core.session_handoffs.c.consumed_at.is_(None),
                )
            ).first()
            if row is None:
                raise ValueError(_HANDOFF_NOT_FOUND.format(id=handoff_id))
            target = handoff_core.row_to_dict(row)
        elif consume is True:
            # candidates is newest-first — [0] IS the "newest matching" rule.
            if not candidates:
                where = f" with scope {scope!r}" if scope else ""
                raise ValueError(f"No unconsumed handoff{where} to consume")
            target = candidates[0]
        elif consume is None and len(candidates) == 1:
            target = candidates[0]

        picked_up: dict | None = None
        if target is not None:
            picked_up = handoff_core._consume_handoff_conn(
                conn, target["id"], consumed_by
            )
            if picked_up is None:
                # Raced away between select and update. For a server-selected
                # target (auto / consume=True without an id) this is the
                # concurrent-session case — degrade to a plain read instead of
                # failing the whole session start (K-32); the snapshot below
                # reflects the post-race state. An EXPLICIT handoff_id keeps
                # the error: the caller asked for that one specifically.
                if handoff_id is not None:
                    raise ValueError(_HANDOFF_NOT_FOUND.format(id=target["id"]))
                target = None

        snap = _build_snapshot(
            conn, project_id, project_slug, sections, since, effective_scope
        )
        if effective_scope and "handoff" in snap:
            # Post-consume count for THIS workstream — 0 means "nothing left
            # for me", which the global pending_count cannot express.
            snap["handoff"]["scope_matched"] = len(
                _query_unconsumed_handoffs(conn, project_id, effective_scope)
            )

    snap["picked_up"] = _json_safe(picked_up)
    snap["consumed"] = picked_up is not None
    return snap


def handoff_work(
    engine: sa.Engine,
    project_slug: str,
    *,
    prompt: str,
    created_by: str,
    item_ids: list[int] | None = None,
    add_item_ids: list[int] | None = None,
    remove_item_ids: list[int] | None = None,
    sprint: str | int | None = None,
    scope: str | None = None,
    context_summary: str | None = None,
) -> dict:
    """Session-end op: sprint membership changes + the handoff ATOMICALLY
    (CP1241). One transaction, sprint writes FIRST, handoff second — a handoff
    validation failure rolls every membership write back.

    Membership semantics (ADR #57 — sprints are multi-session; the session
    edge is ADDITIVE by default):
      - all three lists None → ZERO sprint mutation (no auto-create; K-7).
      - add_item_ids / remove_item_ids → additive edge: pull items into /
        cut items from the target sprint. `remove` of a TERMINAL member
        raises — resolved work is the sprint's history, not removable scope
        (use remove_item_from_sprint for the manual escape hatch).
      - item_ids (list) → conscious REPLACE of the OPEN membership; terminal
        members are immune (K-24). Mutually exclusive with add/remove.
      - item_ids == [] → ValueError: empty replace is not supported (K-27) —
        emptying is clear_sprint's job; delimiter-garbage input must never
        silently wipe a sprint.
    All id lists are STRICT (K-8): unknown/foreign/deleted ids raise and roll
    everything back.

    sprint selects the target among ACTIVE sprints by id or code (K-25:
    id-match takes precedence, an id↔code collision raises). Given without
    any membership list → ValueError (a typo must not silently no-op, K-29).
    Default target: the only active sprint; several actives and no selector →
    ValueError listing candidates. None active → auto-created for replace/add
    paths; remove-only with no active sprint raises.

    Returns {"project", "handoff": full row, "sprint": {"sprint_id", "code",
    "size", "items"} | None (None ⇔ no membership op requested)}; items/size
    describe the OPEN membership after the writes.
    """
    replace_requested = item_ids is not None
    additive_requested = add_item_ids is not None or remove_item_ids is not None
    if replace_requested and additive_requested:
        raise ValueError(
            "item_ids (replace) cannot be combined with add_item_ids / "
            "remove_item_ids (additive) — pick one mode."
        )
    if replace_requested and not item_ids:
        raise ValueError(
            "Empty replace is not supported — keep membership (item_ids=None), "
            "cut scope via remove_item_ids, or empty the sprint explicitly "
            "with clear_sprint."
        )
    membership_op = replace_requested or additive_requested
    if sprint is not None and str(sprint).strip() and not membership_op:
        raise ValueError(
            f"sprint={sprint!r} was given but no membership change was "
            f"requested — pass item_ids / add_item_ids / remove_item_ids, "
            f"or drop the selector."
        )

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)

        sprint_result: dict | None = None
        if membership_op:
            target_sprint: dict | None = None
            if sprint is not None and str(sprint).strip():
                target_sprint = sprints_core._resolve_active_sprint_conn(
                    conn, project_id, sprint
                )
            else:
                active = sprints_core._list_active_sprints_conn(conn, project_id)
                if len(active) > 1:
                    # !r on the DB-sourced label: a newline in a sprint name
                    # must not break the single-line `✗ Error: ...` convention.
                    listing = ", ".join(
                        f"#{s['id']} ({(s.get('code') or s.get('name') or '')!r})"
                        for s in active
                    )
                    raise ValueError(
                        f"{len(active)} active sprints — pass sprint=<id or code> "
                        f"to pick the target: {listing}"
                    )
                if len(active) == 1:
                    target_sprint = active[0]

            if replace_requested:
                assert item_ids is not None
                out = sprints_core._set_current_sprint_items_conn(
                    conn,
                    project_id,
                    item_ids,
                    sprint_id=target_sprint["id"] if target_sprint else None,
                )
                sprint_id = out["sprint_id"]
            else:
                if target_sprint is None:
                    if remove_item_ids and not add_item_ids:
                        raise ValueError(
                            "remove_item_ids given but the project has no "
                            "active sprint to remove from."
                        )
                    target_sprint = sprints_core._ensure_current_sprint_conn(
                        conn, project_id
                    )
                sprint_id = target_sprint["id"]
                now = datetime.now(timezone.utc)
                if add_item_ids:
                    valid = sprints_core._validate_project_items_conn(
                        conn, project_id, list(add_item_ids)
                    )
                    sprints_core._add_members_conn(conn, sprint_id, valid, now)
                if remove_item_ids:
                    valid = sprints_core._validate_project_items_conn(
                        conn, project_id, list(remove_item_ids)
                    )
                    open_members = sprints_core._member_ids_conn(
                        conn, sprint_id, open_only=True
                    )
                    all_members = sprints_core._member_ids_conn(conn, sprint_id)
                    terminal_requested = (valid & all_members) - open_members
                    if terminal_requested:
                        raise ValueError(
                            f"Cannot remove terminal member(s) "
                            f"{sorted(terminal_requested)} — resolved work is "
                            f"the sprint's history (ADR #57). Use "
                            f"remove_item_from_sprint for a manual override."
                        )
                    sprints_core._remove_members_conn(
                        conn, sprint_id, valid & open_members, now
                    )

            open_after = sorted(
                sprints_core._member_ids_conn(conn, sprint_id, open_only=True)
            )
            resolved_target = target_sprint or {"code": None}
            sprint_result = {
                "sprint_id": sprint_id,
                "code": resolved_target.get("code"),
                "size": len(open_after),
                "items": open_after,
            }

        handoff_row = handoff_core._create_handoff_conn(
            conn, project_id, prompt, created_by, scope, context_summary
        )

    return {"project": project_slug, "handoff": handoff_row, "sprint": sprint_result}
