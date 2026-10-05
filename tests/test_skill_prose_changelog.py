"""Prose contracts for labels and changelog proof (#817 / #801): the skills
and the authoritative fallback doc must keep telling the assistant to set a
`short_title`, to record the agent's own `branch` / `worktree`, to write and
read `Changelog:` trailers, and to probe `release_changelog` for `entry_ids`
before a release. A later edit that drops one of these silently reverts the
workflow to the old stamp-everything behaviour."""

import re
from pathlib import Path

import pytest

from dct.assets import skills_dir
from dct.integrations import codex

FALLBACKS = Path(__file__).resolve().parent.parent / "docs" / "assistant-fallbacks.md"


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _codex(name: str) -> str:
    return (codex.ASSETS / f"codex-{name}.md").read_text(encoding="utf-8")


def _fallbacks() -> str:
    return FALLBACKS.read_text(encoding="utf-8")


def _trailer_regex() -> re.Pattern:
    """The strict body-scan regex exactly as the authoritative doc states it."""
    match = re.search(r"`(\^Changelog: [^`]+\$)`", _fallbacks())
    assert match, "assistant-fallbacks.md lost the strict Changelog: regex"
    return re.compile(match.group(1))


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("track"), id="claude-track"),
    pytest.param(lambda: _codex("track"), id="codex-track"),
])
def test_track_sets_short_title_and_checks_the_echo(text):
    body = text()
    assert "short_title" in body
    assert "2-4 word English noun phrase" in body or "2–4 word English noun" in body
    assert "echo" in body  # the ack echo is how an outdated server is detected


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("clog"), id="claude-clog"),
    pytest.param(lambda: _codex("clog"), id="codex-clog"),
])
def test_clog_passes_branch_and_worktree_from_the_agent_cwd(text):
    body = text()
    assert "git branch --show-current" in body
    assert "git rev-parse --show-toplevel" in body
    assert "branch" in body and "worktree" in body
    assert "add_changelog" in body


def test_commit_writes_the_trailer_from_the_staged_diff():
    body = _skill("commit")
    assert "Changelog: <id>, <id>" in body
    assert "git diff --cached" in body
    assert "FINAL staged diff" in body
    assert "include_unbranched=true" in body
    # The heredoc example keeps the trailer before Co-Authored-By.
    assert "Changelog: 12, 15\nCo-Authored-By:" in body


def test_release_probes_entry_ids_and_attests_by_commit():
    body = _skill("release")
    assert "MUST list an `entry_ids` parameter" in body
    assert "chore: attest changelog entries" in body
    assert "git rev-parse --git-path" in body
    assert "ALWAYS an explicit `version`" in body
    assert 'There is NO "include without proof" option' in body


def test_release_has_no_step_numbering_gaps_or_ghost_references():
    body = _skill("release")
    headings = [int(n) for n in re.findall(r"^### Step (\d+)", body, re.MULTILINE)]
    assert headings == list(range(len(headings)))
    assert "Step 2b" not in body and "Step 8b" not in body


def test_fallbacks_carry_the_shared_trailer_and_range_rules():
    body = _fallbacks()
    assert "`^Changelog: [0-9]+(, [0-9]+)*$`" in body
    assert "%(trailers:key=Changelog,valueonly,separator=%x2C)" in body
    assert 'git merge-base --is-ancestor "v$CUR"' in body
    assert "chore: attest changelog entries" in body
    assert "entry_ids" in body and "short_title" in body


@pytest.mark.parametrize("line", ["Changelog: 3, 12", "Changelog: 7", "Changelog: 1, 2, 30"])
def test_strict_trailer_regex_accepts_the_commit_format(line):
    assert _trailer_regex().match(line)


@pytest.mark.parametrize("line", [
    "> Changelog: 3",       # quoted prose
    "- Changelog: 3",       # bulleted prose
    "Changelog:3",          # no space after the colon
    "Changelog: 3,12",      # no space after the comma
    " Changelog: 3",        # indented
    "Changelog: 3, 12.",    # trailing punctuation
    "Changelog: #3",        # hash-prefixed id
    "Changelog: ",          # no ids
])
def test_strict_trailer_regex_rejects_prose_and_malformed_lines(line):
    assert not _trailer_regex().match(line)
