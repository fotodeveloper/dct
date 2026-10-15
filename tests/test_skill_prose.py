"""Prose contracts of the packaged skills that a later edit could silently
undo (#615 / K-27, rewritten for K-73 (d1) of the #877 dogfood): the draft a
handoff approval is about, and the handoffs /pickup asks about, travel INSIDE
the dialog — as option previews — never as message text before it. Text
written before a dialog can land in the thinking block, which the owner sees
only as a one-line summary; in the dogfood that made every "… shown above?"
an approval of text nobody had seen."""

import re
import shlex
from pathlib import Path

import pytest

from dct.assets import skills_dir
from dct.integrations import codex


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


# --- /triage (#930): the ledger is the single source of a triage page --------

TRIAGE_DOCS = ("skill", "codex", "fallback")


def _triage_fallback() -> str:
    text = (Path(__file__).resolve().parent.parent / "docs" / "assistant-fallbacks.md") \
        .read_text(encoding="utf-8")
    start = text.index("## `/triage` fallback")
    end = text.find("\n## ", start + 1)
    end = len(text) if end < 0 else end
    return text[start:end]


def _triage_doc(which: str) -> str:
    if which == "skill":
        return _skill("triage")
    if which == "codex":
        return (codex.ASSETS / "codex-triage.md").read_text(encoding="utf-8")
    return _triage_fallback()


def _calls(text: str) -> list[tuple[str, list[str]]]:
    """Every `name(args…)` call of a snake_case name, with the argument names
    it spells out (`key=value` → key, a bare identifier → itself)."""
    out = []
    for m in re.finditer(r"\b(?:mcp__dct__)?([a-z]+(?:_[a-z]+)+)\(", text):
        depth, i, args, cur = 1, m.end(), [], ""
        while i < len(text) and depth:
            ch = text[i]
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>":
                depth -= 1
            if depth == 1 and ch == ",":
                args.append(cur)
                cur = ""
            elif depth:
                cur += ch
            i += 1
        args.append(cur)
        names = []
        for arg in (a.strip() for a in args):
            key = arg.split("=", 1)[0].strip() if "=" in arg else arg
            if re.fullmatch(r"[a-z_]+", key):
                names.append(key)
        out.append((m.group(1), names))
    return out


def _mcp_tools() -> dict:
    import asyncio

    from dct.mcp import server
    return {t.name: set((t.input_schema or {}).get("properties", {}))
            for t in asyncio.run(server.mcp.list_tools())}


@pytest.mark.parametrize("which", TRIAGE_DOCS)
def test_triage_names_only_real_mcp_tools_and_parameters(which):
    """Every tool call the workflow spells out is a registered MCP tool, and
    every parameter it names is one of that tool's — a renamed parameter would
    otherwise send the model into a refusal it cannot explain."""
    tools = _mcp_tools()
    calls = _calls(_triage_doc(which))
    assert {"open_triage", "record_findings", "suggest_findings", "close_triage"} \
        <= {name for name, _ in calls}, which
    for name, params in calls:
        assert name in tools, f"{which}: {name} is not an MCP tool"
        unknown = set(params) - tools[name]
        assert not unknown, f"{which}: {name} has no parameter(s) {sorted(unknown)}"


def _cli_commands(text: str) -> list[str]:
    flat = _flat(text)
    # A bare `dct triage page` is a mention; a command carries its arguments.
    return [c for c in re.findall(r"`(dct triage [^`]+)`", flat)
            if "…" not in c and len(c.split()) > 3]


@pytest.mark.parametrize("which", ("skill", "fallback"))
def test_triage_cli_commands_as_written_parse(which):
    """The CLI lines the workflow gives the user (or runs itself) are accepted
    by the real parser once the placeholders are filled in."""
    from dct import cli

    commands = _cli_commands(_triage_doc(which))
    verbs = {c.split()[2] for c in commands}
    assert {"page", "decide", "accept", "close"} <= verbs, (which, verbs)
    parser = cli.build_parser()
    for command in commands:
        filled = re.sub(r"<[^>]*>", "x", command).replace("[", "").replace("]", "")
        argv = shlex.split(filled)[1:]
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse reports an unknown flag this way
            pytest.fail(f"{which}: {command!r} does not parse ({exc})")


def test_triage_skill_keeps_decisions_the_owners_own():
    flat = _flat(_skill("triage"))
    for rule in ("Decisions come only from the owner's own text.",
                 "An agent's proposals never become decisions without the owner's acceptance.",
                 "Note and reject never create items.",
                 "never repair his text yourself"):
        assert rule in flat, rule
    # The audit fields name the owner, the proposer is bound to what he saw.
    assert 'decided_by="owner"' in flat
    assert "expected_by=<the SUGGESTED.by of the page he saw>" in flat
    assert "suggestions=<SUGGESTED.answers>" in flat


def test_triage_skill_never_repeats_a_ledger_write_on_resume():
    flat = _flat(_skill("triage"))
    assert "Every mode starts by reading the cycle" in flat
    assert "never record them again" in flat
    assert "never decide, accept or close it again" in flat
    assert '"ledger closed; publication pending"' in flat


def test_triage_page_is_always_derived_from_the_ledger():
    for which in TRIAGE_DOCS:
        flat = _flat(_triage_doc(which))
        assert "export_triage" in flat and "dct triage page" in flat, which
        assert re.search(r"never publish[^.]*another agent edited|"
                         r"another agent edited is never published", flat), which
    flat = _flat(_skill("triage"))
    assert "never `--force` onto any other path" in flat


def test_triage_close_approves_no_commit_push_or_release():
    for which in TRIAGE_DOCS:
        flat = _flat(_triage_doc(which)).lower()
        assert re.search(r"commit, (a )?push", flat), which
        assert "not approve" in flat or "not approved" in flat, which


def test_triage_promotes_backlog_only_after_the_owners_yes():
    flat = _flat(_skill("triage"))
    assert re.search(r'"Promote to dct" \(recommended\) — its preview: the screen', flat)
    assert "Neither deciding nor closing creates an item by itself" in flat


def _call_args(text: str, tool: str) -> list[str]:
    """The raw argument text of every `tool(…)` call in `text` (flattened)."""
    flat = _flat(text)
    out = []
    for m in re.finditer(r"\b(?:mcp__dct__)?" + tool + r"\(", flat):
        depth, i = 1, m.end()
        while i < len(flat) and depth:
            depth += {"(": 1, ")": -1}.get(flat[i], 0)
            i += 1
        out.append(flat[m.end():i - 1])
    return out


@pytest.mark.parametrize("which", TRIAGE_DOCS)
def test_triage_acceptance_passes_the_digest_of_the_page_the_owner_saw(which):
    """K-32: every acceptance names expected_digest, and the workflow says
    where it comes from — the digest recorded for the published page — and
    that a fresh one is never used."""
    text = _triage_doc(which)
    calls = _call_args(text, "accept_suggestions")
    assert calls, which
    for args in calls:
        assert "expected_digest=" in args and "expected_by=" in args, (which, args)
    flat = _flat(text)
    assert re.search(r"never a fresh one", flat, re.IGNORECASE), which
    assert "digest=<hex>" in flat, which  # the prefill records what the build printed
    for command in _cli_commands(text) if which != "codex" else []:
        if command.startswith("dct triage accept"):
            assert "--expected-digest" in command, (which, command)


@pytest.mark.parametrize("which", TRIAGE_DOCS)
def test_triage_close_promotes_only_the_closing_cycles_unlinked_backlog(which):
    """K-35: the backlog listing at close is scoped to the closing cycle and
    complete (limit=0); a finding already linked to an item is skipped."""
    backlog = [a for a in _call_args(_triage_doc(which), "list_findings")
               if 'decision="backlog"' in a]
    assert backlog, which
    for args in backlog:
        assert re.search(r"\bcycle_id=<[^>]*\bcycl", args), (which, args)
        assert "project=<slug>" in args and "limit=0" in args, (which, args)
    assert "item_id" in _flat(_triage_doc(which)), which


@pytest.mark.parametrize("which", TRIAGE_DOCS)
def test_triage_close_needs_the_owners_answers_and_promises_no_fix(which):
    """K-41: no close without the owner's recorded answers and an empty
    `undecided`; discuss stays open in substance; a closed triage is not a
    done fix; open fix / verify findings get an explicit follow-up; skipped
    cards are reported; descriptions follow the conversation's language."""
    flat = _flat(_triage_doc(which)).lower()
    assert re.search(r"close only after (a real owner step|the owner's recorded)", flat), which
    assert "never close on" in flat or "never on proposals alone" in flat, which
    assert "open in substance" in flat, which
    assert re.search(r"(does not|it does not) mean any fix is done", flat), which
    assert re.search(r"follow-up[^.]*(every|each) open fix / verify finding|"
                     r"every open fix / verify finding[^.]*follow-up", flat), which
    assert "skipped" in flat, which
    assert "language of the conversation" in flat, which


@pytest.mark.parametrize("which", TRIAGE_DOCS)
def test_triage_purpose_check_keeps_the_reviewers_values_visible(which):
    """dogfood-930 K-2 (rubric §3): the purpose and use check may lower O and
    drop `security`, but the card's context keeps the reviewer's original O
    and type and the reason for the change — the owner decides on a card
    whose correction he can see."""
    flat = _flat(_triage_doc(which))
    assert "trust model" in flat, which
    assert "the reviewer's original O and type" in flat, which
    assert "the reason for the change" in flat, which
