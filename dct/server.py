"""FastMCP server for dct — exposes tracking operations as MCP tools.

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

from datetime import datetime

from mcp.server.fastmcp import FastMCP

from dct.core.engine import bootstrap_engine

mcp = FastMCP(
    "dct",
    instructions=(
        "Deterministic Context Tracker — cross-project work tracking.\n\n"
        "RESOURCES:\n"
        "- items: features, todos, issues, ideas, improvements (status: open/in_progress/resolved/wont_fix)\n"
        "- checkpoints: acceptance criteria tied to items (status: pending/done/rejected) — ledger\n"
        "- notes: append-only observations on items\n"
        "- changelog_entries: shipped work, versioned via release_changelog\n"
        "- session_handoffs: cross-session prompts / context drafts (ledger)\n\n"
        "RESPONSE FORMAT:\n"
        "- list_* tools return {cols: [...], rows: [[...]]} — compact tabular form.\n"
        "  `cols` carries full field names once; rows are value arrays in the same order.\n"
        "  Short/fixed-width fields come first; long free-text (title, description, entry, prompt) last.\n"
        "- Item cols legend: id, item_type, priority, status, in_current_sprint,\n"
        "  cp=[done,total] checkpoint progress, component, tags, created_at, title, description.\n"
        "- Changelog cols legend: id, version, category, component, related_item_id,\n"
        "  created_at, released_at, entry.\n"
        "- Checkpoint cols legend: id, status, sort_order, text.\n"
        "- Handoff cols legend: id, scope, created_by, created_at, consumed_at, prompt.\n"
        "- get_item returns the full semantic dict with embedded notes, checkpoints, progress.\n"
        "- On list_* tools, format='markdown' gives a human-readable table (lossy);\n"
        "  format='dict' gives a list of full-key dicts (use when you need to hand the\n"
        "  data to a downstream tool that expects the classic shape).\n\n"
        "WORKFLOW FOR COMPLEX TASKS:\n"
        "1. list_items or get_item to find/inspect.\n"
        "2. Item's checkpoints = your TODO list (work in sort_order).\n"
        "3. REMEMBER the id returned by create_item, add_checkpoint, create_handoff.\n"
        "4. For feature/improvement items: sketch 3-7 checkpoints with add_checkpoint\n"
        "   BEFORE starting code. This makes progress trackable.\n"
        "5. complete_checkpoint(id) as each step finishes — real-time, NOT batched.\n"
        "6. Irrelevant checkpoint → reject_checkpoint (ledger-preserving); never hard delete.\n"
        "7. After all checkpoints done/rejected → resolve_item (no auto-transition).\n\n"
        "SPRINT:\n"
        "- set_sprint(project, item_ids='22,21,6') atomically replaces the current sprint.\n"
        "- review_sprint(project, since='last_release') — start/end-of-session dashboard.\n"
        "- resolve_item auto-clears in_current_sprint (sprint = active work only).\n"
        "- Lifecycle: planned|active|deferred|completed|abandoned. update_sprint(sprint_id,\n"
        "  status=...) drives whitelisted transitions: park an unfinished sprint with\n"
        "  status='deferred' (keeps started_at, frees the one-active-per-project slot),\n"
        "  reactivate it with status='active'. Never mark an unfinished sprint\n"
        "  completed/abandoned — defer it.\n\n"
        "HANDOFFS:\n"
        "- create_handoff(prompt, created_by, scope?) — leave note for next session.\n"
        "- Multiple unconsumed per project allowed (parallel sessions / different scopes).\n"
        "- consume_handoff(id, consumed_by?) marks it read but preserves the row.\n\n"
        "TOOL-CALL HYGIENE:\n"
        "- Pass every argument as its own separate top-level parameter. Tools with\n"
        "  several prose fields (create_decision: context/decision/consequences/\n"
        "  alternatives; create_handoff: prompt) expect each in its own parameter —\n"
        "  never nest one field inside another, and never drop the parameter's\n"
        "  namespace prefix. Free-text tools reject literal tool-call markup and\n"
        "  explain the fix; a rejection means the call was mis-serialized — re-send\n"
        "  with the fields split out.\n\n"
        "ALL tables are soft-delete only (ledger). Nothing is ever hard-deleted."
    ),
)

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
    """
    for name, value in fields.items():
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
ITEM_COLS_DEFAULT = [
    "id", "item_type", "priority", "status", "in_current_sprint",
    "cp", "component", "tags", "created_at", "title", "description",
]
CHANGELOG_COLS = [
    "id", "version", "category", "component", "related_item_id",
    "created_at", "released_at", "entry",
]
CHECKPOINT_COLS = ["id", "status", "sort_order", "text"]
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


@mcp.tool()
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


@mcp.tool()
def create_item(
    item_type: str,
    title: str,
    project: str = "",
    description: str = "",
    priority: str = "P2",
    component: str = "",
    source_file: str = "",
    source_lines: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Create a tracked item (issue / todo / feature / idea / improvement).

    REMEMBER the returned `id` — you need it for add_checkpoint / update_item /
    resolve_item / add_note.

    For feature/improvement items: immediately sketch 3-7 checkpoints with
    add_checkpoint before starting work.

    TYPE SELECTION (natural-language phrases → item_type):
      - issue: "problem", "bug", "crash", "error", "broken", "doesn't work",
               "regression" — observable defect in existing code
      - todo: "task", "action item", "need to", "must do" — generic work
              that is neither a bug nor a new user-visible capability
      - feature: "add X", "new capability", "let's build", "we should have" —
                 new user-visible capability that doesn't exist yet
      - idea: "maybe", "what if", "consider", "brainstorm", "explore" —
              exploratory, not yet decided to build
      - improvement: "refactor", "polish", "cleanup", "tidy up", "make X nicer",
                     "simplify" — existing thing works but could be better

    When in doubt, pick `todo` (neutral bucket). Do not ask the user about
    type unless the phrase is genuinely ambiguous.

    Args:
        item_type: One of: issue / todo / feature / idea / improvement.
        title: Short title (≤ 80 chars).
        project: Project slug. Empty = auto-detect from CWD.
        description: Prose context (no checkbox lists — use add_checkpoint instead).
        priority: P1 (blocker) / P2 (important, default) / P3 (backlog) / future.
        component: Component or module name.
        source_file: Relative file path.
        source_lines: Line range (e.g. "42-58").
        tags: List of tag strings. Pass as a JSON array, e.g. ["oss","tests"].
    """
    if (err := _reject_toolcall_markup(title=title, description=description)):
        return err
    from dct.core.items import create_item as _create

    kwargs: dict = {}
    if description: kwargs["description"] = description
    if component: kwargs["component"] = component
    if source_file: kwargs["source_file"] = source_file
    if source_lines: kwargs["source_lines"] = source_lines
    if tags: kwargs["tags"] = tags

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return _create(_get_engine(), project_slug, item_type, title, priority=priority, **kwargs)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_items(
    project: str = "",
    status: str = "open",
    item_type: str = "",
    priority: str = "",
    component: str = "",
    in_current_sprint: str = "",
    search: str = "",
    limit: int = 50,
    format: str = "structured",
):
    """List tracked items in compact {cols,rows} form.

    Each row carries a `cp` field with [done, total] checkpoint progress
    computed in a single SQL query (no N+1).

    PROJECT RESOLUTION — IMPORTANT:
      - `project="<slug>"` — filter to that project (recommended).
      - `project=""` AND CWD is a registered project — filter to that project.
      - `project=""` AND CWD is NOT a registered project — returns items
        ACROSS ALL PROJECTS. This is intentional for cross-project queries
        but can surprise callers expecting project-scoped output. When in
        doubt pass an explicit `project` slug.

    Args:
        project: Slug. Empty = CWD auto-detect; falls back to all projects
            when CWD is unregistered (see PROJECT RESOLUTION above).
        status: Filter by status. Default 'open'. Use '' for all statuses.
        item_type: issue / todo / feature / idea / improvement.
        priority: P1 / P2 / P3 / future.
        component: Filter by component name.
        in_current_sprint: '' (any, default), 'true' (only sprint), 'false' (non-sprint).
        search: Substring in title + description.
        limit: Max rows (default 50).
        format: 'structured' (default, {cols,rows}), 'markdown', 'dict'.

    Returns: {"cols": [...], "rows": [[...]]} by default, list[dict] for
        format="dict", or markdown str for format="markdown".
    """
    from dct.core.items import list_items as _list

    kwargs: dict = {"limit": limit}
    detected = _resolve_project(project, required=False)
    if detected: kwargs["project_slug"] = detected
    if status: kwargs["status"] = status
    if item_type: kwargs["item_type"] = item_type
    if priority: kwargs["priority"] = priority
    if component: kwargs["component"] = component
    if search: kwargs["search"] = search
    sprint_filter = _parse_bool_tri(in_current_sprint)
    if sprint_filter is not None:
        kwargs["in_current_sprint"] = sprint_filter

    try:
        rows = _list(_get_engine(), **kwargs)
        return _format_list(rows, ITEM_COLS_DEFAULT, format)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
def update_item(
    item_id: int,
    title: str = "",
    status: str = "",
    priority: str = "",
    component: str = "",
    resolution: str = "",
    description: str = "",
) -> dict:
    """Update one or more mutable fields on an existing item. Only non-empty
    args are applied; an empty/omitted field is left unchanged.

    Transitioning status to a terminal value (resolved/wont_fix) automatically
    sets resolved_at AND removes the item from the current sprint (its
    item_sprints links are soft-removed). Transitioning back to open/in_progress
    clears resolved_at. NOTE: unlike `resolve_item`, this path does NOT run the
    gate-checkpoint guard — prefer `resolve_item` for terminal transitions.

    Sprint membership is NOT set here — `in_current_sprint` is a derived,
    read-only field. Use set_sprint / assign_item_to_sprint /
    remove_item_from_sprint to change membership.

    Args:
        item_id: Target item.
        title: New title (non-empty).
        status: open / in_progress / resolved / wont_fix.
        priority: P1 / P2 / P3 / future.
        component: Component or module name.
        resolution: Resolution / rationale note.
        description: Prose context.

    Clearing (nulling) a field — pass the literal '__clear__' to null out a
    NULLABLE field: component, resolution, description. Do NOT pass the sentinel
    for title/status/priority — title would be overwritten with an empty string
    and status/priority would fail enum validation ({"error": ...}).

    Returns: updated item dict (full get_item shape) or {"error": "..."}.
    """
    if (err := _reject_toolcall_markup(title=title, description=description, resolution=resolution)):
        return err
    from dct.core.items import update_item as _update

    kwargs: dict = {}
    for field, value in [
        ("title", title), ("status", status), ("priority", priority),
        ("component", component), ("resolution", resolution), ("description", description),
    ]:
        if value == "__clear__":
            kwargs[field] = ""
        elif value:
            kwargs[field] = value

    try:
        result = _update(_get_engine(), item_id, **kwargs)
        return result or {"error": f"Item #{item_id} not found"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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

    Returns: full updated item dict (get_item shape), or {"error": "..."} when
        the item is missing or a pending gate blocks the transition.
    """
    from dct.core.items import resolve_item as _resolve
    try:
        result = _resolve(_get_engine(), item_id, status=status, resolution=resolution or None)
        return result or {"error": f"Item #{item_id} not found"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def add_note(item_id: int, note: str) -> dict:
    """Append a note to an item (ledger — never overwritten).

    Use for: discoveries while working, decisions, blockers, "fixed in commit X".
    Do NOT use for: TODO steps (use add_checkpoint), status changes that have
    their own rationale field (use update_item resolution=).

    /track-update treats status/priority transitions as mandatory note points —
    the audit trail is what lets /pickup explain "why #42 moved to P1".

    Returns: note dict with new `id`, or {"error": "..."} if the item does not exist.
    """
    if (err := _reject_toolcall_markup(note=note)):
        return err
    from dct.core.items import add_note as _add_note
    try:
        return _add_note(_get_engine(), item_id, note)
    except Exception as e:
        return {"error": str(e)}


# ── Checkpoint tools ──────────────────────────────────────────────────────────


@mcp.tool()
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
    """
    from dct.core.checkpoints import add_checkpoint as _add
    try:
        return _add(_get_engine(), item_id, text, kind=kind)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def complete_checkpoint(checkpoint_id: int) -> dict:
    """Mark a checkpoint as done (status 'done').

    CRITICAL: call IMMEDIATELY after the step completes. Do NOT batch completions
    at the end of a task — real-time updates give the user live progress visibility.
    Completing a pending gate checkpoint (dogfood/security/approval/review)
    satisfies it and clears that block on resolve_item.

    Returns: the updated checkpoint dict, or {"error": "Checkpoint #<id> not found"}
    when the id is unknown or already deleted.
    """
    from dct.core.checkpoints import set_status
    try:
        result = set_status(_get_engine(), checkpoint_id, "done")
        return result or {"error": f"Checkpoint #{checkpoint_id} not found"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def reject_checkpoint(checkpoint_id: int) -> dict:
    """Mark a checkpoint as rejected (status 'rejected') — ledger-preserving
    alternative to delete.

    Use when scope changes or an initial assumption turns out wrong. Rejected
    checkpoints drop out of the `active` count, so they no longer weigh on the
    done/active progress percent. For a gate checkpoint (kind dogfood/security/
    approval/review) rejecting is the sanctioned "not applicable" skip that
    unblocks resolve_item.

    Returns: the updated checkpoint dict, or {"error": "Checkpoint #<id> not found"}
    when the id is unknown or already deleted.
    """
    from dct.core.checkpoints import set_status
    try:
        result = set_status(_get_engine(), checkpoint_id, "rejected")
        return result or {"error": f"Checkpoint #{checkpoint_id} not found"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def reopen_checkpoint(checkpoint_id: int) -> dict:
    """Revert a checkpoint back to pending (status 'pending'), typically from done
    or rejected. Re-opening a previously rejected gate makes it pending again and
    re-blocks resolve_item.

    Returns: the updated checkpoint dict, or {"error": "Checkpoint #<id> not found"}
    when the id is unknown or already deleted.
    """
    from dct.core.checkpoints import set_status
    try:
        result = set_status(_get_engine(), checkpoint_id, "pending")
        return result or {"error": f"Checkpoint #{checkpoint_id} not found"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
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

    Returns: the created entry dict (full row, incl. `id`, version=null), or
        {"error": "..."} on failure — empty/whitespace entry, invalid category,
        literal tool-call markup in `entry`, or an unresolvable project (explicit
        slug not found, or empty `project` with an unregistered CWD).
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
        return add_entry(_get_engine(), project_slug, category, entry, **kwargs)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def set_sprint(project: str = "", item_ids: str = "") -> dict:
    """Atomically replace the current sprint's membership with the given ids.

    The current sprint is the project's single active sprint (auto-created if
    none exists). Membership lives in item_sprints — assigns the listed ids and
    soft-removes the rest. Ids that aren't live items of this project (foreign,
    unknown, or soft-deleted) are silently dropped, so the returned `size` /
    `items` may be smaller than the ids you passed. **There is no "no-op" call —
    `item_ids=""` WIPES the sprint** (equivalent to `clear_sprint`). If you want
    to keep the current sprint, don't call this tool.

    Args:
        project: Slug. Empty = auto-detect from CWD.
        item_ids: Comma-separated ids (e.g. "22,21,6"). Empty string clears sprint.

    Returns: {"project": "<slug>", "size": <int>, "items": [valid ids, sorted]}.
        Error: {"error": "..."} on project resolution failure.
    """
    from dct.core.sprint import set_sprint as _set

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        ids = [int(x.strip()) for x in item_ids.split(",") if x.strip()] if item_ids else []
        return _set(_get_engine(), project_slug, ids)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
def review_sprint(project: str = "", since: str = "last_release") -> dict:
    """Session start/end dashboard — in_sprint, recent_activity, stale_in_progress, unconsumed_handoffs.

    Returns structured data only. Interpretation (prioritization logic, "what to
    do next", etc.) lives in the consuming skill — /pickup (session start) and
    /handoff (session end) — so it can iterate without a schema change.

    Args:
        project: Slug. Empty = CWD auto-detect.
        since: Accepted grammar —
            - "last_release" (default) — anchor = max(released_at); falls back
              to all-time when the project has no releases yet.
            - "all" — no time cutoff, everything.
            - "<N>h" where N is one-or-more digits — hours relative to now. Only
              the hour suffix is supported (no "d"/"w"/"m"); e.g. "24h".
            - ISO date / datetime — "2026-04-10" or "2026-04-10T14:00", optional
              timezone (trailing "Z" accepted). Parsed via datetime.fromisoformat.

    Returns: dashboard dict with keys project, since, since_anchor (human
        label), in_sprint, recent_activity, stale_in_progress,
        unconsumed_handoffs, summary. in_sprint / stale_in_progress /
        unconsumed_handoffs are {cols, rows} tables; recent_activity is a dict
        {items_changed: {cols,rows}, checkpoints_completed: int,
        changelog_added: int}; summary is a scalar dict. Error: {"error": "..."}
        on invalid `since`.
    """
    from dct.core.sprint import get_sprint_review
    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return get_sprint_review(_get_engine(), project_slug, since=since)
    except Exception as e:
        return {"error": str(e)}


# ── Handoff tools ─────────────────────────────────────────────────────────────


@mcp.tool()
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
        created_by: REQUIRED, non-empty — self-identify (e.g.
            "claude-opus-4-7 @ 2026-04-17T14:30Z").
        project: Empty = auto-detect from CWD.
        scope: Optional tag like "sprint-v0.2" or "refactor-auth".
        context_summary: Optional snapshot of state at handoff time.

    Returns: new handoff dict including `id`, `created_at`, `consumed_at=null`.
        Error cases: {"error": "..."} when prompt is empty, created_by is empty,
        or project slug does not resolve.
    """
    if (err := _reject_toolcall_markup(prompt=prompt, context_summary=context_summary)):
        return err
    from dct.core.handoff import create_handoff as _create

    try:
        project_slug = _resolve_project(project, required=True)
        assert project_slug is not None
        return _create(
            _get_engine(), project_slug,
            prompt=prompt, created_by=created_by,
            scope=scope or None,
            context_summary=context_summary or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_handoffs(
    project: str = "",
    unconsumed_only: bool = True,
    format: str = "structured",
):
    """List session handoffs for a project.

    PROJECT RESOLUTION — IMPORTANT:
      - `project="<slug>"` — filter to that project (recommended).
      - `project=""` AND CWD is a registered project — filter to that project.
      - `project=""` AND CWD is NOT a registered project — returns handoffs
        ACROSS ALL PROJECTS. Intentional, but can surprise — pass `project`
        explicitly when you want scoped output.

    Args:
        project: Slug. Empty = CWD auto-detect; falls back to all projects
            when CWD is unregistered.
        unconsumed_only: If True (default) returns only handoffs without a
            consumed_at stamp — the "waiting for me" set. False returns all.
        format: 'structured' (default, {cols,rows}), 'markdown', 'dict'.

    Returns: {"cols": [...], "rows": [[...]]} by default, list[dict] for
        format="dict", or markdown str for format="markdown".
    """
    from dct.core.handoff import list_handoffs as _list
    try:
        project_slug = _resolve_project(project, required=False)
        rows = _list(_get_engine(), project_slug, unconsumed_only=unconsumed_only)
        return _format_list(rows, HANDOFF_COLS, format)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def consume_handoff(handoff_id: int, consumed_by: str = "") -> dict:
    """Mark a handoff as consumed. Preserves the row in the ledger.

    Args:
        handoff_id: id returned by create_handoff / list_handoffs.
        consumed_by: optional — self-identify the consuming session (mirrors
            create_handoff's `created_by` format).

    Returns: updated handoff dict with `consumed_at` timestamp set, or
        {"error": "..."} if the handoff is missing / already consumed.
    """
    from dct.core.handoff import consume_handoff as _consume
    try:
        result = _consume(_get_engine(), handoff_id, consumed_by=consumed_by or None)
        return result or {"error": f"Handoff #{handoff_id} not found or already consumed"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
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
        scope: New scope tag (omit/empty = unchanged).
        context_summary: New context snapshot (omit/empty = unchanged).

    Returns: updated handoff dict, or {"error": "..."} if the handoff is
        missing / consumed / deleted, or if no field was supplied.
    """
    if (err := _reject_toolcall_markup(prompt=prompt, context_summary=context_summary)):
        return err
    from dct.core.handoff import update_handoff as _update
    try:
        result = _update(
            _get_engine(), handoff_id,
            prompt=prompt or None,
            scope=scope or None,
            context_summary=context_summary or None,
        )
        return result or {"error": f"Handoff #{handoff_id} not found, consumed, or deleted"}
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — plans / plan_sections / plan_checkpoints ════════════════════════

@mcp.tool()
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

    Returns: created plan dict (status defaults to 'active'), or
    {"error": "..."} on invalid kind, empty slug/title, duplicate slug in the
    project, or an unresolvable project (bad slug / unregistered CWD).
    """
    from dct.core.plans.crud import create_plan as _create
    try:
        return _create(
            _get_engine(),
            _resolve_project(project),
            slug=slug, title=title, kind=kind,
            source_file=source_file or None,
            sprint_id=sprint_id or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
def get_plan(plan_id: int) -> dict:
    """Full plan dict with nested sections + checkpoints + progress rollup.

    Progress: {total, done, rejected, pending, active, percent}. `active =
    total - rejected`, `percent = 100 * done / active`.
    """
    from dct.core.plans.crud import get_plan as _get
    row = _get(_get_engine(), plan_id)
    return row or {"error": f"Plan #{plan_id} not found"}


@mcp.tool()
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

    Returns: created section dict, or {"error": "..."} on ANY failure — both
        validation errors (bad section_type / out-of-range depth) and DB
        errors (e.g. a non-existent plan_id, sprint_id, or parent_section_id
        violating a foreign-key constraint) are caught and returned this way.
    """
    from dct.core.plans.crud import add_plan_section as _add
    try:
        return _add(
            _get_engine(), plan_id=plan_id, heading=heading, depth=depth,
            section_type=section_type,
            parent_section_id=parent_section_id or None,
            slug=slug or None, sort_order=sort_order,
            sprint_id=sprint_id or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
def complete_plan_checkpoint(checkpoint_id: int) -> dict:
    """Mark a plan checkpoint as done. Real-time; never batch completions."""
    from dct.core.plans.crud import complete_plan_checkpoint as _done
    row = _done(_get_engine(), checkpoint_id)
    return row or {"error": f"Plan checkpoint #{checkpoint_id} not found"}


@mcp.tool()
def reject_plan_checkpoint(checkpoint_id: int) -> dict:
    """Mark a plan checkpoint as rejected (scope change, N/A). Preserves ledger."""
    from dct.core.plans.crud import reject_plan_checkpoint as _reject
    row = _reject(_get_engine(), checkpoint_id)
    return row or {"error": f"Plan checkpoint #{checkpoint_id} not found"}


@mcp.tool()
def promote_section_to_item(
    section_id: int,
    item_type: str,
    priority: str = "P2",
    title: str = "",
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

    Returns: the new item dict, or {"error": "..."} on failure.
    """
    from dct.core.plans.crud import promote_section_to_item as _promote
    try:
        return _promote(
            _get_engine(), section_id,
            item_type=item_type, priority=priority,
            title=title or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def promote_checkpoint_to_item(
    checkpoint_id: int,
    item_type: str,
    priority: str = "P2",
    title: str = "",
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

    Returns: the new item dict, or {"error": "..."} on failure.
    """
    from dct.core.plans.crud import promote_checkpoint_to_item as _promote
    try:
        return _promote(
            _get_engine(), checkpoint_id,
            item_type=item_type, priority=priority,
            title=title or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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

    Returns: created sprint dict, or {"error": "..."} on validation failure.
    """
    from dct.core.plans.crud import promote_section_to_sprint as _promote
    try:
        return _promote(
            _get_engine(), section_id,
            code=code, name=name or None, goal=goal or None, kind=kind,
        )
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — sprints / item_sprints ══════════════════════════════════════════

@mcp.tool()
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
            'active' stamps started_at and is capped at ONE active sprint per
            project — creating a second active sprint errors. 'deferred' is NOT
            a legal creation status (a sprint cannot be born parked — it has no
            started_at yet); create it planned/active then defer via
            update_sprint.
        goal: Optional free-text objective.

    Returns the new sprint dict (full row), or {"error": "..."} on failure
    (empty name, invalid kind/status, status='deferred', duplicate code, a
    second active sprint, or an unresolvable project — unknown slug, or empty
    project when the CWD is not a registered project).
    """
    from dct.core.sprints import create_sprint as _create
    try:
        return _create(
            _get_engine(), _resolve_project(project),
            name=name, code=code or None, kind=kind,
            status=status, goal=goal or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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
            (reactivation preserves it) and is capped at ONE active sprint per
            project; reopen (completed/abandoned -> active) clears the stale
            ended_at; completed/abandoned stamp ended_at.
        name: New name (non-empty).
        code: New short handle; re-checked for per-project uniqueness.
        kind: sprint | phase | milestone | block.
        goal: New free-text objective.

    'deferred' semantics: parks a started-but-paused sprint. It KEEPS its
    started_at, frees the single active slot (so another sprint can be
    activated), and is HIDDEN from current-sprint views (get_current_sprint /
    review_sprint). It stays visible via list_sprints(status='deferred').

    Returns the full updated sprint dict, or {"error": "..."} on an unknown id,
    illegal transition, a second active sprint, a duplicate code, an invalid
    kind/status, or no fields supplied.
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
    return result


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def list_items_in_sprint(sprint_id: int, include_removed: bool = False, format: str = "structured") -> dict | list:
    """List items linked to a sprint, newest assignment first.

    Args:
        sprint_id: Target sprint (explicit id).
        include_removed: False (default) -> only active members; True also
            includes soft-removed links (the sprint's historical membership).
        format: 'structured' (default) -> {cols, rows} with cols = [id,
            item_type, priority, status, title]; 'dict' -> list of full item
            dicts.
    """
    from dct.core.sprints import list_items_in_sprint as _list
    rows = _list(_get_engine(), sprint_id=sprint_id, include_removed=include_removed)
    if format == "dict":
        return rows
    cols = ["id", "item_type", "priority", "status", "title"]
    return {"cols": cols, "rows": [[r[c] for c in cols] for r in rows]}


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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
    """
    if (err := _reject_toolcall_markup(
        title=title, context=context, decision=decision,
        consequences=consequences, alternatives=alternatives,
    )):
        return err
    from dct.core.decisions import create_decision as _create
    try:
        return _create(
            _get_engine(), _resolve_project(project),
            kind=kind, title=title,
            ref_code=ref_code or None,
            context=context or None, decision=decision or None,
            consequences=consequences or None, alternatives=alternatives or None,
            status=status,
            plan_id=plan_id or None, plan_section_id=plan_section_id or None,
            sprint_id=sprint_id or None,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
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


@mcp.tool()
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

    Returns the updated decision dict, or an {"error": ...} dict when: decision_id
    is unknown, no (non-empty) fields were supplied, status is not a valid value,
    a ref_code collides with another active decision in the project, or a prose
    field contains tool-call markup.
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
        return row or {"error": f"Decision #{decision_id} not found"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def supersede_decision(old_decision_id: int, new_decision_id: int) -> dict:
    """Mark decision #old_decision_id as superseded by #new_decision_id.

    Sets status='superseded' and superseded_by=#new on the OLD row only; the
    NEW row is left untouched — set its status (e.g. 'accepted') separately via
    update_decision. Errors if either id is unknown or the two ids are equal.
    Returns the updated old-decision dict, or {"error": ...}.
    """
    from dct.core.decisions import supersede_decision as _supersede
    try:
        row = _supersede(_get_engine(), old_decision_id, new_decision_id)
        return row or {"error": "Decision not found"}
    except Exception as e:
        return {"error": str(e)}


# ══ v0.2.0 — plan ingest ═════════════════════════════════════════════════════

@mcp.tool()
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


# ── Frugal MCP deferral exemption (#241) ──────────────────────────────────────
# Claude Code defers ALL MCP tools by default (ENABLE_TOOL_SEARCH unset), so even
# /pickup pays a ToolSearch round-trip before it can run. We exempt only the
# daily-driver tools — the ones the pickup / track* / clog / commit / handoff
# skills reach for every session — by stamping the `anthropic/alwaysLoad` _meta
# hint the CC client reads (requires Claude Code v2.1.121+). Specialist tools
# (plans, decisions, first-class sprints, web, release admin) stay deferred.
# Add a name here ONLY if a routine skill needs it from token zero; keep the long
# tail deferred to spend session context wisely. Takes effect on server restart.
_ALWAYS_LOAD_TOOLS = frozenset({
    # project + item lifecycle (track / track-list / track-update / track-resolve)
    "list_projects", "list_items", "get_item",
    "create_item", "update_item", "resolve_item", "add_note",
    "add_checkpoint", "complete_checkpoint", "reject_checkpoint",
    # changelog / commit
    "add_changelog", "count_unreleased_changelog",
    # sprint + handoff (pickup / handoff)
    "review_sprint", "set_sprint",
    "create_handoff", "list_handoffs", "consume_handoff", "delete_handoff",
})


def _apply_always_load(server: FastMCP) -> None:
    """Stamp the daily-driver tools with Claude Code's alwaysLoad hint so they
    load into context at session start instead of behind a ToolSearch round-trip.

    Raises at import if a hot-set name is not a registered tool — the set must
    track real tool names, so a typo fails loud rather than silently leaving a
    tool deferred.
    """
    registered = server._tool_manager._tools
    missing = _ALWAYS_LOAD_TOOLS - registered.keys()
    if missing:
        raise RuntimeError(
            f"_ALWAYS_LOAD_TOOLS names not registered as tools: {sorted(missing)}"
        )
    for name in _ALWAYS_LOAD_TOOLS:
        registered[name].meta = {"anthropic/alwaysLoad": True}


_apply_always_load(mcp)


if __name__ == "__main__":
    mcp.run()
