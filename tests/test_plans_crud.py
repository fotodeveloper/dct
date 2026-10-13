"""Tests for dct.core.plans.crud — plans + sections + checkpoints CRUD."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from dct.core.plans.crud import (
    add_plan_checkpoint,
    add_plan_section,
    complete_plan_checkpoint,
    create_plan,
    delete_plan,
    delete_plan_section,
    get_plan,
    get_plan_by_slug,
    get_plan_by_ulid,
    get_plan_checkpoint_by_ulid,
    get_plan_section,
    get_plan_section_by_ulid,
    list_plan_checkpoints,
    list_plan_sections,
    list_plans,
    promote_checkpoint_to_item,
    promote_section_to_item,
    promote_section_to_sprint,
    reject_plan_checkpoint,
    reopen_plan_checkpoint,
    set_plan_imported,
    update_plan,
    update_plan_section,
)
from dct.core.projects import create_project


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="dct", name="DCT", path="/tmp/dct")


# ── create_plan ───────────────────────────────────────────────────────────────

def test_create_plan_basic(engine, proj):
    p = create_plan(engine, "dct", slug="v0.2-design", title="v0.2 Design", kind="spec")
    assert p["slug"] == "v0.2-design"
    assert p["title"] == "v0.2 Design"
    assert p["kind"] == "spec"
    assert p["status"] == "active"
    assert len(p["ulid"]) == 26
    assert p["imported_at"] is None


def test_create_plan_with_explicit_ulid(engine, proj):
    ulid = "01HX9Z4K5M7PQRSTVWXYZ00001"
    p = create_plan(engine, "dct", slug="x", title="X", kind="plan", ulid=ulid)
    assert p["ulid"] == ulid


def test_create_plan_duplicate_slug_raises(engine, proj):
    create_plan(engine, "dct", slug="dup", title="A", kind="plan")
    with pytest.raises(ValueError, match="already exists"):
        create_plan(engine, "dct", slug="dup", title="B", kind="plan")


def test_create_plan_invalid_kind_raises(engine, proj):
    with pytest.raises(ValueError, match="Invalid kind"):
        create_plan(engine, "dct", slug="x", title="X", kind="invalid")


def test_create_plan_empty_slug_raises(engine, proj):
    with pytest.raises(ValueError, match="slug cannot be empty"):
        create_plan(engine, "dct", slug="   ", title="X", kind="plan")


# ── list_plans ────────────────────────────────────────────────────────────────

def test_list_plans_filters(engine, proj):
    create_plan(engine, "dct", slug="p1", title="A", kind="plan")
    create_plan(engine, "dct", slug="s1", title="B", kind="spec")
    create_plan(engine, "dct", slug="r1", title="C", kind="roadmap")

    specs = list_plans(engine, project_slug="dct", kind="spec")
    assert {p["slug"] for p in specs} == {"s1"}

    all_plans = list_plans(engine, project_slug="dct")
    assert len(all_plans) == 3


# ── get_plan / get_plan_by_ulid / get_plan_by_slug ───────────────────────────

def test_get_plan_includes_sections_and_checkpoints(engine, proj):
    p = create_plan(engine, "dct", slug="full", title="Full", kind="plan")
    s1 = add_plan_section(engine, p["id"], "Sprint 1", depth=2, section_type="sprint")
    add_plan_checkpoint(engine, s1["id"], "CP A")
    add_plan_checkpoint(engine, s1["id"], "CP B", status="done")

    full = get_plan(engine, p["id"])
    assert full is not None
    assert len(full["sections"]) == 1
    assert len(full["sections"][0]["checkpoints"]) == 2
    assert full["progress"]["total"] == 2
    assert full["progress"]["done"] == 1
    assert full["progress"]["pending"] == 1
    assert full["progress"]["rejected"] == 0
    assert full["progress"]["percent"] == 50


def test_get_plan_progress_with_rejected(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    add_plan_checkpoint(engine, s["id"], "A", status="done")
    add_plan_checkpoint(engine, s["id"], "B", status="done")
    add_plan_checkpoint(engine, s["id"], "C", status="rejected")
    add_plan_checkpoint(engine, s["id"], "D", status="pending")
    full = get_plan(engine, p["id"])
    # active = total - rejected = 4 - 1 = 3; done=2 ⇒ 67%
    assert full["progress"]["total"] == 4
    assert full["progress"]["done"] == 2
    assert full["progress"]["rejected"] == 1
    assert full["progress"]["active"] == 3
    assert full["progress"]["percent"] == 67


def test_get_plan_by_ulid(engine, proj):
    p = create_plan(engine, "dct", slug="u", title="U", kind="plan")
    fetched = get_plan_by_ulid(engine, p["ulid"])
    assert fetched is not None
    assert fetched["id"] == p["id"]


def test_get_plan_by_slug(engine, proj):
    p = create_plan(engine, "dct", slug="by-slug", title="T", kind="plan")
    fetched = get_plan_by_slug(engine, "dct", "by-slug")
    assert fetched is not None
    assert fetched["id"] == p["id"]


def test_get_plan_missing_returns_none(engine):
    assert get_plan(engine, 9999) is None


# ── update_plan / set_plan_imported / delete_plan ─────────────────────────────

def test_update_plan_archive_stamps_archived_at(engine, proj):
    p = create_plan(engine, "dct", slug="x", title="X", kind="plan")
    updated = update_plan(engine, p["id"], status="archived")
    assert updated is not None
    assert updated["status"] == "archived"
    assert updated["archived_at"] is not None


def test_set_plan_imported_stamps_imported_at(engine, proj):
    p = create_plan(engine, "dct", slug="x", title="X", kind="plan")
    assert p["imported_at"] is None
    stamped = set_plan_imported(engine, p["id"], source_hash="deadbeef")
    assert stamped is not None
    assert stamped["imported_at"] is not None
    assert stamped["source_hash"] == "deadbeef"


def test_delete_plan_soft(engine, proj):
    p = create_plan(engine, "dct", slug="x", title="X", kind="plan")
    assert delete_plan(engine, p["id"]) is True
    assert get_plan(engine, p["id"]) is None


# ── add_plan_section ──────────────────────────────────────────────────────────

def test_add_plan_section_basic(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Sprint 1", depth=2, section_type="sprint")
    assert s["heading"] == "Sprint 1"
    assert s["depth"] == 2
    assert s["section_type"] == "sprint"
    assert len(s["ulid"]) == 26


def test_add_plan_section_invalid_type(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    with pytest.raises(ValueError, match="Invalid section_type"):
        add_plan_section(engine, p["id"], "X", depth=2, section_type="rocket")


def test_add_plan_section_depth_out_of_range(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    with pytest.raises(ValueError, match="Invalid depth"):
        add_plan_section(engine, p["id"], "X", depth=7)
    with pytest.raises(ValueError, match="Invalid depth"):
        add_plan_section(engine, p["id"], "X", depth=0)


def test_add_plan_section_with_parent(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    parent = add_plan_section(engine, p["id"], "H2", depth=2)
    child = add_plan_section(
        engine, p["id"], "H3", depth=3, parent_section_id=parent["id"]
    )
    assert child["parent_section_id"] == parent["id"]


def test_list_plan_sections_filter_by_parent(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    root = add_plan_section(engine, p["id"], "Root", depth=1)
    c1 = add_plan_section(engine, p["id"], "Child1", depth=2, parent_section_id=root["id"])
    c2 = add_plan_section(engine, p["id"], "Child2", depth=2, parent_section_id=root["id"])
    unrelated = add_plan_section(engine, p["id"], "Other", depth=2)

    under_root = list_plan_sections(engine, p["id"], parent_section_id=root["id"])
    assert {s["id"] for s in under_root} == {c1["id"], c2["id"]}
    assert unrelated["id"] not in {s["id"] for s in under_root}


def test_get_plan_section_by_ulid(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    fetched = get_plan_section_by_ulid(engine, s["ulid"])
    assert fetched is not None
    assert fetched["id"] == s["id"]


def test_update_plan_section_status(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    updated = update_plan_section(engine, s["id"], status="in_progress")
    assert updated is not None
    assert updated["status"] == "in_progress"


def test_update_plan_section_invalid_confidence(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    with pytest.raises(ValueError, match="section_type_confidence"):
        update_plan_section(engine, s["id"], section_type_confidence="middling")


def test_delete_plan_section_soft(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    assert delete_plan_section(engine, s["id"]) is True
    assert get_plan_section(engine, s["id"]) is None


# ── plan_checkpoints ──────────────────────────────────────────────────────────

def test_add_plan_checkpoint_basic(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    cp = add_plan_checkpoint(engine, s["id"], "Do the thing")
    assert cp["text"] == "Do the thing"
    assert cp["status"] == "pending"
    assert len(cp["ulid"]) == 26


def test_add_plan_checkpoint_empty_raises(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    with pytest.raises(ValueError, match="cannot be empty"):
        add_plan_checkpoint(engine, s["id"], "   ")


def test_list_plan_checkpoints_by_plan_and_status(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    add_plan_checkpoint(engine, s["id"], "A", status="done")
    add_plan_checkpoint(engine, s["id"], "B", status="pending")
    add_plan_checkpoint(engine, s["id"], "C", status="done")

    all_cp = list_plan_checkpoints(engine, plan_id=p["id"])
    assert len(all_cp) == 3

    done_cp = list_plan_checkpoints(engine, plan_id=p["id"], status="done")
    assert len(done_cp) == 2


def test_get_plan_checkpoint_by_ulid(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    cp = add_plan_checkpoint(engine, s["id"], "Do")
    fetched = get_plan_checkpoint_by_ulid(engine, cp["ulid"])
    assert fetched is not None
    assert fetched["id"] == cp["id"]


def test_complete_reject_reopen_cycle(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    cp = add_plan_checkpoint(engine, s["id"], "Work")

    done = complete_plan_checkpoint(engine, cp["id"])
    assert done["status"] == "done"

    rejected = reject_plan_checkpoint(engine, cp["id"])
    assert rejected["status"] == "rejected"

    reopened = reopen_plan_checkpoint(engine, cp["id"])
    assert reopened["status"] == "pending"


def test_complete_missing_checkpoint_returns_none(engine):
    assert complete_plan_checkpoint(engine, 9999) is None


# ── promote section/checkpoint → item ────────────────────────────────────────

def test_promote_section_to_item(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Big feature", depth=2, section_type="sprint")

    item = promote_section_to_item(engine, s["id"], item_type="feature", priority="P1")
    assert item["item_type"] == "feature"
    assert item["priority"] == "P1"
    assert item["title"] == "Big feature"

    # Section is back-linked
    updated_section = get_plan_section(engine, s["id"])
    assert updated_section is not None
    assert updated_section["converted_to_item_id"] == item["id"]


def test_promote_section_custom_title(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Generic heading", depth=2)
    item = promote_section_to_item(
        engine, s["id"], item_type="todo", title="More specific title"
    )
    assert item["title"] == "More specific title"


def test_promote_checkpoint_to_item(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "S", depth=2)
    cp = add_plan_checkpoint(engine, s["id"], "Important task")
    item = promote_checkpoint_to_item(engine, cp["id"], item_type="todo")
    assert item["title"] == "Important task"
    cp_after = get_plan_checkpoint_by_ulid(engine, cp["ulid"])
    assert cp_after is not None
    assert cp_after["converted_to_item_id"] == item["id"]


def test_promote_section_missing_raises(engine):
    with pytest.raises(ValueError, match="not found"):
        promote_section_to_item(engine, 9999, item_type="todo")


def test_promote_checkpoint_missing_raises(engine):
    with pytest.raises(ValueError, match="not found"):
        promote_checkpoint_to_item(engine, 9999, item_type="todo")


# ── promote_section_to_item: one transaction (#909 K-24) ────────────────────


class _BackLinkFails:
    """`plan_sections` whose UPDATE (the back-link write) fails; reads and
    the row lock still see the real table."""

    def __init__(self, table):
        self._table = table

    def __getattr__(self, name):
        return getattr(self._table, name)

    def __clause_element__(self):
        return self._table

    def update(self):
        raise RuntimeError("back-link write failed")


def _item_count(engine) -> int:
    from dct.db import items

    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(items)).scalar_one()


def test_a_failing_back_link_write_leaves_no_section_item(engine, proj, monkeypatch):
    """The item and the back-link land in ONE transaction: a failure of the
    back-link UPDATE rolls the item back (it used to stay, orphaned)."""
    from dct.core.plans import crud

    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Big feature", depth=2)
    monkeypatch.setattr(crud, "plan_sections", _BackLinkFails(crud.plan_sections))

    with pytest.raises(RuntimeError, match="back-link write failed"):
        promote_section_to_item(engine, s["id"], item_type="feature")

    monkeypatch.undo()
    assert _item_count(engine) == 0
    section = get_plan_section(engine, s["id"])
    assert section is not None
    assert section["converted_to_item_id"] is None


def test_promoting_a_deleted_section_is_refused_and_creates_nothing(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Gone", depth=2)
    assert delete_plan_section(engine, s["id"])
    with pytest.raises(ValueError, match="not found"):
        promote_section_to_item(engine, s["id"], item_type="todo")
    assert _item_count(engine) == 0


def test_a_section_promotion_waiting_on_a_deleted_section_creates_nothing(
    concurrent_engine, lock_probe,
):
    """The competitor is PROVEN blocked on the section row while the holder
    soft-deletes it; after the commit it must see the deletion and refuse."""
    from conftest import hold_row_and_race

    from dct.db import lock_row, plan_sections

    eng = concurrent_engine
    create_project(eng, slug="dct", name="DCT", path="/tmp/dct")
    p = create_plan(eng, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(eng, p["id"], "Raced", depth=2)

    outcome = hold_row_and_race(
        eng, lock_probe,
        lambda conn: lock_row(conn, plan_sections, s["id"]),
        lambda: promote_section_to_item(eng, s["id"], item_type="todo"),
        lambda conn: conn.execute(
            plan_sections.update().where(plan_sections.c.id == s["id"])
            .values(deleted_at=datetime.now(UTC))
        ),
    )

    assert isinstance(outcome, ValueError), outcome
    assert "not found" in str(outcome)
    assert _item_count(eng) == 0


def test_a_repeated_section_promotion_is_not_refused(engine, proj):
    """Re-promotion policy unchanged: each call creates a new item and the
    back-link names the latest one."""
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Twice", depth=2)
    first = promote_section_to_item(engine, s["id"], item_type="todo")
    second = promote_section_to_item(engine, s["id"], item_type="todo")
    assert second["id"] != first["id"]
    section = get_plan_section(engine, s["id"])
    assert section is not None
    assert section["converted_to_item_id"] == second["id"]


# ── promote_section_to_sprint ────────────────────────────────────────────────

def test_promote_section_to_sprint_creates_sprint_and_backlinks(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Sprint 1 — Hub Redesign",
                        depth=2, section_type="sprint")

    sprint = promote_section_to_sprint(
        engine, s["id"], code="v0.2-hub", goal="Ship the new hub",
    )
    assert sprint["code"] == "v0.2-hub"
    assert sprint["name"] == "Sprint 1 — Hub Redesign"  # derived from heading
    assert sprint["goal"] == "Ship the new hub"
    assert sprint["kind"] == "sprint"

    # Section is back-linked via sprint_id (existing FK, no new column)
    updated_section = get_plan_section(engine, s["id"])
    assert updated_section is not None
    assert updated_section["sprint_id"] == sprint["id"]


def test_promote_section_to_sprint_override_name(engine, proj):
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Sprint 1", depth=2, section_type="sprint")

    sprint = promote_section_to_sprint(
        engine, s["id"], code="s1", name="v0.2 Shipping Sprint",
    )
    assert sprint["name"] == "v0.2 Shipping Sprint"


def test_promote_section_to_sprint_duplicate_code_raises(engine, proj):
    from dct.core.sprints import create_sprint
    create_sprint(engine, "dct", "Existing", code="taken")

    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Sprint X", depth=2)

    with pytest.raises(ValueError, match="already exists"):
        promote_section_to_sprint(engine, s["id"], code="taken")


def test_promote_section_to_sprint_missing_section_raises(engine):
    with pytest.raises(ValueError, match="#9999 not found"):
        promote_section_to_sprint(engine, 9999, code="x")


def test_promote_section_to_sprint_kind_override(engine, proj):
    """section_type='sprint' is the default label, but kind on the sprint
    row can be overridden — e.g. convert a 'phase' section into a sprint-
    kind=phase record, preserving semantic distinction."""
    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Phase 1 Foundation",
                        depth=2, section_type="phase")

    sprint = promote_section_to_sprint(
        engine, s["id"], code="phase-1", kind="phase",
    )
    assert sprint["kind"] == "phase"


# ── promote_*: title length follows the items column (#613) ──────────────────

def test_promote_section_truncates_over_long_heading(engine, proj):
    from dct.core.items import MAX_TITLE_LENGTH

    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "h" * (MAX_TITLE_LENGTH + 100), depth=2)
    item = promote_section_to_item(engine, s["id"], item_type="todo")
    assert len(item["title"]) == MAX_TITLE_LENGTH


def test_promote_checkpoint_truncates_over_long_text(engine, proj):
    from dct.core.items import MAX_TITLE_LENGTH

    p = create_plan(engine, "dct", slug="p", title="P", kind="plan")
    s = add_plan_section(engine, p["id"], "Section", depth=2)
    cp = add_plan_checkpoint(engine, s["id"], "c" * (MAX_TITLE_LENGTH + 100))
    item = promote_checkpoint_to_item(engine, cp["id"], item_type="todo")
    assert len(item["title"]) == MAX_TITLE_LENGTH
