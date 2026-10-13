"""#910 — a terminal transition and the writers of gate checkpoints serialise
on the item row, so a gate committed while the close waits is SEEN and refused.

Before the fix, on PostgreSQL at READ COMMITTED nothing ordered the gate guard
(`_refuse_pending_gates_conn`, read + status write in one transaction) against
`add_checkpoint` or a reopen: a gate committed between the check and the write
closed the item past a pending gate. SQLite serialises writers, but its driver
opens no transaction before a SELECT, so the same gap existed on a file database.

Proof pattern (K-64, K-91 — never a sleep as evidence): a HOLDER connection
takes the item row lock through the shared helper; the competitor runs in a
worker thread and the test waits until it is PROVEN blocked by that holder
(conftest.LockProbe.wait_until_row_blocked: on PostgreSQL the holder's pid is
in pg_blocking_pids of the competitor's backend); then the holder commits its
write and the competitor's outcome is the assertion.
"""

from datetime import UTC, datetime

import pytest
from conftest import hold_row_and_race

from dct.core.checkpoints import add_checkpoint, set_status
from dct.core.items import create_item, get_item, resolve_item, update_item
from dct.core.projects import create_project
from dct.db import item_checkpoints, items, lock_item_row

pytestmark = pytest.mark.usefixtures("lock_probe")


def _item(eng):
    create_project(eng, "proj-g", "G")
    return create_item(eng, "proj-g", "feature", "guarded")["id"]


def _hold_and_race(eng, probe, item_id, competitor, holder_write):
    """conftest.hold_row_and_race on the item row: the competitor is proven
    blocked by the holder's `lock_item_row` before the holder writes."""
    return hold_row_and_race(
        eng, probe, lambda conn: lock_item_row(conn, item_id), competitor, holder_write)


def _insert_gate(item_id):
    def write(conn):
        conn.execute(item_checkpoints.insert().values(
            item_id=item_id, text="owner sign-off", sort_order=10, kind="approval"))
    return write


@pytest.mark.parametrize("close", ["resolve_item", "update_item"])
def test_a_gate_committed_while_the_close_waits_refuses_the_close(
    concurrent_engine, lock_probe, close,
):
    eng = concurrent_engine
    item_id = _item(eng)
    if close == "resolve_item":
        competitor = lambda: resolve_item(eng, item_id, "resolved", resolution="done")
    else:
        competitor = lambda: update_item(eng, item_id, status="resolved")

    outcome = _hold_and_race(eng, lock_probe, item_id, competitor, _insert_gate(item_id))

    assert isinstance(outcome, ValueError), outcome
    assert str(outcome).startswith(f"Cannot resolve item #{item_id}: 1 gate checkpoint(s) pending")
    item = get_item(eng, item_id)
    assert item["status"] == "open" and item["resolved_at"] is None
    assert [c["status"] for c in item["checkpoints"]] == ["pending"]


def _close_directly(item_id):
    def write(conn):
        conn.execute(items.update().where(items.c.id == item_id).values(
            status="resolved", resolved_at=datetime.now(UTC)))
    return write


@pytest.mark.parametrize("writer", ["add_checkpoint", "reopen"])
def test_every_gate_writer_waits_on_the_item_row(concurrent_engine, lock_probe, writer):
    """The other side of the same lock: add_checkpoint and set_status (the
    reopen path of MCP and CLI) block while a close holds the row, so a gate
    can never slip in between the guard's read and the status write."""
    eng = concurrent_engine
    item_id = _item(eng)
    if writer == "add_checkpoint":
        competitor = lambda: add_checkpoint(eng, item_id, "late gate", kind="approval")
    else:
        cp_id = add_checkpoint(eng, item_id, "gate", kind="approval")["id"]
        set_status(eng, cp_id, "done")
        competitor = lambda: set_status(eng, cp_id, "pending")

    outcome = _hold_and_race(eng, lock_probe, item_id, competitor, _close_directly(item_id))

    assert isinstance(outcome, dict), outcome
    assert outcome["status"] == "pending"
    assert get_item(eng, item_id)["status"] == "resolved", "the holder's close landed first"


def test_lock_item_row_reports_a_missing_item(engine):
    with engine.begin() as conn:
        assert lock_item_row(conn, 424242) is False
