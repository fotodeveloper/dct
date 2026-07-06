import pytest

from dct.core.projects import create_project
from dct.core.handoff import (
    create_handoff, list_handoffs, get_handoff,
    consume_handoff, delete_handoff, update_handoff,
)


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="dct", name="dct", path="/tmp/dct")


def test_create_and_list(engine, proj):
    h = create_handoff(
        engine, "dct",
        prompt="Zacznij od #22, potem #21",
        created_by="claude-opus-4-7 @ 2026-04-17T14:30Z",
        scope="sprint-v0.1",
    )
    assert h["prompt"].startswith("Zacznij")
    assert h["scope"] == "sprint-v0.1"
    assert h["consumed_at"] is None

    listed = list_handoffs(engine, "dct")
    assert len(listed) == 1
    assert listed[0]["id"] == h["id"]


def test_empty_prompt_raises(engine, proj):
    with pytest.raises(ValueError, match="Handoff prompt cannot be empty"):
        create_handoff(engine, "dct", prompt="", created_by="me")


def test_empty_created_by_raises(engine, proj):
    with pytest.raises(ValueError, match="created_by cannot be empty"):
        create_handoff(engine, "dct", prompt="Do something", created_by="")


def test_consume_marks_timestamp(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    consumed = consume_handoff(engine, h["id"], consumed_by="B")
    assert consumed is not None
    assert consumed["consumed_at"] is not None
    assert consumed["consumed_by"] == "B"


def test_consumed_not_in_unconsumed_list(engine, proj):
    h1 = create_handoff(engine, "dct", prompt="P1", created_by="A")
    h2 = create_handoff(engine, "dct", prompt="P2", created_by="A")

    consume_handoff(engine, h1["id"])

    unconsumed = list_handoffs(engine, "dct", unconsumed_only=True)
    assert [x["id"] for x in unconsumed] == [h2["id"]]

    all_entries = list_handoffs(engine, "dct", unconsumed_only=False)
    assert {x["id"] for x in all_entries} == {h1["id"], h2["id"]}


def test_consume_twice_returns_none(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    first = consume_handoff(engine, h["id"])
    assert first is not None

    # Already consumed — second call returns None (no row matches filter).
    second = consume_handoff(engine, h["id"])
    assert second is None


def test_parallel_unconsumed_with_different_scopes(engine, proj):
    """Two concurrent sessions can each leave a handoff; both visible until consumed."""
    h1 = create_handoff(engine, "dct", prompt="Session A work", created_by="A", scope="refactor-auth")
    h2 = create_handoff(engine, "dct", prompt="Session B work", created_by="B", scope="sprint-v0.2")

    listed = list_handoffs(engine, "dct")
    scopes = {x["scope"] for x in listed}
    assert scopes == {"refactor-auth", "sprint-v0.2"}
    assert len(listed) == 2
    assert h1["id"] != h2["id"]


def test_delete_handoff(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    assert delete_handoff(engine, h["id"]) is True
    assert get_handoff(engine, h["id"]) is None
    assert list_handoffs(engine, "dct", unconsumed_only=False) == []


def test_cross_project_isolation(engine):
    create_project(engine, slug="a", name="A", path="/tmp/a")
    create_project(engine, slug="b", name="B", path="/tmp/b")

    create_handoff(engine, "a", prompt="for a", created_by="x")
    create_handoff(engine, "b", prompt="for b", created_by="x")

    a_list = list_handoffs(engine, "a")
    b_list = list_handoffs(engine, "b")
    assert len(a_list) == 1 and a_list[0]["prompt"] == "for a"
    assert len(b_list) == 1 and b_list[0]["prompt"] == "for b"


def test_update_amends_fields_and_preserves_identity(engine, proj):
    h = create_handoff(
        engine, "dct", prompt="old prompt", created_by="A", scope="old-scope",
        context_summary="old summary",
    )
    updated = update_handoff(
        engine, h["id"],
        prompt="new prompt", scope="new-scope", context_summary="new summary",
    )
    assert updated is not None
    assert updated["prompt"] == "new prompt"
    assert updated["scope"] == "new-scope"
    assert updated["context_summary"] == "new summary"
    # identity preserved — amend in place, not supersede
    assert updated["id"] == h["id"]
    assert updated["created_at"] == h["created_at"]
    assert updated["created_by"] == "A"
    assert updated["consumed_at"] is None


def test_update_partial_leaves_other_fields(engine, proj):
    h = create_handoff(engine, "dct", prompt="keep me", created_by="A", scope="s1")
    updated = update_handoff(engine, h["id"], scope="s2")
    assert updated["scope"] == "s2"
    assert updated["prompt"] == "keep me"  # untouched (None = don't change)


def test_update_consumed_returns_none_and_does_not_mutate(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    consume_handoff(engine, h["id"])
    # consumed handoffs are immutable ledger entries
    assert update_handoff(engine, h["id"], prompt="too late") is None
    still = get_handoff(engine, h["id"])
    assert still is not None and still["prompt"] == "P"


def test_update_missing_returns_none(engine, proj):
    assert update_handoff(engine, 99999, prompt="x") is None


def test_update_deleted_returns_none(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    delete_handoff(engine, h["id"])
    assert update_handoff(engine, h["id"], prompt="x") is None


def test_update_empty_prompt_raises(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    with pytest.raises(ValueError, match="Handoff prompt cannot be empty"):
        update_handoff(engine, h["id"], prompt="   ")


def test_update_nothing_to_change_raises(engine, proj):
    h = create_handoff(engine, "dct", prompt="P", created_by="A")
    with pytest.raises(ValueError, match="Nothing to update"):
        update_handoff(engine, h["id"])
