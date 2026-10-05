"""Changelog write-path integrity (fix round R1b):

- K-37: a related item must be a live item of the SAME project (validated in
  the write transaction; refusal writes nothing); the label join only labels
  live same-project items and never rewrites a dangling historical id.
- K-36: server-detected branch metadata is UNCERTAIN — include_unbranched
  treats it like a missing branch; a plain branch filter is an exact match on
  the stored value whatever its source.
- K-42: ONE provenance rule for branch / worktree / branch_source on add and
  update, and ONE string normalization for stored and filtered worktree paths.
- K-39: update / delete serialize with release on the project write lock.

Every test runs on the in-memory SQLite engine and on the PostgreSQL lane.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa

from dct.core import changelog
from dct.core.changelog import (
    add_entry,
    count_unreleased,
    delete_entry,
    list_entries,
    release_entries,
    update_entry,
)
from dct.core.items import create_item, delete_item
from dct.core.projects import create_project
from dct.db import changelog_entries, create_tables, get_engine, projects


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    create_project(engine, slug="proj-b", name="Project B", path="/tmp/proj-b")
    return engine


def _snapshot(engine):
    """Every changelog + project column — to prove a call wrote nothing."""
    with engine.begin() as conn:
        entries = [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))]
        projs = [tuple(r) for r in conn.execute(sa.select(projects).order_by(projects.c.id))]
    return entries, projs


def _row(engine, entry_id, slug="proj-a"):
    return {r["id"]: r for r in list_entries(engine, slug)}[entry_id]


# ── K-37: related item validation ─────────────────────────────────────────────


@pytest.fixture
def bad_items(proj):
    """An unknown id, another project's live item, and a soft-deleted own item."""
    foreign = create_item(proj, "proj-b", "todo", "Theirs")["id"]
    deleted = create_item(proj, "proj-a", "todo", "Gone")["id"]
    delete_item(proj, deleted)
    return {"unknown": 987654, "foreign": foreign, "deleted": deleted}


@pytest.mark.parametrize("case", ["unknown", "foreign", "deleted"])
def test_add_entry_refuses_a_bad_related_item_and_writes_nothing(proj, bad_items, case):
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=r"Unknown item id\(s\) for this project"):
        add_entry(proj, "proj-a", "added", "x", related_item_id=bad_items[case])
    assert _snapshot(proj) == before


@pytest.mark.parametrize("case", ["unknown", "foreign", "deleted"])
def test_update_entry_refuses_a_bad_related_item_and_writes_nothing(proj, bad_items, case):
    eid = add_entry(proj, "proj-a", "added", "x")["id"]
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=r"Unknown item id\(s\) for this project"):
        update_entry(proj, "proj-a", eid, entry="changed", related_item_id=bad_items[case])
    assert _snapshot(proj) == before


@pytest.mark.parametrize("bad", [0, -1, -2])
def test_non_positive_related_item_ids_are_refused_not_stored(proj, bad):
    """K-47: 0 / negative ids never reach the FK — a controlled refusal on
    both writers (the MCP sentinels 0 / -1 are mapped before core)."""
    eid = add_entry(proj, "proj-a", "added", "x")["id"]
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="Unknown item id"):
        add_entry(proj, "proj-a", "added", "y", related_item_id=bad)
    with pytest.raises(ValueError, match="Unknown item id"):
        update_entry(proj, "proj-a", eid, related_item_id=bad)
    assert _snapshot(proj) == before


def test_live_same_project_item_links_and_unlink_stays_legal(proj):
    item = create_item(proj, "proj-a", "todo", "Mine", short_title="Mine label")["id"]
    eid = add_entry(proj, "proj-a", "added", "x", related_item_id=item)["id"]
    assert _row(proj, eid)["item_short_title"] == "Mine label"
    assert update_entry(proj, "proj-a", eid, related_item_id=None)["related_item_id"] is None


def test_label_join_skips_deleted_items_and_keeps_the_historical_id(proj):
    item = create_item(proj, "proj-a", "todo", "Later deleted", short_title="Old label")["id"]
    eid = add_entry(proj, "proj-a", "added", "x", related_item_id=item)["id"]
    delete_item(proj, item)
    row = _row(proj, eid)
    assert row["related_item_id"] == item  # history is never rewritten
    assert row["item_short_title"] is None


def test_label_join_never_labels_another_projects_item(proj):
    """A historical cross-project link (written before validation existed)
    keeps its id but never shows the foreign item's label."""
    foreign = create_item(proj, "proj-b", "todo", "Secret", short_title="Theirs")["id"]
    eid = add_entry(proj, "proj-a", "added", "x")["id"]
    with proj.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == eid)
                     .values(related_item_id=foreign))
    row = _row(proj, eid)
    assert row["related_item_id"] == foreign
    assert row["item_short_title"] is None


def test_rows_carry_item_short_title_not_the_old_name(proj):
    """K-38: one name for a row that references an item."""
    eid = add_entry(proj, "proj-a", "added", "x")["id"]
    row = _row(proj, eid)
    assert "item_short_title" in row and "related_item_short_title" not in row


# ── K-36: server-detected = uncertain ─────────────────────────────────────────


@pytest.fixture
def sourced(proj):
    ids = {
        "client_x": add_entry(proj, "proj-a", "added", "a", branch="feat/x")["id"],
        "server_main": add_entry(proj, "proj-a", "added", "b", branch="main",
                                 worktree="/tmp/proj-a", branch_source="server")["id"],
        "server_x": add_entry(proj, "proj-a", "added", "c", branch="feat/x",
                              branch_source="server")["id"],
        "client_y": add_entry(proj, "proj-a", "added", "d", branch="feat/y")["id"],
        "none": add_entry(proj, "proj-a", "added", "e")["id"],
    }
    return proj, ids


def _ids(rows):
    return {r["id"] for r in rows}


def test_include_unbranched_counts_server_detected_rows_of_any_branch(sourced):
    engine, ids = sourced
    expected = {ids["client_x"], ids["server_x"], ids["server_main"], ids["none"]}
    rows = list_entries(engine, "proj-a", branch="feat/x", include_unbranched=True)
    assert _ids(rows) == expected
    assert count_unreleased(engine, "proj-a", branch="feat/x", include_unbranched=True) == 4


def test_uncertain_rows_keep_their_stored_metadata_for_diagnostics(sourced):
    engine, ids = sourced
    rows = {r["id"]: r for r in list_entries(
        engine, "proj-a", branch="feat/y", include_unbranched=True)}
    server = rows[ids["server_main"]]
    assert (server["branch"], server["worktree"], server["branch_source"]) == (
        "main", "/tmp/proj-a", "server")


def test_plain_branch_filter_is_an_exact_match_whatever_the_source(sourced):
    engine, ids = sourced
    assert _ids(list_entries(engine, "proj-a", branch="feat/x")) == {
        ids["client_x"], ids["server_x"]}
    assert _ids(list_entries(engine, "proj-a", branch="main")) == {ids["server_main"]}
    assert count_unreleased(engine, "proj-a", branch="main") == 1


# ── K-42: one provenance rule ─────────────────────────────────────────────────


def test_add_entry_with_both_values_null_has_no_source(proj):
    e = add_entry(proj, "proj-a", "added", "x", branch_source="server")
    assert (e["branch"], e["worktree"], e["branch_source"]) == (None, None, None)


def test_update_clearing_both_fields_leaves_no_source(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="feat/x", worktree="/tmp/wt")["id"]
    row = update_entry(proj, "proj-a", eid, branch=None, worktree=None)
    assert (row["branch"], row["worktree"], row["branch_source"]) == (None, None, None)


def test_update_branch_only_drops_an_unconfirmed_server_worktree(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="main", worktree="/tmp/proj-a",
                    branch_source="server")["id"]
    row = update_entry(proj, "proj-a", eid, branch="feat/x")
    assert (row["branch"], row["worktree"], row["branch_source"]) == ("feat/x", None, "client")


def test_update_worktree_only_drops_an_unconfirmed_server_branch(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="main", worktree="/tmp/proj-a",
                    branch_source="server")["id"]
    row = update_entry(proj, "proj-a", eid, worktree="/tmp/wt-x")
    assert (row["branch"], row["worktree"], row["branch_source"]) == (None, "/tmp/wt-x", "client")


def test_update_confirming_both_server_values_marks_them_client(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="main", worktree="/tmp/proj-a",
                    branch_source="server")["id"]
    row = update_entry(proj, "proj-a", eid, branch="main", worktree="/tmp/proj-a")
    assert (row["branch"], row["worktree"], row["branch_source"]) == (
        "main", "/tmp/proj-a", "client")


def test_update_branch_only_keeps_a_client_worktree(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="feat/x", worktree="/tmp/wt")["id"]
    row = update_entry(proj, "proj-a", eid, branch="feat/y")
    assert (row["branch"], row["worktree"], row["branch_source"]) == ("feat/y", "/tmp/wt", "client")


def test_update_without_branch_fields_never_touches_provenance(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="main", worktree="/tmp/proj-a",
                    branch_source="server")["id"]
    row = update_entry(proj, "proj-a", eid, entry="reworded")
    assert (row["branch"], row["worktree"], row["branch_source"]) == (
        "main", "/tmp/proj-a", "server")


def test_update_clearing_the_branch_of_a_server_row_clears_its_worktree_too(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="main", worktree="/tmp/proj-a",
                    branch_source="server")["id"]
    row = update_entry(proj, "proj-a", eid, branch=None)
    assert (row["branch"], row["worktree"], row["branch_source"]) == (None, None, None)


# ── K-42: one path normalization ──────────────────────────────────────────────


@pytest.mark.parametrize("raw,stored", [
    ("/tmp/wt-x/", "/tmp/wt-x"),
    ("/tmp//wt-x", "/tmp/wt-x"),
    ("/tmp/./wt-x", "/tmp/wt-x"),
    ("/tmp/other/../wt-x", "/tmp/wt-x"),
    ("/", "/"),
])
def test_stored_worktree_is_string_normalized(proj, raw, stored):
    e = add_entry(proj, "proj-a", "added", "x", worktree=raw)
    assert e["worktree"] == stored
    eid = add_entry(proj, "proj-a", "added", "y")["id"]
    assert update_entry(proj, "proj-a", eid, worktree=raw)["worktree"] == stored


def test_client_worktree_is_never_resolved_against_the_servers_filesystem(proj, tmp_path):
    """A client path may live in another namespace: no realpath, no symlink
    resolution, no existence check — only the string normalization."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    e = add_entry(proj, "proj-a", "added", "x", worktree=f"{link}/")
    assert e["worktree"] == str(link)
    missing = add_entry(proj, "proj-a", "added", "y", worktree="/no/such/dir")
    assert missing["worktree"] == "/no/such/dir"


@pytest.mark.parametrize("bad", ["wt-x", "./wt-x", "../wt-x", "~/wt-x", "tmp/wt"])
def test_relative_worktree_is_refused_on_every_path(proj, bad):
    eid = add_entry(proj, "proj-a", "added", "x")["id"]
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="worktree must be an absolute path"):
        add_entry(proj, "proj-a", "added", "y", worktree=bad)
    with pytest.raises(ValueError, match="worktree must be an absolute path"):
        update_entry(proj, "proj-a", eid, worktree=bad)
    with pytest.raises(ValueError, match="worktree must be an absolute path"):
        list_entries(proj, "proj-a", worktree=bad)
    with pytest.raises(ValueError, match="worktree must be an absolute path"):
        count_unreleased(proj, "proj-a", worktree=bad)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("query", ["/tmp/wt-x", "/tmp/wt-x/", "/tmp//wt-x", "/tmp/a/../wt-x"])
def test_worktree_filter_uses_the_same_normalization(proj, query):
    eid = add_entry(proj, "proj-a", "added", "x", worktree="/tmp/wt-x/")["id"]
    add_entry(proj, "proj-a", "added", "y", worktree="/tmp/wt-other")
    assert _ids(list_entries(proj, "proj-a", worktree=query)) == {eid}
    assert count_unreleased(proj, "proj-a", worktree=query) == 1


def test_historical_unnormalized_rows_are_not_rewritten(proj):
    eid = add_entry(proj, "proj-a", "added", "x")["id"]
    with proj.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == eid)
                     .values(worktree="/tmp/legacy/"))
    update_entry(proj, "proj-a", eid, entry="reworded")
    assert _row(proj, eid)["worktree"] == "/tmp/legacy/"


# ── K-39: update / delete serialize with release ──────────────────────────────


@pytest.fixture
def concurrent_engine(engine, tmp_path):
    """The guarded PG lane, or a file SQLite database with real connections."""
    if engine.dialect.name == "postgresql":
        yield engine
        return
    eng = get_engine(f"sqlite:///{tmp_path / 'clog.db'}")
    create_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_edit_committed_before_the_release_is_included(concurrent_engine, monkeypatch):
    """The edit holds the project lock; a release started meanwhile waits and
    then stamps the EDITED row (PG before K-39: the release snapshot missed
    the edit and its UPDATE died with SQLSTATE 40001)."""
    eng = concurrent_engine
    create_project(eng, "proj-c", "C", current_version="1.0.0")
    eid = add_entry(eng, "proj-c", "added", "original")["id"]
    original = changelog._load_unreleased_entry

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def read_then_start_release(conn, project_id, entry_id):
            row = original(conn, project_id, entry_id)
            futures.append(pool.submit(release_entries, eng, "proj-c", "1.1.0"))
            Event().wait(0.5)  # the release must be blocked on the lock now
            return row

        monkeypatch.setattr(changelog, "_load_unreleased_entry", read_then_start_release)
        update_entry(eng, "proj-c", eid, entry="edited")
        result = futures[0].result(timeout=15)
    assert result["released"] == 1
    row = _row(eng, eid, "proj-c")
    assert (row["entry"], row["version"]) == ("edited", "1.1.0")


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_edit_after_the_release_gets_the_controlled_refusal(
    concurrent_engine, monkeypatch, operation,
):
    """The release holds the project lock; an edit started meanwhile waits and
    is then refused as 'already released' — never a raw serialization error."""
    eng = concurrent_engine
    create_project(eng, "proj-c", "C", current_version="1.0.0")
    eid = add_entry(eng, "proj-c", "added", "original")["id"]
    from dct.core import records

    validate = records.validate_project_rows

    def edit():
        if operation == "update":
            return update_entry(eng, "proj-c", eid, entry="too late")
        return delete_entry(eng, "proj-c", eid)

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def validate_then_start_edit(*args, **kwargs):
            result = validate(*args, **kwargs)
            futures.append(pool.submit(edit))
            Event().wait(0.5)  # the edit must be blocked on the lock now
            return result

        monkeypatch.setattr(records, "validate_project_rows", validate_then_start_edit)
        released = release_entries(eng, "proj-c", "1.1.0", entry_ids=[eid])
        with pytest.raises(ValueError, match="already released"):
            futures[0].result(timeout=15)
    assert released["released"] == 1
    row = _row(eng, eid, "proj-c")
    assert (row["entry"], row["version"]) == ("original", "1.1.0")
