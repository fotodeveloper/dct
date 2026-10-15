"""K-81 (#917 round 2): one gate-completion policy for every closing workflow.

The packaged GATE POLICY (the add_checkpoint tool description) closes a gate
by its recorded condition: a technical gate on its evidence, a gate whose
condition is a person's act or acceptance only on that person's
confirmation, a rejection only on the user's decision, and a consent the
user already gave is not asked again. /track-resolve, /track-update and the
MCP-only fallback used to stop on EVERY pending gate and wait for a ritual
"done" — a client-dependent extra stop for a technical gate whose evidence
is already complete.

Locked here, per scenario of the accepted scope (evidence sufficient,
evidence missing or stale, a human condition, consent already given, plus
the rejection rule): the policy states it, and each of the three closing
texts states the same rule. Each clause is a regex over the flattened text
— meaning, not wording — and every guard is shown red by mutating the real
text in memory (the clause deleted, or its meaning flipped).
"""

from __future__ import annotations

import re

import pytest
from test_gate_policy import POLICY_CLAUSES, _descriptions
from test_skill_prose_changelog import _codex, _skill, _workflow_fallback


def _flat(text: str) -> str:
    return " ".join(text.split())


def _resolve_closing(body: str) -> str:
    """Only the pending-gate step, bounded before the resolution step."""
    start = body.index("### Step 2")
    return body[start:body.index("### Step 3", start)]


def _resolve_skill() -> str:
    return _resolve_closing(_skill("track-resolve"))


def _resolve_fallback() -> str:
    return _resolve_closing(_workflow_fallback("track-resolve"))


def _update_procedure(body: str) -> str:
    """The terminal-status route AND the shared gate procedure it points below to.

    Sprint membership is a separate operation and bounds this procedure;
    later status/rule paragraphs cannot satisfy its evidence requirements.
    """
    start = body.index('- "resolved"')
    return body[start:body.index("\nSprint membership", start)]


def _update_closing() -> str:
    return _update_procedure(_skill("track-update"))


TEXTS = {
    "track-resolve-skill": _resolve_skill,
    "track-resolve-codex": lambda: _resolve_closing(_codex("track-resolve")),
    "track-resolve-fallback": _resolve_fallback,
    "track-update-skill": _update_closing,
    "track-update-codex": lambda: _update_procedure(_codex("track-update")),
    "track-update-fallback": lambda: _update_procedure(_workflow_fallback("track-update")),
}

# What the closing workflows must say (regexes over the flattened text).
WORKFLOW_CLAUSES = {
    "no terminal update bypass": re.compile(
        r"never use `update_item` with a terminal status to bypass it", re.IGNORECASE),
    "taken by its recorded condition": re.compile(
        r"by (?:its|each gate's) recorded condition", re.IGNORECASE),
    "met with current evidence completes without a ritual done": re.compile(
        r"\bevidence\b[^.]{0,160}\bcurrent\b[^.]{0,200}complet\w*[^.]{0,120}"
        r"\bwithout asking for an? (?:separate|extra|further) \"?done\b", re.IGNORECASE),
    "missing or stale evidence stays pending": re.compile(
        r"\bmissing or stale\b[^.]{0,160}\bstays pending\b", re.IGNORECASE),
    "own test never replaces an independent review": re.compile(
        r"own test never replaces? an independent review", re.IGNORECASE),
    "a person's act needs that person's confirmation": re.compile(
        r"person's act or acceptance[^.]{0,200}\bonly (?:on )?that person's confirmation"
        r"|\bonly (?:on )?that person's confirmation[^.]{0,200}person's act or acceptance",
        re.IGNORECASE),
    "no stand-in for that confirmation": re.compile(
        r"implementation, green tests or the instruction to close the item do not replace",
        re.IGNORECASE),
    "the kind alone does not decide": re.compile(
        r"\bkind alone does not decide\b", re.IGNORECASE),
    "consent already given is not asked again": re.compile(
        r"already gave explicitly[^.]{0,80}\bis not asked again\b", re.IGNORECASE),
    "rejected only on the user's decision": re.compile(
        r"\breject\w*[^.]{0,120}\bonly on the user's (?:own )?decision", re.IGNORECASE),
}

# What the packaged policy says beyond the clauses test_gate_policy pins.
POLICY_EXTRA = {
    "close by recorded condition": re.compile(r"close a gate by its recorded condition",
                                              re.IGNORECASE),
    "technical gate on its evidence": re.compile(r"technical gate[^.;]{0,60}on its evidence",
                                                 re.IGNORECASE),
}


def _policy_clause(name: str) -> re.Pattern:
    return POLICY_EXTRA.get(name) or POLICY_CLAUSES[name]


# scenario → (policy clauses, workflow clauses): both sides must state it.
SCENARIOS = {
    "evidence sufficient": (
        ["close by recorded condition", "technical gate on its evidence",
         "complete only when met"],
        ["taken by its recorded condition", "no terminal update bypass",
         "met with current evidence completes without a ritual done"],
    ),
    "evidence missing or stale": (
        ["complete only when met"],
        ["missing or stale evidence stays pending",
         "own test never replaces an independent review"],
    ),
    "human condition": (
        ["human act needs that person's confirmation", "no stand-in for the confirmation"],
        ["a person's act needs that person's confirmation", "no stand-in for that confirmation",
         "the kind alone does not decide"],
    ),
    "consent already given": (
        ["given consent not re-asked"],
        ["consent already given is not asked again"],
    ),
    "rejection": (
        ["reject only on the user's decision"],
        ["rejected only on the user's decision"],
    ),
}


def _missing(flat: str, names) -> list[str]:
    return [n for n in names if not WORKFLOW_CLAUSES[n].search(flat)]


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_packaged_policy_states_the_scenario(scenario):
    flat = _flat(_descriptions()["add_checkpoint"])
    policy, _ = SCENARIOS[scenario]
    absent = [n for n in policy if not _policy_clause(n).search(flat)]
    assert not absent, f"GATE POLICY no longer says ({scenario}): {absent}"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("text", sorted(TEXTS))
def test_each_closing_workflow_states_the_scenario_as_the_policy_does(text, scenario):
    _, workflow = SCENARIOS[scenario]
    flat = _flat(TEXTS[text]())
    assert _missing(flat, workflow) == [], f"{text} disagrees with GATE POLICY on {scenario}"


@pytest.mark.parametrize("text", sorted(TEXTS))
def test_no_workflow_stops_on_every_pending_gate(text):
    """The old rule — any pending gate → STOP and wait for "done" — is the
    client-dependent extra stop K-81 removes."""
    flat = _flat(TEXTS[text]())
    assert not re.search(r"pending gate(?: checkpoint)?\b[^.]{0,80}→\s*STOP", flat, re.IGNORECASE)
    assert not re.search(r"\ba pending gate stops the close\b", flat, re.IGNORECASE)


# ── the guards are real: each turns red on a mutated text ───────────────────

# A flip keeps the words but reverses the rule.
FLIPS = {
    "no terminal update bypass": (r"Never use `update_item`", "Use `update_item`"),
    "met with current evidence completes without a ritual done":
        (r"without asking for a", "after asking for a"),
    "missing or stale evidence stays pending": (r"stays pending", "is completed"),
    "own test never replaces an independent review":
        (r"own test never replaces", "own test replaces"),
    "a person's act needs that person's confirmation":
        (r"only (on )?that person's confirmation", "any confirmation"),
    "the kind alone does not decide": (r"kind alone does not decide", "kind alone decides"),
    "consent already given is not asked again": (r"is not asked again", "is asked again"),
    "rejected only on the user's decision": (r"only on the user's", "on the assistant's"),
}

GUARDED = [(text, clause) for text in sorted(TEXTS) for clause in sorted(WORKFLOW_CLAUSES)]


@pytest.mark.parametrize(("text", "clause"), GUARDED)
def test_a_workflow_guard_notices_a_dropped_clause(text, clause):
    flat = _flat(TEXTS[text]())
    mutated = WORKFLOW_CLAUSES[clause].sub("", flat)
    assert mutated != flat
    assert clause in _missing(mutated, [clause])


@pytest.mark.parametrize(("text", "clause"),
                         [(t, c) for t, c in GUARDED if c in FLIPS])
def test_a_workflow_guard_notices_a_flipped_clause(text, clause):
    flat = _flat(TEXTS[text]())
    pattern, replacement = FLIPS[clause]
    mutated = re.sub(pattern, replacement, flat, flags=re.IGNORECASE)
    assert mutated != flat
    assert clause in _missing(mutated, [clause])


@pytest.mark.parametrize("name", sorted(POLICY_EXTRA))
def test_the_policy_guard_notices_a_dropped_extra_clause(name):
    flat = _flat(_descriptions()["add_checkpoint"])
    mutated = POLICY_EXTRA[name].sub("", flat)
    assert mutated != flat and not POLICY_EXTRA[name].search(mutated)


@pytest.mark.parametrize('clause', sorted(WORKFLOW_CLAUSES))
def test_resolve_fallback_extraction_cannot_borrow_clause_from_neighbor_workflow(monkeypatch, clause):
    """An intact adjacent workflow must not hide a deleted closing requirement."""
    import test_skill_prose_changelog as prose

    valid = _flat(_resolve_skill())
    mutated = WORKFLOW_CLAUSES[clause].sub('', valid)
    body = ('## `/track-resolve` fallback\n\n##### Step 2: Pending gates\n' + mutated
            + '\n##### Step 3: Determine resolution\nOnly this workflow.\n'
            + '\n## `/track-update` fallback\n\n' + valid)
    monkeypatch.setattr(prose, '_fallbacks', lambda: body)
    extracted = _resolve_fallback()
    assert clause in _missing(_flat(extracted), [clause])
    assert 'track-update' not in extracted


@pytest.mark.parametrize('clause', sorted(WORKFLOW_CLAUSES))
def test_update_closing_extraction_follows_shared_procedure_and_excludes_later_steps(monkeypatch, clause):
    """The routed shared procedure is required; a later copied clause cannot satisfy it."""
    valid = _flat(_resolve_skill())
    mutated = WORKFLOW_CLAUSES[clause].sub('', valid)
    body = ('- "resolved" → follow the shared procedure below before resolve:\n\n' + mutated
            + '\n\nSprint membership (NOT via update_item):\nKeep sprint history.\n'
            + '\n## Step 3: Execute\n' + valid)
    monkeypatch.setattr(__import__(__name__), '_skill', lambda _: body)
    extracted = _update_closing()
    assert clause in _missing(_flat(extracted), [clause])
    assert 'Keep sprint history' not in extracted
