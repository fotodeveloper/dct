"""Web view tests: home radar + project-home dashboard (read-only)."""

import pytest

from dct.core.projects import create_project
from dct.core.items import create_item
from dct.web.app import create_app


@pytest.fixture
def web_engine(engine):
    """Seed two projects with a handful of open items (generic placeholders)."""
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    create_project(engine, slug="proj-b", name="Project B", path="/tmp/proj-b")
    create_item(engine, "proj-a", "feature", "Alpha feature", priority="P1")
    create_item(engine, "proj-a", "issue", "Beta bug", priority="P2")
    create_item(engine, "proj-b", "todo", "Gamma task", priority="P2")
    return engine


@pytest.fixture
def client(web_engine):
    app = create_app(web_engine, read_only=True)
    app.config.update(TESTING=True)
    return app.test_client()


def test_home_lists_both_projects_with_open_count(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "proj-a" in body
    assert "proj-b" in body
    # proj-a has two open items → radar open_total == 2 rendered as "open 2"
    assert "open 2" in body
    # proj-b has one open item → "open 1"
    assert "open 1" in body


def test_project_home_renders_sprint_summary(client):
    resp = client.get("/p/proj-a")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "sprint completion" in body
    assert "total open" in body
    # proj-a has 2 open items, none flagged for sprint → "0/0" completion, 2 open
    assert "0/0" in body


def test_unknown_project_returns_404(client):
    resp = client.get("/p/does-not-exist")
    assert resp.status_code == 404
