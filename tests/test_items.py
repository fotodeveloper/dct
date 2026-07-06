import pytest

from dct.core.items import (
    create_item, list_items, get_item, update_item,
    resolve_item, add_note, list_notes, delete_item,
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
    item = create_item(engine, "proj-a", "feature", "Add caching", tags=["perf", "arch"])
    assert item["tags"] == ["perf", "arch"]

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
