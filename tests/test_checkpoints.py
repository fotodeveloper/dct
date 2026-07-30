"""Tests for checkpoint CRUD and progress computation."""

import pytest

from dct.core.checkpoints import (
    add_checkpoint,
    delete_checkpoint,
    get_checkpoint,
    get_progress,
    list_checkpoints,
    set_status,
    update_text,
)
from dct.core.items import create_item, get_item
from dct.core.projects import create_project


@pytest.fixture
def item(engine):
    create_project(engine, slug="p", name="P")
    return create_item(engine, "p", "feature", "Test feature")


# ── Add/list basic ───────────────────────────────────────────────────────────


def test_add_and_list(engine, item):
    cp = add_checkpoint(engine, item["id"], "Step 1")
    assert cp["text"] == "Step 1"
    assert cp["status"] == "pending"
    assert cp["sort_order"] == 10

    cps = list_checkpoints(engine, item["id"])
    assert len(cps) == 1


def test_sort_order_auto_increment(engine, item):
    c1 = add_checkpoint(engine, item["id"], "First")
    c2 = add_checkpoint(engine, item["id"], "Second")
    c3 = add_checkpoint(engine, item["id"], "Third")
    assert c1["sort_order"] == 10
    assert c2["sort_order"] == 20
    assert c3["sort_order"] == 30

    cps = list_checkpoints(engine, item["id"])
    assert [c["text"] for c in cps] == ["First", "Second", "Third"]


def test_sort_order_explicit(engine, item):
    c1 = add_checkpoint(engine, item["id"], "A", sort_order=100)
    c2 = add_checkpoint(engine, item["id"], "B", sort_order=50)
    cps = list_checkpoints(engine, item["id"])
    assert [c["text"] for c in cps] == ["B", "A"]
    assert c1["sort_order"] == 100
    assert c2["sort_order"] == 50


# ── Validation ───────────────────────────────────────────────────────────────


def test_empty_text_raises(engine, item):
    with pytest.raises(ValueError, match="cannot be empty"):
        add_checkpoint(engine, item["id"], "")


def test_whitespace_text_raises(engine, item):
    with pytest.raises(ValueError, match="cannot be empty"):
        add_checkpoint(engine, item["id"], "   ")


def test_nonexistent_item_raises(engine):
    with pytest.raises(ValueError, match="not found"):
        add_checkpoint(engine, 99999, "Orphan")


def test_item_deleted_checkpoint_raises(engine):
    create_project(engine, slug="p", name="P")
    i = create_item(engine, "p", "feature", "Gone")
    from dct.core.items import delete_item
    delete_item(engine, i["id"])
    with pytest.raises(ValueError, match="not found"):
        add_checkpoint(engine, i["id"], "Should fail")


# ── Status transitions ──────────────────────────────────────────────────────


def test_transition_to_done(engine, item):
    cp = add_checkpoint(engine, item["id"], "X")
    updated = set_status(engine, cp["id"], "done")
    assert updated["status"] == "done"
    assert updated["updated_at"] != cp["updated_at"]


def test_transition_to_rejected(engine, item):
    cp = add_checkpoint(engine, item["id"], "Scope-change step")
    updated = set_status(engine, cp["id"], "rejected")
    assert updated["status"] == "rejected"


def test_reopen_from_done(engine, item):
    cp = add_checkpoint(engine, item["id"], "Y")
    set_status(engine, cp["id"], "done")
    reopened = set_status(engine, cp["id"], "pending")
    assert reopened["status"] == "pending"


def test_reopen_from_rejected(engine, item):
    cp = add_checkpoint(engine, item["id"], "Z")
    set_status(engine, cp["id"], "rejected")
    reopened = set_status(engine, cp["id"], "pending")
    assert reopened["status"] == "pending"


def test_invalid_status(engine, item):
    cp = add_checkpoint(engine, item["id"], "Q")
    with pytest.raises(ValueError, match="Invalid status"):
        set_status(engine, cp["id"], "completed")


def test_set_status_nonexistent_returns_none(engine):
    assert set_status(engine, 99999, "done") is None


# ── Text update ──────────────────────────────────────────────────────────────


def test_update_text(engine, item):
    cp = add_checkpoint(engine, item["id"], "Original")
    updated = update_text(engine, cp["id"], "Revised")
    assert updated["text"] == "Revised"


def test_update_text_empty_raises(engine, item):
    cp = add_checkpoint(engine, item["id"], "X")
    with pytest.raises(ValueError, match="cannot be empty"):
        update_text(engine, cp["id"], "")


# ── Soft delete ──────────────────────────────────────────────────────────────


def test_soft_delete(engine, item):
    cp = add_checkpoint(engine, item["id"], "To be deleted")
    assert delete_checkpoint(engine, cp["id"]) is True
    assert get_checkpoint(engine, cp["id"]) is None
    assert list_checkpoints(engine, item["id"]) == []


def test_double_delete(engine, item):
    cp = add_checkpoint(engine, item["id"], "X")
    assert delete_checkpoint(engine, cp["id"]) is True
    assert delete_checkpoint(engine, cp["id"]) is False


def test_list_include_deleted(engine, item):
    cp = add_checkpoint(engine, item["id"], "Keep-in-archive")
    delete_checkpoint(engine, cp["id"])
    assert list_checkpoints(engine, item["id"]) == []
    all_cps = list_checkpoints(engine, item["id"], include_deleted=True)
    assert len(all_cps) == 1


def test_list_hides_rejected_by_default(engine, item):
    """Rejected checkpoints are ledger artifacts, not active TODOs. Hide them
    from list_checkpoints by default; include_rejected=True brings them back."""
    active = add_checkpoint(engine, item["id"], "Still relevant")
    scrapped = add_checkpoint(engine, item["id"], "Scope-change — abandon")
    set_status(engine, scrapped["id"], "rejected")

    default = list_checkpoints(engine, item["id"])
    assert [c["id"] for c in default] == [active["id"]]

    full = list_checkpoints(engine, item["id"], include_rejected=True)
    assert {c["id"] for c in full} == {active["id"], scrapped["id"]}


def test_list_with_rejected_and_done_default(engine, item):
    """Default view keeps 'done' checkpoints (part of TODO history), drops 'rejected'."""
    a = add_checkpoint(engine, item["id"], "A")
    add_checkpoint(engine, item["id"], "B")
    c = add_checkpoint(engine, item["id"], "C")
    set_status(engine, a["id"], "done")
    set_status(engine, c["id"], "rejected")

    default = list_checkpoints(engine, item["id"])
    statuses = {cp["status"] for cp in default}
    assert statuses == {"done", "pending"}  # rejected hidden


def test_get_item_still_returns_all_checkpoints(engine, item):
    """get_item() is the 'inspect this specific thing' API — it must NOT filter
    by checkpoint status (user decision 2026-04-17). Only list_checkpoints does.
    """
    add_checkpoint(engine, item["id"], "A")
    b = add_checkpoint(engine, item["id"], "B")
    set_status(engine, b["id"], "rejected")

    fetched = get_item(engine, item["id"])
    assert fetched is not None
    statuses = {cp["status"] for cp in fetched["checkpoints"]}
    assert statuses == {"pending", "rejected"}


# ── Progress ─────────────────────────────────────────────────────────────────


def test_progress_empty(engine, item):
    p = get_progress(engine, item["id"])
    assert p == {"total": 0, "done": 0, "rejected": 0, "pending": 0, "active": 0, "percent": 0}


def test_progress_all_pending(engine, item):
    for i in range(3):
        add_checkpoint(engine, item["id"], f"Step {i}")
    p = get_progress(engine, item["id"])
    assert p["total"] == 3
    assert p["pending"] == 3
    assert p["done"] == 0
    assert p["active"] == 3
    assert p["percent"] == 0


def test_progress_mixed(engine, item):
    c1 = add_checkpoint(engine, item["id"], "A")
    c2 = add_checkpoint(engine, item["id"], "B")
    add_checkpoint(engine, item["id"], "C")
    c4 = add_checkpoint(engine, item["id"], "D")
    set_status(engine, c1["id"], "done")
    set_status(engine, c2["id"], "done")
    set_status(engine, c4["id"], "rejected")

    p = get_progress(engine, item["id"])
    assert p["total"] == 4
    assert p["done"] == 2
    assert p["pending"] == 1
    assert p["rejected"] == 1
    assert p["active"] == 3  # total - rejected
    assert p["percent"] == 67  # 2 of 3 active = 66.67 → 67


def test_progress_all_done(engine, item):
    for i in range(3):
        c = add_checkpoint(engine, item["id"], f"Step {i}")
        set_status(engine, c["id"], "done")
    p = get_progress(engine, item["id"])
    assert p["percent"] == 100


def test_progress_only_rejected(engine, item):
    c = add_checkpoint(engine, item["id"], "A")
    set_status(engine, c["id"], "rejected")
    p = get_progress(engine, item["id"])
    assert p["active"] == 0
    assert p["percent"] == 0  # no division by zero


def test_progress_ignores_deleted(engine, item):
    c1 = add_checkpoint(engine, item["id"], "Kept")
    c2 = add_checkpoint(engine, item["id"], "Gone")
    set_status(engine, c1["id"], "done")
    delete_checkpoint(engine, c2["id"])
    p = get_progress(engine, item["id"])
    assert p["total"] == 1
    assert p["percent"] == 100


# ── get_item integration ─────────────────────────────────────────────────────


def test_get_item_includes_checkpoints_and_progress(engine, item):
    add_checkpoint(engine, item["id"], "Step 1")
    c2 = add_checkpoint(engine, item["id"], "Step 2")
    set_status(engine, c2["id"], "done")

    fetched = get_item(engine, item["id"])
    assert "checkpoints" in fetched
    assert len(fetched["checkpoints"]) == 2
    assert "progress" in fetched
    assert fetched["progress"]["done"] == 1
    assert fetched["progress"]["percent"] == 50


def test_get_item_no_checkpoints(engine, item):
    fetched = get_item(engine, item["id"])
    assert fetched["checkpoints"] == []
    assert fetched["progress"]["total"] == 0


# ── Cross-item isolation ─────────────────────────────────────────────────────


def test_isolation_between_items(engine):
    create_project(engine, slug="p", name="P")
    i1 = create_item(engine, "p", "feature", "A")
    i2 = create_item(engine, "p", "feature", "B")
    add_checkpoint(engine, i1["id"], "For A")
    add_checkpoint(engine, i2["id"], "For B")

    assert len(list_checkpoints(engine, i1["id"])) == 1
    assert len(list_checkpoints(engine, i2["id"])) == 1
    assert list_checkpoints(engine, i1["id"])[0]["text"] == "For A"


# ── Gate checkpoints (v0.4.0) ────────────────────────────────────────────────


class TestGateCheckpoints:
    """Gate checkpoints (kind != 'task' except 'cc-task') block resolve_item
    until completed or rejected. See #33 and CLAUDE.md "Gate checkpoints"."""

    def test_default_kind_is_task(self, engine, item):
        cp = add_checkpoint(engine, item["id"], "Normal step")
        assert cp["kind"] == "task"

    def test_task_checkpoint_does_not_block_resolve(self, engine, item):
        from dct.core.items import resolve_item
        add_checkpoint(engine, item["id"], "Pending task")  # kind=task, pending
        # Task pending doesn't block — it's not a gate
        result = resolve_item(engine, item["id"])
        assert result is not None
        assert result["status"] == "resolved"

    def test_pending_dogfood_gate_blocks_resolve(self, engine, item):
        from dct.core.items import resolve_item
        add_checkpoint(engine, item["id"], "User validates", kind="dogfood")
        with pytest.raises(ValueError, match="gate checkpoint.*pending"):
            resolve_item(engine, item["id"])

    def test_completed_gate_unblocks_resolve(self, engine, item):
        from dct.core.items import resolve_item
        cp = add_checkpoint(engine, item["id"], "Security review", kind="security")
        set_status(engine, cp["id"], "done")
        result = resolve_item(engine, item["id"])
        assert result is not None
        assert result["status"] == "resolved"

    def test_rejected_gate_unblocks_resolve(self, engine, item):
        """Rejecting a gate means 'not applicable' — resolve should succeed."""
        from dct.core.items import resolve_item
        cp = add_checkpoint(engine, item["id"], "Stakeholder approval", kind="approval")
        set_status(engine, cp["id"], "rejected")
        result = resolve_item(engine, item["id"])
        assert result is not None

    def test_cc_task_kind_is_non_blocking(self, engine, item):
        """cc-task mirrors Claude Code TaskCreate — ephemeral, non-blocking."""
        from dct.core.items import resolve_item
        add_checkpoint(engine, item["id"], "Mirrored from CC", kind="cc-task")
        result = resolve_item(engine, item["id"])
        assert result is not None

    def test_multiple_gates_all_must_close(self, engine, item):
        from dct.core.items import resolve_item
        g1 = add_checkpoint(engine, item["id"], "Review 1", kind="review")
        g2 = add_checkpoint(engine, item["id"], "Review 2", kind="review")
        set_status(engine, g1["id"], "done")
        # g2 still pending
        with pytest.raises(ValueError, match="gate checkpoint"):
            resolve_item(engine, item["id"])
        set_status(engine, g2["id"], "done")
        assert resolve_item(engine, item["id"]) is not None

    def test_mixed_tasks_and_gates(self, engine, item):
        from dct.core.items import resolve_item
        add_checkpoint(engine, item["id"], "Pending task")  # non-blocking
        gate = add_checkpoint(engine, item["id"], "Dogfood", kind="dogfood")
        with pytest.raises(ValueError):
            resolve_item(engine, item["id"])
        set_status(engine, gate["id"], "done")
        assert resolve_item(engine, item["id"]) is not None

    def test_empty_kind_raises(self, engine, item):
        with pytest.raises(ValueError, match="kind cannot be empty"):
            add_checkpoint(engine, item["id"], "X", kind="   ")

    def test_custom_kind_treated_as_gate(self, engine, item):
        """Unknown kinds (OSS users define own) are conservatively gates."""
        from dct.core.items import resolve_item
        add_checkpoint(engine, item["id"], "Legal sign-off", kind="legal")
        with pytest.raises(ValueError, match="gate checkpoint"):
            resolve_item(engine, item["id"])

    def test_gate_error_message_lists_kind(self, engine, item):
        from dct.core.items import resolve_item
        add_checkpoint(engine, item["id"], "G1", kind="dogfood")
        add_checkpoint(engine, item["id"], "G2", kind="security")
        with pytest.raises(ValueError) as exc:
            resolve_item(engine, item["id"])
        msg = str(exc.value)
        assert "[dogfood]" in msg
        assert "[security]" in msg
        assert "2 gate checkpoint" in msg
