from datetime import UTC, datetime, timedelta

import pytest

from dct.core.checkpoints import add_checkpoint, set_status
from dct.core.items import create_item, get_item, resolve_item, update_item
from dct.core.projects import create_project
from dct.core.sprint import (
    clear_sprint,
    get_sprint_review,
    list_sprint_items,
    parse_since,
    set_sprint,
)


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="dct", name="dct", path="/tmp/dct")


def test_set_sprint_flags_items(engine, proj):
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    c = create_item(engine, "dct", "feature", "C")

    result = set_sprint(engine, "dct", [a["id"], b["id"]])
    assert result["open_size"] == 2
    assert "size" not in result
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
    delta = datetime.now(UTC) - anchor
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
    assert review["in_sprint"]["cols"] == ["id", "priority", "status", "cp", "short_title", "title"]
    assert len(review["in_sprint"]["rows"]) == 1
    row = review["in_sprint"]["rows"][0]
    # cp should be [1, 1]
    cp_index = review["in_sprint"]["cols"].index("cp")
    assert row[cp_index] == [1, 1]

    assert review["summary"]["sprint_size"] == 1
    assert review["summary"]["handoffs_pending"] == 0


# ── K-18 metric fixes (#413): review metrics tell the truth ───────────────────

def test_total_open_counts_in_progress(engine, proj):
    """total_open_in_project = open + in_progress (in-flight work IS open work)."""
    create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    update_item(engine, b["id"], status="in_progress")

    review = get_sprint_review(engine, "dct", since="all")
    assert review["summary"]["total_open_in_project"] == 2


def test_since_all_shows_recent_activity(engine, proj):
    """since='all' means NO cutoff — activity must show everything, not nothing."""
    a = create_item(engine, "dct", "feature", "A")
    cp = add_checkpoint(engine, a["id"], "Step 1")
    set_status(engine, cp["id"], "done")

    review = get_sprint_review(engine, "dct", since="all")
    changed_ids = [r[0] for r in review["recent_activity"]["items_changed"]["rows"]]
    assert a["id"] in changed_ids
    assert review["recent_activity"]["checkpoints_completed"] == 1


def test_no_release_fallback_shows_activity(engine, proj):
    """Default since=last_release with zero releases labels itself 'showing all
    time' — the data must match the label."""
    a = create_item(engine, "dct", "feature", "A")

    review = get_sprint_review(engine, "dct")  # default since
    assert review["since_anchor"] == "no releases — showing all time"
    changed_ids = [r[0] for r in review["recent_activity"]["items_changed"]["rows"]]
    assert a["id"] in changed_ids


def test_sprint_completion_counts_resolved_members(engine, proj):
    """Resolving a sprint member leaves the in_sprint VIEW but keeps membership,
    so completion finally counts: 1/2, not the perpetual 0/N."""
    a = create_item(engine, "dct", "feature", "A")
    b = create_item(engine, "dct", "feature", "B")
    set_sprint(engine, "dct", [a["id"], b["id"]])

    resolve_item(engine, a["id"], resolution="Done")

    review = get_sprint_review(engine, "dct", since="all")
    assert review["summary"]["sprint_size"] == 2
    assert review["summary"]["sprint_completion"] == "1/2"
    in_sprint_ids = [r[0] for r in review["in_sprint"]["rows"]]
    assert in_sprint_ids == [b["id"]]
    fetched = get_item(engine, a["id"])
    assert fetched is not None and fetched["in_current_sprint"] is False


def test_wont_fix_member_counts_in_completion(engine, proj):
    a = create_item(engine, "dct", "feature", "A")
    set_sprint(engine, "dct", [a["id"]])
    update_item(engine, a["id"], status="wont_fix")

    review = get_sprint_review(engine, "dct", since="all")
    assert review["summary"]["sprint_completion"] == "1/1"
    assert review["in_sprint"]["rows"] == []


def test_reopened_member_returns_to_sprint_view(engine, proj):
    """Membership survives resolve, so reopening an item puts it back in the
    current-sprint view and out of the completed count."""
    a = create_item(engine, "dct", "feature", "A")
    set_sprint(engine, "dct", [a["id"]])
    resolve_item(engine, a["id"], resolution="Done")
    update_item(engine, a["id"], status="open")

    review = get_sprint_review(engine, "dct", since="all")
    assert review["summary"]["sprint_completion"] == "0/1"
    assert [r[0] for r in review["in_sprint"]["rows"]] == [a["id"]]


# ── _to_cols_rows: JSON safety + row caps ─────────────────────────────────────

def test_to_cols_rows_serializes_datetimes_and_nested_cells():
    """K-36: no `datetime` may survive into a payload — nested cells included."""
    import json

    from dct.core.sprint import _to_cols_rows

    when = datetime.fromisoformat("2026-07-28T12:00:00+00:00")
    tbl = _to_cols_rows(
        [{"id": 1, "at": when, "cp": [1, 2], "title": "already a string"}],
        ["id", "at", "cp", "title"],
    )
    assert tbl["rows"][0] == [1, when.isoformat(), [1, 2], "already a string"]
    json.dumps(tbl)


def test_to_cols_rows_never_double_converts_strings():
    """An already-ISO string must come back byte-identical, not re-parsed."""
    from dct.core.sprint import _to_cols_rows

    iso = "2026-07-28T12:00:00+00:00"
    tbl = _to_cols_rows([{"at": iso}], ["at"])
    assert tbl["rows"][0][0] == iso


def test_to_cols_rows_uncapped_keeps_two_key_shape():
    from dct.core.sprint import _to_cols_rows

    tbl = _to_cols_rows([{"id": i} for i in range(5)], ["id"])
    assert set(tbl) == {"cols", "rows"}
    assert len(tbl["rows"]) == 5


@pytest.mark.parametrize("n,cap,expected_rows,truncated", [
    (0, 3, 0, False),
    (2, 3, 2, False),
    (3, 3, 3, False),   # exactly at the cap is NOT truncated
    (4, 3, 3, True),
])
def test_to_cols_rows_cap_boundary(n, cap, expected_rows, truncated):
    from dct.core.sprint import _to_cols_rows

    tbl = _to_cols_rows([{"id": i} for i in range(n)], ["id"], cap=cap)
    assert len(tbl["rows"]) == expected_rows
    assert tbl["truncated"] is truncated


# ── _query_unconsumed_handoffs: SQL-side scope filter ─────────────────────────

def test_query_unconsumed_handoffs_scope_filter(engine, proj):
    from dct.core.handoff import create_handoff
    from dct.core.projects import resolve_project_id
    from dct.core.sprint import _query_unconsumed_handoffs

    who = "tester @ 2026-07-28T12:00Z"
    a = create_handoff(engine, "dct", "in scope", who, scope="g1")
    create_handoff(engine, "dct", "out of scope", who, scope="other")
    create_handoff(engine, "dct", "no scope at all", who)

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, "dct")
        assert len(_query_unconsumed_handoffs(conn, project_id)) == 3
        scoped = _query_unconsumed_handoffs(conn, project_id, "g1")
    assert [h["id"] for h in scoped] == [a["id"]]


# ── _parse_since_conn: no assert (works under python -O) ──────────────────────

def test_parse_since_last_release_without_conn_raises_valueerror():
    from dct.core.sprint import _parse_since_conn

    with pytest.raises(ValueError, match="needs a connection"):
        _parse_since_conn(None, 0, "last_release")
