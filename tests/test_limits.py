"""Column-length contract (#792): refuse, never truncate — limits from the schema.

PostgreSQL (the primary backend) raises StringDataRightTruncation for a value
longer than a VARCHAR(N); SQLite silently stores it. `ensure_fits` makes the
contract dialect-independent and turns the refusal into a readable ValueError
BEFORE any statement runs, so the MCP layer never leaks driver text.
"""

import pytest
import sqlalchemy as sa

from dct.core.handoff import create_handoff, get_handoff
from dct.core.items import create_item, get_item, update_item
from dct.core.limits import ensure_fits
from dct.core.projects import create_project
from dct.db import items, session_handoffs

_meta = sa.MetaData()
SYNTH = sa.Table(
    "synth",
    _meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("code", sa.String(5)),
    sa.Column("body", sa.Text),
    sa.Column("free", sa.String),  # String without a length: unbounded
    sa.Column("count", sa.Integer),
)

DRIVER_LEAKS = ("SQL", "psycopg", "UPDATE", "INSERT", "StringDataRightTruncation")


# ── ensure_fits unit contract ───────────────────────────────────────────────


def test_exactly_at_limit_passes():
    ensure_fits(SYNTH, {"code": "abcde"})


def test_one_over_limit_refused_naming_label_and_limit():
    with pytest.raises(ValueError) as exc:
        ensure_fits(SYNTH, {"code": "abcdef"})
    msg = str(exc.value)
    assert msg == "code too long: 6 chars, max 5 — refused, never truncated"


def test_empty_string_passes():
    ensure_fits(SYNTH, {"code": ""})


@pytest.mark.parametrize("value", [None, 123456789, 1.5, b"abcdefgh", ["abcdefgh"]])
def test_non_str_values_ignored(value):
    ensure_fits(SYNTH, {"code": value})


def test_unknown_key_ignored():
    ensure_fits(SYNTH, {"not_a_column": "x" * 10_000})


@pytest.mark.parametrize("column", ["body", "free", "count"])
def test_columns_without_a_string_length_ignored(column):
    ensure_fits(SYNTH, {column: "x" * 10_000})


def test_labels_override_used():
    with pytest.raises(ValueError, match=r"^Short code too long: 6 chars, max 5"):
        ensure_fits(SYNTH, {"code": "abcdef"}, labels={"code": "Short code"})


def test_label_falls_back_to_key_when_labels_lack_it():
    with pytest.raises(ValueError, match=r"^code too long"):
        ensure_fits(SYNTH, {"code": "abcdef"}, labels={"other": "Other"})


def test_multibyte_characters_counted_as_characters():
    # 5 characters, 15+ UTF-8 bytes: fits a VARCHAR(5) (PG counts characters).
    ensure_fits(SYNTH, {"code": "żółćę"})
    ensure_fits(SYNTH, {"code": "🙂🙂🙂🙂🙂"})
    with pytest.raises(ValueError, match="6 chars, max 5"):
        ensure_fits(SYNTH, {"code": "żółćęż"})


def test_first_offending_value_reported_among_several():
    with pytest.raises(ValueError, match="code too long"):
        ensure_fits(SYNTH, {"body": "x" * 99, "code": "abcdef", "count": 3})


def test_empty_values_mapping_is_noop():
    ensure_fits(SYNTH, {})


# ── items: component / source_file / source_lines ───────────────────────────


@pytest.fixture
def project(engine):
    return create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")


@pytest.mark.parametrize("field", ["component", "source_file", "source_lines"])
def test_create_item_refuses_overlong_string_field(engine, project, field):
    limit = items.c[field].type.length
    with pytest.raises(ValueError, match=rf"{field} too long: {limit + 1} chars, max {limit}"):
        create_item(engine, "proj-a", "todo", "T", **{field: "x" * (limit + 1)})


@pytest.mark.parametrize("field", ["component", "source_file", "source_lines"])
def test_create_item_accepts_string_field_at_limit(engine, project, field):
    limit = items.c[field].type.length
    item = create_item(engine, "proj-a", "todo", "T", **{field: "x" * limit})
    assert get_item(engine, item["id"])[field] == "x" * limit


@pytest.mark.parametrize("field", ["component", "source_file", "source_lines"])
def test_update_item_refuses_overlong_string_field_and_writes_nothing(engine, project, field):
    limit = items.c[field].type.length
    item = create_item(engine, "proj-a", "todo", "T", **{field: "keep"})
    with pytest.raises(ValueError, match=rf"{field} too long: {limit + 1} chars, max {limit}"):
        update_item(engine, item["id"], **{field: "x" * (limit + 1)}, priority="P1")
    after = get_item(engine, item["id"])
    assert after[field] == "keep"
    assert after["priority"] == "P2"  # the whole update was refused


def test_item_title_message_keeps_its_label(engine, project):
    limit = items.c.title.type.length
    with pytest.raises(ValueError, match=rf"^Item title too long: {limit + 1} chars, max {limit}"):
        create_item(engine, "proj-a", "todo", "A" * (limit + 1))


# ── handoffs: MCP surface ───────────────────────────────────────────────────

CREATED_BY_MAX = session_handoffs.c.created_by.type.length
CONSUMED_BY_MAX = session_handoffs.c.consumed_by.type.length
SCOPE_MAX = session_handoffs.c.scope.type.length


def test_schema_limits_are_the_documented_ones():
    """The MCP docstrings state these numbers; a column change must update them."""
    assert (CREATED_BY_MAX, CONSUMED_BY_MAX, SCOPE_MAX) == (200, 200, 100)


def _assert_clean_refusal(out: dict, field: str, limit: int):
    assert set(out) == {"error"}, out
    err = out["error"]
    assert field in err and str(limit) in err, err
    assert "refused, never truncated" in err
    for leak in DRIVER_LEAKS:
        assert leak not in err, err


def _handoff(engine, **kw):
    return create_handoff(engine, "test", prompt="p", created_by="tester", **kw)


def _count_handoffs(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(session_handoffs)).scalar_one()


def test_mcp_consume_handoff_refuses_overlong_consumed_by(srv, engine):
    h = _handoff(engine)
    out = srv.consume_handoff(h["id"], consumed_by="c" * (CONSUMED_BY_MAX + 1))
    _assert_clean_refusal(out, "consumed_by", CONSUMED_BY_MAX)
    row = get_handoff(engine, h["id"])
    assert row["consumed_at"] is None and row["consumed_by"] is None


def test_mcp_consume_handoff_accepts_consumed_by_at_limit(srv, engine):
    h = _handoff(engine)
    out = srv.consume_handoff(h["id"], consumed_by="c" * CONSUMED_BY_MAX)
    assert "error" not in out
    assert get_handoff(engine, h["id"])["consumed_by"] == "c" * CONSUMED_BY_MAX


def test_mcp_create_handoff_refuses_overlong_created_by(srv, engine):
    out = srv.create_handoff(prompt="p", created_by="c" * (CREATED_BY_MAX + 1), project="test")
    _assert_clean_refusal(out, "created_by", CREATED_BY_MAX)
    assert _count_handoffs(engine) == 0


def test_mcp_create_handoff_accepts_created_by_at_limit(srv, engine):
    out = srv.create_handoff(prompt="p", created_by="c" * CREATED_BY_MAX, project="test")
    assert "error" not in out
    assert get_handoff(engine, out["id"])["created_by"] == "c" * CREATED_BY_MAX


def test_mcp_create_handoff_refuses_overlong_scope(srv, engine):
    out = srv.create_handoff(prompt="p", created_by="t", project="test", scope="s" * (SCOPE_MAX + 1))
    _assert_clean_refusal(out, "scope", SCOPE_MAX)
    assert _count_handoffs(engine) == 0


def test_mcp_handoff_work_refuses_overlong_created_by_and_rolls_back(srv, engine):
    item = create_item(engine, "test", "todo", "T")
    out = srv.handoff_work(
        prompt="p", created_by="c" * (CREATED_BY_MAX + 1), project="test",
        add_item_ids=str(item["id"]),
    )
    _assert_clean_refusal(out, "created_by", CREATED_BY_MAX)
    assert _count_handoffs(engine) == 0
    # The sprint write of the same transaction rolled back too.
    assert get_item(engine, item["id"])["in_current_sprint"] is False


def test_mcp_handoff_work_accepts_created_by_at_limit(srv, engine):
    out = srv.handoff_work(prompt="p", created_by="c" * CREATED_BY_MAX, project="test")
    assert "error" not in out
    assert get_handoff(engine, out["handoff_id"])["created_by"] == "c" * CREATED_BY_MAX


def test_mcp_handoff_work_refuses_overlong_scope(srv, engine):
    out = srv.handoff_work(prompt="p", created_by="t", project="test", scope="s" * (SCOPE_MAX + 1))
    _assert_clean_refusal(out, "scope", SCOPE_MAX)
    assert _count_handoffs(engine) == 0


def test_mcp_pickup_work_refuses_overlong_consumed_by(srv, engine):
    h = _handoff(engine)  # exactly one candidate → AUTO consume would fire
    out = srv.pickup_work(project="test", consumed_by="c" * (CONSUMED_BY_MAX + 1))
    _assert_clean_refusal(out, "consumed_by", CONSUMED_BY_MAX)
    assert get_handoff(engine, h["id"])["consumed_at"] is None


@pytest.mark.parametrize("n_candidates", [0, 2])
def test_mcp_pickup_work_refuses_overlong_consumed_by_without_single_candidate(
    srv, engine, n_candidates
):
    """The limit is part of the contract, not of the consume path: with 0 or
    several candidates nothing is consumed, yet the same argument must be
    refused as with exactly one — otherwise the answer depends on DB state."""
    for _ in range(n_candidates):
        _handoff(engine)
    out = srv.pickup_work(project="test", consumed_by="c" * (CONSUMED_BY_MAX + 1))
    _assert_clean_refusal(out, "consumed_by", CONSUMED_BY_MAX)


def test_mcp_pickup_work_accepts_consumed_by_at_limit(srv, engine):
    h = _handoff(engine)
    out = srv.pickup_work(project="test", consumed_by="c" * CONSUMED_BY_MAX)
    assert out.get("consumed") is True, out
    assert get_handoff(engine, h["id"])["consumed_by"] == "c" * CONSUMED_BY_MAX


def test_mcp_update_handoff_refuses_overlong_scope(srv, engine):
    h = _handoff(engine, scope="orig")
    out = srv.update_handoff(h["id"], scope="s" * (SCOPE_MAX + 1), prompt="new")
    _assert_clean_refusal(out, "scope", SCOPE_MAX)
    row = get_handoff(engine, h["id"])
    assert (row["scope"], row["prompt"]) == ("orig", "p")


def test_mcp_update_handoff_accepts_scope_at_limit(srv, engine):
    h = _handoff(engine, scope="orig")
    out = srv.update_handoff(h["id"], scope="s" * SCOPE_MAX)
    assert "error" not in out
    assert get_handoff(engine, h["id"])["scope"] == "s" * SCOPE_MAX
