"""MCP-layer contract tests for the G1 workflow ops (#413).

dct/workflows/session.py is covered by tests/test_workflows.py; what is tested
HERE is the thin MCP marshalling layer in dct/mcp/server.py — the string→typed
translations the calling model actually drives:
  (a) `include` CSV parsing (subset / 'none' sentinel / empty = all),
  (b) the STRICT `consume` enum (a typo must NEVER silently become 'never'),
  (c) the `item_ids` sentinel triple ('__keep__' / '' / '22,21,6'),
  (d) handoff_id=0 meaning "no explicit id",
  (e) the slim handoff_work ack (no prompt echo) vs the FULL picked-up prompt,
  (f) guards (markup, unresolvable project) firing BEFORE any DB access.

Tools are called directly (FastMCP's @mcp.tool() leaves the function callable),
with server._get_engine monkeypatched onto the in-memory fixture engine.
"""

import pytest

from dct.core.handoff import create_handoff as core_create_handoff
from dct.core.handoff import list_handoffs as core_list_handoffs
from dct.core.items import create_item as core_create_item
from dct.core.projects import create_project
from dct.core.sprints import get_current_sprint as core_get_current_sprint
from dct.core.sprints import list_items_in_sprint as core_list_items_in_sprint
from dct.core.sprints import set_current_sprint_items as core_set_current_sprint_items
from dct.mcp import server as S

WHO = "claude-test @ 2026-07-28T12:00Z"
LONG_PROMPT = "Continue the migration. " * 20  # > 200 chars: preview would truncate


# ── get_context_snapshot ──────────────────────────────────────────────────────


def test_snapshot_include_csv_selects_sections(srv):
    out = srv.get_context_snapshot(project="test", include="sprint,gates")
    assert out["sections"] == ["sprint", "gates"]  # canonical order echoed back
    assert "sprint" in out and "gates" in out
    for skipped in ("handoff", "decisions", "recent_changes"):
        assert skipped not in out  # omitted, never None
    assert "summary" in out


def test_snapshot_include_none_sentinel_yields_summary_only(srv):
    out = srv.get_context_snapshot(project="test", include="none")
    assert out["sections"] == []
    for section in ("sprint", "handoff", "gates", "decisions", "recent_changes"):
        assert section not in out
    assert out["summary"]["total_open_in_project"] == 0


def test_snapshot_empty_include_returns_all_sections(srv):
    out = srv.get_context_snapshot(project="test")
    assert out["sections"] == ["sprint", "handoff", "gates", "decisions", "recent_changes"]


def test_snapshot_unresolvable_project_errors(srv):
    out = srv.get_context_snapshot(project="zzz")
    assert set(out) == {"error"}


def test_snapshot_unknown_section_errors(srv):
    out = srv.get_context_snapshot(project="test", include="sprint,bogus")
    assert set(out) == {"error"}
    assert "bogus" in out["error"]


# ── pickup_work ───────────────────────────────────────────────────────────────


def test_pickup_auto_consumes_single_handoff_with_full_prompt(srv):
    eng = srv._get_engine()
    core_create_handoff(eng, "test", LONG_PROMPT, WHO)

    out = srv.pickup_work(project="test", consumed_by="picker")

    assert out["consumed"] is True
    assert out["picked_up"]["prompt"] == LONG_PROMPT  # FULL text, not a preview
    assert out["picked_up"]["consumed_by"] == "picker"
    # POST-consume view: the handoff section no longer advertises it
    assert out["handoff"]["latest"] is None
    assert out["handoff"]["pending_count"] == 0
    assert out["summary"]["handoffs_pending"] == 0
    assert core_list_handoffs(eng, "test", unconsumed_only=True) == []


def test_pickup_consume_enum_is_strict_and_touches_nothing(srv):
    eng = srv._get_engine()
    core_create_handoff(eng, "test", LONG_PROMPT, WHO)

    out = srv.pickup_work(project="test", consume="always", consumed_by="picker")

    assert set(out) == {"error"}
    for token in ("'' (auto)", "'true'", "'false'", "always"):
        assert token in out["error"]
    # the permissive tri-state parser would have read 'always' as False (a silent
    # pure read); the strict enum must leave the handoff untouched instead
    assert len(core_list_handoffs(eng, "test", unconsumed_only=True)) == 1


@pytest.mark.parametrize("consume", ["true", "false", ""])
def test_pickup_valid_consume_tokens_accepted(srv, consume):
    core_create_handoff(srv._get_engine(), "test", LONG_PROMPT, WHO)
    out = srv.pickup_work(project="test", consume=consume, consumed_by="picker")
    assert "error" not in out
    assert out["consumed"] is (consume != "false")


def test_pickup_blank_consumed_by_errors(srv):
    eng = srv._get_engine()
    core_create_handoff(eng, "test", LONG_PROMPT, WHO)

    out = srv.pickup_work(project="test")  # consume auto, consumed_by empty

    assert set(out) == {"error"}
    assert "consumed_by" in out["error"]
    assert len(core_list_handoffs(eng, "test", unconsumed_only=True)) == 1


def test_pickup_handoff_id_zero_means_no_explicit_id(srv):
    """0 is the 'unset' sentinel — it must never be looked up as an id, and the
    auto/newest selection paths must still work behind it."""
    eng = srv._get_engine()
    core_create_handoff(eng, "test", "older", WHO)
    newest = core_create_handoff(eng, "test", "newer", WHO)

    # two candidates → auto consumes NOTHING (the human picks)
    auto = srv.pickup_work(project="test", consumed_by="picker")
    assert auto["consumed"] is False and auto["picked_up"] is None
    assert auto["handoff"]["pending_count"] == 2

    # consume='true' with handoff_id=0 → newest match, no "handoff #0" lookup
    out = srv.pickup_work(project="test", consume="true", handoff_id=0, consumed_by="picker")
    assert out["consumed"] is True
    assert out["picked_up"]["id"] == newest["id"]


def test_pickup_explicit_handoff_id_consumes_that_row(srv):
    eng = srv._get_engine()
    older = core_create_handoff(eng, "test", "older", WHO)
    core_create_handoff(eng, "test", "newer", WHO)

    out = srv.pickup_work(project="test", handoff_id=older["id"], consumed_by="picker")

    assert out["picked_up"]["id"] == older["id"]
    assert out["handoff"]["pending_count"] == 1


def test_pickup_scope_filters_candidates(srv):
    eng = srv._get_engine()
    core_create_handoff(eng, "test", "a", WHO, scope="alpha")
    core_create_handoff(eng, "test", "b", WHO, scope="beta")

    out = srv.pickup_work(project="test", scope="beta", consumed_by="picker")

    assert out["consumed"] is True
    assert out["picked_up"]["scope"] == "beta"


def test_pickup_include_csv_and_unresolvable_project(srv):
    core_create_handoff(srv._get_engine(), "test", LONG_PROMPT, WHO)
    out = srv.pickup_work(project="test", include="handoff", consumed_by="picker")
    assert out["sections"] == ["handoff"]
    assert "sprint" not in out
    assert out["consumed"] is True  # consumption is independent of the sections

    assert set(srv.pickup_work(project="zzz", consumed_by="picker")) == {"error"}


def test_pickup_markup_guard_fires_before_db(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    out = srv.pickup_work(project="test", consumed_by="<invoke name=x> picker")
    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert calls == []


# ── handoff_work ──────────────────────────────────────────────────────────────


def test_handoff_work_ack_is_slim(srv):
    out = srv.handoff_work(
        prompt=LONG_PROMPT,
        created_by=WHO,
        project="test",
        scope="sprint-x",
        context_summary="lots of context",
    )
    assert set(out) == {"handoff_id", "project", "scope", "created_at", "sprint"}
    assert out["project"] == "test"
    assert out["scope"] == "sprint-x"
    assert out["created_at"] is not None
    assert out["sprint"] is None  # item_ids defaulted to '__keep__'
    for stripped in ("prompt", "context_summary", "handoff", "created_by"):
        assert stripped not in out
    assert LONG_PROMPT not in str(out)


@pytest.mark.parametrize(
    "field", ["prompt", "context_summary", "created_by", "scope"]
)
def test_handoff_work_markup_guard_fires_before_db(srv, monkeypatch, field):
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    kwargs = {"prompt": "clean", "created_by": WHO, "project": "test"}
    kwargs[field] = "<invoke name=create_handoff> leaked"
    out = srv.handoff_work(**kwargs)
    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert field in out["error"]
    assert calls == []  # guard fires before any DB access


def test_handoff_work_item_ids_keep_sentinel_creates_no_sprint(srv):
    eng = srv._get_engine()
    out = srv.handoff_work(prompt="p", created_by=WHO, project="test")
    assert out["sprint"] is None
    assert core_get_current_sprint(eng, "test") is None  # no auto-create


def test_handoff_work_empty_item_ids_rejected(srv):
    """K-27: '' and delimiter-only tokens error instead of wiping the sprint;
    membership and the handoff ledger stay untouched."""
    eng = srv._get_engine()
    item = core_create_item(eng, "test", "todo", "Carry over")
    core_set_current_sprint_items(eng, "test", [item["id"]])
    current = core_get_current_sprint(eng, "test")
    assert current is not None

    for bad in ("", ",", " , ,"):
        out = srv.handoff_work(prompt="p", created_by=WHO, project="test", item_ids=bad)
        assert set(out) == {"error"}
        assert "clear_sprint" in out["error"] or "empty list" in out["error"]

    members = core_list_items_in_sprint(eng, current["id"])
    assert [m["id"] for m in members] == [item["id"]]


def test_handoff_work_additive_params(srv):
    eng = srv._get_engine()
    a = core_create_item(eng, "test", "todo", "A")
    b = core_create_item(eng, "test", "todo", "B")
    core_set_current_sprint_items(eng, "test", [a["id"]])

    out = srv.handoff_work(
        prompt="p", created_by=WHO, project="test",
        add_item_ids=str(b["id"]), remove_item_ids=str(a["id"]),
    )
    assert out["sprint"]["items"] == [b["id"]]

    both = srv.handoff_work(
        prompt="p", created_by=WHO, project="test",
        item_ids=str(a["id"]), add_item_ids=str(b["id"]),
    )
    assert set(both) == {"error"} and "cannot be combined" in both["error"]


def test_handoff_work_item_ids_list_sets_membership(srv):
    eng = srv._get_engine()
    a = core_create_item(eng, "test", "todo", "A")
    b = core_create_item(eng, "test", "todo", "B")

    out = srv.handoff_work(
        prompt="p", created_by=WHO, project="test", item_ids=f"{a['id']}, {b['id']}"
    )

    assert out["sprint"]["open_size"] == 2
    assert sorted(out["sprint"]["items"]) == sorted([a["id"], b["id"]])
    assert out["handoff_id"] is not None


def test_handoff_work_unparsable_item_ids_errors_without_writing(srv):
    eng = srv._get_engine()
    out = srv.handoff_work(prompt="p", created_by=WHO, project="test", item_ids="5x,7")
    assert set(out) == {"error"}
    assert "comma-separated integers" in out["error"] and "5x,7" in out["error"]
    # nothing created: no sprint, no handoff
    assert core_get_current_sprint(eng, "test") is None
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []


def test_handoff_work_unknown_item_id_rolls_the_handoff_back(srv):
    eng = srv._get_engine()
    out = srv.handoff_work(prompt="p", created_by=WHO, project="test", item_ids="999999")
    assert set(out) == {"error"}
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []  # atomic


def test_handoff_work_unresolvable_project_errors(srv):
    out = srv.handoff_work(prompt="p", created_by=WHO, project="zzz")
    assert set(out) == {"error"}


def test_handoff_work_then_pickup_round_trip_across_projects(srv):
    """Producer/consumer round-trip: what handoff_work writes for one project is
    exactly what pickup_work hands back — and never leaks into a sibling project."""
    eng = srv._get_engine()
    create_project(eng, slug="proj-b", name="Project B", path="/tmp/proj-b")

    ack = srv.handoff_work(prompt=LONG_PROMPT, created_by=WHO, project="test", scope="s1")

    other = srv.pickup_work(project="proj-b", consumed_by="picker")
    assert other["consumed"] is False and other["handoff"]["pending_count"] == 0

    mine = srv.pickup_work(project="test", consumed_by="picker")
    assert mine["picked_up"]["id"] == ack["handoff_id"]
    assert mine["picked_up"]["prompt"] == LONG_PROMPT
    assert mine["picked_up"]["scope"] == "s1"


# ── MCP payloads are JSON-native (K-36) + capped (K-34) ───────────────────────


def test_snapshot_payload_is_json_serializable(srv):
    """The MCP boundary must hand the model plain JSON — a `datetime` deep in
    the payload would fail serialization at transport time, not here."""
    import json

    core_create_handoff(srv._get_engine(), "test", LONG_PROMPT, WHO)
    item = core_create_item(srv._get_engine(), "test", "feature", "A")
    core_set_current_sprint_items(srv._get_engine(), "test", [item["id"]])

    json.dumps(srv.get_context_snapshot(project="test"))
    json.dumps(srv.pickup_work(project="test", consume="true", consumed_by=WHO))


def test_snapshot_capped_tables_expose_truncated(srv):
    from dct.workflows.session import SNAPSHOT_CAPS

    for i in range(SNAPSHOT_CAPS["handoff_others"] + 5):
        core_create_handoff(srv._get_engine(), "test", f"h{i}", WHO)

    out = srv.get_context_snapshot(project="test")
    others = out["handoff"]["others"]
    assert len(others["rows"]) == SNAPSHOT_CAPS["handoff_others"]
    assert others["truncated"] is True
    assert out["summary"]["handoffs_pending"] == SNAPSHOT_CAPS["handoff_others"] + 5


def test_pickup_scope_matched_surfaces_through_mcp(srv):
    core_create_handoff(srv._get_engine(), "test", "other work", WHO, scope="other")
    out = srv.pickup_work(project="test", scope="g1", consumed_by=WHO)
    assert out["consumed"] is False
    assert out["handoff"]["scope_matched"] == 0     # nothing for MY workstream
    assert out["handoff"]["pending_count"] == 1     # but the project is not idle


def test_pickup_handoff_id_ignores_scope_through_mcp(srv):
    eng = srv._get_engine()
    target = core_create_handoff(eng, "test", "explicit", WHO, scope="other")
    leftover = core_create_handoff(eng, "test", "newest", WHO, scope="misc")

    out = srv.pickup_work(
        project="test", handoff_id=target["id"], scope="g1", consumed_by=WHO,
    )
    assert out["picked_up"]["id"] == target["id"]
    assert out["handoff"]["latest"]["id"] == leftover["id"]
    assert "scope_matched" not in out["handoff"]


def test_snapshot_include_none_with_sections_errors(srv):
    out = srv.get_context_snapshot(project="test", include="none,sprint")
    assert set(out) == {"error"}
    assert "'none' must be used alone" in out["error"]


# ── invalid `since` comes back as {"error"}, never a traceback ────────────────


@pytest.mark.parametrize("since", ["garbage", "7d", "yesterday", "2026-13-45"])
def test_snapshot_invalid_since_errors(srv, since):
    """The MCP boundary must translate the ValueError into the documented
    {"error": ...} shape — an exception here would surface as a transport
    failure the model cannot act on."""
    out = srv.get_context_snapshot(project="test", since=since)
    assert set(out) == {"error"}
    assert "Invalid `since`" in out["error"] and since in out["error"]


def test_pickup_invalid_since_errors_without_consuming(srv):
    """K-21 through the MCP surface: the consume is rolled back with the
    transaction, so the handoff is still pending afterwards."""
    eng = srv._get_engine()
    core_create_handoff(eng, "test", LONG_PROMPT, WHO)

    out = srv.pickup_work(project="test", consume="true", consumed_by=WHO, since="garbage")

    assert set(out) == {"error"}
    assert "Invalid `since`" in out["error"]
    assert len(core_list_handoffs(eng, "test", unconsumed_only=True)) == 1


# ── markup guard covers EVERY free-text field pickup_work accepts ─────────────


@pytest.mark.parametrize("field", ["consumed_by", "scope"])
def test_pickup_markup_guard_covers_every_free_text_field(srv, monkeypatch, field):
    """`scope` is model-authored free text just like consumed_by — literal
    tool-call markup in it must be refused BEFORE any DB access."""
    calls = []
    monkeypatch.setattr(S, "_get_engine", lambda: calls.append("engine"))
    kwargs = {"project": "test", "consumed_by": "picker"}
    kwargs[field] = "<invoke name=consume_handoff> leaked"

    out = srv.pickup_work(**kwargs)

    assert "error" in out and "Malformed MCP tool call" in out["error"]
    assert field in out["error"]
    assert calls == []


# ── the `sprint` selector through MCP ─────────────────────────────────────────


def test_handoff_work_sprint_selector_by_code_and_id(srv):
    from dct.core.sprints import create_sprint as core_create_sprint

    eng = srv._get_engine()
    a = core_create_item(eng, "test", "todo", "A")
    b = core_create_item(eng, "test", "todo", "B")
    s1 = core_create_sprint(eng, "test", "S1", code="s1", status="active")
    s2 = core_create_sprint(eng, "test", "S2", code="s2", status="active")

    by_code = srv.handoff_work(
        prompt="p", created_by=WHO, project="test",
        item_ids=str(a["id"]), sprint="s1",
    )
    assert by_code["sprint"]["sprint_id"] == s1["id"]
    assert by_code["sprint"]["code"] == "s1"

    by_id = srv.handoff_work(
        prompt="p", created_by=WHO, project="test",
        item_ids=str(b["id"]), sprint=str(s2["id"]),
    )
    assert by_id["sprint"]["sprint_id"] == s2["id"]
    assert [m["id"] for m in core_list_items_in_sprint(eng, s1["id"])] == [a["id"]]
    assert [m["id"] for m in core_list_items_in_sprint(eng, s2["id"])] == [b["id"]]


def test_handoff_work_sprint_selector_not_active_errors(srv):
    from dct.core.sprints import create_sprint as core_create_sprint

    eng = srv._get_engine()
    a = core_create_item(eng, "test", "todo", "A")
    core_create_sprint(eng, "test", "Planned", code="pl")  # planned, not active

    out = srv.handoff_work(
        prompt="p", created_by=WHO, project="test",
        item_ids=str(a["id"]), sprint="pl",
    )
    assert set(out) == {"error"} and "not an active sprint" in out["error"]
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []


def test_handoff_work_several_active_sprints_without_selector_errors(srv):
    from dct.core.sprints import create_sprint as core_create_sprint

    eng = srv._get_engine()
    a = core_create_item(eng, "test", "todo", "A")
    core_create_sprint(eng, "test", "S1", code="s1", status="active")
    core_create_sprint(eng, "test", "S2", code="s2", status="active")

    out = srv.handoff_work(prompt="p", created_by=WHO, project="test", item_ids=str(a["id"]))

    assert set(out) == {"error"}
    assert "2 active sprints" in out["error"]
    assert "'s1'" in out["error"] and "'s2'" in out["error"]  # candidates listed
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []


def test_handoff_work_sprint_selector_id_code_collision_errors(srv):
    """K-25: a sprint whose CODE is another sprint's ID must not be guessed at."""
    from dct.core.sprints import create_sprint as core_create_sprint

    eng = srv._get_engine()
    a = core_create_item(eng, "test", "todo", "A")
    s1 = core_create_sprint(eng, "test", "First", status="active")
    core_create_sprint(eng, "test", "Second", code=str(s1["id"]), status="active")

    out = srv.handoff_work(
        prompt="p", created_by=WHO, project="test",
        item_ids=str(a["id"]), sprint=str(s1["id"]),
    )
    assert set(out) == {"error"} and "ambiguous" in out["error"]
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []


def test_handoff_work_sprint_selector_without_membership_change_errors(srv):
    from dct.core.sprints import create_sprint as core_create_sprint

    eng = srv._get_engine()
    core_create_sprint(eng, "test", "S1", code="s1", status="active")

    out = srv.handoff_work(prompt="p", created_by=WHO, project="test", sprint="s1")

    assert set(out) == {"error"} and "no membership change" in out["error"]
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []


# ── K-1/K-26: a foreign item id is indistinguishable from a missing one ───────


def test_handoff_work_foreign_and_unknown_item_id_report_identically(srv):
    """No existence leak: `999999` (does not exist) and a real item belonging to
    ANOTHER project must produce the same message modulo the id — and neither
    may echo the foreign item's title."""
    eng = srv._get_engine()
    create_project(eng, slug="proj-b", name="Project B", path="/tmp/proj-b")
    foreign = core_create_item(eng, "proj-b", "todo", "SECRET foreign title")

    foreign_out = srv.handoff_work(
        prompt="p", created_by=WHO, project="test", item_ids=str(foreign["id"])
    )
    unknown_out = srv.handoff_work(
        prompt="p", created_by=WHO, project="test", item_ids="999999"
    )

    def normalize(msg, item_id):
        return msg.replace(str(item_id), "<ID>")

    assert set(foreign_out) == set(unknown_out) == {"error"}
    assert normalize(foreign_out["error"], foreign["id"]) == normalize(
        unknown_out["error"], 999999
    )
    assert "SECRET" not in foreign_out["error"]
    # Nothing written for either attempt, and proj-b is untouched.
    assert core_list_handoffs(eng, "test", unconsumed_only=False) == []
    assert core_get_current_sprint(eng, "proj-b") is None
