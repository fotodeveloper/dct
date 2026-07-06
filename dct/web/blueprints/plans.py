"""Plan views — list + detail rendering the nested section tree recursively."""

from __future__ import annotations

from flask import Blueprint, abort, g, render_template

from dct.web.app import current_engine
from dct.core.plans import crud as plans_core
from dct.core import projects as projects_core

bp = Blueprint("plans", __name__)


@bp.route("/p/<slug>/plans")
def list(slug):  # noqa: A001 — endpoint name plans.list is part of the contract
    engine = current_engine()
    plans = plans_core.list_plans(engine, project_slug=slug)
    return render_template("plans/list.html", slug=slug, plans=plans)


@bp.route("/plans/<int:plan_id>")
def detail(plan_id):
    engine = current_engine()
    plan = plans_core.get_plan(engine, plan_id)
    if plan is None:
        abort(404)
    # /plans/<id> carries no slug; resolve the owning project so the sidebar +
    # breadcrumb scope to it (projects core has no get-by-id → map from list).
    slug = next(
        (p["slug"] for p in projects_core.list_projects(engine)
         if p["id"] == plan.get("project_id")),
        None,
    )
    g.current_slug = slug
    g.current_section = "plans"
    # get_plan returns a FLAT `sections` list (each with parent_section_id +
    # embedded `checkpoints`). Rebuild parent->children adjacency for the
    # recursive template; sections are already sort_order-ordered.
    children_map: dict[int, list] = {}
    roots = []
    for section in plan["sections"]:
        parent_id = section.get("parent_section_id")
        if parent_id is None:
            roots.append(section)
        else:
            children_map.setdefault(parent_id, []).append(section)
    return render_template(
        "plans/detail.html",
        plan=plan,
        roots=roots,
        children_map=children_map,
        progress=plan["progress"],
    )
