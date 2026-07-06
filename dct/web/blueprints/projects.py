"""Project home view — ready-made sprint-review dashboard (GET /p/<slug>)."""

from flask import Blueprint, abort, render_template

from dct.core.projects import get_project
from dct.core.sprint import get_sprint_review
from dct.web.app import current_engine

bp = Blueprint("projects", __name__)


@bp.route("/p/<slug>")
def home(slug):
    """Per-project dashboard backed by sprint.get_sprint_review.

    A missing/soft-deleted slug yields 404 (distinct from an empty project).
    """
    engine = current_engine()
    project = get_project(engine, slug)
    if project is None:
        abort(404)
    review = get_sprint_review(engine, slug)
    return render_template("project_home.html", project=project, review=review)
