"""Changelog view — grouped by version, unreleased bucket first."""

from __future__ import annotations

from flask import Blueprint, render_template

from dct.web.app import current_engine
from dct.core import changelog as changelog_core

bp = Blueprint("changelog", __name__)


@bp.route("/p/<slug>/changelog")
def list(slug):  # noqa: A001 — endpoint name changelog.list is part of the contract
    engine = current_engine()
    entries = changelog_core.list_entries(engine, slug)
    unreleased_count = changelog_core.count_unreleased(engine, slug)

    # Group by version, preserving created_at-desc order. version=None => unreleased.
    groups: dict = {}
    order: list = []
    for entry in entries:
        key = entry.get("version")  # None for unreleased
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)

    # Unreleased bucket (None) always rendered first, then versions in encounter order.
    version_groups = []
    if None in groups:
        version_groups.append((None, groups[None]))
    version_groups.extend((k, groups[k]) for k in order if k is not None)

    return render_template(
        "changelog/list.html",
        slug=slug,
        version_groups=version_groups,
        unreleased_count=unreleased_count,
    )
