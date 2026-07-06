"""Analytics dashboard blueprint: Backlog Health + Velocity (read-only)."""
from __future__ import annotations

from flask import Blueprint, abort, render_template

from dct.core.analytics import backlog_health, velocity
from dct.core.projects import get_project
from dct.web.app import current_engine

bp = Blueprint("analytics", __name__)


@bp.route("/p/<slug>/analytics")
def dashboard(slug: str):
    """Render the two-tab BI dashboard for a single project.

    Tab 1 (Backlog Health) uses CSS/SVG bars over ``backlog_health`` dicts.
    Tab 2 (Velocity) emits the ``velocity`` dict as an embedded JSON payload
    that the vendored uPlot chart-init (static/app.js) reads on load.
    """
    engine = current_engine()
    project = get_project(engine, slug)
    if project is None:
        abort(404)

    health = backlog_health(engine, slug)
    vel = velocity(engine, slug)
    return render_template(
        "analytics.html",
        project=project,
        slug=slug,
        health=health,
        velocity=vel,
    )
