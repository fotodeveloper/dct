"""#909 K-17 / K-20 — create_item_conn, the one item-creation path.

Contract (dct/core/items.py::create_item_conn): on the CALLER's connection,
with create_item's full validation (title, short_title, widths, enums, a LIVE
project, tags through the similarity guard, provenance by column defaults);
it never commits or rolls back itself, so a refusal anywhere in the caller's
transaction leaves no item. create_item is a one-transaction wrapper around
it, and promote_checkpoint_to_item creates its item and its back-link in ONE
transaction with the checkpoint row locked first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import hold_row_and_race

from dct.core.items import create_item, create_item_conn
from dct.core.plans.crud import (
    add_plan_checkpoint,
    add_plan_section,
    create_plan,
    promote_checkpoint_to_item,
)
from dct.core.projects import create_project, delete_project
from dct.core.provenance import Provenance, get_provenance, set_provenance
from dct.db import item_tags, items, lock_row, plan_checkpoints, projects, tags


def _project_id(engine, slug="proj-a") -> int:
    return create_project(engine, slug=slug, name=slug.upper(), path=f"/tmp/{slug}")["id"]


def _count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def _checkpoint(engine, slug="proj-a", text="Important task") -> int:
    plan = create_plan(engine, slug, slug="p", title="P", kind="plan")
    section = add_plan_section(engine, plan["id"], "S", depth=2)
    return add_plan_checkpoint(engine, section["id"], text)["id"]


def _converted_to(engine, checkpoint_id):
    with engine.connect() as conn:
        return conn.execute(
            sa.select(plan_checkpoints.c.converted_to_item_id)
            .where(plan_checkpoints.c.id == checkpoint_id)
        ).scalar_one()


# ── the live-project rule (K-20) ─────────────────────────────────────────────


def test_a_soft_deleted_project_is_refused_alike_by_create_item_and_create_item_conn(engine):
    pid = _project_id(engine)
    assert delete_project(engine, "proj-a")

    with pytest.raises(ValueError) as by_slug:
        create_item(engine, "proj-a", "issue", "Lost work")
    with pytest.raises(ValueError) as by_id, engine.begin() as conn:
        create_item_conn(conn, pid, "issue", "Lost work")

    assert str(by_id.value) == str(by_slug.value) == "Project 'proj-a' not found"
    assert _count(engine, items) == 0


def test_an_unknown_project_id_is_refused_and_writes_nothing(engine):
    _project_id(engine)
    with pytest.raises(ValueError, match=r"^Project #424242 not found$"), engine.begin() as conn:
        create_item_conn(conn, 424242, "issue", "Orphan")
    assert _count(engine, items) == 0


def test_promoting_a_checkpoint_of_a_soft_deleted_project_leaves_no_item_and_no_link(engine):
    _project_id(engine)
    cp_id = _checkpoint(engine)
    assert delete_project(engine, "proj-a")

    with pytest.raises(ValueError, match="Project 'proj-a' not found"):
        promote_checkpoint_to_item(engine, cp_id, item_type="todo")

    assert _count(engine, items) == 0
    assert _converted_to(engine, cp_id) is None


# ── the same validation as create_item ───────────────────────────────────────


@pytest.mark.parametrize("kwargs,message", [
    ({"title": ""}, "Item title cannot be empty"),
    ({"title": " \t\n"}, "Item title cannot be empty"),
    ({"title": "x" * 501}, "too long"),
    ({"item_type": "bug"}, "Invalid item_type"),
    ({"priority": "P0"}, "Invalid priority"),
    ({"status": "done"}, "Invalid status"),
    ({"component": "c" * 101}, "too long"),
    ({"source_file": "f" * 501}, "too long"),
    ({"source_lines": "1" * 101}, "too long"),
    ({"short_title": "s" * 51}, "too long"),
])
def test_every_create_item_refusal_holds_on_the_connection_path(engine, kwargs, message):
    pid = _project_id(engine)
    args = {"item_type": "issue", "title": "Valid title", **kwargs}
    with pytest.raises(ValueError, match=message):
        create_item(engine, "proj-a", **args)
    with pytest.raises(ValueError, match=message), engine.begin() as conn:
        create_item_conn(conn, pid, **args)
    assert _count(engine, items) == 0


def test_the_widest_accepted_values_fit_on_the_connection_path(engine):
    pid = _project_id(engine)
    with engine.begin() as conn:
        item = create_item_conn(
            conn, pid, "improvement", "t" * 500, component="c" * 100,
            source_file="f" * 500, source_lines="1" * 100, short_title="  " + "s" * 48,
        )
    assert len(item["title"]) == 500
    assert item["short_title"] == "s" * 48  # stripped after the raw length check


def test_a_refused_tag_rolls_the_item_back_with_the_callers_transaction(engine):
    pid = _project_id(engine)
    create_item(engine, "proj-a", "todo", "Seed", tags=["test"])
    before = _count(engine, items)

    with pytest.raises(ValueError, match="(?i)similar"), engine.begin() as conn:
        create_item_conn(conn, pid, "todo", "Second", tags=["tests"])

    assert _count(engine, items) == before
    with engine.connect() as conn:
        names = conn.execute(sa.select(tags.c.name)).scalars().all()
    assert "tests" not in names


def test_the_item_follows_the_callers_rollback(engine):
    """create_item_conn never commits: a failure the CALLER hits after it
    (e.g. writing the promotion link) leaves no item behind."""
    pid = _project_id(engine)

    class LinkFailed(Exception):
        pass

    with pytest.raises(LinkFailed), engine.begin() as conn:
        created = create_item_conn(conn, pid, "issue", "Rolled back", tags=["perf"])
        assert created["id"] is not None
        raise LinkFailed

    assert _count(engine, items) == 0
    assert _count(engine, item_tags) == 0


def test_the_result_is_the_full_item_with_tags_provenance_and_the_given_clock(engine):
    pid = _project_id(engine)
    # Naive on purpose: items.created_at is TIMESTAMP WITHOUT TIME ZONE, and an
    # aware value is shifted by the PostgreSQL session time zone.
    stamp = datetime(2026, 1, 2, 3, 4, 5)  # noqa: DTZ001
    previous = get_provenance()
    set_provenance(Provenance(actor_type="agent", actor_client="codex", source="mcp"))
    try:
        with engine.begin() as conn:
            item = create_item_conn(conn, pid, "feature", "Clocked", tags=["b", "a"], now=stamp)
    finally:
        set_provenance(previous)

    assert item["tags"] == ["a", "b"]
    assert item["in_current_sprint"] is False
    assert (item["status"], item["priority"]) == ("open", "P2")
    with engine.connect() as conn:
        row = conn.execute(sa.select(items).where(items.c.id == item["id"])).one()
    assert row.created_at.replace(tzinfo=None) == stamp
    assert row.updated_at.replace(tzinfo=None) == stamp
    assert (row.actor_type, row.actor_client, row.source) == ("agent", "codex", "mcp")


def test_without_a_clock_the_database_default_stamps_the_row(engine):
    pid = _project_id(engine)
    with engine.begin() as conn:
        item = create_item_conn(conn, pid, "todo", "Default clock")
    assert item["created_at"] is not None


# ── promote_checkpoint_to_item: one transaction (K-17) ───────────────────────


def test_promotion_keeps_its_contract_title_from_the_text_and_a_back_link(engine):
    _project_id(engine)
    cp_id = _checkpoint(engine, text="x" * 600)
    item = promote_checkpoint_to_item(engine, cp_id, item_type="todo", short_title=" Label ")
    assert item["title"] == "x" * 500  # the checkpoint text cut to the column, as before
    assert item["short_title"] == "Label"
    assert _converted_to(engine, cp_id) == item["id"]


def test_a_failing_tag_write_during_promotion_leaves_no_item_and_no_link(engine, monkeypatch):
    _project_id(engine)
    cp_id = _checkpoint(engine)

    def refuse(*_args, **_kwargs):
        raise ValueError("tag write refused")

    monkeypatch.setattr("dct.core.items.set_item_tags_conn", refuse)
    with pytest.raises(ValueError, match="tag write refused"):
        promote_checkpoint_to_item(engine, cp_id, item_type="todo")

    assert _count(engine, items) == 0
    assert _converted_to(engine, cp_id) is None


def test_a_refused_promotion_input_leaves_no_item_and_no_link(engine):
    _project_id(engine)
    cp_id = _checkpoint(engine)
    with pytest.raises(ValueError, match="Invalid priority"):
        promote_checkpoint_to_item(engine, cp_id, item_type="todo", priority="P9")
    assert _count(engine, items) == 0
    assert _converted_to(engine, cp_id) is None


def test_a_promotion_waiting_on_a_deleted_checkpoint_creates_nothing(
    concurrent_engine, lock_probe,
):
    """The competitor is PROVEN blocked on the checkpoint row while the
    holder soft-deletes it; after the commit it must see the deletion and
    refuse — no item, no link (two transactions used to create the item
    from a checkpoint read before the delete)."""
    eng = concurrent_engine
    _project_id(eng)
    cp_id = _checkpoint(eng)

    outcome = hold_row_and_race(
        eng, lock_probe,
        lambda conn: lock_row(conn, plan_checkpoints, cp_id),
        lambda: promote_checkpoint_to_item(eng, cp_id, item_type="todo"),
        lambda conn: conn.execute(
            plan_checkpoints.update().where(plan_checkpoints.c.id == cp_id)
            .values(deleted_at=datetime.now(UTC))
        ),
    )

    assert isinstance(outcome, ValueError), outcome
    assert "not found" in str(outcome)
    assert _count(eng, items) == 0


def test_two_promotions_of_one_checkpoint_queue_and_each_lands_whole(
    concurrent_engine, lock_probe,
):
    """The existing contract creates a new item per call; under concurrency
    each call is atomic and they serialise on the checkpoint row, so the
    back-link names the item of the LAST promotion and no half-written
    promotion exists."""
    eng = concurrent_engine
    _project_id(eng)
    cp_id = _checkpoint(eng)
    first: dict = {}

    def holder_promotes(conn):
        # The holder's own promotion, inside the locked transaction.
        pid = conn.execute(sa.select(projects.c.id)).scalar_one()
        first.update(create_item_conn(conn, pid, "todo", "Important task"))
        conn.execute(plan_checkpoints.update().where(plan_checkpoints.c.id == cp_id)
                     .values(converted_to_item_id=first["id"]))

    outcome = hold_row_and_race(
        eng, lock_probe,
        lambda conn: lock_row(conn, plan_checkpoints, cp_id),
        lambda: promote_checkpoint_to_item(eng, cp_id, item_type="todo"),
        holder_promotes,
    )

    assert isinstance(outcome, dict), outcome
    assert outcome["id"] != first["id"]
    assert _count(eng, items) == 2
    assert _converted_to(eng, cp_id) == outcome["id"]
