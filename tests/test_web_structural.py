"""Structural web views: sprint detail (members + checkpoint steps),
plan detail (recursive nested sections), changelog grouping, 404s."""

import pytest

from dct.core.projects import create_project
from dct.core.items import create_item
from dct.core.checkpoints import add_checkpoint
from dct.core.sprints import create_sprint, assign_item_to_sprint
from dct.core.plans.crud import create_plan, add_plan_section, add_plan_checkpoint
from dct.core.changelog import add_entry, release_entries


@pytest.fixture
def client(engine):
    """Flask test client over the in-memory engine, read-only."""
    from dct.web.app import create_app

    app = create_app(engine, read_only=True)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def project(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    return "proj-a"


# ── sprint detail: members + their checkpoint steps ───────────────────────────

def test_sprint_detail_shows_members_and_checkpoint_steps(engine, project, client):
    sprint = create_sprint(engine, project, name="Sprint One", code="1.0",
                           status="active", goal="ship the thing")
    it1 = create_item(engine, project, "feature", "Volume identity watcher", priority="P1")
    it2 = create_item(engine, project, "feature", "Catalog recursive import", priority="P1")
    add_checkpoint(engine, it1["id"], "VolumeProvider trait")
    add_checkpoint(engine, it1["id"], "diskutil plist parse")
    add_checkpoint(engine, it2["id"], "walk directory tree")
    assign_item_to_sprint(engine, it1["id"], sprint["id"])
    assign_item_to_sprint(engine, it2["id"], sprint["id"])

    resp = client.get(f"/sprints/{sprint['id']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Sprint header
    assert "Sprint One" in body
    assert "ship the thing" in body
    # Both member item titles
    assert "Volume identity watcher" in body
    assert "Catalog recursive import" in body
    # Checkpoint step text rendered (expandable steps)
    assert "VolumeProvider trait" in body
    assert "diskutil plist parse" in body
    assert "walk directory tree" in body


def test_sprint_list_shows_first_class_and_current_members(engine, project, client):
    # A planned first-class sprint renders in the "all sprints" section.
    create_sprint(engine, project, name="First Class Sprint", code="fc-1")
    # An item placed in the current (active) sprint renders in the current
    # section — sourced from item_sprints, the single source of truth (#216).
    from dct.core.sprints import set_current_sprint_items
    member = create_item(engine, project, "todo", "Current chore", priority="P2")
    set_current_sprint_items(engine, project, [member["id"]])

    resp = client.get(f"/p/{project}/sprints")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    current_section = body.index('data-model="current_sprint"')
    all_section = body.index('data-model="item_sprints"')

    # The planned first-class sprint's name renders in the all-sprints section.
    assert body.index("First Class Sprint") > all_section

    # The current-sprint member renders inside the current section (real data,
    # not just a label).
    member_pos = body.index("Current chore")
    assert member_pos > current_section
    assert f"#{member['id']}" in body


def test_sprint_detail_unknown_id_404(engine, project, client):
    assert client.get("/sprints/999999").status_code == 404


# ── plan detail: recursive nested sections ────────────────────────────────────

def test_plan_detail_renders_nested_child_section(engine, project, client):
    plan = create_plan(engine, project, slug="p1", title="Plan One", kind="plan")
    parent = add_plan_section(engine, plan["id"], heading="Parent Heading", depth=1)
    child = add_plan_section(engine, plan["id"], heading="Child Heading", depth=2,
                             parent_section_id=parent["id"])
    add_plan_checkpoint(engine, child["id"], "do the nested thing")

    resp = client.get(f"/plans/{plan['id']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Plan One" in body
    assert "Parent Heading" in body
    # The recursive macro must descend into children
    assert "Child Heading" in body
    assert "do the nested thing" in body


def test_plan_detail_unknown_id_404(engine, project, client):
    assert client.get("/plans/999999").status_code == 404


# ── changelog grouped by version, unreleased bucket first ─────────────────────

def test_changelog_grouped_unreleased_first(engine, project, client):
    add_entry(engine, project, "added", "shipped feature alpha")
    release_entries(engine, project, "1.0.0")
    add_entry(engine, project, "fixed", "pending bugfix beta")  # stays unreleased

    resp = client.get(f"/p/{project}/changelog")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "1.0.0" in body
    assert "shipped feature alpha" in body
    assert "pending bugfix beta" in body
    # Unreleased bucket renders before the 1.0.0 version group
    assert body.index("pending bugfix beta") < body.index("shipped feature alpha")
