"""Contracts of the skills and docs for labels and changelog proof (#817 /
#801): the skills and the authoritative fallback doc must keep telling the
assistant to set a `short_title` and to record the agent's own `branch` /
`worktree`; /commit and /release are a THIN DIALOG over the release workflow
(K-72) — checked against the workflow's REAL contract: every `dct release` /
`dct clog` subcommand and flag they name exists in the production parser
(dct.cli.build_parser), the keys they read exist in the workflow's schemas,
the revert message and the quoted example behave as documented under the
production proof rules, and the release dialog never performs an effect
(ledger write, commit, tag, version sync, changelog edit) itself."""

import argparse
import inspect
import re
from pathlib import Path

import pytest

from dct.assets import skills_dir
from dct.cli import build_parser
from dct.integrations import codex
from dct.workflows.release.proof import (
    CHANGELOG_LINE_PATTERN,
    TRAILER_FORMAT,
    ids_from_body,
    revert_target,
)

ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = ROOT / "docs" / "assistant-fallbacks.md"
WORKFLOW_DOC = ROOT / "docs" / "release-workflow.md"
SNIPPET = ROOT / "dct" / "assets" / "claude-md-snippet.md"


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _codex(name: str) -> str:
    return (codex.ASSETS / f"codex-{name}.md").read_text(encoding="utf-8")


def _fallbacks() -> str:
    return FALLBACKS.read_text(encoding="utf-8")


def _snippet() -> str:
    return SNIPPET.read_text(encoding="utf-8")


def _trailer_regex() -> re.Pattern:
    """The strict body-scan regex of the PRODUCTION proof layer — the doc
    must state the very same pattern (asserted in
    test_fallbacks_carry_the_shared_trailer_and_range_rules)."""
    return re.compile(CHANGELOG_LINE_PATTERN)


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
    """K-40: short_title is optional on update — omitting it keeps the label;
    K-56: a whitespace-only value is explicit and clears it, in both clients."""
    flat = " ".join(text().split())
    assert re.search(r'omit it \(or pass `""`\) to keep the stored label', flat)
    assert "'__clear__'" in flat
    assert re.search(r"whitespace-only value (is not \"omitted\" — it strips to empty and )?"
                     r"(also )?clears", flat)
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


def _section(body: str, start: str, end: str) -> str:
    a = body.index(start)
    return body[a:body.index(end, a)]


def _in_order(body: str, *markers: str) -> None:
    positions = [body.index(m) for m in markers]
    assert positions == sorted(positions), list(zip(markers, positions))


def _release_fallback() -> str:
    return _section(_fallbacks(), "## `/release` fallback", "## `/commit` fallback")


def _commit_fallback() -> str:
    return _section(_fallbacks(), "## `/commit` fallback", "## `/track`")


# ── the workflow's CLI is the contract the dialogs are written against ──────


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _cli() -> dict[str, dict[str, argparse.ArgumentParser]]:
    """{group: {subcommand: parser}} for the `dct release` / `dct clog` groups
    of the PRODUCTION parser."""
    top = _subparsers(build_parser())
    return {group: _subparsers(top[group]) for group in ("release", "clog")}


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


_COMMAND_RE = re.compile(r"\bdct (release|clog) ([a-z][a-z-]*)([^`\n]*)")


def _commands(text: str) -> list[tuple[str, str, list[str]]]:
    """(group, subcommand, flags) for every `dct release …` / `dct clog …`
    invocation named in `text` (inline code and code blocks)."""
    out = []
    for group, sub, rest in _COMMAND_RE.findall(text):
        rest = rest.split("#", 1)[0]  # a shell comment is not part of the command
        out.append((group, sub, re.findall(r"(?<![\w-])--[a-z][a-z-]*", rest)))
    return out


DIALOG_DOCS = [
    pytest.param(lambda: _skill("release"), id="release-skill"),
    pytest.param(_release_fallback, id="release-fallback"),
    pytest.param(lambda: _skill("commit"), id="commit-skill"),
    pytest.param(_commit_fallback, id="commit-fallback"),
    pytest.param(lambda: _section(_snippet(), "**Committing**", "**Session end**"),
                 id="claude-md-snippet"),
]


@pytest.mark.parametrize("text", DIALOG_DOCS)
def test_every_named_workflow_command_and_flag_exists_in_the_cli(text):
    """A renamed subcommand or flag breaks the dialog docs, not the owner's
    release: each `dct release <sub> --flag` they name is in the parser."""
    cli = _cli()
    commands = _commands(text())
    assert commands, "no dct release / dct clog command named"
    for group, sub, flags in commands:
        assert sub in cli[group], f"dct {group} {sub}: no such subcommand"
        unknown = set(flags) - _option_strings(cli[group][sub])
        assert not unknown, f"dct {group} {sub}: unknown flags {sorted(unknown)}"


def test_release_skill_and_fallback_drive_the_same_workflow_commands():
    """K-72: the skill and the authoritative fallback describe ONE dialog over
    the same commands — every effects / proof subcommand of the CLI."""
    effects = {"plan", "attest", "apply", "resume", "abandon", "reconstruct", "status"}
    assert effects <= set(_cli()["release"])
    for text in (_skill("release"), _release_fallback()):
        named = {sub for group, sub, _ in _commands(text) if group == "release"}
        assert named == effects, sorted(named ^ effects)


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
])
def test_release_dialog_passes_the_owners_decisions_as_flags(text):
    """Explicit version, explicit selection, explicit confirmation — the flags
    the effects layer requires (it refuses otherwise)."""
    calls = [(sub, flags) for group, sub, flags in _commands(text()) if group == "release"]
    applies = [flags for sub, flags in calls if sub == "apply" and len(flags) > 1]
    assert applies, "no complete `dct release apply …` invocation"
    for flags in applies:
        assert "--version" in flags and "--confirm" in flags
        assert "--plan-digest" in flags  # K-80: the approved snapshot
        assert ("--entries" in flags) != ("--no-entries" in flags)
        assert not {"--patch", "--minor", "--major"} & set(flags)  # V computed once
    abandons = [flags for sub, flags in calls if sub == "abandon" and flags]
    assert abandons and all({"--version", "--reason", "--confirm"} <= set(f) for f in abandons)
    attests = [flags for sub, flags in calls if sub == "attest" and flags]
    assert attests and all({"--entries", "--confirm"} <= set(f) for f in attests)
    flat = " ".join(text().split())
    assert "--confirm-version-files" in flat
    # The flags the dialog relies on are really required / present.
    cli = _cli()["release"]
    required = {a.option_strings[0] for a in cli["abandon"]._actions if a.required}
    assert {"--version", "--reason"} <= required


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
])
def test_release_dialog_never_performs_the_effects_itself(text):
    """K-72: skills only do the dialog — no ledger write, no git effect, no
    version sync and no changelog-file edit of their own."""
    body = text()
    for forbidden in ("advance_release(", "release_changelog(", "release-advance",
                      "git tag", "git commit", "git add", "git push origin v",
                      "version-sync.sh", "export_changelog", "--allow-empty"):
        assert forbidden not in body, forbidden
    for block in re.findall(r"```(?:bash)?\n(.*?)```", body, re.DOTALL):
        for line in block.splitlines():
            assert not line.lstrip().startswith("git "), line


def test_release_skill_plans_first_and_reports_versions_separately():
    body = _skill("release")
    _in_order(body, "dct release plan --json", "## Resume path")
    _in_order(body, "### Step 0:", "### Step 1:", "### Step 4:", "### Step 6:")
    headings = [int(n) for n in re.findall(r"^### Step (\d+)", body, re.MULTILINE)]
    assert headings == list(range(len(headings)))
    flat = " ".join(body.split())
    # K-55 / K-31: requested V, the record's version and current_version.
    for key in ("details.requested_version", "details.current_version"):
        assert key in flat
    assert "range.confirm_required" in flat and "range.approximate" in flat
    assert "attestation_groups" in flat
    assert 'There is NO "include without proof" option' in body
    assert "version-only" in flat.lower() and "consent" in flat


def test_release_fallback_says_the_effects_need_the_local_cli():
    flat = " ".join(_release_fallback().split())
    assert "Without the CLI" in flat
    assert re.search(r"ledger tools .{0,80}cannot verify", flat)


def test_release_documented_keys_exist_in_the_workflow_schemas():
    """The plan / result keys the dialog reads are the workflow's real ones."""
    from dct.workflows.release.effects import OUTCOMES, RESULT_KEYS
    from dct.workflows.release.plan import PLAN_KEYS

    flat = " ".join((_skill("release") + _release_fallback()).split())
    for key in ("kind", "unfinished_release", "range", "entries", "attestation_groups",
                "index", "version_files", "warnings"):
        assert key in PLAN_KEYS and re.search(rf"`(range\.)?{key}[`. ]", flat), key
    for key in ("details", "steps", "version", "release"):
        assert key in RESULT_KEYS
    for outcome in ("completed", "noop", "refused", "failed", "attested", "preview"):
        assert outcome in OUTCOMES and f"`{outcome}`" in flat, outcome


# ── /commit: candidates from the workflow, K-30 order, K-53 / K-66 / K-68 ──


def test_commit_selects_from_the_workflow_candidates_after_staging():
    """K-30: stage → select from the final index → commit (no second add);
    K-53: the selection is the workflow's `dct clog candidates`."""
    body = _skill("commit")
    _in_order(body, "### Step 4: Stage", "### Step 5: Select", "### Step 6: Commit")
    step5 = _section(body, "### Step 5: Select", "### Step 6")
    assert "dct clog candidates --json" in step5
    assert "FINAL staged diff" in body and "git diff --cached" in step5
    step6 = _section(body, "### Step 6: Commit", "### Step 7")
    assert "git add" not in step6.split("```bash", 1)[1].split("```", 1)[0]
    assert "Changelog: 12, 15\nCo-Authored-By:" in body
    assert "partially staged" in body
    recovery = " ".join(_section(body, "### Step 7", "### Step 8").split())
    assert "ANY change to the index" in recovery and "redo Step 5" in recovery


def test_commit_fallback_keeps_the_same_order_and_candidates():
    commit = _commit_fallback()
    _in_order(commit, "**Stage first.**", "**Then select the trailer**",
              "**Then commit — WITHOUT another `git add`.**")
    assert "partially staged" in commit and "ANY change to the index" in commit
    assert "dct clog candidates --json" in commit


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("commit"), id="skill"),
    pytest.param(_commit_fallback, id="fallback"),
])
def test_commit_attaches_uncertain_candidates_only_after_confirmation(text):
    """K-53 / K-66: uncertain (server / no provenance / detached HEAD) are
    matched but attached only after the owner confirms and update_changelog
    fixes BOTH fields against the actual checkout — never automatically."""
    flat = " ".join(text().split())
    from dct.workflows.release.plan import CANDIDATE_KEYS

    for key in ("confirmed", "uncertain", "other_branch", "already_proven"):
        assert key in CANDIDATE_KEYS and f"`{key}`" in flat, key
    assert "update_changelog(entry_id, branch=<current>, worktree=<toplevel>)" in flat
    assert re.search(r"never (attached )?automatically", flat)
    assert re.search(r"[Dd]etached HEAD", flat)


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("commit"), id="skill"),
    pytest.param(_commit_fallback, id="fallback"),
])
def test_commit_revert_message_is_detected_by_the_production_rule(text):
    """K-68: the documented revert message, filled in, is what proof.py
    recognises as a revert of that commit."""
    flat = " ".join(text().split())
    assert 'Revert "<original subject>"' in flat
    assert "This reverts commit <full sha>." in flat
    sha = "0123456789abcdef0123456789abcdef01234567"
    message = (f'Revert "feat(core): add parser (v1.0.0)"\n\n'
               f"This reverts commit {sha}.\n")
    assert revert_target(message) == sha


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("commit"), id="skill"),
    pytest.param(_commit_fallback, id="fallback"),
])
def test_commit_quoted_trailer_example_proves_nothing(text):
    """K-66: the strict format is reserved; the documented quoted example is
    not proof under the production body scan, the unquoted line is."""
    assert "`> Changelog: 12`" in text()
    assert ids_from_body("feat: x\n\nThe format is\n> Changelog: 12\n") == set()
    assert ids_from_body("feat: x\n\nChangelog: 12\n") == {12}


def test_fallbacks_point_the_shared_rules_at_the_code():
    shared = _section(_fallbacks(), "## Changelog proof", "## `/release` fallback")
    flat = " ".join(shared.split())
    assert f"`{CHANGELOG_LINE_PATTERN}`" in shared
    assert TRAILER_FORMAT in shared
    assert "dct/workflows/release/proof.py" in flat and "release-workflow.md" in flat
    assert "nearest release tag that is an ancestor" in flat
    assert "dct release attest" in flat and "chore: attest changelog entries" in flat
    assert "never an automatic assignment" in flat
    assert "never proof that an entry belongs to the commit" in flat
    assert re.search(r"Never fall into an unfiltered list", flat)


# ── docs/release-workflow.md: merge policy (K-69) and layering (K-72) ───────


def _workflow_doc() -> str:
    return WORKFLOW_DOC.read_text(encoding="utf-8")


def test_workflow_doc_states_the_merge_policy():
    policy = " ".join(_section(_workflow_doc(), "## Merge policy", "\n## ").split())
    for mode in ("merge commit", "rebase merge", "hosted squash that keeps the commit messages",
                 "title-only squash", "git merge --squash"):
        assert mode in policy, mode
    assert "INDENTS" in policy and "attestation_groups" in policy


def test_own_release_tool_integration_uses_the_real_ledger_parameters():
    """K-72: a project with its own tool uses the plan for proof and the
    exact-stamp parameters of the REAL release_changelog / advance_release."""
    from dct.core.releases import RELEASE_FLOW
    from dct.mcp import server

    doc = _workflow_doc()
    integration = doc[doc.index("### Integrating a project's own release tool"):]
    assert "dct release plan --version V --entries <ids> --json" in integration
    assert "snapshot_digest" in integration  # K-80: re-plan before the stamp
    call = re.search(r"release_changelog\(([^)]*)\)", integration, re.DOTALL).group(1)
    named = set(re.findall(r"(\w+)=", call))
    assert {"version", "entry_ids", "candidate_sha", "checkout_root", "proof"} <= named
    assert named <= set(inspect.signature(server.release_changelog).parameters)
    assert "advance_release(version=V, status=" in integration
    statuses = re.findall(r"`(files_written|committed|tagged|pushed|completed)`", integration)
    assert list(dict.fromkeys(statuses)) == list(RELEASE_FLOW[1:])
    assert set(inspect.signature(server.advance_release).parameters) >= {
        "version", "status", "release_commit_sha", "manifest"}


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


