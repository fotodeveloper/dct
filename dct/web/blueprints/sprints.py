"""Sprint views — list (both models, labeled) + detail (members + steps)."""

from __future__ import annotations

from flask import Blueprint, abort, g, render_template

from dct.core import checkpoints as checkpoints_core
from dct.core import projects as projects_core
from dct.core import sprints as sprints_core
from dct.core.items import TERMINAL_STATUSES
from dct.web.app import current_engine

bp = Blueprint("sprints", __name__)


@bp.route("/p/<slug>/sprints")
def list(slug):
    engine = current_engine()
    first_class = sprints_core.list_sprints(engine, project_slug=slug)
    # Current sprint = the project's primary active sprint (newest started;
    # several may be active since G1); members come from item_sprints (one
    # source of truth, #216). No more legacy-boolean section.
    current = sprints_core.get_current_sprint(engine, slug)
    # Current-work view: terminal members keep their link (K-18, #413) but are
    # shown on the sprint DETAIL page, not in the current-sprint section.
    current_items = (
        [
            m
            for m in sprints_core.list_items_in_sprint(engine, current["id"])
            if m.get("status") not in TERMINAL_STATUSES
        ]
        if current
        else []
    )
    return render_template(
        "sprints/list.html",
        slug=slug,
        sprints=first_class,
        current=current,
        current_items=current_items,
    )


@bp.route("/sprints/<int:sprint_id>")
def detail(sprint_id):
    engine = current_engine()
    sprint = sprints_core.get_sprint(engine, sprint_id)
    if sprint is None:
        abort(404)
    # get_sprint returns project_id (int) but NO slug; resolve it so member
    # links can build url_for('items.detail', slug=..., id=...). projects core
    # has no get-by-id, so map id->slug from list_projects.
    slug = next(
        (p["slug"] for p in projects_core.list_projects(engine)
         if p["id"] == sprint["project_id"]),
        None,
    )
    # Scope the sidebar + breadcrumb to the owning project (no slug in the URL).
    g.current_slug = slug
    g.current_section = "sprints"
    members = sprints_core.list_items_in_sprint(engine, sprint_id)
    rows = []
    done = total = 0
    for it in members:
        cps = checkpoints_core.list_checkpoints(
            engine, it["id"], include_rejected=True
        )
        rows.append({"item": it, "checkpoints": cps})
        for cp in cps:
            if cp["status"] != "rejected":
                total += 1
                if cp["status"] == "done":
                    done += 1
    return render_template(
        "sprints/detail.html",
        sprint=sprint,
        slug=slug,
        rows=rows,
        cp_done=done,
        cp_total=total,
    )
