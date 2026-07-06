import pytest

from dct.core.changelog import (
    add_entry, list_entries, count_unreleased, release_entries, delete_entry,
)
from dct.core.projects import create_project


@pytest.fixture
def project(engine):
    return create_project(engine, slug="proj-a", name="Project A")


def test_add_and_list(engine, project):
    e = add_entry(engine, "proj-a", "added", "New parser for Python")
    assert e["category"] == "added"
    assert e["version"] is None  # unreleased
    assert e["deleted_at"] is None

    entries = list_entries(engine, "proj-a")
    assert len(entries) == 1


def test_invalid_category_raises(engine, project):
    with pytest.raises(ValueError, match="Invalid category"):
        add_entry(engine, "proj-a", "improved", "Something")


def test_unreleased_filter(engine, project):
    add_entry(engine, "proj-a", "added", "Feature A")
    add_entry(engine, "proj-a", "fixed", "Bug B")
    add_entry(engine, "proj-a", "added", "Feature C", version="0.9")

    assert count_unreleased(engine, "proj-a") == 2
    unreleased = list_entries(engine, "proj-a", unreleased_only=True)
    assert len(unreleased) == 2
    assert all(e["version"] is None for e in unreleased)


def test_release_entries(engine, project):
    add_entry(engine, "proj-a", "added", "Feature A")
    add_entry(engine, "proj-a", "fixed", "Bug B")

    released_count = release_entries(engine, "proj-a", "1.0")
    assert released_count == 2
    assert count_unreleased(engine, "proj-a") == 0

    entries = list_entries(engine, "proj-a", version="1.0")
    assert len(entries) == 2
    assert all(e["version"] == "1.0" for e in entries)
    assert all(e["released_at"] is not None for e in entries)


def test_release_does_not_touch_already_released(engine, project):
    add_entry(engine, "proj-a", "added", "Old", version="0.9")
    add_entry(engine, "proj-a", "added", "New")

    release_entries(engine, "proj-a", "1.0")
    old = list_entries(engine, "proj-a", version="0.9")
    assert len(old) == 1
    assert old[0]["entry"] == "Old"


def test_soft_delete(engine, project):
    e = add_entry(engine, "proj-a", "added", "Deletable")
    assert delete_entry(engine, e["id"]) is True
    assert count_unreleased(engine, "proj-a") == 0
    assert len(list_entries(engine, "proj-a")) == 0


def test_source_file_on_changelog(engine, project):
    e = add_entry(engine, "proj-a", "fixed", "Fix crash in parser",
                   source_file="cmd/parse.go", source_lines="120-135")
    assert e["source_file"] == "cmd/parse.go"
    assert e["source_lines"] == "120-135"


def test_related_item(engine, project):
    from dct.core.items import create_item
    item = create_item(engine, "proj-a", "issue", "Parser bug")
    e = add_entry(engine, "proj-a", "fixed", "Fixed parser bug", related_item_id=item["id"])
    assert e["related_item_id"] == item["id"]


def test_release_updates_project_version(engine, project):
    add_entry(engine, "proj-a", "added", "Feature")
    from dct.core.changelog import release_entries
    release_entries(engine, "proj-a", "1.1")

    from dct.core.projects import get_project
    p = get_project(engine, "proj-a")
    assert p["current_version"] == "1.1"


def test_cross_project_isolation(engine):
    create_project(engine, slug="a", name="A")
    create_project(engine, slug="b", name="B")
    add_entry(engine, "a", "added", "Feature in A")
    add_entry(engine, "b", "added", "Feature in B")

    assert count_unreleased(engine, "a") == 1
    assert count_unreleased(engine, "b") == 1
