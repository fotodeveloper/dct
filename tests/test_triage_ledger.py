"""#909 — the triage findings ledger workflow (dct/workflows/triage.py).

Contract under test (spec #909 §3, owner decisions §8):
- record / decide / suggest are BATCH-ATOMIC: one invalid entry writes nothing;
  unknown keys are refused naming them; a field passed under both of its
  spellings (short template key and full column name) is refused.
- first_seen links a finding to the earliest live finding of the SAME project
  with an equal normalised fingerprint (case, whitespace, trailing
  punctuation); another project never links.
- the cycle is a gate: nothing is recorded, decided or suggested into a
  closed cycle; a close is refused while a finding is undecided ('discuss'
  counts as decided); record / decide / close serialise on the cycle row
  (proven with the K-64 / K-91 pattern of tests/test_gate_race.py).
- promotion creates one item per finding, only for a fix / verify / backlog
  decision (K-2), through the shared create_item_conn path (K-17 / K-20);
  reject / note is refused while the promoted item is open; `unreproduced`
  only on an explicit unconfirmed result (K-12).
- one cycle resolver (K-13), the CARD shape on input, decision origin
  (K-5), integer ranges (K-8), the fingerprint identity (K-9), the file
  filter (K-7) and the HTML-safe page constants (K-1).
- lock order project → cycle → finding → item; the races are proven with
  conftest.hold_row_and_race (pid-precise on PostgreSQL, K-16).
- the export uses the template's CARDS / CLOSED / SUGGESTED names exactly.
Expectations come from the spec and the rubric, not from reading the code.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
import sqlalchemy as sa
from conftest import hold_row_and_race

from dct.core import provenance
from dct.core.items import create_item, create_item_conn, get_item, resolve_item
from dct.core.projects import create_project, delete_project
from dct.db import (
    finding_options,
    findings,
    items,
    lock_cycle_row,
    lock_row,
    projects,
    triage_cycles,
)
from dct.workflows import triage as triage_module
from dct.workflows.triage import (
    close_triage,
    decide_findings,
    export_triage,
    finding_history,
    list_findings,
    normalize_fingerprint,
    open_triage,
    page_constants_json,
    promote_finding_to_item,
    record_findings,
    resolve_cycle,
    reviewer_precision,
    suggest_findings,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _setup(eng, slug="proj-a", code="rev-1"):
    create_project(eng, slug, slug.upper())
    return open_triage(eng, slug, code, "review of X", "claude-code")["cycle_id"]


def finding(**over):
    """A valid reviewer finding in the short (template) spelling."""
    base = {
        "title": "Orders of other customers are readable",
        "fp": "owner filter violated at order detail by a foreign pk",
        "S": 3, "O": 2, "E": 2, "K": "S", "origin": "diff", "type": "security",
        "area": "shop/views.py:42", "src": "codex",
        "ctx": ["Any logged-in user reads any order.", "Request shown."],
        "opts": [
            {"k": "a", "t": "Filter by owner", "why": "removes the cause", "root": True, "rec": True},
            {"k": "b", "t": "Hide the URL", "why": "obscurity", "root": False, "rec": False},
        ],
    }
    base.update(over)
    return base


def _count(eng, table):
    with eng.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def _row(eng, finding_id):
    with eng.connect() as conn:
        return conn.execute(sa.select(findings).where(findings.c.id == finding_id)).mappings().one()


def _recorded(eng, cycle_id, *batch):
    return record_findings(eng, cycle_id, list(batch))["recorded"]


# The decision's origin (K-5): who decided and where the owner's words are.
OWNER = {"decided_by": "owner", "decision_source": "owner chat 2026-10-13"}


def decide(eng, cycle, decisions, **kw):
    """decide_findings with the owner's stamp (K-5) — tests of the stamp
    itself call decide_findings directly."""
    return decide_findings(eng, cycle, decisions, **{**OWNER, **kw})


# ── open_triage ──────────────────────────────────────────────────────────────


class TestOpenTriage:
    def test_a_duplicate_live_code_is_refused_in_the_same_project_only(self, engine):
        _setup(engine)
        with pytest.raises(ValueError, match="already exists"):
            open_triage(engine, "proj-a", "rev-1", "again", "claude-code")
        create_project(engine, "proj-b", "B")
        assert open_triage(engine, "proj-b", "rev-1", "other", "codex")["code"] == "rev-1"

    def test_a_soft_deleted_cycle_frees_its_code(self, engine):
        cycle_id = _setup(engine)
        with engine.begin() as conn:
            conn.execute(triage_cycles.update().where(triage_cycles.c.id == cycle_id)
                         .values(deleted_at=datetime.now(UTC)))
        assert open_triage(engine, "proj-a", "rev-1", "reopened", "x")["cycle_id"] != cycle_id

    @pytest.mark.parametrize("field", ["code", "topic", "created_by"])
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_required_text_is_refused_when_blank(self, engine, field, value):
        create_project(engine, "proj-a", "A")
        args = {"code": "rev-1", "topic": "t", "created_by": "me", field: value}
        with pytest.raises(ValueError, match=field):
            open_triage(engine, "proj-a", **args)
        assert _count(engine, triage_cycles) == 0

    @pytest.mark.parametrize(("field", "limit"), [
        ("code", 100), ("topic", 200), ("created_by", 200), ("base_commit", 64), ("artifact_url", 500),
    ])
    def test_every_varchar_is_refused_over_its_width_never_truncated(self, engine, field, limit):
        create_project(engine, "proj-a", "A")
        args = {"code": "rev-1", "topic": "t", "created_by": "me"}
        args[field] = "x" * limit  # exactly at the limit: stored whole
        cycle = open_triage(engine, "proj-a", **args)
        with engine.connect() as conn:
            stored = conn.execute(sa.select(triage_cycles.c[field]).where(
                triage_cycles.c.id == cycle["cycle_id"])).scalar_one()
        assert stored == "x" * limit
        args["code"] = "rev-2"
        args[field] = "x" * (limit + 1)
        with pytest.raises(ValueError, match="never truncated"):
            open_triage(engine, "proj-a", **args)

    def test_an_unknown_project_is_refused(self, engine):
        with pytest.raises(ValueError, match="not found"):
            open_triage(engine, "nope", "rev-1", "t", "me")

    @pytest.mark.parametrize("version", [0, -1, True, "2", 32768])
    def test_rubric_version_must_fit_its_smallint_column(self, engine, version):
        """K-8: refused before SQL with the field named — SQLite would store
        32768, PostgreSQL would fail with a raw driver error."""
        create_project(engine, "proj-a", "A")
        with pytest.raises(ValueError, match="rubric_version must be an integer from 1 to 32767"):
            open_triage(engine, "proj-a", "rev-1", "t", "me", rubric_version=version)
        assert _count(engine, triage_cycles) == 0

    def test_the_highest_rubric_version_is_stored(self, engine):
        create_project(engine, "proj-a", "A")
        cycle_id = open_triage(engine, "proj-a", "rev-1", "t", "me", rubric_version=32767)["cycle_id"]
        with engine.connect() as conn:
            assert conn.execute(sa.select(triage_cycles.c.rubric_version).where(
                triage_cycles.c.id == cycle_id)).scalar_one() == 32767


# ── record_findings ──────────────────────────────────────────────────────────


class TestRecordValidation:
    @pytest.mark.parametrize(("bad", "match"), [
        ({"severity": "HIGH"}, "unknown keys: severity"),
        ({"S": 4}, "S \\(impact\\) must be 0-3"),
        ({"S": True}, "S \\(impact\\) must be 0-3"),
        ({"S": -1}, "S \\(impact\\) must be 0-3"),
        ({"O": 4}, "reachability"),
        ({"O": "unknown"}, "reachability"),
        ({"E": 2.0}, "E \\(evidence\\) must be 0-3"),
        ({"K": "s"}, "K \\(fix_cost\\)"),
        ({"origin": "new"}, "origin"),
        ({"type": "bug"}, "type must be one of"),
        ({"fp": "  "}, "fp \\(fingerprint\\) is required"),
        ({"fp": " ...!? "}, "empty after normalisation"),
        ({"title": ""}, "title is required"),
        ({"src": None}, "src \\(source\\) is required"),
        ({"ctx": [1, 2]}, "ctx must be"),
        ({"flags": {"conflict": "yes"}}, "conflict must be true or false"),
        ({"flags": {"severe": True}}, "unknown keys: severe"),
        ({"flags": []}, "expected an object"),
        ({"cls": "fix"}, "clsWhy"),
        ({"clsWhy": "because"}, "clsWhy without cls"),
        ({"cls": "urgent", "clsWhy": "x"}, "cls \\(class override\\)"),
        ({"seq": 0}, "seq must be an integer from 1 to 2147483647"),
        ({"seq": "1"}, "seq must be an integer from 1 to 2147483647"),
        ({"seq": True}, "seq must be an integer from 1 to 2147483647"),
        ({"seq": 2_147_483_648}, "seq must be an integer from 1 to 2147483647"),
        ({"origin": None}, "origin is required"),
        ({"type": None}, "type \\(finding_type\\) is required"),
        ({"opts": [{"k": "a", "t": "x", "rec": True}, {"k": "b", "t": "y", "rec": True}]},
         "more than one recommended"),
        ({"opts": [{"k": "a", "t": "x"}, {"k": "a", "t": "y"}]}, "duplicate option key 'a'"),
        ({"opts": [{"k": "a", "t": "x", "label": "?"}]}, "unknown keys: label"),
        ({"opts": [{"k": "a"}]}, "option text"),
        ({"opts": {"k": "a"}}, "opts must be a list"),
    ])
    def test_one_invalid_finding_rolls_the_whole_batch_back(self, engine, bad, match):
        cycle_id = _setup(engine)
        batch = [finding(), finding(fp="another fingerprint"), finding(**bad)]
        with pytest.raises(ValueError, match=match):
            record_findings(engine, cycle_id, batch)
        assert _count(engine, findings) == 0
        assert _count(engine, finding_options) == 0

    @pytest.mark.parametrize(("missing", "named"), [
        ("S", "S (impact)"), ("O", "O (reachability)"), ("E", "E (evidence)"),
        ("K", "K (fix_cost)"), ("fp", "fp (fingerprint)"), ("title", "title"),
        ("src", "src (source)"), ("origin", "origin"), ("type", "type (finding_type)"),
    ])
    def test_a_required_rubric_field_cannot_be_omitted(self, engine, missing, named):
        """K-4: origin and type are rubric §1 fields like S / O / E / K — the
        refusal names the missing one (on an old SQLite table the column is
        still nullable, so this check is the guard)."""
        cycle_id = _setup(engine)
        f = finding()
        del f[missing]
        with pytest.raises(ValueError, match=re.escape(f"findings[0]: {named} is required")):
            record_findings(engine, cycle_id, [f])
        assert _count(engine, findings) == 0

    def test_unknown_keys_are_named_all_at_once(self, engine):
        cycle_id = _setup(engine)
        with pytest.raises(ValueError, match="unknown keys: confidence, severity"):
            record_findings(engine, cycle_id, [finding(severity="HIGH", confidence=9)])

    @pytest.mark.parametrize(("short", "full"), [
        ("S", "impact"), ("O", "reachability"), ("E", "evidence"), ("K", "fix_cost"),
        ("type", "finding_type"), ("fp", "fingerprint"), ("src", "source"), ("ctx", "context"),
        ("opts", "options"),
    ])
    def test_both_spellings_of_one_field_are_refused(self, engine, short, full):
        cycle_id = _setup(engine)
        f = finding()
        f[full] = f[short]
        with pytest.raises(ValueError, match=f"{short} and {full} name the same field"):
            record_findings(engine, cycle_id, [f])

    def test_short_and_full_names_store_the_same_finding(self, engine):
        cycle_id = _setup(engine)
        full = {
            "title": "t", "fingerprint": "fp one", "impact": 2, "reachability": "?",
            "evidence": 1, "fix_cost": "M", "origin": "pre", "finding_type": "robustness",
            "area": "a.py:1", "source": "panel:security", "context": "one\n\ntwo",
            "flags": {"open_design": True}, "class_override": "backlog", "class_reason": "why",
            "options": [{"key": "a", "text": "x", "why": "w", "is_root": True, "is_recommended": True}],
        }
        short = {
            "title": "t", "fp": "fp one", "S": 2, "O": "?", "E": 1, "K": "M", "origin": "pre",
            "type": "robustness", "area": "a.py:1", "src": "panel:security", "ctx": ["one", "two"],
            "flags": {"openDesign": True}, "cls": "backlog", "clsWhy": "why",
            "opts": [{"k": "a", "t": "x", "why": "w", "root": True, "rec": True}],
        }
        ids = [r["finding_id"] for r in _recorded(engine, cycle_id, full, short)]
        cards = export_triage(engine, cycle_id)["cards"]
        first, second = ({k: v for k, v in c.items() if k != "id"} for c in cards)
        assert first == second
        a, b = _row(engine, ids[0]), _row(engine, ids[1])
        skip = {"id", "seq", "first_seen_id", "created_at", "updated_at"}
        assert {k: v for k, v in a.items() if k not in skip} == {
            k: v for k, v in b.items() if k not in skip}

    @pytest.mark.parametrize("value", [2, "2"])
    def test_levels_accept_an_int_or_its_digit_string(self, engine, value):
        cycle_id = _setup(engine)
        fid = _recorded(engine, cycle_id, finding(S=value, O=value, E=value))[0]["finding_id"]
        row = _row(engine, fid)
        assert (row["impact"], row["reachability"], row["evidence"]) == (2, "2", 2)

    @pytest.mark.parametrize("findings_in", [[], None, {"title": "x"}])
    def test_an_empty_or_non_list_batch_is_refused(self, engine, findings_in):
        cycle_id = _setup(engine)
        with pytest.raises(ValueError, match="non-empty list"):
            record_findings(engine, cycle_id, findings_in)

    def test_a_missing_cycle_is_refused(self, engine):
        with pytest.raises(ValueError, match="#999 not found"):
            record_findings(engine, 999, [finding()])

    @pytest.mark.parametrize(("key", "column", "limit"), [
        ("title", "title", 500), ("fp", "fingerprint", 500), ("area", "area", 500),
        ("src", "reviewer", 100),
    ])
    def test_every_free_text_varchar_is_refused_over_its_width(self, engine, key, column, limit):
        cycle_id = _setup(engine)
        fid = _recorded(engine, cycle_id, finding(**{key: "y" * limit}))[0]["finding_id"]
        assert _row(engine, fid)[column] == "y" * limit
        with pytest.raises(ValueError, match="never truncated"):
            record_findings(engine, cycle_id, [finding(**{key: "y" * (limit + 1)})])
        assert _count(engine, findings) == 1

    def test_an_option_key_wider_than_its_column_is_refused(self, engine):
        cycle_id = _setup(engine)
        with pytest.raises(ValueError, match="never truncated"):
            record_findings(engine, cycle_id, [finding(opts=[{"k": "abcde", "t": "x"}])])
        assert _count(engine, findings) == 0


class TestRecordSeqAndClass:
    def test_seq_auto_continues_after_the_highest_live_seq(self, engine):
        cycle_id = _setup(engine)
        assert [r["seq"] for r in _recorded(engine, cycle_id, finding(), finding())] == [1, 2]
        assert [r["seq"] for r in _recorded(engine, cycle_id, finding(seq=7), finding())] == [7, 8]

    def test_auto_seq_never_collides_with_an_explicit_seq_later_in_the_batch(self, engine):
        cycle_id = _setup(engine)
        assert [r["seq"] for r in _recorded(engine, cycle_id, finding(), finding(seq=1))] == [2, 1]

    def test_a_duplicate_seq_in_the_batch_is_refused(self, engine):
        cycle_id = _setup(engine)
        with pytest.raises(ValueError, match="duplicate seq in the batch: 3"):
            record_findings(engine, cycle_id, [finding(seq=3), finding(seq=3)])
        assert _count(engine, findings) == 0

    def test_a_seq_already_recorded_is_refused_with_nothing_written(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(seq=2))
        with pytest.raises(ValueError, match="already recorded .*K-2"):
            record_findings(engine, cycle_id, [finding(seq=5), finding(seq=2)])
        assert _count(engine, findings) == 1

    @pytest.mark.parametrize(("fields", "expected"), [
        ({"S": 3, "O": 2, "E": 2, "K": "S", "type": "security"}, "fix"),       # G1
        ({"S": 3, "O": 0, "E": 2, "K": "S", "type": "security"}, "reject"),    # G2
        ({"S": 2, "O": 1, "E": 1, "K": "S", "type": "correctness"}, "verify"),  # G3
        ({"S": 1, "O": 2, "E": 2, "K": "M", "type": "security"}, "backlog"),   # G6
        ({"S": 2, "O": 3, "E": 2, "K": "S", "type": "spec-gap"}, "decide"),    # G7
        ({"S": 3, "O": 3, "E": 3, "K": "S", "flags": {"violation": False}}, "reject"),
    ])
    def test_class_rec_is_the_rubric_class(self, engine, fields, expected):
        cycle_id = _setup(engine)
        fid = _recorded(engine, cycle_id, finding(**fields))[0]["finding_id"]
        assert _row(engine, fid)["class_rec"] == expected

    def test_an_override_keeps_the_derived_class_and_its_reason(self, engine):
        cycle_id = _setup(engine)
        fid = _recorded(engine, cycle_id, finding(cls="backlog", clsWhy="owner deferred"))[0]["finding_id"]
        row = _row(engine, fid)
        assert (row["class_rec"], row["class_override"], row["class_reason"]) == (
            "fix", "backlog", "owner deferred")
        assert list_findings(engine, "proj-a")["rows"][0][9] == "backlog"


class TestFirstSeen:
    @pytest.mark.parametrize("variant", [
        "Owner filter violated at order detail by a foreign pk.",
        "  OWNER   filter violated at\torder detail by a foreign pk  ",
        "owner filter violated at order detail\nby a foreign pk;",
    ])
    def test_a_normalised_fingerprint_links_to_the_first_finding(self, engine, variant):
        cycle_id = _setup(engine)
        first = _recorded(engine, cycle_id, finding())[0]
        cycle2 = open_triage(engine, "proj-a", "rev-2", "round 2", "me")["cycle_id"]
        again = _recorded(engine, cycle2, finding(fp=variant))[0]
        assert first["first_seen_id"] is None
        assert again["first_seen_id"] == first["finding_id"]

    def test_a_rephrasing_is_a_different_fingerprint(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding())
        other = _recorded(engine, cycle_id, finding(fp="owner check violated at order detail by a foreign pk"))
        assert other[0]["first_seen_id"] is None

    def test_the_same_fingerprint_in_another_project_never_links(self, engine):
        cycle_a = _setup(engine)
        _recorded(engine, cycle_a, finding())
        cycle_b = _setup(engine, slug="proj-b")
        assert _recorded(engine, cycle_b, finding())[0]["first_seen_id"] is None

    def test_a_duplicate_inside_one_batch_links_to_the_earlier_entry(self, engine):
        cycle_id = _setup(engine)
        a, b, c = _recorded(engine, cycle_id, finding(), finding(), finding())
        assert a["first_seen_id"] is None
        assert b["first_seen_id"] == c["first_seen_id"] == a["finding_id"]

    def test_a_soft_deleted_first_finding_is_not_the_first_seen(self, engine):
        cycle_id = _setup(engine)
        first, _second = _recorded(engine, cycle_id, finding(), finding(fp="other"))
        with engine.begin() as conn:
            conn.execute(findings.update().where(findings.c.id == first["finding_id"])
                         .values(deleted_at=datetime.now(UTC)))
        assert _recorded(engine, cycle_id, finding())[0]["first_seen_id"] is None

    @pytest.mark.parametrize(("raw", "norm"), [
        ("A  b\tC.", "a b c"),
        ("x…", "x"),
        ("end ?!", "end"),
        ("keeps (inner) punctuation: yes", "keeps (inner) punctuation: yes"),
    ])
    def test_normalize_fingerprint(self, raw, norm):
        assert normalize_fingerprint(raw) == norm


# ── decide / suggest / close ─────────────────────────────────────────────────


@pytest.fixture
def triaged(engine):
    """A cycle with K-1 (options a/b) and K-2 (no options)."""
    cycle_id = _setup(engine)
    _recorded(engine, cycle_id, finding(), finding(fp="second", opts=[]))
    return engine, cycle_id


class TestDecide:
    def test_keys_may_be_k_labels_or_seqs_and_undecided_is_reported(self, triaged):
        eng, cycle_id = triaged
        assert decide(eng, cycle_id, {"K-1": {"dec": "fix", "opt": "a"}}) == {
            "decided": 1, "undecided": [2]}
        assert decide(eng, cycle_id, {2: {"dec": "backlog"}}) == {
            "decided": 1, "undecided": []}

    @pytest.mark.parametrize(("decisions", "match"), [
        ({"K-1": {"dec": "fix"}, "K-9": {"dec": "note"}}, "no live finding K-9"),
        ({"K-1": {"dec": "approve"}}, "dec must be one of"),
        ({"K-1": {"opt": "a"}}, "dec must be one of"),
        ({"K-1": {"dec": "fix", "opt": "z"}}, "'z' is not an option of K-1"),
        ({"K-2": {"dec": "fix", "opt": "a"}}, "it has no options"),
        ({"K-1": {"dec": "fix", "variant": "a"}}, "unknown keys: variant"),
        ({"K-1": {"dec": "fix"}, 1: {"dec": "note"}}, "addressed twice"),
        ({"Q-1": {"dec": "fix"}}, "expected 'K-<n>'"),
        ({"K-1": "fix"}, "expected an object"),
        ({"K-1": {"dec": "fix", "note": 5}}, "note must be a string"),
    ])
    def test_one_bad_entry_decides_nothing(self, triaged, decisions, match):
        eng, cycle_id = triaged
        with pytest.raises(ValueError, match=match):
            decide(eng, cycle_id, {"K-2": {"dec": "note"}, **decisions})
        with eng.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).where(
                findings.c.decision.is_not(None))).scalar_one() == 0

    @pytest.mark.parametrize("decisions", [{}, None, [("K-1", {"dec": "fix"})]])
    def test_an_empty_or_non_object_map_is_refused(self, triaged, decisions):
        eng, cycle_id = triaged
        with pytest.raises(ValueError, match="non-empty object"):
            decide(eng, cycle_id, decisions)

    def test_an_empty_opt_means_no_variant(self, triaged):
        """K-10: the row asserted is K-1 itself (picked by seq), so a '' stored
        as a variant would fail the test."""
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "discuss", "opt": ""}})
        out = list_findings(eng, "proj-a", cycle="rev-1")
        seq, opt, dec = (out["cols"].index(c) for c in ("seq", "decision_opt", "decision"))
        k1 = next(r for r in out["rows"] if r[seq] == 1)
        assert (k1[dec], k1[opt]) == ("discuss", None)

    def test_a_decision_is_overwritten_while_open_and_the_note_kept_verbatim(
        self, triaged, monkeypatch,
    ):
        """Q1 + K-10: the overwrite refreshes decided_at to the SECOND
        decision's instant (two controlled clock instants — no sleep, no
        threshold) and its stamp, and keeps the new comment verbatim."""
        eng, cycle_id = triaged
        first = datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC)
        second = first + timedelta(hours=2, seconds=7)
        monkeypatch.setattr(triage_module, "_now", lambda: first)
        decide(eng, cycle_id, {"K-1": {"dec": "fix", "opt": "a", "note": "first"}})
        first_stored = _row(eng, 1)["decided_at"]
        monkeypatch.setattr(triage_module, "_now", lambda: second)
        note = "  Zgoda — ale <b>tylko</b> wariant (b)\n\ndruga linia  "
        decide(eng, cycle_id, {"K-1": {"dec": "backlog", "opt": "b", "note": note}},
               decided_by="owner (2nd)", decision_source="owner chat, second answer")
        row = _row(eng, 1)
        assert (row["decision"], row["decision_opt"], row["decision_note"]) == ("backlog", "b", note)
        # Exactly the second instant: the stored values differ by exactly
        # the clock's step (a naive column may hold the session's local time,
        # so the offset — not the wall clock — is compared).
        assert row["decided_at"] - first_stored == second - first
        assert (row["decided_by"], row["decision_source"]) == (
            "owner (2nd)", "owner chat, second answer")


class TestSuggest:
    def test_suggestions_never_touch_the_decision(self, triaged):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "reject", "note": "owner"}})
        assert suggest_findings(eng, cycle_id, "Codex", {
            "K-1": {"dec": "fix", "opt": "a", "note": "proposed"}}) == {"suggested": 1}
        row = _row(eng, 1)
        assert (row["decision"], row["decision_note"]) == ("reject", "owner")
        assert (row["suggested_decision"], row["suggested_opt"], row["suggested_by"]) == (
            "fix", "a", "Codex")

    def test_export_carries_suggested_with_the_latest_proposer(self, triaged):
        eng, cycle_id = triaged
        assert export_triage(eng, cycle_id)["suggested"] is None
        suggest_findings(eng, cycle_id, "Codex", {"K-1": {"dec": "fix", "opt": "a"}})
        suggest_findings(eng, cycle_id, "triage-redeploy", {"K-2": {"dec": "backlog", "note": "n"}})
        sug = export_triage(eng, cycle_id)["suggested"]
        assert sug["by"] == "triage-redeploy"
        assert sug["date"] == datetime.now(UTC).strftime("%Y-%m-%d")
        assert sug["answers"] == {
            "K-1": {"dec": "fix", "opt": "a", "note": ""},
            "K-2": {"dec": "backlog", "opt": "", "note": "n"},
        }

    @pytest.mark.parametrize("by", ["", "  ", None])
    def test_the_proposer_is_required(self, triaged, by):
        eng, cycle_id = triaged
        with pytest.raises(ValueError, match="by is required"):
            suggest_findings(eng, cycle_id, by, {"K-1": {"dec": "fix"}})

    def test_a_proposer_name_over_its_width_is_refused(self, triaged):
        eng, cycle_id = triaged
        with pytest.raises(ValueError, match="never truncated"):
            suggest_findings(eng, cycle_id, "c" * 101, {"K-1": {"dec": "fix"}})

    def test_one_bad_suggestion_writes_none(self, triaged):
        eng, cycle_id = triaged
        with pytest.raises(ValueError, match="no live finding K-3"):
            suggest_findings(eng, cycle_id, "Codex", {"K-1": {"dec": "fix"}, "K-3": {"dec": "fix"}})
        assert _row(eng, 1)["suggested_decision"] is None


class TestClose:
    def test_close_is_refused_while_a_finding_is_undecided(self, triaged):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        with pytest.raises(ValueError, match="1 finding\\(s\\) undecided — K-2"):
            close_triage(eng, cycle_id, source="chat")
        with eng.connect() as conn:
            assert conn.execute(sa.select(triage_cycles.c.closed_at)).scalar_one() is None

    def test_a_discuss_decision_lets_the_cycle_close(self, triaged):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {
            "K-1": {"dec": "fix", "opt": "a", "note": "go"},
            "K-2": {"dec": "discuss", "note": "left open by the owner"},
        })
        out = close_triage(eng, cycle_id, source="chat 2026-10-13",
                           notes=["Executed: K-1.", "K-2 stays open."])
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert out["closed"] == {
            "date": today, "source": "chat 2026-10-13", "rubric": 2,
            "notes": ["Executed: K-1.", "K-2 stays open."],
            "answers": {
                "K-1": {"dec": "fix", "opt": "a", "note": "go"},
                "K-2": {"dec": "discuss", "opt": "", "note": "left open by the owner"},
            },
        }
        assert export_triage(eng, cycle_id)["closed"] == out["closed"]
        json.dumps(out)  # JSON-native payload

    def test_after_close_record_decide_suggest_and_close_are_refused(self, triaged):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}, "K-2": {"dec": "note"}})
        close_triage(eng, cycle_id, source="chat")
        for call in (
            lambda: record_findings(eng, cycle_id, [finding()]),
            lambda: decide(eng, cycle_id, {"K-1": {"dec": "reject"}}),
            lambda: suggest_findings(eng, cycle_id, "Codex", {"K-1": {"dec": "reject"}}),
            lambda: close_triage(eng, cycle_id, source="again"),
        ):
            with pytest.raises(ValueError, match="is closed"):
                call()
        assert _row(eng, 1)["decision"] == "fix"
        assert _count(eng, findings) == 2

    def test_an_empty_cycle_closes_with_no_answers(self, engine):
        cycle_id = _setup(engine)
        closed = close_triage(engine, cycle_id, source="chat", notes="nothing found")["closed"]
        assert (closed["answers"], closed["notes"]) == ({}, ["nothing found"])

    @pytest.mark.parametrize("source", ["", "  ", None])
    def test_the_record_source_is_required(self, triaged, source):
        eng, cycle_id = triaged
        with pytest.raises(ValueError, match="source is required"):
            close_triage(eng, cycle_id, source=source)

    def test_a_source_over_its_width_is_refused(self, engine):
        cycle_id = _setup(engine)
        with pytest.raises(ValueError, match="source too long"):
            close_triage(engine, cycle_id, source="s" * 201)

    def test_a_missing_cycle_cannot_close(self, engine):
        with pytest.raises(ValueError, match="not found"):
            close_triage(engine, 404, source="chat")


# ── promote ──────────────────────────────────────────────────────────────────


class TestPromote:
    @pytest.mark.parametrize("dec", ["reject", "note", "discuss", None])
    def test_only_fix_verify_and_backlog_become_items(self, triaged, dec):
        """K-2 / K-12 / K-13: reject and note are ledger only; discuss and an
        undecided finding wait for the owner — none becomes an item."""
        eng, cycle_id = triaged
        if dec is not None:
            decide(eng, cycle_id, {"K-1": {"dec": dec}})
        state = "undecided" if dec is None else f"decided '{dec}'"
        with pytest.raises(ValueError, match=f"K-1\\) is {state} — only a finding decided "
                                             "fix / verify / backlog becomes an item"):
            promote_finding_to_item(eng, 1)
        assert _count(eng, items) == 0
        assert _row(eng, 1)["item_id"] is None

    def test_a_finding_is_promoted_once(self, triaged):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "fix", "opt": "a", "note": "do it"}})
        item = promote_finding_to_item(eng, 1)
        with pytest.raises(ValueError, match=f"already promoted to item #{item['id']}"):
            promote_finding_to_item(eng, 1)
        assert _count(eng, items) == 1
        assert _row(eng, 1)["item_id"] == item["id"]

    def test_the_item_carries_the_finding_and_the_rubric_outcome(self, triaged):
        """A 'verify' decision orders a check; it is not a failed one (K-12):
        no `unreproduced` tag without an explicit unconfirmed result."""
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "verify", "opt": "a", "note": "check"}})
        item = get_item(eng, promote_finding_to_item(eng, 1)["id"])
        assert item["title"] == "Orders of other customers are readable"
        assert item["item_type"] == "issue"
        assert item["tags"] == ["security"]
        assert item["priority"] == "P1"  # Verify of S3: Fix now once confirmed
        for part in ("rev-1 K-1", "owner filter violated at order detail",
                     "shop/views.py:42", "Any logged-in user", "(a): Filter by owner", "check"):
            assert part in item["description"]
        assert "NOT reproduced" not in item["description"]

    def test_an_unconfirmed_verify_is_tagged_with_its_conditions(self, triaged):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "verify"}})
        item = get_item(eng, promote_finding_to_item(
            eng, 1, unconfirmed=True,
            checked_conditions="foreign pk on /orders/<id> as user B, PG and SQLite",
            reopen_condition="any report of a foreign order shown")["id"])
        assert item["tags"] == ["security", "unreproduced"]
        assert "Verification attempted: NOT reproduced (unconfirmed)." in item["description"]
        assert ("Conditions checked: foreign pk on /orders/<id> as user B, PG and SQLite"
                in item["description"])
        assert "Reopen when: any report of a foreign order shown" in item["description"]

    @pytest.mark.parametrize(("dec", "kwargs", "match"), [
        ("verify", {"unconfirmed": True, "reopen_condition": "r"}, "checked_conditions"),
        ("verify", {"unconfirmed": True, "checked_conditions": "c"}, "reopen_condition"),
        ("verify", {"unconfirmed": True, "checked_conditions": " ", "reopen_condition": "r"},
         "checked_conditions"),
        ("verify", {"checked_conditions": "c", "reopen_condition": "r"},
         "pass them with unconfirmed=true"),
        ("verify", {"unconfirmed": "yes"}, "unconfirmed must be true or false"),
        ("fix", {"unconfirmed": True, "checked_conditions": "c", "reopen_condition": "r"},
         "applies to a 'verify' finding only"),
    ])
    def test_unreproduced_needs_an_explicit_complete_result(self, triaged, dec, kwargs, match):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": dec}})
        with pytest.raises(ValueError, match=match):
            promote_finding_to_item(eng, 1, **kwargs)
        assert _count(eng, items) == 0
        assert _row(eng, 1)["item_id"] is None

    def test_tags_come_from_the_dictionary_without_a_similarity_bypass(self, triaged):
        """K-12: no blanket force — a canonical name confusable with an
        existing tag is refused with nothing written; an existing exact
        name is reused, never duplicated."""
        from dct.core.tags import add_tag, list_tags

        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        add_tag(eng, "securty", force=True)
        with pytest.raises(ValueError, match="similar to existing 'securty'"):
            promote_finding_to_item(eng, 1)
        assert _count(eng, items) == 0
        assert _row(eng, 1)["item_id"] is None
        add_tag(eng, "security", force=True)
        assert get_item(eng, promote_finding_to_item(eng, 1)["id"])["tags"] == ["security"]
        assert sorted(t["name"] for t in list_tags(eng)) == ["security", "securty"]

    @pytest.mark.parametrize(("dec", "fields", "expected"), [
        ("fix", {}, "P1"),
        ("backlog", {"S": 2}, "P2"),
        ("backlog", {"S": 1}, "P3"),
        ("verify", {"S": 2, "O": 1, "E": 1}, "P2"),
        ("verify", {"S": 2, "O": 2, "E": 1}, "P1"),
    ])
    def test_priority_follows_the_rubric_outcome_table(self, engine, dec, fields, expected):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(type="correctness", **fields))
        decide(engine, cycle_id, {"K-1": {"dec": dec}})
        item = promote_finding_to_item(engine, 1)
        assert item["priority"] == expected
        assert item["tags"] == []

    def test_explicit_fields_win_and_the_project_is_the_cycles(self, engine):
        _setup(engine)  # proj-a
        cycle_b = _setup(engine, slug="proj-b")
        _recorded(engine, cycle_b, finding(fp="b-only"))
        decide(engine, cycle_b, {"K-1": {"dec": "backlog"}})
        item = promote_finding_to_item(engine, 1, item_type="improvement", priority="P3",
                                       component="shop")
        with engine.connect() as conn:
            slug = conn.execute(sa.text(
                "SELECT p.slug FROM items i JOIN projects p ON p.id = i.project_id "
                "WHERE i.id = :i"), {"i": item["id"]}).scalar_one()
        assert slug == "proj-b"
        assert (item["item_type"], item["priority"], item["component"]) == ("improvement", "P3", "shop")

    @pytest.mark.parametrize(("kwargs", "match"), [
        ({"item_type": "bug"}, "item_type"),
        ({"priority": "P0"}, "priority"),
        ({"component": "c" * 101}, "never truncated"),
    ])
    def test_bad_arguments_write_nothing(self, triaged, kwargs, match):
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        with pytest.raises(ValueError, match=match):
            promote_finding_to_item(eng, 1, **kwargs)
        assert _count(eng, items) == 0
        assert _row(eng, 1)["item_id"] is None

    @pytest.mark.parametrize(("finding_id", "match"), [
        (77, "#77 not found"),
        (2_147_483_648, "finding_id must be an integer from 1 to 2147483647"),
        (True, "finding_id must be an integer"),
    ])
    def test_a_missing_or_impossible_finding_is_refused(self, engine, finding_id, match):
        with pytest.raises(ValueError, match=match):
            promote_finding_to_item(engine, finding_id)

    def test_a_soft_deleted_project_refuses_like_create_item(self, triaged):
        """K-20: promotion goes through create_item_conn, so a deleted
        project refuses exactly as create_item does — no item, no link."""
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        assert delete_project(eng, "proj-a")
        with pytest.raises(ValueError) as direct:
            create_item(eng, "proj-a", "issue", "direct")
        with pytest.raises(ValueError) as promoted:
            promote_finding_to_item(eng, 1)
        assert str(promoted.value) == str(direct.value) == "Project 'proj-a' not found"
        assert _count(eng, items) == 0
        assert _row(eng, 1)["item_id"] is None


class TestRedecideAfterPromotion:
    """K-2: reject / note never leave active work behind — a promoted
    finding is re-decided reject / note only after its item is closed by
    the owner; the historic link stays."""

    def _promoted(self, eng, cycle_id):
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        return promote_finding_to_item(eng, 1)["id"]

    @pytest.mark.parametrize("dec", ["reject", "note"])
    def test_reject_or_note_is_refused_while_the_item_is_open(self, triaged, dec):
        eng, cycle_id = triaged
        item_id = self._promoted(eng, cycle_id)
        with pytest.raises(ValueError, match=f"promoted to item #{item_id}, still open — "
                                             f"close item #{item_id} first"):
            decide(eng, cycle_id, {"K-2": {"dec": "fix"}, "K-1": {"dec": dec}})
        row = _row(eng, 1)
        assert (row["decision"], row["item_id"]) == ("fix", item_id)
        assert _row(eng, 2)["decision"] is None  # the whole batch refused
        assert get_item(eng, item_id)["status"] == "open"  # never closed for the owner

    @pytest.mark.parametrize("closing", ["resolved", "wont_fix"])
    def test_after_the_owner_closes_the_item_the_link_stays_as_history(self, triaged, closing):
        eng, cycle_id = triaged
        item_id = self._promoted(eng, cycle_id)
        resolve_item(eng, item_id, status=closing)
        decide(eng, cycle_id, {"K-1": {"dec": "reject", "note": "not a bug after all"}})
        row = _row(eng, 1)
        assert (row["decision"], row["item_id"]) == ("reject", item_id)

    def test_other_re_decisions_of_a_promoted_finding_stay_allowed(self, triaged):
        eng, cycle_id = triaged
        item_id = self._promoted(eng, cycle_id)
        decide(eng, cycle_id, {"K-1": {"dec": "backlog"}})
        assert (_row(eng, 1)["decision"], _row(eng, 1)["item_id"]) == ("backlog", item_id)

    def test_a_rejected_finding_is_never_promoted(self, triaged):
        """The other order: decide reject first, then promote."""
        eng, cycle_id = triaged
        decide(eng, cycle_id, {"K-1": {"dec": "reject"}})
        with pytest.raises(ValueError, match="decided 'reject'"):
            promote_finding_to_item(eng, 1)
        assert _count(eng, items) == 0


# ── reads ────────────────────────────────────────────────────────────────────


class TestReads:
    def _two_cycles(self, eng):
        c1 = _setup(eng)
        _recorded(eng, c1,
                  finding(),
                  finding(fp="n plus one", S=2, O=3, E=2, K="M", type="perf",
                          area="shop/list.py:9 · 100%_done.py:1", src="panel:perf"))
        decide(eng, c1, {"K-1": {"dec": "fix"}, "K-2": {"dec": "reject"}})
        close_triage(eng, c1, source="chat")
        c2 = open_triage(eng, "proj-a", "rev-2", "round 2", "me")["cycle_id"]
        _recorded(eng, c2, finding(fp="Owner filter violated at order detail by a foreign pk."))
        decide(eng, c2, {"K-1": {"dec": "fix"}})
        return c1, c2

    def test_list_findings_brief_view_and_filters(self, engine):
        self._two_cycles(engine)
        out = list_findings(engine, "proj-a")
        assert out["cols"] == ["id", "cycle_code", "seq", "S", "O", "E", "K", "origin", "type",
                               "class", "decision", "decision_opt", "item_id", "first_seen_id",
                               "source", "title"]
        assert [(r[1], r[2]) for r in out["rows"]] == [("rev-2", 1), ("rev-1", 1), ("rev-1", 2)]
        assert len(list_findings(engine, "proj-a", cycle="rev-1")["rows"]) == 2
        assert len(list_findings(engine, "proj-a", fingerprint="OWNER filter violated at "
                                 "order detail by a foreign pk")["rows"]) == 2
        assert [r[1:3] for r in list_findings(engine, "proj-a", file="100%_")["rows"]] == [["rev-1", 2]]
        assert list_findings(engine, "proj-a", file="100%x")["rows"] == []
        assert [r[9] for r in list_findings(engine, "proj-a", cls="fix")["rows"]] == ["fix"] * 3
        assert len(list_findings(engine, "proj-a", decision="reject")["rows"]) == 1
        assert len(list_findings(engine, "proj-a", source="panel:perf")["rows"]) == 1
        assert len(list_findings(engine, "proj-a", limit=1)["rows"]) == 1
        assert len(list_findings(engine, "proj-a", limit=0)["rows"]) == 3
        json.dumps(out)

    @pytest.mark.parametrize(("kwargs", "match"), [
        ({"cls": "urgent"}, "cls"), ({"decision": "maybe"}, "decision"), ({"limit": -1}, "limit"),
    ])
    def test_list_findings_refuses_bad_filters(self, engine, kwargs, match):
        _setup(engine)
        with pytest.raises(ValueError, match=match):
            list_findings(engine, "proj-a", **kwargs)

    def test_finding_history_is_chronological_across_cycles(self, engine):
        self._two_cycles(engine)
        hist = finding_history(engine, "proj-a", "owner filter violated at order detail by a foreign pk")
        assert [(r[1], r[4]) for r in hist["rows"]] == [("rev-1", "fix"), ("rev-2", "fix")]
        json.dumps(hist)

    def test_finding_history_stays_in_its_project(self, engine):
        self._two_cycles(engine)
        cycle_b = _setup(engine, slug="proj-b")
        _recorded(engine, cycle_b, finding())
        assert len(finding_history(engine, "proj-b", finding()["fp"])["rows"]) == 1

    def test_reviewer_precision_groups_decided_findings(self, engine):
        _c1, c2 = self._two_cycles(engine)
        _recorded(engine, c2, finding(fp="undecided one"))
        other = _setup(engine, slug="proj-b")
        _recorded(engine, other, finding())
        decide(engine, other, {"K-1": {"dec": "reject"}})
        out = reviewer_precision(engine, "proj-a")
        assert out["cols"] == ["source", "decision", "n"]
        assert sorted(map(tuple, out["rows"])) == [("codex", "fix", 2), ("panel:perf", "reject", 1)]
        assert reviewer_precision(engine, "proj-a", since="all") == out
        assert reviewer_precision(engine, "proj-a", since="2999-01-01")["rows"] == []


# ── export ───────────────────────────────────────────────────────────────────

# Frozen from the template's CARDS example and comments (spec §3: names EXACT).
CARD_KEYS = {"id", "title", "area", "src", "S", "O", "E", "K", "origin", "type", "fp", "ctx", "opts"}
CARD_OPTIONAL = {"conflict", "violation", "dispute", "openDesign", "cls", "clsWhy"}
OPT_KEYS = {"k", "rec", "root", "t", "why"}


class TestExport:
    def test_cards_use_the_template_field_names(self, triaged):
        eng, cycle_id = triaged
        out = export_triage(eng, cycle_id)
        assert out["closed"] is None and out["suggested"] is None
        k1 = out["cards"][0]
        assert set(k1) == CARD_KEYS
        assert all(set(o) == OPT_KEYS for o in k1["opts"])
        assert k1 == {
            "id": "K-1", "title": "Orders of other customers are readable",
            "area": "shop/views.py:42", "src": "codex", "S": 3, "O": 2, "E": 2, "K": "S",
            "origin": "diff", "type": "security",
            "fp": "owner filter violated at order detail by a foreign pk",
            "ctx": ["Any logged-in user reads any order.", "Request shown."],
            "opts": [
                {"k": "a", "rec": True, "root": True, "t": "Filter by owner", "why": "removes the cause"},
                {"k": "b", "rec": False, "root": False, "t": "Hide the URL", "why": "obscurity"},
            ],
        }
        assert out["cycle"]["code"] == "rev-1" and out["cycle"]["project"] == "proj-a"
        json.dumps(out)

    def test_flags_override_and_unresolved_o_survive_the_export(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(
            O="?", area=None, ctx=None, opts=[{"k": "a", "t": "x"}],
            flags={"conflict": True, "violation": False, "dispute": True, "openDesign": True},
            cls="note", clsWhy="owner call"))
        card = export_triage(engine, cycle_id)["cards"][0]
        assert set(card) == CARD_KEYS | CARD_OPTIONAL
        assert (card["O"], card["area"], card["ctx"]) == ("?", "", [])
        assert card["opts"] == [{"k": "a", "rec": False, "root": False, "t": "x", "why": ""}]
        assert (card["conflict"], card["violation"], card["dispute"], card["openDesign"]) == (
            True, False, True, True)
        assert (card["cls"], card["clsWhy"]) == ("note", "owner call")

    def test_exported_cards_record_back_unchanged_into_an_identical_cycle(self, engine):
        """Bidirectional (K-13): what export produces, record accepts AS IS —
        id 'K-n' is the seq, the top-level flags are the flags."""
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(seq=1), finding(
            seq=5, fp="second", O="?", opts=[],
            flags={"conflict": True, "violation": False, "openDesign": True},
            cls="note", clsWhy="owner call"))
        cards = export_triage(engine, cycle_id)["cards"]
        c2 = open_triage(engine, "proj-a", "rev-copy", "copy", "me")["cycle_id"]
        recorded = record_findings(engine, c2, cards)["recorded"]
        assert [r["seq"] for r in recorded] == [1, 5]
        assert export_triage(engine, c2)["cards"] == cards
        assert all(_row(engine, r["finding_id"])["decision"] is None for r in recorded)

    @pytest.mark.parametrize(("extra", "match"), [
        ({"id": "K-3", "seq": 3}, "id and seq name the same field"),
        ({"id": "3"}, "id must be 'K-<n>'"),
        ({"id": 3}, "id must be 'K-<n>'"),
        ({"id": "K-0"}, "seq must be an integer from 1"),
        ({"conflict": True, "flags": {"conflict": False}}, "conflict given both"),
        ({"openDesign": True, "flags": {"open_design": True}}, "open_design given both"),
        ({"dispute": "yes"}, "dispute must be true or false"),
        ({"id": "K-3", "decision": "fix"}, "unknown keys: decision"),
    ])
    def test_the_card_shape_keeps_one_spelling_per_field(self, engine, extra, match):
        cycle_id = _setup(engine)
        with pytest.raises(ValueError, match=match):
            record_findings(engine, cycle_id, [finding(fp="ok"), finding(**extra)])
        assert _count(engine, findings) == 0

    def test_top_level_and_nested_flags_of_different_fields_combine(self, engine):
        cycle_id = _setup(engine)
        fid = _recorded(engine, cycle_id, finding(dispute=True, flags={"conflict": True}))[0]["finding_id"]
        row = _row(engine, fid)
        assert (row["dispute"], row["conflict"], row["violation"]) == (True, True, True)


class TestPageConstants:
    """K-1: the page's constants are embedded in an inline <script>."""

    HOSTILE = ("</script><script>alert(1)</script> <!-- & \u2028 \u2029 "
               "<b onmouseover=x>é</b> ]]> --> \\u003c")

    def test_the_text_survives_and_cannot_leave_the_script(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(
            title=self.HOSTILE, area=self.HOSTILE, ctx=[self.HOSTILE],
            opts=[{"k": "a", "t": self.HOSTILE, "why": self.HOSTILE}]))
        suggest_findings(engine, cycle_id, "Codex </script>", {
            "K-1": {"dec": "fix", "opt": "a", "note": self.HOSTILE}})
        decide(engine, cycle_id, {"K-1": {"dec": "fix", "note": self.HOSTILE}})
        close_triage(engine, cycle_id, source="chat </script><!--", notes=[self.HOSTILE])
        out = export_triage(engine, cycle_id)
        assert out["closed"] is not None and out["suggested"] is not None
        # The data stays the original text — no escape sequences in it.
        assert out["cards"][0]["title"] == self.HOSTILE
        for name, value in (("CARDS", out["cards"]), ("CLOSED", out["closed"]),
                            ("SUGGESTED", out["suggested"])):
            text = page_constants_json(value)
            assert json.loads(text) == value
            assert not set(text) & {"<", ">", "&", "\u2028", "\u2029"}, name
            page = f"<html><body><script>const {name} = {text};</script></body></html>"
            parser = _TagCounter()
            parser.feed(page)
            parser.close()
            assert parser.starts == ["html", "body", "script"], name
            assert parser.script_data == [f"const {name} = {text};"], name

    def test_non_ascii_stays_readable(self):
        assert page_constants_json({"t": "zażółć"}, indent=None) == '{"t": "zażółć"}'


class _TagCounter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.starts: list[str] = []
        self.script_data: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.starts.append(tag)
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_data.append(data)


# ── the open-cycle contract under concurrency ────────────────────────────────


def _hold_and_race(eng, probe, cycle_id, competitor, holder_write):
    """The holder locks the cycle row; the competitor is PROVEN blocked on
    it (pid-precise on PostgreSQL), then the holder writes and commits."""
    return hold_row_and_race(
        eng, probe, lambda conn: lock_cycle_row(conn, cycle_id), competitor, holder_write)


def _insert_finding(conn, cycle_id, seq, fp, **over):
    """A complete findings row written directly by a holder transaction."""
    values = {
        "cycle_id": cycle_id, "seq": seq, "fingerprint": fp,
        "fingerprint_norm": normalize_fingerprint(fp), "title": fp, "impact": 1,
        "reachability": "1", "evidence": 1, "fix_cost": "S", "origin": "diff",
        "finding_type": "correctness", "reviewer": "codex", "class_rec": "note",
    }
    values.update(over)
    return conn.execute(findings.insert().values(**values)).inserted_primary_key[0]


def _close_directly(cycle_id):
    def write(conn):
        conn.execute(triage_cycles.update().where(triage_cycles.c.id == cycle_id).values(
            closed_at=datetime.now(UTC), closed_source="holder"))
    return write


class TestCycleRace:
    @pytest.mark.parametrize("writer", ["record", "decide", "suggest"])
    def test_a_close_committed_while_a_writer_waits_refuses_the_writer(
        self, concurrent_engine, lock_probe, writer,
    ):
        eng = concurrent_engine
        cycle_id = _setup(eng)
        _recorded(eng, cycle_id, finding())
        competitor = {
            "record": lambda: record_findings(eng, cycle_id, [finding(fp="late")]),
            "decide": lambda: decide(eng, cycle_id, {"K-1": {"dec": "fix"}}),
            "suggest": lambda: suggest_findings(eng, cycle_id, "Codex", {"K-1": {"dec": "fix"}}),
        }[writer]

        outcome = _hold_and_race(eng, lock_probe, cycle_id, competitor, _close_directly(cycle_id))

        assert isinstance(outcome, ValueError), outcome
        assert "is closed" in str(outcome)
        row = _row(eng, 1)
        assert _count(eng, findings) == 1
        assert row["decision"] is None and row["suggested_decision"] is None

    def test_a_finding_committed_while_the_close_waits_refuses_the_close(
        self, concurrent_engine, lock_probe,
    ):
        eng = concurrent_engine
        cycle_id = _setup(eng)
        _recorded(eng, cycle_id, finding())
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})

        def insert_undecided(conn):
            _insert_finding(conn, cycle_id, 2, "late")

        outcome = _hold_and_race(
            eng, lock_probe, cycle_id,
            lambda: close_triage(eng, cycle_id, source="chat"), insert_undecided)

        assert isinstance(outcome, ValueError), outcome
        assert "undecided — K-2" in str(outcome)
        with eng.connect() as conn:
            assert conn.execute(sa.select(triage_cycles.c.closed_at)).scalar_one() is None


# ── K-5: the origin of a decision ────────────────────────────────────────────


class TestDecisionOrigin:
    def test_who_decided_and_where_it_is_recorded_land_with_decided_at(self, triaged):
        eng, cycle_id = triaged
        decide_findings(eng, cycle_id, {"K-1": {"dec": "fix"}, "K-2": {"dec": "note"}},
                        decided_by="owner", decision_source="owner paste 2026-10-13")
        for fid in (1, 2):
            row = _row(eng, fid)
            assert (row["decided_by"], row["decision_source"]) == (
                "owner", "owner paste 2026-10-13")
            assert row["decided_at"] is not None

    def test_a_suggestion_never_writes_a_decision_origin(self, triaged):
        eng, cycle_id = triaged
        suggest_findings(eng, cycle_id, "Codex", {"K-1": {"dec": "fix"}})
        row = _row(eng, 1)
        assert (row["decided_by"], row["decision_source"], row["decided_at"]) == (None, None, None)

    @pytest.mark.parametrize("field", ["decided_by", "decision_source"])
    @pytest.mark.parametrize("value", ["", "   ", None, 7])
    def test_both_are_required_on_every_decision(self, triaged, field, value):
        eng, cycle_id = triaged
        stamp = {**OWNER, field: value}
        with pytest.raises(ValueError, match=f"{field} is required"):
            decide_findings(eng, cycle_id, {"K-1": {"dec": "fix"}}, **stamp)
        assert _row(eng, 1)["decision"] is None

    @pytest.mark.parametrize("field", ["decided_by", "decision_source"])
    def test_a_value_over_its_width_is_refused_never_truncated(self, triaged, field):
        eng, cycle_id = triaged
        decide_findings(eng, cycle_id, {"K-1": {"dec": "fix"}}, **{**OWNER, field: "x" * 200})
        assert _row(eng, 1)[field] == "x" * 200
        with pytest.raises(ValueError, match=f"{field} too long: 201 chars, max 200"):
            decide_findings(eng, cycle_id, {"K-1": {"dec": "note"}}, **{**OWNER, field: "x" * 201})
        assert _row(eng, 1)["decision"] == "fix"


# ── K-23: the provenance of the decision's write ─────────────────────────────

CREATION = ("actor_type", "actor_client", "session_id", "source")
WRITER = ("decision_actor_type", "decision_actor_client", "decision_session_id",
          "decision_actor_source")
CLIENT_A = provenance.Provenance("agent", "codex", "sess-a", "mcp")
CLIENT_B = provenance.Provenance("agent", "claude-code", "sess-b", "mcp")
# Client C knows less: no session, no client — those must become NULL, never
# stay B's.
CLIENT_C = provenance.Provenance(None, None, None, "cli")


@pytest.fixture
def as_client():
    """Switch the entry-point provenance (the ContextVar the CLI / MCP set
    once) between calls; restored to the empty default afterwards."""
    yield provenance.set_provenance
    provenance.set_provenance(provenance.Provenance())


def _fields(eng, finding_id, names):
    row = _row(eng, finding_id)
    return tuple(row[n] for n in names)


class TestDecisionWriter:
    """The last decision's writer, per Q1 — distinct from the client that
    recorded the finding, refreshed whole on every overwrite, never touched
    by a suggestion; an audit record, not authentication."""

    def test_the_writer_of_each_decision_is_recorded_apart_from_the_recorder(
        self, engine, as_client,
    ):
        as_client(CLIENT_A)
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding())
        assert _fields(engine, 1, CREATION) == ("agent", "codex", "sess-a", "mcp")
        assert _fields(engine, 1, WRITER) == (None, None, None, None)

        as_client(CLIENT_B)
        decide(engine, cycle_id, {"K-1": {"dec": "fix"}})
        assert _fields(engine, 1, WRITER) == ("agent", "claude-code", "sess-b", "mcp")
        assert _fields(engine, 1, CREATION) == ("agent", "codex", "sess-a", "mcp")

        as_client(CLIENT_C)
        decide(engine, cycle_id, {"K-1": {"dec": "backlog"}})
        assert _fields(engine, 1, WRITER) == (None, None, None, "cli")
        assert _fields(engine, 1, CREATION) == ("agent", "codex", "sess-a", "mcp")

    def test_a_suggestion_changes_nothing_in_the_decision_writer(self, engine, as_client):
        as_client(CLIENT_A)
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(), finding(fp="second", opts=[]))
        as_client(CLIENT_B)
        decide(engine, cycle_id, {"K-1": {"dec": "fix"}})

        as_client(CLIENT_C)
        suggest_findings(engine, cycle_id, "Codex", {"K-1": {"dec": "note"}, "K-2": {"dec": "fix"}})

        assert _fields(engine, 1, WRITER) == ("agent", "claude-code", "sess-b", "mcp")
        assert _fields(engine, 2, WRITER) == (None, None, None, None)
        assert _fields(engine, 2, CREATION) == ("agent", "codex", "sess-a", "mcp")

    def test_a_refused_decision_leaves_the_previous_writer(self, engine, as_client):
        as_client(CLIENT_A)
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding())
        as_client(CLIENT_B)
        decide(engine, cycle_id, {"K-1": {"dec": "fix"}})

        as_client(CLIENT_C)
        with pytest.raises(ValueError, match="no live finding K-9"):
            decide(engine, cycle_id, {"K-1": {"dec": "note"}, "K-9": {"dec": "fix"}})

        assert _fields(engine, 1, WRITER) == ("agent", "claude-code", "sess-b", "mcp")


# ── K-7: the file filter ─────────────────────────────────────────────────────


class TestFileFilter:
    """One semantics on SQLite and PostgreSQL: ASCII letters match in any
    case, every other character exactly; % and _ are literal."""

    AREAS = ("Dct/Db.py:12", "src/100%_Done.py:1", "src/100xyDone.py:2", "docs/Été.md:3")

    def _areas(self, eng, needle):
        out = list_findings(eng, "proj-a", file=needle)
        title = out["cols"].index("title")
        return sorted(r[title] for r in out["rows"])

    @pytest.fixture
    def recorded(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, *(finding(fp=a, title=a, area=a) for a in self.AREAS))
        return engine

    @pytest.mark.parametrize(("needle", "expected"), [
        ("dct/db.py", ["Dct/Db.py:12"]),
        ("DCT/DB.PY", ["Dct/Db.py:12"]),
        ("dCt/dB", ["Dct/Db.py:12"]),
        ("100%_done", ["src/100%_Done.py:1"]),
        ("100%", ["src/100%_Done.py:1"]),
        ("0_d", []),
        ("%_", ["src/100%_Done.py:1"]),
        ("100__", []),
        ("done.py", ["src/100%_Done.py:1", "src/100xyDone.py:2"]),
        ("Été", ["docs/Été.md:3"]),
        ("ÉTé.MD", ["docs/Été.md:3"]),  # ASCII T and MD fold, É and é match exactly
        ("été", []),  # non-ASCII letters are not folded — on either backend
    ])
    def test_ascii_case_insensitive_with_literal_wildcards(self, recorded, needle, expected):
        assert self._areas(recorded, needle) == expected


# ── K-8: integer ranges ──────────────────────────────────────────────────────


class TestSeqRange:
    def test_the_highest_seq_is_stored(self, engine):
        cycle_id = _setup(engine)
        assert _recorded(engine, cycle_id, finding(seq=2_147_483_647))[0]["seq"] == 2_147_483_647

    def test_an_exhausted_auto_seq_refuses_the_whole_batch(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(seq=2_147_483_647))
        with pytest.raises(ValueError, match="seq exhausted in cycle rev-1"):
            record_findings(engine, cycle_id, [finding(fp="explicit", seq=5), finding(fp="auto")])
        assert _count(engine, findings) == 1
        assert _count(engine, finding_options) == 2  # the first finding's only


# ── K-9: the fingerprint identity ────────────────────────────────────────────


class TestFingerprintIdentity:
    @pytest.mark.parametrize("raw", [
        "… by Z. .", "a . . .", "X!?… ;", "İstanbul", "STRASSE", "Straße", "é",
        "  many   spaces  .  ", "ǅ", "Å ring", "a.́", "end :\t;\n.", "",
        "İ" * 3 + " .",
    ])
    def test_normalisation_is_idempotent(self, raw):
        once = normalize_fingerprint(raw)
        assert normalize_fingerprint(once) == once

    def test_punctuation_and_spaces_are_stripped_to_a_fixed_point(self):
        assert normalize_fingerprint("Owner check violated by Z. .") == "owner check violated by z"

    @pytest.mark.parametrize(("first", "again"), [
        ("guard violated by Z. .", "guard violated by Z"),
        ("café menu violated by X", "café menu violated by X"),  # NFC vs NFD
        ("Å unit violated by X", "Å unit violated by X"),  # Angstrom sign
    ])
    def test_equivalent_spellings_link_across_cycles(self, engine, first, again):
        cycle_id = _setup(engine)
        root = _recorded(engine, cycle_id, finding(fp=first))[0]
        cycle2 = open_triage(engine, "proj-a", "rev-2", "round 2", "me")["cycle_id"]
        assert _recorded(engine, cycle2, finding(fp=again))[0]["first_seen_id"] == root["finding_id"]

    @pytest.mark.parametrize(("first", "other"), [
        ("street violated by straße", "street violated by strasse"),  # no casefold
        ("a.b violated", "ab violated"),
        ("guard violated by Z1", "guard violated by Z2"),
    ])
    def test_different_fingerprints_never_link(self, engine, first, other):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(fp=first))
        assert _recorded(engine, cycle_id, finding(fp=other))[0]["first_seen_id"] is None

    def test_a_normal_form_wider_than_its_column_is_refused_never_cut(self, engine):
        """'İ' lowers to two characters: a 500-char fingerprint can outgrow
        the 500-char identity. Cutting it would link it to the finding
        with the same first 500 characters — a false recurrence."""
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(fp="x" * 499 + "i"))
        with pytest.raises(ValueError, match=re.escape(
                "fp (fingerprint) too long after normalisation: 501 chars, max 500 "
                "— refused, never truncated")):
            record_findings(engine, cycle_id, [finding(fp="ok"), finding(fp="x" * 499 + "İ")])
        assert _count(engine, findings) == 1

    def test_lookups_use_the_same_identity(self, engine):
        cycle_id = _setup(engine)
        _recorded(engine, cycle_id, finding(fp="café menu violated by X."))
        nfd = "CAFÉ menu violated by x . "
        assert len(finding_history(engine, "proj-a", nfd)["rows"]) == 1
        assert len(list_findings(engine, "proj-a", fingerprint=nfd)["rows"]) == 1


# ── K-13: one cycle resolver ─────────────────────────────────────────────────


class TestResolveCycle:
    @pytest.fixture
    def cycles(self, engine):
        """proj-a: #1 code '2', #2 code 'rev-x'; proj-b: #3 code '2'."""
        create_project(engine, "proj-a", "A")
        create_project(engine, "proj-b", "B")
        ids = [open_triage(engine, slug, code, "t", "me")["cycle_id"]
               for slug, code in (("proj-a", "2"), ("proj-a", "rev-x"), ("proj-b", "2"))]
        assert ids == [1, 2, 3]
        return engine

    def test_an_int_is_a_global_id(self, cycles):
        assert resolve_cycle(cycles, None, 3) == 3
        assert resolve_cycle(cycles, "proj-b", 3) == 3

    def test_a_string_is_a_code_of_the_project_never_an_id(self, cycles):
        assert resolve_cycle(cycles, "proj-a", "2") == 1
        assert resolve_cycle(cycles, "proj-b", "2") == 3
        with pytest.raises(ValueError, match="No triage cycle '3' in project 'proj-a'"):
            resolve_cycle(cycles, "proj-a", "3")

    @pytest.mark.parametrize(("project", "ref", "match"), [
        ("proj-a", 3, "#3 does not belong to project 'proj-a'"),
        (None, "rev-x", "needs its project"),
        ("proj-a", "", "cycle must be a cycle id"),
        ("proj-a", True, "cycle must be a cycle id"),
        ("proj-a", 2.0, "cycle must be a cycle id"),
        (None, 0, "cycle id must be an integer from 1 to 2147483647"),
        (None, 2_147_483_648, "cycle id must be an integer from 1 to 2147483647"),
        (None, 99, "Triage cycle #99 not found"),
        ("nope", "2", "not found"),
    ])
    def test_refusals(self, cycles, project, ref, match):
        with pytest.raises(ValueError, match=match):
            resolve_cycle(cycles, project, ref)

    def test_a_soft_deleted_cycle_is_not_resolved(self, cycles):
        with cycles.begin() as conn:
            conn.execute(triage_cycles.update().where(triage_cycles.c.id == 2)
                         .values(deleted_at=datetime.now(UTC)))
        with pytest.raises(ValueError, match="No triage cycle 'rev-x'"):
            resolve_cycle(cycles, "proj-a", "rev-x")
        with pytest.raises(ValueError, match="#2 not found"):
            resolve_cycle(cycles, None, 2)

    def test_every_cycle_function_takes_a_code_with_its_project(self, cycles):
        eng = cycles
        kw = {"project": "proj-a"}
        assert record_findings(eng, "rev-x", [finding()], **kw)["cycle_id"] == 2
        suggest_findings(eng, "rev-x", "Codex", {"K-1": {"dec": "fix"}}, **kw)
        assert decide(eng, "rev-x", {"K-1": {"dec": "fix"}}, **kw) == {
            "decided": 1, "undecided": []}
        assert export_triage(eng, "rev-x", **kw)["cycle"]["id"] == 2
        assert len(list_findings(eng, "proj-a", cycle="rev-x")["rows"]) == 1
        assert len(list_findings(eng, "proj-a", cycle=2)["rows"]) == 1
        assert close_triage(eng, "rev-x", source="chat", **kw)["cycle_id"] == 2

    def test_a_cycle_of_another_project_writes_nothing(self, cycles):
        eng = cycles
        for call in (
            lambda: record_findings(eng, 3, [finding()], project="proj-a"),
            lambda: decide(eng, 3, {"K-1": {"dec": "fix"}}, project="proj-a"),
            lambda: suggest_findings(eng, 3, "Codex", {"K-1": {"dec": "fix"}}, project="proj-a"),
            lambda: close_triage(eng, 3, source="chat", project="proj-a"),
            lambda: export_triage(eng, 3, project="proj-a"),
            lambda: list_findings(eng, "proj-a", cycle=3),
        ):
            with pytest.raises(ValueError, match="does not belong to project 'proj-a'"):
                call()
        assert _count(eng, findings) == 0
        with eng.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).where(
                triage_cycles.c.closed_at.is_not(None))).scalar_one() == 0


# ── K-6 / K-16 / K-2: the remaining races ────────────────────────────────────


def _lock_project(eng, slug):
    with eng.connect() as conn:
        pid = conn.execute(sa.select(projects.c.id).where(projects.c.slug == slug)).scalar_one()
    return lambda conn: lock_row(conn, projects, pid)


class TestProjectRace:
    def test_two_cycles_recording_one_new_fingerprint_link_the_second_to_the_first(
        self, concurrent_engine, lock_probe,
    ):
        """K-6: a recorder of cycle B waits on the project row a recorder of
        cycle A holds, then sees A's committed root — one root, one link."""
        eng = concurrent_engine
        cycle_a = _setup(eng)
        cycle_b = open_triage(eng, "proj-a", "rev-2", "parallel review", "codex")["cycle_id"]
        root = {}

        def record_root(conn):
            root["id"] = _insert_finding(conn, cycle_a, 1, "Shared guard violated by X.")

        outcome = hold_row_and_race(
            eng, lock_probe, _lock_project(eng, "proj-a"),
            lambda: record_findings(eng, cycle_b, [finding(fp="shared guard violated by x")]),
            record_root)

        assert not isinstance(outcome, Exception), outcome
        assert outcome["recorded"][0]["first_seen_id"] == root["id"]
        with eng.connect() as conn:
            roots = conn.execute(sa.select(sa.func.count()).where(
                findings.c.first_seen_id.is_(None))).scalar_one()
        assert roots == 1

    def test_a_recorder_waiting_on_the_seq_allocation_gets_fresh_unique_seqs(
        self, concurrent_engine, lock_probe,
    ):
        """K-16: auto seq is allocated under the project lock — a batch that
        waited behind a committed K-1 continues after it, whole."""
        eng = concurrent_engine
        cycle_id = _setup(eng)

        outcome = hold_row_and_race(
            eng, lock_probe, _lock_project(eng, "proj-a"),
            lambda: record_findings(eng, cycle_id, [finding(fp="one"), finding(fp="two")]),
            lambda conn: _insert_finding(conn, cycle_id, 1, "holder"))

        assert not isinstance(outcome, Exception), outcome
        assert [r["seq"] for r in outcome["recorded"]] == [2, 3]
        with eng.connect() as conn:
            seqs = list(conn.execute(sa.select(findings.c.seq).where(
                findings.c.cycle_id == cycle_id).order_by(findings.c.seq)).scalars())
        assert seqs == [1, 2, 3]


class TestPromotionRace:
    def _decided(self, eng):
        cycle_id = _setup(eng)
        _recorded(eng, cycle_id, finding())
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        return cycle_id

    @staticmethod
    def _promote_in(conn, made):
        """What a committed promotion leaves: an item and the link."""
        project_id = conn.execute(sa.select(projects.c.id)).scalar_one()
        made["item"] = create_item_conn(conn, project_id, "issue", "holder's item")["id"]
        conn.execute(findings.update().where(findings.c.id == 1).values(item_id=made["item"]))

    def test_two_promotions_of_one_finding_create_one_item(self, concurrent_engine, lock_probe):
        eng = concurrent_engine
        cycle_id = self._decided(eng)
        made = {}

        outcome = _hold_and_race(eng, lock_probe, cycle_id,
                                 lambda: promote_finding_to_item(eng, 1),
                                 lambda conn: self._promote_in(conn, made))

        assert isinstance(outcome, ValueError), outcome
        assert f"already promoted to item #{made['item']}" in str(outcome)
        assert _count(eng, items) == 1
        assert _row(eng, 1)["item_id"] == made["item"]

    def test_a_reject_waiting_on_a_promotion_is_refused(self, concurrent_engine, lock_probe):
        """K-2, promote first: the re-decision sees the committed open item."""
        eng = concurrent_engine
        cycle_id = self._decided(eng)
        made = {}

        outcome = _hold_and_race(eng, lock_probe, cycle_id,
                                 lambda: decide(eng, cycle_id, {"K-1": {"dec": "reject"}}),
                                 lambda conn: self._promote_in(conn, made))

        assert isinstance(outcome, ValueError), outcome
        assert f"close item #{made['item']} first" in str(outcome)
        assert (_row(eng, 1)["decision"], _row(eng, 1)["item_id"]) == ("fix", made["item"])

    def test_a_promotion_waiting_on_a_reject_is_refused(self, concurrent_engine, lock_probe):
        """K-2, reject first: the promotion sees the committed decision."""
        eng = concurrent_engine
        cycle_id = self._decided(eng)

        outcome = _hold_and_race(
            eng, lock_probe, cycle_id, lambda: promote_finding_to_item(eng, 1),
            lambda conn: conn.execute(findings.update().where(findings.c.id == 1)
                                      .values(decision="reject")))

        assert isinstance(outcome, ValueError), outcome
        assert "decided 'reject'" in str(outcome)
        assert _count(eng, items) == 0
        assert _row(eng, 1)["item_id"] is None


# ── K-21: implicit foreign-key locks against the row locks ───────────────────


def _pause_inside(monkeypatch, name, during):
    """Wrap triage_module.<name>: the workflow call reaches it holding its
    row locks, runs `during(conn)` on ITS connection, then continues."""
    real = getattr(triage_module, name)

    def paused(conn, *args, **kwargs):
        during(conn)
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(triage_module, name, paused)


class TestForeignKeyLocks:
    """K-21: lock_row is FOR NO KEY UPDATE on PostgreSQL, so the FOR KEY
    SHARE a foreign-key check takes on a referenced row never waits for it.
    Each test runs the REAL workflow calls: the holder is paused inside its
    transaction, after its row locks, while the competitor runs; both must
    then complete. The two promotion interleavings deadlocked with FOR
    UPDATE ('deadlock detected', or the competitor's lock timeout); the two
    re-decision interleavings are guards of the same property. No retry
    anywhere — an exception from either side fails the test."""

    def _decided(self, eng):
        cycle_id = _setup(eng)
        _recorded(eng, cycle_id, finding())
        decide(eng, cycle_id, {"K-1": {"dec": "fix"}})
        return cycle_id

    @staticmethod
    def _run(holder_call, during_factory):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            state = {"pool": pool}
            during_factory(state)
            try:
                held = holder_call()
            except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
                held = exc
            competed = state["future"].result(timeout=30)
        return held, competed

    def test_record_waits_on_the_cycle_a_promotion_holds_and_both_complete(
        self, concurrent_engine, lock_probe, monkeypatch,
    ):
        """record holds the project row and waits on the cycle the promotion
        holds; the promotion then inserts its item (KEY SHARE on the
        project). FOR UPDATE: a cycle of waits → deadlock."""
        eng = concurrent_engine
        cycle_id = self._decided(eng)

        def factory(state):
            def during(conn):
                state["future"] = state["pool"].submit(
                    lock_probe.capture, record_findings, eng, cycle_id, [finding(fp="late")])
                lock_probe.wait_until_row_blocked(conn)
            _pause_inside(monkeypatch, "create_item_conn", during)

        promoted, recorded = self._run(lambda: promote_finding_to_item(eng, 1), factory)

        assert not isinstance(promoted, Exception), promoted
        assert not isinstance(recorded, Exception), recorded
        assert _row(eng, 1)["item_id"] == promoted["id"]
        assert [r["seq"] for r in recorded["recorded"]] == [2]

    def test_record_linking_first_seen_to_a_finding_being_promoted_is_not_blocked(
        self, concurrent_engine, monkeypatch,
    ):
        """record in another cycle of the project links first_seen to the
        finding the promotion holds (KEY SHARE on that finding) while holding
        the project row the promotion's item INSERT references. FOR UPDATE:
        record waits on the finding, the promotion on the project →
        deadlock. FOR NO KEY UPDATE: record completes while the promotion
        still holds its locks."""
        eng = concurrent_engine
        if eng.dialect.name != "postgresql":
            pytest.skip("SQLite reserves the whole database: one writer at a time")
        self._decided(eng)
        other = open_triage(eng, "proj-a", "rev-2", "parallel review", "codex")["cycle_id"]

        def factory(state):
            def during(conn):
                state["future"] = state["pool"].submit(
                    record_findings, eng, other, [finding()])
                # Completes while this transaction still holds cycle + finding.
                state["done_while_held"] = state["future"].result(timeout=10)
            _pause_inside(monkeypatch, "create_item_conn", during)

        promoted, recorded = self._run(lambda: promote_finding_to_item(eng, 1), factory)

        assert not isinstance(promoted, Exception), promoted
        assert not isinstance(recorded, Exception), recorded
        assert recorded["recorded"][0]["first_seen_id"] == 1
        assert _row(eng, 1)["item_id"] == promoted["id"]

    def _promoted_and_closed(self, eng):
        cycle_id = self._decided(eng)
        item_id = promote_finding_to_item(eng, 1)["id"]
        resolve_item(eng, item_id)
        return cycle_id, item_id

    def test_record_waits_on_the_cycle_a_re_decision_of_a_promoted_finding_holds(
        self, concurrent_engine, lock_probe, monkeypatch,
    ):
        """decide 'reject' of a promoted (closed) finding holds the cycle and
        the item row, paused after its UPDATE; record of the same project
        holds the project row and waits on the cycle. Both complete. A guard,
        not a reproduction: decide locks no row the recorder holds, so this
        interleaving completed under FOR UPDATE as well."""
        eng = concurrent_engine
        cycle_id, item_id = self._promoted_and_closed(eng)

        def factory(state):
            def during(conn):
                state["future"] = state["pool"].submit(
                    lock_probe.capture, record_findings, eng, cycle_id, [finding(fp="late")])
                lock_probe.wait_until_row_blocked(conn)
            _pause_inside(monkeypatch, "_undecided", during)

        decided, recorded = self._run(
            lambda: decide(eng, cycle_id, {"K-1": {"dec": "reject"}}), factory)

        assert not isinstance(decided, Exception), decided
        assert not isinstance(recorded, Exception), recorded
        assert (_row(eng, 1)["decision"], _row(eng, 1)["item_id"]) == ("reject", item_id)
        assert [r["seq"] for r in recorded["recorded"]] == [2]

    def test_record_linking_first_seen_to_a_finding_being_re_decided_is_not_blocked(
        self, concurrent_engine, monkeypatch,
    ):
        """record in another cycle links first_seen to the promoted finding a
        re-decision holds (cycle + item locked, finding updated); it
        completes while the decision is still uncommitted. A guard, not a
        reproduction: the finding is held only by the UPDATE's own NO KEY
        UPDATE lock, so this completed under FOR UPDATE as well."""
        eng = concurrent_engine
        if eng.dialect.name != "postgresql":
            pytest.skip("SQLite reserves the whole database: one writer at a time")
        cycle_id, _item_id = self._promoted_and_closed(eng)
        other = open_triage(eng, "proj-a", "rev-2", "parallel review", "codex")["cycle_id"]

        def factory(state):
            def during(conn):
                state["future"] = state["pool"].submit(
                    record_findings, eng, other, [finding()])
                state["future"].result(timeout=10)
            _pause_inside(monkeypatch, "_undecided", during)

        decided, recorded = self._run(
            lambda: decide(eng, cycle_id, {"K-1": {"dec": "reject"}}), factory)

        assert not isinstance(decided, Exception), decided
        assert not isinstance(recorded, Exception), recorded
        assert recorded["recorded"][0]["first_seen_id"] == 1
        assert _row(eng, 1)["decision"] == "reject"
