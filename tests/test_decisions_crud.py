"""Tests for dct.core.decisions — CRUD + supersede lifecycle."""

import pytest

from dct.core.decisions import (
    create_decision,
    delete_decision,
    get_decision,
    get_decision_by_ref_code,
    get_decision_by_ulid,
    list_decisions,
    supersede_decision,
    update_decision,
)
from dct.core.projects import create_project


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="dct", name="DCT", path="/tmp/dct")


# ── create_decision ──────────────────────────────────────────────────────────

def test_create_decision_basic(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="Use ULIDs for identifiers")
    assert d["kind"] == "adr"
    assert d["status"] == "proposed"
    assert len(d["ulid"]) == 26
    assert d["superseded_by"] is None


def test_create_decision_all_anchors(engine, proj):
    from dct.core.plans.crud import add_plan_section, create_plan
    from dct.core.sprints import create_sprint

    plan = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    section = add_plan_section(engine, plan["id"], "S", depth=2)
    sprint = create_sprint(engine, "dct", "Sprint 3")

    d = create_decision(
        engine, "dct", kind="sprint_amendment",
        title="Sprint 3 scope change",
        ref_code="SPRINT-3-AMEND-1",
        plan_id=plan["id"], plan_section_id=section["id"], sprint_id=sprint["id"],
        context="ctx", decision="dec",
        consequences="cons", alternatives="alt",
    )
    assert d["ref_code"] == "SPRINT-3-AMEND-1"
    assert d["plan_id"] == plan["id"]
    assert d["plan_section_id"] == section["id"]
    assert d["sprint_id"] == sprint["id"]
    assert d["context"] == "ctx"


def test_create_decision_duplicate_ref_code_raises(engine, proj):
    create_decision(engine, "dct", kind="adr", title="A", ref_code="ADR-001")
    with pytest.raises(ValueError, match="already exists"):
        create_decision(engine, "dct", kind="adr", title="B", ref_code="ADR-001")


def test_create_decision_invalid_kind_raises(engine, proj):
    with pytest.raises(ValueError, match="Invalid kind"):
        create_decision(engine, "dct", kind="rocket", title="X")


def test_create_decision_empty_title_raises(engine, proj):
    with pytest.raises(ValueError, match="cannot be empty"):
        create_decision(engine, "dct", kind="adr", title="  ")


# ── list / get ────────────────────────────────────────────────────────────────

def test_list_decisions_filters(engine, proj):
    from dct.core.sprints import create_sprint
    sprint = create_sprint(engine, "dct", "S")

    create_decision(engine, "dct", kind="adr", title="A")
    create_decision(engine, "dct", kind="inline_plan", title="B")
    create_decision(engine, "dct", kind="sprint_amendment", title="C", sprint_id=sprint["id"])
    create_decision(engine, "dct", kind="open_question", title="D")

    adrs = list_decisions(engine, project_slug="dct", kind="adr")
    assert {d["title"] for d in adrs} == {"A"}

    on_sprint = list_decisions(engine, project_slug="dct", sprint_id=sprint["id"])
    assert {d["title"] for d in on_sprint} == {"C"}


def test_get_decision_by_ulid(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X")
    fetched = get_decision_by_ulid(engine, d["ulid"])
    assert fetched is not None
    assert fetched["id"] == d["id"]


def test_get_decision_by_ref_code(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X", ref_code="ADR-007")
    fetched = get_decision_by_ref_code(engine, "dct", "ADR-007")
    assert fetched is not None
    assert fetched["id"] == d["id"]


def test_get_decision_missing_returns_none(engine):
    assert get_decision(engine, 9999) is None


# ── update_decision ───────────────────────────────────────────────────────────

def test_update_decision_status(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X")
    updated = update_decision(engine, d["id"], status="accepted")
    assert updated is not None
    assert updated["status"] == "accepted"


def test_update_decision_invalid_status_raises(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X")
    with pytest.raises(ValueError, match="Invalid status"):
        update_decision(engine, d["id"], status="blessed")


def test_update_decision_no_fields_raises(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X")
    with pytest.raises(ValueError, match="No valid fields"):
        update_decision(engine, d["id"])


# ── supersede_decision ───────────────────────────────────────────────────────

def test_supersede_marks_old_and_links_new(engine, proj):
    old = create_decision(engine, "dct", kind="adr", title="Old approach")
    new = create_decision(engine, "dct", kind="adr", title="New approach")

    updated = supersede_decision(engine, old["id"], new["id"])
    assert updated is not None
    assert updated["status"] == "superseded"
    assert updated["superseded_by"] == new["id"]


def test_supersede_self_raises(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X")
    with pytest.raises(ValueError, match="cannot supersede itself"):
        supersede_decision(engine, d["id"], d["id"])


def test_supersede_missing_old_raises(engine, proj):
    new = create_decision(engine, "dct", kind="adr", title="New")
    with pytest.raises(ValueError, match="#9999 not found"):
        supersede_decision(engine, 9999, new["id"])


def test_supersede_missing_new_raises(engine, proj):
    old = create_decision(engine, "dct", kind="adr", title="Old")
    with pytest.raises(ValueError, match="#8888 not found"):
        supersede_decision(engine, old["id"], 8888)


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_decision_soft(engine, proj):
    d = create_decision(engine, "dct", kind="adr", title="X")
    assert delete_decision(engine, d["id"]) is True
    assert get_decision(engine, d["id"]) is None
    # Still visible with include_deleted
    all_d = list_decisions(engine, project_slug="dct", include_deleted=True)
    assert any(x["id"] == d["id"] for x in all_d)
