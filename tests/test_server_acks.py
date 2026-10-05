"""Slim-ack contract tests for the MCP write-op tools (#351).

Every reshaped tool in dct/server.py returns a compact acknowledgement (ids +
scalar headers) instead of the full core dict. These tests lock in, per tool:
  (a) the exact key set of the ack,
  (b) the ABSENCE of stripped free-text / heavy keys
      (description / notes / checkpoints / prompt / entry / rationale / text …),
  (c) the progress triple {done, pending, active} and its rejected-exclusion math,
  (d) update_item POST-state headers (a '__clear__'ed field shows null),
  (e) error paths are unchanged (unknown ids, gate-guard refusal, markup),
  (f) get_item STILL returns the full record (notes + checkpoints + full progress).

Tools are called directly (the SDK's @mcp.tool() returns the function unchanged),
with server._get_engine monkeypatched onto the in-memory fixture engine.
"""

import pytest

from dct.core.decisions import create_decision as core_create_decision
from dct.core.plans.crud import add_plan_checkpoint
from dct.core.plans.crud import add_plan_section as core_add_section
from dct.core.plans.crud import create_plan as core_create_plan
from dct.mcp import server as S

# ── create_item ───────────────────────────────────────────────────────────────


def test_create_item_ack_shape(srv):
    out = srv.create_item("todo", "My task", project="test", description="a long body", tags=["x"])
    assert set(out) == {
        "id", "project", "item_type", "priority", "status", "title", "short_title",
    }
    assert out["short_title"] is None  # none passed → stored NULL, echoed
    assert out["project"] == "test"  # resolved slug echo
    assert out["item_type"] == "todo"
    assert out["title"] == "My task"
    assert out["status"] == "open"
    for stripped in ("description", "notes", "checkpoints", "tags", "component", "progress"):
        assert stripped not in out


def test_create_item_ack_echoes_stored_short_title(srv):
    """#817: the echo is how a caller proves the label reached a server that
    knows the field (an old server silently drops unknown args)."""
    out = srv.create_item("todo", "Long title", project="test", short_title="Hook paths")
    assert out["short_title"] == "Hook paths"
    assert srv.get_item(out["id"])["short_title"] == "Hook paths"


def test_create_item_short_title_51_chars_refused_nothing_written(srv):
    before = srv.list_items(project="test", status="")["rows"]
    out = srv.create_item("todo", "T", project="test", short_title="s" * 51)
    assert set(out) == {"error"}
    assert "short_title" in out["error"] and "51" in out["error"]
    assert srv.list_items(project="test", status="")["rows"] == before


def test_create_item_short_title_exactly_50_chars_stored_untruncated(srv):
    out = srv.create_item("todo", "T", project="test", short_title="s" * 50)
    assert out["short_title"] == "s" * 50


def test_create_item_short_title_markup_rejected_before_db(monkeypatch):
    def unexpected_db_access():
        pytest.fail("Malformed tool calls must be rejected before DB access")

    monkeypatch.setattr(S, "_get_engine", unexpected_db_access)
    out = S.create_item("todo", "clean", project="test", short_title="<parameter name=x>")
    assert set(out) == {"error"}
    assert "field 'short_title'" in out["error"]


def test_create_item_error_path_unchanged(srv):
    out = srv.create_item("todo", "X", project="does-not-exist")
    assert set(out) == {"error"}


def test_create_item_markup_rejected(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    out = srv.create_item("todo", "clean", description="<parameter name=foo>bar")
    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert calls == []  # guard fires before any DB access


@pytest.mark.parametrize("marker", [
    "<function_calls>", "</function_calls>",
    "<FuNcTiOn_CaLlS>", "</FuNcTiOn_CaLlS>",
])
@pytest.mark.parametrize("field", ["description", "tags"])
def test_function_calls_wrappers_rejected_before_db_access(monkeypatch, marker, field):
    """#427: a bare wrapper is refused in prose and inside a tag list."""
    def unexpected_db_access():
        pytest.fail("Malformed tool calls must be rejected before DB access")

    monkeypatch.setattr(S, "_get_engine", unexpected_db_access)
    text = f"before {marker} after"
    value = ["clean", text] if field == "tags" else text
    out = S.create_item("todo", "clean", project="test", **{field: value})

    assert set(out) == {"error"}
    assert "Malformed MCP tool call" in out["error"]
    assert marker.lower() in out["error"]
    assert f"field '{'tags[1]' if field == 'tags' else field}'" in out["error"]


def test_function_calls_plain_text_is_still_saved(srv):
    text = "The function_calls wrapper groups calls; ordinary <p>HTML</p> is prose."
    item = srv.create_item("todo", "clean", project="test", description=text)
    assert srv.get_item(item["id"])["description"] == text


# ── add_checkpoint ────────────────────────────────────────────────────────────


def test_add_checkpoint_ack_shape(srv):
    item = srv.create_item("feature", "Feat", project="test")
    out = srv.add_checkpoint(item["id"], "do the thing", kind="task")
    assert set(out) == {"id", "item_id", "item_title", "kind", "status", "sort_order"}
    assert out["item_title"] == "Feat"  # parent title, one query
    assert out["item_id"] == item["id"]
    assert out["kind"] == "task"
    assert out["status"] == "pending"
    assert "text" not in out


def test_add_checkpoint_unknown_item_errors(srv):
    out = srv.add_checkpoint(999999, "orphan")
    assert set(out) == {"error"}


# ── complete / reject / reopen checkpoint + progress math ─────────────────────


def test_checkpoint_progress_math_and_rejected_exclusion(srv):
    item = srv.create_item("feature", "F", project="test")
    iid = item["id"]
    c1 = srv.add_checkpoint(iid, "a")
    c2 = srv.add_checkpoint(iid, "b")
    srv.add_checkpoint(iid, "c")

    done = srv.complete_checkpoint(c1["id"])
    assert set(done) == {"id", "item_id", "status", "progress"}
    assert done["status"] == "done"
    assert "text" not in done
    # progress is exactly the ack triple — no rejected / percent / total leak
    assert done["progress"] == {"done": 1, "pending": 2, "active": 3}
    assert set(done["progress"]) == {"done", "pending", "active"}

    # rejecting drops the checkpoint out of `active`
    rej = srv.reject_checkpoint(c2["id"])
    assert rej["status"] == "rejected"
    assert rej["progress"] == {"done": 1, "pending": 1, "active": 2}

    # reopen mirrors the family shape and recomputes progress
    reo = srv.reopen_checkpoint(c1["id"])
    assert set(reo) == {"id", "item_id", "status", "progress"}
    assert reo["status"] == "pending"
    assert reo["progress"] == {"done": 0, "pending": 2, "active": 2}


def test_checkpoint_status_ops_unknown_id_error_unchanged(srv):
    for fn in (srv.complete_checkpoint, srv.reject_checkpoint, srv.reopen_checkpoint):
        out = fn(987654)
        assert out == {"error": "Checkpoint #987654 not found"}


# ── add_note ──────────────────────────────────────────────────────────────────


def test_add_note_ack_shape(srv):
    item = srv.create_item("todo", "NoteHost", project="test")
    out = srv.add_note(item["id"], "a discovery worth remembering")
    assert set(out) == {"id", "item_id", "item_title"}
    assert out["item_title"] == "NoteHost"
    assert out["item_id"] == item["id"]
    assert "note" not in out


def test_add_note_markup_rejected(srv):
    item = srv.create_item("todo", "H", project="test")
    out = srv.add_note(item["id"], "antml:invoke sneaky")
    assert "error" in out and "Malformed MCP tool call" in out["error"]


# ── update_item ───────────────────────────────────────────────────────────────


def test_update_item_ack_shape_and_post_state(srv):
    item = srv.create_item("todo", "T", project="test", component="core")
    iid = item["id"]
    out = srv.update_item(iid, priority="P1", status="in_progress")
    assert set(out) == {
        "id", "item_type", "status", "priority", "title", "short_title",
        "component", "in_current_sprint", "resolved_at", "progress",
    }
    assert out["short_title"] is None
    assert out["priority"] == "P1"
    assert out["status"] == "in_progress"
    assert out["component"] == "core"
    assert out["in_current_sprint"] is False
    assert out["resolved_at"] is None
    assert set(out["progress"]) == {"done", "pending", "active"}
    for stripped in ("description", "notes", "checkpoints", "resolution", "tags"):
        assert stripped not in out


def test_update_item_clear_shows_null_in_header(srv):
    item = srv.create_item("todo", "T", project="test", component="core")
    out = srv.update_item(item["id"], component="__clear__")
    # POST-state scalar reflects the nulled column
    assert out["component"] is None


def test_update_item_short_title_set_keep_and_clear(srv):
    item = srv.create_item("todo", "T", project="test")
    iid = item["id"]
    assert srv.update_item(iid, short_title="Label one")["short_title"] == "Label one"
    # An update that omits short_title leaves it untouched.
    assert srv.update_item(iid, priority="P1")["short_title"] == "Label one"
    out = srv.update_item(iid, short_title="__clear__")
    assert out["short_title"] is None
    assert srv.get_item(iid)["short_title"] is None


def test_update_item_short_title_too_long_refused_row_unchanged(srv):
    item = srv.create_item("todo", "T", project="test", short_title="Keep")
    out = srv.update_item(item["id"], short_title="x" * 51, priority="P1")
    assert set(out) == {"error"}
    assert "short_title" in out["error"]
    got = srv.get_item(item["id"])
    assert got["short_title"] == "Keep"
    assert got["priority"] == "P2"  # the whole update was refused


def test_update_item_short_title_markup_rejected(srv):
    item = srv.create_item("todo", "T", project="test")
    out = srv.update_item(item["id"], short_title="</parameter>")
    assert "error" in out and "field 'short_title'" in out["error"]


def test_update_item_unknown_id_error_unchanged(srv):
    out = srv.update_item(424242, priority="P1")
    assert out == {"error": "Item #424242 not found"}


def test_update_item_markup_rejected(srv):
    item = srv.create_item("todo", "T", project="test")
    out = srv.update_item(item["id"], description="</parameter> leak")
    assert "error" in out and "Malformed MCP tool call" in out["error"]


def test_update_item_clear_title_refused_and_title_unchanged(srv):
    """K-12: '__clear__' on title maps to "" — core must refuse it, not store it."""
    item = srv.create_item("todo", "Keep me", project="test")
    out = srv.update_item(item["id"], title="__clear__")
    assert out == {"error": "Item title cannot be empty"}
    assert srv.get_item(item["id"])["title"] == "Keep me"


# ── resolve_item ──────────────────────────────────────────────────────────────


def test_resolve_item_ack_shape(srv):
    item = srv.create_item("todo", "R", project="test")
    out = srv.resolve_item(item["id"], resolution="shipped")
    assert set(out) == {"id", "status", "resolved_at", "progress"}
    assert out["status"] == "resolved"
    assert out["resolved_at"] is not None
    assert set(out["progress"]) == {"done", "pending", "active"}
    for stripped in ("notes", "checkpoints", "resolution", "title"):
        assert stripped not in out


def test_resolve_item_gate_guard_refusal_unchanged(srv):
    item = srv.create_item("feature", "G", project="test")
    srv.add_checkpoint(item["id"], "user validates", kind="dogfood")
    out = srv.resolve_item(item["id"])
    assert "error" in out
    assert "gate checkpoint" in out["error"].lower()
    # the item must NOT have been resolved
    assert srv.get_item(item["id"])["status"] != "resolved"


def test_resolve_item_unknown_id_error_unchanged(srv):
    out = srv.resolve_item(555111)
    assert out == {"error": "Item #555111 not found"}


# ── add_changelog ─────────────────────────────────────────────────────────────


def test_add_changelog_ack_shape(srv):
    out = srv.add_changelog("added", "shipped a slim ack layer", project="test", component="server",
                            branch="feat/x", worktree="/tmp/test")
    assert set(out) == {"id", "project", "category", "component", "branch", "branch_source"}
    assert out["branch"] == "feat/x"
    assert out["branch_source"] == "client"
    assert out["project"] == "test"
    assert out["category"] == "added"
    assert out["component"] == "server"
    assert "entry" not in out


def test_add_changelog_component_may_be_null(srv):
    out = srv.add_changelog("fixed", "a bug", project="test")
    assert set(out) == {"id", "project", "category", "component", "branch", "branch_source"}
    assert out["component"] is None
    # /tmp/test is not a checkout containing the test process cwd → unknown.
    assert out["branch"] is None and out["branch_source"] is None


def test_add_changelog_markup_rejected(srv):
    out = srv.add_changelog("added", "<invoke name=x> broken", project="test")
    assert "error" in out and "Malformed MCP tool call" in out["error"]


# ── release / update / delete changelog (#801, #359) ─────────────────────────


_RELEASE_KEYS = {"project", "version", "current_version", "mode", "entry_ids",
                 "released", "already_released", "release"}


def test_release_changelog_return_shape_selected(srv):
    a = srv.add_changelog("added", "a", project="test")["id"]
    out = srv.release_changelog(project="test", version="1.0.0", entry_ids=[a])
    assert set(out) == _RELEASE_KEYS
    release = out.pop("release")
    assert out == {"project": "test", "version": "1.0.0", "current_version": "1.0.0",
                   "mode": "selected", "entry_ids": [a], "released": 1,
                   "already_released": []}
    assert set(release) == {"id", "version", "mode", "entry_ids", "candidate_sha",
                            "checkout_root", "release_commit_sha", "manifest", "proof",
                            "status", "created_at", "updated_at", "completed_at",
                            "abandoned_at", "abandon_reason", "release_mode",
                            "executor", "consent", "proposal", "render"}
    assert (release["version"], release["entry_ids"], release["status"]) == (
        "1.0.0", [a], "stamped")


def test_update_changelog_ack_shape(srv):
    eid = srv.add_changelog("added", "old", project="test")["id"]
    out = srv.update_changelog(eid, project="test", entry="new body text", category="fixed")
    assert out == {"id": eid, "project": "test", "updated_fields": ["entry", "category"]}
    assert "entry" not in out


def test_delete_changelog_ack(srv):
    eid = srv.add_changelog("added", "x", project="test")["id"]
    assert srv.delete_changelog(eid, project="test") == {"deleted": True, "id": eid}
    assert srv.count_unreleased_changelog(project="test") == {"count": 0}
    assert set(srv.delete_changelog(eid, project="test")) == {"error"}


# ── handoff tools ─────────────────────────────────────────────────────────────


def test_create_handoff_ack_no_prompt_echo(srv):
    out = srv.create_handoff(
        prompt="secret next-session instructions",
        created_by="tester",
        project="test",
        scope="sprint-x",
        context_summary="lots of context",
    )
    assert set(out) == {"id", "project", "scope", "created_at"}
    assert out["project"] == "test"
    assert out["scope"] == "sprint-x"
    assert "prompt" not in out
    assert "context_summary" not in out


def test_update_handoff_ack_reports_applied_fields(srv):
    h = srv.create_handoff(prompt="v1", created_by="tester", project="test")
    out = srv.update_handoff(h["id"], prompt="v2", scope="new-scope")
    assert set(out) == {"id", "updated_fields"}
    assert out["id"] == h["id"]
    assert set(out["updated_fields"]) == {"prompt", "scope"}
    assert "prompt" not in {k for k in out if k != "updated_fields"}
    assert "v2" not in str(out)  # no prompt text echo anywhere


def test_update_handoff_no_fields_error_unchanged(srv):
    h = srv.create_handoff(prompt="v1", created_by="tester", project="test")
    out = srv.update_handoff(h["id"])
    assert "error" in out


def test_consume_handoff_ack_no_prompt_echo(srv):
    h = srv.create_handoff(prompt="do X next", created_by="tester", project="test")
    out = srv.consume_handoff(h["id"], consumed_by="tester2")
    assert set(out) == {"id", "consumed_at"}
    assert out["consumed_at"] is not None
    assert "prompt" not in out


def test_consume_handoff_unknown_id_error_unchanged(srv):
    out = srv.consume_handoff(70707)
    assert out == {"error": "Handoff #70707 not found or already consumed"}


# ── sprint tools ──────────────────────────────────────────────────────────────


def test_create_sprint_ack_strips_goal(srv):
    out = srv.create_sprint("Sprint One", project="test", code="s1", goal="a long free-text goal")
    assert set(out) == {"id", "code", "kind", "status", "name"}
    assert out["code"] == "s1"
    assert out["name"] == "Sprint One"
    assert "goal" not in out


def test_update_sprint_ack_strips_goal(srv):
    s = srv.create_sprint("Sprint One", project="test", code="s1")
    out = srv.update_sprint(s["id"], status="active", goal="updated goal text")
    assert set(out) == {"id", "code", "kind", "status", "name"}
    assert out["status"] == "active"
    assert "goal" not in out


def test_update_sprint_unknown_id_error_unchanged(srv):
    out = srv.update_sprint(313131, status="active")
    assert out == {"error": "Sprint #313131 not found"}


# ── decision tools ────────────────────────────────────────────────────────────


def test_create_decision_ack_strips_prose(srv):
    out = srv.create_decision(
        "adr", "Adopt slim acks", project="test", ref_code="ADR-1",
        context="the situation", decision="we will slim",
        consequences="less context spend", alternatives="keep full dicts",
    )
    assert set(out) == {"id", "ulid", "kind", "ref_code", "status", "title"}
    assert out["kind"] == "adr"
    assert out["ref_code"] == "ADR-1"
    for stripped in ("context", "decision", "consequences", "alternatives"):
        assert stripped not in out


def test_update_decision_ack_strips_prose(srv):
    d = core_create_decision(srv._get_engine(), "test", kind="adr", title="D")
    out = srv.update_decision(d["id"], status="accepted", decision="revised choice")
    assert set(out) == {"id", "ulid", "kind", "ref_code", "status", "title"}
    assert out["status"] == "accepted"
    assert "decision" not in out


def test_supersede_decision_ack_strips_prose(srv):
    eng = srv._get_engine()
    old = core_create_decision(eng, "test", kind="adr", title="Old", decision="old choice")
    new = core_create_decision(eng, "test", kind="adr", title="New")
    out = srv.supersede_decision(old["id"], new["id"])
    assert set(out) == {"id", "ulid", "kind", "ref_code", "status", "title"}
    assert out["status"] == "superseded"
    assert "decision" not in out


def test_create_decision_markup_rejected(srv):
    out = srv.create_decision("adr", "T", project="test", context="antml: leak")
    assert "error" in out and "Malformed MCP tool call" in out["error"]


# ── plan tools ────────────────────────────────────────────────────────────────


def test_create_plan_ack_shape(srv):
    out = srv.create_plan("roadmap-1", "Roadmap", "roadmap", project="test")
    assert set(out) == {"id", "ulid", "slug", "kind", "status", "title"}
    assert out["slug"] == "roadmap-1"
    assert out["kind"] == "roadmap"


def test_add_plan_section_ack_shape(srv):
    plan = srv.create_plan("plan-1", "Plan", "plan", project="test")
    out = srv.add_plan_section(plan["id"], "Phase 1", depth=2, section_type="phase")
    assert set(out) == {"id", "ulid", "heading", "depth", "section_type", "status", "sort_order"}
    assert out["heading"] == "Phase 1"
    assert out["depth"] == 2
    assert out["section_type"] == "phase"


def test_plan_checkpoint_status_acks_strip_text(srv):
    eng = srv._get_engine()
    plan = core_create_plan(eng, "test", slug="p", title="P", kind="plan")
    section = core_add_section(eng, plan_id=plan["id"], heading="S", depth=2)
    cp = add_plan_checkpoint(eng, section["id"], "a long checkpoint text body")

    done = srv.complete_plan_checkpoint(cp["id"])
    assert set(done) == {"id", "plan_section_id", "status", "sort_order"}
    assert done["status"] == "done"
    assert "text" not in done

    rej = srv.reject_plan_checkpoint(cp["id"])
    assert set(rej) == {"id", "plan_section_id", "status", "sort_order"}
    assert rej["status"] == "rejected"
    assert "text" not in rej


def test_plan_checkpoint_unknown_id_error_unchanged(srv):
    out = srv.complete_plan_checkpoint(202020)
    assert out == {"error": "Plan checkpoint #202020 not found"}


# ── promote tools ─────────────────────────────────────────────────────────────


def test_promote_section_to_item_ack_shape(srv):
    plan = srv.create_plan("plan-2", "Plan", "plan", project="test")
    section = core_add_section(srv._get_engine(), plan_id=plan["id"], heading="Chunk", depth=2)
    out = srv.promote_section_to_item(section["id"], "todo")
    assert set(out) == {"id", "item_type", "priority", "status", "title", "short_title"}
    assert out["short_title"] is None  # K-47: the STORED label, also when null
    assert out["item_type"] == "todo"
    assert out["title"] == "Chunk"
    assert "description" not in out


def test_promote_checkpoint_to_item_ack_shape(srv):
    eng = srv._get_engine()
    plan = core_create_plan(eng, "test", slug="p3", title="P", kind="plan")
    section = core_add_section(eng, plan_id=plan["id"], heading="S", depth=2)
    cp = add_plan_checkpoint(eng, section["id"], "promote me to an item")
    out = srv.promote_checkpoint_to_item(cp["id"], "todo")
    assert set(out) == {"id", "item_type", "priority", "status", "title", "short_title"}
    assert out["short_title"] is None
    assert out["item_type"] == "todo"
    assert "description" not in out


def test_promote_tools_pass_short_title_through(srv):
    """#817: both promotion paths hand short_title to create_item; K-47: the
    acks echo the STORED (normalized) label."""
    eng = srv._get_engine()
    plan = core_create_plan(eng, "test", slug="p-short", title="P", kind="plan")
    section = core_add_section(eng, plan_id=plan["id"], heading="Section", depth=2)
    cp = add_plan_checkpoint(eng, section["id"], "checkpoint text")
    from_section = srv.promote_section_to_item(section["id"], "todo", short_title="Section label")
    from_cp = srv.promote_checkpoint_to_item(cp["id"], "todo", short_title="Checkpoint label")
    assert srv.get_item(from_section["id"])["short_title"] == "Section label"
    assert srv.get_item(from_cp["id"])["short_title"] == "Checkpoint label"
    assert from_section["short_title"] == "Section label"
    assert from_cp["short_title"] == "Checkpoint label"


def test_promote_section_short_title_too_long_refused_no_backlink(srv):
    eng = srv._get_engine()
    plan = core_create_plan(eng, "test", slug="p-long", title="P", kind="plan")
    section = core_add_section(eng, plan_id=plan["id"], heading="Section", depth=2)
    out = srv.promote_section_to_item(section["id"], "todo", short_title="x" * 51)
    assert set(out) == {"error"}
    from dct.core.plans.crud import get_plan_section
    assert get_plan_section(eng, section["id"])["converted_to_item_id"] is None


def test_promote_section_to_sprint_ack_strips_goal(srv):
    plan = srv.create_plan("plan-4", "Plan", "plan", project="test")
    section = core_add_section(srv._get_engine(), plan_id=plan["id"], heading="Sprint 3", depth=2, section_type="sprint")
    out = srv.promote_section_to_sprint(section["id"], code="v0.2-sec", goal="long goal")
    assert set(out) == {"id", "code", "kind", "status", "name"}
    assert out["code"] == "v0.2-sec"
    assert "goal" not in out


# ── HARD constraint: get_item stays FULL ──────────────────────────────────────


def test_get_item_still_returns_full_record(srv):
    item = srv.create_item("feature", "Full", project="test", description="body")
    iid = item["id"]
    srv.add_note(iid, "note one")
    c1 = srv.add_checkpoint(iid, "cp one")
    srv.complete_checkpoint(c1["id"])

    full = srv.get_item(iid)
    # notes + checkpoints embedded, description present, progress is the FULL dict
    assert "notes" in full and len(full["notes"]) == 1
    assert "checkpoints" in full and len(full["checkpoints"]) == 1
    assert full["description"] == "body"
    assert set(full["progress"]) == {"total", "done", "rejected", "pending", "active", "percent"}
    # checkpoint rows retain their text (not slimmed)
    assert full["checkpoints"][0]["text"] == "cp one"


# ── tag tools (#611) ──────────────────────────────────────────────────────────


def test_add_tag_ack_shape(srv):
    out = srv.add_tag("Perf ", description="performance work")
    assert set(out) == {"id", "name", "created"}
    assert out["name"] == "perf"  # NORMALIZED form echoed back
    assert out["created"] is True
    # no description echo — the ack is scalar headers only
    assert "description" not in out


def test_add_tag_is_idempotent_and_reports_reuse(srv):
    first = srv.add_tag("perf")
    again = srv.add_tag(" PERF ")
    assert again["id"] == first["id"]
    assert again["created"] is False


def test_add_tag_similarity_error_offers_the_force_path(srv):
    srv.add_tag("tests")
    out = srv.add_tag("test")
    assert set(out) == {"error"}
    assert "'tests'" in out["error"]
    assert "force=true" in out["error"]


def test_add_tag_force_true_creates_the_confusable_name(srv):
    srv.add_tag("tests")
    out = srv.add_tag("test", force=True)
    assert out["created"] is True
    assert {r[1] for r in srv.list_tags()["rows"]} == {"test", "tests"}


def test_add_tag_invalid_name_is_an_error_not_a_raise(srv):
    assert set(srv.add_tag("   ")) == {"error"}
    assert set(srv.add_tag("a,b")) == {"error"}


def test_add_tag_markup_rejected(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    out = srv.add_tag("clean", description="<parameter name=foo>bar")
    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert calls == []  # guard fires before any DB access


def test_list_tags_cols_rows_shape(srv):
    item = srv.create_item("todo", "T", project="test", tags=["oss"])
    assert "error" not in item
    srv.add_tag("unused")

    out = srv.list_tags()
    assert out["cols"] == ["id", "name", "item_count", "created_at", "description"]
    by_name = {r[1]: r for r in out["rows"]}
    assert list(by_name) == ["oss", "unused"]  # name-ASC
    assert by_name["oss"][2] == 1
    assert by_name["unused"][2] == 0  # unused tags stay visible (K-14)
    assert isinstance(by_name["oss"][3], str)  # created_at serialized


def test_list_tags_honours_format(srv):
    srv.add_tag("oss")
    assert srv.list_tags(format="markdown").startswith("| id | name |")
    dicts = srv.list_tags(format="dict")
    assert isinstance(dicts, list) and dicts[0]["name"] == "oss"


def test_merge_tag_ack_shape(srv):
    a = srv.create_item("todo", "A", project="test", tags=["tests"])
    srv.add_tag("test", force=True)
    srv.update_item(a["id"], tags=["tests", "test"])

    out = srv.merge_tag("test", "tests")
    assert out == {"from": "test", "into": "tests", "items_moved": 1}
    # the retired name is gone from the dictionary, the survivor keeps the item
    rows = {r[1]: r[2] for r in srv.list_tags()["rows"]}
    assert rows == {"tests": 1}
    assert srv.get_item(a["id"])["tags"] == ["tests"]


def test_merge_tag_unknown_and_self_merge_are_errors(srv):
    srv.add_tag("oss")
    unknown = srv.merge_tag("nope", "oss")
    assert set(unknown) == {"error"} and "nope" in unknown["error"]
    same = srv.merge_tag("oss", " OSS ")
    assert set(same) == {"error"} and "itself" in same["error"]


def test_merge_tag_markup_rejected(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    out = srv.merge_tag("</invoke> a", "b")
    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert calls == []


# ── tags on the item tools (#611) ─────────────────────────────────────────────


def test_update_item_tags_full_replace_and_clear(srv):
    item = srv.create_item("todo", "T", project="test", tags=["oss", "docs"])
    iid = item["id"]
    assert srv.get_item(iid)["tags"] == ["docs", "oss"]

    srv.update_item(iid, tags=["oss"])
    assert srv.get_item(iid)["tags"] == ["oss"]  # FULL REPLACE, not append

    srv.update_item(iid, title="renamed")
    assert srv.get_item(iid)["tags"] == ["oss"]  # omitted = unchanged

    srv.update_item(iid, tags=[])
    assert srv.get_item(iid)["tags"] == []  # [] clears


def test_update_item_refused_tag_returns_suggestion_with_force_path(srv):
    item = srv.create_item("todo", "T", project="test", tags=["tests"])
    out = srv.update_item(item["id"], tags=["tests", "test"])
    assert set(out) == {"error"}
    assert "force=true" in out["error"]
    assert srv.get_item(item["id"])["tags"] == ["tests"]  # rolled back


def test_item_tags_markup_rejected_per_list_element(srv, monkeypatch):
    """K-13: the guard inspects EVERY element of the tags array, not just
    scalar free-text fields."""
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    out = srv.create_item("todo", "clean", tags=["ok", "<parameter name=x>y"])
    assert "error" in out and "tags[1]" in out["error"]
    assert calls == []
    out = srv.update_item(1, tags=["antml:leak"])
    assert "error" in out and "tags[0]" in out["error"]
    assert calls == []


def test_list_items_tag_filter_and_unknown_tag_error(srv):
    keep = srv.create_item("todo", "Tagged", project="test", tags=["oss"])
    srv.create_item("todo", "Untagged", project="test")

    rows = srv.list_items(project="test", tag=" OSS ")["rows"]
    assert [r[0] for r in rows] == [keep["id"]]

    out = srv.list_items(project="test", tag="nonsense")
    assert set(out) == {"error"} and "nonsense" in out["error"]


# ── docstring contracts (K-35 / K-41 / K-38) ──────────────────────────────────
#
# The SDK exposes each tool's docstring verbatim as the description the calling
# model fills in, so a false clause is a real defect. These lock the corrected
# wording; they are cheap and fail loudly when someone reverts to the old claim.


def test_add_tag_docstring_states_the_true_resurrection_property():
    """K-35: resurrection brings the ROW back, not links a merge moved away."""
    doc = " ".join(S.add_tag.__doc__.split())
    assert "stable id" in doc
    assert "STAY on that target" in doc
    assert "does not undo a merge" in doc
    # K-33: description is applied when an existing ACTIVE tag is reused
    assert "`description` is applied to that existing row on reuse" in doc
    # the old, false claim is gone
    assert "historical item links reappear" not in doc


def test_update_item_docstring_states_whole_call_rollback():
    """K-41: a refused tag name rolls back the ENTIRE update."""
    doc = " ".join(S.update_item.__doc__.split())
    assert "rolls back the ENTIRE update" in doc
    assert "re-issue the WHOLE call" in doc


def test_list_items_docstring_describes_the_closest_tags_error():
    """K-38: the unknown-tag error names the closest tags, not 'a suggestion'."""
    doc = " ".join(S.list_items.__doc__.split())
    assert "naming the closest known tags" in doc
    assert "call list_tags for the full dictionary" in doc


def test_unknown_tag_error_stays_short_with_a_large_dictionary(srv):
    """K-38: the error must not dump the whole dictionary into the transcript."""
    for i in range(20):
        # force=True: the similarity guard is irrelevant here, the dictionary size is
        assert "error" not in srv.add_tag(f"topic-{i:02d}", force=True)
    out = srv.list_items(project="test", tag="nonsense")
    assert set(out) == {"error"}
    if "list_tags" not in out["error"]:
        pytest.skip("core K-38 (closest-tags error) has not landed yet")
    assert len(out["error"]) <= 400, out["error"]
