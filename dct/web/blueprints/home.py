"""Home view — cross-project Radar rollup cards (GET /)."""

from flask import Blueprint, render_template

from dct.core.analytics import radar as compute_radar
from dct.web.app import current_engine

bp = Blueprint("home", __name__)


@bp.route("/")
def radar():
    """Cross-project operational rollup: open/priority/in-progress/stale/handoffs."""
    data = compute_radar(current_engine())
    return render_template("home.html", radar=data)
