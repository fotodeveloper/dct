"""K-75 (#917): a project's own ban on a commit trailer is found wherever the
project keeps its rules.

/dct-init (Q3, commit lines) and /commit (skill and its authoritative MCP-only
fallback) let the project's own rules win over a stored trailer preference.
The rules live in `AGENTS.md` (dct's own repository has no `CLAUDE.md` since
K-52) and, in other users' projects, still in `CLAUDE.md` /
`CLAUDE.local.md`. A text that looks only in `CLAUDE.md`, or that counts on
Claude Code's automatic `@import` expansion to see `AGENTS.md`, misses the
ban of an AGENTS.md-only project and offers the forbidden line again.
"""

import re
from pathlib import Path

import pytest

from dct.assets import skills_dir

ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = ROOT / "docs" / "assistant-fallbacks.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _commit_fallback() -> str:
    text = FALLBACKS.read_text(encoding="utf-8")
    start = text.index("## `/commit` fallback")
    return text[start:text.index("\n## ", start + 1)]


def _rule(body: str) -> str:
    """The statement that the project's own rules win: from "own rules" to
    the end of its paragraph (or to the next case of the same list)."""
    paragraphs = [_flat(p) for p in re.split(r"\n\s*\n", body)
                  if re.search(r"own rules win\b", _flat(p))]
    assert len(paragraphs) == 1, f"expected one own-rules statement, got {len(paragraphs)}"
    paragraph = paragraphs[0]
    unit = paragraph[paragraph.index("own rules win"):]
    for end in ("Not a dct project", " - "):
        if end in unit:
            unit = unit[:unit.index(end)]
    return _flat(unit)


RULE_TEXTS = [
    pytest.param(lambda: _rule(_skill("dct-init")), id="dct-init-q3"),
    pytest.param(lambda: _rule(_skill("commit")), id="commit-skill"),
    pytest.param(lambda: _rule(_commit_fallback()), id="commit-fallback"),
]


@pytest.mark.parametrize("text", RULE_TEXTS)
def test_the_ban_is_read_from_agents_md_and_the_claude_md_files(text):
    """An AGENTS.md-only project is covered, and a project that still keeps
    its rules in CLAUDE.md / CLAUDE.local.md stays covered."""
    rule = text()
    for name in ("`AGENTS.md`", "`CLAUDE.md`", "`CLAUDE.local.md`"):
        assert name in rule, f"the own-rules check does not read {name}: {rule}"


@pytest.mark.parametrize("text", RULE_TEXTS)
def test_the_ban_does_not_depend_on_automatic_import_expansion(text):
    """The files are read from disk; a `@path` import line is followed by
    reading that file — never left to the client's automatic expansion."""
    rule = text()
    assert re.search(r"\bnever (rely|count) on\b[^.]{0,80}\b(automatic|auto-?)\w*[^.]{0,40}\bimport",
                     rule, re.IGNORECASE), f"the check may still rely on @import expansion: {rule}"
    assert re.search(r"`@[^`]*`[^.]{0,80}\bread\b|\bread\b[^.]{0,80}`@[^`]*`", rule), \
        f"an import line is not followed by reading the imported file: {rule}"


@pytest.mark.parametrize("text", RULE_TEXTS)
def test_a_forbidden_line_is_never_offered_again(text):
    """The ban wins over a stored yes AND is not put to the owner as an
    option to approve again (stored as off)."""
    rule = text()
    assert re.search(r"\bnever written\b|\bnever add|\bleave out\b|\bleft out\b", rule,
                     re.IGNORECASE), f"a stored yes may still write a forbidden line: {rule}"
    assert re.search(r"\bnever offered\b|\bnot (offered|asked)\b|\bleave out the options?\b",
                     rule, re.IGNORECASE), f"the forbidden line may be offered again: {rule}"
    assert re.search(r"`?off`?", rule), f"the forbidden line is not stored as off: {rule}"


@pytest.mark.parametrize("body", [
    pytest.param(lambda: _skill("dct-init"), id="dct-init"),
    pytest.param(lambda: _skill("commit"), id="commit-skill"),
    pytest.param(_commit_fallback, id="commit-fallback"),
])
def test_no_text_looks_for_the_ban_in_claude_md_alone(body):
    """The pre-K-75 wording "when its CLAUDE.md forbids" (dct-init Q3) is
    the defect: a CLAUDE.md-only check."""
    flat = _flat(body())
    assert not re.search(r"\bits `?CLAUDE\.md`? forbids\b", flat), \
        "the ban is still looked up in CLAUDE.md alone"


def test_dcts_own_ban_lives_in_its_agents_md():
    """The texts say dct's own repository forbids a session link; since K-52
    that rule must be findable in AGENTS.md, the only project instruction
    file — the AGENTS.md-only project the rule has to cover."""
    agents = _flat((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    assert re.search(r"`Claude-Session:` trailer is banned", agents), \
        "dct's AGENTS.md no longer carries the session-link ban the skills cite"
