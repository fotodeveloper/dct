"""Prose contracts of the packaged skills that a later edit could silently
undo (#615 / K-27, rewritten for K-73 (d1) of the #877 dogfood): the draft a
handoff approval is about, and the handoffs /pickup asks about, travel INSIDE
the dialog — as option previews — never as message text before it. Text
written before a dialog can land in the thinking block, which the owner sees
only as a one-line summary; in the dogfood that made every "… shown above?"
an approval of text nobody had seen."""

import re

from dct.assets import skills_dir


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_handoff_puts_the_draft_in_the_save_preview():
    flat = _flat(_skill("handoff"))
    assert re.search(r'"Save" \(recommended\) — its preview: the screen', flat), \
        "the Save option does not carry the draft as its preview"
    assert "The question text and the option labels and descriptions carry none of the draft" \
        in flat
    assert "Every word of the draft and of the context_summary is on a page" in flat


def test_pickup_puts_each_handoff_in_its_option_preview():
    flat = _flat(_skill("pickup"))
    assert re.search(r"each with that handoff's screen as its preview", flat)
    assert re.search(r"\(at most 15 lines\)", flat), "the fit rule of a handoff preview is gone"


def test_no_skill_points_a_dialog_at_text_above_it():
    for name in ("handoff", "pickup", "commit", "release", "dct-init", "dct-import"):
        text = _skill(name)
        for phrase in ("shown above?", "listed above.", "Turn 1 —", "TWO TURNS",
                       "print the draft as plain message text"):
            assert phrase not in text, f"{name}: {phrase!r}"


def test_no_skill_instructs_embedding_the_draft_in_the_question_text():
    # The draft goes into previews; the question and option labels stay short.
    for name in ("handoff", "pickup"):
        text = _skill(name).lower()
        assert "embed the draft in the question" not in text
        assert "question: \"<draft" not in text
