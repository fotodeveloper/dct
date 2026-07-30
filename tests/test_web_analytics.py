import json
import re

import pytest

from dct.core.changelog import add_entry
from dct.core.items import create_item
from dct.core.projects import create_project
from dct.web.app import create_app


@pytest.fixture
def client(engine):
    """A test client over a project seeded with distribution-bearing data."""
    create_project(engine, slug="test", name="Test Project", path="/tmp/test")
    # 3 P1 todos sharing one component → by_priority["P1"] == 3, hotspot on "api".
    for _ in range(3):
        create_item(engine, "test", "todo", "A todo", priority="P1", component="api")
    create_item(engine, "test", "feature", "A feature", priority="P2", component="ui")
    add_entry(engine, "test", "added", "seed changelog entry")

    app = create_app(engine, read_only=True)
    app.config.update(TESTING=True)
    return app.test_client()


def test_analytics_renders_backlog_distribution(client):
    resp = client.get("/p/test/analytics")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # A distribution label is rendered through the CSS bars macro.
    assert "P1" in body
    # The computed distribution count (three P1 todos) is rendered in the
    # bars-macro count cell — value-pinned, not a bare substring match.
    assert '<span class="bar-count">3</span>' in body
    # And the embedded velocity payload parses and carries velocity()'s keys.
    m = re.search(
        r'<script id="velocity-data" type="application/json">(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert m, "velocity JSON payload missing"
    payload = json.loads(m.group(1))
    for key in ("resolved_per_week", "created_per_week", "backlog_growth"):
        assert key in payload, f"missing velocity key: {key}"


def test_analytics_embeds_velocity_json(client):
    resp = client.get("/p/test/analytics")
    body = resp.get_data(as_text=True)
    # uPlot mount element present.
    assert 'id="velocity-chart"' in body
    # Embedded series payload is valid JSON with every velocity() key.
    m = re.search(
        r'<script id="velocity-data" type="application/json">(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert m, "velocity JSON payload missing"
    payload = json.loads(m.group(1))
    for key in (
        "resolved_per_week",
        "created_per_week",
        "backlog_growth",
        "changelog_per_week",
        "release_cadence",
        "sprint_completion",
    ):
        assert key in payload, f"missing velocity key: {key}"


def test_analytics_unknown_project_returns_404(client):
    assert client.get("/p/nope/analytics").status_code == 404
