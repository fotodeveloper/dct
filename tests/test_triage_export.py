"""#909 — the triage export speaks the triage page template's language.

The page template (the owner's private triage-artifact.html, not in this
repo) documents its constants in comments; the field lists below are copied
from those comments and FROZEN here. The export must produce exactly these
names — a renamed or missing key makes the page render an empty chip or drop
a card silently, which no Python-side test of the ledger would notice.

Bidirectional: what the export produces must also be what record_findings
accepts (a card re-recorded into a fresh cycle exports identically), so the
producer and the consumer of the shape cannot drift apart.
"""

from __future__ import annotations

import json

import pytest

from dct.core.projects import create_project
from dct.workflows.triage import (
    close_triage,
    decide_findings,
    export_triage,
    open_triage,
    record_findings,
    suggest_findings,
)

# ── frozen from the template comments ────────────────────────────────────────
# CARDS: "One entry per K-n. Fields per rubric §1: S, O, E, K, origin, type, fp
# ..." + the example card's keys (id, title, area, src, ctx, opts).
CARD_REQUIRED = {"id", "title", "area", "src", "S", "O", "E", "K", "origin", "type",
                 "fp", "ctx", "opts"}
# "Optional §4 flags: conflict (rule 1), violation:false (rule 3), dispute
# (rule 4), openDesign (rule 7). Coordinator override: cls + clsWhy".
CARD_OPTIONAL = {"conflict", "violation", "dispute", "openDesign", "cls", "clsWhy"}
# Not a template key: dct's own (#930 §2a) — the canonical reviewer tokens,
# exported only when recorded; the template ignores unknown keys.
CARD_DCT_EXTRA = {"reviewers"}
# "opts: k = letter, rec = recommended variant, root = removes the cause" + t, why.
OPTION_KEYS = {"k", "rec", "root", "t", "why"}
# CLOSED: "{ date, source, rubric, notes: [...], answers: { 'K-1': { dec, opt, note } } }"
CLOSED_KEYS = {"date", "source", "rubric", "notes", "answers"}
ANSWER_KEYS = {"dec", "opt", "note"}
# SUGGESTED: "{ by, date, answers: { 'K-1': { dec, opt, note } } }"
SUGGESTED_KEYS = {"by", "date", "answers"}
# Not a template key: dct's own (#930 K-32) — the hash of the cards and
# proposals the page shows, which accept_suggestions(expected_digest=…) checks.
SUGGESTED_DCT_EXTRA = {"digest"}
DECISIONS = {"fix", "verify", "backlog", "reject", "note", "discuss"}


def card_input(**over):
    base = {
        "title": "Orders of other customers are readable",
        "fp": "owner filter violated at order detail by a foreign pk",
        "S": 3, "O": 2, "E": 2, "K": "S", "origin": "diff", "type": "security",
        "area": "shop/views.py:42 · shop/urls.py:7", "src": "codex",
        "ctx": ["First paragraph.", "Second paragraph — with ünïcode."],
        "opts": [
            {"k": "a", "t": "Filter by owner", "why": "removes the cause", "root": True, "rec": True},
            {"k": "b", "t": "Hide the URL", "why": "", "root": False, "rec": False},
        ],
    }
    base.update(over)
    return base


@pytest.fixture
def cycle(engine):
    create_project(engine, "proj-a", "A")
    return open_triage(engine, "proj-a", "rev-1", "review", "claude-code")["cycle_id"]


def test_a_plain_card_has_exactly_the_required_template_keys(engine, cycle):
    record_findings(engine, cycle, [card_input()])
    (card,) = export_triage(engine, cycle)["cards"]
    assert set(card) == CARD_REQUIRED
    assert all(set(o) == OPTION_KEYS for o in card["opts"])


def test_flags_and_override_use_only_the_template_optional_keys(engine, cycle):
    record_findings(engine, cycle, [card_input(
        flags={"conflict": True, "violation": False, "dispute": True, "openDesign": True},
        cls="backlog", clsWhy="owner already scheduled it",
    )])
    (card,) = export_triage(engine, cycle)["cards"]
    assert set(card) == CARD_REQUIRED | CARD_OPTIONAL
    assert card["violation"] is False and card["conflict"] is True
    assert (card["cls"], card["clsWhy"]) == ("backlog", "owner already scheduled it")


@pytest.mark.parametrize(("o_in", "o_out"), [(0, 0), ("3", 3), ("?", "?")])
def test_value_types_are_the_template_types(engine, cycle, o_in, o_out):
    """S / E numbers, O a number or '?', K a letter — classify() on the page
    compares numbers (`S >= 2`), so a string '2' would misclassify."""
    record_findings(engine, cycle, [card_input(O=o_in, S="2", E=1)])
    (card,) = export_triage(engine, cycle)["cards"]
    assert card["O"] == o_out and type(card["O"]) is type(o_out)
    assert card["S"] == 2 and isinstance(card["S"], int)
    assert card["E"] == 1 and isinstance(card["E"], int)
    assert card["K"] == "S"


def test_cards_follow_seq_and_carry_the_k_number(engine, cycle):
    record_findings(engine, cycle, [card_input(seq=3), card_input(fp="x", seq=1), card_input(fp="y")])
    assert [c["id"] for c in export_triage(engine, cycle)["cards"]] == ["K-1", "K-3", "K-4"]


def test_open_cycle_exports_null_closed_and_null_suggested(engine, cycle):
    record_findings(engine, cycle, [card_input()])
    out = export_triage(engine, cycle)
    assert out["closed"] is None and out["suggested"] is None


def test_closed_and_suggested_have_the_template_shape(engine, cycle):
    record_findings(engine, cycle, [card_input(), card_input(fp="two", opts=[])])
    suggest_findings(engine, cycle, "Codex", {"K-1": {"dec": "fix", "opt": "a", "note": "proposal"}})
    decide_findings(engine, cycle, {"K-1": {"dec": "fix", "opt": "a", "note": "Tak — (a), zrób."},
                                    "K-2": {"dec": "discuss"}},
                    decided_by="owner", decision_source="chat 2026-10-13")
    close_triage(engine, cycle, source="chat", notes=["Executed K-1.", "Nothing else approved."])
    out = export_triage(engine, cycle)

    closed = out["closed"]
    assert set(closed) == CLOSED_KEYS
    assert set(closed["answers"]) == {"K-1", "K-2"}
    assert all(set(a) == ANSWER_KEYS and a["dec"] in DECISIONS for a in closed["answers"].values())
    assert closed["answers"]["K-1"]["note"] == "Tak — (a), zrób."  # verbatim
    assert closed["answers"]["K-2"] == {"dec": "discuss", "opt": "", "note": ""}
    assert closed["notes"] == ["Executed K-1.", "Nothing else approved."]
    assert closed["rubric"] == 2 and len(closed["date"]) == 10

    sugg = out["suggested"]
    assert set(sugg) == SUGGESTED_KEYS | SUGGESTED_DCT_EXTRA
    assert sugg["by"] == "Codex" and set(sugg["answers"]) == {"K-1"}
    assert set(sugg["answers"]["K-1"]) == ANSWER_KEYS | {"by"}
    assert sugg["answers"]["K-1"]["by"] == "Codex"


def test_each_suggested_answer_names_its_own_proposer(engine, cycle):
    """#945: the page names the proposer per card — with two agents the
    top-level `by` (the latest) is not the author of every proposal."""
    record_findings(engine, cycle, [card_input(), card_input(fp="two")])
    suggest_findings(engine, cycle, "Codex (gpt-5.5)", {"K-1": {"dec": "fix", "opt": "a", "note": "x"}})
    suggest_findings(engine, cycle, "Claude", {"K-2": {"dec": "note", "opt": "", "note": "y"}})
    sugg = export_triage(engine, cycle)["suggested"]
    assert sugg["by"] == "Claude"
    assert {k: a["by"] for k, a in sugg["answers"].items()} == {"K-1": "Codex (gpt-5.5)", "K-2": "Claude"}


def test_the_digest_names_exactly_the_cards_and_proposals_shown(engine, cycle):
    """K-32: SUGGESTED.digest is a sha256 hex, the same for two exports of an
    unchanged cycle and different after anything the owner would read
    changes — a revision by the SAME author, a new card, an edited option."""
    record_findings(engine, cycle, [card_input()])
    suggest_findings(engine, cycle, "Codex", {"K-1": {"dec": "fix", "opt": "a", "note": "v1"}})
    first = export_triage(engine, cycle)["suggested"]["digest"]
    assert len(first) == 64 and int(first, 16) >= 0
    assert export_triage(engine, cycle)["suggested"]["digest"] == first  # deterministic

    seen = {first}
    suggest_findings(engine, cycle, "Codex", {"K-1": {"dec": "fix", "opt": "a", "note": "v2"}})
    revised = export_triage(engine, cycle)["suggested"]["digest"]
    assert revised not in seen
    seen.add(revised)
    record_findings(engine, cycle, [card_input(fp="late", title="Late card")])
    assert export_triage(engine, cycle)["suggested"]["digest"] not in seen

    # Another cycle with the same cards and proposals is another page.
    other = open_triage(engine, "proj-a", "rev-2", "review", "claude-code")["cycle_id"]
    record_findings(engine, other, [card_input()])
    suggest_findings(engine, other, "Codex", {"K-1": {"dec": "fix", "opt": "a", "note": "v1"}})
    assert export_triage(engine, other)["suggested"]["digest"] != first


def test_the_whole_export_is_json_native(engine, cycle):
    record_findings(engine, cycle, [card_input()])
    suggest_findings(engine, cycle, "Codex", {"K-1": {"dec": "verify"}})
    out = export_triage(engine, cycle)
    assert json.loads(json.dumps(out)) == out  # no datetime / tuple / Decimal leaks


def _card_to_record_input(card: dict) -> dict:
    """The page's CARD shape → record_findings' input (the reviewer §2 shape):
    id carries the seq, the §4 flags travel in `flags`."""
    out = {k: v for k, v in card.items()
           if k not in ("id", "conflict", "violation", "dispute", "openDesign")}
    out["seq"] = int(card["id"].removeprefix("K-"))
    flags = {k: card[k] for k in ("conflict", "violation", "dispute", "openDesign") if k in card}
    if flags:
        out["flags"] = flags
    return out


def test_round_trip_export_rerecord_export_is_identical(engine, cycle):
    record_findings(engine, cycle, [
        card_input(),
        card_input(fp="fp 2", title="Two", O="?", type="spec-gap", ctx="single paragraph",
                   flags={"violation": False, "openDesign": True}),
        card_input(fp="fp 3", title="Three", cls="note", clsWhy="known", opts=[], area=None),
    ])
    first = export_triage(engine, cycle)["cards"]

    other = open_triage(engine, "proj-a", "rev-2", "re-run", "claude-code")["cycle_id"]
    record_findings(engine, other, [_card_to_record_input(c) for c in first])
    assert export_triage(engine, other)["cards"] == first


def test_exported_cards_record_back_as_they_are(engine, cycle):
    """K-13: the CARD shape is an accepted input — no conversion step between
    the page and the ledger (id 'K-n' → seq, flags at the top level)."""
    record_findings(engine, cycle, [
        card_input(),
        card_input(fp="fp 2", title="Two", O="?", flags={"violation": False, "dispute": True}),
    ])
    first = export_triage(engine, cycle)["cards"]

    other = open_triage(engine, "proj-a", "rev-2", "re-run", "claude-code")["cycle_id"]
    record_findings(engine, other, first)
    assert export_triage(engine, other)["cards"] == first


def test_reviewer_tokens_ride_along_only_when_recorded_and_record_back(engine, cycle):
    """#930 §2a: a card gains `reviewers` (lowercased, recorded order) only
    when the finding has tokens — a plain card keeps exactly the template
    keys — and the exported card records back unchanged (K-13 round trip)."""
    record_findings(engine, cycle, [
        card_input(reviewers=["Codex", "panel:security"]),
        card_input(fp="fp 2", title="Two"),
    ])
    first = export_triage(engine, cycle)["cards"]
    assert set(first[0]) == CARD_REQUIRED | CARD_DCT_EXTRA
    assert first[0]["reviewers"] == ["codex", "panel:security"]
    assert set(first[1]) == CARD_REQUIRED

    other = open_triage(engine, "proj-a", "rev-2", "re-run", "claude-code")["cycle_id"]
    record_findings(engine, other, first)
    assert export_triage(engine, other)["cards"] == first
