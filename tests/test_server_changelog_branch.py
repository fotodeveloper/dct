"""MCP surface of changelog branch metadata, selective release and edit/delete
(#801 C3-C6, #359): ack shapes, the list legend, markup guards, the release
return shape, and — through a real client connection — that `entry_ids` is
advertised and stamps exactly the given set."""

import asyncio
import json
import os
import shutil

import pytest
from conftest import make_git_repo
from mcp import Client

from dct.core.changelog import list_entries
from dct.core.items import create_item as core_create_item
from dct.core.projects import create_project


def _call(srv, name: str, args: dict):
    async def go():
        async with Client(srv.mcp) as client:
            return await client.call_tool(name, args)
    return asyncio.run(go())


def _rows(srv, **kw):
    out = srv.list_changelog(project="test", format="dict", **kw)
    return {r["id"]: r for r in out}


# ── add_changelog ─────────────────────────────────────────────────────────────


def test_add_changelog_explicit_branch_is_client(srv):
    out = srv.add_changelog("added", "x", project="test", branch="feat/x")
    assert (out["branch"], out["branch_source"]) == ("feat/x", "client")
    assert _rows(srv)[out["id"]]["worktree"] is None


def test_add_changelog_over_long_branch_refused_nothing_written(srv):
    out = srv.add_changelog("added", "x", project="test", branch="b" * 256)
    assert set(out) == {"error"} and "branch too long" in out["error"]
    assert _rows(srv) == {}


@pytest.mark.parametrize("field", ["branch", "worktree", "component", "source_file", "source_lines"])
def test_add_changelog_markup_guard_covers_free_text_params(srv, field):
    out = srv.add_changelog("added", "x", project="test", **{field: "</parameter> leak"})
    assert "error" in out and f"field '{field}'" in out["error"]
    assert _rows(srv) == {}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_add_changelog_detects_server_cwd_inside_project_as_server(srv, tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo", branch="feat/server")
    create_project(srv._get_engine(), slug="proj-g", name="G", path=root)
    monkeypatch.chdir(root)
    out = srv.add_changelog("added", "x", project="proj-g")
    assert (out["branch"], out["branch_source"]) == ("feat/server", "server")
    row = list_entries(srv._get_engine(), "proj-g")[0]
    assert row["worktree"] == os.path.realpath(root)


# ── list / count ──────────────────────────────────────────────────────────────


def test_list_changelog_cols_legend(srv):
    out = srv.list_changelog(project="test")
    assert out["cols"] == [
        "id", "version", "category", "component", "branch", "branch_source",
        "related_item_id", "item_short_title", "created_at", "released_at",
        "worktree", "entry",
    ]
    assert out["cols"] == srv.CHANGELOG_COLS


def test_list_changelog_row_carries_item_label(srv):
    item = core_create_item(srv._get_engine(), "test", "todo", "Long title", short_title="Label")
    srv.add_changelog("added", "x", project="test", related_item_id=item["id"])
    out = srv.list_changelog(project="test")
    row = dict(zip(out["cols"], out["rows"][0], strict=True))
    assert row["item_short_title"] == "Label"


def test_list_and_count_branch_filters(srv):
    a = srv.add_changelog("added", "a", project="test", branch="feat/x")["id"]
    b = srv.add_changelog("added", "b", project="test", branch="feat/y")["id"]
    n = srv.add_changelog("added", "n", project="test")["id"]
    assert set(_rows(srv)) == {a, b, n}
    assert set(_rows(srv, branch="feat/x")) == {a}
    assert set(_rows(srv, branch="feat/x", include_unbranched=True)) == {a, n}
    assert srv.count_unreleased_changelog(project="test") == {"count": 3}
    assert srv.count_unreleased_changelog(project="test", branch="feat/x") == {"count": 1}
    assert srv.count_unreleased_changelog(
        project="test", branch="feat/x", include_unbranched=True) == {"count": 2}
    assert srv.count_unreleased_changelog(project="test", include_unbranched=True) == {"count": 3}


# ── release_changelog ─────────────────────────────────────────────────────────


_RELEASE_KEYS = {"project", "version", "current_version", "mode", "entry_ids",
                 "released", "already_released"}


def test_release_changelog_all_mode_adds_warning_on_mixed_branches(srv):
    srv.add_changelog("added", "a", project="test", branch="feat/x")
    srv.add_changelog("added", "b", project="test")
    out = srv.release_changelog(project="test", version="1.0.0")
    assert set(out) == _RELEASE_KEYS | {"warning"}
    assert out["mode"] == "all" and out["released"] == 2


def test_release_changelog_version_wins_over_bump_type(srv):
    out = srv.release_changelog(project="test", version="2.0.0", bump_type="patch", entry_ids=[])
    assert out["version"] == "2.0.0" and out["mode"] == "version_only"


def test_release_changelog_bump_computed_and_returned(srv):
    srv.release_changelog(project="test", version="1.2.3", entry_ids=[])
    out = srv.release_changelog(project="test", bump_type="minor", entry_ids=[])
    assert out["version"] == "1.3.0" and out["current_version"] == "1.3.0"


@pytest.mark.parametrize("kwargs,match", [
    ({}, "Provide version or bump_type"),
    ({"bump_type": "patch"}, "No current version"),
    ({"version": "1.0.0", "entry_ids": [424242]}, "Unknown changelog entry id"),
])
def test_release_changelog_errors(srv, kwargs, match):
    out = srv.release_changelog(project="test", **kwargs)
    assert set(out) == {"error"} and match in out["error"]


def test_release_changelog_rewind_refused(srv):
    srv.release_changelog(project="test", version="2.0.0", entry_ids=[])
    out = srv.release_changelog(project="test", version="1.0.0", entry_ids=[])
    assert "lower than the current version" in out["error"]


def test_release_changelog_schema_advertises_entry_ids():
    from dct.mcp import server as S

    tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
    props = tools["release_changelog"].input_schema["properties"]
    assert "entry_ids" in props
    assert "JSON array" in tools["release_changelog"].description


def test_release_changelog_over_the_wire_stamps_exactly_the_given_set(srv):
    ids = [srv.add_changelog("added", f"e{i}", project="test")["id"] for i in range(4)]
    chosen = [ids[0], ids[2]]
    result = _call(srv, "release_changelog",
                   {"project": "test", "version": "1.0.0", "entry_ids": chosen})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["entry_ids"] == chosen and payload["released"] == 2
    rows = _rows(srv, unreleased_only=False)
    assert {i for i, r in rows.items() if r["version"] == "1.0.0"} == set(chosen)
    assert {i for i, r in rows.items() if r["version"] is None} == {ids[1], ids[3]}


# ── update_changelog / delete_changelog ───────────────────────────────────────


def test_update_changelog_ack_and_applied_fields(srv):
    eid = srv.add_changelog("added", "old", project="test", component="core")["id"]
    out = srv.update_changelog(eid, project="test", entry="new", branch="feat/x",
                               component="__clear__")
    assert out == {"id": eid, "project": "test",
                   "updated_fields": ["entry", "component", "branch"]}
    row = _rows(srv)[eid]
    assert (row["entry"], row["component"], row["branch"], row["branch_source"]) == (
        "new", None, "feat/x", "client")


def test_update_changelog_related_item_set_keep_clear(srv):
    item = core_create_item(srv._get_engine(), "test", "todo", "T")
    eid = srv.add_changelog("added", "x", project="test")["id"]
    srv.update_changelog(eid, project="test", related_item_id=item["id"])
    assert _rows(srv)[eid]["related_item_id"] == item["id"]
    srv.update_changelog(eid, project="test", entry="y")  # 0 = unchanged
    assert _rows(srv)[eid]["related_item_id"] == item["id"]
    srv.update_changelog(eid, project="test", related_item_id=-1)
    assert _rows(srv)[eid]["related_item_id"] is None


def test_update_changelog_nothing_given_errors(srv):
    eid = srv.add_changelog("added", "x", project="test")["id"]
    assert "Nothing to update" in srv.update_changelog(eid, project="test")["error"]


def test_update_changelog_markup_rejected(srv):
    eid = srv.add_changelog("added", "x", project="test")["id"]
    out = srv.update_changelog(eid, project="test", worktree="<invoke name=x>")
    assert "field 'worktree'" in out["error"]
    assert _rows(srv)[eid]["worktree"] is None


def test_update_and_delete_changelog_refuse_released_entries(srv):
    eid = srv.add_changelog("added", "x", project="test")["id"]
    srv.release_changelog(project="test", version="1.0.0")
    assert "already released" in srv.update_changelog(eid, project="test", entry="y")["error"]
    assert "already released" in srv.delete_changelog(eid, project="test")["error"]


def test_update_and_delete_changelog_are_project_scoped(srv):
    create_project(srv._get_engine(), slug="proj-b", name="B")
    theirs = srv.add_changelog("added", "theirs", project="proj-b")["id"]
    assert "not found in this project" in srv.update_changelog(
        theirs, project="test", entry="mine")["error"]
    assert "not found in this project" in srv.delete_changelog(theirs, project="test")["error"]
