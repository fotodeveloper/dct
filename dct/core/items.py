"""Item CRUD operations with soft deletes and validation."""

import json
import re
import unicodedata
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.tags import (
    set_item_tags_conn,
    tag_id_for_name_conn,
    tags_for_items_conn,
)
from dct.core.text import is_blank_label, preview
from dct.db import (
    item_checkpoints,
    item_notes,
    item_sprints,
    item_tags,
    items,
    lock_item_row,
    projects,
    row_to_dict,
    sprints,
)

# items.title is VARCHAR(500): PostgreSQL (the primary backend) rejects longer
# values with StringDataRightTruncation while SQLite silently stores them, so
# the limit is enforced HERE — one dialect-independent contract. Derived from
# the column, never a hand-copied literal, so widening the column moves the
# guard with it.
MAX_TITLE_LENGTH: int = items.c.title.type.length

VALID_TYPES = {"issue", "todo", "feature", "idea", "improvement"}
VALID_PRIORITIES = {"P1", "P2", "P3", "future"}
VALID_STATUSES = {"open", "in_progress", "resolved", "wont_fix"}
# Terminal = the item is done with (resolved) or consciously dropped
# (wont_fix). A TUPLE, not a set: this is the single source of truth for every
# `status.in_(...)` / `notin_(...)` filter in the codebase (core, web, CLI) and
# SQLAlchemy wants a deterministic sequence there — membership checks and
# `sorted()` in error messages work the same either way.
TERMINAL_STATUSES = ("resolved", "wont_fix")
OPEN_STATUSES = {"open", "in_progress"}

# Priority values in display order, P1 first — the ranking behind
# _priority_order_expr().
PRIORITY_ORDER = ("P1", "P2", "P3", "future")

# The gate refusal (_refuse_pending_gates_conn) names at most this many pending
# gates — id, kind and text — and counts the rest in a tail that names the
# full read path ("(+M more — get_item(N) or `dct show N` lists them all)").
# Each text (and kind) is compacted to at most GATE_REFUSAL_TEXT_CHARS
# (ellipsis included, text.preview) and encoded as one-line data
# (_gate_literal), so the message stays one readable, unambiguous line however
# many gates there are or whatever their texts contain. Presentation only:
# the stored kind and condition are never changed.
GATE_REFUSAL_LISTED = 5
GATE_REFUSAL_TEXT_CHARS = 60

# A kind shown bare in the refusal; any other kind is shown as a JSON string.
_BARE_KIND = re.compile(r"[A-Za-z0-9_.-]+")


def _priority_order_expr() -> sa.Case:
    """SQL sort key ranking items P1 → P2 → P3 → future (unknown values last).

    Factory, not a module constant: the expression is bound to `items.c` and
    is embedded in several statements, so each caller gets its own instance.
    Every "priority-ordered" view (list_items, the sprint snapshot, the
    sprint-wide gate list) MUST use this — one ranking, one place to change."""
    return sa.case(
        (items.c.priority == "P1", 1),
        (items.c.priority == "P2", 2),
        (items.c.priority == "P3", 3),
        (items.c.priority == "future", 4),
        else_=5,
    )


def _cp_progress_exprs() -> tuple[sa.Label, sa.Label]:
    """(cp_done, cp_total) aggregate columns for a query that outer-joins
    item_checkpoints and groups by item.

    cp_done counts checkpoints in status 'done'; cp_total counts every
    NON-rejected checkpoint (rejected ones are abandoned scope, not work) —
    the same math get_progress reports as done/active. Both coalesce to 0 so
    an item without checkpoints reads [0, 0] rather than [None, None]."""
    cp_done = sa.func.coalesce(
        sa.func.sum(sa.case((item_checkpoints.c.status == "done", 1), else_=0)),
        0,
    ).label("cp_done")
    cp_total = sa.func.coalesce(
        sa.func.sum(sa.case((item_checkpoints.c.status != "rejected", 1), else_=0)),
        0,
    ).label("cp_total")
    return cp_done, cp_total


def _membership_count_exprs() -> tuple[sa.ColumnElement, sa.ColumnElement]:
    """(total, completed) counting columns for a sprint-membership query.

    Both are COUNT(DISTINCT …) over items.c.id, so a query that fans out over
    several link rows still counts each item once; `completed` counts only
    items in a TERMINAL status (membership survives resolution since K-18,
    which is what makes sprint completion computable). Scoping — one sprint,
    a set of sprints, or every active sprint of a project — is the caller's
    WHERE/GROUP BY; only the counting rule is shared."""
    total = sa.func.count(sa.distinct(items.c.id))
    completed = sa.func.count(
        sa.distinct(sa.case((items.c.status.in_(TERMINAL_STATUSES), items.c.id)))
    )
    return total, completed


def _validate_enum(value: str, valid: set[str] | tuple[str, ...], field_name: str) -> str:
    if value not in valid:
        raise ValueError(f"Invalid {field_name}: '{value}'. Valid: {', '.join(sorted(valid))}")
    return value


_ITEM_LABELS = {"title": "Item title"}


def _validate_title_length(title: str) -> str:
    """Refuse a title the items.title column cannot hold — never truncate.

    Silent truncation would lose the tail of a title on PostgreSQL-vs-SQLite
    terms nobody controls; a loud ValueError lets the caller shorten it."""
    ensure_fits(items, {"title": title}, _ITEM_LABELS)
    return title


def _validate_title(title: str) -> str:
    """Refuse an empty/blank title or an overlong one. ONE rule for
    create_item AND update_item (K-12: update used to store "")."""
    if not title or not title.strip():
        raise ValueError("Item title cannot be empty")
    return _validate_title_length(title)


def normalize_short_title(short_title: str | None) -> str | None:
    """The ONE short_title write rule (#817, K-43) — create, update, promote.

    FIRST the raw value is length-checked against items.short_title (> 50
    chars → ValueError, refused, never truncated — surrounding spaces count),
    THEN a blank value (text.is_blank_label: only whitespace and Unicode
    format characters, K-64) is NULL and anything else is stripped of
    surrounding whitespace — format characters inside a visible label are
    kept. None → None. A caller that omits the field never reaches here, so
    omission never clears a label.
    """
    if short_title is None:
        return None
    ensure_fits(items, {"short_title": short_title})
    if is_blank_label(short_title):
        return None
    return short_title.strip()


def _pending_gate_exists(gate: str):
    """Correlated EXISTS: the item has a live PENDING gate checkpoint —
    of any gate kind for 'any', else of that kind (trimmed, compared
    case-insensitively). A WHERE predicate, like _has_tag_exists, so the
    checkpoint aggregates do not fan out. ValueError on a blank value or a
    non-gate kind (dct.core.checkpoints.NON_GATE_KINDS)."""
    from dct.core.checkpoints import (
        NON_GATE_KINDS,  # local: checkpoints imports items lazily too
    )

    wanted = gate.strip().lower() if isinstance(gate, str) else ""
    if not wanted or wanted in NON_GATE_KINDS:
        raise ValueError(
            f"gate must be 'any' or a gate kind (dogfood, review, security, approval, …), "
            f"got {gate!r} — {', '.join(NON_GATE_KINDS)} never gate")
    # An alias: the list query already joins item_checkpoints for the cp
    # aggregates, and the bare table would auto-correlate to that join.
    cp = item_checkpoints.alias("gate_cp")
    q = sa.select(sa.literal(1)).select_from(cp).where(
        cp.c.item_id == items.c.id,
        cp.c.deleted_at.is_(None),
        cp.c.status == "pending",
        cp.c.kind.notin_(NON_GATE_KINDS),
    )
    if wanted != "any":
        q = q.where(sa.func.lower(cp.c.kind) == wanted)
    return q.exists()


def _has_tag_exists(tag_id: int):
    """Correlated EXISTS: the item carries an ACTIVE link to `tag_id`.

    A WHERE predicate, deliberately not a join — joining item_tags into the
    list query would fan out the checkpoint aggregates (#611)."""
    return (
        sa.select(sa.literal(1))
        .select_from(item_tags)
        .where(
            item_tags.c.item_id == items.c.id,
            item_tags.c.tag_id == tag_id,
            item_tags.c.removed_at.is_(None),
        )
        .exists()
    )


def _current_sprint_exists():
    """Correlated EXISTS: the item has an active item_sprints link to an
    active sprint AND is still open work. This IS `in_current_sprint` (#216) —
    derived on read, never stored. Terminal items (resolved / wont_fix) KEEP
    their membership link (K-18, #413 — that is what makes sprint_completion
    computable) but drop out of this flag and every current-work view built
    on it. Use as a labeled column in SELECT or directly in WHERE."""
    return (
        sa.select(sa.literal(1))
        .select_from(item_sprints.join(sprints, sprints.c.id == item_sprints.c.sprint_id))
        .where(
            item_sprints.c.item_id == items.c.id,
            item_sprints.c.removed_at.is_(None),
            sprints.c.status == "active",
            sprints.c.deleted_at.is_(None),
            items.c.status.notin_(TERMINAL_STATUSES),
        )
        .exists()
    )


def _item_to_dict(row: sa.Row, tag_names: list[str] | None = None) -> dict:
    d = row_to_dict(row)
    # The `tags` column is frozen-deprecated (#611): drop whatever it holds and
    # publish the names read from item_tags instead.
    d.pop("tags", None)
    d["tags"] = list(tag_names or [])
    if "in_current_sprint" in d:  # present only when the derived column was selected
        d["in_current_sprint"] = bool(d["in_current_sprint"])
    return d


def _item_with_cp_to_dict(row_mapping, tag_names: list[str] | None = None) -> dict:
    """Convert an items row enriched with cp_done/cp_total aggregates into a dict
    with a compact `cp: [done, total]` field replacing the two raw columns.
    """
    d = dict(row_mapping)
    d.pop("tags", None)
    d["tags"] = list(tag_names or [])
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
    *,
    force_tags: bool = False,
    short_title: str | None = None,
) -> dict:
    """Create an item. `tags` is list[str] (CSV splitting stays at the CLI
    boundary, #31); names are normalized and registered in the global tag
    dictionary. A name the similarity guard refuses rolls the WHOLE call back —
    no orphan item, no dictionary row. `force_tags=True` bypasses that guard
    for machine callers (not exposed over MCP). `short_title` (#817) is the
    item's 2-4 word label, normalized by normalize_short_title (length
    checked on the raw value, then stripped; empty → NULL). One transaction
    around create_item_conn — the item-creation path shared with the
    promotions (the task mirror inserts its fixed-metadata items directly)."""
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        return create_item_conn(
            conn, project_id, item_type, title,
            description=description, priority=priority, status=status,
            component=component, source_file=source_file,
            source_lines=source_lines, tags=tags,
            force_tags=force_tags, short_title=short_title,
        )


def create_item_conn(
    conn: sa.Connection,
    project_id: int,
    item_type: str,
    title: str,
    description: str | None = None,
    priority: str = "P2",
    status: str = "open",
    component: str | None = None,
    source_file: str | None = None,
    source_lines: str | None = None,
    tags: list[str] | None = None,
    *,
    force_tags: bool = False,
    short_title: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Item creation on the caller's connection (#909 K-17/K-20/K-24) —
    create_item, promote_checkpoint_to_item, promote_finding_to_item and
    promote_section_to_item create their item here, so the item and the
    caller's link land in ONE transaction under the same rules. Not every
    item is created here: the task mirror (dct/workflows/task_mirror.py)
    inserts its fixed-metadata item directly inside its own composed
    transaction.

    Validation, all before any write: title (non-blank, fits the column),
    short_title (normalize_short_title), component / source_file /
    source_lines widths (ensure_fits — refused, never truncated), item_type /
    priority / status enums, then `project_id` must name a LIVE project (a
    soft-deleted one → ValueError "Project '<slug>' not found", the same
    refusal resolve_project_id gives create_item; an unknown id → "Project
    #<id> not found"). Tags go through
    set_item_tags_conn in the same transaction (a refused name raises and
    the caller's transaction rolls the item back); provenance columns are
    filled by their column defaults. `now` stamps created_at / updated_at
    (None = the database default). Returns the full item dict with `tags`
    and in_current_sprint=False (a brand-new item belongs to no sprint).
    Raises ValueError on any refusal; never commits or rolls back itself."""
    _validate_title(title)
    short_title = normalize_short_title(short_title)
    ensure_fits(items, {
        "component": component, "source_file": source_file, "source_lines": source_lines,
    })
    _validate_enum(item_type, VALID_TYPES, "item_type")
    _validate_enum(priority, VALID_PRIORITIES, "priority")
    _validate_enum(status, VALID_STATUSES, "status")
    project = conn.execute(
        sa.select(projects.c.slug, projects.c.deleted_at).where(projects.c.id == project_id)
    ).first()
    if project is None:
        raise ValueError(f"Project #{project_id} not found")
    if project.deleted_at is not None:  # resolve_project_id's wording, by slug
        raise ValueError(f"Project '{project.slug}' not found")

    values = {
        "project_id": project_id,
        "item_type": item_type,
        "title": title,
        "short_title": short_title,
        "description": description,
        "priority": priority,
        "status": status,
        "component": component,
        "source_file": source_file,
        "source_lines": source_lines,
    }
    if now is not None:
        values["created_at"] = now
        values["updated_at"] = now
    row = conn.execute(items.insert().values(**values).returning(items)).first()
    assert row is not None
    # Same transaction as the INSERT: a refused tag rolls the item back too.
    tag_names = set_item_tags_conn(
        conn, row.id, tags if tags is not None else [], force=force_tags,
    )
    d = _item_to_dict(row, tag_names)
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
    tag: str | None = None,
    gate: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Priority-ordered item page.

    `gate` (#947) keeps items with a PENDING gate checkpoint, sprint or not:
    'any' = any gate kind, else that kind (trimmed, case-insensitive). A
    blank value or a non-gate kind (task / cc-task) raises ValueError.

    `tag` filters on an ACTIVE item_tags link (closed dictionary — an unknown
    name raises ValueError naming the known tags). Tags themselves come from a
    SECOND query over the materialized page (1 + ceil(n/500) queries), never
    from a join: joining here would fan out the checkpoint aggregates.
    `limit=0` means no limit (existing semantics).
    """
    # Priority ordering: P1 first, future last
    priority_order = _priority_order_expr()

    # Checkpoint progress aggregated in a single query — avoids N+1.
    # cp_done = count of done, cp_total = done + pending (rejected excluded from "real" total).
    cp_done, cp_total = _cp_progress_exprs()

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
    if gate is not None:
        q = q.where(_pending_gate_exists(gate))
    if limit:
        q = q.limit(limit)

    with engine.begin() as conn:
        if project_slug:
            pid = resolve_project_id(conn, project_slug)
            q = q.where(items.c.project_id == pid)
        if tag:
            q = q.where(_has_tag_exists(tag_id_for_name_conn(conn, tag)))
        rows = [r._mapping for r in conn.execute(q)]
        tags_by_item = tags_for_items_conn(conn, [r["id"] for r in rows])
        return [_item_with_cp_to_dict(r, tags_by_item.get(r["id"])) for r in rows]


def get_item(engine: sa.Engine, item_id: int) -> dict | None:
    from dct.core.checkpoints import get_progress
    from dct.db import item_checkpoints

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(items, _current_sprint_exists().label("in_current_sprint"))
            .where(items.c.id == item_id, items.c.deleted_at.is_(None))
        ).first()
        if not row:
            return None
        d = _item_to_dict(row, tags_for_items_conn(conn, [item_id]).get(item_id))
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


def _gate_literal(value: str) -> str:
    """`value` as a one-line JSON string: quotes, backslashes and control
    characters stay escaped data (K-62). json.dumps escapes U+0000–U+001F;
    the rest of Cc (DEL, C1 such as U+0085 NEL) and the line / paragraph
    separators U+2028 / U+2029 — line breaks to str.splitlines and to many
    renderers — are escaped here too. Other Unicode stays readable."""
    return "".join(
        f"\\u{ord(ch):04x}" if unicodedata.category(ch) in {"Cc", "Zl", "Zp"} else ch
        for ch in json.dumps(value, ensure_ascii=False)
    )


def _gate_entry(gate: dict) -> str:
    """One refusal entry: `#<id> [<kind>] "<text>"`. The id comes from the
    record; a kind that is a plain token is shown bare, any other kind as a
    JSON string; the text is whitespace-collapsed, previewed and always a
    JSON string — so neither field can pose as another entry."""
    kind = gate["kind"]
    if not _BARE_KIND.fullmatch(kind):
        kind = _gate_literal(preview(kind, GATE_REFUSAL_TEXT_CHARS))
    text = _gate_literal(preview(" ".join(gate["text"].split()), GATE_REFUSAL_TEXT_CHARS))
    return f"#{gate['id']} [{kind}] {text}"


def _refuse_pending_gates_conn(conn: sa.Connection, item_id: int) -> None:
    """The ONE gate guard of every terminal transition — resolve_item and
    update_item(status='resolved'|'wont_fix') alike, so no client (MCP, CLI,
    core) can close an item past a pending gate. Raises ValueError while any
    gate checkpoint of the item is pending; never rejects a gate itself.

    The message — the ONE refusal text MCP and CLI both surface — starts
    "Cannot resolve item #N: K gate checkpoint(s) pending", names the first
    GATE_REFUSAL_LISTED gates in sort order as one-line entries
    (_gate_entry), ends the list with "(+M more — get_item(N) or `dct show N`
    lists them all)" when gates are left out, and says how to handle them in
    client-neutral terms: complete a gate only once its recorded condition is
    met — a person's act (dogfood, approval) only on that person's
    confirmation — reject one only on the user's decision, and never re-ask a
    consent already given. It names no repo-only file (K-22).

    It runs on the caller's connection, inside the transaction that writes
    the status: the refusal rolls back everything the call would write
    (fields, tags, reason note) and no separate transaction opens a gap
    between the check and the write."""
    from dct.core.checkpoints import pending_gates_conn

    pending_gates = pending_gates_conn(conn, item_id)
    if pending_gates:
        listed = pending_gates[:GATE_REFUSAL_LISTED]
        gate_summary = "; ".join(_gate_entry(g) for g in listed)
        rest = len(pending_gates) - len(listed)
        more = (
            f" (+{rest} more — get_item({item_id}) or `dct show {item_id}` lists them all)"
            if rest else ""
        )
        raise ValueError(
            f"Cannot resolve item #{item_id}: {len(pending_gates)} gate checkpoint(s) "
            f"pending — {gate_summary}{more}. Handle each gate by its recorded "
            f"condition: complete it (MCP complete_checkpoint / CLI "
            f"`dct checkpoint done <id>`) only once that condition is met — a gate "
            f"whose condition is a person's act (dogfood: the user's own check in a "
            f"fresh session; approval: a stakeholder's sign-off) only on that "
            f"person's confirmation, which finished implementation, green tests or "
            f"an instruction to close the item do not replace; reject one that "
            f"consciously does not apply (MCP reject_checkpoint / CLI "
            f"`dct checkpoint reject <id>`) only on the user's decision. A consent "
            f"the user already gave explicitly is not asked again."
        )


def update_item(engine: sa.Engine, item_id: int, **kwargs) -> dict | None:
    """Update the given fields of item `item_id` → the full item dict, or None
    for an unknown / soft-deleted id. `reason` (optional, non-blank) is the
    WHY of the change: appended as an item note in the SAME transaction as
    the update, so a refused update (invalid field, refused tag, unknown
    item) leaves no orphan note and a reason alone is no update (ValueError,
    like any call without a field to change).

    A terminal `status` (resolved / wont_fix) runs the same gate guard as
    resolve_item (_refuse_pending_gates_conn) in the update's transaction:
    with a gate checkpoint pending the whole call is refused (ValueError) —
    no field, tag or note is written."""
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

    # Tags are no longer a column (#611) — pull them out of kwargs as a
    # SENTINEL (K-16): absent AND explicit None both mean "leave tags alone",
    # only an explicit list (including []) replaces them.
    tags = kwargs.pop("tags", None)
    force_tags = kwargs.pop("force_tags", False)
    reason = kwargs.pop("reason", None)
    if reason is not None and not reason.strip():
        raise ValueError("reason cannot be empty")

    allowed = {"title", "short_title", "description", "priority", "status", "component", "source_file", "source_lines", "resolution", "item_type"}
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
        elif k == "title":
            _validate_title(v)
        if k == "short_title":
            # K-43: an explicit value (even all-blank) is normalized; an
            # empty result clears the label. Omitting the key keeps it.
            values[k] = normalize_short_title(v)
            continue
        if v == "" and k in {
            "component", "source_file", "source_lines", "resolution", "description",
        }:
            v = None
        values[k] = v

    if not values and tags is None:
        raise ValueError("No valid fields to update")
    ensure_fits(items, values, _ITEM_LABELS)

    now = datetime.now(UTC)
    values["updated_at"] = now

    # Invariant: terminal status ↔ resolved_at set. Sprint MEMBERSHIP is kept
    # on terminal transitions (K-18, #413) — the derived in_current_sprint flag
    # filters terminal items out, and retained links are what make
    # sprint_completion computable (resolved members / all members).
    if "status" in values:
        if values["status"] in TERMINAL_STATUSES:
            values.setdefault("resolved_at", now)
        elif values["status"] in OPEN_STATUSES:
            values["resolved_at"] = None

    with engine.begin() as conn:
        # K-11: existence is checked BEFORE any tag resolution, so a typo'd id
        # can never register tags in the dictionary as a side effect. The
        # check is the item row lock (#910): a gate writer commits either
        # before this transaction reads the gates or after it wrote the status.
        if not lock_item_row(conn, item_id):
            return None
        # Gate guard BEFORE any write, in this transaction: a refusal raises
        # out of the `begin()` block, so nothing of this call is committed.
        if values.get("status") in TERMINAL_STATUSES:
            _refuse_pending_gates_conn(conn, item_id)
        # K-37: the UPDATE repeats the soft-delete guard. The SELECT above is a
        # separate statement, so without it a concurrent delete_item between the
        # two would let this write resurrect a soft-deleted row.
        result = conn.execute(
            items.update()
            .where(items.c.id == item_id, items.c.deleted_at.is_(None))
            .values(**values)
        )
        if result.rowcount == 0:
            return None
        if tags is not None:
            set_item_tags_conn(conn, item_id, tags, force=force_tags, now=now)
        if reason is not None:
            conn.execute(item_notes.insert().values(item_id=item_id, note=reason))

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
    The check (_refuse_pending_gates_conn — shared with update_item's
    terminal transitions) runs in the transaction that writes the status.

    A missing or soft-deleted item returns None before the gate check (the
    answer update_item gives), so it never draws a gate refusal.

    Dogfood gate rationale: user-facing features (e.g. a new screen,
    command, endpoint, skill or MCP tool) carry a `kind='dogfood'` checkpoint
    as their last one — this enforces that the feature was validated by the
    user in a fresh session, not just self-reviewed by the implementer. The full policy (gate kinds,
    the dogfood rule and its exceptions) ships in the add_checkpoint MCP
    tool description (dct/mcp/server.py).
    """
    _validate_enum(status, TERMINAL_STATUSES, "resolve status")

    now = datetime.now(UTC)
    values: dict = {
        "status": status,
        "resolved_at": now,
        "updated_at": now,
    }
    if resolution:
        values["resolution"] = resolution

    with engine.begin() as conn:
        # K-63: a missing or soft-deleted item is "not found" (None) BEFORE the
        # gate guard — the answer update_item gives — so a deleted item never
        # draws a "handle its gates" refusal and nothing is written for it.
        # The check is the item row lock (#910), shared with every gate writer.
        if not lock_item_row(conn, item_id):
            return None
        _refuse_pending_gates_conn(conn, item_id)
        # The UPDATE keeps the soft-delete guard (K-37): the SELECT above is a
        # separate statement.
        result = conn.execute(items.update().where(items.c.id == item_id, items.c.deleted_at.is_(None)).values(**values))
        if result.rowcount == 0:
            return None
        # Sprint membership is intentionally KEPT (K-18, #413): the derived
        # in_current_sprint flag hides terminal items from current-work views,
        # while the retained link lets sprint_completion count this item.

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
            .values(deleted_at=datetime.now(UTC))
        )
        return result.rowcount > 0
