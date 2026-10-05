"""MCP server for dct (SDK 2.0 MCPServer) — exposes tracking operations as MCP tools.

Response format conventions:
- list_* tools return {"cols": [...], "rows": [[...]]} — a compact table.
  `cols` carries full field names once; `rows` contains value arrays in the
  same order. Short/fixed-width fields come first; long free-text (title,
  description, entry, prompt) comes last so row-wise scanning stays legible.
- get_* tools return the full semantic dict for a single record.
- format="markdown" (opt-in) returns a human-readable table (lossy).
- format="dict" (opt-in) returns a list of full-key dicts.

Every tool returns JSON-native (dict / list / str). No double-encoded JSON.
"""

import contextlib
from datetime import datetime

from mcp.server import MCPServer

from dct.core.engine import bootstrap_engine

# ONE preview implementation for the whole product (K-35, #413): the MCP list
# views and the snapshot share dct.core.text._preview (re-exported by
# dct.workflows.session), so the documented "200-char preview" means the same
# ceiling everywhere — 200 chars INCLUDING the trailing ellipsis.
from dct.core.text import _preview

_INSTRUCTIONS = (
        "Deterministic Context Tracker — cross-project work tracking.\n\n"
        "RESOURCES (all soft-delete ledgers, never hard-deleted):\n"
        "- items: features/todos/issues/ideas/improvements (open/in_progress/resolved/wont_fix)\n"
        "- checkpoints: acceptance criteria on items (pending/done/rejected)\n"
        "- notes: append-only; changelog_entries: shipped work, versioned via\n"
        "  release_changelog; session_handoffs: cross-session prompts.\n\n"
        "RESPONSE FORMAT:\n"
        "- Write ops return a minimal ack (id + a few post-state scalars), NOT the full\n"
        "  row. For full context fetch it: get_item(id) (notes, checkpoints, progress),\n"
        "  get_handoff(id) (untruncated prompt + context_summary).\n"
        "- list_* return {cols:[...], rows:[[...]]}: cols names the fields once, rows are\n"
        "  value arrays in that order (short fields first, long free-text last). Legends —\n"
        "  item: id, item_type, priority, status, in_current_sprint, cp=[done,total],\n"
        "  component, tags (list[str]), created_at, short_title (2-4 word label, may be\n"
        "  null), title — list_items(view='full') appends description; changelog: id, version,\n"
        "  category, component, related_item_id, created_at, released_at, entry;\n"
        "  checkpoint: id, status, sort_order, text; handoff: id, scope, created_by,\n"
        "  created_at, consumed_at, prompt (200-char preview). format='markdown' (lossy)\n"
        "  or 'dict' (full-key dicts) opt in.\n\n"
        "PROJECT RESOLUTION (any `project` param): '<slug>' = that project; '' = CWD\n"
        "auto-detect; '' with an unregistered CWD = list_* tools (list_items,\n"
        "list_handoffs) span ALL projects, while project-required tools — every write\n"
        "op AND review_sprint — error. Pass an explicit slug when unsure.\n\n"
        "ID NORMALIZATION: '#42', '42', 'ITEM-42', '0042' all mean item_id=42.\n\n"
        "TAGS: one GLOBAL (cross-project) dictionary of normalized names —\n"
        "list_items(tag='oss') filters on it (unknown name = error naming the closest\n"
        "known tags), update_item(tags=[...]) FULL-REPLACES an item's tags (omit =\n"
        "unchanged, [] = clear). Dictionary admin (deferred): add_tag (a name confusable\n"
        "with an existing one is refused — reuse it or re-call with force=true; a\n"
        "non-empty description updates an existing tag), list_tags,\n"
        "merge_tag(from_name, into_name).\n\n"
        "WORKFLOW:\n"
        "1. list_items / get_item to find/inspect; an item's checkpoints are your TODO\n"
        "   list (sort_order). REMEMBER ids from create_item/add_checkpoint/create_handoff.\n"
        "2. For feature/improvement items sketch 3-7 checkpoints BEFORE coding.\n"
        "3. complete_checkpoint(id) as each step finishes (real-time, NOT batched);\n"
        "   reject_checkpoint irrelevant ones. Then resolve_item (no auto-transition).\n\n"
        "SPRINT & HANDOFFS:\n"
        "- set_sprint(project, item_ids='22,21,6') atomically replaces the sprint;\n"
        "  resolve_item clears the DERIVED in_current_sprint flag — membership itself\n"
        "  is RETAINED as the sprint's completion history.\n"
        "- Several sprints may be active in parallel; current-sprint WRITES then refuse\n"
        "  to guess and error listing the candidates — target one via assign_item_to_sprint\n"
        "  / handoff_work(sprint=<id or code>).\n"
        "- Lifecycle planned|active|deferred|completed|abandoned via update_sprint: park\n"
        "  unfinished as 'deferred' (hidden from current-sprint views, started_at kept),\n"
        "  reactivate 'active'; never complete/abandon an unfinished one.\n"
        "  review_sprint = session dashboard.\n"
        "- create_handoff(prompt, created_by, scope?) notes next session (multiple\n"
        "  unconsumed ok); consume_handoff(id) marks read, preserving the row.\n\n"
        "WORKFLOW OPS (composed, one transaction each — start here):\n"
        "- Session start: pickup_work(consumed_by=...) = snapshot + consume the handoff\n"
        "  (full prompt) in one call. Session end with unfinished work:\n"
        "  handoff_work(prompt, created_by, item_ids=...) = sprint + handoff atomically.\n"
        "- Mid-session \"where do we stand?\": get_context_snapshot(include=...).\n"
        "  The granular tools above remain for surgical operations.\n\n"
        "TOOL-CALL HYGIENE: pass every argument as its own top-level parameter — never\n"
        "nest one prose field in another. Free-text tools reject literal tool-call markup;\n"
        "a rejection means re-send with the fields split out."
)

# Passive update notice (#356): append a one-line "newer release available" hint
# from the cached PyPI probe when present. Guarded so a missing module / cache /
# config can NEVER break server start; this path does ZERO network I/O (pure cache
# read via get_cached_notice).
with contextlib.suppress(Exception):
    from dct.update import get_cached_notice

    _update_notice = get_cached_notice()
    if _update_notice:
        _INSTRUCTIONS += f"\n\nUPDATE: {_update_notice}"

mcp = MCPServer("dct", instructions=_INSTRUCTIONS)

# ── Frugal MCP deferral exemption (#241) ──────────────────────────────────────
# Claude Code defers ALL MCP tools by default (ENABLE_TOOL_SEARCH unset), so even
# /pickup pays a ToolSearch round-trip before it can run. We exempt only the
# daily-driver tools — the ones the pickup / track* / clog / commit / handoff
# skills reach for every session — via the `anthropic/alwaysLoad` _meta hint the
# CC client reads (requires Claude Code v2.1.121+), passed as the SDK 2.0
# official `meta=` decorator param (K-47). Specialist tools (plans, decisions,
# first-class sprints, web, release admin) stay deferred.
# Add a name ONLY if a routine skill needs it from token zero; keep the long
# tail deferred to spend session context wisely. Takes effect on server restart.
# The hot-set is the mid-session daily drivers — the tools a working session
# touches turn after turn (item lifecycle, checkpoints, changelog add) — PLUS the
# composed workflow ENTRY POINTS (#413): they ARE the session edge, and there the
# ToolSearch round-trip was 100% of pre-work latency (nothing else is loaded yet
# when /pickup starts). The GRANULAR session-edge tools they compose
# (review_sprint, set_sprint, list_handoffs, create_handoff, consume_handoff,
# list_projects, count_unreleased_changelog) stay deferred BEHIND them: once the
# entry point covers the routine path, reaching for a granular one is the rare
# surgical case and can afford its one round-trip.
# CONTRACT: _ALWAYS_LOAD_TOOLS is the spec, the decorators' meta=_ALWAYS_LOAD_META
# is the mechanism, and tests/test_server_alwaysload.py binds the two — the wire
# alwaysLoad set must equal this frozenset exactly.
_ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}
_ALWAYS_LOAD_TOOLS = frozenset({
    # item + checkpoint lifecycle (track / track-list / track-update / track-resolve)
    "create_item", "get_item", "list_items", "update_item", "resolve_item",
    "add_note", "add_checkpoint", "complete_checkpoint", "reject_checkpoint",
    # changelog add (clog / commit)
    "add_changelog",
    # workflow ops (pickup / handoff / snapshot) — #413
    "get_context_snapshot", "pickup_work", "handoff_work",
})

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = bootstrap_engine(create=True)
    return _engine


def _resolve_project(project_arg: str, required: bool = True) -> str | None:
    """Auto-detect project from CWD when project_arg is empty.

    Each CC session spawns its own MCP subprocess inheriting the session's CWD,
    so `os.getcwd()` identifies the active project reliably.
    """
    if project_arg:
        return project_arg
    import os

    from dct.core.projects import get_project_by_path

    cwd = os.getcwd()
    p = get_project_by_path(_get_engine(), cwd)
    if p:
        return p["slug"]
    if required:
        raise ValueError(
            f"No project specified and CWD ({cwd}) is not a registered project. "
            f"Pass project=<slug> or register with: dct project add <slug> <path>"
        )
    return None


# ── Tool-call markup guard (#240) ─────────────────────────────────────────────
# The model occasionally mis-serializes a tool call — drops the `antml:` prefix
# or nests a <parameter> tag inside another field's value. The harness parser
# then recognises only the well-formed parameters and folds the siblings into
# ONE free-text field as literal markup, so the structured fields arrive empty.
# Persisting that yields silently-corrupted rows (e.g. a decision whose
# decision/consequences/alternatives are NULL). We catch it at the MCP boundary
# and reject with a self-correcting message, so the model re-issues the call
# cleanly and nothing bad is stored. CLI args and plan-ingest markdown never
# produce this markup, so the guard is intentionally MCP-only (not in core).
_TOOLCALL_MARKERS = (
    "<function_calls>",
    "</function_calls>",
    "<parameter>",
    "<parameter name=",
    "</parameter>",
    "<invoke name=",
    "</invoke>",
    "antml:",
)


def _reject_toolcall_markup(**fields) -> dict | None:
    """Return an {"error": ...} dict if any string field carries literal
    tool-call markup, else None.

    The presence of these tokens means the call was mangled in transit: sibling
    parameters collapsed into one field as raw text. Returning the error makes
    the tool fail loud (no row written) and tells the model how to retry.
    Non-string / empty values are skipped. Matching is case-insensitive.
    Bare function_calls wrappers are rejected too, even without nested
    invocation or parameter tags (#427).

    List values (K-13: `tags` on create_item / update_item) are inspected
    ELEMENT BY ELEMENT — a mangled call can collapse siblings into one array
    entry just as easily as into a scalar field — and the offending element is
    reported as 'field[i]'.
    """
    flat: list[tuple[str, object]] = []
    for name, value in fields.items():
        if isinstance(value, list | tuple):
            flat.extend((f"{name}[{i}]", element) for i, element in enumerate(value))
        else:
            flat.append((name, value))
    for name, value in flat:
        if not isinstance(value, str) or not value:
            continue
        low = value.lower()
        for marker in _TOOLCALL_MARKERS:
            if marker in low:
                return {"error": (
                    f"Malformed MCP tool call: field '{name}' contains literal "
                    f"tool-call markup '{marker}'. Sibling parameters collapsed into "
                    f"one field as raw text, so structured fields were lost — nothing "
                    f"was saved. Re-issue the call passing each field as its own "
                    f"separate top-level parameter (with the antml: prefix); never "
                    f"nest parameter tags inside another field's value."
                )}
    return None


# ── Slim-ack helpers (#351) ───────────────────────────────────────────────────
# Write-op MCP tools return a compact acknowledgement (ids + scalar headers)
# instead of the full core dict. The calling model rarely needs the free-text it
# just sent echoed back (description / prompt / entry / rationale / section
# bodies), and the slim shape spends session context wisely. Core functions still
# return FULL dicts (CLI + web depend on them) — slimming happens ONLY at this
# MCP boundary. get_item stays full by design (single-record inspect).


def _slim_progress(progress: dict) -> dict:
    """Project a full checkpoints.get_progress dict onto the ack triple
    {done, pending, active}. `active` already excludes rejected (same source as
    get_item.progress) — we only SELECT three keys here, never recompute."""
    return {k: progress[k] for k in ("done", "pending", "active")}


def _item_progress_ack(engine, item_id: int) -> dict:
    """Slim progress ack for an item, via the shared core computation."""
    from dct.core.checkpoints import get_progress

    return _slim_progress(get_progress(engine, item_id))


def _item_title(engine, item_id: int) -> str | None:
    """Cheap single-column lookup of an item's title (for checkpoint/note acks)."""
    import sqlalchemy as sa

    from dct.db import items

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(items.c.title).where(
                items.c.id == item_id, items.c.deleted_at.is_(None)
            )
        ).first()
    return row[0] if row else None


# ── Format helpers ────────────────────────────────────────────────────────────


def _serialize_value(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _to_cols_rows(rows: list[dict], cols: list[str]) -> dict:
    """Render list of dicts as compact {cols, rows} table."""
    return {
        "cols": cols,
        "rows": [[_serialize_value(r.get(c)) for c in cols] for r in rows],
    }


def _to_markdown(rows: list[dict], cols: list[str]) -> str:
    header = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    if not rows:
        return header + "\n" + sep + "\n_(empty)_"
    lines = [header, sep]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, datetime):
                v = v.isoformat()
            if v is None:
                v = ""
            cells.append(str(v).replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _format_list(rows: list[dict], cols: list[str], format: str):
    if format == "markdown":
        return _to_markdown(rows, cols)
    if format == "dict":
        return rows
    return _to_cols_rows(rows, cols)


# Column sets — short/fixed-width fields first, long free-text last.
# list_items defaults to the BRIEF view (#817): descriptions dominate the
# payload of a large project's backlog, so they are opt-in via view='full'.
ITEM_COLS_BRIEF = [
    "id", "item_type", "priority", "status", "in_current_sprint",
    "cp", "component", "tags", "created_at", "short_title", "title",
]
ITEM_COLS_FULL = ITEM_COLS_BRIEF + ["description"]
ITEM_VIEWS = {"brief": ITEM_COLS_BRIEF, "full": ITEM_COLS_FULL}
CHANGELOG_COLS = [
    "id", "version", "category", "component", "related_item_id",
    "created_at", "released_at", "entry",
]
CHECKPOINT_COLS = ["id", "status", "sort_order", "text"]
TAG_COLS = ["id", "name", "item_count", "created_at", "description"]
PROJECT_COLS = ["id", "slug", "current_version", "name", "path", "changelog_path"]
HANDOFF_COLS = [
    "id", "scope", "created_by", "created_at", "consumed_at", "prompt",
]


def _parse_bool_tri(val: str) -> bool | None:
    """Tri-state bool for MCP string params.

    Empty string → None (no filter / no change).
    Truthy tokens ('true', '1', 'yes') → True.
    Anything else → False.

    Accept multiple tokens because LLM callers vary by training regime.
    Docstrings surfacing this param should list the canonical tokens
    ('true' / 'false') — the function is permissive but the contract is narrow.
    """
    if not val:
        return None
    return val.lower() in ("true", "1", "yes")


# ── Project tools ─────────────────────────────────────────────────────────────


@mcp.tool(structured_output=False)
def list_projects(format: str = "structured"):
    """List all registered projects. No filtering; no CWD auto-detection —
    this is a discovery tool, not a scope-narrowing one. Use
    `get_project_by_path` (via CLI) or CWD prefix matching if you need the
    current project's slug.

    Args:
        format: "structured" (default, {cols,rows}) | "markdown" | "dict".

    Returns: table of all non-deleted projects with {id, slug, current_version,
        name, path, changelog_path}, shaped per `format`. changelog_path (#225)
        is the changelog file location relative to the project root (default
        'changelog.md'); /release resolves {path}/{changelog_path}.
    """
    from dct.core.projects import list_projects as _list
    rows = _list(_get_engine())
    return _format_list(rows, PROJECT_COLS, format)


# ── Item tools ────────────────────────────────────────────────────────────────


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def create_item(
    item_type: str,
    title: str,
    project: str = "",
    short_title: str = "",
    description: str = "",
    priority: str = "P2",
    component: str = "",
    source_file: str = "",
    source_lines: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Create a tracked item (issue / todo / feature / idea / improvement).

    REMEMBER the returned `id` — needed for add_checkpoint / update_item /
    resolve_item / add_note. For feature/improvement items, sketch 3-7
    checkpoints with add_checkpoint before starting work.

    TYPE SELECTION (pick from the user's phrasing; when in doubt use `todo`):
      - issue: defect in existing code — bug, crash, error, broken, regression
      - todo: generic work that is neither a bug nor a new capability
      - feature: new user-visible capability that doesn't exist yet ("add X")
      - idea: exploratory, not yet decided — "maybe", "what if", "consider"
      - improvement: existing thing works but could be better — refactor, polish

    Args:
        item_type: One of: issue / todo / feature / idea / improvement.
        title: Short title — hard limit 500 chars (≤ 80 recommended);
            longer titles are refused, never truncated.
        project: Project slug. Empty = auto-detect from CWD.
        short_title: 2-4 word English noun phrase naming what the item is
            about (e.g. 'Windows hook paths'); ≤ 50 chars, refused not
            truncated; always pass it.
            Shown next to the item number in every list and snapshot.
        description: Prose context (no checkbox lists — use add_checkpoint instead).
        priority: P1 (blocker) / P2 (important, default) / P3 (backlog) / future.
        component: Component or module name (≤ 100 chars).
        source_file: Relative file path (≤ 500 chars).
        source_lines: Line range (e.g. "42-58", ≤ 100 chars).
        tags: List of tag strings. Pass as a JSON array, e.g. ["oss","tests"].
            Names are normalized (trimmed, lowercased) and registered in the
            GLOBAL tag dictionary. A name confusable with an existing tag is
            REFUSED and the whole call rolls back (no orphan item) — reuse the
            suggested name, or register yours first with add_tag(force=true).

    Returns: slim ack {id, project, item_type, priority, status, title,
        short_title} — `project` is the resolved slug (auto-detect safety echo);
        `short_title` echoes the STORED label (null when none was passed) — a
        missing key means the server predates the field and ignored it. NOT the full
        item dict (no description/tags echo); call get_item for the rest.
        {"error": "..."} on invalid enum, markup, a refused tag name, an
        over-long field (refused, never truncated), or an unresolvable project.
    """
    if (err := _reject_toolcall_markup(
        title=title, short_title=short_title, description=description, tags=tags
    )):
        return err
    from dct.core.items import create_item as _create

    kwargs: dict = {}
    if short_title: kwargs["short_title"] = short_title
    if description: kwargs["description"] = description
    if component: kwargs["component"] = component
    if source_file: kwargs["source_file"] = source_file
    if source_lines: kwargs["source_lines"] = source_lines
    if tags: kwargs["tags"] = tags

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        d = _create(_get_engine(), project_slug, item_type, title, priority=priority, **kwargs)
        return {
            "id": d["id"], "project": project_slug, "item_type": d["item_type"],
            "priority": d["priority"], "status": d["status"], "title": d["title"],
            "short_title": d["short_title"],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def list_items(
    project: str = "",
    status: str = "open",
    item_type: str = "",
    priority: str = "",
    component: str = "",
    in_current_sprint: str = "",
    search: str = "",
    tag: str = "",
    limit: int = 50,
    format: str = "structured",
    view: str = "brief",
):
    """List tracked items in compact {cols,rows} form.

    Each row carries a `cp` field with [done, total] checkpoint progress
    computed in a single SQL query (no N+1), a `tags` list[str] (name-ASC) and
    `short_title` (the item's 2-4 word label, may be null).

    VIEWS — default 'brief' because descriptions dominate the payload of a
    large project's backlog; call get_item(id) or view='full' for them:
      - 'brief' (default): id, item_type, priority, status, in_current_sprint,
        cp, component, tags, created_at, short_title, title.
      - 'full': the brief columns + description.

    Args:
        project: slug; empty = CWD auto-detect, unregistered CWD = cross-project
            — see server instructions.
        status: Filter by status. Default 'open'. Use '' for all statuses.
        item_type: issue / todo / feature / idea / improvement.
        priority: P1 / P2 / P3 / future.
        component: Filter by component name.
        in_current_sprint: '' (any, default), 'true' (only sprint), 'false' (non-sprint).
        search: Substring in title + description — matched server-side in
            EVERY view, so a brief row may match on a description it omits.
        tag: ONE tag name (normalized — trimmed, lowercased); keeps only items
            carrying it. The dictionary is CLOSED: an unknown name returns
            {"error": ...} naming the closest known tags (up to 3, plus a count
            of the rest — call list_tags for the full dictionary), never a
            silently empty page.
            Composes with `project` (the dictionary is global, item lists stay
            project-scoped). Use list_tags to see what exists.
        limit: Max rows (default 50). 0 = no limit.
        format: 'structured' (default, {cols,rows}), 'markdown', 'dict'.
        view: 'brief' (default) | 'full' — see VIEWS. With format='dict',
            'brief' projects each dict onto the brief columns; 'full' returns
            the full item dicts (project_id, source_*, resolution, timestamps…).

    Returns: {"cols": [...], "rows": [[...]]} by default, list[dict] for
        format="dict", or markdown str for format="markdown".
        {"error": "..."} on an unknown tag or an unknown view.
    """
    from dct.core.items import list_items as _list

    cols = ITEM_VIEWS.get(view)
    if cols is None:
        return {"error": f"Unknown view {view!r} — valid: {', '.join(ITEM_VIEWS)}"}
    kwargs: dict = {"limit": limit}
    detected = _resolve_project(project, required=False)
    if detected: kwargs["project_slug"] = detected
    if status: kwargs["status"] = status
    if item_type: kwargs["item_type"] = item_type
    if priority: kwargs["priority"] = priority
    if component: kwargs["component"] = component
    if search: kwargs["search"] = search
    if tag: kwargs["tag"] = tag
    sprint_filter = _parse_bool_tri(in_current_sprint)
    if sprint_filter is not None:
        kwargs["in_current_sprint"] = sprint_filter

    try:
        rows = _list(_get_engine(), **kwargs)
        if format == "dict" and view == "brief":
            rows = [{c: r.get(c) for c in cols} for r in rows]
        return _format_list(rows, cols, format)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def get_item(item_id: int) -> dict:
    """Get full item details — fields, notes, checkpoints, progress.

    CALL THIS FIRST when resuming work — the checkpoints list is your TODO plan
    (pending = to do, done = skip, rejected = abandoned).

    Returns a full semantic dict (not cols/rows) — single record is legible.
    """
    from dct.core.items import get_item as _get

    result = _get(_get_engine(), item_id)
    if result is None:
        return {"error": f"Item #{item_id} not found"}
    return result


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def update_item(
    item_id: int,
    title: str = "",
    short_title: str = "",
    status: str = "",
    priority: str = "",
    component: str = "",
    resolution: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Update one or more mutable fields on an existing item. Only non-empty
    args are applied; an empty/omitted field is left unchanged.

    Terminal status (resolved/wont_fix) sets resolved_at AND clears the DERIVED
    in_current_sprint flag — the sprint MEMBERSHIP is retained as the sprint's
    completion history; reopening (open/in_progress) clears resolved_at. This path does
    NOT run the gate-checkpoint guard — prefer resolve_item for terminal moves.
    Sprint membership is not set here (`in_current_sprint` is read-only) — use
    set_sprint / assign_item_to_sprint / remove_item_from_sprint.

    Args:
        item_id: Target item.
        title: New title (non-empty, ≤ 500 chars — ≤ 80 recommended; blank or
            '__clear__' is refused).
        short_title: 2-4 word English noun phrase naming what the item is
            about (e.g. 'Windows hook paths'); ≤ 50 chars, refused not
            truncated; always pass it.
            '__clear__' nulls it.
        status: open / in_progress / resolved / wont_fix.
        priority: P1 / P2 / P3 / future.
        component: Component or module name (≤ 100 chars).
        resolution: Resolution / rationale note.
        description: Prose context.
        tags: FULL REPLACE of the item's tag set — the list you pass becomes the
            item's tags exactly. Omit (or null) = tags unchanged; [] = clear all
            tags. Names are normalized (trimmed, lowercased) against the GLOBAL
            dictionary; a name confusable with an existing tag is REFUSED with
            {"error": ...} naming the suggestion — reuse it, or register yours
            first via add_tag(name=..., force=true) and repeat this call.
            A refused tag name rolls back the ENTIRE update — no field is
            applied, not even the non-tag ones; re-issue the WHOLE call once
            the tag is resolved.

    Clearing a field — pass literal '__clear__' to null a NULLABLE field
    (short_title, component, resolution, description). NOT for title/status/priority: all
    three are refused (title cannot be empty; status/priority fail enum
    validation). Tags use
    their own convention above ([], not '__clear__').

    Returns: slim POST-state ack {id, item_type, status, priority, title,
        short_title, component, in_current_sprint, resolved_at, progress} —
        scalars reflect the row AFTER the update (a '__clear__'ed component
        shows null here; a missing short_title key means the server predates
        the field and ignored it);
        progress is {done, pending, active}. NOT the full item dict (no
        notes/checkpoints/tags/description echo) — call get_item to read the
        resulting tag list back. {"error": "..."} on unknown id, a refused tag
        name, a blank title, an over-long field (refused, never truncated), or
        markup.
    """
    if (err := _reject_toolcall_markup(
        title=title, short_title=short_title, description=description,
        resolution=resolution, tags=tags,
    )):
        return err
    from dct.core.items import update_item as _update

    kwargs: dict = {}
    for field, value in [
        ("title", title), ("short_title", short_title), ("status", status),
        ("priority", priority), ("component", component),
        ("resolution", resolution), ("description", description),
    ]:
        if value == "__clear__":
            kwargs[field] = ""
        elif value:
            kwargs[field] = value
    # Sentinel (K-16): only an explicit list reaches core — None means "leave
    # the tags alone", so it must NOT be passed through as a kwarg.
    if tags is not None:
        kwargs["tags"] = tags

    try:
        result = _update(_get_engine(), item_id, **kwargs)
        if not result:
            return {"error": f"Item #{item_id} not found"}
        return {
            "id": result["id"], "item_type": result["item_type"],
            "status": result["status"], "priority": result["priority"],
            "title": result["title"], "short_title": result["short_title"],
            "component": result["component"],
            "in_current_sprint": result["in_current_sprint"],
            "resolved_at": result["resolved_at"],
            "progress": _slim_progress(result["progress"]),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def resolve_item(item_id: int, status: str = "resolved", resolution: str = "") -> dict:
    """Resolve or close an item (terminal status). Auto-clears in_current_sprint
    and stamps resolved_at.

    GATE GUARD: if the item has any pending gate checkpoint (kind not in
    {'task','cc-task'} — e.g. 'dogfood', 'security', 'approval', 'review'), the
    transition is REFUSED and the tool returns
    {"error": "Cannot resolve item #N: ... gate checkpoint(s) pending ..."}.
    Complete (done) or reject (not applicable) those gates first.

    PREFER THIS OVER update_item FOR TERMINAL TRANSITIONS — explicit intent, and
    it runs the gate guard above. update_item(status="resolved") also stamps
    resolved_at and clears the sprint, but SKIPS the gate check.

    Args:
        item_id: Item ID.
        status: resolved (default) or wont_fix. Any other value is rejected.
        resolution: Free-text resolution note (recommended — feeds audit trail).

    Returns: slim ack {id, status, resolved_at, progress} — progress is
        {done, pending, active}. NOT the full item dict. {"error": "..."} when
        the item is missing or a pending gate blocks the transition (the gate
        message is byte-identical to before).
    """
    from dct.core.items import resolve_item as _resolve
    try:
        result = _resolve(_get_engine(), item_id, status=status, resolution=resolution or None)
        if not result:
            return {"error": f"Item #{item_id} not found"}
        return {
            "id": result["id"], "status": result["status"],
            "resolved_at": result["resolved_at"],
            "progress": _slim_progress(result["progress"]),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def add_note(item_id: int, note: str) -> dict:
    """Append a note to an item (ledger — never overwritten).

    Use for: discoveries while working, decisions, blockers, "fixed in commit X".
    Do NOT use for: TODO steps (use add_checkpoint), status changes that have
    their own rationale field (use update_item resolution=).

    /track-update treats status/priority transitions as mandatory note points —
    the audit trail is what lets /pickup explain "why #42 moved to P1".

    Returns: slim ack {id, item_id, item_title} — `id` is the new note's id,
        `item_title` the parent item's title. NOT the note body echo.
        {"error": "..."} if the item does not exist or the note is empty/markup.
    """
    if (err := _reject_toolcall_markup(note=note)):
        return err
    from dct.core.items import add_note as _add_note
    try:
        engine = _get_engine()
        n = _add_note(engine, item_id, note)
        return {"id": n["id"], "item_id": n["item_id"], "item_title": _item_title(engine, n["item_id"])}
    except Exception as e:
        return {"error": str(e)}


# ── Tag tools ─────────────────────────────────────────────────────────────────
# Deferred by design (no _ALWAYS_LOAD_META): the hot-set stays the 13 daily
# drivers. Tagging itself rides on create_item / update_item / list_items, which
# are already loaded; these three are the dictionary-admin surface.


@mcp.tool(structured_output=False)
def add_tag(name: str, description: str = "", force: bool = False) -> dict:
    """Register a tag in the GLOBAL (user-scoped, cross-project) dictionary.

    Idempotent: an existing active name is REUSED (never duplicated) and comes
    back with created=false, and a non-empty `description` is applied to that
    existing row on reuse. A soft-deleted name is resurrected as the SAME
    dictionary row (stable id): links that were never re-pointed become active
    again, but links an earlier merge_tag moved onto another tag STAY on that
    target — resurrection does not undo a merge.

    FRAGMENTATION GUARD: a name confusable with an existing one ('test' next to
    'tests') is refused with {"error": ...} naming the suggestion. The guard is
    advisory — repeat the call with force=true when the new name is genuinely
    distinct. An EXACT existing name is reused regardless of `force`.

    Args:
        name: Tag name. Normalized before storage (whitespace collapsed,
            lowercased); rejected when empty, containing a comma (the CLI CSV
            separator), or longer than 50 chars after normalization.
        description: Optional prose describing what the tag is for.
        force: True bypasses the similarity guard only.

    Returns: slim ack {id, name, created} — `name` is the NORMALIZED form,
        `created` is False when an active row was reused. {"error": "..."} on an
        invalid name, a refused similar name, or markup.
    """
    if (err := _reject_toolcall_markup(name=name, description=description)):
        return err
    from dct.core.tags import add_tag as _add

    try:
        return _add(_get_engine(), name, description=description or None, force=force)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_tags(format: str = "structured"):
    """List the whole tag dictionary, name-ASC, with usage counts.

    NO `project` param: the dictionary is GLOBAL, and so are the counts —
    `item_count` spans every project (item LISTS stay project-scoped; the
    asymmetry is deliberate). Counts exclude soft-removed links and soft-deleted
    items; an unused tag is listed with item_count=0.

    Args:
        format: 'structured' (default, {cols,rows}), 'markdown', 'dict'.

    Returns: {"cols": ["id","name","item_count","created_at","description"],
        "rows": [[...]]} by default, list[dict] for format="dict", or a markdown
        table for format="markdown".
    """
    from dct.core.tags import list_tags as _list

    try:
        return _format_list(_list(_get_engine()), TAG_COLS, format)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def merge_tag(from_name: str, into_name: str) -> dict:
    """Consolidate two tags: move every ACTIVE link of `from_name` onto
    `into_name`, then retire the source row (soft delete).

    The action to take on a similarity suggestion. Ledger-preserving: historical
    (already removed) links of the source keep their own timestamps, and an item
    that carried BOTH tags ends with exactly one active link. One-way by design —
    add_tag can resurrect the source NAME later, but the moved links stay on the
    target.

    Args:
        from_name: Source tag, retired by this call (named `from_name` because
            `from` is a Python keyword). Must be an ACTIVE tag.
        into_name: Target tag that absorbs the links. Must be an ACTIVE tag.

    Returns: slim ack {"from", "into", "items_moved"} — normalized names plus
        the number of items relinked. {"error": "..."} when either name is
        unknown/retired (the message names the known tags), when both normalize
        to the SAME name (self-merge), or on markup.
    """
    if (err := _reject_toolcall_markup(from_name=from_name, into_name=into_name)):
        return err
    from dct.core.tags import merge_tags as _merge

    try:
        return _merge(_get_engine(), from_name, into_name)
    except Exception as e:
        return {"error": str(e)}


# ── Checkpoint tools ──────────────────────────────────────────────────────────


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def add_checkpoint(item_id: int, text: str, kind: str = "task") -> dict:
    """Add an acceptance criterion (checkpoint) to an item.

    USE LIBERALLY — one checkpoint per concrete, actionable step.
    For features/improvements, sketch 3-7 checkpoints BEFORE starting work.

    REMEMBER the returned `id` — needed for complete_checkpoint / reject_checkpoint.

    Args:
        item_id: Target item.
        text: Concrete, actionable step description.
        kind: Checkpoint class. Default 'task' (non-blocking). Gate kinds that
              BLOCK resolve_item until done/rejected:
                'dogfood'  — user-facing feature needs fresh-session validation
                'security' — security review required before release
                'approval' — external stakeholder sign-off
                'review'   — code review / PR approval
              'cc-task' — mirrored Claude Code TaskCreate entry (non-blocking,
              cleaned up on /handoff).

              User-facing features (new skill, CLI command, MCP tool) MUST
              include a final `kind='dogfood'` checkpoint. See CLAUDE.md
              "Gate checkpoints" section.

    Returns: slim ack {id, item_id, item_title, kind, status, sort_order} —
        `item_title` is the parent item's title. NOT the checkpoint text echo.
        {"error": "..."} on empty text or an unknown item_id.
    """
    from dct.core.checkpoints import add_checkpoint as _add
    try:
        engine = _get_engine()
        cp = _add(engine, item_id, text, kind=kind)
        return {
            "id": cp["id"], "item_id": cp["item_id"],
            "item_title": _item_title(engine, cp["item_id"]),
            "kind": cp["kind"], "status": cp["status"], "sort_order": cp["sort_order"],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def complete_checkpoint(checkpoint_id: int) -> dict:
    """Mark a checkpoint as done (status 'done').

    CRITICAL: call IMMEDIATELY after the step completes. Do NOT batch completions
    at the end of a task — real-time updates give the user live progress visibility.
    Completing a pending gate checkpoint (dogfood/security/approval/review)
    satisfies it and clears that block on resolve_item.

    Returns: slim ack {id, item_id, status, progress} — progress is the parent
        item's {done, pending, active} after the transition. NOT the checkpoint
        text echo. {"error": "Checkpoint #<id> not found"} when the id is
        unknown or already deleted.
    """
    from dct.core.checkpoints import set_status
    try:
        engine = _get_engine()
        result = set_status(engine, checkpoint_id, "done")
        if not result:
            return {"error": f"Checkpoint #{checkpoint_id} not found"}
        return {
            "id": result["id"], "item_id": result["item_id"],
            "status": result["status"],
            "progress": _item_progress_ack(engine, result["item_id"]),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def reject_checkpoint(checkpoint_id: int) -> dict:
    """Mark a checkpoint as rejected (status 'rejected') — ledger-preserving
    alternative to delete.

    Use when scope changes or an initial assumption turns out wrong. Rejected
    checkpoints drop out of the `active` count, so they no longer weigh on the
    done/active progress percent. For a gate checkpoint (kind dogfood/security/
    approval/review) rejecting is the sanctioned "not applicable" skip that
    unblocks resolve_item.

    Returns: slim ack {id, item_id, status, progress} — progress is the parent
        item's {done, pending, active} after the rejection (which drops out of
        `active`). NOT the checkpoint text echo. {"error": "Checkpoint #<id>
        not found"} when the id is unknown or already deleted.
    """
    from dct.core.checkpoints import set_status
    try:
        engine = _get_engine()
        result = set_status(engine, checkpoint_id, "rejected")
        if not result:
            return {"error": f"Checkpoint #{checkpoint_id} not found"}
        return {
            "id": result["id"], "item_id": result["item_id"],
            "status": result["status"],
            "progress": _item_progress_ack(engine, result["item_id"]),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def reopen_checkpoint(checkpoint_id: int) -> dict:
    """Revert a checkpoint back to pending (status 'pending'), typically from done
    or rejected. Re-opening a previously rejected gate makes it pending again and
    re-blocks resolve_item.

    Returns: slim ack {id, item_id, status, progress} — matches its
        complete/reject_checkpoint siblings; progress is the parent item's
        {done, pending, active} after re-opening. {"error": "Checkpoint #<id>
        not found"} when the id is unknown or already deleted.
    """
    from dct.core.checkpoints import set_status
    try:
        engine = _get_engine()
        result = set_status(engine, checkpoint_id, "pending")
        if not result:
            return {"error": f"Checkpoint #{checkpoint_id} not found"}
        return {
            "id": result["id"], "item_id": result["item_id"],
            "status": result["status"],
            "progress": _item_progress_ack(engine, result["item_id"]),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_checkpoints(
    item_id: int,
    include_rejected: bool = False,
    format: str = "structured",
):
    """List checkpoints for an item in sort_order.

    Defaults hide rejected checkpoints — the typical caller wants the active
    TODO list. Set include_rejected=True for full audit view. get_item(id)
    always returns all checkpoints regardless of status (single-item inspect).

    Args:
        item_id: parent item id.
        include_rejected: False (default) hides rejected; True returns all.
        format: 'structured' (default, {cols,rows}) | 'markdown' | 'dict'.

    Returns: {cols, rows} by default — cols = [id, status, sort_order, text].
    """
    from dct.core.checkpoints import list_checkpoints as _list
    try:
        rows = _list(_get_engine(), item_id, include_rejected=include_rejected)
        return _format_list(rows, CHECKPOINT_COLS, format)
    except Exception as e:
        return {"error": str(e)}


# ── Changelog tools ───────────────────────────────────────────────────────────


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def add_changelog(
    category: str,
    entry: str,
    project: str = "",
    component: str = "",
    source_file: str = "",
    source_lines: str = "",
    related_item_id: int = 0,
) -> dict:
    """Add an unreleased changelog entry (version stays NULL until release_changelog stamps it).

    Each field below is a SEPARATE top-level parameter.

    Args:
        category: One of added / changed / fixed / removed (rejected otherwise).
        entry: The changelog line — required, non-empty. Rendered as a "- ..." bullet on export.
        project: Slug. Empty = auto-detect from CWD.
        component: Optional area/module tag; appended as " (component)" to the bullet on export.
        source_file: Optional relative path of the file the change touched.
        source_lines: Optional line range (e.g. "42-58").
        related_item_id: Tracked-item id to link (0 = none).

    Returns: slim ack {id, project, category, component} — `project` is the
        resolved slug, `component` may be null. NOT the entry text echo (nor the
        full row). {"error": "..."} on failure — empty/whitespace entry, invalid
        category, literal tool-call markup in `entry`, or an unresolvable project
        (explicit slug not found, or empty `project` with an unregistered CWD).
    """
    if (err := _reject_toolcall_markup(entry=entry)):
        return err
    from dct.core.changelog import add_entry

    kwargs: dict = {}
    if component: kwargs["component"] = component
    if source_file: kwargs["source_file"] = source_file
    if source_lines: kwargs["source_lines"] = source_lines
    if related_item_id: kwargs["related_item_id"] = related_item_id

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        row = add_entry(_get_engine(), project_slug, category, entry, **kwargs)
        return {"id": row["id"], "project": project_slug, "category": row["category"], "component": row["component"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_changelog(
    project: str = "",
    unreleased_only: bool = True,
    format: str = "structured",
):
    """List changelog entries for a project.

    Args:
        project: Slug. Empty = CWD auto-detect.
        unreleased_only: True (default) → only entries without a version; False → all.
        format: 'structured' (default, {cols,rows}) | 'markdown' | 'dict'.

    Returns: {cols, rows} by default, list[dict] for 'dict', markdown str for 'markdown'.
    """
    from dct.core.changelog import list_entries
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        rows = list_entries(_get_engine(), project_slug, unreleased_only=unreleased_only)
        return _format_list(rows, CHANGELOG_COLS, format)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def count_unreleased_changelog(project: str = "") -> dict:
    """Count unreleased changelog entries (version=NULL) for a project.

    Args:
        project: Slug. Empty = auto-detect from CWD.

    Returns: {"count": <int>} on success, {"error": "..."} on failure.
    Typical use: gate before /release (stop if count == 0).
    """
    from dct.core.changelog import count_unreleased
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return {"count": count_unreleased(_get_engine(), project_slug)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def release_changelog(project: str = "", version: str = "", bump_type: str = "") -> dict:
    """Stamp every unreleased entry (version=NULL) with a version AND set the
    project's current_version to that version.

    Provide either an explicit version OR a bump_type — explicit version wins.

    Args:
        project: Slug. Empty = auto-detect from CWD.
        version: Explicit version like "0.2.0". Takes precedence over bump_type.
        bump_type: patch / minor / major — bumped from the project's current_version.

    Side effect: also stamps released_at on every entry it versions, and writes
    current_version onto the project row. current_version is bumped even when
    there are zero unreleased entries (released = 0).

    Returns: {"released": <count>, "version": "<version>", "project": "<slug>"}
        on success (released = number of changelog entries stamped; the project
        row update is not counted). On failure returns {"error": "..."}, e.g.:
        neither version nor bump_type given; bump_type set but the project has no
        current_version to bump from; an invalid bump_type (not patch/minor/major);
        or the project can't be resolved (empty project + unregistered CWD, or an
        unknown slug).
    """
    from dct.core.changelog import release_entries
    from dct.core.projects import get_project
    from dct.core.version import bump

    try:
        engine = _get_engine()
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        if not version:
            if not bump_type:
                return {"error": "Provide version or bump_type (patch/minor/major)"}
            proj = get_project(engine, project_slug)
            if not proj or not proj.get("current_version"):
                return {"error": f"No current version for '{project_slug}'. Provide explicit version."}
            version = bump(proj["current_version"], bump_type)

        count = release_entries(engine, project_slug, version)
        return {"released": count, "version": version, "project": project_slug}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def export_changelog(project: str = "", include_unreleased: bool = True, version: str = ""):
    """Export changelog as Keep a Changelog markdown.

    For /release commits, call with include_unreleased=False so the
    committed changelog.md contains only released versions (DB stays the
    source of truth for unreleased).

    For the append-only /release flow, pass version="X.Y.Z" to render ONLY
    that version's section (no `# Changelog` header). The skill prepends that
    section into the existing changelog.md instead of regenerating the whole
    file — regeneration drops pre-adoption history for projects that adopted
    dct mid-life (item #214). An unknown version renders to "".

    Args:
        project: Slug. Empty = CWD auto-detect.
        include_unreleased: If True (default), include [Unreleased] section at
            top. Ignored when `version` is set (single-version render never
            includes Unreleased).
        version: When non-empty, render only that version's `## [version]`
            section, headerless, for the append-only prepend flow.

    Returns: markdown str on success, {"error": "..."} dict on failure —
        aligned with every other MCP tool in this server. Callers should
        `isinstance(result, dict)` check before writing to file.
    """
    from dct.export import export_changelog_md
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return export_changelog_md(
            _get_engine(), project_slug,
            include_unreleased=include_unreleased, version=version or None,
        )
    except Exception as e:
        return {"error": str(e)}


# ── Sprint tools ──────────────────────────────────────────────────────────────


@mcp.tool(structured_output=False)
def set_sprint(project: str = "", item_ids: str = "") -> dict:
    """Atomically replace the current sprint's membership with the given ids.

    The current sprint is the project's PRIMARY active sprint — the most
    recently started one; several may be active in parallel (auto-created if
    none exists). Membership lives in item_sprints — assigns the listed ids and
    soft-removes the rest. STRICT: ids that aren't live items of this project
    (foreign, unknown, or soft-deleted) return {"error": ...} listing them and
    the sprint stays unchanged. **There is no "no-op" call — `item_ids=""`
    WIPES the sprint** (equivalent to `clear_sprint`). If you want to keep the
    current sprint, don't call this tool.

    Args:
        project: Slug. Empty = auto-detect from CWD.
        item_ids: Comma-separated ids (e.g. "22,21,6"). Empty string clears sprint.

    Returns: {"project": "<slug>", "open_size": <int>, "items": [ids, sorted]}
        — open_size/items describe the OPEN membership after the swap
        (terminal members are retained as history and are NOT listed; the
        snapshot's active_sprints[].size counts open + terminal instead).
        Error: {"error": "..."} on project resolution failure, on ids that are
        not live items of this project (unknown / foreign / soft-deleted — the
        message lists them, nothing is written), or when SEVERAL sprints are
        active: a current-sprint write refuses to guess and names the
        candidates — use assign_item_to_sprint / remove_item_from_sprint for an
        explicit target.
    """
    from dct.core.sprint import set_sprint as _set

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        ids = [int(x.strip()) for x in item_ids.split(",") if x.strip()] if item_ids else []
        return _set(_get_engine(), project_slug, ids)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def clear_sprint(project: str = "") -> dict:
    """Unflag every item from the project's current sprint (fresh planning).

    Soft-removes every member of the active sprint; the sprint row itself stays
    active and empty. No-op (cleared=0) when the project has no current sprint.

    Args:
        project: Slug. Empty = auto-detect from CWD (NOT all-projects).

    Returns: {"project": "<slug>", "cleared": <int>} — count of memberships
        soft-removed. Error: {"error": "..."} on project resolution failure.
    """
    from dct.core.sprint import clear_sprint as _clear
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return _clear(_get_engine(), project_slug)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def review_sprint(project: str = "", since: str = "last_release") -> dict:
    """Session start/end dashboard — in_sprint, recent_activity, stale_in_progress, unconsumed_handoffs.

    Structured data only; interpretation lives in the consuming skill (/pickup,
    /handoff), so it iterates without a schema change.

    Args:
        project: slug; empty = CWD auto-detect. Requires a resolvable project:
            unlike list_* tools, an empty project with an unregistered CWD does
            NOT span all projects — it returns {"error": ...}. See server
            instructions.
        since: "last_release" (default; anchor = max(released_at), falls back to
            all-time when no releases) | "all" (no cutoff) | "<N>h" (hours from
            now, hour suffix only) | ISO date/datetime ("2026-04-10" or
            "2026-04-10T14:00", trailing "Z" ok).

    Returns: dashboard dict with keys project, since, since_anchor (human
        label), in_sprint, recent_activity, stale_in_progress,
        unconsumed_handoffs, summary. in_sprint / stale_in_progress /
        unconsumed_handoffs are {cols, rows} tables; recent_activity is a dict
        {items_changed: {cols,rows}, checkpoints_completed: int,
        changelog_added: int}; summary is a scalar dict. Every item table
        (in_sprint, items_changed, stale_in_progress) carries a `short_title`
        column (the item's label, may be null) before `title`. NOTE the two sprint
        numbers measure different sets: `in_sprint` lists only the OPEN members
        (current work) while `summary.sprint_size` / `sprint_completion` count
        ALL members including terminal ones (resolved work stays linked as the
        sprint's history, ADR #57) — expect len(in_sprint.rows) <= sprint_size.
        Error: {"error": "..."} on invalid `since`.
    """
    from dct.core.sprint import get_sprint_review
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return get_sprint_review(_get_engine(), project_slug, since=since)
    except Exception as e:
        return {"error": str(e)}


# ── Handoff tools ─────────────────────────────────────────────────────────────


@mcp.tool(structured_output=False)
def create_handoff(
    prompt: str,
    created_by: str,
    project: str = "",
    scope: str = "",
    context_summary: str = "",
) -> dict:
    """Leave a handoff draft for the next session.

    Multiple unconsumed handoffs per project are allowed (parallel sessions,
    different scopes). Use `scope` to disambiguate.

    Pass `prompt` as a single top-level parameter — a call carrying literal
    tool-call markup (a stray <parameter> tag) is rejected, not stored (#240).

    Args:
        prompt: Free-form "for next session: ..." text. Must be non-empty.
        created_by: REQUIRED, non-empty, ≤ 200 chars — self-identify (e.g.
            "claude-opus-4-7 @ 2026-04-17T14:30Z").
        project: Empty = auto-detect from CWD.
        scope: Optional tag like "sprint-v0.2" or "refactor-auth" (≤ 100 chars).
        context_summary: Optional snapshot of state at handoff time.

    Over-long created_by/scope is refused, never truncated.

    Returns: slim ack {id, project, scope, created_at} — `project` is the
        resolved slug, `scope` may be null. NO prompt/context_summary echo.
        Error cases: {"error": "..."} when prompt is empty, created_by is empty,
        created_by/scope exceeds its limit, or project slug does not resolve.
    """
    if (err := _reject_toolcall_markup(prompt=prompt, context_summary=context_summary)):
        return err
    from dct.core.handoff import create_handoff as _create

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        h = _create(
            _get_engine(), project_slug,
            prompt=prompt, created_by=created_by,
            scope=scope or None,
            context_summary=context_summary or None,
        )
        return {"id": h["id"], "project": project_slug, "scope": h["scope"], "created_at": h["created_at"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_handoffs(
    project: str = "",
    unconsumed_only: bool = True,
    format: str = "structured",
):
    """List session handoffs for a project.

    The `prompt` column is a PREVIEW — truncated to 200 chars with a trailing
    '…' when longer. Fetch the full prompt (plus context_summary and the rest
    of the row) with get_handoff(id).

    Args:
        project: slug; empty = CWD auto-detect, unregistered CWD = cross-project
            — see server instructions.
        unconsumed_only: If True (default) returns only handoffs without a
            consumed_at stamp — the "waiting for me" set. False returns all.
        format: 'structured' (default, {cols,rows}), 'markdown', 'dict'.

    Returns: {"cols": [...], "rows": [[...]]} by default, list[dict] for
        format="dict", or markdown str for format="markdown". In every format
        the prompt is the 200-char preview (see get_handoff for full text).
    """
    from dct.core.handoff import list_handoffs as _list
    try:
        project_slug = _resolve_project(project, required=False)
        rows = _list(_get_engine(), project_slug, unconsumed_only=unconsumed_only)
        for r in rows:
            r["prompt"] = _preview(r.get("prompt"))
        return _format_list(rows, HANDOFF_COLS, format)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def get_handoff(handoff_id: int) -> dict:
    """Fetch one handoff in full — the untruncated companion to list_handoffs.

    Args:
        handoff_id: id from list_handoffs / create_handoff.

    Returns: the full semantic dict (prompt, context_summary, scope, created_by,
        created_at, consumed_by, consumed_at, and the rest of the row) with the
        prompt NOT truncated. {"error": "Handoff #N not found"} if the id is
        unknown or soft-deleted.
    """
    from dct.core.handoff import get_handoff as _get
    try:
        result = _get(_get_engine(), handoff_id)
        if not result:
            return {"error": f"Handoff #{handoff_id} not found"}
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def consume_handoff(handoff_id: int, consumed_by: str = "") -> dict:
    """Mark a handoff as consumed. Preserves the row in the ledger.

    Args:
        handoff_id: id returned by create_handoff / list_handoffs.
        consumed_by: optional, ≤ 200 chars (refused, never truncated) —
            self-identify the consuming session (mirrors create_handoff's
            `created_by` format).

    Returns: slim ack {id, consumed_at} — the consumed timestamp only. NO
        prompt echo. {"error": "..."} if the handoff is missing / already
        consumed, or consumed_by exceeds 200 chars (nothing is written).
    """
    from dct.core.handoff import consume_handoff as _consume
    try:
        result = _consume(_get_engine(), handoff_id, consumed_by=consumed_by or None)
        if not result:
            return {"error": f"Handoff #{handoff_id} not found or already consumed"}
        return {"id": result["id"], "consumed_at": result["consumed_at"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def delete_handoff(handoff_id: int) -> dict:
    """Soft-delete a handoff (hide from lists). Preserves the row for audit.

    Returns: {"deleted": <bool>, "id": <int>}. `deleted=False` when no matching
        row existed or it was already deleted. Error payload {"error": "..."}
        on unexpected failures.
    """
    from dct.core.handoff import delete_handoff as _delete
    try:
        ok = _delete(_get_engine(), handoff_id)
        return {"deleted": ok, "id": handoff_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def update_handoff(
    handoff_id: int,
    prompt: str = "",
    scope: str = "",
    context_summary: str = "",
) -> dict:
    """Amend an UNCONSUMED handoff in place — preserves id + created_at.

    The in-place alternative to delete_handoff + create_handoff (supersede):
    refresh a stale draft without churning its id, so the next /pickup sees the
    latest text under the same handoff. Only non-empty args are applied; omit a
    field to leave it unchanged (you cannot blank a field through this tool).

    Consumed or soft-deleted handoffs are immutable ledger entries — updating
    one returns an error (the audit trail of what a session was handed is fixed
    once it is read).

    Args:
        handoff_id: id returned by create_handoff / list_handoffs.
        prompt: New prompt text (omit/empty = unchanged; cannot be blanked).
        scope: New scope tag, ≤ 100 chars — refused, never truncated
            (omit/empty = unchanged).
        context_summary: New context snapshot (omit/empty = unchanged).

    Returns: slim ack {id, updated_fields} — `updated_fields` lists the applied
        field names (subset of prompt / scope / context_summary). NO prompt
        echo. {"error": "..."} if the handoff is missing / consumed / deleted,
        if no field was supplied, or scope exceeds 100 chars (nothing is
        written).
    """
    if (err := _reject_toolcall_markup(prompt=prompt, context_summary=context_summary)):
        return err
    from dct.core.handoff import update_handoff as _update
    applied = [
        name for name, val in
        (("prompt", prompt), ("scope", scope), ("context_summary", context_summary))
        if val
    ]
    try:
        result = _update(
            _get_engine(), handoff_id,
            prompt=prompt or None,
            scope=scope or None,
            context_summary=context_summary or None,
        )
        if not result:
            return {"error": f"Handoff #{handoff_id} not found, consumed, or deleted"}
        return {"id": result["id"], "updated_fields": applied}
    except Exception as e:
        return {"error": str(e)}


# ── Workflow ops (#413) ───────────────────────────────────────────────────────
# Composed session-edge entry points: ONE call in place of the 4-8 granular ones
# a /pickup or /handoff used to chain. The implementation lives in
# dct/workflows/session.py (single transaction each) and is shared with the CLI
# and the packaged skills — these tools only marshal MCP strings into it.


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def get_context_snapshot(
    project: str = "",
    include: str = "",
    since: str = "last_release",
) -> dict:
    """One composed read of a project's state — sprint + handoff + gates + decisions + recent changes.

    PREFER THIS OVER review_sprint for session-state reads — one composed call
    covers sprint + handoff + gates + decisions + recent changes, in a SINGLE
    transaction, so every section describes the same instant. review_sprint
    remains the narrower legacy dashboard.

    Structured data only; interpretation lives in the calling skill.

    Args:
        project: Slug. Empty = auto-detect from CWD. Requires a resolvable
            project — an unregistered CWD errors, it does NOT span projects.
        include: Comma-separated section names to keep ('sprint,gates'). Empty
            (default) = ALL sections. The single sentinel 'none' = zero sections
            (summary-only, the cheap confirm call). Valid names: sprint,
            handoff, gates, decisions, recent_changes; an unknown name errors.
        since: Anchor for recent_changes — "last_release" (default; max
            released_at, all-time when the project has no release) | "all" |
            "<N>h" (hours) | ISO date/datetime ("2026-04-10T14:00", "Z" ok).

    Returns: dict ALWAYS carrying project, since, since_anchor, sections (the
        resolved list) and summary (active_sprint_count, sprint_size,
        sprint_completion, total_open_in_project, handoffs_pending,
        gates_pending, stale_count). Requested sections are added as keys and
        SKIPPED ONES ARE OMITTED (probe with `"gates" in result` — never None):
        sprint {primary_sprint_id, active_sprints, items, checkpoints,
        stale_in_progress}, handoff {latest (FULL prompt), others (200-char
        previews), pending_count}, gates / decisions ({cols,rows}),
        recent_changes {items_changed, checkpoints_completed, changelog_added}.
        LABELS: rows that ARE an item (sprint.items, sprint.stale_in_progress,
        recent_changes.items_changed) carry `short_title` (may be null); rows
        that REFERENCE an item (gates, sprint.checkpoints) carry
        `item_short_title` = the item's short_title, else its title compacted
        to 50 chars (never null for a titled item). COUNTING: an item in TWO active sprints counts once per sprint in
        active_sprints[].size/done, but only ONCE (DISTINCT) in
        summary.sprint_size — the per-sprint sizes are not expected to sum to
        it. CAPS: sprint.items (100), sprint.checkpoints (500),
        sprint.stale_in_progress (50), gates (50) and handoff.others (20) are
        row-capped and each carries "truncated": true/false; the summary
        counters are computed over the FULL sets, and handoff.latest is never
        truncated. Every value is JSON-native (timestamps are ISO 8601
        strings). {"error": "..."} on an unknown section name, 'none' combined
        with other sections, an invalid `since`, or an unresolvable project.
    """
    from dct.workflows.session import get_context_snapshot as _snapshot
    from dct.workflows.session import parse_sections
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return _snapshot(
            _get_engine(), project_slug,
            include=parse_sections(include),
            since=since,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def pickup_work(
    project: str = "",
    consume: str = "",
    consumed_by: str = "",
    handoff_id: int = 0,
    scope: str = "",
    since: str = "last_release",
    include: str = "",
) -> dict:
    """Session-start op: context snapshot + (possibly) consume ONE handoff, atomically.

    Prefer over review_sprint + list_handoffs + get_handoff + consume_handoff
    (4-8 calls → 1-2). The picked-up handoff comes back with its FULL prompt, so
    no follow-up get_handoff is needed.

    Args:
        project: Slug. Empty = auto-detect from CWD (must resolve).
        consume: STRICT enum — no other token is accepted (a typo errors, it
            never silently downgrades). '' (default) = AUTO: consume
            `handoff_id` when given, otherwise consume ONLY when exactly ONE
            unconsumed handoff matches (after the scope filter) — several
            candidates mean nothing is consumed, they ride back in the handoff
            section and you re-call with the chosen handoff_id. 'true' = consume
            `handoff_id` or the newest match; errors when there is nothing to
            consume. 'false' = pure read (contradictory with handoff_id).
        consumed_by: REQUIRED whenever consumption is possible (consume ''
            or 'true') — self-identify like create_handoff's created_by
            ("claude-opus-4-7 @ 2026-04-17T14:30Z"). Empty errors; > 200
            chars is refused (never truncated) when a consume happens.
        handoff_id: Consume this exact handoff (0 = none given). WINS over
            scope, which is then ignored EVERYWHERE — candidate selection AND
            the returned handoff.latest. An id from another project reads as
            "not found or already consumed" — no cross-project consume.
        scope: Narrow candidates to one workstream tag (empty = any; stored
            tags are ≤ 100 chars); also makes the returned handoff section
            scope-coherent and adds handoff.scope_matched.
        since: recent_changes anchor — same vocabulary as get_context_snapshot.
        include: Comma-separated sections to keep; empty = all; 'none' = none.

    Returns: the get_context_snapshot dict (same sections, same caps and
        "truncated" flags, same JSON-native values) — POST-consume, so the
        handoff section and summary.handoffs_pending already exclude what was
        just picked up — plus picked_up (the FULL consumed handoff row, or
        null) and consumed (bool). With a `scope` (and no handoff_id) the
        handoff section also carries scope_matched: how many unconsumed
        handoffs of that scope remain AFTER the write, so 0 ("nothing for this
        workstream") is distinguishable from "several pending, pick one"; the
        key is absent when no scope was given. {"error": "..."} on an invalid consume token (nothing
        is read or written), empty consumed_by while consumption is possible,
        a > 200-char consumed_by on a consume (nothing is written),
        handoff_id with consume='false', an unknown/already-consumed handoff_id,
        consume='true' with no candidate, an unknown section, or an unresolvable
        project.
    """
    if (err := _reject_toolcall_markup(consumed_by=consumed_by, scope=scope)):
        return err
    consume_tokens = {"": None, "true": True, "false": False}
    if consume not in consume_tokens:
        return {"error": f"consume must be '' (auto), 'true' or 'false' — got '{consume}'"}
    from dct.workflows.session import parse_sections
    from dct.workflows.session import pickup_work as _pickup
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return _pickup(
            _get_engine(), project_slug,
            consume=consume_tokens[consume],
            handoff_id=handoff_id or None,
            scope=scope or None,
            consumed_by=consumed_by or None,
            since=since,
            include=parse_sections(include),
        )
    except Exception as e:
        return {"error": str(e)}


def _parse_id_csv(value: str, field: str) -> list[int]:
    """Comma-separated ids → ints. Empty/delimiter-only input raises — a
    blank shell variable must never read as 'no items' (K-27, #413)."""
    try:
        ids = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError:
        raise ValueError(
            f"{field} must be comma-separated integers ('22,21,6') — got '{value}'"
        )
    if not ids:
        raise ValueError(
            f"{field} parsed to an empty list (got '{value}') — omit the "
            f"parameter to skip it; emptying a sprint is clear_sprint's job."
        )
    return ids


@mcp.tool(structured_output=False, meta=_ALWAYS_LOAD_META)
def handoff_work(
    prompt: str,
    created_by: str,
    project: str = "",
    item_ids: str = "__keep__",
    add_item_ids: str = "",
    remove_item_ids: str = "",
    sprint: str = "",
    scope: str = "",
    context_summary: str = "",
) -> dict:
    """Session-end op: sprint membership changes + the handoff in ONE transaction.

    Prefer over set_sprint + create_handoff — atomic, one call: a handoff
    validation failure rolls every membership write back. Sprints are
    MULTI-SESSION (ADR #57): the default session edge keeps membership and,
    when needed, ADDS newly discovered items / CUTS scope — full replace is a
    conscious replanning act.

    Args:
        prompt: Free-form "for next session: ..." text (required, non-empty).
        created_by: REQUIRED, non-empty, ≤ 200 chars — self-identify (e.g.
            "claude-opus-4-7 @ 2026-04-17T14:30Z").
        item_ids: REPLACE mode. '__keep__' (DEFAULT) = do NOT touch the sprint;
            '22,21,6' = replace the sprint's OPEN membership with exactly those
            ids (STRICT — unknown/foreign/soft-deleted ids error and roll the
            whole call back; terminal members stay as the sprint's completion
            history). '' or a delimiter-only string errors — emptying a sprint
            is clear_sprint's job. Mutually exclusive with add/remove below.
        add_item_ids: ADDITIVE mode — comma-separated ids to pull INTO the
            target sprint (STRICT validation; empty string = not used).
        remove_item_ids: ADDITIVE mode — ids to cut from the sprint's OPEN
            scope. Removing a terminal (resolved/wont_fix) member errors —
            that is history, not scope. Empty string = not used.
        project: Slug. Empty = auto-detect from CWD.
        sprint: Target among the ACTIVE sprints, by id or code. Id match takes
            precedence; an id↔code collision between two sprints errors. Given
            without any membership change → error (a typo must not silently
            no-op). Empty = the only active sprint, or auto-created when none
            is active (replace/add paths); several active and no selector →
            error listing the candidates.
        scope: Optional workstream tag ("sprint-v0.2", ≤ 100 chars) —
            pickup_work filters on it.
        context_summary: Optional state snapshot stored next to the prompt.

    Over-long created_by/scope is refused, never truncated, and rolls the
    membership writes back.

    Returns: slim ack {handoff_id, project, scope, created_at, sprint} — `sprint`
        is {sprint_id, code, open_size, items} (OPEN membership after the
        writes; snapshot's active_sprints[].size counts open + terminal
        history instead) or
        null (null ⇔ no membership change requested). NO prompt /
        context_summary echo (fetch with get_handoff). {"error": "..."} on
        empty prompt or created_by, created_by/scope over its limit,
        malformed/empty id lists, replace combined
        with add/remove, unknown/foreign item ids, removing a terminal member,
        an ambiguous or non-active sprint selector, a selector without a
        membership change, literal tool-call markup in a free-text field, or
        an unresolvable project.
    """
    if (err := _reject_toolcall_markup(
        prompt=prompt, context_summary=context_summary,
        scope=scope, created_by=created_by,
    )):
        return err
    from dct.workflows.session import handoff_work as _handoff
    try:
        ids: list[int] | None = None
        if item_ids != "__keep__":
            ids = _parse_id_csv(item_ids, "item_ids")
        add_ids = _parse_id_csv(add_item_ids, "add_item_ids") if add_item_ids.strip() else None
        remove_ids = (
            _parse_id_csv(remove_item_ids, "remove_item_ids")
            if remove_item_ids.strip() else None
        )
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        out = _handoff(
            _get_engine(), project_slug,
            prompt=prompt, created_by=created_by,
            item_ids=ids,
            add_item_ids=add_ids,
            remove_item_ids=remove_ids,
            sprint=sprint or None,
            scope=scope or None,
            context_summary=context_summary or None,
        )
        h = out["handoff"]
        return {
            "handoff_id": h["id"], "project": out["project"], "scope": h["scope"],
            "created_at": h["created_at"], "sprint": out["sprint"],
        }
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — plans / plan_sections / plan_checkpoints ════════════════════════

@mcp.tool(structured_output=False)
def create_plan(
    slug: str,
    title: str,
    kind: str,
    project: str = "",
    source_file: str = "",
    sprint_id: int = 0,
) -> dict:
    """Register a plan row programmatically (e.g. for a DB-only roadmap).

    For plans whose source of truth is a markdown file, prefer /dct-import or
    the autoscan hook: those inject the ULID anchor back into the file and
    record source_hash. This tool does neither — it only generates a DB-side
    ULID and stores source_file verbatim (source_hash stays NULL).

    Args:
        slug: Short handle, unique per project (required, non-empty).
        title: Plan title (required, non-empty).
        kind: plan | spec | roadmap | adr | retro | backlog.
        project: Slug. Empty = auto-detect from CWD.
        source_file: Optional relative path (empty = DB-only); stored verbatim.
        sprint_id: Optional sprint FK (0 = none).

    Returns: slim ack {id, ulid, slug, kind, status, title} — scalar headers
        (status defaults to 'active'). NOT the full plan row. {"error": "..."}
        on invalid kind, empty slug/title, duplicate slug in the project, or an
        unresolvable project (bad slug / unregistered CWD).
    """
    from dct.core.plans.crud import create_plan as _create
    try:
        p = _create(
            _get_engine(),
            _resolve_project(project),
            slug=slug, title=title, kind=kind,
            source_file=source_file or None,
            sprint_id=sprint_id or None,
        )
        return {"id": p["id"], "ulid": p["ulid"], "slug": p["slug"], "kind": p["kind"], "status": p["status"], "title": p["title"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_plans(
    project: str = "",
    kind: str = "",
    status: str = "",
    format: str = "structured",
) -> dict | list | str:
    """List plans in a project, with optional filters.

    Args:
        project: Slug. Empty = auto-detect from CWD; falls back to ALL
            projects when CWD is not a registered project.
        kind: plan | spec | roadmap | adr | retro | backlog (empty = all).
        status: draft | active | completed | archived (empty = all).
        format: 'structured' (cols/rows) | 'dict' (list of full dicts).

    Returns: {cols, rows} by default — cols are id, ulid, kind, status,
    slug, title. Rows are flat plan records (no nested sections — use
    get_plan for sections + checkpoints).
    """
    from dct.core.plans.crud import list_plans as _list
    project_slug = _resolve_project(project, required=False)
    try:
        rows = _list(
            _get_engine(), project_slug=project_slug,
            kind=kind or None, status=status or None,
        )
    except Exception as e:
        return {"error": str(e)}
    if format == "dict":
        return rows
    cols = ["id", "ulid", "kind", "status", "slug", "title"]
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


@mcp.tool(structured_output=False)
def get_plan(plan_id: int) -> dict:
    """Full plan dict with nested sections + checkpoints + progress rollup.

    Progress: {total, done, rejected, pending, active, percent}. `active =
    total - rejected`, `percent = 100 * done / active`.
    """
    from dct.core.plans.crud import get_plan as _get
    row = _get(_get_engine(), plan_id)
    return row or {"error": f"Plan #{plan_id} not found"}


@mcp.tool(structured_output=False)
def add_plan_section(
    plan_id: int,
    heading: str,
    depth: int,
    section_type: str = "generic",
    parent_section_id: int = 0,
    slug: str = "",
    sort_order: int = 0,
    sprint_id: int = 0,
) -> dict:
    """Append a section to a plan.

    Args:
        plan_id: Parent plan FK (required).
        heading: Section heading text (required).
        depth: Heading level, 1-6 (H1-H6) (required, no default). Out-of-range
            values raise and are surfaced as {"error": ...}.
        section_type: sprint | phase | task | block | milestone | decision |
            prose | changelog | adr_section | reference | generic | uncertain
            (default: generic). Unknown values raise and are returned as an
            error dict.
        parent_section_id: Optional parent-section FK for nesting
            (0 = top-level / no parent).
        slug: Optional stable slug (empty = none).
        sort_order: Ordering within the plan (default 0).
        sprint_id: Optional sprint FK to link this section (0 = none).

    Returns: slim ack {id, ulid, heading, depth, section_type, status,
        sort_order} — scalar headers only. {"error": "..."} on ANY failure —
        both validation errors (bad section_type / out-of-range depth) and DB
        errors (e.g. a non-existent plan_id, sprint_id, or parent_section_id
        violating a foreign-key constraint) are caught and returned this way.
    """
    from dct.core.plans.crud import add_plan_section as _add
    try:
        s = _add(
            _get_engine(), plan_id=plan_id, heading=heading, depth=depth,
            section_type=section_type,
            parent_section_id=parent_section_id or None,
            slug=slug or None, sort_order=sort_order,
            sprint_id=sprint_id or None,
        )
        return {
            "id": s["id"], "ulid": s["ulid"], "heading": s["heading"],
            "depth": s["depth"], "section_type": s["section_type"],
            "status": s["status"], "sort_order": s["sort_order"],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_plan_sections(
    plan_id: int,
    parent_section_id: int = 0,
    section_type: str = "",
    format: str = "structured",
) -> dict | list:
    """List sections in a plan, optionally filtered by parent or type.

    Args:
        plan_id: Plan FK (required).
        parent_section_id: Restrict to direct children of this section
            (0 = no parent filter — return all sections regardless of nesting).
        section_type: Filter by type (empty = all). One of sprint | phase |
            task | block | milestone | decision | prose | changelog |
            adr_section | reference | generic | uncertain.
        format: 'structured' (cols/rows) | 'dict' (list of full dicts).

    Returns: {cols, rows} by default — cols are id, ulid, depth, status,
    section_type, sort_order, heading.
    """
    from dct.core.plans.crud import list_plan_sections as _list
    rows = _list(
        _get_engine(), plan_id=plan_id,
        parent_section_id=parent_section_id or None,
        section_type=section_type or None,
    )
    if format == "dict":
        return rows
    cols = ["id", "ulid", "depth", "status", "section_type", "sort_order", "heading"]
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


@mcp.tool(structured_output=False)
def complete_plan_checkpoint(checkpoint_id: int) -> dict:
    """Mark a plan checkpoint as done. Real-time; never batch completions.

    Returns: slim ack {id, plan_section_id, status, sort_order} — NOT the
        checkpoint text echo. {"error": "..."} on an unknown id.
    """
    from dct.core.plans.crud import complete_plan_checkpoint as _done
    row = _done(_get_engine(), checkpoint_id)
    if not row:
        return {"error": f"Plan checkpoint #{checkpoint_id} not found"}
    return {"id": row["id"], "plan_section_id": row["plan_section_id"], "status": row["status"], "sort_order": row["sort_order"]}


@mcp.tool(structured_output=False)
def reject_plan_checkpoint(checkpoint_id: int) -> dict:
    """Mark a plan checkpoint as rejected (scope change, N/A). Preserves ledger.

    Returns: slim ack {id, plan_section_id, status, sort_order} — NOT the
        checkpoint text echo. {"error": "..."} on an unknown id.
    """
    from dct.core.plans.crud import reject_plan_checkpoint as _reject
    row = _reject(_get_engine(), checkpoint_id)
    if not row:
        return {"error": f"Plan checkpoint #{checkpoint_id} not found"}
    return {"id": row["id"], "plan_section_id": row["plan_section_id"], "status": row["status"], "sort_order": row["sort_order"]}


@mcp.tool(structured_output=False)
def promote_section_to_item(
    section_id: int,
    item_type: str,
    priority: str = "P2",
    title: str = "",
    short_title: str = "",
) -> dict:
    """Convert a plan_section into a standalone items row with back-link.

    Useful when a section ('Sprint 3') deserves its own lifecycle outside the
    plan. The section is preserved in the plan and back-linked via
    `converted_to_item_id`. Project is inherited from the section's plan.

    Args:
        section_id: Source plan_section id (required).
        item_type: issue | todo | feature | idea | improvement (required).
        priority: P1 | P2 | P3 | future (default P2).
        title: Override item title. Empty → use the section heading.
        short_title: 2-4 word English noun phrase naming what the item is
            about (e.g. 'Windows hook paths'); ≤ 50 chars, refused not
            truncated; always pass it. Empty = no label.

    Returns: slim ack {id, item_type, priority, status, title} (the new item) —
        NOT the full item dict (no description echo). {"error": "..."} on failure.
    """
    from dct.core.plans.crud import promote_section_to_item as _promote
    try:
        d = _promote(
            _get_engine(), section_id,
            item_type=item_type, priority=priority,
            title=title or None, short_title=short_title or None,
        )
        return {"id": d["id"], "item_type": d["item_type"], "priority": d["priority"], "status": d["status"], "title": d["title"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def promote_checkpoint_to_item(
    checkpoint_id: int,
    item_type: str,
    priority: str = "P2",
    title: str = "",
    short_title: str = "",
) -> dict:
    """Convert a plan_checkpoint into a standalone items row with back-link.

    The checkpoint stays in its plan and is back-linked via
    `converted_to_item_id`. Project is inherited from the checkpoint's plan.

    Args:
        checkpoint_id: Source plan_checkpoint FK (required).
        item_type: issue | todo | feature | idea | improvement.
        priority: P1 | P2 | P3 | future (default P2).
        title: Override item title. Empty → use the checkpoint text
            (truncated to 500 chars).
        short_title: 2-4 word English noun phrase naming what the item is
            about (e.g. 'Windows hook paths'); ≤ 50 chars, refused not
            truncated; always pass it. Empty = no label.

    Returns: slim ack {id, item_type, priority, status, title} (the new item) —
        NOT the full item dict (no description echo). {"error": "..."} on failure.
    """
    from dct.core.plans.crud import promote_checkpoint_to_item as _promote
    try:
        d = _promote(
            _get_engine(), checkpoint_id,
            item_type=item_type, priority=priority,
            title=title or None, short_title=short_title or None,
        )
        return {"id": d["id"], "item_type": d["item_type"], "priority": d["priority"], "status": d["status"], "title": d["title"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def promote_section_to_sprint(
    section_id: int,
    code: str,
    name: str = "",
    goal: str = "",
    kind: str = "sprint",
) -> dict:
    """Materialize a `## Sprint N` plan section as a first-class sprint row.

    `section_type='sprint'` on a plan_section is only a semantic label —
    this call creates the real `sprints` entry with lifecycle + code +
    project-scoped uniqueness, and back-links the section via
    `plan_sections.sprint_id`.

    `code` is mandatory and must be unique per project (enforced by partial
    unique index). Heading-based auto-codes aren't safe — plan A's "Sprint 1"
    would collide with plan B's "Sprint 1". Caller picks a namespaced code
    like `v0.2-hub` or `0.5-ux-polish`.

    Args:
        name: Override sprint name. Empty → use section heading.
        goal: Optional free-text sprint goal.
        kind: sprint | phase | milestone | block (default: sprint).

    Returns: slim ack {id, code, kind, status, name} — scalar headers (the
        free-text `goal` is not echoed). {"error": "..."} on validation failure.
    """
    from dct.core.plans.crud import promote_section_to_sprint as _promote
    try:
        s = _promote(
            _get_engine(), section_id,
            code=code, name=name or None, goal=goal or None, kind=kind,
        )
        return {"id": s["id"], "code": s["code"], "kind": s["kind"], "status": s["status"], "name": s["name"]}
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — sprints / item_sprints ══════════════════════════════════════════

@mcp.tool(structured_output=False)
def create_sprint(
    name: str,
    project: str = "",
    code: str = "",
    kind: str = "sprint",
    status: str = "planned",
    goal: str = "",
) -> dict:
    """Create a sprint (a named time-/scope-boxed unit of work) under a project.

    Args:
        name: Sprint name (required, non-empty).
        project: Slug. Empty = auto-detect from CWD.
        code: Optional short handle ("0.5", "M0", "sprint-v0.1"); unique per
            project when set.
        kind: sprint | phase | milestone | block (default sprint). Subtype is
            informational — every kind shares one lifecycle.
        status: planned | active | completed | abandoned (default planned).
            'active' stamps started_at. PARALLEL ACTIVE SPRINTS ARE LEGAL —
            there is no one-active cap; note that current-sprint WRITES
            (set_sprint / clear_sprint / add_item_to_current_sprint) then
            refuse to guess a target and error listing the candidates.
            'deferred' is NOT a legal creation status (a sprint cannot be born
            parked — it has no started_at yet); create it planned/active then
            defer via update_sprint.
        goal: Optional free-text objective.

    Returns: slim ack {id, code, kind, status, name} — scalar headers only (the
        free-text `goal` is not echoed). {"error": "..."} on failure (empty
        name, invalid kind/status, status='deferred', duplicate code, or an
        unresolvable project — unknown slug, or empty project when the CWD is
        not a registered project).
    """
    from dct.core.sprints import create_sprint as _create
    try:
        s = _create(
            _get_engine(), _resolve_project(project),
            name=name, code=code or None, kind=kind,
            status=status, goal=goal or None,
        )
        return {"id": s["id"], "code": s["code"], "kind": s["kind"], "status": s["status"], "name": s["name"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def update_sprint(
    sprint_id: int,
    status: str = "",
    name: str = "",
    code: str = "",
    kind: str = "",
    goal: str = "",
) -> dict:
    """Update an existing sprint's fields, enforcing the lifecycle whitelist.

    Empty string = field unchanged (mirrors update_item). Each argument is a
    SEPARATE top-level parameter.

    Args:
        sprint_id: Target sprint (explicit id).
        status: New lifecycle status. The legal transition matrix is:
            planned   -> active, abandoned
            active    -> deferred, completed, abandoned
            deferred  -> active, completed, abandoned
            completed -> active            (reopen)
            abandoned -> active            (reopen)
            Any other move errors, listing the legal targets. A status equal to
            the current one is an idempotent no-op (timestamps untouched).
            Timestamp effects: ->active stamps started_at only when it was NULL
            (reactivation preserves it) — there is NO one-active cap, several
            sprints may run in parallel; reopen (completed/abandoned -> active)
            clears the stale ended_at; completed/abandoned stamp ended_at.
        name: New name (non-empty).
        code: New short handle; re-checked for per-project uniqueness.
        kind: sprint | phase | milestone | block.
        goal: New free-text objective.

    'deferred' semantics: parks a started-but-paused sprint. It KEEPS its
    started_at and is HIDDEN from current-sprint views (get_current_sprint /
    review_sprint). It stays visible via list_sprints(status='deferred').
    Parallel active sprints are legal — deferring is for parking work, not
    for freeing a slot.

    Returns: slim ack {id, code, kind, status, name} — scalar headers only (the
        free-text `goal` is not echoed). {"error": "..."} on an unknown id,
        illegal transition, a duplicate code, an invalid kind/status, or no
        fields supplied.
    """
    from dct.core.sprints import update_sprint as _update
    kwargs: dict = {}
    if status:
        kwargs["status"] = status
    if name:
        kwargs["name"] = name
    if code:
        kwargs["code"] = code
    if kind:
        kwargs["kind"] = kind
    if goal:
        kwargs["goal"] = goal
    try:
        result = _update(_get_engine(), sprint_id, **kwargs)
    except Exception as e:
        return {"error": str(e)}
    if result is None:
        return {"error": f"Sprint #{sprint_id} not found"}
    return {"id": result["id"], "code": result["code"], "kind": result["kind"], "status": result["status"], "name": result["name"]}


@mcp.tool(structured_output=False)
def list_sprints(
    project: str = "",
    status: str = "",
    kind: str = "",
    format: str = "structured",
) -> dict | list:
    """List a project's sprints, newest first, optionally filtered.

    Args:
        project: Slug. Empty = auto-detect from CWD; lists across ALL projects
            when CWD is not a registered project.
        status: Filter by planned | active | deferred | completed | abandoned.
            Empty = any. Use status='deferred' to surface parked sprints (they
            are hidden from the current-sprint views).
        kind: Filter by sprint | phase | milestone | block. Empty = any.
        format: 'structured' (default) -> {cols, rows} with cols = [id, code,
            kind, status, started_at, ended_at, name]; 'dict' -> list of full
            sprint dicts.

    Soft-deleted sprints are excluded.
    """
    from dct.core.sprints import list_sprints as _list
    project_slug = _resolve_project(project, required=False)
    rows = _list(
        _get_engine(), project_slug=project_slug,
        status=status or None, kind=kind or None,
    )
    if format == "dict":
        return rows
    cols = ["id", "code", "kind", "status", "started_at", "ended_at", "name"]
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


@mcp.tool(structured_output=False)
def assign_item_to_sprint(item_id: int, sprint_id: int) -> dict:
    """Link an item to a sprint (both by explicit id). Idempotent: an
    already-active link is returned unchanged; a soft-removed link is
    reactivated (clears removed_at, refreshes assigned_at).

    Returns the item_sprints link row, or {"error": "..."} if the item or the
    sprint id does not exist.
    """
    from dct.core.sprints import assign_item_to_sprint as _assign
    try:
        return _assign(_get_engine(), item_id=item_id, sprint_id=sprint_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def remove_item_from_sprint(item_id: int, sprint_id: int) -> dict:
    """Soft-remove the item<->sprint link (both by explicit id) — sets
    removed_at while preserving assigned_at history ("this item WAS in this
    sprint").

    Returns the updated link row, or {"error": "Link not found or already
    removed"} when there is no active link to remove.
    """
    from dct.core.sprints import remove_item_from_sprint as _remove
    row = _remove(_get_engine(), item_id=item_id, sprint_id=sprint_id)
    return row or {"error": "Link not found or already removed"}


@mcp.tool(structured_output=False)
def list_items_in_sprint(sprint_id: int, include_removed: bool = False, format: str = "structured") -> dict | list:
    """List items linked to a sprint, newest assignment first.

    Args:
        sprint_id: Target sprint (explicit id).
        include_removed: False (default) -> only active members; True also
            includes soft-removed links (the sprint's historical membership).
        format: 'structured' (default) -> {cols, rows} with cols = [id,
            item_type, priority, status, short_title, title] (short_title may
            be null); 'dict' -> list of full item dicts.
    """
    from dct.core.sprints import list_items_in_sprint as _list
    rows = _list(_get_engine(), sprint_id=sprint_id, include_removed=include_removed)
    if format == "dict":
        return rows
    cols = ["id", "item_type", "priority", "status", "short_title", "title"]
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


@mcp.tool(structured_output=False)
def add_item_to_current_sprint(item_id: int, project: str = "") -> dict:
    """Add ONE item to the project's current (active) sprint, auto-creating the
    sprint if none exists. Additive — keeps existing members (unlike set_sprint,
    which REPLACES the whole sprint). Use for "add #N to the sprint" / "flag for
    sprint". Returns {project, sprint_id, item_id, active}."""
    from dct.core.sprints import add_item_to_current_sprint as _add
    try:
        slug = _resolve_project(project, required=True)
        assert slug is not None
        return _add(_get_engine(), slug, item_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def remove_item_from_current_sprint(item_id: int, project: str = "") -> dict:
    """Remove ONE item from the project's current (active) sprint. No-op
    (removed=False) if there is no current sprint or the item isn't a member.
    Use for "remove #N from the sprint" / "unflag"."""
    from dct.core.sprints import remove_item_from_current_sprint as _remove
    try:
        slug = _resolve_project(project, required=True)
        assert slug is not None
        return _remove(_get_engine(), slug, item_id)
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — decisions ═══════════════════════════════════════════════════════

@mcp.tool(structured_output=False)
def create_decision(
    kind: str,
    title: str,
    project: str = "",
    ref_code: str = "",
    context: str = "",
    decision: str = "",
    consequences: str = "",
    alternatives: str = "",
    status: str = "proposed",
    plan_id: int = 0,
    plan_section_id: int = 0,
    sprint_id: int = 0,
) -> dict:
    """Create a decision record (ADR / sprint amendment / inline decision / open question).

    Each content field is its OWN separate top-level parameter — pass them
    independently, never bundle several into one value. The four prose fields:
      - context:      the situation / forces / problem prompting the decision
      - decision:     the choice made (the "we will …" statement)
      - consequences: what follows — trade-offs, risks, follow-up work
      - alternatives: options considered and rejected, and why
    A call carrying literal tool-call markup in any field is rejected, not
    stored (#240) — so structure the call correctly the first time.

    Args:
        kind: adr | sprint_amendment | inline_plan | open_question.
        title: Short decision title (required).
        context / decision / consequences / alternatives: the prose fields above (all optional).
        ref_code: Short reference like "ADR-001" (optional, unique per project).
        status: proposed | accepted | superseded | rejected.
        plan_id / plan_section_id / sprint_id: optional anchors (0 = none).

    Returns: slim ack {id, ulid, kind, ref_code, status, title} — scalar headers
        only; the four prose fields (context/decision/consequences/alternatives)
        are NOT echoed. {"error": "..."} on invalid kind/status, duplicate
        ref_code, markup, or an unresolvable project.
    """
    if (err := _reject_toolcall_markup(
        title=title, context=context, decision=decision,
        consequences=consequences, alternatives=alternatives,
    )):
        return err
    from dct.core.decisions import create_decision as _create
    try:
        d = _create(
            _get_engine(), _resolve_project(project),
            kind=kind, title=title,
            ref_code=ref_code or None,
            context=context or None, decision=decision or None,
            consequences=consequences or None, alternatives=alternatives or None,
            status=status,
            plan_id=plan_id or None, plan_section_id=plan_section_id or None,
            sprint_id=sprint_id or None,
        )
        return {"id": d["id"], "ulid": d["ulid"], "kind": d["kind"], "ref_code": d["ref_code"], "status": d["status"], "title": d["title"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def list_decisions(
    project: str = "",
    kind: str = "",
    status: str = "",
    plan_id: int = 0,
    sprint_id: int = 0,
    format: str = "structured",
) -> dict | list:
    """List decisions newest-first (created_at desc), optionally filtered.

    All filters optional — empty / 0 means "no filter":
        project: slug; empty = auto-detect from CWD, and if CWD is not a
            registered project, lists decisions across ALL projects.
        kind:    adr | sprint_amendment | inline_plan | open_question.
        status:  proposed | accepted | superseded | rejected.
        plan_id / sprint_id: keep only decisions anchored to that plan / sprint.
        format:  'structured' (default) → {"cols", "rows"} with
                 cols=[id, kind, status, ref_code, created_at, title];
                 'dict' → list of full-key decision dicts.
    """
    from dct.core.decisions import list_decisions as _list
    project_slug = _resolve_project(project, required=False)
    rows = _list(
        _get_engine(), project_slug=project_slug,
        kind=kind or None, status=status or None,
        plan_id=plan_id or None, sprint_id=sprint_id or None,
    )
    if format == "dict":
        return rows
    cols = ["id", "kind", "status", "ref_code", "created_at", "title"]
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


@mcp.tool(structured_output=False)
def update_decision(
    decision_id: int,
    title: str = "",
    context: str = "",
    decision: str = "",
    consequences: str = "",
    alternatives: str = "",
    status: str = "",
    ref_code: str = "",
) -> dict:
    """Edit a decision's fields in place; empty string = leave that field unchanged.

    decision_id (required): id of the decision to update. Each editable field is
    its own separate top-level parameter:
        title, context, decision, consequences, alternatives — the prose fields
            (same semantic roles as in create_decision).
        status — one of: proposed | accepted | superseded | rejected
            (an invalid value returns an {"error": ...} dict; nothing is written).
        ref_code — short reference like "ADR-001". The (project_id, ref_code) pair
            is unique among active decisions, so setting a ref_code already used by
            another decision in the project returns an {"error": ...} dict.
    kind and the plan/section/sprint anchors are NOT editable here — they are not
    parameters of this tool. At least one field must be supplied.

    Literal tool-call markup is rejected (nothing written) only in the prose fields
    title / context / decision / consequences / alternatives (#240); status and
    ref_code are NOT markup-scanned (status is instead validated against the enum
    above, ref_code is stored as given).

    Returns: slim ack {id, ulid, kind, ref_code, status, title} — scalar headers
        only; the four prose fields are NOT echoed. {"error": ...} when:
        decision_id is unknown, no (non-empty) fields were supplied, status is
        not a valid value, a ref_code collides with another active decision in
        the project, or a prose field contains tool-call markup.
    """
    if (err := _reject_toolcall_markup(
        title=title, context=context, decision=decision,
        consequences=consequences, alternatives=alternatives,
    )):
        return err
    from dct.core.decisions import update_decision as _update
    kwargs = {k: v for k, v in {
        "title": title, "context": context, "decision": decision,
        "consequences": consequences, "alternatives": alternatives,
        "status": status, "ref_code": ref_code,
    }.items() if v}
    try:
        row = _update(_get_engine(), decision_id=decision_id, **kwargs)
        if not row:
            return {"error": f"Decision #{decision_id} not found"}
        return {"id": row["id"], "ulid": row["ulid"], "kind": row["kind"], "ref_code": row["ref_code"], "status": row["status"], "title": row["title"]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(structured_output=False)
def supersede_decision(old_decision_id: int, new_decision_id: int) -> dict:
    """Mark decision #old_decision_id as superseded by #new_decision_id.

    Sets status='superseded' and superseded_by=#new on the OLD row only; the
    NEW row is left untouched — set its status (e.g. 'accepted') separately via
    update_decision. Errors if either id is unknown or the two ids are equal.

    Returns: slim ack {id, ulid, kind, ref_code, status, title} for the updated
        OLD decision — scalar headers only, no prose echo. {"error": ...} on an
        unknown id or equal ids.
    """
    from dct.core.decisions import supersede_decision as _supersede
    try:
        row = _supersede(_get_engine(), old_decision_id, new_decision_id)
        if not row:
            return {"error": "Decision not found"}
        return {"id": row["id"], "ulid": row["ulid"], "kind": row["kind"], "ref_code": row["ref_code"], "status": row["status"], "title": row["title"]}
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — plan ingest ═════════════════════════════════════════════════════

@mcp.tool(structured_output=False)
def ingest_plan_file(
    file_path: str,
    project: str = "",
    auto_resolve_uncertain: bool = True,
) -> dict:
    """Ingest a markdown plan/spec/roadmap/ADR file into dct.

    Runs the full pipeline: parse → ensure ULIDs → atomic write-back → DB
    upsert. On first import reads `[x]` as `status='done'` (historical
    truth, one-time). Subsequent re-ingests ignore MD checkbox state.

    Args:
        file_path: Absolute or CWD-relative path to the file.
        project: Project slug. Empty = auto-detect from CWD.
        auto_resolve_uncertain: When True, uncertain sections go through
            `claude --print` classification. Use False to skip LLM calls
            (sections remain with section_type='uncertain').

    Returns: IngestReport dict {plan_id, plan_ulid, is_first_import,
        source_hash, sections_created, sections_updated, checkpoints_created,
        uncertain_resolved, uncertain_remaining, ulids_injected, warnings}.
    """
    from dct.core.plans.ingest import ingest_file as _ingest
    try:
        return _ingest(
            _get_engine(), file_path,
            project_slug=_resolve_project(project),
            auto_resolve_uncertain=auto_resolve_uncertain,
        )
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run()
