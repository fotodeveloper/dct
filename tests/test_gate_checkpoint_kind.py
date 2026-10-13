"""K-65 (#917): add_checkpoint refuses a kind the column cannot hold.

The kind set is OPEN — any non-blank kind is accepted and every kind but
task / cc-task is a gate — so the only limits are a blank kind and the width
of the `item_checkpoints.kind` column. That width is read from the schema
(ensure_fits): a longer kind is refused with a readable error, never
truncated, on every backend (PostgreSQL would raise a driver error, SQLite
would store it). The checkpoint text column has no length limit.
"""

import asyncio
import re

import pytest

from dct.core.checkpoints import add_checkpoint, list_checkpoints
from dct.core.items import create_item
from dct.core.projects import create_project
from dct.db import item_checkpoints

KIND_MAX = item_checkpoints.c.kind.type.length


@pytest.fixture
def item(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    return create_item(engine, "proj-a", "feature", "Gated feature")


def test_the_kind_column_has_a_length():
    assert isinstance(KIND_MAX, int) and KIND_MAX > 0


@pytest.mark.parametrize("kind", ("", " ", "\t\n", "　"))
def test_a_blank_kind_is_refused_and_nothing_is_written(engine, item, kind):
    with pytest.raises(ValueError, match="kind cannot be empty"):
        add_checkpoint(engine, item["id"], "Step", kind=kind)
    assert list_checkpoints(engine, item["id"]) == []


@pytest.mark.parametrize("char", ("k", "ż", "門"))
def test_a_kind_at_the_column_width_is_stored_whole(engine, item, char):
    kind = char * KIND_MAX
    cp = add_checkpoint(engine, item["id"], "Step", kind=kind)
    assert cp["kind"] == kind


@pytest.mark.parametrize("char", ("k", "ż", "門"))
def test_a_kind_one_past_the_column_width_is_refused_never_truncated(engine, item, char):
    kind = char * (KIND_MAX + 1)
    with pytest.raises(ValueError) as exc:
        add_checkpoint(engine, item["id"], "Step", kind=kind)
    msg = str(exc.value)
    assert "kind" in msg.lower() and f"max {KIND_MAX}" in msg and "never truncated" in msg
    assert list_checkpoints(engine, item["id"]) == []


def test_a_custom_kind_is_accepted_no_whitelist(engine, item):
    assert add_checkpoint(engine, item["id"], "Legal sign-off", kind="legal")["kind"] == "legal"


def test_a_long_text_is_stored_whole(engine, item):
    text = "Step " + "x" * 20_000
    assert add_checkpoint(engine, item["id"], text)["text"] == text


def test_mcp_add_checkpoint_returns_the_refusal(srv):
    item = srv.create_item("feature", "F", project="test")
    result = srv.add_checkpoint(item["id"], "Step", kind="k" * (KIND_MAX + 1))
    assert set(result) == {"error"} and f"max {KIND_MAX}" in result["error"]
    assert "error" in srv.add_checkpoint(item["id"], "Step", kind=" ")


def test_mcp_description_names_every_refusal():
    from dct.mcp.server import mcp

    desc = {t.name: t.description for t in asyncio.run(mcp.list_tools())}["add_checkpoint"]
    returns = " ".join(desc.split("Returns:", 1)[1].split())
    assert re.search(r"empty text", returns), returns
    assert re.search(r"empty or whitespace-only kind", returns), returns
    assert re.search(rf"kind longer than {KIND_MAX} chars", returns), returns
    assert re.search(r"unknown item_id", returns), returns
