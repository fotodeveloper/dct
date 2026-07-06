"""Handoff views — ledger split into unconsumed inbox + consumed history."""

from __future__ import annotations

from flask import Blueprint, render_template

from dct.web.app import current_engine
from dct.core import handoff as handoff_core

bp = Blueprint("handoffs", __name__)


@bp.route("/p/<slug>/handoffs")
def list(slug):  # noqa: A001 — endpoint name handoffs.list is part of the contract
    engine = current_engine()
    all_handoffs = handoff_core.list_handoffs(
        engine, project_slug=slug, unconsumed_only=False
    )
    unconsumed = [h for h in all_handoffs if h.get("consumed_at") is None]
    consumed = [h for h in all_handoffs if h.get("consumed_at") is not None]
    return render_template(
        "handoffs/list.html",
        slug=slug,
        unconsumed=unconsumed,
        consumed=consumed,
    )
