"""Gate guard on EVERY terminal transition, whatever the client.

A pending gate checkpoint (any kind outside NON_GATE_KINDS — dogfood,
security, approval, review, custom) blocks the move to resolved / wont_fix.
resolve_item always enforced it; update_item(status=<terminal>) used to skip
it, so any MCP client, `dct update --status resolved` or a core caller could
close an item past its dogfood gate. Both paths now run ONE shared check in
the same transaction as the status write, and a refusal leaves no trace: no
field change, no reason note, no tag change. Pending gates are never
rejected automatically.
"""

import pytest
from conftest import run_cli

from dct.core.checkpoints import add_checkpoint, get_checkpoint, set_status
from dct.core.items import create_item, get_item, resolve_item, update_item
from dct.core.projects import create_project

TERMINAL = ("resolved", "wont_fix")


@pytest.fixture
def gated(engine):
    """An open feature with tags and one PENDING dogfood gate."""
    create_project(engine, slug="proj-g", name="Gate Project", path="/tmp/proj-g")
    item = create_item(engine, "proj-g", "feature", "Gated", tags=["ui"], priority="P2")
    gate = add_checkpoint(engine, item["id"], "Owner validates", kind="dogfood")
    return item, gate


def _snapshot(engine, item_id):
    """Everything a refused update must leave untouched."""
    d = get_item(engine, item_id)
    return {
        k: d[k]
        for k in (
            "title", "status", "priority", "component", "resolution",
            "description", "resolved_at", "updated_at", "tags",
        )
    } | {"notes": [n["note"] for n in d["notes"]]}


# ── core ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", TERMINAL)
def test_core_update_to_terminal_refused_with_pending_gate(engine, gated, status):
    item, gate = gated
    before = _snapshot(engine, item["id"])
    with pytest.raises(ValueError, match="gate checkpoint.*pending"):
        update_item(
            engine, item["id"], status=status, title="Renamed", priority="P1",
            resolution="done", tags=["ui", "shipped"], reason="closing it",
        )
    # No partial update: no field, no note, no tag change.
    assert _snapshot(engine, item["id"]) == before
    # And the gate itself is NOT rejected automatically.
    assert get_checkpoint(engine, gate["id"])["status"] == "pending"


@pytest.mark.parametrize("status", TERMINAL)
def test_core_refusal_message_is_the_resolve_item_message(engine, gated, status):
    """One check, one message — update_item does not word its own copy."""
    item, _ = gated
    with pytest.raises(ValueError) as via_update:
        update_item(engine, item["id"], status=status)
    with pytest.raises(ValueError) as via_resolve:
        resolve_item(engine, item["id"], status=status)
    assert str(via_update.value) == str(via_resolve.value)


@pytest.mark.parametrize("gate_status", ("done", "rejected"))
@pytest.mark.parametrize("status", TERMINAL)
def test_core_update_to_terminal_allowed_once_gate_closed(engine, gated, status, gate_status):
    item, gate = gated
    set_status(engine, gate["id"], gate_status)
    out = update_item(engine, item["id"], status=status, reason="validated")
    assert out["status"] == status
    assert out["resolved_at"] is not None
    assert [n["note"] for n in out["notes"]] == ["validated"]


@pytest.mark.parametrize(
    "fields",
    (
        {"status": "in_progress"},
        {"status": "open"},
        {"priority": "P1", "title": "Retitled"},
        {"tags": ["ui", "later"]},
        {"resolution": "draft rationale"},
    ),
)
def test_core_non_terminal_update_unaffected_by_pending_gate(engine, gated, fields):
    item, _ = gated
    out = update_item(engine, item["id"], **fields)
    for k, v in fields.items():
        assert out[k] == (sorted(v) if k == "tags" else v)
    assert out["status"] not in TERMINAL


def test_core_task_and_cc_task_checkpoints_do_not_gate_update(engine):
    create_project(engine, slug="proj-t", name="T", path="/tmp/proj-t")
    item = create_item(engine, "proj-t", "todo", "Plain")
    add_checkpoint(engine, item["id"], "step", kind="task")
    add_checkpoint(engine, item["id"], "mirror", kind="cc-task")
    assert update_item(engine, item["id"], status="resolved")["status"] == "resolved"


def test_core_custom_gate_kind_blocks_update(engine):
    create_project(engine, slug="proj-c", name="C", path="/tmp/proj-c")
    item = create_item(engine, "proj-c", "feature", "Custom gate")
    add_checkpoint(engine, item["id"], "Legal sign-off", kind="legal")
    with pytest.raises(ValueError, match="gate checkpoint"):
        update_item(engine, item["id"], status="wont_fix")
    assert get_item(engine, item["id"])["status"] == "open"


def test_core_unknown_item_still_returns_none(engine):
    assert update_item(engine, 987654, status="resolved") is None


# ── MCP ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", TERMINAL)
def test_mcp_update_item_to_terminal_refused_with_pending_gate(srv, status):
    item = srv.create_item("feature", "G", project="test", tags=["ui"])
    gate = srv.add_checkpoint(item["id"], "user validates", kind="dogfood")
    before = srv.get_item(item["id"])
    out = srv.update_item(
        item["id"], status=status, priority="P1", tags=["ui", "done"],
        resolution="shipped", reason="closing",
    )
    assert set(out) == {"error"}
    assert "gate checkpoint" in out["error"].lower()
    after = srv.get_item(item["id"])
    for k in ("status", "priority", "resolution", "resolved_at", "updated_at", "tags", "notes"):
        assert after[k] == before[k], k
    assert after["checkpoints"][0]["id"] == gate["id"]
    assert after["checkpoints"][0]["status"] == "pending"


@pytest.mark.parametrize("gate_status", ("done", "rejected"))
def test_mcp_update_item_to_terminal_allowed_once_gate_closed(srv, gate_status):
    item = srv.create_item("feature", "G", project="test")
    gate = srv.add_checkpoint(item["id"], "user validates", kind="dogfood")
    closer = srv.complete_checkpoint if gate_status == "done" else srv.reject_checkpoint
    closer(gate["id"])
    out = srv.update_item(item["id"], status="resolved")
    assert out["status"] == "resolved"
    assert out["resolved_at"] is not None


def test_mcp_update_item_non_terminal_with_pending_gate_allowed(srv):
    item = srv.create_item("feature", "G", project="test")
    srv.add_checkpoint(item["id"], "user validates", kind="dogfood")
    out = srv.update_item(item["id"], status="in_progress", priority="P1")
    assert out["status"] == "in_progress"
    assert out["priority"] == "P1"


# ── CLI: dct update --status resolved ─────────────────────────────────────────


@pytest.mark.parametrize("status", TERMINAL)
def test_cli_update_status_terminal_refused_with_pending_gate(cli_engine, monkeypatch, capsys, status):
    item = create_item(cli_engine, "proj-a", "feature", "CLI gated", tags=["ui"])
    add_checkpoint(cli_engine, item["id"], "Owner validates", kind="dogfood")
    before = _snapshot(cli_engine, item["id"])
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, [
            "update", str(item["id"]), "--status", status,
            "--tags", "ui,done", "--reason", "closing",
        ])
    assert exc.value.code == 1
    assert "gate checkpoint" in capsys.readouterr().err
    assert _snapshot(cli_engine, item["id"]) == before


def test_cli_update_status_resolved_allowed_once_gate_done(cli_engine, monkeypatch, capsys):
    item = create_item(cli_engine, "proj-a", "feature", "CLI gated")
    gate = add_checkpoint(cli_engine, item["id"], "Owner validates", kind="dogfood")
    set_status(cli_engine, gate["id"], "done")
    run_cli(monkeypatch, ["update", str(item["id"]), "--status", "resolved"])
    assert capsys.readouterr().out.startswith("✓")
    assert get_item(cli_engine, item["id"])["status"] == "resolved"
