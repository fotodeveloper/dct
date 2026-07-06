"""Items views — filterable list table + detail page (read-only).

GET /p/<slug>/items            filter form + htmx-swappable table
GET /p/<slug>/items/<int:id>   detail: markdown description, notes, checkpoints
"""

from flask import Blueprint, abort, render_template, request

from dct.web.app import current_engine
from dct.core.items import list_items, get_item

bp = Blueprint("items", __name__)

# Checkpoint kinds that are NOT gates. Anything else is a gate (conservative
# default — mirrors dct.core.checkpoints gate semantics).
GATE_NONKINDS = {"task", "cc-task"}


def is_gate(kind: str | None) -> bool:
    return (kind or "task") not in GATE_NONKINDS


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
    filters = {
        "item_type": _clean(request.args.get("type")),
        "priority": _clean(request.args.get("priority")),
        "status": _clean(request.args.get("status")),
        "component": _clean(request.args.get("component")),
        "search": _clean(request.args.get("q")),
    }
    rows = list_items(
        engine,
        project_slug=slug,
        item_type=filters["item_type"],
        priority=filters["priority"],
        status=filters["status"],
        component=filters["component"],
        search=filters["search"],
        limit=500,
    )

    # Optional tag filter — applied in Python because list_items has no tag
    # parameter; tags are already parsed to list[str] in the returned dicts.
    tag = _clean(request.args.get("tag"))
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]

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
