"""K-73 (d1), dogfood of #877: the screen travels INSIDE the dialog.

The dogfood session approved a commit, two releases and a handoff without
seeing any of them: the screens the skills told it to print before the dialog
were written inside the model's thinking block (the session jsonl has no
`text` block for them), and Claude Code shows thinking only as a one-line
summary. Tool inputs always arrive verbatim, and an option's `preview`
showed a 16-line screen in full (a ~40-line one was not shown at all).

The rule, one canonical paragraph in every skill that asks for a consent and
in the fallbacks: the screen is the preview of the option it belongs to, at
most 15 lines; longer content on "Details" pages; never "shown above".
"""

import re
from pathlib import Path

import pytest

from dct.assets import skills_dir

ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = ROOT / "docs" / "assistant-fallbacks.md"

SCREEN_RULE = (
    'What the owner must read before answering is the preview of the option it '
    'belongs to, at most 15 lines as the dialog shows them. The dialog wraps long '
    'lines itself, about 60 characters wide: a paragraph is one line, never broken '
    'by hand (a second break leaves single words on a line), and a list or table '
    'line stays under 60 characters. Message text written before a dialog is never '
    'the screen: it can land in the thinking block, which the owner sees only as a '
    'one-line summary, so no question or option says "shown above". A longer screen '
    "is split into pages of at most 15 lines: the option's preview shows page 1, and "
    'one more single-select question, "Details", has one option per page (2 to 4), '
    'each with that page as its preview; its answer is not a decision.'
)

# Skills whose dialog asks for a consent on content the owner must read.
CONSENT_SKILLS = ("commit", "handoff", "pickup", "release", "dct-init", "dct-import",
                  "plan-resolve-uncertain")

# Each consent screen is pinned to the option it belongs to.
SCREEN_OPTIONS = {
    "commit": r'"Commit" \(its preview: the screen\)',
    "handoff": r'"Save" \(recommended\) — its preview: the screen',
    "pickup": r"each with that handoff's screen as its preview",
    "release": r'"Release" \(in each of its forms\) carries the screen of the shown variant',
    "dct-import": r'the preview of "Import all as listed"',
    "dct-init": r'"Register" \(recommended\) — its preview',
}

# Phrases of the retired print-then-ask pattern.
RETIRED = re.compile(
    r"shown above\?|listed above\.|shown in full above|Turn 1 —|Turn 2 —|TWO TURNS|"
    r"two-turn pattern|print the (draft|screen|handoff)[^.]{0,40}plain message text|"
    r"printed as plain message text",
    re.IGNORECASE,
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", CONSENT_SKILLS)
def test_every_consent_skill_states_the_screen_rule_among_its_rules(name):
    flat = _flat(_skill(name))
    rules = flat.find("## Rules")
    assert rules >= 0
    assert "**The screen travels inside the dialog.** " + SCREEN_RULE in flat[rules:], \
        f"{name}: the screen rule is missing from its Rules"


def test_the_fallbacks_state_the_screen_rule_before_the_first_workflow():
    flat = _flat(FALLBACKS.read_text(encoding="utf-8"))
    assert SCREEN_RULE in flat[:flat.find("## `/")]


@pytest.mark.parametrize("name", sorted(SCREEN_OPTIONS))
def test_each_consent_screen_is_the_preview_of_its_option(name):
    assert re.search(SCREEN_OPTIONS[name], _flat(_skill(name))), \
        f"{name}: its screen is not tied to the option it belongs to"


@pytest.mark.parametrize("name", sorted(p.name for p in skills_dir().iterdir()
                                        if (p / "SKILL.md").is_file()))
def test_no_skill_keeps_the_print_then_ask_pattern(name):
    hits = RETIRED.findall(_skill(name))
    assert not hits, f"{name}: {hits}"


def test_the_fallbacks_drop_the_print_then_ask_pattern():
    assert not RETIRED.findall(FALLBACKS.read_text(encoding="utf-8"))


def test_the_details_pages_are_reading_not_a_decision_in_the_question_count():
    """The release counts decisions; the Details question must not inflate
    them (and must not be mistaken for one)."""
    for text in (_skill("release"), FALLBACKS.read_text(encoding="utf-8")):
        assert re.search(r'"Details" question of a screen longer than 15 lines is reading, '
                         r"not a decision", _flat(text))


def test_a_long_release_screen_keeps_the_decision_lines_on_its_first_page():
    """With a screen longer than 15 lines, the Release preview is a summary
    page that still carries the headline and the "Then:" lines (where it is
    pushed) — the owner never decides without them."""
    for text in (_skill("release"), FALLBACKS.read_text(encoding="utf-8")):
        flat = _flat(text)
        assert re.search(r"summary page — the headline, one line per block with its count "
                         r'\("Goes in: 25 entries — on the Details pages"\) and the "Then:" lines',
                         flat)


def test_a_report_without_a_decision_is_the_final_text():
    """plan-status had a table followed by a drill-down dialog; the table is
    now the turn's final text and the drill-down a named request."""
    flat = _flat(_skill("plan-status"))
    assert "The table is the final text of the turn" in flat
    assert "Name a plan to see its sections." in flat


def test_no_document_wraps_a_preview_by_hand():
    """K-79 (dogfood repo 2): lines hard-wrapped at 80 characters were wrapped
    a second time by a ~64-column preview pane, leaving single words on a
    line. Previews keep a paragraph on one line and let the dialog wrap it."""
    docs = {name: _skill(name) for name in CONSENT_SKILLS}
    docs["fallbacks"] = FALLBACKS.read_text(encoding="utf-8")
    for name, text in docs.items():
        flat = _flat(text)
        assert not re.search(r"(wrapped at|at most) 80 characters", flat), name
    for name in ("handoff", "pickup"):
        assert re.search(r"one line per paragraph[^.]{0,40}\(the dialog wraps it", _flat(_skill(name))), \
            f"{name}: the draft is not kept unwrapped for the dialog"
