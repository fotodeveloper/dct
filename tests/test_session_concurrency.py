"""#430: real separate-connection races in composed session operations."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa

from dct.core import sprints
from dct.core.handoff import create_handoff
from dct.core.items import create_item
from dct.core.projects import create_project
from dct.db import create_tables, get_engine
from dct.workflows import session


@pytest.fixture
def concurrent_engine(engine, tmp_path):
    """Use the guarded PG lane or WAL SQLite with separate connections."""
    if engine.dialect.name == "postgresql":
        yield engine
        return
    eng = get_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    create_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.mark.parametrize("reader", ["snapshot", "pickup"])
def test_snapshot_cannot_mix_membership_before_and_after_commit(
    concurrent_engine, monkeypatch, reader,
):
    eng = concurrent_engine
    create_project(eng, "test", "Test")
    sprint = sprints.create_sprint(eng, "test", "Sprint", status="active")
    item = create_item(eng, "test", "todo", "Concurrent member")
    original = sprints._sprint_membership_stats_by_sprint

    def stats_then_commit(conn, project_id, sprint_ids):
        result = original(conn, project_id, sprint_ids)
        # A separate connection commits AFTER the snapshot read its counts.
        sprints.assign_item_to_sprint(eng, item["id"], sprint["id"])
        return result

    monkeypatch.setattr(sprints, "_sprint_membership_stats_by_sprint", stats_then_commit)
    if reader == "snapshot":
        result = session.get_context_snapshot(eng, "test")
    else:
        result = session.pickup_work(eng, "test", consume=False)
    assert result["sprint"]["active_sprints"][0]["size"] == 0
    assert result["sprint"]["items"]["rows"] == []
    assert result["summary"]["sprint_size"] == 0
    assert len(sprints.list_items_in_sprint(eng, sprint["id"])) == 1


@pytest.mark.parametrize("operations", [
    ("ensure", "ensure"), ("handoff", "handoff"), ("ensure", "handoff"),
    ("add", "handoff"), ("set", "handoff"),
])
def test_concurrent_auto_creation_reuses_one_sprint(concurrent_engine, monkeypatch, operations):
    eng = concurrent_engine
    create_project(eng, "test", "Test")
    items = [create_item(eng, "test", "todo", str(i)) for i in range(2)]
    first_read = Event()
    second_read = Event()
    original = sprints._get_current_sprint_conn

    def coordinated_lookup(conn, project_id):
        result = original(conn, project_id)
        if not first_read.is_set():
            first_read.set()
            # Correct serialization prevents the second reader entering until
            # commit; the old code lets BOTH cache the absent sprint here.
            second_read.wait(0.3)
        else:
            second_read.set()
        return result

    monkeypatch.setattr(sprints, "_get_current_sprint_conn", coordinated_lookup)

    def run(index):
        if operations[index] == "ensure":
            return sprints.ensure_current_sprint(eng, "test")["id"]
        if operations[index] == "add":
            return sprints.add_item_to_current_sprint(eng, "test", items[index]["id"])["sprint_id"]
        if operations[index] == "set":
            return sprints.set_current_sprint_items(eng, "test", [items[index]["id"]])["sprint_id"]
        return session.handoff_work(
            eng, "test", prompt="Continue", created_by=f"agent-{index}",
            add_item_ids=[items[index]["id"]],
        )["sprint"]["sprint_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, 0)
        assert first_read.wait(5)
        second = pool.submit(run, 1)
        ids = [first.result(timeout=10), second.result(timeout=10)]
    assert ids[0] == ids[1]
    assert len(sprints.list_active_sprints(eng, "test")) == 1
    expected = {items[i]["id"] for i, op in enumerate(operations) if op != "ensure"}
    assert {item["id"] for item in sprints.list_items_in_sprint(eng, ids[0])} == expected


def test_invalid_current_sprint_add_rolls_back_auto_creation(concurrent_engine):
    eng = concurrent_engine
    create_project(eng, "test", "Test")
    with pytest.raises(ValueError, match="Item #999999 not found"):
        sprints.add_item_to_current_sprint(eng, "test", 999999)
    assert sprints.list_active_sprints(eng, "test") == []


def test_parallel_pickups_consume_handoff_exactly_once(concurrent_engine, monkeypatch):
    eng = concurrent_engine
    create_project(eng, "test", "Test")
    handoff = create_handoff(eng, "test", "Continue", "previous-agent")
    first_read = Event()
    second_read = Event()
    original = session._query_unconsumed_handoffs

    def coordinated_candidates(conn, project_id, scope=None):
        rows = original(conn, project_id, scope)
        if not first_read.is_set():
            first_read.set()
            second_read.wait(0.3)
        else:
            second_read.set()
        return rows

    monkeypatch.setattr(session, "_query_unconsumed_handoffs", coordinated_candidates)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.pickup_work, eng, "test", consumed_by="one")
        assert first_read.wait(5)
        second = pool.submit(session.pickup_work, eng, "test", consumed_by="two")
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert sum(result["consumed"] for result in results) == 1
    assert [r["picked_up"]["id"] for r in results if r["consumed"]] == [handoff["id"]]
    assert all(r["summary"]["handoffs_pending"] == 0 for r in results)


@pytest.mark.parametrize("fail", [False, True])
def test_project_lock_released_after_commit_or_rollback(concurrent_engine, fail):
    eng = concurrent_engine
    project = create_project(eng, "test", "Test")
    item = create_item(eng, "test", "todo", "Member")
    # Reserve a DISTINCT physical connection; pooled reuse could hide a leaked
    # session advisory lock because PostgreSQL lets the owner acquire it again.
    with eng.connect() as observer:
        if fail:
            with pytest.raises(ValueError):
                session.handoff_work(eng, "test", prompt="", created_by="agent", add_item_ids=[item["id"]])
        else:
            session.handoff_work(eng, "test", prompt="Continue", created_by="agent", add_item_ids=[item["id"]])
        if eng.dialect.name == "postgresql":
            acquired = observer.execute(sa.text(
                "SELECT pg_try_advisory_lock(430, :project_id)"
            ), {"project_id": project["id"]}).scalar_one()
            try:
                assert acquired is True
            finally:
                if acquired:
                    observer.execute(sa.text(
                        "SELECT pg_advisory_unlock(430, :project_id)"
                    ), {"project_id": project["id"]})
        else:
            observer.exec_driver_sql("BEGIN IMMEDIATE")
    assert len(sprints.list_active_sprints(eng, "test")) == (0 if fail else 1)


def test_failed_pg_unlock_discards_locked_connection(concurrent_engine):
    eng = concurrent_engine
    if eng.dialect.name != "postgresql":
        pytest.skip("PostgreSQL session advisory lock cleanup")
    project = create_project(eng, "test", "Test")

    def fail_unlock(conn, cursor, statement, parameters, context, executemany):
        if "pg_advisory_unlock" in statement:
            raise RuntimeError("injected unlock failure")

    with eng.connect() as observer:
        sa.event.listen(eng, "before_cursor_execute", fail_unlock)
        try:
            with pytest.raises(RuntimeError, match="injected unlock failure"):
                session.handoff_work(eng, "test", prompt="Continue", created_by="agent")
        finally:
            sa.event.remove(eng, "before_cursor_execute", fail_unlock)
        acquired = observer.execute(sa.text(
            "SELECT pg_try_advisory_lock(430, :project_id)"
        ), {"project_id": project["id"]}).scalar_one()
        try:
            assert acquired is True
        finally:
            if acquired:
                observer.execute(sa.text(
                    "SELECT pg_advisory_unlock(430, :project_id)"
                ), {"project_id": project["id"]})


@pytest.mark.parametrize("mode", ["auto", "true", "explicit"])
def test_pg_pickup_losing_to_standalone_consume_preserves_target(
    concurrent_engine, monkeypatch, mode,
):
    eng = concurrent_engine
    if eng.dialect.name != "postgresql":
        pytest.skip("PostgreSQL repeatable-read serialization conflict")
    create_project(eng, "test", "Test")
    target = create_handoff(eng, "test", "First", "previous-agent")
    original = session.handoff_core._consume_handoff_conn
    followup = {}

    def competitor_wins(conn, handoff_id, consumed_by):
        # Standalone consume is a supported surface outside the project lock.
        # Its separate transaction commits AFTER pickup selected its target.
        with eng.begin() as competitor:
            assert original(competitor, handoff_id, "winner") is not None
        followup.update(create_handoff(eng, "test", "Different handoff", "winner"))
        return original(conn, handoff_id, consumed_by)

    monkeypatch.setattr(session.handoff_core, "_consume_handoff_conn", competitor_wins)
    kwargs = {"consumed_by": "loser"}
    if mode == "true":
        kwargs["consume"] = True
    elif mode == "explicit":
        kwargs["handoff_id"] = target["id"]

    if mode == "explicit":
        with pytest.raises(ValueError, match=f"Handoff #{target['id']} not found or already consumed"):
            session.pickup_work(eng, "test", **kwargs)
    else:
        result = session.pickup_work(eng, "test", **kwargs)
        assert result["consumed"] is False
        assert result["picked_up"] is None
        assert result["handoff"]["latest"]["id"] == followup["id"]
        assert result["summary"]["handoffs_pending"] == 1
    pending = session.handoff_core.list_handoffs(eng, "test", unconsumed_only=True)
    assert [row["id"] for row in pending] == [followup["id"]]


def test_pickup_does_not_hide_other_database_errors(concurrent_engine, monkeypatch):
    eng = concurrent_engine
    create_project(eng, "test", "Test")
    create_handoff(eng, "test", "Continue", "previous-agent")
    original = RuntimeError("deadlock")
    original.sqlstate = "40P01"
    error = sa.exc.OperationalError("consume", {}, original)

    def fail(conn, handoff_id, consumed_by):
        raise error

    monkeypatch.setattr(session.handoff_core, "_consume_handoff_conn", fail)
    with pytest.raises(sa.exc.OperationalError) as raised:
        session.pickup_work(eng, "test", consumed_by="agent")
    assert raised.value is error
