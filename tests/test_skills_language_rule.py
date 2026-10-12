"""K-75 (dogfood of #877): every user-facing workflow carries ONE language rule.

A Polish-speaking owner got every dialog, screen and report in English: the
skills quote English sentences ("Commit as shown above?", "Release proj-a and
push it to origin/main?") and the model copied them word for word. The rule:
a quoted question, option, screen or report fixes what it SAYS, not its
wording — it is written in the language of the conversation — while
identifiers, commands, paths, file contents, commit messages, changelog
entries, short titles and anything shown verbatim stay unchanged.

One canonical sentence in every skill, the fallbacks and the Codex skills, so
the documents cannot drift apart.
"""

import re
from pathlib import Path

import pytest

from dct.assets import skills_dir
from dct.integrations import codex

ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = ROOT / "docs" / "assistant-fallbacks.md"

LANGUAGE_RULE = (
    "A quoted question, option, screen or report fixes what it says, not its "
    "wording: write it in the language of the conversation. Keep unchanged: "
    "identifiers, commands, paths, file contents, commit messages, changelog "
    "entries, short titles and anything shown verbatim."
)

SKILLS = sorted(p.name for p in skills_dir().iterdir() if (p / "SKILL.md").is_file())


def _flat(text: str) -> str:
    """Markdown wraps lines anywhere — compare with whitespace collapsed."""
    return re.sub(r"\s+", " ", text)


def test_every_skill_is_checked():
    assert len(SKILLS) >= 13, SKILLS


@pytest.mark.parametrize("name", SKILLS)
def test_every_skill_states_the_language_rule_among_its_rules(name):
    text = _flat((skills_dir() / name / "SKILL.md").read_text(encoding="utf-8"))
    rules = text.find("## Rules")
    assert rules >= 0, f"{name}: no Rules section to carry the language rule"
    assert LANGUAGE_RULE in text[rules:], f"{name}: the language rule is missing from its Rules"


def test_the_fallbacks_state_the_language_rule_before_the_first_workflow():
    text = _flat(FALLBACKS.read_text(encoding="utf-8"))
    first_workflow = text.find("## `/")
    assert first_workflow > 0
    assert LANGUAGE_RULE in text[:first_workflow], \
        "the language rule must apply to every fallback, so it sits in the header"


@pytest.mark.parametrize("name", codex.SKILLS)
def test_every_codex_skill_states_the_language_rule(name):
    text = _flat((codex.ASSETS / f"codex-{name}.md").read_text(encoding="utf-8"))
    assert LANGUAGE_RULE in text, f"codex-{name}: the language rule is missing"


def test_the_rule_keeps_what_must_never_be_translated():
    """Changelog entries and short titles are English by their own rules
    (clog, track); commit messages follow the project's convention — the
    language rule must not override them."""
    for kept in ("commit messages", "changelog entries", "short titles", "commands", "paths"):
        assert kept in LANGUAGE_RULE.split("Keep unchanged:")[1], kept
