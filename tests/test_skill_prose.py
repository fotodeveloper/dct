"""Prose contracts of the packaged skills that a later edit could silently
undo (#615 / K-27): the handoff approval and the pickup read-out MUST use the
two-turn pattern — long content printed as plain message text in its own
turn, then a dialog whose options only REFERENCE it. The owner's
AskUserQuestion dialog truncates long text, so a draft embedded in the
question is a draft nobody can read before approving it."""

import re

from dct.assets import skills_dir


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def test_handoff_prints_the_draft_before_asking():
    text = _skill("handoff")
    assert "Turn 1 — print the draft as plain message text" in text
    assert "NEVER embed draft content in the question" in text
    # #877: the one question is Save / Change / Cancel; its Save option
    # refers to the draft printed above instead of carrying it.
    assert re.search(r'"Save\b[^"\n]*"', text), "no Save option"
    assert "shown above" in text


def test_pickup_prints_the_handoff_before_asking():
    text = _skill("pickup")
    assert "Turn 1 — print the handoff prompt(s) as plain message text" in text
    assert "printed as plain message text in its own turn FIRST" in text


def test_no_skill_instructs_embedding_the_draft_in_a_dialog():
    # The anti-pattern this guard exists for: putting the prompt body inside
    # the AskUserQuestion `question`/`preview`/option fields.
    for name in ("handoff", "pickup"):
        text = _skill(name).lower()
        assert "embed the draft in the question" not in text
        assert "preview: <draft" not in text
