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
# K-36: the reference every Claude user is pointed at, read on demand (the
# guide formerly attached as the CLAUDE.md snippet). The short pointer that
# lands in CLAUDE.md is pinned in test_claude_md_snippet.py.
REFERENCE = ROOT / "dct" / "assets" / "dct-reference.md"


def _skill(name: str) -> str:
    return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")


def _codex(name: str) -> str:
    return (codex.ASSETS / f"codex-{name}.md").read_text(encoding="utf-8")


def _fallbacks() -> str:
    return FALLBACKS.read_text(encoding="utf-8")


def _reference() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _skill_invocation(body: str, name: str) -> str:
    """Exact native prefix from the workflow heading, independent of fallback wrappers."""
    heading = re.search(r"(?m)^# ([/$]?)[a-z][a-z-]*\b", body)
    assert heading, "missing workflow heading to identify its native invocation"
    return heading.group(1) + name


def _question_interaction(body: str) -> str:
    """Claude retains its UI; other renderings use the supported readable interaction."""
    heading = re.search(r"(?m)^# ([/$]?)[a-z][a-z-]*\b", body)
    assert heading
    if heading.group(1) == "/":
        return "AskUserQuestion"
    assert "AskUserQuestion" not in body
    return "decision interaction"


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
    pytest.param(lambda: _codex("track-update"), id="codex-track-update"),
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


def _workflow_fallback(name: str) -> str:
    body = _fallbacks()
    start = body.index(f"## `/{name}` fallback")
    end = body.find("\n## ", start + 1)
    section = body[start:end if end >= 0 else len(body)]
    return re.sub(r"(?m)^#{3}(#+)? ", lambda m: "#" * (len(m.group(1) or "") + 1) + " ", section)


def _release_fallback() -> str:
    return _workflow_fallback("release")


def _commit_fallback() -> str:
    return _workflow_fallback("commit")


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
    pytest.param(_reference,
                 id="dct-reference"),
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
    the same commands — every effects / proof subcommand of the CLI. Phase 2
    (section H): the attestation is made by `apply --consent` (the "with all
    N" variant, `prepare --preview --attest`), so a standalone `dct release
    attest` is no longer a step the dialog must name."""
    effects = {"plan", "attest", "prepare", "apply", "resume", "abandon", "reconstruct", "status",
               "takeover", "version-files"}
    required = effects - {"attest"}
    assert effects <= set(_cli()["release"])
    named = [{sub for group, sub, _ in _commands(text) if group == "release"}
             for text in (_skill("release"), _release_fallback())]
    assert named[0] == named[1], sorted(named[0] ^ named[1])
    assert required <= named[0] <= effects, sorted(named[0] ^ required)


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
])
def test_release_dialog_passes_the_owners_decisions_as_flags(text):
    """The owner's decisions reach the effects layer as flags it requires.
    Phase 2 (section H): the screen's content is bound by the preview, so
    every apply of the dialog is `apply --consent C --confirm --json` (a
    binding flag beside it is refused, consent_flag_conflict) and every
    prepare is the preview (`--preview`, or `--discard-preview` on Stop) —
    never the D/P prepare with `--plan-digest`."""
    calls = [(sub, flags) for group, sub, flags in _commands(text()) if group == "release"]
    applies = [flags for sub, flags in calls if sub == "apply" and len(flags) > 1]
    assert applies, "no complete `dct release apply …` invocation"
    for flags in applies:
        assert set(flags) == {"--consent", "--confirm", "--json"}, flags
    prepares = [flags for sub, flags in calls if sub == "prepare" and len(flags) > 1]
    assert prepares, "no complete `dct release prepare …` invocation"
    for flags in prepares:
        assert ("--preview" in flags) != ("--discard-preview" in flags), flags
        assert "--plan-digest" not in flags
        assert not ({"--entries", "--no-entries"} <= set(flags))
    abandons = [flags for sub, flags in calls if sub == "abandon" and flags]
    assert abandons and all({"--version", "--reason", "--confirm"} <= set(f) for f in abandons)
    attests = [flags for sub, flags in calls if sub == "attest" and flags]
    assert all({"--entries", "--confirm"} <= set(f) for f in attests)
    flat = " ".join(text().split())
    assert "--confirm-version-files" not in flat  # removed (K-76): it refuses now
    # The flags the dialog relies on are really required / present.
    cli = _cli()["release"]
    required = {a.option_strings[0] for a in cli["abandon"]._actions if a.required}
    assert {"--version", "--reason"} <= required
    assert "--consent" in _option_strings(cli["apply"])
    assert {"--preview", "--discard-preview", "--message-file", "--include-untracked",
            "--attest"} <= _option_strings(cli["prepare"])


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
])
def test_release_dialog_never_performs_the_effects_itself(text):
    """K-72: skills only do the dialog — no ledger write, no git effect, no
    version sync and no changelog-file edit of their own. K-120: the guard
    bans RAW commands (the commit procedure has ONE source, /commit), not
    the run itself — the same text must still tell the agent to commit the
    work in progress and to let the workflow push to the approved target
    (test_release_dialog_guard_keeps_the_wip_commit_and_the_push)."""
    body = text()
    for forbidden in ("advance_release(", "release_changelog(", "release-advance",
                      "git tag", "git commit", "git add", "git push origin v",
                      "export_changelog", "--allow-empty"):
        assert forbidden not in body, forbidden
    # The version script is NAMED by its path (the owner fixes or adds it),
    # but never RUN by the dialog: no execution form anywhere, and the
    # prohibition itself is stated.
    assert not re.search(r"(\bbash|\bsh|\bsource|\./|\bexec)\s+\S*version-sync\.sh", body), \
        "the dialog runs the version script itself"
    assert re.search(r"never runs? the version script itself", " ".join(body.split())), \
        "the dialog does not say it never runs the version script itself"
    for block in re.findall(r"```(?:bash)?\n(.*?)```", body, re.DOTALL):
        assert "version-sync.sh" not in block, block
        for line in block.splitlines():
            assert not line.lstrip().startswith("git "), line


_BAN_RE = re.compile(r"\bnever\b[^.;:()]{0,70}\b(?:commits?|push(?:es)?)\b[^.;:()]{0,70}",
                     re.IGNORECASE)


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
])
def test_release_dialog_guard_keeps_the_wip_commit_and_the_push(text):
    """K-120, deliberately rewritten for phase 2 (section H, K-a (a)): the
    work in progress is still committed INSIDE the release — no longer by
    the agent's own `git commit` and a re-plan, but by `apply --consent`
    (through the repository's hooks, under the release's lock) from the
    message the agent drafted (`--message-file`); the owner is never sent
    away to commit, and the workflow performs the push the screen bound. No
    sentence bans a commit or a push wholesale: every "never … commit /
    push" clause is scoped to the release's own effects."""
    flat = " ".join(text().split())
    # (worded without "never … commit": the scoped-ban check below stays)
    assert re.search(r"not a request (to|that|for) the owner|"
                     r"do not tell the owner to commit", flat, re.IGNORECASE), \
        "the dialog may send the owner away to commit"
    # candidates → the drafted message on the preview → apply --consent commits it
    # (the Finish route of a resumed consent may name apply --consent earlier)
    for marker in ("dct clog candidates --json", "--message-file", "dct release apply --consent"):
        assert marker in flat, marker
    _in_order(flat, "dct clog candidates --json", "--message-file")
    assert flat.find("dct release apply --consent", flat.index("--message-file")) != -1, \
        "no apply --consent after the drafted message"
    assert re.search(r"apply --consent`?[^.]{0,200}\bcommits?\b[^.]{0,120}"
                     r"(work in progress|uncommitted|your changes)|"
                     r"(work in progress|uncommitted changes)[^.]{0,200}committed by "
                     r"`?(dct|apply)", flat, re.IGNORECASE), \
        "the dialog does not say apply --consent commits the work in progress"
    assert re.search(r"push(es)? .{0,120}(approved|bound|the screen)", flat)
    bans =[m.group(0) for m in _BAN_RE.finditer(flat)]
    unscoped = [b for b in bans if not re.search(
        r"FOR THE RELEASE|of its own|on your own|by itself|--no-verify|--amend|without", b)]
    assert not unscoped, unscoped


def test_release_skill_plans_first_and_reports_versions_separately():
    body = _skill("release")
    _in_order(body, "dct release plan --json", "## Resume path")
    # Phase 2 (section H) replaces the old Steps 2-9 by the one screen: only
    # the probe and the routing keep their numbers; the steps stay sequential.
    _in_order(body, "### Step 0:", "### Step 1:")
    headings = [int(n) for n in re.findall(r"^### Step (\d+)", body, re.MULTILINE)]
    assert headings == list(range(len(headings)))
    flat = " ".join(body.split())
    # K-55 / K-31: requested V, the record's version and current_version.
    for key in ("details.requested_version", "details.current_version"):
        assert key in flat
    # The range is shown from the preview's display (its text, approximate
    # and notice), no longer from the plan's range.* keys.
    assert "display.range" in flat
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
    # Phase 2 (section H): the dialog reads the plan for routing only (the
    # screen comes from the preview), so the old Step 4 keys (range,
    # entries, attestation_groups, index, version_files) are not required.
    for key in ("kind", "unfinished_release", "executor", "changelog_file", "pending_consent",
                "warnings"):
        assert key in PLAN_KEYS and re.search(rf"`{key}[`. ]", flat), key
    for key in ("details", "steps", "version", "release"):
        assert key in RESULT_KEYS
    # (`attested` / `prepared` were the outcomes of the old Steps 6 / 8.)
    for outcome in ("completed", "noop", "refused", "failed", "preview",
                    "taken_over", "confirmed", "conflict"):
        assert outcome in OUTCOMES and f"`{outcome}`" in flat, outcome
    # K-111 / K-119: the routing keys are the plan's real ones (schema 3);
    # #876: schema 4 added changelog_file; schema 5 version.suggested and
    # pending_consent.
    from dct.workflows.release.plan import SCHEMA_VERSION

    assert SCHEMA_VERSION == 5 and "executor" in PLAN_KEYS and "changelog_file" in PLAN_KEYS
    assert "pending_consent" in PLAN_KEYS
    for key in ("executor.project", "executor.record"):
        assert f"`{key}" in flat, key


# ── /release: ONE executable workflow, skill = fallback (K-97, K-76, K-85) ──


RELEASE_DIALOGS = [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_dialog_runs_plan_prepare_apply_with_the_push_choice(text):
    """K-76 / K-97 F2 / F3, phase 2 (section H): plan → `prepare --preview`
    (the version sync, the files, the checks, the contract and the push
    target computed BEFORE the one question) → `apply --consent`. The push
    choice is part of the previewed content: "Change something" previews
    again with the owner's push target (all three of remote, branch, URL)
    or `--no-push`; the workflow pushes, so no "never pushes" survives."""
    body = text()
    flat = " ".join(body.split())
    calls = [(sub, flags) for group, sub, flags in _commands(body) if group == "release"]
    previews = [set(flags) for sub, flags in calls if sub == "prepare" and "--preview" in flags]
    assert any("--no-push" in f for f in previews), "no preview with --no-push"
    assert any({"--push-remote", "--push-branch", "--push-url"} <= f for f in previews), \
        "no preview with the complete push target"
    assert any("--consent" in flags for sub, flags in calls if sub == "apply")
    lowered = flat.lower()
    for stale in ("never pushes", "never pushed", "--confirm-version-files"):
        assert stale not in lowered, stale
    for key in ("display.checks", "display.contract", "display.sync", "output_dropped_bytes"):
        assert key in flat, key


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_dialog_commits_work_in_progress_inline(text):
    """K-97 F1: local uncommitted changes are committed IN this workflow on
    the owner's consent (the /commit procedure: stage, trailers from the
    final staged diff, hooks respected), then re-planned — never "run /commit
    yourself", never a split workflow."""
    flat = " ".join(text().split())
    assert re.search(r"work in progress", flat, re.IGNORECASE)
    assert "dct clog candidates --json" in flat  # the trailer selection of that commit
    # phase 2 (H1.3): `Changelog: <confirmed ids matching the diff>` — any placeholder
    assert re.search(r"Changelog: <[^>]+>", flat)
    assert "--no-verify" in flat  # named as forbidden
    assert re.search(r"in this (same )?workflow", flat, re.IGNORECASE)
    assert not re.search(r"(run|with|use|invoke) (the )?`?/commit`?( dialog| flow)? "
                         r"(first|yourself)", flat, re.IGNORECASE), "WIP sent out of the workflow"
    assert not re.search(r"/commit` flow \(its own commit\)", flat)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_dialog_respects_the_executor_and_the_stored_consent(text):
    """K-85 / K-111: the executor comes from the ONE plan result (`own` →
    hand over, nothing run) — no second read of the project setting;
    K-97 F4: resume executes the stored consent without a new approval;
    K-88: a conflict (exit 75) is shown, never retried by the dialog."""
    flat = " ".join(text().split())
    assert "dct project release-executor" not in flat  # K-111: one read, the plan
    assert "`executor.project" in flat and "`executor.record" in flat and "`own`" in flat
    assert "`schema_version` lower than 5" in flat  # the capability check (K-114, #876, schema 5)
    assert re.search(r"no new approval", flat, re.IGNORECASE)
    assert "dct release resume --json" in flat
    assert "`conflict`" in flat and "75" in flat and "details.observed_effects" in flat
    assert "details.lock" in flat and "never loop" in flat


# ── routing from ONE plan result (K-111, K-119) ─────────────────────────────

ROUTING_HEADER = "| the plan shows"
_CONDITION_RE = re.compile(r"`([a-z_.]+) == ([^`]+)`")


def routing_rows(body: str) -> list[dict]:
    """The dialog's routing table (Step 1) as data: [{conditions: [(path,
    value)], who, path, text}] in document order — the FIRST matching row
    routes. Conditions are the backticked `<plan path> == <JSON value>`
    terms of the first column."""
    import json

    assert ROUTING_HEADER in body, "the dialog has no routing table (K-111)"
    table = _section(body, ROUTING_HEADER, "\n\n")
    rows = []
    for line in table.splitlines()[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 3, line
        conditions = [(path, json.loads(value))
                      for path, value in _CONDITION_RE.findall(cells[0])]
        assert conditions, line
        rows.append({"conditions": conditions, "who": cells[1], "path": cells[2],
                     "text": " ".join(line.split())})
    return rows


def plan_value(plan: dict, path: str):
    """`plan` at the dotted `path`; a missing or null step → the sentinel
    `...` (no condition equals it)."""
    node = plan
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return ...
        node = node[key]
    return node


def route(body: str, plan: dict) -> dict:
    """The row of `body`'s routing table that routes `plan` — the first
    whose every condition holds; exactly the dialog's rule."""
    for row in routing_rows(body):
        if all(plan_value(plan, path) == value for path, value in row["conditions"]):
            return row
    raise AssertionError(f"no routing row matches kind={plan.get('kind')!r}, "
                         f"executor={plan.get('executor')!r}")


def test_release_routing_table_is_the_same_in_skill_and_fallback():
    skill, fallback = routing_rows(_skill("release")), routing_rows(_release_fallback())
    assert [r["conditions"] for r in skill] == [r["conditions"] for r in fallback]
    assert len(skill) == 5


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_dialog_routes_an_unfinished_release_before_the_project_setting(text):
    """K-111: FIRST "is there an unfinished release?" — routed by ITS stored
    progress owner and consent; the project setting decides only a NEW
    release; the decision comes from one plan result (no second read), and
    a changed setting alone never makes the dialog ask again."""
    body = text()
    flat = " ".join(body.split())
    rows = routing_rows(body)
    kinds = [dict(row["conditions"]).get("kind") for row in rows]
    assert set(kinds) == {"resume", "release"}
    assert kinds == sorted(kinds, key=lambda kind: kind != "resume")  # resume rows first
    for row, kind in zip(rows, kinds):
        paths = {path for path, _ in row["conditions"]}
        if kind == "resume":
            assert not any(p.startswith("executor.project") for p in paths), row["text"]
            assert any(p.startswith("executor.record.") for p in paths), row["text"]
            assert "Resume path" in row["path"], row["text"]
        else:
            assert paths == {"kind", "executor.project"}, row["text"]
    # the table sits in the routing step, before the legacy check and the WIP step
    for marker in ("dct release plan --json", "dct release status --version",
                   "dct clog candidates --json"):
        assert marker in body, marker
    _in_order(body, "dct release plan --json", ROUTING_HEADER,
              "dct release status --version", "dct clog candidates --json")
    assert re.search(r"`executor\.project`[^.]{0,160}ONLY[^.]{0,60}NEW release", flat)
    assert re.search(r"never ask[^.]{0,200}setting[^.]{0,80}changed", flat, re.IGNORECASE)
    assert re.search(r"ONE (plan )?result", flat)


def _reference_releasing() -> str:
    return _section(_reference(), "## Workflow use without duplication", "## Type triggers")


def test_reference_release_paragraph_only_points_to_the_skill_and_the_fallback():
    """#877 K-53 (replaces the K-111 pin, which made the reference — then the
    CLAUDE.md snippet — a third copy of the release sequence): the reference
    every Claude user is pointed at sends the assistant to `/release` and to
    the authoritative `/release` fallback; the CLI
    sequence, the routing keys and the flags live ONLY in those two. The ban
    is limited to the Releasing paragraph — the same flags may be described
    elsewhere."""
    body = _reference_releasing()
    flat = " ".join(body.split())
    assert "installed native skill" in flat and "complete workflow" in flat
    assert "docs/assistant-fallbacks.md" not in flat
    assert not _commands(body), f"the reference still carries release commands: {_commands(body)}"
    for copy in ("--plan-digest", "--proposal-digest", "--consent", "--push-", "--confirm",
                 "executor.", "attest", "takeover", "prepare", "apply"):
        assert copy not in flat, f"a piece of the CLI sequence is still copied here: {copy}"


def test_reference_promises_one_screen_only_for_the_typical_case():
    """"The typical case has one screen" — never one question in every
    exception (a changelog file to settle, an unreadable version, Change
    something add rounds)."""
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(_reference_releasing().split())):
        if re.search(r"\b(one|ONE|a single|single) (question|dialog|screen)\b", sentence):
            assert re.search(r"\btypical", sentence), f"an unconditional promise: {sentence}"
    assert "a report is not a consent request" in " ".join(_reference_releasing().split()).lower()


# ── resume without a second question (K-112) ────────────────────────────────


def _resume_path(body: str) -> str:
    """The dialog's resume section: `## Resume path` … `## Legacy path` in
    the skill, the `**Resume path**` … `**Legacy path**` paragraphs in the
    fallback."""
    marks = ("## Resume path", "## Legacy path") if "\n## Resume path" in body else (
        "**Resume path** (", "**Legacy path** —")
    start = body.index("\n" + marks[0])
    return body[start:body.index("\n" + marks[1], start)]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_resume_path_resumes_without_a_second_question(text):
    """K-112: the owner asked to continue and the scope is unchanged → show
    the stored state and resume AT ONCE on the stored consent; no
    Finish / Abandon / Stop menu. Abandon only on an explicit request with
    a reason; a new consent only for a concrete change of scope; an earlier
    stop stays binding; a record alone never starts automation; an error or
    a conflict is reported, never looped; commit and push finish in the
    same workflow."""
    section = _resume_path(text())
    flat = " ".join(section.split())
    assert not re.search(r"Finish release v|Abandon release v", flat), "the menu is back"
    assert not re.search(r"\bfinish\b[^.]{0,60}/[^.]{0,60}\babandon\b[^.]{0,60}/[^.]{0,30}\bstop\b",
                         flat, re.IGNORECASE), "the menu is back"
    assert not re.search(r"Otherwise (ask|the owner chooses)", flat)
    assert re.search(r"at once[^.]{0,200}stored consent|stored consent[^.]{0,200}at once", flat)
    assert re.search(r"no (second|new) question|without (a second|another) question|"
                     r"do not ask again", flat, re.IGNORECASE)
    _in_order(section, "dct release resume --json", "dct release abandon --version")
    assert re.search(r"[Aa]bandon[^.]{0,160}explicit[^.]{0,160}reason", flat)
    assert "details.outside_consent" in flat
    assert re.search(r"new[^.]{0,40}consent[^.]{0,200}concrete change of (the )?scope", flat)
    assert re.search(r"stop[^.]{0,160}binding", flat, re.IGNORECASE)
    assert re.search(r"record[^.]{0,120}never starts", flat)
    assert re.search(r"never loop", flat)
    assert re.search(r"commit[^.]{0,80}push[^.]{0,160}same workflow", flat)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_resume_path_reads_the_block_and_never_offers_finish_as_doable(text):
    """K-34 (#877): the plan reports `resume.blocked` (the predicate resume
    itself refuses with: the approved changelog file, the `restore`
    commands, the abandon way) — the dialog shows them and does not offer
    finishing the release while the block is known."""
    section = " ".join(_resume_path(text()).split())
    assert "resume.blocked" in section, "the Resume path does not read resume.blocked"
    at = section.index("resume.blocked")
    passage = section[max(0, at - 300):at + 900]
    assert "restore" in passage and "abandon" in passage, passage
    assert re.search(r"(never|not|no)\b[^.]{0,80}\b(finish|resume)", passage, re.IGNORECASE), \
        f"the dialog does not say that a blocked record is not finished by resume: {passage}"


def _dct_executes(text: str) -> str:
    """The Resume path's subsection of a release dct executes."""
    section = _resume_path(text)
    if "### A release dct executes" in section:
        return _section(section, "### A release dct executes", "### A record nobody")
    return _section(section, "*A release dct executes*", "*A record nobody")


def _runs_resume(sentence: str) -> bool:
    """A sentence that tells the dialog to run the resume (not one that
    forbids it)."""
    return bool(re.search(r"\b(run|runs|running|then)\b[^.]{0,40}\bresume\b|"
                          r"\bresume when\b|dct release resume", sentence, re.IGNORECASE)) \
        and not re.search(r"\b(do not|never|don't)\b[^.]{0,20}\b(run|resume)", sentence,
                          re.IGNORECASE)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_resume_path_every_branch_checks_the_block_first(text):
    """Delta review F6 (K-34): `resume.blocked` is a precondition of EVERY
    branch of the Resume path that runs resume — the "something else"
    branch (finish this release first, run resume on "finish it") included.
    Either the subsection states the check once for every branch BEFORE the
    branches, or each branch that runs resume names it itself."""
    section = " ".join(_dct_executes(text()).split())
    first_branch = section.index("asked to continue")
    precondition = any(
        "resume.blocked" in s and re.search(
            r"\b(every|each|any|all)\b[^.]{0,60}\b(branch|branches|case|cases|request|way)",
            s, re.IGNORECASE)
        for s in re.split(r"(?<=[.;])\s+", section[:first_branch]))
    if precondition:
        return
    branches = re.split(r"(?<=\.)\s+(?=[A-Z*-])|\s-\s\*\*", section)
    unguarded = [b for b in branches if _runs_resume(b) and "resume.blocked" not in b]
    assert unguarded == [], \
        f"Resume-path branches run resume without checking resume.blocked: {unguarded}"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_something_else_branch_is_gated_by_the_block(text):
    """Delta review F6: the concrete gap — the "something else" branch says
    the release is finished first and resumes on "finish it"; while
    `resume.blocked` is not null that resume is not doable."""
    section = " ".join(_dct_executes(text()).split())
    start = section.index("something else")
    end = min(i for i in (section.find(m, start) for m in
                          ("An earlier explicit", "**An earlier explicit")) if i != -1)
    branch = section[start:end]
    first_branch = section.index("asked to continue")
    global_gate = re.search(
        r"resume\.blocked[^.]{0,200}\b(every|each|any|all)\b[^.]{0,60}\b(branch|case|request)|"
        r"\b(every|each|any|all)\b[^.]{0,60}\b(branch|case|request)[^.]{0,200}resume\.blocked",
        section[:first_branch], re.IGNORECASE)
    assert "resume.blocked" in branch or global_gate, \
        f"the 'something else' branch resumes without the resume.blocked check: {branch}"


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("commit"), id="skill"),
    pytest.param(_commit_fallback, id="fallback"),
])
def test_commit_has_no_unreleased_count_release_threshold(text):
    """#877: a count of unreleased entries is no reason to suggest /release;
    only a STAGED version bump redirects there."""
    flat = " ".join(text().split())
    assert not re.search(r"(≥|>=)\s*10\b", flat), "the >= 10 entries threshold is still there"
    assert "release-ready" not in flat


def test_commit_keeps_the_staged_version_bump_redirect():
    flat = " ".join(_skill("commit").split())
    assert "/release" in flat
    assert "## [X.Y.Z]" in flat and "pyproject.toml" in flat


def test_track_update_records_the_reason_with_the_update():
    """#877: the reason of a transition is part of the update (one
    operation, one transaction — update_item(reason=…)), never a separate
    add_note that survives a refused update."""
    flat = " ".join(_skill("track-update").split())
    assert "BEFORE the `update_item` call" not in flat, \
        "the reason is still a separate add_note before the update"
    assert re.search(r"update_item\([^)]*reason", flat), "update_item(reason=…) is not used"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_resume_path_offers_the_takeover_as_a_new_consent(text):
    """R8d / K-119 M3: a `dct` record without a consent → the takeover
    preview, the owner approves THAT digest, then resume; an `own` record →
    the ledger path only (never the takeover)."""
    section = _resume_path(text())
    flat = " ".join(section.split())
    calls = [(sub, flags) for group, sub, flags in _commands(section) if group == "release"]
    takeovers = [set(flags) for sub, flags in calls if sub == "takeover"]
    assert any("--takeover-digest" not in f and "--json" in f for f in takeovers), "no preview"
    assert any({"--takeover-digest", "--confirm"} <= f for f in takeovers), "no confirmation"
    assert all({"--version"} <= f and ({"--push-remote", "--no-push"} & f) for f in takeovers)
    for key in ("details.consent", "details.missing_effects", "details.takeover_digest"):
        assert key in flat, key
    assert re.search(r"NEW consent", flat) and re.search(r"explicit(ly)? approv", flat)
    confirmed = section.index("--takeover-digest")
    assert "dct release resume --json" in section[confirmed:], "no resume after the takeover"
    assert re.search(r"`own`[^.]{0,200}never[^.]{0,80}takeover", flat)
    assert re.search(r"abandon_release`?[^.]{0,160}refus", flat)  # K-119 M6
    assert 'expected_executor="own"' in flat and "tag_proof" in flat  # the own tool's steps


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_dialog_shows_what_round_four_reports(text):
    """The skill side of R8a–R8d: the keys and flags the dialog shows or
    passes are the workflow's current ones."""
    body = text()
    flat = " ".join(body.split())
    # proof: the origin of a PR number is shown with it (K-116 H7)
    assert "pr_source" in flat and re.search(r"client[^.]{0,80}(declar|attest)", flat)
    # an entry never takes a version from the dialog (K-102)
    assert re.search(r"never takes a version", flat)
    # ONE push URL, the owner's choice among several, warnings. Phase 2: the
    # target is bound by the preview (`push.url` of the consent view), so the
    # owner's choice is passed to `prepare --preview`, not to apply.
    assert re.search(r"(details\.push_target|push)\.url`", flat) and "--push-url" in flat
    previews = [set(f) for g, s, f in _commands(body)
                if (g, s) == ("release", "prepare") and "--preview" in f]
    assert any("--push-url" in f for f in previews)
    assert re.search(r"`warnings`[^.]{0,200}(post-commit|cleanup|cleaned)", flat)
    # legacy path (R8d)
    for key in ("evidence.verification", "limited", "evidence.contract", "details.history",
                "--release-commit", "--evidence", "details.executor", "details.finish"):
        assert key in flat, key
    assert re.search(r"provenance", flat) and re.search(r"no (flag|waiver)", flat)
    assert re.search(r"never composed by (this dialog|the assistant)", flat)
    versions = [set(f) for g, s, f in _commands(body) if (g, s) == ("release", "version-files")]
    assert any("--set" in f and "--confirm" in f for f in versions)
    assert any("--none" in f and "--confirm" in f for f in versions)


_ALIAS_RE = re.compile(r"clog release(?![-\w])([^`\n]*)")


@pytest.mark.parametrize("text", [
    *RELEASE_DIALOGS,
    pytest.param(_reference, id="dct-reference"),
    pytest.param(lambda: (ROOT / "README.md").read_text(encoding="utf-8"), id="readme"),
])
def test_every_executable_clog_release_usage_respects_the_shared_consent(text):
    """K-96 D1 / K-117 / K-120: the alias commits, tags and pushes — every
    EXECUTABLE usage of it (a mention with an argument) carries the approved
    proposal (`--proposal-digest`) and `--confirm`; text that only NAMES the
    alias may do so without flags. F6: the release dialogs use apply."""
    body = text()
    for rest in _ALIAS_RE.findall(body):
        if not re.match(r"\s+[\w<\[…-]", rest):
            continue  # the bare name (architecture text)
        assert "--confirm" in rest, f"clog release{rest}"
        assert "--proposal-digest" in rest, f"clog release{rest}"
    assert "never pushed automatically" not in " ".join(body.split()).lower()


_PUSH_SET = {"--push-remote", "--push-branch", "--push-url"}


@pytest.mark.parametrize("text", [
    *RELEASE_DIALOGS,
    # (the dct reference shows no release command at all since K-53 —
    # test_reference_release_paragraph_only_points_to_the_skill_and_the_fallback)
    pytest.param(lambda: (ROOT / "README.md").read_text(encoding="utf-8"), id="readme"),
    pytest.param(lambda: _workflow_doc(), id="release-workflow"),
])
def test_every_executable_release_names_the_complete_push_target(text):
    """K-123: apply (and its alias) never selects the push target — every
    EXECUTABLE `dct release apply` / `dct clog release` usage the documents
    show (one that carries `--confirm`) names the complete approved target
    (`--push-remote`, `--push-branch`, `--push-url`) or `--no-push`. The
    no-flag form, which pushed to whatever the upstream was at apply time,
    is gone from all of them. `apply --consent C` is the exception by
    construction: the approved consent view binds the push target, and a
    push flag beside it is refused (consent_flag_conflict)."""
    body = text()
    executable = [set(flags) for group, sub, flags in _commands(body)
                  if (group, sub) in {("release", "apply"), ("clog", "release")}
                  and "--confirm" in flags]
    usages = [flags for flags in executable if "--consent" not in flags]
    assert executable, "no executable apply / alias usage"
    for flags in usages:
        # (the workflow doc's command table shows both alternatives in one line)
        assert _PUSH_SET <= flags or (
            "--no-push" in flags and not _PUSH_SET & flags), sorted(flags)
    for flags in executable:
        if "--consent" in flags:
            assert not (_PUSH_SET | {"--no-push"}) & flags, sorted(flags)
    # Phase 2: the release dialogs apply only `--consent` (the push target is
    # in the previewed content); the other documents still show the D/P path.
    if usages or "--consent" not in set().union(*executable):
        assert any(_PUSH_SET <= flags for flags in usages), "no pushing usage shown"


def test_cli_help_states_the_complete_push_target():
    """The same contract in `dct release apply --help` / `dct clog release
    --help` (the epilog and the three flags' help)."""
    cli = _cli()
    for parser in (cli["release"]["apply"], cli["clog"]["release"]):
        epilog = " ".join(parser.epilog.split())
        for flag in sorted(_PUSH_SET):
            assert flag in epilog
        assert "never selects" in epilog
        assert "--no-follow-tags" in epilog and "--recurse-submodules=no" in epilog
        helps = {a.option_strings[0]: a.help for a in parser._actions if a.option_strings}
        assert all("REQUIRED" in helps[flag] for flag in _PUSH_SET)


def test_release_dialogs_never_run_the_alias():
    for body in (_skill("release"), _release_fallback()):
        assert ("clog", "release") not in {(g, s) for g, s, _ in _commands(body)}


def _effects_detail_keys() -> set[str]:
    """Every key the effects layer can put into a result's `details`: the
    keyword arguments of its refusals and the string keys of its dict
    literals (refusal details, prepare / apply / conflict details)."""
    import ast

    from dct.workflows.release import effects

    tree = ast.parse(Path(effects.__file__).read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", "")
            if "refuse" in name.lower() or name == "ReleaseRefused":
                keys |= {k.arg for k in node.keywords if k.arg}
        elif isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and getattr(node.value, "id", None) == "details":
            keys.add(node.slice.value)
    # The CLI adds the refusal's message as `details.error` (cli.py, the
    # release result boundary) — it is part of every refusal.
    return (keys - {"details", "steps", "release"}) | {"error"}


def _refusal_table_keys(body: str) -> set[str]:
    """The `details.<key>` names in the first column of the dialog's
    refusal table (the rows after its `| details` header)."""
    table = _section(body, "| details", "\n\n")
    first_columns = [row.split("|")[1] for row in table.splitlines()[2:] if row.startswith("|")]
    return {key for col in first_columns for key in re.findall(r"`details\.(\w+)", col)}


REFUSAL_KEYS = {
    "error", "executor", "consent", "staged", "unstaged", "conflicted", "in_progress",
    "untracked", "unfinished", "unproven", "approved_digest", "derived_digest", "mismatch",
    "pending", "approved_script", "changed", "missing_blobs", "checkout_changed", "sync",
    "environment", "proposal_digest", "plan_digest", "foreign", "foreign_paths",
    "foreign_content", "git_error", "hook", "head", "tree", "expected_tree",
    "unpublished_commit", "expected_parent", "pinned_ref", "head_ref", "signed", "subject",
    "commit", "parents", "candidate", "approved_ids", "proven_ids", "git_version",
    "required_git_version", "foreign_staged", "index_lock", "push_target", "push_urls",
    "rewrite_rule", "url", "remote_tags", "remote_branch", "outside_consent", "lock",
    "holder", "reason", "process_groups", "conflict", "observed_effects", "record_state",
    "recorded", "changelog_file", "approved",
}
# Keys the table named before round 4 that the workflow no longer reports
# under that meaning — a row for them would describe a state that cannot occur.
STALE_REFUSAL_KEYS = {"url_now", "proposal", "contract"}


def test_release_refusal_table_is_the_same_in_skill_and_fallback_and_from_the_code():
    """One refusal table (F6 / F7): the same `details` keys in the skill and
    the authoritative fallback, every one a key the effects layer really
    produces, covering the refusal families of R7c."""
    skill, fallback = (_refusal_table_keys(_skill("release")),
                       _refusal_table_keys(_release_fallback()))
    assert skill == fallback, sorted(skill ^ fallback)
    code = _effects_detail_keys()
    assert skill <= code, sorted(skill - code)
    assert REFUSAL_KEYS <= skill, sorted(REFUSAL_KEYS - skill)
    assert REFUSAL_KEYS <= code, sorted(REFUSAL_KEYS - code)
    assert not STALE_REFUSAL_KEYS & skill, sorted(STALE_REFUSAL_KEYS & skill)
    assert "url_now" not in code  # R8c S11: dropped with the table's move to push_urls


def _refusal_rows(body: str) -> dict[str, str]:
    """{details key: its table row} of the dialog's refusal table."""
    table = _section(body, "| details", "\n\n")
    rows = {}
    for row in table.splitlines()[2:]:
        for key in re.findall(r"`details\.(\w+)", row.split("|")[1]):
            rows.setdefault(key, " ".join(row.split()))
    return rows


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_release_refusal_rows_tell_the_round_four_actions(text):
    """R8c: what each new refusal means and what the owner does next."""
    rows = _refusal_rows(text())
    missing = {"hook", "unpublished_commit", "parents", "candidate", "subject",
               "pinned_ref", "signed", "proposal_digest", "plan_digest", "push_urls",
               "rewrite_rule", "url", "reason", "holder", "process_groups", "git_version",
               "required_git_version",
               "environment", "approved_ids", "proven_ids"} - set(rows)
    assert not missing, sorted(missing)
    # (1) a hook that refuses or changes the commit: NOTHING is published —
    # fix the hook or put the files back, then resume; no reset.
    hook = rows["unpublished_commit"]
    assert rows["hook"] == hook == rows["subject"] == rows["pinned_ref"] == rows["signed"]
    assert re.search(r"nothing (is|was) published", hook, re.IGNORECASE)
    assert re.search(r"then resume", hook) and not re.search(r"reset", hook, re.IGNORECASE)
    # (2) a leftover commit at HEAD: reported exactly; the hint is
    # conditional and the owner decides — never an automatic reset.
    leftover = rows["parents"]
    assert rows["candidate"] == leftover and "`details.commit`" in leftover
    assert re.search(r"\bIF\b", leftover) and re.search(r"owner decides", leftover)
    assert re.search(r"never resets", leftover)
    # (3) the alias row is gone; a new release without an approved proposal
    # — phase 2: the way back is a new preview (the old Step 8 is gone)
    assert rows["proposal_digest"] == rows["plan_digest"]
    assert re.search(r"prepare --preview|preview again", rows["proposal_digest"])
    # (4) push: several URLs → the owner picks ONE; a rewrite rule
    assert "--push-url" in rows["push_urls"] and re.search(r"owner (picks|chooses)",
                                                           rows["push_urls"])
    assert rows["rewrite_rule"] == rows["url"]
    # (5) the lock: null + reason = register / correct the path, not "wait";
    # leftover processes → the owner decides, never a loop
    assert rows["reason"] != rows["holder"]
    assert re.search(r"register|correct the path", rows["reason"])
    assert not re.search(r"\bwait\b", rows["reason"])
    assert re.search(r"never loop", rows["process_groups"])
    assert re.search(r"owner decides", rows["process_groups"])
    # (6) git version, the sync environment, attest ids
    assert rows["git_version"] == rows["required_git_version"]
    assert "environment" in rows and rows["approved_ids"] == rows["proven_ids"]


def test_release_skill_and_fallback_keep_the_same_step_order():
    """F6 / F7: the skill and the fallback walk the SAME steps in the same
    order — probe, routing from the plan (K-111), the report-only status
    read, the drafted work-in-progress message, then (phase 2, section H)
    the preview, the one screen's apply --consent, Stop's discard, resume.
    An AUXILIARY documentation test (K-120): routing and continuation are
    pinned on the real workflow in tests/test_release_dialog_routing.py and
    tests/test_release_skill_one_dialog.py."""
    markers = ("dct release plan --json", ROUTING_HEADER, "dct release status --version",
               "dct clog candidates --json", "dct release prepare --preview",
               "dct release prepare --discard-preview",
               "dct release resume --json", "dct release abandon --version",
               "dct release takeover --version", "dct release reconstruct --version",
               "dct release version-files")
    for body in (_skill("release"), _release_fallback()):
        needed = (*markers, "dct release apply --consent")
        assert not [m for m in needed if m not in body], [m for m in needed if m not in body]
        _in_order(body, *markers)
        # the screen's apply (the Finish route may name it before the preview)
        preview = body.index("dct release prepare --preview")
        assert body.find("dct release apply --consent", preview) != -1
        assert not re.search(r"dct release plan --version \S+ --entries", body)


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
    _in_order(commit, "Step 4: Stage", "Step 5: Select", "Step 6: Commit")
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


