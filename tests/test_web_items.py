"""Web items blueprint: list filtering + detail rendering."""

import pytest

from dct.core.projects import create_project
from dct.core.items import create_item, add_note
from dct.web.app import create_app


@pytest.fixture
def app(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    return create_app(engine, read_only=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded(engine):
    """Three varied items in proj-a; returns the markdown-feature item id."""
    a = create_item(
        engine, "proj-a", "feature", "Alpha feature",
        description="Intro paragraph.\n\n- bullet one\n- bullet two",
        priority="P1", status="open", component="api", tags=["backend", "urgent"],
    )
    create_item(
        engine, "proj-a", "issue", "Beta bug",
        description="Something broke", priority="P2", status="in_progress",
        component="ui", tags=["frontend"],
    )
    create_item(
        engine, "proj-a", "todo", "Gamma chore",
        description="zeta keyword here", priority="P3", status="open", tags=[],
    )
    return a["id"]


def test_items_list_200_lists_titles(client, seeded):
    r = client.get("/p/proj-a/items")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Alpha feature" in body
    assert "Beta bug" in body
    assert "Gamma chore" in body


def test_filter_priority_narrows(client, seeded):
    r = client.get("/p/proj-a/items?priority=P1")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Alpha feature" in body
    assert "Beta bug" not in body
    assert "Gamma chore" not in body


def test_filter_status_narrows(client, seeded):
    r = client.get("/p/proj-a/items?status=in_progress")
    body = r.get_data(as_text=True)
    assert "Beta bug" in body
    assert "Alpha feature" not in body


def test_query_search_matches_description(client, seeded):
    r = client.get("/p/proj-a/items?q=zeta")
    body = r.get_data(as_text=True)
    assert "Gamma chore" in body
    assert "Alpha feature" not in body


def test_filter_swap_returns_partial_only(client, seeded):
    """The filter-form swap (hx-target="#items-table" → HX-Target: items-table)
    must return the table fragment, not the full chrome page."""
    full = client.get("/p/proj-a/items").get_data(as_text=True)
    assert "<html" in full.lower()
    frag = client.get(
        "/p/proj-a/items",
        headers={"HX-Request": "true", "HX-Target": "items-table"},
    ).get_data(as_text=True)
    assert "<html" not in frag.lower()
    assert "Alpha feature" in frag


def test_sse_refresh_returns_full_page(client, seeded):
    """The SSE live-refresh re-fetches #main (HX-Target: main). It MUST receive the
    full page so htmx's select:"#main" finds the chrome — regression guard for the
    items list blanking on background DB writes (the scenario live-refresh targets)."""
    body = client.get(
        "/p/proj-a/items",
        headers={"HX-Request": "true", "HX-Target": "main"},
    ).get_data(as_text=True)
    assert "<html" in body.lower()
    assert "Alpha feature" in body


def test_item_detail_renders_markdown_and_checkpoints(engine, client, seeded):
    from dct.core.checkpoints import add_checkpoint
    add_checkpoint(engine, seeded, "Wire the endpoint")
    add_checkpoint(engine, seeded, "User validates feature in fresh session", kind="dogfood")
    add_note(engine, seeded, "First investigation note")

    r = client.get(f"/p/proj-a/items/{seeded}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # markdown description rendered to HTML (bullet list -> <li>)
    assert "<li>bullet one</li>" in body
    # checkpoint rows present
    assert "Wire the endpoint" in body
    assert "User validates feature in fresh session" in body
    # gate badge for kind=dogfood
    assert "dogfood" in body
    assert "gate" in body.lower()
    # note timeline
    assert "First investigation note" in body


def test_item_detail_source_file_deeplink(engine, client):
    it = create_item(
        engine, "proj-a", "todo", "Has source",
        source_file="/tmp/proj-a/foo.py", source_lines="10-20",
    )
    r = client.get(f"/p/proj-a/items/{it['id']}")
    body = r.get_data(as_text=True)
    assert "vscode://file//tmp/proj-a/foo.py:10" in body


def test_item_detail_unknown_id_404(client, seeded):
    r = client.get("/p/proj-a/items/999999")
    assert r.status_code == 404


def test_markdown_is_sanitized(engine, client):
    it = create_item(
        engine, "proj-a", "todo", "XSS attempt",
        description="hello <script>alert(1)</script> world",
    )
    r = client.get(f"/p/proj-a/items/{it['id']}")
    body = r.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "alert(1)" in body  # escaped, still visible as text
