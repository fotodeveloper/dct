"""Web: an item's short_title renders as a muted label next to its title
(#817) on the items list, item detail, sprint detail and sprint list pages —
and nothing renders for an item without one."""

import pytest

from dct.core.items import create_item
from dct.core.projects import create_project
from dct.core.sprints import get_current_sprint, set_current_sprint_items
from dct.web.app import create_app

LABEL = '<span class="short-title">Web label</span>'


@pytest.fixture
def client(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    return create_app(engine, read_only=True).test_client()


@pytest.fixture
def items(engine, client):
    labelled = create_item(engine, "proj-a", "todo", "Labelled", short_title="Web label")
    bare = create_item(engine, "proj-a", "todo", "Bare")
    set_current_sprint_items(engine, "proj-a", [labelled["id"], bare["id"]])
    return labelled, bare


def _body(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


def test_items_list_renders_label_once(client, items):
    body = _body(client, "/p/proj-a/items")
    assert body.count(LABEL) == 1


def test_item_detail_renders_label_only_for_labelled_item(client, items):
    labelled, bare = items
    assert LABEL in _body(client, f"/p/proj-a/items/{labelled['id']}")
    assert 'class="short-title"' not in _body(client, f"/p/proj-a/items/{bare['id']}")


def test_sprint_pages_render_label(engine, client, items):
    sprint_id = get_current_sprint(engine, "proj-a")["id"]
    assert _body(client, f"/sprints/{sprint_id}").count(LABEL) == 1
    assert _body(client, "/p/proj-a/sprints").count(LABEL) == 1


def test_label_is_html_escaped(engine, client):
    create_item(engine, "proj-a", "todo", "X", short_title="<b>bold</b>")
    body = _body(client, "/p/proj-a/items")
    assert "<b>bold</b>" not in body
    assert "&lt;b&gt;bold&lt;/b&gt;" in body


def test_changelog_related_item_renders_id_and_label(engine, client):
    """#801/C4: a changelog entry's related item shows `#id · label` — the
    short_title, or the title compacted to 50 chars when there is none."""
    from dct.core.changelog import add_entry

    labelled = create_item(engine, "proj-a", "todo", "Labelled", short_title="Web label")
    bare = create_item(engine, "proj-a", "todo", "B" * 80)
    add_entry(engine, "proj-a", "added", "with label", related_item_id=labelled["id"])
    add_entry(engine, "proj-a", "fixed", "with title", related_item_id=bare["id"])
    add_entry(engine, "proj-a", "fixed", "no item")
    body = _body(client, "/p/proj-a/changelog")
    assert f'#{labelled["id"]} · Web label</a>' in body
    assert f'#{bare["id"]} · {"B" * 49}…</a>' in body
    assert body.count('class="rel"') == 2
