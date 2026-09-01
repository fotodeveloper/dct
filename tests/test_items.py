import pytest
import sqlalchemy as sa

from dct.core.items import (
    add_note,
    create_item,
    delete_item,
    get_item,
    list_items,
    list_notes,
    resolve_item,
    update_item,
)
from dct.core.projects import create_project


@pytest.fixture
def project(engine):
    return create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")


def test_create_and_get(engine, project):
    item = create_item(engine, "proj-a", "todo", "Fix parser", description="It crashes", priority="P1")
    assert item["title"] == "Fix parser"
    assert item["priority"] == "P1"
    assert item["status"] == "open"
    assert item["tags"] == []

    fetched = get_item(engine, item["id"])
    assert fetched is not None
    assert fetched["title"] == "Fix parser"
    assert fetched["notes"] == []


def test_create_with_tags(engine, project):
    # Tag order is name-ASC, NOT insertion order (#611, plan decision 3): the
    # link table has no meaningful sequence, so the read order is deterministic
    # by name everywhere (create / get / list).
    item = create_item(engine, "proj-a", "feature", "Add caching", tags=["perf", "arch"])
    assert item["tags"] == ["arch", "perf"]

    # Core is list-only (see #31) — string input must raise
    with pytest.raises(AssertionError, match="tags must be list"):
        create_item(engine, "proj-a", "feature", "Add logging", tags="debug,observability")  # type: ignore[arg-type]


def test_invalid_type_raises(engine, project):
    with pytest.raises(ValueError, match="Invalid item_type"):
        create_item(engine, "proj-a", "invalid_type", "Bad item")


def test_invalid_priority_raises(engine, project):
    with pytest.raises(ValueError, match="Invalid priority"):
        create_item(engine, "proj-a", "todo", "Bad", priority="URGENT")


def test_list_filters(engine, project):
    create_item(engine, "proj-a", "todo", "A", priority="P1")
    create_item(engine, "proj-a", "issue", "B", priority="P2")
    create_item(engine, "proj-a", "feature", "C", priority="P3", status="resolved")

    assert len(list_items(engine)) == 3
    assert len(list_items(engine, project_slug="proj-a")) == 3
    assert len(list_items(engine, status="open")) == 2
    assert len(list_items(engine, item_type="issue")) == 1
    assert len(list_items(engine, priority="P1")) == 1


def test_list_priority_ordering(engine, project):
    create_item(engine, "proj-a", "todo", "Low", priority="P3")
    create_item(engine, "proj-a", "todo", "High", priority="P1")
    create_item(engine, "proj-a", "todo", "Future", priority="future")
    create_item(engine, "proj-a", "todo", "Mid", priority="P2")

    result = list_items(engine)
    priorities = [r["priority"] for r in result]
    assert priorities == ["P1", "P2", "P3", "future"]


def test_search(engine, project):
    create_item(engine, "proj-a", "todo", "Fix parser crash")
    create_item(engine, "proj-a", "todo", "Add logging", description="parser output")
    create_item(engine, "proj-a", "todo", "Unrelated task")

    results = list_items(engine, search="parser")
    assert len(results) == 2


def test_update(engine, project):
    item = create_item(engine, "proj-a", "todo", "Original")
    updated = update_item(engine, item["id"], title="Changed", priority="P1")
    assert updated is not None
    assert updated["title"] == "Changed"
    assert updated["priority"] == "P1"


def test_update_no_fields_raises(engine, project):
    item = create_item(engine, "proj-a", "todo", "X")
    with pytest.raises(ValueError, match="No valid fields"):
        update_item(engine, item["id"])


def test_resolve(engine, project):
    item = create_item(engine, "proj-a", "issue", "Bug")
    resolved = resolve_item(engine, item["id"], resolution="Fixed in commit abc123")
    assert resolved is not None
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
    assert resolved["resolution"] == "Fixed in commit abc123"


def test_update_to_resolved_sets_resolved_at(engine, project):
    """Regression: update_item(status='resolved') must set resolved_at.
    Previously only resolve_item() set the timestamp, so items resolved via
    update_item were silently missing the audit stamp.
    """
    item = create_item(engine, "proj-a", "issue", "Bug")
    assert item["resolved_at"] is None

    updated = update_item(engine, item["id"], status="resolved", resolution="Done")
    assert updated is not None
    assert updated["status"] == "resolved"
    assert updated["resolved_at"] is not None
    assert updated["resolution"] == "Done"


def test_update_to_wont_fix_sets_resolved_at(engine, project):
    item = create_item(engine, "proj-a", "issue", "Bug")
    updated = update_item(engine, item["id"], status="wont_fix", resolution="Out of scope")
    assert updated is not None
    assert updated["status"] == "wont_fix"
    assert updated["resolved_at"] is not None


def test_update_reopen_clears_resolved_at(engine, project):
    """Symmetric invariant: reopening a resolved item must clear resolved_at."""
    item = create_item(engine, "proj-a", "issue", "Bug")
    resolve_item(engine, item["id"], resolution="Fixed")

    reopened = update_item(engine, item["id"], status="open")
    assert reopened is not None
    assert reopened["status"] == "open"
    assert reopened["resolved_at"] is None


def test_update_inprogress_clears_resolved_at(engine, project):
    item = create_item(engine, "proj-a", "issue", "Bug")
    resolve_item(engine, item["id"])

    reopened = update_item(engine, item["id"], status="in_progress")
    assert reopened is not None
    assert reopened["status"] == "in_progress"
    assert reopened["resolved_at"] is None


def test_notes_append_only(engine, project):
    item = create_item(engine, "proj-a", "todo", "Task")
    add_note(engine, item["id"], "First thought")
    add_note(engine, item["id"], "Second thought")

    notes = list_notes(engine, item["id"])
    assert len(notes) == 2
    assert notes[0]["note"] == "First thought"
    assert notes[1]["note"] == "Second thought"

    # Notes also returned via get_item
    fetched = get_item(engine, item["id"])
    assert fetched is not None
    assert len(fetched["notes"]) == 2


def test_note_on_missing_item_raises(engine, project):
    with pytest.raises(ValueError, match="not found"):
        add_note(engine, 999, "Orphan note")


def test_soft_delete(engine, project):
    item = create_item(engine, "proj-a", "todo", "Deletable")
    assert delete_item(engine, item["id"]) is True
    assert get_item(engine, item["id"]) is None
    assert len(list_items(engine)) == 0


def test_cross_project_isolation(engine):
    create_project(engine, slug="a", name="A")
    create_project(engine, slug="b", name="B")
    create_item(engine, "a", "todo", "Task A")
    create_item(engine, "b", "todo", "Task B")

    assert len(list_items(engine, project_slug="a")) == 1
    assert len(list_items(engine, project_slug="b")) == 1
    assert len(list_items(engine)) == 2  # cross-project


def test_source_file_and_lines(engine, project):
    item = create_item(engine, "proj-a", "issue", "Bug here",
                        source_file="cmd/index.go", source_lines="42-58")
    assert item["source_file"] == "cmd/index.go"
    assert item["source_lines"] == "42-58"


# ── tags: aggregate fan-out guard + filtering (#611) ──────────────────────────


def _cp(engine, item_id: int, status: str):
    from dct.core.checkpoints import add_checkpoint, set_status

    cp = add_checkpoint(engine, item_id, f"step {status}")
    if status != "pending":
        set_status(engine, cp["id"], status)
    return cp


def test_cp_progress_is_immune_to_tag_fanout(engine, project):
    """The list query SUMs over an outer-joined item_checkpoints and groups by
    item. If tags were read by joining item_tags into that same query, every
    checkpoint row would be multiplied by the number of tags (3 cps × 3 tags →
    cp == [6, 9]). Tags must come from a SEPARATE query — this asserts the
    arithmetic that proves it."""
    tagged = create_item(engine, "proj-a", "todo", "Tagged", tags=["perf", "db", "ui"])
    _cp(engine, tagged["id"], "done")
    _cp(engine, tagged["id"], "done")
    _cp(engine, tagged["id"], "pending")
    create_item(engine, "proj-a", "todo", "Untagged")

    rows = {r["title"]: r for r in list_items(engine, project_slug="proj-a")}

    assert rows["Tagged"]["cp"] == [2, 3]
    assert rows["Tagged"]["tags"] == ["db", "perf", "ui"]  # name-ASC
    assert rows["Untagged"]["cp"] == [0, 0]
    assert rows["Untagged"]["tags"] == []


def test_list_items_filter_by_tag(engine, project):
    a = create_item(engine, "proj-a", "todo", "A", tags=["perf", "db"])
    create_item(engine, "proj-a", "todo", "B", tags=["perf"])
    create_item(engine, "proj-a", "todo", "C")

    assert {r["title"] for r in list_items(engine, tag="perf")} == {"A", "B"}
    assert {r["title"] for r in list_items(engine, tag="db")} == {"A"}
    # the filter value is normalized like any other tag name
    assert {r["title"] for r in list_items(engine, tag="  PERF ")} == {"A", "B"}

    # a soft-removed link drops the item out of the filter
    update_item(engine, a["id"], tags=["db"])
    assert {r["title"] for r in list_items(engine, tag="perf")} == {"B"}


def test_tag_filter_composes_with_project_and_status(engine, project):
    create_project(engine, slug="proj-b", name="Project B", path="/tmp/proj-b")
    create_item(engine, "proj-a", "todo", "A open", tags=["perf"])
    create_item(engine, "proj-a", "todo", "A resolved", tags=["perf"], status="resolved")
    create_item(engine, "proj-b", "todo", "B open", tags=["perf"])

    assert len(list_items(engine, tag="perf")) == 3  # cross-project by default
    assert {r["title"] for r in list_items(engine, project_slug="proj-a", tag="perf")} == {
        "A open", "A resolved"}
    assert [r["title"] for r in list_items(engine, project_slug="proj-a", tag="perf",
                                           status="open")] == ["A open"]


def test_list_items_unknown_tag_raises_with_suggestion(engine, project):
    """Closed dictionary: a typo must be an error naming the real tags, not a
    silently empty list that reads as 'nothing matches'."""
    create_item(engine, "proj-a", "todo", "A", tags=["perf"])

    with pytest.raises(ValueError) as exc:
        list_items(engine, tag="prf")
    msg = str(exc.value)
    assert "prf" in msg and "perf" in msg


def test_list_items_unknown_tag_raises_on_an_empty_dictionary(engine, project):
    create_item(engine, "proj-a", "todo", "A")
    with pytest.raises(ValueError):
        list_items(engine, tag="anything")


# ── K-37: the UPDATE carries its own soft-delete guard ────────────────────────

def test_update_soft_deleted_item_returns_none_and_writes_nothing(engine, project):
    """The plain (non-racing) case: a soft-deleted item is not updatable."""
    item = create_item(engine, "proj-a", "todo", "Original", priority="P2")
    delete_item(engine, item["id"])

    assert update_item(engine, item["id"], title="Rewritten", priority="P1") is None

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT title, priority FROM items WHERE id = :id"),
            {"id": item["id"]},
        ).one()
    assert tuple(row) == ("Original", "P2")


def test_update_loses_a_race_with_delete_and_returns_none(engine, project):
    """Adversarial (K-37): the existence SELECT and the UPDATE are two separate
    statements. A delete_item landing BETWEEN them must make update_item a
    no-op returning None — without the guard on the UPDATE itself the write
    would resurrect a soft-deleted row.

    The race is injected AFTER the existence SELECT has run (a
    `before_cursor_execute` hook would delete too early and be caught by that
    SELECT instead), through a second DBAPI cursor on the SAME connection — so
    the soft delete is visible to the UPDATE that follows inside the very same
    transaction.
    """
    item = create_item(engine, "proj-a", "todo", "Original", priority="P2")
    fired: list[int] = []

    def _delete_between(conn, cursor, statement, parameters, context, executemany):
        if fired or not statement.strip().startswith("SELECT items.id"):
            return
        fired.append(1)
        side = cursor.connection.cursor()
        side.execute(
            f"UPDATE items SET deleted_at = CURRENT_TIMESTAMP WHERE id = {item['id']}"
        )
        side.close()

    sa.event.listen(engine, "after_cursor_execute", _delete_between)
    try:
        result = update_item(engine, item["id"], title="Rewritten", priority="P1")
    finally:
        sa.event.remove(engine, "after_cursor_execute", _delete_between)

    assert fired, "the existence SELECT was never observed — test seam is stale"
    assert result is None
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT title, priority FROM items WHERE id = :id"),
            {"id": item["id"]},
        ).one()
    assert tuple(row) == ("Original", "P2")
