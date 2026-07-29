"""Tests for dct.core.sprints — sprints CRUD + item_sprints links."""

import pytest

from dct.core.items import create_item
from dct.core.projects import create_project
from dct.core.sprints import (
    ALLOWED_TRANSITIONS,
    assign_item_to_sprint,
    create_sprint,
    delete_sprint,
    get_current_sprint,
    get_sprint,
    get_sprint_by_code,
    list_items_in_sprint,
    list_sprints,
    list_sprints_for_item,
    remove_item_from_sprint,
    update_sprint,
)


def _make_status(engine, name, target):
    """Create a fresh sprint and drive it to `target` status via legal edges.

    Uses the shortest legal path so tests can start from any lifecycle state:
      planned  → (as created)
      active   → activate
      deferred → activate → defer
      completed→ activate → complete
      abandoned→ abandon (planned → abandoned is legal)
    """
    s = create_sprint(engine, "dct", name)  # planned
    if target == "planned":
        return s
    if target == "abandoned":
        return update_sprint(engine, s["id"], status="abandoned")
    s = update_sprint(engine, s["id"], status="active")
    if target == "active":
        return s
    if target == "deferred":
        return update_sprint(engine, s["id"], status="deferred")
    if target == "completed":
        return update_sprint(engine, s["id"], status="completed")
    raise AssertionError(f"unhandled target {target}")


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="dct", name="DCT", path="/tmp/dct")


# ── create_sprint ─────────────────────────────────────────────────────────────

def test_create_sprint_basic(engine, proj):
    s = create_sprint(engine, "dct", "Sprint 1: Foundation")
    assert s["name"] == "Sprint 1: Foundation"
    assert s["kind"] == "sprint"
    assert s["status"] == "planned"
    assert s["started_at"] is None


def test_create_sprint_active_sets_started_at(engine, proj):
    s = create_sprint(engine, "dct", "Sprint X", status="active")
    assert s["status"] == "active"
    assert s["started_at"] is not None


def test_create_sprint_with_code(engine, proj):
    s = create_sprint(engine, "dct", "v0.1 ship", code="v0.1")
    assert s["code"] == "v0.1"


def test_create_sprint_duplicate_code_raises(engine, proj):
    create_sprint(engine, "dct", "First", code="dup")
    with pytest.raises(ValueError, match="already exists"):
        create_sprint(engine, "dct", "Second", code="dup")


def test_create_sprint_empty_name_raises(engine, proj):
    with pytest.raises(ValueError, match="cannot be empty"):
        create_sprint(engine, "dct", "   ")


def test_create_sprint_invalid_kind_raises(engine, proj):
    with pytest.raises(ValueError, match="Invalid kind"):
        create_sprint(engine, "dct", "Bad", kind="rocket")


# ── list / get ────────────────────────────────────────────────────────────────

def test_list_sprints_filters_by_status(engine, proj):
    create_sprint(engine, "dct", "Planned A")
    create_sprint(engine, "dct", "Active B", status="active")
    create_sprint(engine, "dct", "Planned C")

    planned = list_sprints(engine, project_slug="dct", status="planned")
    assert {s["name"] for s in planned} == {"Planned A", "Planned C"}

    active = list_sprints(engine, project_slug="dct", status="active")
    assert [s["name"] for s in active] == ["Active B"]


def test_get_sprint_by_code(engine, proj):
    create_sprint(engine, "dct", "Target", code="target")
    s = get_sprint_by_code(engine, "dct", "target")
    assert s is not None
    assert s["name"] == "Target"


def test_get_sprint_missing_returns_none(engine):
    assert get_sprint(engine, 9999) is None


# ── update_sprint — lifecycle invariants ─────────────────────────────────────

def test_update_sprint_to_active_sets_started_at(engine, proj):
    s = create_sprint(engine, "dct", "Dormant")
    assert s["started_at"] is None
    updated = update_sprint(engine, s["id"], status="active")
    assert updated is not None
    assert updated["status"] == "active"
    assert updated["started_at"] is not None


def test_update_sprint_to_completed_sets_ended_at(engine, proj):
    s = create_sprint(engine, "dct", "Active", status="active")
    updated = update_sprint(engine, s["id"], status="completed")
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["ended_at"] is not None


def test_update_sprint_completed_preserves_started_at(engine, proj):
    s = create_sprint(engine, "dct", "Active", status="active")
    original_start = s["started_at"]
    updated = update_sprint(engine, s["id"], status="completed")
    assert updated["started_at"] == original_start


def test_update_sprint_noop_empty_raises(engine, proj):
    s = create_sprint(engine, "dct", "X")
    with pytest.raises(ValueError, match="No valid fields"):
        update_sprint(engine, s["id"])


def test_update_sprint_blank_name_raises(engine, proj):
    """update_sprint must reject a whitespace-only name, exactly like
    create_sprint — otherwise a sprint's name could be blanked to '   '."""
    s = create_sprint(engine, "dct", "Named")
    with pytest.raises(ValueError, match="cannot be empty"):
        update_sprint(engine, s["id"], name="   ")
    # The name is left untouched by the rejected update.
    assert get_sprint(engine, s["id"])["name"] == "Named"


def test_update_sprint_strips_name_padding(engine, proj):
    """A padded name is trimmed on update, matching create_sprint's strip."""
    s = create_sprint(engine, "dct", "Named")
    updated = update_sprint(engine, s["id"], name="  Padded  ")
    assert updated is not None
    assert updated["name"] == "Padded"


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete_sprint_soft(engine, proj):
    s = create_sprint(engine, "dct", "Gone")
    assert delete_sprint(engine, s["id"]) is True
    assert get_sprint(engine, s["id"]) is None
    all_sprints = list_sprints(engine, project_slug="dct", include_deleted=True)
    assert any(x["id"] == s["id"] for x in all_sprints)


# ── assign_item_to_sprint / remove_item_from_sprint ───────────────────────────

def test_assign_item_to_sprint_creates_link(engine, proj):
    s = create_sprint(engine, "dct", "S")
    item = create_item(engine, "dct", "issue", "Bug")
    link = assign_item_to_sprint(engine, item["id"], s["id"])
    assert link["item_id"] == item["id"]
    assert link["sprint_id"] == s["id"]
    assert link["removed_at"] is None


def test_assign_is_idempotent(engine, proj):
    s = create_sprint(engine, "dct", "S")
    item = create_item(engine, "dct", "issue", "Bug")
    a = assign_item_to_sprint(engine, item["id"], s["id"])
    b = assign_item_to_sprint(engine, item["id"], s["id"])
    assert a["item_id"] == b["item_id"]
    items_in = list_items_in_sprint(engine, s["id"])
    assert len(items_in) == 1


def test_assign_reactivates_removed_link(engine, proj):
    s = create_sprint(engine, "dct", "S")
    item = create_item(engine, "dct", "issue", "Bug")
    assign_item_to_sprint(engine, item["id"], s["id"])
    remove_item_from_sprint(engine, item["id"], s["id"])
    reactivated = assign_item_to_sprint(engine, item["id"], s["id"])
    assert reactivated["removed_at"] is None


def test_remove_item_from_sprint_marks_removed_at(engine, proj):
    s = create_sprint(engine, "dct", "S")
    item = create_item(engine, "dct", "issue", "Bug")
    assign_item_to_sprint(engine, item["id"], s["id"])
    removed = remove_item_from_sprint(engine, item["id"], s["id"])
    assert removed is not None
    assert removed["removed_at"] is not None


def test_remove_nonexistent_link_returns_none(engine, proj):
    s = create_sprint(engine, "dct", "S")
    item = create_item(engine, "dct", "issue", "Bug")
    assert remove_item_from_sprint(engine, item["id"], s["id"]) is None


def test_assign_missing_item_raises(engine, proj):
    s = create_sprint(engine, "dct", "S")
    with pytest.raises(ValueError, match="Item #9999 not found"):
        assign_item_to_sprint(engine, 9999, s["id"])


def test_assign_missing_sprint_raises(engine, proj):
    item = create_item(engine, "dct", "issue", "Bug")
    with pytest.raises(ValueError, match="Sprint #9999 not found"):
        assign_item_to_sprint(engine, item["id"], 9999)


# ── list_items_in_sprint / list_sprints_for_item ──────────────────────────────

def test_list_items_in_sprint_excludes_removed_by_default(engine, proj):
    s = create_sprint(engine, "dct", "S")
    a = create_item(engine, "dct", "issue", "A")
    b = create_item(engine, "dct", "issue", "B")
    assign_item_to_sprint(engine, a["id"], s["id"])
    assign_item_to_sprint(engine, b["id"], s["id"])
    remove_item_from_sprint(engine, a["id"], s["id"])

    active_items = list_items_in_sprint(engine, s["id"])
    assert {i["id"] for i in active_items} == {b["id"]}

    all_items = list_items_in_sprint(engine, s["id"], include_removed=True)
    assert {i["id"] for i in all_items} == {a["id"], b["id"]}


def test_list_sprints_for_item_shows_both_assignments(engine, proj):
    s1 = create_sprint(engine, "dct", "S1")
    s2 = create_sprint(engine, "dct", "S2")
    item = create_item(engine, "dct", "issue", "X")
    assign_item_to_sprint(engine, item["id"], s1["id"])
    assign_item_to_sprint(engine, item["id"], s2["id"])
    sprints_for_item = list_sprints_for_item(engine, item["id"])
    assert {s["id"] for s in sprints_for_item} == {s1["id"], s2["id"]}


# ── current sprint: multiple actives allowed, primary = newest started (G1) ────

def test_get_current_sprint_none_initially(engine, proj):
    from dct.core.sprints import get_current_sprint
    assert get_current_sprint(engine, "dct") is None


def test_get_current_sprint_ignores_planned(engine, proj):
    """Only an ACTIVE sprint is the current one — planned/completed don't count."""
    from dct.core.sprints import get_current_sprint
    create_sprint(engine, "dct", "Planned")           # planned
    assert get_current_sprint(engine, "dct") is None


def test_get_current_sprint_returns_the_active_one(engine, proj):
    from dct.core.sprints import get_current_sprint
    create_sprint(engine, "dct", "Planned")            # not current
    active = create_sprint(engine, "dct", "Now", status="active")
    cur = get_current_sprint(engine, "dct")
    assert cur is not None
    assert cur["id"] == active["id"]


def test_second_active_sprint_allowed(engine, proj):
    """G1 (#413 K-5): the ≤1-active cap is lifted — parallel active sprints
    are legal and both stay active."""
    a = create_sprint(engine, "dct", "First", status="active")
    b = create_sprint(engine, "dct", "Second", status="active")
    from dct.core.sprints import list_sprints
    active = [s for s in list_sprints(engine, "dct", status="active")]
    assert {s["id"] for s in active} == {a["id"], b["id"]}


def test_activating_second_sprint_allowed(engine, proj):
    first = create_sprint(engine, "dct", "First", status="active")
    other = create_sprint(engine, "dct", "Other")      # planned
    activated = update_sprint(engine, other["id"], status="active")
    assert activated["status"] == "active"
    assert activated["started_at"] is not None
    from dct.core.sprints import list_sprints
    active_ids = {s["id"] for s in list_sprints(engine, "dct", status="active")}
    assert active_ids == {first["id"], other["id"]}


def test_get_current_sprint_primary_is_newest_started(engine, proj):
    """With several actives, the 'current' (primary) sprint is the most
    recently started; equal started_at falls back to the higher id."""
    from dct.core.sprints import get_current_sprint
    from dct.db import sprints as sprints_t

    a = create_sprint(engine, "dct", "Older", status="active")
    b = create_sprint(engine, "dct", "Newer", status="active")
    assert get_current_sprint(engine, "dct")["id"] == b["id"]

    # Force identical started_at → id tie-break must pick the higher id.
    with engine.begin() as conn:
        conn.execute(
            sprints_t.update()
            .where(sprints_t.c.id.in_([a["id"], b["id"]]))
            .values(started_at=a["started_at"])
        )
    assert get_current_sprint(engine, "dct")["id"] == b["id"]


def test_list_active_sprints_orders_newest_first(engine, proj):
    from dct.core.sprints import list_active_sprints
    assert list_active_sprints(engine, "dct") == []
    a = create_sprint(engine, "dct", "A", status="active")
    create_sprint(engine, "dct", "Planned")            # excluded
    b = create_sprint(engine, "dct", "B", status="active")
    rows = list_active_sprints(engine, "dct")
    assert [r["id"] for r in rows] == [b["id"], a["id"]]


def test_list_active_sprints_excludes_deferred_and_deleted(engine, proj):
    from dct.core.sprints import delete_sprint, list_active_sprints
    a = create_sprint(engine, "dct", "A", status="active")
    b = create_sprint(engine, "dct", "B", status="active")
    c = create_sprint(engine, "dct", "C", status="active")
    update_sprint(engine, a["id"], status="deferred")
    delete_sprint(engine, b["id"])
    assert [r["id"] for r in list_active_sprints(engine, "dct")] == [c["id"]]


def test_updating_the_sole_active_sprint_does_not_trip_invariant(engine, proj):
    """Editing the one active sprint must not count it against itself."""
    s = create_sprint(engine, "dct", "Solo", status="active")
    updated = update_sprint(engine, s["id"], name="Solo renamed")
    assert updated is not None
    assert updated["name"] == "Solo renamed"
    assert updated["status"] == "active"


def test_active_invariant_is_per_project(engine):
    """Two projects may each have their own active (current) sprint."""
    from dct.core.sprints import get_current_sprint
    create_project(engine, slug="p1", name="P1", path="/tmp/p1")
    create_project(engine, slug="p2", name="P2", path="/tmp/p2")
    create_sprint(engine, "p1", "A1", status="active")
    create_sprint(engine, "p2", "A2", status="active")  # must NOT raise
    c1 = get_current_sprint(engine, "p1")
    c2 = get_current_sprint(engine, "p2")
    assert c1 is not None and c1["name"] == "A1"
    assert c2 is not None and c2["name"] == "A2"


# ── ensure / set / clear current sprint (the /handoff + /pickup surface) ───────

def test_ensure_current_sprint_creates_active_when_none(engine, proj):
    from dct.core.sprints import ensure_current_sprint, get_current_sprint
    s = ensure_current_sprint(engine, "dct")
    assert s["status"] == "active"
    cur = get_current_sprint(engine, "dct")
    assert cur is not None and cur["id"] == s["id"]


def test_ensure_current_sprint_returns_existing(engine, proj):
    from dct.core.sprints import ensure_current_sprint
    first = ensure_current_sprint(engine, "dct")
    second = ensure_current_sprint(engine, "dct")
    assert first["id"] == second["id"]
    assert len(list_sprints(engine, project_slug="dct", status="active")) == 1


def test_set_current_sprint_items_assigns(engine, proj):
    from dct.core.sprints import set_current_sprint_items
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    res = set_current_sprint_items(engine, "dct", [a["id"], b["id"]])
    assert res["size"] == 2
    members = {i["id"] for i in list_items_in_sprint(engine, res["sprint_id"])}
    assert members == {a["id"], b["id"]}


def test_set_current_sprint_items_replaces_atomically(engine, proj):
    from dct.core.sprints import set_current_sprint_items
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    c = create_item(engine, "dct", "feature", "C")
    set_current_sprint_items(engine, "dct", [a["id"], b["id"]])
    res = set_current_sprint_items(engine, "dct", [c["id"]])  # a,b out; c in
    members = {i["id"] for i in list_items_in_sprint(engine, res["sprint_id"])}
    assert members == {c["id"]}


def test_set_current_sprint_items_autocreates_sprint(engine, proj):
    from dct.core.sprints import set_current_sprint_items, get_current_sprint
    assert get_current_sprint(engine, "dct") is None
    a = create_item(engine, "dct", "feature", "A")
    set_current_sprint_items(engine, "dct", [a["id"]])
    assert get_current_sprint(engine, "dct") is not None


def test_set_current_sprint_items_rejects_foreign_items(engine, proj):
    """STRICT since G1 (K-8, #413): a foreign id raises and the whole
    membership swap is rolled back — a composed handoff must never promise
    work on an item that silently failed to join the sprint."""
    from dct.core.sprints import get_current_sprint, set_current_sprint_items
    create_project(engine, slug="other", name="Other", path="/tmp/other")
    mine = create_item(engine, "dct", "feature", "Mine")
    foreign = create_item(engine, "other", "feature", "Foreign")
    with pytest.raises(ValueError, match=rf"Unknown item id\(s\).*{foreign['id']}"):
        set_current_sprint_items(engine, "dct", [mine["id"], foreign["id"]])
    cur = get_current_sprint(engine, "dct")
    if cur is not None:  # auto-created sprint may exist, but membership must not
        assert list_items_in_sprint(engine, cur["id"]) == []


def test_set_current_sprint_items_rejects_unknown_and_deleted_ids(engine, proj):
    from dct.core.items import delete_item
    from dct.core.sprints import set_current_sprint_items
    a = create_item(engine, "dct", "feature", "A")
    ghost = create_item(engine, "dct", "feature", "Ghost")
    delete_item(engine, ghost["id"])
    with pytest.raises(ValueError, match=r"Unknown item id\(s\)"):
        set_current_sprint_items(engine, "dct", [a["id"], ghost["id"], 999999])
    # Valid call afterwards still works — the failed attempt left no debris.
    res = set_current_sprint_items(engine, "dct", [a["id"]])
    assert res["items"] == [a["id"]]


def test_clear_current_sprint_removes_members_keeps_sprint(engine, proj):
    from dct.core.sprints import (
        set_current_sprint_items, clear_current_sprint, get_current_sprint,
    )
    a = create_item(engine, "dct", "feature", "A")
    res = set_current_sprint_items(engine, "dct", [a["id"]])
    cleared = clear_current_sprint(engine, "dct")
    assert cleared["cleared"] == 1
    assert list_items_in_sprint(engine, res["sprint_id"]) == []
    assert get_current_sprint(engine, "dct") is not None  # empty but still current


def test_clear_current_sprint_no_active_is_noop(engine, proj):
    from dct.core.sprints import clear_current_sprint
    assert clear_current_sprint(engine, "dct")["cleared"] == 0


# ── derived in_current_sprint (no column; computed from item_sprints) ──────────

def test_in_current_sprint_true_via_active_sprint(engine, proj):
    from dct.core.sprints import set_current_sprint_items
    from dct.core.items import get_item
    a = create_item(engine, "dct", "feature", "A")
    set_current_sprint_items(engine, "dct", [a["id"]])
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is True


def test_in_current_sprint_false_when_sprint_only_planned(engine, proj):
    """Membership in a PLANNED sprint is not 'current' — only the active one."""
    from dct.core.items import get_item
    s = create_sprint(engine, "dct", "Planned")        # planned, not active
    a = create_item(engine, "dct", "feature", "A")
    assign_item_to_sprint(engine, a["id"], s["id"])
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is False


def test_in_current_sprint_false_after_clear(engine, proj):
    from dct.core.sprints import set_current_sprint_items, clear_current_sprint
    from dct.core.items import get_item
    a = create_item(engine, "dct", "feature", "A")
    set_current_sprint_items(engine, "dct", [a["id"]])
    clear_current_sprint(engine, "dct")
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is False


def test_new_item_not_in_current_sprint(engine, proj):
    from dct.core.items import get_item
    a = create_item(engine, "dct", "feature", "A")
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is False


def test_list_items_filter_derives_in_current_sprint(engine, proj):
    from dct.core.sprints import set_current_sprint_items
    from dct.core.items import list_items
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    set_current_sprint_items(engine, "dct", [a["id"]])

    only_in = list_items(engine, "dct", status="", in_current_sprint=True)
    assert {i["id"] for i in only_in} == {a["id"]}

    not_in = {i["id"] for i in list_items(engine, "dct", status="", in_current_sprint=False)}
    assert a["id"] not in not_in
    assert b["id"] in not_in


def test_resolve_item_clears_flag_but_keeps_link(engine, proj):
    """resolve clears the derived flag but KEEPS the item_sprints link (K-18,
    #413) — retained membership is what sprint_completion counts. Supersedes
    the pre-G1 soft-remove contract."""
    from dct.core.sprints import set_current_sprint_items, get_current_sprint
    from dct.core.items import resolve_item, get_item
    a = create_item(engine, "dct", "feature", "A")
    set_current_sprint_items(engine, "dct", [a["id"]])
    resolve_item(engine, a["id"], resolution="done")
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is False
    cur = get_current_sprint(engine, "dct")
    assert cur is not None
    members = list_items_in_sprint(engine, cur["id"])
    assert [m["id"] for m in members] == [a["id"]]  # link retained (ledger)


# ── single-item add/remove to the current sprint (the /track-update surface) ───

def test_add_item_to_current_sprint_autocreates_and_flags(engine, proj):
    from dct.core.sprints import add_item_to_current_sprint, get_current_sprint
    from dct.core.items import get_item
    a = create_item(engine, "dct", "feature", "A")
    add_item_to_current_sprint(engine, "dct", a["id"])
    assert get_current_sprint(engine, "dct") is not None
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is True


def test_add_item_to_current_sprint_does_not_replace_existing(engine, proj):
    """Single add is additive — unlike set_sprint, it keeps current members."""
    from dct.core.sprints import (
        set_current_sprint_items, add_item_to_current_sprint, get_current_sprint,
    )
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    set_current_sprint_items(engine, "dct", [a["id"]])
    add_item_to_current_sprint(engine, "dct", b["id"])
    cur = get_current_sprint(engine, "dct")
    assert cur is not None
    members = {i["id"] for i in list_items_in_sprint(engine, cur["id"])}
    assert members == {a["id"], b["id"]}


def test_remove_item_from_current_sprint(engine, proj):
    from dct.core.sprints import add_item_to_current_sprint, remove_item_from_current_sprint
    from dct.core.items import get_item
    a = create_item(engine, "dct", "feature", "A")
    add_item_to_current_sprint(engine, "dct", a["id"])
    res = remove_item_from_current_sprint(engine, "dct", a["id"])
    assert res["removed"] is True
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is False


def test_remove_item_from_current_sprint_no_current_is_noop(engine, proj):
    from dct.core.sprints import remove_item_from_current_sprint
    a = create_item(engine, "dct", "feature", "A")
    res = remove_item_from_current_sprint(engine, "dct", a["id"])
    assert res["removed"] is False


# ── status transition whitelist (deferred lifecycle, #292) ────────────────────

_ALLOWED_EDGES = sorted(
    (src, dst) for src, targets in ALLOWED_TRANSITIONS.items() for dst in sorted(targets)
)


@pytest.mark.parametrize("src,dst", _ALLOWED_EDGES)
def test_allowed_transition_succeeds(engine, proj, src, dst):
    s = _make_status(engine, f"{src}->{dst}", src)
    updated = update_sprint(engine, s["id"], status=dst)
    assert updated is not None
    assert updated["status"] == dst


@pytest.mark.parametrize(
    "src,dst",
    [
        ("planned", "deferred"),   # never started — already parked
        ("planned", "completed"),  # cannot complete what never started
        ("active", "planned"),     # demotion — owner rule
        ("deferred", "planned"),   # demotion — owner rule
        ("completed", "deferred"), # only reopen (→active) is legal
        ("abandoned", "completed"),# only reopen (→active) is legal
    ],
)
def test_rejected_transition_raises(engine, proj, src, dst):
    s = _make_status(engine, f"{src}->{dst}", src)
    with pytest.raises(ValueError, match="Illegal sprint status transition"):
        update_sprint(engine, s["id"], status=dst)


def test_rejected_transition_lists_allowed_targets(engine, proj):
    s = _make_status(engine, "planned", "planned")
    with pytest.raises(ValueError, match="active"):
        update_sprint(engine, s["id"], status="completed")


def test_same_status_is_noop_timestamps_untouched(engine, proj):
    s = _make_status(engine, "active", "active")
    updated = update_sprint(engine, s["id"], status="active")
    assert updated is not None
    assert updated["status"] == "active"
    assert updated["started_at"] == s["started_at"]
    assert updated["ended_at"] == s["ended_at"]
    assert updated["updated_at"] == s["updated_at"]  # nothing written


def test_same_status_planned_noop(engine, proj):
    s = create_sprint(engine, "dct", "Still planned")
    updated = update_sprint(engine, s["id"], status="planned")
    assert updated["status"] == "planned"
    assert updated["started_at"] is None


def test_same_status_but_other_field_updates(engine, proj):
    s = _make_status(engine, "active", "active")
    updated = update_sprint(engine, s["id"], status="active", name="Renamed")
    assert updated["name"] == "Renamed"
    assert updated["status"] == "active"
    assert updated["started_at"] == s["started_at"]


# ── deferred semantics ────────────────────────────────────────────────────────

def test_defer_keeps_started_at_and_null_ended_at(engine, proj):
    s = create_sprint(engine, "dct", "Working", status="active")
    started = s["started_at"]
    deferred = update_sprint(engine, s["id"], status="deferred")
    assert deferred["status"] == "deferred"
    assert deferred["started_at"] == started  # keeps started_at
    assert deferred["ended_at"] is None       # never stamps ended_at


def test_defer_frees_current_sprint_slot(engine, proj):
    s = create_sprint(engine, "dct", "Active", status="active")
    assert get_current_sprint(engine, "dct") is not None
    update_sprint(engine, s["id"], status="deferred")
    assert get_current_sprint(engine, "dct") is None  # parked, slot freed


def test_deferred_invisible_in_review_sprint_in_sprint(engine, proj):
    from dct.core.sprint import get_sprint_review
    from dct.core.sprints import add_item_to_current_sprint
    a = create_item(engine, "dct", "feature", "A")
    add_item_to_current_sprint(engine, "dct", a["id"])
    cur = get_current_sprint(engine, "dct")
    update_sprint(engine, cur["id"], status="deferred")
    review = get_sprint_review(engine, "dct")
    assert review["in_sprint"]["rows"] == []  # deferred sprint drops out


def test_deferred_visible_in_list_sprints_filter(engine, proj):
    s = create_sprint(engine, "dct", "Parked", status="active")
    update_sprint(engine, s["id"], status="deferred")
    deferred = list_sprints(engine, project_slug="dct", status="deferred")
    assert [x["id"] for x in deferred] == [s["id"]]


def test_create_sprint_deferred_raises(engine, proj):
    with pytest.raises(ValueError, match="cannot be created with status 'deferred'"):
        create_sprint(engine, "dct", "Nope", status="deferred")


# ── reopen clears ended_at ────────────────────────────────────────────────────

def test_reopen_completed_clears_ended_at(engine, proj):
    s = create_sprint(engine, "dct", "Done", status="active")
    completed = update_sprint(engine, s["id"], status="completed")
    assert completed["ended_at"] is not None
    reopened = update_sprint(engine, s["id"], status="active")
    assert reopened["status"] == "active"
    assert reopened["ended_at"] is None
    assert reopened["started_at"] == s["started_at"]  # preserved


def test_reopen_abandoned_clears_ended_at(engine, proj):
    s = create_sprint(engine, "dct", "Dropped", status="active")
    abandoned = update_sprint(engine, s["id"], status="abandoned")
    assert abandoned["ended_at"] is not None
    reopened = update_sprint(engine, s["id"], status="active")
    assert reopened["status"] == "active"
    assert reopened["ended_at"] is None


# ── reactivation alongside another active sprint (cap lifted, G1) ─────────────

def test_reactivating_deferred_while_other_active_allowed(engine, proj):
    """G1 (#413 K-5): a deferred sprint reactivates fine next to an active one;
    started_at is preserved from the first activation."""
    a = create_sprint(engine, "dct", "A", status="active")
    a_started = a["started_at"]
    update_sprint(engine, a["id"], status="deferred")
    create_sprint(engine, "dct", "B", status="active")
    reactivated = update_sprint(engine, a["id"], status="active")
    assert reactivated["status"] == "active"
    assert reactivated["started_at"] == a_started


# ── acceptance scenario from handoff #46 ──────────────────────────────────────

def test_handoff_46_park_and_resume(engine, proj):
    """Create active A → defer A (slot freed) → activate B → complete B →
    reactivate A, asserting A.started_at is preserved throughout."""
    a = create_sprint(engine, "dct", "Sprint A", status="active")
    a_started = a["started_at"]
    assert a_started is not None

    # Defer A — slot freed, no current sprint.
    a_def = update_sprint(engine, a["id"], status="deferred")
    assert a_def["started_at"] == a_started
    assert get_current_sprint(engine, "dct") is None

    # Activate B in the freed slot.
    b = create_sprint(engine, "dct", "Sprint B", status="active")
    cur = get_current_sprint(engine, "dct")
    assert cur is not None and cur["id"] == b["id"]

    # Complete B — slot freed again.
    update_sprint(engine, b["id"], status="completed")
    assert get_current_sprint(engine, "dct") is None

    # Reactivate A — started_at unchanged the whole way through.
    a_back = update_sprint(engine, a["id"], status="active")
    assert a_back["status"] == "active"
    assert a_back["started_at"] == a_started
    assert get_current_sprint(engine, "dct")["id"] == a["id"]


# ── update_sprint duplicate-code pre-check ────────────────────────────────────

def test_update_sprint_duplicate_code_raises(engine, proj):
    create_sprint(engine, "dct", "First", code="taken")
    other = create_sprint(engine, "dct", "Second")
    with pytest.raises(ValueError, match="already exists"):
        update_sprint(engine, other["id"], code="taken")


def test_update_sprint_keep_own_code_is_fine(engine, proj):
    """Re-setting a sprint's own code must not trip the uniqueness pre-check."""
    s = create_sprint(engine, "dct", "Solo", code="mine")
    updated = update_sprint(engine, s["id"], code="mine", name="Solo v2")
    assert updated["name"] == "Solo v2"
    assert updated["code"] == "mine"


def test_update_sprint_unknown_id_returns_none(engine, proj):
    assert update_sprint(engine, 9999, status="active") is None


# ── K-33: current-sprint WRITES refuse to guess among parallel actives ─────────

def test_current_sprint_writes_refuse_ambiguity(engine, proj):
    """set/clear/add/remove current-sprint ops error with 2 active sprints
    instead of silently retargeting the newest one."""
    from dct.core.sprints import (
        add_item_to_current_sprint,
        clear_current_sprint,
        remove_item_from_current_sprint,
        set_current_sprint_items,
    )
    a = create_item(engine, "dct", "feature", "A")
    create_sprint(engine, "dct", "S1", status="active")
    create_sprint(engine, "dct", "S2", status="active")

    for op in (
        lambda: set_current_sprint_items(engine, "dct", [a["id"]]),
        lambda: clear_current_sprint(engine, "dct"),
        lambda: add_item_to_current_sprint(engine, "dct", a["id"]),
        lambda: remove_item_from_current_sprint(engine, "dct", a["id"]),
    ):
        with pytest.raises(ValueError, match="refuse to guess"):
            op()
