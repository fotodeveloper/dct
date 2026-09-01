"""Items views — filterable list table + detail page (read-only).

GET /p/<slug>/items            filter form + htmx-swappable table
GET /p/<slug>/items/<int:id>   detail: markdown description, notes, checkpoints
"""

from flask import Blueprint, abort, render_template, request

from dct.core.checkpoints import NON_GATE_KINDS
from dct.core.items import get_item, list_items
from dct.core.projects import get_project
from dct.core.tags import tag_id_for_name_conn
from dct.web.app import current_engine

bp = Blueprint("items", __name__)


def is_gate(kind: str | None) -> bool:
    """Anything but a plain task / mirrored cc-task is a gate — one rule,
    shared with core (dct.core.checkpoints.NON_GATE_KINDS)."""
    return (kind or "task") not in NON_GATE_KINDS


# Filter values offered in the list form. Kept here (not in templates) so the
# allowed set is one source of truth.
TYPE_OPTIONS = ["issue", "todo", "feature", "idea", "improvement"]
PRIORITY_OPTIONS = ["P1", "P2", "P3", "future"]
STATUS_OPTIONS = ["open", "in_progress", "resolved", "wont_fix"]
SORT_OPTIONS = [
    ("status", "status"),
    ("priority", "priority"),
    ("recent", "recently updated"),
    ("created", "newest"),
    ("title", "title"),
]
_PRI_RANK = {"P1": 1, "P2": 2, "P3": 3, "future": 4}
# Active work first, terminal states sink to the bottom: in_progress, open,
# then wont_fix, resolved. Priority is the secondary key within each status.
_STATUS_RANK = {"in_progress": 0, "open": 1, "wont_fix": 2, "resolved": 3}


def _clean(value: str | None) -> str | None:
    """Empty querystring value -> None so it is not passed as a filter."""
    if value is None:
        return None
    value = value.strip()
    return value or None


@bp.route("/p/<slug>/items")
def list(slug):
    engine = current_engine()
    # Resolve the project FIRST, like every other project route: an unknown or
    # soft-deleted slug is a 404, not a 200 with an empty table.
    if get_project(engine, slug) is None:
        abort(404)
    filters = {
        "item_type": _clean(request.args.get("type")),
        "priority": _clean(request.args.get("priority")),
        "status": _clean(request.args.get("status")),
        "component": _clean(request.args.get("component")),
        "search": _clean(request.args.get("q")),
    }
    # The tag dictionary is CLOSED: core raises ValueError for an unknown (or
    # unnormalizable) name. A querystring must never 500 — surface the message
    # as an empty result plus a hint rendered by the table partial. The lookup
    # is done UP FRONT so the except covers the tag resolution ONLY: a
    # ValueError from the item query itself is a real bug and must propagate,
    # never be mislabelled as an unknown-tag hint.
    tag = _clean(request.args.get("tag"))
    tag_error = None
    if tag:
        with engine.connect() as conn:
            try:
                tag_id_for_name_conn(conn, tag)
            except ValueError as exc:
                tag_error = str(exc)

    rows = [] if tag_error else list_items(
        engine,
        project_slug=slug,
        item_type=filters["item_type"],
        priority=filters["priority"],
        status=filters["status"],
        component=filters["component"],
        search=filters["search"],
        tag=tag,
        limit=500,
    )

    # Sorting — list_items returns priority-then-newest; re-sort in Python for
    # the other modes (no extra query). "priority" keeps the core order.
    sort = _clean(request.args.get("sort")) or "status"
    if sort == "status":
        rows.sort(key=lambda r: (_STATUS_RANK.get(r["status"], 9), _PRI_RANK.get(r["priority"], 9), -r["id"]))
    elif sort == "recent":
        rows.sort(key=lambda r: (r.get("updated_at") or r.get("created_at")), reverse=True)
    elif sort == "created":
        rows.sort(key=lambda r: r.get("created_at"), reverse=True)
    elif sort == "title":
        rows.sort(key=lambda r: (r.get("title") or "").lower())

    ctx = {
        "slug": slug,
        "items": rows,
        "filters": filters,
        "tag": tag or "",
        "tag_error": tag_error,
        "sort": sort,
        "type_options": TYPE_OPTIONS,
        "priority_options": PRIORITY_OPTIONS,
        "status_options": STATUS_OPTIONS,
        "sort_options": SORT_OPTIONS,
    }

    # Return ONLY the table fragment when the filter form swaps it
    # (hx-target="#items-table" → HX-Target: items-table). Other htmx requests
    # — notably the SSE live-refresh which re-fetches #main (HX-Target: main) —
    # must receive the full page so `select:"#main"` finds the chrome.
    if request.headers.get("HX-Target") == "items-table":
        return render_template("partials/_items_table.html", **ctx)
    return render_template("items_list.html", **ctx)


@bp.route("/p/<slug>/items/<int:id>")
def detail(slug, id):
    engine = current_engine()
    item = get_item(engine, id)
    if item is None:
        abort(404)
    # Prev/next within the project, in the default list order (priority, newest).
    siblings = list_items(engine, project_slug=slug, limit=1000)
    ids = [r["id"] for r in siblings]
    prev_item = next_item = None
    if id in ids:
        i = ids.index(id)
        if i > 0:
            prev_item = siblings[i - 1]
        if i < len(ids) - 1:
            next_item = siblings[i + 1]
    return render_template(
        "item_detail.html", slug=slug, item=item, is_gate=is_gate,
        prev_item=prev_item, next_item=next_item,
    )
