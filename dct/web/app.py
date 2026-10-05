"""Flask application factory for the dct web viewer (v1, read-only).

Third adapter over ``dct.core`` — symmetric to the CLI and MCP server. Read-only
means the web layer NEVER calls ``create_tables()``: when it has to open its own
engine it uses ``bootstrap_engine(create=False)``. Schema ownership stays with
the CLI / MCP writers.
"""

import flask

from dct.core.engine import bootstrap_engine
from dct.web.serializers import parse_tags, render_markdown, to_iso

_BLUEPRINT_MODULES = (
    "home",
    "projects",
    "items",
    "sprints",
    "plans",
    "changelog",
    "decisions",
    "handoffs",
    "analytics",
    "events",
)


def create_app(engine=None, *, read_only: bool = True) -> flask.Flask:
    """Build the Flask app.

    When `engine` is None, open a fresh read-only engine via
    ``bootstrap_engine(create=False)`` — never creating tables. The engine is
    stashed on ``app.config["DCT_ENGINE"]`` for blueprints to read via
    :func:`current_engine`.
    """
    if engine is None:
        engine = bootstrap_engine(create=False)

    app = flask.Flask(__name__)
    app.config["DCT_ENGINE"] = engine
    app.config["DCT_READ_ONLY"] = read_only

    # Jinja filters — registered HERE only. Templates across all tasks rely on
    # these; no other task registers filters or adds a conditional fallback.
    app.jinja_env.filters["render_markdown"] = render_markdown
    app.jinja_env.filters["to_iso"] = to_iso
    app.jinja_env.filters["parse_tags"] = parse_tags
    # Shared blank-label fallback (K-64): a stored label of only whitespace /
    # format characters renders as no label.
    from dct.core.text import visible_label
    app.jinja_env.filters["visible_label"] = visible_label

    from importlib import import_module

    for name in _BLUEPRINT_MODULES:
        module = import_module(f"dct.web.blueprints.{name}")
        app.register_blueprint(module.bp)

    @app.context_processor
    def _inject_nav():
        """Expose the project list + current location to every template, so the
        sidebar can render the project switcher, highlight the active project,
        and show a per-project section sub-nav."""
        from dct.core.projects import list_projects

        view_args = (flask.request.view_args or {}) if flask.request else {}
        endpoint = (flask.request.endpoint or "") if flask.request else ""
        try:
            projects = list_projects(engine)
        except Exception:  # noqa: BLE001 — nav must render even if the DB hiccups
            projects = []
        # Detail routes (/sprints/<id>, /plans/<id>) have no slug in the URL but
        # set flask.g.current_slug/section so the sidebar + breadcrumb still scope
        # to the owning project. g wins; URL/endpoint are the fallback.
        return {
            "projects": projects,
            "current_slug": getattr(flask.g, "current_slug", None) or view_args.get("slug"),
            "current_section": getattr(flask.g, "current_section", None)
            or (endpoint.split(".")[0] if endpoint else ""),
        }

    return app


def current_engine():
    """Return the shared engine bound to the active Flask app."""
    return flask.current_app.config["DCT_ENGINE"]
