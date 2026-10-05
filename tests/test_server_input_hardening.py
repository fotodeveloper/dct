"""MCP input hardening of the fix round R1b (K-36, K-37, K-40, K-43, K-47,
K-48): what the server refuses before core, what its acks echo, and that its
docstrings state the contracts the code enforces."""

import shutil

import pytest
from conftest import make_git_repo

from dct.core.changelog import list_entries
from dct.core.items import create_item as core_create_item
from dct.core.items import delete_item as core_delete_item
from dct.core.plans.crud import add_plan_checkpoint, get_plan_section
from dct.core.plans.crud import add_plan_section as core_add_section
from dct.core.plans.crud import create_plan as core_create_plan
from dct.core.projects import create_project


def _entries(srv, slug="test"):
    return {r["id"]: r for r in list_entries(srv._get_engine(), slug)}


# ── K-47: update_changelog sentinels ──────────────────────────────────────────


def test_update_changelog_refuses_clearing_the_entry_text(srv):
    eid = srv.add_changelog("added", "keep me", project="test")["id"]
    out = srv.update_changelog(eid, project="test", entry="__clear__")
    assert set(out) == {"error"} and "entry cannot be cleared" in out["error"]
    assert _entries(srv)[eid]["entry"] == "keep me"


@pytest.mark.parametrize("bad", [-2, -99])
def test_update_changelog_refuses_related_item_below_minus_one(srv, bad):
    item = core_create_item(srv._get_engine(), "test", "todo", "T")["id"]
    eid = srv.add_changelog("added", "x", project="test", related_item_id=item)["id"]
    out = srv.update_changelog(eid, project="test", related_item_id=bad)
    assert set(out) == {"error"} and "Invalid related_item_id" in out["error"]
    assert _entries(srv)[eid]["related_item_id"] == item


def test_update_changelog_sentinels_zero_keeps_minus_one_unlinks(srv):
    item = core_create_item(srv._get_engine(), "test", "todo", "T")["id"]
    eid = srv.add_changelog("added", "x", project="test", related_item_id=item)["id"]
    srv.update_changelog(eid, project="test", entry="y", related_item_id=0)
    assert _entries(srv)[eid]["related_item_id"] == item
    srv.update_changelog(eid, project="test", related_item_id=-1)
    assert _entries(srv)[eid]["related_item_id"] is None


# ── K-37: related item validation over MCP ────────────────────────────────────


def test_add_and_update_changelog_refuse_foreign_and_deleted_items(srv):
    eng = srv._get_engine()
    create_project(eng, slug="proj-b", name="B")
    foreign = core_create_item(eng, "proj-b", "todo", "Theirs")["id"]
    gone = core_create_item(eng, "test", "todo", "Gone")["id"]
    core_delete_item(eng, gone)
    for bad in (foreign, gone, 987654):
        out = srv.add_changelog("added", "x", project="test", related_item_id=bad)
        assert set(out) == {"error"} and "Unknown item id" in out["error"]
    assert _entries(srv) == {}
    eid = srv.add_changelog("added", "x", project="test")["id"]
    out = srv.update_changelog(eid, project="test", related_item_id=foreign)
    assert set(out) == {"error"} and "Unknown item id" in out["error"]
    assert _entries(srv)[eid]["related_item_id"] is None


# ── K-36: the server-detected warning ─────────────────────────────────────────


def test_add_changelog_client_values_carry_no_warning(srv):
    out = srv.add_changelog("added", "x", project="test", branch="feat/x",
                            worktree="/tmp/test")
    assert out["branch_source"] == "client" and "warning" not in out


def test_add_changelog_without_context_carries_no_warning(srv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not a checkout of the project
    out = srv.add_changelog("added", "x", project="test")
    assert out["branch_source"] is None and "warning" not in out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_add_changelog_server_detected_ack_warns_and_names_the_fix(srv, tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo", branch="main")
    create_project(srv._get_engine(), slug="proj-g", name="G", path=root)
    monkeypatch.chdir(root)
    out = srv.add_changelog("added", "x", project="proj-g")
    assert out["branch_source"] == "server"
    assert "MCP server's own checkout" in out["warning"]
    assert f"update_changelog({out['id']}" in out["warning"]


# ── K-42: relative worktrees refused on every MCP path ───────────────────────


def test_relative_worktree_refused_by_add_update_list_and_count(srv):
    eid = srv.add_changelog("added", "x", project="test")["id"]
    for out in (
        srv.add_changelog("added", "y", project="test", worktree="wt-x"),
        srv.update_changelog(eid, project="test", worktree="wt-x"),
        srv.list_changelog(project="test", worktree="wt-x"),
        srv.count_unreleased_changelog(project="test", worktree="wt-x"),
    ):
        assert set(out) == {"error"} and "absolute path" in out["error"]
    assert list(_entries(srv)) == [eid]


# ── K-40 / K-43: update_item label semantics ─────────────────────────────────


def test_update_item_status_only_keeps_the_label(srv):
    iid = srv.create_item("todo", "T", project="test", short_title="Chosen label")["id"]
    out = srv.update_item(iid, status="in_progress")
    assert out["short_title"] == "Chosen label"
    assert srv.get_item(iid)["short_title"] == "Chosen label"


def test_update_item_whitespace_only_label_is_blank_not_a_clear(srv):
    iid = srv.create_item("todo", "T", project="test", short_title="Chosen label")["id"]
    out = srv.update_item(iid, short_title="   ", priority="P1")
    assert out["short_title"] == "Chosen label" and out["priority"] == "P1"


def test_update_item_whitespace_only_label_over_the_limit_is_refused(srv):
    iid = srv.create_item("todo", "T", project="test", short_title="Keep")["id"]
    out = srv.update_item(iid, short_title=" " * 51, priority="P1")
    assert set(out) == {"error"} and "short_title too long" in out["error"]
    assert srv.get_item(iid)["priority"] == "P2"


def test_update_item_label_is_stripped_in_the_ack(srv):
    iid = srv.create_item("todo", "T", project="test")["id"]
    assert srv.update_item(iid, short_title="  Padded  ")["short_title"] == "Padded"


def test_update_item_docstring_does_not_demand_the_label():
    """K-40: 'always pass it' belongs to create_item and the promote tools."""
    from dct.mcp import server

    update_doc = " ".join(server.update_item.__doc__.split())
    assert "always pass it" not in update_doc
    assert "Omit (or blank) = keep the current label" in update_doc
    for creator in (server.create_item, server.promote_section_to_item,
                    server.promote_checkpoint_to_item):
        assert "always pass it" in creator.__doc__


# ── K-47: promote tools ───────────────────────────────────────────────────────


@pytest.fixture
def plan_sources(srv):
    eng = srv._get_engine()
    plan = core_create_plan(eng, "test", slug="p-harden", title="P", kind="plan")
    section = core_add_section(eng, plan_id=plan["id"], heading="Section", depth=2)
    cp = add_plan_checkpoint(eng, section["id"], "checkpoint text")
    return eng, section["id"], cp["id"]


def _item_count(srv):
    return len(srv.list_items(project="test", status="", format="dict"))


def _cp_backlink(eng, cp_id):
    import sqlalchemy as sa

    from dct.db import plan_checkpoints

    with eng.connect() as conn:
        return conn.execute(sa.select(plan_checkpoints.c.converted_to_item_id).where(
            plan_checkpoints.c.id == cp_id)).scalar()


@pytest.mark.parametrize("field", ["title", "short_title"])
@pytest.mark.parametrize("tool", ["section", "checkpoint"])
def test_promote_markup_refused_no_item_no_backlink(srv, plan_sources, field, tool):
    eng, section_id, cp_id = plan_sources
    before = _item_count(srv)
    kwargs = {field: "</parameter> leak"}
    if tool == "section":
        out = srv.promote_section_to_item(section_id, "todo", **kwargs)
    else:
        out = srv.promote_checkpoint_to_item(cp_id, "todo", **kwargs)
    assert set(out) == {"error"} and f"field '{field}'" in out["error"]
    assert _item_count(srv) == before
    assert get_plan_section(eng, section_id)["converted_to_item_id"] is None
    assert _cp_backlink(eng, cp_id) is None


def test_promote_checkpoint_over_long_label_refused_no_item_no_backlink(srv, plan_sources):
    eng, _, cp_id = plan_sources
    before = _item_count(srv)
    out = srv.promote_checkpoint_to_item(cp_id, "todo", short_title="x" * 51)
    assert set(out) == {"error"}
    assert _item_count(srv) == before
    assert _cp_backlink(eng, cp_id) is None


def test_promote_acks_echo_the_stored_normalized_label(srv, plan_sources):
    _, section_id, cp_id = plan_sources
    assert srv.promote_section_to_item(section_id, "todo", short_title="  Sec  ")[
        "short_title"] == "Sec"
    assert srv.promote_checkpoint_to_item(cp_id, "todo", short_title="   ")[
        "short_title"] is None


# ── K-48: one clear sentinel, one preview ─────────────────────────────────────


def test_server_uses_the_one_clear_sentinel_and_preview():
    from dct.core import text
    from dct.mcp import server

    assert server.CLEAR is text.CLEAR == "__clear__"
    assert server.preview is text.preview
    assert not hasattr(text, "_preview")
    assert not hasattr(server, "_CLEAR")
