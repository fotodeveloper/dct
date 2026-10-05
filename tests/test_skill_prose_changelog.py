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
    # K-47: a missing ack key is an unsupported (outdated) server contract —
    # reported, never "fixed" by re-running the create (that duplicates the item).
    flat = " ".join(body.split())
    assert re.search(r"echoes?( the stored)? `short_title`", flat)
    assert "unsupported (outdated)" in flat
    assert re.search(r"do not retry (`create_item`|the create)", flat, re.IGNORECASE)
    assert "set the label later with `update_item`" not in flat


def test_dct_import_stops_instead_of_retrying_on_an_outdated_create_ack():
    flat = " ".join(_skill("dct-import").split())
    assert "unsupported server contract" in flat
    assert "Do NOT retry the create" in flat


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("track-update"), id="claude-track-update"),
    pytest.param(lambda: _codex("track"), id="codex-track"),
])
def test_update_keeps_the_label_unless_asked_to_change_it(text):
    """K-40: short_title is optional on update — omitting it keeps the label."""
    flat = " ".join(text().split())
    assert re.search(r"omit it to keep the stored label", flat)
    assert "'__clear__'" in flat
    assert "always pass" not in flat.lower().split("update_item")[-1]


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
    # K-36: a server-detected ack is corrected at once, never trusted.
    flat = " ".join(body.split())
    assert '"server"' in flat
    assert re.search(r"(NOT confirmed|never treated as confirmed)", flat)
    assert "update_changelog" in flat


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


def _section(body: str, start: str, end: str) -> str:
    a = body.index(start)
    return body[a:body.index(end, a)]


def _in_order(body: str, *markers: str) -> None:
    positions = [body.index(m) for m in markers]
    assert positions == sorted(positions), list(zip(markers, positions))


def test_commit_stages_before_selecting_and_commits_without_adding():
    """K-30: stage → select from the final index → commit (no second add)."""
    body = _skill("commit")
    _in_order(body, "### Step 4: Stage", "### Step 5: Select", "### Step 6: Commit")
    step6 = _section(body, "### Step 6: Commit", "### Step 7")
    assert "git add" not in step6.split("```bash", 1)[1].split("```", 1)[0]
    assert "partially staged" in body
    recovery = " ".join(_section(body, "### Step 7", "### Step 8").split())
    assert "ANY change to the index" in recovery and "redo Step 5" in recovery


def test_commit_fallback_keeps_the_same_staging_order():
    commit = _section(_fallbacks(), "## `/commit` fallback", "## `/track`")
    _in_order(commit, "**Stage first.**", "**Then select the trailer**",
              "**Then commit — WITHOUT another `git add`.**")
    assert "partially staged" in commit
    assert "ANY change to the index" in commit


def test_release_wip_commit_follows_the_commit_order():
    step1 = _section(_skill("release"), "### Step 1:", "### Step 2:")
    _in_order(step1, "**Stage first", "**Then select the trailer**",
              "**Commit WITHOUT another `git add`**")
    fallback = _section(_fallbacks(), "**Normal path:**", "**Resume path**")
    _in_order(fallback, "A dirty tree is NOT a stop", "stage the",
              "THEN select the trailer", "THEN `git commit` without")


def test_release_checks_for_an_unfinished_release_before_wip_and_bump():
    """K-41: resume detection precedes the WIP auto-commit and the bump."""
    body = _skill("release")
    _in_order(body, "### Step 0:", "mcp__dct__get_release(project)",
              "### Step 1: Ensure a clean working tree", "**Question 2 — bump type.**")
    assert "## Resume path" in body
    fallback = _section(_fallbacks(), "**Normal path:**", "**Resume path**")
    _in_order(fallback, "`get_release(project)`", "A dirty tree is NOT a stop",
              "Ask via AskUserQuestion: `patch`")


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_fallbacks, id="fallback"),
])
def test_every_release_call_passes_version_entry_ids_and_the_pin(text):
    calls = re.findall(r"release_changelog\((project[^)`]*)\)", text())
    assert calls, "no release_changelog call documented"
    for args in calls:
        assert "version=V" in args and "bump_type" not in args
        assert "entry_ids=[" in args  # explicit — never omitted (= stamp all)
        assert "candidate_sha=SHA" in args and "checkout_root=checkout" in args


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_fallbacks, id="fallback"),
])
def test_release_advances_the_record_in_order(text):
    """Normal path: one advance per finished effect, in status order; the
    move to 'committed' re-pins the release commit."""
    body = text()
    if "**Normal path:**" in body:  # the fallback doc
        normal = _section(body, "**Normal path:**", "**Resume path**")
    else:  # the skill
        normal = body[:body.index("## Resume path")]
    calls = re.findall(r"advance_release\(([^)]*\)?[^)]*)\)", normal)
    statuses = [re.search(r'"(\w+)"', c).group(1) for c in calls]
    assert statuses == ["files_written", "committed", "tagged", "completed"]
    committed = calls[1]
    assert "candidate_sha=$(git rev-parse HEAD" in committed


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_fallbacks, id="fallback"),
])
def test_historical_rerun_never_syncs_or_tags(text):
    """K-31: V != current_version → no version-sync, no tag; report both."""
    flat = " ".join(text().split())
    assert re.search(r"`current_version` ≠ `V`.{0,120}never run version-sync or create a tag", flat)
    assert re.search(r"requested `V` and the returned `current_version`", flat)
    assert re.search(r"non-zero exit STOPS the release", flat)


def test_fallback_bootstrap_writes_a_version_only_section_and_counts_only_appends():
    """K-32: one changelog-file contract; +1 applies to an existing file only."""
    contract = _section(_fallbacks(), "**Changelog-file contract", "**Normal path:**")
    bootstrap = _section(contract, "- `F` missing (bootstrap)", "- `F` exists")
    assert "version-only" in bootstrap and "exactly once" in bootstrap
    assert "N + 1" not in bootstrap
    assert "N + 1" in _section(contract, "- `F` exists", "- `## [V]` more than once")
    assert "created_at" in contract  # a resume keeps the original date


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_fallbacks, id="fallback"),
])
def test_release_resolves_files_in_the_current_checkout(text):
    """K-34: F and the sync script live under the CURRENT checkout's project root."""
    flat = " ".join(text().split())
    assert "--git-common-dir" in flat
    assert re.search(r"NOT the destination", flat)
    assert "Authoritative cross-check" not in flat and "authoritative cross-check" not in flat


def test_fallback_states_detached_head_and_squash_rules():
    flat = " ".join(_fallbacks().split())
    assert "git merge --squash" in flat and "never an automatic assignment" in flat
    assert re.search(r"Detached HEAD.{0,400}never fall into that unfiltered list", flat, re.IGNORECASE)
    assert "never proof that an entry belongs to the commit" in flat
