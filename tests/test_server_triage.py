"""MCP-layer contract tests for the triage findings ledger tools (#909).

dct/workflows/triage.py is covered by tests/test_triage_ledger.py; what is
tested HERE is the thin MCP marshalling layer in dct/mcp/server.py — what the
calling model drives:
  (a) every refusal comes back as {"error": ...} and writes NOTHING,
  (b) the '' / 0 "not given" sentinels of the string / int parameters,
  (c) the tool-call markup guard reaching INTO the batch payloads (a mangled
      call can fold siblings into a nested option text as easily as into a
      top-level field),
  (d) the slim acks — every write names its target: cycle_id / finding_id
      and the project slug, read from the cycle, never from the CWD (K-11),
  (e) the ten tools are registered, deferred (no alwaysLoad) and named in the
      server instructions' TRIAGE LEDGER paragraph without typos,
  (f) the cycle given as cycle_id OR its code with project, one shape for
      every cycle-taking tool (K-13), and the decision's audit fields (K-5).

Tools are called directly (the SDK's @mcp.tool() returns the function
unchanged) with server._get_engine patched onto the fixture engine (`srv`).
"""

from __future__ import annotations

import asyncio
import re

import pytest

from dct.core.items import get_item
from dct.core.projects import create_project

TRIAGE_TOOLS = {
    "open_triage", "record_findings", "decide_findings", "suggest_findings",
    "close_triage", "list_findings", "finding_history", "reviewer_precision",
    "export_triage", "promote_finding_to_item",
}


def finding(**over):
    base = {
        "title": "Orders of other customers are readable",
        "fp": "owner filter violated at order detail by a foreign pk",
        "S": 3, "O": 2, "E": 2, "K": "S", "origin": "diff", "type": "security",
        "area": "shop/views.py:42", "src": "codex",
        "ctx": ["Any logged-in user reads any order."],
        "opts": [
            {"k": "a", "t": "Filter by owner", "why": "removes the cause", "root": True, "rec": True},
            {"k": "b", "t": "Hide the URL", "why": "obscurity"},
        ],
    }
    base.update(over)
    return base


# K-5: who decided and where the owner's words are — required on every decide.
OWNER = {"decided_by": "owner", "decision_source": "owner paste 2026-10-13"}


def _open(srv, code="rev-1", project="test"):
    ack = srv.open_triage(code=code, topic="review of X", created_by="claude-test",
                          project=project)
    assert "error" not in ack, ack
    return ack["cycle_id"]


def _rows(table):
    return [dict(zip(table["cols"], r, strict=True)) for r in table["rows"]]


# ── registration / instructions ──────────────────────────────────────────────


def _wire_tools(srv):
    return {t.name: t for t in asyncio.run(srv.mcp.list_tools())}


def test_all_ten_tools_are_registered_and_deferred(srv):
    tools = _wire_tools(srv)
    assert TRIAGE_TOOLS <= set(tools)
    for name in TRIAGE_TOOLS:
        meta = tools[name].model_dump(by_alias=True, exclude_none=True).get("_meta") or {}
        assert meta.get("anthropic/alwaysLoad") is not True, name
    assert not TRIAGE_TOOLS & srv._ALWAYS_LOAD_TOOLS


def test_batch_parameters_are_typed_json_structures_on_the_wire(srv):
    """The model fills the schema: `findings` must be an array of objects and
    `decisions` / `suggestions` objects — never a string to stuff JSON into."""
    tools = _wire_tools(srv)
    assert tools["record_findings"].input_schema["properties"]["findings"]["type"] == "array"
    assert tools["decide_findings"].input_schema["properties"]["decisions"]["type"] == "object"
    assert tools["suggest_findings"].input_schema["properties"]["suggestions"]["type"] == "object"


def test_instructions_paragraph_names_only_real_triage_tools(srv):
    text = srv._INSTRUCTIONS
    start = text.index("TRIAGE LEDGER")
    paragraph = text[start:text.index("\n\n", start)]
    named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", paragraph))
    assert named, paragraph
    assert named <= TRIAGE_TOOLS, sorted(named - TRIAGE_TOOLS)
    assert {"open_triage", "record_findings", "decide_findings", "suggest_findings",
            "close_triage", "export_triage"} <= named


# ── open_triage ──────────────────────────────────────────────────────────────


def test_open_triage_ack_and_empty_optionals_stored_as_null(srv):
    ack = srv.open_triage(code="rev-1", topic="t", created_by="me", project="test")
    assert set(ack) == {"cycle_id", "code", "project"}
    assert ack["project"] == "test"
    cycle = srv.export_triage(cycle_id=ack["cycle_id"])["cycle"]
    assert cycle["scope"] is None and cycle["base_commit"] is None
    assert cycle["artifact_url"] is None and cycle["rubric_version"] == 2


def test_open_triage_duplicate_code_is_an_error(srv):
    _open(srv)
    out = srv.open_triage(code="rev-1", topic="again", created_by="me", project="test")
    assert set(out) == {"error"} and "already exists" in out["error"]


def test_open_triage_markup_is_refused_before_any_write(srv):
    out = srv.open_triage(code="rev-1", topic='x <parameter name="scope">y', created_by="me",
                          project="test")
    assert set(out) == {"error"} and "markup" in out["error"]
    missing = srv.export_triage(cycle="rev-1", project="test")
    assert set(missing) == {"error"} and "No triage cycle 'rev-1'" in missing["error"]
    assert "error" not in srv.open_triage(code="rev-1", topic="t", created_by="me", project="test")


def test_open_triage_unknown_project_is_an_error(srv):
    out = srv.open_triage(code="rev-1", topic="t", created_by="me", project="zzz")
    assert set(out) == {"error"}


# ── record_findings ──────────────────────────────────────────────────────────


def test_record_findings_numbers_the_batch_and_links_recurrence(srv):
    c1 = _open(srv, "rev-1")
    out = srv.record_findings(cycle_id=c1, findings=[finding(), finding(fp="other fp", title="Other")])
    assert out["cycle_id"] == c1
    assert [r["seq"] for r in out["recorded"]] == [1, 2]
    assert [r["first_seen_id"] for r in out["recorded"]] == [None, None]

    c2 = _open(srv, "rev-2")
    again = srv.record_findings(cycle_id=c2, findings=[finding(fp="  Owner filter VIOLATED at order detail by a foreign pk.")])
    assert again["recorded"][0]["first_seen_id"] == out["recorded"][0]["finding_id"]


def test_record_findings_unknown_key_refuses_the_whole_batch(srv):
    cycle_id = _open(srv)
    out = srv.record_findings(cycle_id=cycle_id, findings=[finding(), finding(severity="HIGH")])
    assert set(out) == {"error"} and "severity" in out["error"]
    assert srv.list_findings(project="test")["rows"] == []


def test_record_findings_markup_nested_in_an_option_is_refused(srv):
    """The guard walks the nested payload and names the offending path."""
    cycle_id = _open(srv)
    bad = finding(opts=[{"k": "a", "t": "fix it</parameter><parameter name=\"x\">", "rec": True}])
    out = srv.record_findings(cycle_id=cycle_id, findings=[bad])
    assert set(out) == {"error"}
    assert "findings[0].opts[0].t" in out["error"]
    assert srv.list_findings(project="test")["rows"] == []


def test_record_findings_into_a_missing_cycle_is_an_error(srv):
    out = srv.record_findings(cycle_id=999, findings=[finding()])
    assert set(out) == {"error"} and "not found" in out["error"]


# ── suggest / decide / close ─────────────────────────────────────────────────


def test_suggestions_never_become_decisions(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding()])
    assert srv.suggest_findings(cycle_id=cycle_id, by="Codex", suggestions={"K-1": {"dec": "fix", "opt": "a"}}) == {
        "cycle_id": cycle_id, "project": "test", "suggested": 1}
    export = srv.export_triage(cycle_id=cycle_id)
    assert export["suggested"]["by"] == "Codex"
    assert export["suggested"]["answers"]["K-1"]["dec"] == "fix"
    assert _rows(srv.list_findings(project="test"))[0]["decision"] is None
    out = srv.close_triage(cycle_id=cycle_id, source="chat")
    assert set(out) == {"error"} and "K-1" in out["error"]


def test_decide_reports_the_undecided_and_refuses_an_unknown_option(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding(), finding(fp="fp two", title="Two")])
    bad = srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix", "opt": "z"}})
    assert set(bad) == {"error"} and "'z'" in bad["error"]
    assert _rows(srv.list_findings(project="test"))[0]["decision"] is None

    ok = srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix", "opt": "a", "note": "Tak, (a)."}})
    assert ok == {"cycle_id": cycle_id, "project": "test", "decided": 1, "undecided": [2]}


def test_decide_unknown_entry_key_is_an_error(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding()])
    out = srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix", "decision": "fix"}})
    assert set(out) == {"error"} and "decision" in out["error"]


def test_close_returns_the_closed_constant_and_locks_the_cycle(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding(), finding(fp="fp two", title="Two")])
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix", "opt": "a", "note": "ok"},
                                   "K-2": {"dec": "discuss", "note": "talk"}})
    out = srv.close_triage(cycle_id=cycle_id, source="chat 2026-10-13", notes=["Executed: K-1."])
    assert set(out) == {"cycle_id", "project", "closed_at", "closed"}
    assert (out["cycle_id"], out["project"]) == (cycle_id, "test")
    closed = out["closed"]
    assert closed["source"] == "chat 2026-10-13" and closed["notes"] == ["Executed: K-1."]
    assert closed["answers"]["K-2"] == {"dec": "discuss", "opt": "", "note": "talk"}

    late = srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "reject"}})
    assert set(late) == {"error"} and "closed" in late["error"]
    assert set(srv.record_findings(cycle_id=cycle_id, findings=[finding(fp="new")])) == {"error"}
    assert set(srv.suggest_findings(cycle_id=cycle_id, by="Codex", suggestions={"K-1": {"dec": "note"}})) == {"error"}
    assert set(srv.close_triage(cycle_id=cycle_id, source="again")) == {"error"}


def test_close_notes_omitted_means_none(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding()])
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "note"}})
    assert srv.close_triage(cycle_id=cycle_id, source="chat")["closed"]["notes"] == []


def test_close_markup_in_notes_is_refused(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding()])
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "note"}})
    out = srv.close_triage(cycle_id=cycle_id, source="chat", notes=["ok", "</invoke>"])
    assert set(out) == {"error"} and "notes[1]" in out["error"]
    assert srv.export_triage(cycle_id=cycle_id)["closed"] is None


# ── reads ────────────────────────────────────────────────────────────────────


def test_list_findings_filters_and_sentinels(srv):
    c1 = _open(srv, "rev-1")
    c2 = _open(srv, "rev-2")
    srv.record_findings(cycle_id=c1, findings=[finding()])
    srv.record_findings(cycle_id=c2, findings=[finding(fp="opinion fp", type="opinion", src="panel:style",
                                       area="docs/readme.md:1")])

    everything = srv.list_findings(project="test")
    assert len(everything["rows"]) == 2
    assert everything["cols"][:3] == ["id", "cycle_code", "seq"]
    assert [r["cycle_code"] for r in _rows(srv.list_findings(project="test", cycle="rev-1"))] == ["rev-1"]
    assert [r["cycle_code"] for r in _rows(srv.list_findings(project="test", cycle_id=c2))] == ["rev-2"]
    assert [r["class"] for r in _rows(srv.list_findings(project="test", cls="note"))] == ["note"]
    assert len(srv.list_findings(project="test", file="views.py")["rows"]) == 1
    assert len(srv.list_findings(project="test", source="panel:style")["rows"]) == 1
    assert len(srv.list_findings(project="test", limit=1)["rows"]) == 1


@pytest.mark.parametrize("kwargs", [
    {"cycle": "rev-1", "cycle_id": 1},
    {"cls": "urgent"},
    {"decision": "maybe"},
    {"limit": -1},
    {"project": "zzz"},
])
def test_list_findings_refusals(srv, kwargs):
    _open(srv)
    out = srv.list_findings(**{"project": "test", **kwargs})
    assert set(out) == {"error"}


def test_finding_history_spans_cycles_chronologically(srv):
    c1 = _open(srv, "rev-1")
    c2 = _open(srv, "rev-2")
    srv.record_findings(cycle_id=c1, findings=[finding()])
    srv.record_findings(cycle_id=c2, findings=[finding(fp="OWNER filter violated at order detail by a foreign pk")])
    hist = srv.finding_history("owner filter violated at order detail by a foreign pk",
                               project="test")
    assert [r["cycle_code"] for r in _rows(hist)] == ["rev-1", "rev-2"]
    assert set(srv.finding_history("", project="test")) == {"error"}


def test_reviewer_precision_counts_only_decided_findings(srv):
    cycle_id = _open(srv)
    srv.record_findings(cycle_id=cycle_id, findings=[finding(), finding(fp="b"), finding(fp="c", src="panel")])
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix"}, "K-2": {"dec": "reject"}})
    out = srv.reviewer_precision(project="test")
    assert out["cols"] == ["source", "decision", "n"]
    assert sorted(map(tuple, out["rows"])) == [("codex", "fix", 1), ("codex", "reject", 1)]
    assert set(srv.reviewer_precision(project="test", since="yesterday-ish")) == {"error"}


def test_export_missing_cycle_is_an_error(srv):
    assert set(srv.export_triage(cycle_id=12345)) == {"error"}


# ── promote_finding_to_item ──────────────────────────────────────────────────


def test_promote_ack_back_link_and_refusals(srv):
    cycle_id = _open(srv)
    rec = srv.record_findings(cycle_id=cycle_id, findings=[finding(), finding(fp="b", type="opinion")])["recorded"]
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix", "opt": "a"}, "K-2": {"dec": "reject"}})
    fid = rec[0]["finding_id"]

    ack = srv.promote_finding_to_item(fid)
    assert set(ack) == {"id", "project", "item_type", "priority", "status", "title",
                        "short_title", "tags", "finding_id"}
    assert ack["finding_id"] == fid and ack["item_type"] == "issue" and ack["project"] == "test"
    assert ack["priority"] == "P1" and ack["tags"] == ["security"]
    assert get_item(srv._get_engine(), ack["id"])["title"] == finding()["title"]
    assert _rows(srv.list_findings(project="test"))[0]["item_id"] == ack["id"]

    twice = srv.promote_finding_to_item(fid)
    assert set(twice) == {"error"} and f"#{ack['id']}" in twice["error"]
    rejected = srv.promote_finding_to_item(rec[1]["finding_id"])
    assert set(rejected) == {"error"} and "reject" in rejected["error"]


def test_promote_explicit_priority_and_invalid_enum(srv):
    cycle_id = _open(srv)
    fid = srv.record_findings(cycle_id=cycle_id, findings=[finding()])["recorded"][0]["finding_id"]
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": "fix"}})
    assert set(srv.promote_finding_to_item(fid, item_type="bug")) == {"error"}
    ack = srv.promote_finding_to_item(fid, item_type="todo", priority="P3", component="shop")
    assert (ack["item_type"], ack["priority"]) == ("todo", "P3")


def test_promote_missing_finding_is_an_error(srv):
    assert set(srv.promote_finding_to_item(4242)) == {"error"}


def test_cycles_are_project_scoped_for_reads(srv):
    create_project(srv._get_engine(), slug="other", name="Other", path="/tmp/other")
    c_other = _open(srv, "rev-1", project="other")
    srv.record_findings(cycle_id=c_other, findings=[finding()])
    assert srv.list_findings(project="test")["rows"] == []
    assert len(srv.list_findings(project="other")["rows"]) == 1


# ── decision provenance (K-5) ────────────────────────────────────────────────


def _finding_row(srv, finding_id):
    import sqlalchemy as sa

    from dct.db import findings

    with srv._get_engine().connect() as conn:
        return conn.execute(
            sa.select(findings).where(findings.c.id == finding_id)).mappings().one()


def test_decide_requires_who_decided_and_where_on_the_wire(srv):
    """The model fills the schema: both audit fields are required parameters,
    not optional ones it may silently leave out."""
    schema = _wire_tools(srv)["decide_findings"].input_schema
    assert {"decisions", "decided_by", "decision_source"} <= set(schema["required"])
    assert not {"cycle_id", "cycle", "project"} & set(schema["required"])


@pytest.mark.parametrize("override", [
    {"decided_by": ""},
    {"decision_source": "   "},
    {"decided_by": "x" * 201},
    {"decision_source": "y" * 201},
    {"decision_source": 'chat <parameter name="decided_by">owner'},
])
def test_decide_refuses_a_missing_or_bad_audit_field_and_writes_nothing(srv, override):
    cycle_id = _open(srv)
    fid = srv.record_findings(cycle_id=cycle_id, findings=[finding()])["recorded"][0]["finding_id"]
    out = srv.decide_findings(cycle_id=cycle_id, decisions={"K-1": {"dec": "fix"}},
                              **{**OWNER, **override})
    assert set(out) == {"error"}
    row = _finding_row(srv, fid)
    assert (row["decision"], row["decided_by"], row["decision_source"]) == (None, None, None)


def test_decide_stores_the_audit_fields_and_a_redecision_replaces_them(srv):
    cycle_id = _open(srv)
    fid = srv.record_findings(cycle_id=cycle_id, findings=[finding()])["recorded"][0]["finding_id"]
    srv.decide_findings(cycle_id=cycle_id, decisions={"K-1": {"dec": "backlog"}},
                        decided_by="owner", decision_source="chat 2026-10-13 10:00")
    srv.decide_findings(cycle_id=cycle_id, decisions={"K-1": {"dec": "fix", "opt": "a"}},
                        decided_by="owner", decision_source="answers.txt line 3")
    row = _finding_row(srv, fid)
    assert (row["decision"], row["decided_by"], row["decision_source"]) == (
        "fix", "owner", "answers.txt line 3")


def test_suggestions_carry_no_decision_provenance(srv):
    cycle_id = _open(srv)
    fid = srv.record_findings(cycle_id=cycle_id, findings=[finding()])["recorded"][0]["finding_id"]
    srv.suggest_findings(cycle_id=cycle_id, by="Codex", suggestions={"K-1": {"dec": "fix"}})
    row = _finding_row(srv, fid)
    assert (row["decision"], row["decided_by"], row["decision_source"]) == (None, None, None)


# ── cycle by id or code, expected project (K-13 / K-11) ──────────────────────


def _writes(srv, target: dict):
    """One call of every cycle-taking tool against `target` (cycle_id / cycle
    / project kwargs) — each must accept or refuse it the same way."""
    return {
        "record": lambda: srv.record_findings(findings=[finding(fp="via target")], **target),
        "decide": lambda: srv.decide_findings(decisions={"K-1": {"dec": "note"}}, **OWNER, **target),
        "suggest": lambda: srv.suggest_findings(by="Codex", suggestions={"K-1": {"dec": "note"}},
                                                **target),
        "close": lambda: srv.close_triage(source="chat", **target),
        "export": lambda: srv.export_triage(**target),
    }


def _ledger_state(srv, cycle_id):
    """Everything a refused call must leave untouched."""
    out = srv.export_triage(cycle_id=cycle_id)
    return out["cards"], out["closed"], out["suggested"], _rows(srv.list_findings(project="test"))


@pytest.mark.parametrize("target", [
    {"cycle_id": "ID", "cycle": "rev-1"},          # both
    {},                                            # neither
    {"cycle": "rev-1"},                            # a code without its project
    {"cycle": "rev-9", "project": "test"},         # an unknown code
    {"cycle_id": "ID", "project": "other"},        # an id of another project
    {"cycle": "rev-1", "project": "other"},        # the code exists only in 'test'
    {"cycle_id": -1},                              # out of range
    {"cycle_id": 2_147_483_648},
    {"cycle": "1", "project": "test"},             # a numeric code is a code, not id #1
], ids=["both", "neither", "code-no-project", "unknown-code", "id-other-project",
        "code-other-project", "negative-id", "id-past-int", "digit-code"])
@pytest.mark.parametrize("tool", ["record", "decide", "suggest", "close", "export"])
def test_every_cycle_tool_refuses_a_bad_target_and_writes_nothing(srv, target, tool):
    create_project(srv._get_engine(), slug="other", name="Other", path="/tmp/other")
    _open(srv, "rev-x", project="other")
    cycle_id = _open(srv, "rev-1")
    assert cycle_id == 2  # cycle #1 exists (in 'other'): "1" must not reach it
    srv.record_findings(cycle_id=cycle_id, findings=[finding()])
    before = _ledger_state(srv, cycle_id)
    target = {k: (cycle_id if v == "ID" else v) for k, v in target.items()}

    out = _writes(srv, target)[tool]()
    assert set(out) == {"error"}, out
    assert _ledger_state(srv, cycle_id) == before


@pytest.mark.parametrize("target", [
    {"cycle_id": "ID"},
    {"cycle_id": "ID", "project": "test"},
    {"cycle": "rev-1", "project": "test"},
], ids=["id", "id-and-project", "code-and-project"])
def test_every_cycle_tool_accepts_an_id_or_a_code_and_acks_the_target(srv, target):
    cycle_id = _open(srv, "rev-1")
    srv.record_findings(cycle_id=cycle_id, findings=[finding()])
    target = {k: (cycle_id if v == "ID" else v) for k, v in target.items()}
    calls = _writes(srv, target)

    for name in ("record", "suggest", "decide", "close"):
        ack = calls[name]()
        assert "error" not in ack, (name, ack)
        assert (ack["cycle_id"], ack["project"]) == (cycle_id, "test"), name
        if name == "record":  # the recorded K-2 needs a decision before the close
            srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-2": {"dec": "note"}})
    exported = calls["export"]()
    assert (exported["cycle"]["id"], exported["cycle"]["project"]) == (cycle_id, "test")
    assert exported["closed"]["answers"]["K-1"]["dec"] == "note"


def test_the_ack_names_the_cycles_project_not_the_servers(srv, monkeypatch, tmp_path):
    """K-11: without `project` the ack still names where the write went — the
    cycle's project, read from the cycle, whatever directory the server runs in."""
    create_project(srv._get_engine(), slug="other", name="Other", path=str(tmp_path))
    monkeypatch.chdir("/tmp")  # 'test' is registered at /tmp/test, 'other' elsewhere
    c_other = _open(srv, "rev-1", project="other")
    ack = srv.record_findings(cycle_id=c_other, findings=[finding()])
    assert (ack["cycle_id"], ack["project"]) == (c_other, "other")


def test_a_numeric_code_is_found_as_a_code(srv):
    first = _open(srv, "rev-1")
    coded = _open(srv, str(first))  # a cycle CODED with the other cycle's id
    srv.record_findings(cycle=str(first), project="test", findings=[finding()])
    assert len(srv.export_triage(cycle_id=coded)["cards"]) == 1
    assert srv.export_triage(cycle_id=first)["cards"] == []


def test_list_findings_refuses_an_unknown_cycle(srv):
    _open(srv)
    for kwargs in ({"cycle": "nope"}, {"cycle_id": 999}):
        out = srv.list_findings(project="test", **kwargs)
        assert set(out) == {"error"}, kwargs


# ── promote: expected project, explicit verification result (K-11 / K-12) ────


def _decided_finding(srv, dec="fix", **over):
    cycle_id = _open(srv)
    fid = srv.record_findings(cycle_id=cycle_id, findings=[finding(**over)])["recorded"][0][
        "finding_id"]
    srv.decide_findings(cycle_id=cycle_id, **OWNER, decisions={"K-1": {"dec": dec}})
    return fid


def test_promote_of_another_projects_finding_is_refused_with_no_item(srv):
    create_project(srv._get_engine(), slug="other", name="Other", path="/tmp/other")
    fid = _decided_finding(srv)
    out = srv.promote_finding_to_item(fid, project="other")
    assert set(out) == {"error"} and "other" in out["error"]
    assert _finding_row(srv, fid)["item_id"] is None
    ack = srv.promote_finding_to_item(fid, project="test")
    assert ack["project"] == "test" and _finding_row(srv, fid)["item_id"] == ack["id"]


def test_promote_undecided_is_refused(srv):
    cycle_id = _open(srv)
    fid = srv.record_findings(cycle_id=cycle_id, findings=[finding()])["recorded"][0]["finding_id"]
    out = srv.promote_finding_to_item(fid)
    assert set(out) == {"error"} and "undecided" in out["error"]
    assert _finding_row(srv, fid)["item_id"] is None


def test_promote_unconfirmed_verify_is_tagged_and_explained(srv):
    fid = _decided_finding(srv, dec="verify", type="correctness")
    ack = srv.promote_finding_to_item(
        fid, unconfirmed=True, checked_conditions="ran the race test 50x on PG",
        reopen_condition="a CI failure on the same assert")
    assert ack["tags"] == ["unreproduced"]
    description = get_item(srv._get_engine(), ack["id"])["description"]
    assert "ran the race test 50x on PG" in description
    assert "a CI failure on the same assert" in description


def test_promote_plain_verify_is_not_called_unreproduced(srv):
    """'verify' orders a check; it reports none — no tag without the result."""
    fid = _decided_finding(srv, dec="verify", type="correctness")
    assert srv.promote_finding_to_item(fid)["tags"] == []


@pytest.mark.parametrize("kwargs", [
    {"unconfirmed": True},                                         # texts missing
    {"unconfirmed": True, "checked_conditions": "x"},              # one text missing
    {"checked_conditions": "x", "reopen_condition": "y"},          # texts without the flag
], ids=["no-texts", "one-text", "texts-without-flag"])
def test_promote_unconfirmed_needs_both_texts_and_the_flag(srv, kwargs):
    fid = _decided_finding(srv, dec="verify", type="correctness")
    out = srv.promote_finding_to_item(fid, **kwargs)
    assert set(out) == {"error"}
    assert _finding_row(srv, fid)["item_id"] is None


def test_promote_unconfirmed_on_a_fix_finding_is_refused(srv):
    fid = _decided_finding(srv, dec="fix")
    out = srv.promote_finding_to_item(fid, unconfirmed=True, checked_conditions="x",
                                      reopen_condition="y")
    assert set(out) == {"error"} and "verify" in out["error"]
