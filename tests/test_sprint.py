from datetime import datetime, timedelta, timezone

import pytest

from dct.core.items import create_item, update_item, resolve_item, get_item
from dct.core.checkpoints import add_checkpoint, set_status
from dct.core.projects import create_project
from dct.core.sprint import (
    set_sprint, clear_sprint, list_sprint_items,
    get_sprint_review, parse_since,
)


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="dct", name="dct", path="/tmp/dct")


def test_set_sprint_flags_items(engine, proj):
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    c = create_item(engine, "dct", "feature", "C")

    result = set_sprint(engine, "dct", [a["id"], b["id"]])
    assert result["size"] == 2
    assert set(result["items"]) == {a["id"], b["id"]}

    flagged = list_sprint_items(engine, "dct")
    flagged_ids = {i["id"] for i in flagged}
    assert flagged_ids == {a["id"], b["id"]}

    # c is NOT in sprint
    fetched_c = get_item(engine, c["id"])
    assert fetched_c is not None
    assert fetched_c["in_current_sprint"] is False


def test_set_sprint_replaces_existing(engine, proj):
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    c = create_item(engine, "dct", "feature", "C")

    set_sprint(engine, "dct", [a["id"], b["id"]])
    set_sprint(engine, "dct", [c["id"]])  # replaces — a, b out; c in

    flagged = {i["id"] for i in list_sprint_items(engine, "dct")}
    assert flagged == {c["id"]}


def test_clear_sprint(engine, proj):
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    set_sprint(engine, "dct", [a["id"], b["id"]])

    result = clear_sprint(engine, "dct")
    assert result["cleared"] == 2
    assert list_sprint_items(engine, "dct") == []


def test_resolve_item_auto_unflags_sprint(engine, proj):
    """Invariant: terminal status ⇒ item leaves the sprint."""
    a = create_item(engine, "dct", "feature", "A")
    set_sprint(engine, "dct", [a["id"]])

    resolve_item(engine, a["id"], resolution="Done")

    fetched = get_item(engine, a["id"])
    assert fetched is not None
    assert fetched["status"] == "resolved"
    assert fetched["in_current_sprint"] is False


def test_update_to_terminal_auto_unflags_sprint(engine, proj):
    a = create_item(engine, "dct", "feature", "A")
    set_sprint(engine, "dct", [a["id"]])

    update_item(engine, a["id"], status="wont_fix")

    fetched = get_item(engine, a["id"])
    assert fetched is not None
    assert fetched["in_current_sprint"] is False


def test_update_item_no_longer_flags_sprint_directly(engine, proj):
    """v0.2+: update_item(in_current_sprint=...) raises; use set_sprint instead.

    Supersedes the v0.1 test `test_update_can_reflag_sprint` — the API contract
    changed as of migration 003. Covered more thoroughly in
    test_migration.py::test_update_item_raises_on_in_current_sprint_kwarg.
    """
    import pytest as _pytest
    a = create_item(engine, "dct", "feature", "A")
    with _pytest.raises(ValueError, match="deprecated in v0.2.0"):
        update_item(engine, a["id"], in_current_sprint=True)

    # set_sprint now records membership in item_sprints (the active sprint);
    # the derived in_current_sprint reflects it — no boolean column is written.
    set_sprint(engine, "dct", [a["id"]])
    from dct.core.items import get_item
    got = get_item(engine, a["id"])
    assert got is not None and got["in_current_sprint"] is True


def test_parse_since_hours(engine, proj):
    anchor, label = parse_since("24h", engine, "dct")
    assert anchor is not None
    assert "24h" in label
    # anchor is ~24h ago
    delta = datetime.now(timezone.utc) - anchor
    assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)


def test_parse_since_iso(engine, proj):
    anchor, label = parse_since("2026-04-10", engine, "dct")
    assert anchor is not None
    assert anchor.year == 2026 and anchor.month == 4 and anchor.day == 10
    assert "2026" in label


def test_parse_since_all(engine, proj):
    anchor, label = parse_since("all", engine, "dct")
    assert anchor is None
    assert "all time" in label


def test_parse_since_no_releases_falls_back(engine, proj):
    anchor, label = parse_since("last_release", engine, "dct")
    assert anchor is None
    assert "no releases" in label


def test_parse_since_invalid_raises(engine, proj):
    with pytest.raises(ValueError, match="Invalid `since`"):
        parse_since("yesterday", engine, "dct")


def test_get_sprint_review_shape(engine, proj):
    a = create_item(engine, "dct", "feature", "A", priority="P1")
    cp = add_checkpoint(engine, a["id"], "Step 1")
    set_status(engine, cp["id"], "done")
    set_sprint(engine, "dct", [a["id"]])

    review = get_sprint_review(engine, "dct", since="all")

    assert review["project"] == "dct"
    assert review["in_sprint"]["cols"] == ["id", "priority", "status", "cp", "title"]
    assert len(review["in_sprint"]["rows"]) == 1
    row = review["in_sprint"]["rows"][0]
    # cp should be [1, 1]
    cp_index = review["in_sprint"]["cols"].index("cp")
    assert row[cp_index] == [1, 1]

    assert review["summary"]["sprint_size"] == 1
    assert review["summary"]["handoffs_pending"] == 0
