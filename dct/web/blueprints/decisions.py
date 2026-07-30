"""Decision views — ADR list with supersede chains + status."""

from __future__ import annotations

from flask import Blueprint, render_template

from dct.core import decisions as decisions_core
from dct.web.app import current_engine

bp = Blueprint("decisions", __name__)


@bp.route("/p/<slug>/decisions")
def list(slug):
    engine = current_engine()
    decisions = decisions_core.list_decisions(engine, project_slug=slug)
    by_id = {d["id"]: d for d in decisions}
    return render_template(
        "decisions/list.html",
        slug=slug,
        decisions=decisions,
        by_id=by_id,
    )
