"""Changelog branch metadata, filters, selective release and edit/delete
(#801 C3-C6, #359) — core layer."""

import shutil

import pytest
import sqlalchemy as sa
from conftest import git, make_git_repo

from dct.core.changelog import (
    add_entry,
    count_unreleased,
    delete_entry,
    list_entries,
    release_entries,
    resolve_branch_fields,
    update_entry,
)
from dct.core.items import create_item
from dct.core.projects import create_project, get_project
from dct.db import changelog_entries, projects

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    return engine


def _add(engine, text, branch=None, worktree=None, slug="proj-a", **kw):
    return add_entry(engine, slug, "added", text, branch=branch, worktree=worktree, **kw)["id"]


def _snapshot(engine):
    """Every changelog + project column — to prove a call wrote nothing."""
    with engine.begin() as conn:
        entries = [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))]
        projs = [tuple(r) for r in conn.execute(sa.select(projects).order_by(projects.c.id))]
    return entries, projs


# ── C3: add + resolution rule ─────────────────────────────────────────────────


def test_add_entry_explicit_branch_defaults_source_client(proj):
    e = add_entry(proj, "proj-a", "added", "x", branch="feat/x", worktree="/tmp/proj-a")
    assert (e["branch"], e["worktree"], e["branch_source"]) == ("feat/x", "/tmp/proj-a", "client")


def test_add_entry_without_branch_stores_nulls(proj):
    e = add_entry(proj, "proj-a", "added", "x")
    assert (e["branch"], e["worktree"], e["branch_source"]) == (None, None, None)


@pytest.mark.parametrize("field,limit", [("branch", 255), ("worktree", 500)])
def test_add_entry_refuses_over_long_values_and_writes_nothing(proj, field, limit):
    with pytest.raises(ValueError, match=rf"{field} too long: {limit + 1} chars, max {limit}"):
        add_entry(proj, "proj-a", "added", "x", **{field: "b" * (limit + 1)})
    assert list_entries(proj, "proj-a") == []


def test_add_entry_exact_limit_stored_untruncated(proj):
    e = add_entry(proj, "proj-a", "added", "x", branch="b" * 255, worktree="w" * 500)
    assert len(e["branch"]) == 255 and len(e["worktree"]) == 500


def test_add_entry_rejects_unknown_branch_source(proj):
    with pytest.raises(ValueError, match="branch_source"):
        add_entry(proj, "proj-a", "added", "x", branch="b", branch_source="guess")


def test_resolve_explicit_values_win_and_are_client(proj, tmp_path):
    out = resolve_branch_fields(proj, "proj-a", branch="feat/x", cwd=str(tmp_path),
                                detected_source="server")
    assert out == {"branch": "feat/x", "worktree": None, "branch_source": "client"}


def test_resolve_explicit_over_long_is_refused(proj):
    with pytest.raises(ValueError, match="branch too long"):
        resolve_branch_fields(proj, "proj-a", branch="b" * 256)


def test_resolve_no_cwd_and_no_values_is_all_null(proj):
    assert resolve_branch_fields(proj, "proj-a") == {
        "branch": None, "worktree": None, "branch_source": None}


def test_resolve_rejects_unknown_detected_source(proj):
    with pytest.raises(ValueError):
        resolve_branch_fields(proj, "proj-a", detected_source="guess")


@needs_git
def test_resolve_detects_inside_project_path_with_given_source(engine, tmp_path):
    root = make_git_repo(tmp_path / "repo", branch="feat/x")
    create_project(engine, slug="proj-g", name="G", path=root)
    for source in ("server", "client"):
        assert resolve_branch_fields(engine, "proj-g", cwd=root, detected_source=source) == {
            "branch": "feat/x", "worktree": root, "branch_source": source}


@needs_git
def test_resolve_detects_a_linked_worktree_inside_the_project(engine, tmp_path):
    root = make_git_repo(tmp_path / "repo")
    linked = tmp_path / "repo" / ".worktrees" / "wt"
    git(root, "worktree", "add", "-q", "-b", "feat/wt", str(linked))
    create_project(engine, slug="proj-g", name="G", path=root)
    out = resolve_branch_fields(engine, "proj-g", cwd=str(linked), detected_source="server")
    assert out["branch"] == "feat/wt"
    assert out["worktree"] == str(linked.resolve())


@needs_git
def test_resolve_ignores_a_checkout_outside_the_project(engine, tmp_path):
    """The MCP server started in ANOTHER repo must not stamp that repo's branch."""
    create_project(engine, slug="proj-g", name="G", path=str(tmp_path / "proj"))
    (tmp_path / "proj").mkdir()
    elsewhere = make_git_repo(tmp_path / "elsewhere", branch="feat/other")
    assert resolve_branch_fields(engine, "proj-g", cwd=elsewhere, detected_source="server") == {
        "branch": None, "worktree": None, "branch_source": None}


@needs_git
def test_resolve_project_without_path_is_null(engine, tmp_path):
    root = make_git_repo(tmp_path / "repo")
    create_project(engine, slug="proj-g", name="G")
    assert resolve_branch_fields(engine, "proj-g", cwd=root)["branch_source"] is None


@needs_git
def test_resolve_detached_head_keeps_worktree(engine, tmp_path):
    root = make_git_repo(tmp_path / "repo")
    git(root, "checkout", "-q", "--detach")
    create_project(engine, slug="proj-g", name="G", path=root)
    assert resolve_branch_fields(engine, "proj-g", cwd=root, detected_source="server") == {
        "branch": None, "worktree": root, "branch_source": "server"}


@needs_git
def test_resolve_over_long_detected_branch_becomes_null_never_blocks(engine, tmp_path):
    # Nested components: one ref-file name component may not exceed 255 bytes.
    long_branch = "feat/" + "/".join(["x" * 50] * 6)
    assert len(long_branch) > 255
    root = make_git_repo(tmp_path / "repo", branch=long_branch)
    create_project(engine, slug="proj-g", name="G", path=root)
    out = resolve_branch_fields(engine, "proj-g", cwd=root, detected_source="server")
    assert out == {"branch": None, "worktree": root, "branch_source": "server"}
    add_entry(engine, "proj-g", "added", "still stored", **out)


@needs_git
def test_resolve_detects_a_linked_worktree_outside_the_project_path(engine, tmp_path):
    """K-34: same repository (git common dir), not "inside projects.path"."""
    root = make_git_repo(tmp_path / "repo")
    linked = tmp_path / "elsewhere" / "wt"
    git(root, "worktree", "add", "-q", "-b", "feat/out", str(linked))
    create_project(engine, slug="proj-g", name="G", path=root)
    assert resolve_branch_fields(engine, "proj-g", cwd=str(linked), detected_source="client") == {
        "branch": "feat/out", "worktree": str(linked.resolve()), "branch_source": "client"}


@needs_git
def test_resolve_ignores_a_nested_foreign_repo_inside_the_project(engine, tmp_path):
    root = make_git_repo(tmp_path / "repo")
    nested = make_git_repo(tmp_path / "repo" / "vendor" / "lib", branch="feat/vendor")
    create_project(engine, slug="proj-g", name="G", path=root)
    assert resolve_branch_fields(engine, "proj-g", cwd=nested, detected_source="server") == {
        "branch": None, "worktree": None, "branch_source": None}


@needs_git
def test_resolve_monorepo_subproject_stores_the_checkout_root(engine, tmp_path):
    root = make_git_repo(tmp_path / "mono", branch="main")
    app = tmp_path / "mono" / "packages" / "app"
    app.mkdir(parents=True)
    create_project(engine, slug="proj-g", name="G", path=str(app))
    out = resolve_branch_fields(engine, "proj-g", cwd=root, detected_source="server")
    assert out == {"branch": "main", "worktree": root, "branch_source": "server"}


@needs_git
def test_resolve_process_cwd_is_read_only_when_detection_is_needed(engine, tmp_path, monkeypatch):
    from dct.core import gitinfo

    root = make_git_repo(tmp_path / "repo", branch="feat/x")
    create_project(engine, slug="proj-g", name="G", path=root)

    def boom():
        raise AssertionError("getcwd must not be read")

    monkeypatch.setattr(gitinfo.os, "getcwd", boom)
    out = resolve_branch_fields(engine, "proj-g", branch="feat/y", use_process_cwd=True)
    assert out == {"branch": "feat/y", "worktree": None, "branch_source": "client"}
    monkeypatch.undo()
    monkeypatch.chdir(root)
    assert resolve_branch_fields(engine, "proj-g", use_process_cwd=True,
                                 detected_source="server")["branch"] == "feat/x"


@needs_git
def test_resolve_deleted_process_cwd_is_no_context(engine, tmp_path, monkeypatch):
    import os

    root = make_git_repo(tmp_path / "repo")
    create_project(engine, slug="proj-g", name="G", path=root)
    gone = tmp_path / "repo" / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    os.rmdir(gone)
    assert resolve_branch_fields(engine, "proj-g", use_process_cwd=True) == {
        "branch": None, "worktree": None, "branch_source": None}


# ── C4: filters + label ───────────────────────────────────────────────────────


@pytest.fixture
def mixed(proj):
    ids = {
        "x1": _add(proj, "x1", branch="feat/x", worktree="/tmp/wt-x"),
        "x2": _add(proj, "x2", branch="feat/x", worktree="/tmp/wt-other"),
        "y": _add(proj, "y", branch="feat/y"),
        "none": _add(proj, "none"),
    }
    return proj, ids


def _ids(rows):
    return {r["id"] for r in rows}


def test_unfiltered_list_and_count_return_everything(mixed):
    engine, ids = mixed
    assert _ids(list_entries(engine, "proj-a")) == set(ids.values())
    assert count_unreleased(engine, "proj-a") == 4


def test_include_unbranched_is_ignored_without_branch(mixed):
    engine, ids = mixed
    assert _ids(list_entries(engine, "proj-a", include_unbranched=True)) == set(ids.values())
    assert count_unreleased(engine, "proj-a", include_unbranched=True) == 4


def test_branch_filter_excludes_other_branches_and_unbranched(mixed):
    engine, ids = mixed
    assert _ids(list_entries(engine, "proj-a", branch="feat/x")) == {ids["x1"], ids["x2"]}
    assert count_unreleased(engine, "proj-a", branch="feat/x") == 2


def test_branch_filter_with_include_unbranched_adds_null_rows(mixed):
    engine, ids = mixed
    rows = list_entries(engine, "proj-a", branch="feat/x", include_unbranched=True)
    assert _ids(rows) == {ids["x1"], ids["x2"], ids["none"]}
    assert count_unreleased(engine, "proj-a", branch="feat/x", include_unbranched=True) == 3


def test_unknown_branch_counts_zero_but_unbranched_still_count(mixed):
    engine, _ = mixed
    assert count_unreleased(engine, "proj-a", branch="feat/nope") == 0
    assert count_unreleased(engine, "proj-a", branch="feat/nope", include_unbranched=True) == 1


def test_worktree_filter_is_exact_and_combines_with_branch(mixed):
    engine, ids = mixed
    assert _ids(list_entries(engine, "proj-a", worktree="/tmp/wt-x")) == {ids["x1"]}
    assert _ids(list_entries(engine, "proj-a", worktree="/tmp/wt")) == set()
    assert count_unreleased(engine, "proj-a", branch="feat/y", worktree="/tmp/wt-x") == 0
    assert count_unreleased(engine, "proj-a", branch="feat/x", worktree="/tmp/wt-x") == 1


def test_filters_respect_released_and_version_arguments(mixed):
    engine, ids = mixed
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"]])
    assert count_unreleased(engine, "proj-a", branch="feat/x") == 1
    released = list_entries(engine, "proj-a", version="1.1.0", branch="feat/x")
    assert _ids(released) == {ids["x1"]}
    assert _ids(list_entries(engine, "proj-a", unreleased_only=True, branch="feat/x")) == {ids["x2"]}


def test_all_null_database_unfiltered_output_unchanged(proj):
    """K-21: on a pre-#801 database (every branch NULL) the unfiltered list /
    count are exactly the legacy result — same ids, same order."""
    for i in range(3):
        _add(proj, f"e{i}")
    rows = list_entries(proj, "proj-a")
    with proj.begin() as conn:
        legacy = [r.id for r in conn.execute(
            sa.select(changelog_entries.c.id).where(changelog_entries.c.deleted_at.is_(None))
            .order_by(changelog_entries.c.created_at.desc(), changelog_entries.c.id.desc()))]
    assert [r["id"] for r in rows] == legacy
    assert count_unreleased(proj, "proj-a") == 3


def test_related_item_short_title_label(proj):
    labelled = create_item(proj, "proj-a", "todo", "A very long descriptive title",
                           short_title="Hook paths")
    plain = create_item(proj, "proj-a", "todo", "t" * 80)
    _add(proj, "a", related_item_id=labelled["id"])
    _add(proj, "b", related_item_id=plain["id"])
    _add(proj, "c")
    by_entry = {r["entry"]: r for r in list_entries(proj, "proj-a")}
    assert by_entry["a"]["related_item_short_title"] == "Hook paths"
    assert by_entry["b"]["related_item_short_title"] == "t" * 49 + "…"
    assert by_entry["c"]["related_item_short_title"] is None
    # The join never leaks helper columns or duplicates rows.
    assert len(by_entry) == 3
    assert not any(k.startswith("_item") for k in by_entry["a"])


# ── C5: release ───────────────────────────────────────────────────────────────


def test_release_all_legacy_mode(mixed):
    engine, ids = mixed
    out = release_entries(engine, "proj-a", "1.1.0")
    assert out["mode"] == "all"
    assert out["released"] == 4
    assert sorted(out["entry_ids"]) == sorted(ids.values())
    assert out["current_version"] == "1.1.0"
    assert out["already_released"] == []
    assert count_unreleased(engine, "proj-a") == 0
    assert get_project(engine, "proj-a")["current_version"] == "1.1.0"


def test_release_all_warns_when_branches_are_mixed(mixed):
    engine, _ = mixed
    warning = release_entries(engine, "proj-a", "1.1.0")["warning"]
    assert "3 branches" in warning
    for name in ("feat/x", "feat/y", "(unbranched)"):
        assert name in warning


def test_release_all_single_branch_has_no_warning(proj):
    _add(proj, "a", branch="main")
    _add(proj, "b", branch="main")
    assert "warning" not in release_entries(proj, "proj-a", "1.1.0")


def test_release_all_only_unbranched_has_no_warning(proj):
    _add(proj, "a")
    assert "warning" not in release_entries(proj, "proj-a", "1.1.0")


def test_release_selected_stamps_exactly_the_given_set(mixed):
    engine, ids = mixed
    out = release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"], ids["none"]])
    assert out["mode"] == "selected"
    assert out["released"] == 2
    assert out["entry_ids"] == [ids["x1"], ids["none"]]
    assert "warning" not in out
    stamped = _ids(list_entries(engine, "proj-a", version="1.1.0"))
    assert stamped == {ids["x1"], ids["none"]}
    assert _ids(list_entries(engine, "proj-a", unreleased_only=True)) == {ids["x2"], ids["y"]}
    row = next(r for r in list_entries(engine, "proj-a") if r["id"] == ids["x1"])
    assert row["released_at"] is not None


def test_release_selected_dedupes_ids(mixed):
    engine, ids = mixed
    out = release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["y"], ids["y"], ids["y"]])
    assert out["entry_ids"] == [ids["y"]]
    assert out["released"] == 1


@pytest.mark.parametrize("case", ["unknown", "foreign", "deleted"])
def test_release_selected_is_strict_and_atomic(mixed, case):
    engine, ids = mixed
    if case == "unknown":
        bad = 999_999
    elif case == "foreign":
        create_project(engine, slug="proj-b", name="B")
        bad = _add(engine, "b", slug="proj-b")
    else:
        bad = _add(engine, "gone")
        delete_entry(engine, "proj-a", bad)
    before = _snapshot(engine)
    with pytest.raises(ValueError, match=r"Unknown changelog entry id\(s\)"):
        release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"], bad])
    assert _snapshot(engine) == before


def test_release_selected_entry_of_another_version_fails_whole_call(mixed):
    engine, ids = mixed
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["y"]])
    before = _snapshot(engine)
    with pytest.raises(ValueError, match=r"another version: #\d+ \(1\.1\.0\)"):
        release_entries(engine, "proj-a", "1.2.0", entry_ids=[ids["x1"], ids["y"]])
    assert _snapshot(engine) == before


def test_release_selected_retry_is_a_noop_without_any_write(mixed):
    engine, ids = mixed
    first = release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"], ids["x2"]])
    before = _snapshot(engine)
    again = release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x2"], ids["x1"]])
    assert _snapshot(engine) == before
    assert again["released"] == 0
    assert again["already_released"] == [ids["x2"], ids["x1"]]
    assert again["current_version"] == first["current_version"] == "1.1.0"


def test_release_retry_after_a_later_release_is_noop_not_rewind(mixed):
    """K-20 order: the no-op check runs BEFORE the no-rewind check."""
    engine, ids = mixed
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"]])
    release_entries(engine, "proj-a", "1.2.0", entry_ids=[ids["x2"]])
    before = _snapshot(engine)
    out = release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"]])
    assert _snapshot(engine) == before
    assert out["current_version"] == "1.2.0"
    assert out["already_released"] == [ids["x1"]]


def test_release_partial_retry_stamps_only_the_rest(mixed):
    engine, ids = mixed
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"]])
    out = release_entries(engine, "proj-a", "1.1.0", entry_ids=[ids["x1"], ids["x2"]])
    assert out["released"] == 1
    assert out["already_released"] == [ids["x1"]]
    assert _ids(list_entries(engine, "proj-a", version="1.1.0")) == {ids["x1"], ids["x2"]}


@pytest.mark.parametrize("entry_ids", [None, [], "selected"])
def test_release_never_rewinds(mixed, entry_ids):
    engine, ids = mixed
    selection = [ids["y"]] if entry_ids == "selected" else entry_ids
    before = _snapshot(engine)
    with pytest.raises(ValueError, match="lower than the current version 1.0.0"):
        release_entries(engine, "proj-a", "0.9.0", entry_ids=selection)
    assert _snapshot(engine) == before


def test_release_equal_version_with_trailing_zero_is_not_a_rewind(proj):
    with proj.begin() as conn:
        conn.execute(projects.update().values(current_version="1.5"))
    _add(proj, "a")
    assert release_entries(proj, "proj-a", "1.5.0")["released"] == 1


def test_release_unparsable_versions_are_not_compared(proj):
    with proj.begin() as conn:
        conn.execute(projects.update().values(current_version="2026-rc"))
    _add(proj, "a")
    assert release_entries(proj, "proj-a", "0.1.0")["current_version"] == "0.1.0"


def test_release_version_only_sets_version_and_stamps_nothing(mixed):
    engine, _ = mixed
    out = release_entries(engine, "proj-a", "1.1.0", entry_ids=[])
    assert out["mode"] == "version_only"
    assert (out["released"], out["entry_ids"], out["already_released"]) == (0, [], [])
    assert out["current_version"] == "1.1.0"
    assert count_unreleased(engine, "proj-a") == 4
    before = _snapshot(engine)
    again = release_entries(engine, "proj-a", "1.1.0", entry_ids=[])
    assert _snapshot(engine) == before
    assert again["current_version"] == "1.1.0"


def test_release_all_with_nothing_unreleased_and_same_version_is_noop(proj):
    before = _snapshot(proj)
    out = release_entries(proj, "proj-a", "1.0.0")
    assert _snapshot(proj) == before
    assert out["released"] == 0


@pytest.mark.parametrize("bump_type,expected", [
    ("patch", "1.0.1"), ("minor", "1.1.0"), ("major", "2.0.0")])
def test_release_bump_type_computed_in_transaction_and_returned(proj, bump_type, expected):
    _add(proj, "a")
    out = release_entries(proj, "proj-a", bump_type=bump_type)
    assert out["version"] == out["current_version"] == expected


def test_release_bump_without_current_version_errors(engine):
    create_project(engine, slug="proj-n", name="N", current_version=None, path=None)
    with pytest.raises(ValueError, match="No current version"):
        release_entries(engine, "proj-n", bump_type="patch")


@pytest.mark.parametrize("kwargs", [{}, {"version": "1.1.0", "bump_type": "patch"}])
def test_release_requires_exactly_one_of_version_or_bump(proj, kwargs):
    with pytest.raises(ValueError, match="exactly one"):
        release_entries(proj, "proj-a", **kwargs)


def test_release_invalid_bump_type_errors(proj):
    with pytest.raises(ValueError, match="Invalid bump type"):
        release_entries(proj, "proj-a", bump_type="huge")


@pytest.mark.parametrize("bad", [["1"], [1.0], [True]])
def test_release_non_integer_ids_refused(proj, bad):
    with pytest.raises(TypeError, match="integers"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=bad)


def test_release_is_project_scoped(engine):
    create_project(engine, slug="proj-a", name="A", current_version="1.0.0")
    create_project(engine, slug="proj-b", name="B", current_version="3.0.0")
    _add(engine, "a")
    b = _add(engine, "b", slug="proj-b")
    release_entries(engine, "proj-a", "1.1.0")
    assert count_unreleased(engine, "proj-b") == 1
    assert get_project(engine, "proj-b")["current_version"] == "3.0.0"
    assert list_entries(engine, "proj-b")[0]["id"] == b


# ── C6: update / delete ───────────────────────────────────────────────────────


def test_update_entry_fields_and_updated_at(proj):
    eid = _add(proj, "old text", component="core")
    before = list_entries(proj, "proj-a")[0]
    row = update_entry(proj, "proj-a", eid, entry="new text", category="fixed",
                       component=None, source_file="dct/x.py", source_lines="1-2")
    assert row["entry"] == "new text" and row["category"] == "fixed"
    assert row["component"] is None
    assert (row["source_file"], row["source_lines"]) == ("dct/x.py", "1-2")
    assert row["updated_at"] >= before["updated_at"]


def test_update_entry_branch_marks_client_and_can_clear(proj):
    eid = add_entry(proj, "proj-a", "added", "x", branch="feat/x",
                    branch_source="server")["id"]
    row = update_entry(proj, "proj-a", eid, branch="feat/y")
    assert (row["branch"], row["branch_source"]) == ("feat/y", "client")
    row = update_entry(proj, "proj-a", eid, branch=None, worktree=None)
    assert row["branch"] is None and row["worktree"] is None


def test_update_entry_related_item_set_and_clear(proj):
    item = create_item(proj, "proj-a", "todo", "T", short_title="Label")
    eid = _add(proj, "x")
    assert update_entry(proj, "proj-a", eid, related_item_id=item["id"])["related_item_id"] == item["id"]
    assert update_entry(proj, "proj-a", eid, related_item_id=None)["related_item_id"] is None


@pytest.mark.parametrize("fields,match", [
    ({}, "Nothing to update"),
    ({"entry": ""}, "cannot be empty"),
    ({"entry": "   "}, "cannot be empty"),
    ({"category": "improved"}, "Invalid category"),
    ({"branch": "b" * 256}, "branch too long"),
    ({"component": "c" * 101}, "component too long"),
    ({"version": "9.9"}, "Unknown changelog field"),
])
def test_update_entry_refusals_write_nothing(proj, fields, match):
    eid = _add(proj, "x")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=match):
        update_entry(proj, "proj-a", eid, **fields)
    assert _snapshot(proj) == before


def test_update_and_delete_distinguish_missing_from_released(engine):
    create_project(engine, slug="proj-a", name="A", current_version="1.0.0")
    create_project(engine, slug="proj-b", name="B")
    released = _add(engine, "shipped")
    release_entries(engine, "proj-a", "1.1.0")
    foreign = _add(engine, "theirs", slug="proj-b")
    deleted = _add(engine, "gone")
    delete_entry(engine, "proj-a", deleted)

    for fn, kwargs in ((update_entry, {"entry": "x"}), (delete_entry, {})):
        with pytest.raises(ValueError, match=r"already released \(1\.1\.0\)"):
            fn(engine, "proj-a", released, **kwargs)
        for eid in (foreign, deleted, 424242):
            with pytest.raises(ValueError, match="not found in this project"):
                fn(engine, "proj-a", eid, **kwargs)
    # Nothing moved: the foreign entry is intact, the released one unchanged.
    assert list_entries(engine, "proj-b")[0]["entry"] == "theirs"
    assert list_entries(engine, "proj-a", version="1.1.0")[0]["entry"] == "shipped"


def test_delete_entry_soft_deletes_and_stamps_updated_at(proj):
    eid = _add(proj, "x")
    assert delete_entry(proj, "proj-a", eid) is True
    assert count_unreleased(proj, "proj-a") == 0
    with proj.begin() as conn:
        row = conn.execute(sa.select(changelog_entries).where(changelog_entries.c.id == eid)).first()
    assert row.deleted_at is not None
    assert row.updated_at is not None
    with pytest.raises(ValueError, match="not found"):
        delete_entry(proj, "proj-a", eid)  # second delete: already deleted


# ── K-44: release_entries uses the one version contract ──────────────────────


def test_release_v_prefixed_lower_version_is_refused(proj):
    with proj.begin() as conn:
        conn.execute(projects.update().values(current_version="1.5.0"))
    _add(proj, "a")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="lower than the current version 1.5.0"):
        release_entries(proj, "proj-a", "v1.4.0")
    assert _snapshot(proj) == before


def test_release_v_prefixed_version_is_stored_normalized_and_bumps_cleanly(proj):
    with proj.begin() as conn:
        conn.execute(projects.update().values(current_version="1.5.0"))
    a = _add(proj, "a")
    out = release_entries(proj, "proj-a", "v1.6.0")
    assert out["version"] == out["current_version"] == "1.6.0"
    assert get_project(proj, "proj-a")["current_version"] == "1.6.0"
    assert [e["id"] for e in list_entries(proj, "proj-a", version="1.6.0")] == [a]
    _add(proj, "b")
    assert release_entries(proj, "proj-a", bump_type="patch")["current_version"] == "1.6.1"


@pytest.mark.parametrize("bad", ["1.6.0-rc1", "1", "1.6.0.1", "latest", "vv1.6"])
@pytest.mark.parametrize("entry_ids", [None, [], "selected"])
def test_release_malformed_version_is_refused_without_writes(proj, bad, entry_ids):
    a = _add(proj, "a")
    selection = [a] if entry_ids == "selected" else entry_ids
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="Unsupported version"):
        release_entries(proj, "proj-a", bad, entry_ids=selection)
    assert _snapshot(proj) == before


def test_release_bump_of_unsupported_current_is_refused_without_writes(proj):
    with proj.begin() as conn:
        conn.execute(projects.update().values(current_version="2026-rc"))
    _add(proj, "a")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="Unsupported version"):
        release_entries(proj, "proj-a", bump_type="patch")
    assert _snapshot(proj) == before


def test_release_v_prefixed_retry_of_the_current_version_is_a_noop(proj):
    _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0")
    before = _snapshot(proj)
    out = release_entries(proj, "proj-a", "v1.1.0")
    assert _snapshot(proj) == before
    assert (out["version"], out["current_version"], out["released"]) == ("1.1.0", "1.1.0", 0)


@pytest.mark.parametrize("retry,requested", [
    ("1.1.0", "1.1.0"), ("v1.1.0", "1.1.0"), ("1.1", "1.1")])
def test_release_all_historical_retry_writes_nothing(proj, retry, requested):
    """Release 1.1.0, then 1.2.0, retry 1.1.0 in mode 'all' with nothing
    unreleased: a no-op that keeps current_version, never a rewind."""
    _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0")
    _add(proj, "b")
    release_entries(proj, "proj-a", "1.2.0")
    before = _snapshot(proj)
    out = release_entries(proj, "proj-a", retry)
    assert _snapshot(proj) == before
    assert out["released"] == 0
    assert out["current_version"] == "1.2.0"
    assert out["version"] == requested


def test_release_all_never_released_lower_version_is_still_a_rewind(proj):
    """Nothing unreleased + a LOWER version no entry carries is a rewind
    attempt (a typo), not a historical retry."""
    _add(proj, "a")
    release_entries(proj, "proj-a", "1.2.0")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="lower than the current version"):
        release_entries(proj, "proj-a", "1.1.5")
    assert _snapshot(proj) == before


def test_release_all_with_require_entries_refuses_an_implicit_version_only(proj):
    """K-31: the CLI's consent rule — mode 'all' with nothing to stamp and a
    NEW version would silently become a version-only release."""
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="No unreleased entries"):
        release_entries(proj, "proj-a", "1.1.0", require_entries=True)
    assert _snapshot(proj) == before
    # The no-op retry of the current version stays allowed under the flag.
    assert release_entries(proj, "proj-a", "1.0.0", require_entries=True)["released"] == 0
