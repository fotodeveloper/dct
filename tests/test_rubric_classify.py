"""#909 — rubric v2 §4 class rule (dct/workflows/rubric.py).

classify() is the one Python port of the triage page template's classify();
the class the ledger stores must be the class the owner's page shows. The
expectations below are NOT read from the code: they are the rubric's golden
set (review-rubric-cases.md §2, copied here — the companion file lives
outside the repo) plus one boundary case per §4 rule, each derived from the
rule text ("first matching rule wins").
"""

from __future__ import annotations

import itertools

import pytest

from dct.workflows.rubric import (
    CLASS_ORDER,
    CLASSES,
    DECISIONS,
    FINDING_TYPES,
    classify,
    priority,
)


def card(s, o, e, k="S", ftype="correctness", **flags):
    return {"S": s, "O": o, "E": e, "K": k, "type": ftype, **flags}


# Rubric golden set (review-rubric-cases.md §2): case → fields → class.
GOLDEN = [
    ("G1 IDOR on order detail, request shown", card(3, 2, 2, "S", "security"), "fix"),
    ("G2 as G1, staff-only access accepted (O0 shown)", card(3, 0, 2, "S", "security"), "reject"),
    ("G3 double coupon, interleaving asserted", card(2, 1, 1, "S"), "verify"),
    ("G3' interleaving reproduced (S2 O1 fails rule 6)", card(2, 1, 3, "S"), "quick"),
    ("G4 float cents drift, pre is eligible", card(1, 3, 2, "S"), "quick"),
    ("G5 N+1 on public listing", card(2, 3, 2, "M", "perf"), "fix"),
    ("G6 contact form without throttle — no security floor", card(1, 2, 2, "M", "security"), "backlog"),
    ("G7 contract silent on re-paying", card(2, 2, 2, "S", "spec-gap"), "decide"),
    ("G8 export drops archived items, proof shown", card(2, 3, 2, "S", "spec"), "fix"),
    ("G8' same claim, only 'I think the spec wanted'", card(2, 3, 1, "S", "spec"), "verify"),
    ("G9 prefer a dataclass", card(0, 3, 0, "S", "opinion"), "note"),
    ("G10 empty webhook secret, provisioning unchecked", card(3, "?", 1, "S", "security"), "verify"),
    ("G10 → secret always provisioned", card(3, 0, 2, "S", "security"), "reject"),
    ("G10 → unset in staging", card(3, 1, 2, "S", "security"), "fix"),
    ("G11 bulk endpoint over unsafe function (latent)", card(2, 2, 2, "M", "robustness"), "fix"),
    ("G12 reviewers dispute S3 vs S1", card(3, 2, 2, "S", dispute=True), "verify"),
]


@pytest.mark.parametrize(("case", "fields", "expected"), GOLDEN, ids=[g[0][:3].strip() for g in GOLDEN])
def test_golden_set(case, fields, expected):
    assert classify(fields) == expected, case


# One boundary per §4 rule: the case that just meets it and the neighbour that
# just misses it, so an off-by-one in a threshold changes the class.
BOUNDARIES = [
    # rule 1: a conflict decides even a finding that would be Fix now
    (card(3, 3, 3, conflict=True), "decide"),
    # rule 1 before rule 2: spec-gap wins over everything
    (card(0, 0, 0, "L", "spec-gap"), "decide"),
    # rule 2 before rule 3: an opinion with O0 is still a note
    (card(3, 0, 3, "S", "opinion"), "note"),
    # rule 3: no violation after reading rejects a Fix-now field set
    (card(3, 3, 3, violation=False), "reject"),
    # rule 3 before rule 4: O0 rejects even a disputed finding
    (card(3, 0, 3, dispute=True), "reject"),
    # rule 5: S2 with E1 verifies; S1 with E1 does not
    (card(2, 3, 1), "verify"),
    (card(1, 3, 1), "note"),
    # rule 5: O? verifies even with E3
    (card(2, "?", 3), "verify"),
    # rule 6: S3 needs O≥1, S2 needs O≥2
    (card(3, 1, 2, "L"), "fix"),
    (card(2, 1, 2, "L"), "backlog"),
    (card(2, 2, 2, "L"), "fix"),
    # rule 7: K:M is not a quick win; an open design decision is not either
    (card(1, 1, 2, "M"), "note"),
    (card(1, 1, 2, "S"), "quick"),
    (card(1, 2, 2, "S", openDesign=True), "backlog"),
    # rule 7 with the template's `defect: false` (never stored by the ledger)
    (card(1, 1, 2, "S", defect=False), "note"),
    # rule 9: S1 O2 E2 not quick (K:L) → backlog; E1 → note
    (card(1, 2, 2, "L"), "backlog"),
    (card(1, 2, 1, "L"), "note"),
    # rule 10: S0 with every other field maximal
    (card(0, 3, 3, "S"), "quick"),
    (card(0, 3, 3, "M"), "note"),
]


@pytest.mark.parametrize(("fields", "expected"), BOUNDARIES)
def test_rule_boundaries(fields, expected):
    assert classify(fields) == expected


def test_classify_is_total_over_the_field_space():
    """Every combination of the rubric domains and §4 flags classifies into
    one of the template's classes — no combination raises or falls through."""
    flags = [{}, {"conflict": True}, {"violation": False}, {"dispute": True}, {"openDesign": True}]
    seen = set()
    for s, o, e, k, t, f in itertools.product(
        range(4), [0, 1, 2, 3, "?"], range(4), "SML", FINDING_TYPES, flags,
    ):
        seen.add(classify(card(s, o, e, k, t, **f)))
    assert seen == set(CLASS_ORDER)


def test_every_class_recommends_a_decision_the_page_offers():
    assert {c["dec"] for c in CLASSES.values()} <= set(DECISIONS)
    assert CLASS_ORDER == ("fix", "verify", "quick", "decide", "backlog", "reject", "note")


@pytest.mark.parametrize(("fields", "cls", "expected"), [
    (card(2, 1, 2), "backlog", "P2"),
    (card(1, 2, 2), "backlog", "P3"),
    (card(3, "?", 1), "verify", "P1"),
    (card(2, 2, 1), "verify", "P1"),
    (card(2, 1, 1), "verify", "P2"),
    (card(2, "?", 1), "verify", "P2"),
    (card(3, 3, 3), "fix", ""),
])
def test_priority_matches_the_rubric_outcome_table(fields, cls, expected):
    assert priority(fields, cls) == expected
