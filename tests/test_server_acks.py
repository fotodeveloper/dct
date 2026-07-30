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

from dct.core.decisions import create_decision as core_create_decision
from dct.core.plans.crud import add_plan_checkpoint
from dct.core.plans.crud import add_plan_section as core_add_section
from dct.core.plans.crud import create_plan as core_create_plan
from dct.mcp import server as S

# ── create_item ───────────────────────────────────────────────────────────────


def test_create_item_ack_shape(srv):
    out = srv.create_item("todo", "My task", project="test", description="a long body", tags=["x"])
    assert set(out) == {"id", "project", "item_type", "priority", "status", "title"}
    assert out["project"] == "test"  # resolved slug echo
    assert out["item_type"] == "todo"
    assert out["title"] == "My task"
    assert out["status"] == "open"
    for stripped in ("description", "notes", "checkpoints", "tags", "component", "progress"):
        assert stripped not in out


def test_create_item_error_path_unchanged(srv):
    out = srv.create_item("todo", "X", project="does-not-exist")
    assert set(out) == {"error"}


def test_create_item_markup_rejected(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    out = srv.create_item("todo", "clean", description="<parameter name=foo>bar")
    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert calls == []  # guard fires before any DB access


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
        "id", "item_type", "status", "priority", "title",
        "component", "in_current_sprint", "resolved_at", "progress",
    }
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


def test_update_item_unknown_id_error_unchanged(srv):
    out = srv.update_item(424242, priority="P1")
    assert out == {"error": "Item #424242 not found"}


def test_update_item_markup_rejected(srv):
    item = srv.create_item("todo", "T", project="test")
    out = srv.update_item(item["id"], description="</parameter> leak")
    assert "error" in out and "Malformed MCP tool call" in out["error"]


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
    out = srv.add_changelog("added", "shipped a slim ack layer", project="test", component="server")
    assert set(out) == {"id", "project", "category", "component"}
    assert out["project"] == "test"
    assert out["category"] == "added"
    assert out["component"] == "server"
    assert "entry" not in out


def test_add_changelog_component_may_be_null(srv):
    out = srv.add_changelog("fixed", "a bug", project="test")
    assert set(out) == {"id", "project", "category", "component"}
    assert out["component"] is None


def test_add_changelog_markup_rejected(srv):
    out = srv.add_changelog("added", "<invoke name=x> broken", project="test")
    assert "error" in out and "Malformed MCP tool call" in out["error"]


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
    assert set(out) == {"id", "item_type", "priority", "status", "title"}
    assert out["item_type"] == "todo"
    assert out["title"] == "Chunk"
    assert "description" not in out


def test_promote_checkpoint_to_item_ack_shape(srv):
    eng = srv._get_engine()
    plan = core_create_plan(eng, "test", slug="p3", title="P", kind="plan")
    section = core_add_section(eng, plan_id=plan["id"], heading="S", depth=2)
    cp = add_plan_checkpoint(eng, section["id"], "promote me to an item")
    out = srv.promote_checkpoint_to_item(cp["id"], "todo")
    assert set(out) == {"id", "item_type", "priority", "status", "title"}
    assert out["item_type"] == "todo"
    assert "description" not in out


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
