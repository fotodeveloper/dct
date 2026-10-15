"""K-22 (#917): the gate-checkpoint policy ships with the package.

A client running outside the dct repository sees only what the package gives
it: the MCP tool descriptions and the refusal message. So the policy (what a
gate is, the dogfood rule and its exceptions) lives in the add_checkpoint
description, and the refusal of a terminal transition names every gate it
lists and says how to handle it — never "See CLAUDE.md", a file the client
does not have.
"""

import asyncio
import json
import re
import unicodedata
from pathlib import Path

import pytest
from conftest import run_cli

from dct.core.checkpoints import add_checkpoint, get_checkpoint
from dct.core.items import (
    GATE_REFUSAL_LISTED,
    GATE_REFUSAL_TEXT_CHARS,
    _gate_entry,
    _gate_literal,
    create_item,
    get_item,
    resolve_item,
    update_item,
)
from dct.core.projects import create_project
from dct.core.text import preview

PREFIX = re.compile(r"^Cannot resolve item #(\d+): (\d+) gate checkpoint\(s\) pending")


@pytest.fixture
def item(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    return create_item(engine, "proj-a", "feature", "Gated feature")


def _refusal(engine, item_id) -> str:
    with pytest.raises(ValueError) as exc:
        resolve_item(engine, item_id)
    return str(exc.value)


def _gates(engine, item_id, n, text=lambda i: f"Gate condition number {i}"):
    kinds = ("dogfood", "security", "approval", "review", "legal")
    return [
        add_checkpoint(engine, item_id, text(i), kind=kinds[i % len(kinds)])
        for i in range(n)
    ]


def _named(msg: str, gate: dict) -> bool:
    return f"#{gate['id']} [{gate['kind']}]" in msg


_DECODER = json.JSONDecoder()
_KIND_TOKEN = re.compile(r"[A-Za-z0-9_.-]+")


def _entries(msg: str) -> list[tuple[int, str, str]]:
    """Parse the refusal's gate list back into (id, kind, text) — the
    grammar a reader relies on: `#<id> [<kind>] <text>` entries joined by
    "; ", kind a bare token or a JSON string, text always a JSON string.
    Anything else (an injected fake entry, a stray quote) breaks the parse."""
    m = PREFIX.match(msg)
    assert m, msg
    rest = msg[m.end():]
    assert rest.startswith(" — "), rest
    pos, out = 3, []
    while True:
        head = re.compile(r"#(\d+) \[").match(rest, pos)
        assert head, rest[pos:]
        pos = head.end()
        if rest[pos] == '"':
            kind, pos = _DECODER.raw_decode(rest, pos)
        else:
            tok = _KIND_TOKEN.match(rest, pos)
            assert tok, rest[pos:]
            kind, pos = tok.group(0), tok.end()
        assert rest.startswith("] ", pos), rest[pos:]
        text, pos = _DECODER.raw_decode(rest, pos + 2)
        out.append((int(head.group(1)), kind, text))
        if rest.startswith("; ", pos):
            pos += 2
            continue
        return out


def _shown(text: str) -> str:
    """What the refusal shows of a gate text: whitespace runs collapsed to
    one space, then the GATE_REFUSAL_TEXT_CHARS preview."""
    return preview(" ".join(text.split()), GATE_REFUSAL_TEXT_CHARS)


def _one_line(msg: str) -> bool:
    return len(msg.splitlines()) == 1 and not any(
        unicodedata.category(ch) in {"Cc", "Zl", "Zp"} for ch in msg
    )


# ── the refusal message (core) ────────────────────────────────────────────────


def test_bound_is_a_sane_positive_number():
    assert 1 <= GATE_REFUSAL_LISTED <= 20
    assert GATE_REFUSAL_TEXT_CHARS >= 20


def test_refusal_at_exactly_the_bound_names_every_gate_and_has_no_tail(engine, item):
    gates = _gates(engine, item["id"], GATE_REFUSAL_LISTED)
    msg = _refusal(engine, item["id"])
    m = PREFIX.match(msg)
    assert m and int(m.group(1)) == item["id"] and int(m.group(2)) == len(gates)
    for g in gates:
        assert _named(msg, g), g
        assert preview(g["text"], GATE_REFUSAL_TEXT_CHARS) in msg
    assert "more)" not in msg


def test_refusal_beyond_the_bound_lists_the_bound_and_counts_the_rest(engine, item):
    gates = _gates(engine, item["id"], GATE_REFUSAL_LISTED + 1)
    msg = _refusal(engine, item["id"])
    assert int(PREFIX.match(msg).group(2)) == GATE_REFUSAL_LISTED + 1
    for g in gates[:GATE_REFUSAL_LISTED]:
        assert _named(msg, g), g
    assert not _named(msg, gates[-1])
    assert gates[-1]["text"] not in msg
    assert _tail(msg, item["id"]) == 1


TAIL = re.compile(r"\(\+(\d+) more — get_item\((\d+)\) or `dct show (\d+)` lists them all\)")


def _tail(msg: str, item_id: int) -> int | None:
    """The omitted-gates count of the refusal's tail, which must also name a
    complete read command — MCP get_item and CLI `dct show` — for THIS item."""
    found = TAIL.findall(msg)
    if not found:
        return None
    assert len(found) == 1, msg
    count, mcp_id, cli_id = found[0]
    assert int(mcp_id) == item_id and int(cli_id) == item_id, msg
    return int(count)


def test_refusal_far_beyond_the_bound_counts_every_unlisted_gate(engine, item):
    _gates(engine, item["id"], GATE_REFUSAL_LISTED + 7)
    msg = _refusal(engine, item["id"])
    assert _tail(msg, item["id"]) == 7
    assert len(_entries(msg)) == GATE_REFUSAL_LISTED


def test_no_pending_gate_means_no_refusal(engine, item):
    add_checkpoint(engine, item["id"], "Plain step", kind="task")
    assert resolve_item(engine, item["id"])["status"] == "resolved"


@pytest.mark.parametrize("n", (1, GATE_REFUSAL_LISTED - 1, GATE_REFUSAL_LISTED))
def test_refusal_within_the_bound_has_no_tail(engine, item, n):
    gates = _gates(engine, item["id"], n)
    msg = _refusal(engine, item["id"])
    assert _tail(msg, item["id"]) is None and "more" not in msg
    assert [e[0] for e in _entries(msg)] == [g["id"] for g in gates]


@pytest.mark.parametrize("extra", (1, 2, 30))
def test_listed_plus_omitted_is_the_pending_count(engine, item, extra):
    gates = _gates(engine, item["id"], GATE_REFUSAL_LISTED + extra)
    msg = _refusal(engine, item["id"])
    listed = _entries(msg)
    assert [e[0] for e in listed] == [g["id"] for g in gates[:GATE_REFUSAL_LISTED]]
    assert len(listed) + _tail(msg, item["id"]) == int(PREFIX.match(msg).group(2))


def test_the_read_path_the_tail_names_lists_every_pending_gate_mcp(srv, engine):
    item = srv.create_item("feature", "Many gates", project="test")
    gates = _gates(engine, item["id"], GATE_REFUSAL_LISTED + 3)
    msg = srv.resolve_item(item["id"])["error"]
    assert _tail(msg, item["id"]) == 3
    shown = {(c["id"], c["kind"], c["text"]) for c in srv.get_item(item["id"])["checkpoints"]
             if c["status"] == "pending"}
    assert {(g["id"], g["kind"], g["text"]) for g in gates} <= shown


def test_the_read_path_the_tail_names_lists_every_pending_gate_cli(
    cli_engine, monkeypatch, capsys,
):
    item = create_item(cli_engine, "proj-a", "feature", "Many gates")
    gates = _gates(cli_engine, item["id"], GATE_REFUSAL_LISTED + 3)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["resolve", str(item["id"])])
    assert _tail(capsys.readouterr().err.strip(), item["id"]) == 3
    run_cli(monkeypatch, ["show", str(item["id"])])
    out = capsys.readouterr().out
    for g in gates:
        assert re.search(rf"#?{g['id']}\b[^\n]*\b{re.escape(g['kind'])}\b[^\n]*{re.escape(g['text'])}",
                         out), (g, out)


# ── K-62: every listed gate is ONE unambiguous one-line entry ────────────────


def test_a_text_posing_as_a_second_entry_stays_inside_its_own_entry(engine, item):
    forged = 'Run "make check"; #9 [review] "fake'
    gate = add_checkpoint(engine, item["id"], forged, kind="dogfood")
    msg = _refusal(engine, item["id"])
    assert _entries(msg) == [(gate["id"], "dogfood", forged)]
    assert _one_line(msg)


# Every control character the refusal must keep as escaped data: C0 (NUL,
# bell, ESC, tab, newline), DEL, C1 (U+0085 NEL) and the line / paragraph
# separators. NUL is checked on the formatter itself (K-79): PostgreSQL text
# cannot store it, so a DB-backed case would fail on the write, not on the
# formatting — the storage cases below use only what both backends accept.
_CONTROL_CHARS = ("\x00", "\x07", "\x1b", "\t", "\n", "\x7f", "\x85", "\u2028", "\u2029")
_ALL_CONTROLS = "nul\x00 bell\x07 esc\x1b[31m tab\tnl\n del\x7f c1\x85 ls\u2028 ps\u2029 end"


def _one_entry_refusal(entry: str) -> str:
    """A refusal carrying exactly one formatted entry — the shape _entries parses."""
    return f"Cannot resolve item #1: 1 gate checkpoint(s) pending — {entry}"


@pytest.mark.parametrize("value", (*_CONTROL_CHARS, _ALL_CONTROLS, 'q"\\\x00"; #9 [review] "x'))
def test_gate_literal_keeps_every_control_char_as_one_line_data(value):
    shown = _gate_literal(value)
    assert _one_line(shown), repr(shown)
    decoded, end = _DECODER.raw_decode(shown)
    assert (decoded, end) == (value, len(shown))


@pytest.mark.parametrize("value", (*_CONTROL_CHARS, _ALL_CONTROLS))
def test_gate_entry_with_control_chars_in_kind_and_text_is_one_unambiguous_entry(value):
    entry = _gate_entry({"id": 7, "kind": f"k{value}k", "text": f"check {value} done"})
    msg = _one_entry_refusal(entry)
    assert _entries(msg) == [(7, f"k{value}k", _shown(f"check {value} done"))]
    assert _one_line(msg)


@pytest.mark.parametrize(
    "text",
    (
        'He said "ship it"',
        "C:\\path\\to\\file \\\" tricky",
        "bell\x07 esc\x1b[31m del\x7f c1\x85 ls\u2028 ps\u2029 tab\tend",
        "zażółć gęślą jaźń — “quotes” ‘too’",
    ),
)
def test_gate_text_with_quotes_backslashes_and_control_chars_is_data(engine, item, text):
    gate = add_checkpoint(engine, item["id"], text, kind="review")
    msg = _refusal(engine, item["id"])
    assert _entries(msg) == [(gate["id"], "review", _shown(text))]
    assert _one_line(msg)


@pytest.mark.parametrize(
    "kind",
    ('multi\nline', 'q"uote', "back\\slash", 'dogfood] "x"; #9 [review', "ctl\x1b\x85", "zgoda-prawna"),
)
def test_an_odd_gate_kind_is_shown_exactly_on_one_line(engine, item, kind):
    gate = add_checkpoint(engine, item["id"], "Some check", kind=kind)
    msg = _refusal(engine, item["id"])
    assert _entries(msg) == [(gate["id"], kind, "Some check")]
    assert _one_line(msg)
    # Presentation only: the stored kind and condition are untouched.
    stored = get_checkpoint(engine, gate["id"])
    assert (stored["kind"], stored["text"]) == (kind, "Some check")


def test_the_listed_id_is_the_record_id(engine, item):
    gates = [add_checkpoint(engine, item["id"], f"#{999 + i} looks like an id", kind="approval")
             for i in range(3)]
    assert [e[0] for e in _entries(_refusal(engine, item["id"]))] == [g["id"] for g in gates]


def test_long_and_multiline_gate_text_is_compacted_to_one_line(engine, item):
    long_text = "Owner validates\nthe whole   flow " + "x" * 400
    gate = add_checkpoint(engine, item["id"], long_text, kind="dogfood")
    msg = _refusal(engine, item["id"])
    assert "\n" not in msg
    assert "x" * 400 not in msg
    assert _named(msg, gate)
    assert "Owner validates the whole flow" in msg
    assert "…" in msg


def test_refusal_lists_only_pending_gates(engine, item):
    from dct.core.checkpoints import set_status

    done = add_checkpoint(engine, item["id"], "Already validated", kind="dogfood")
    set_status(engine, done["id"], "done")
    rejected = add_checkpoint(engine, item["id"], "Not applicable", kind="security")
    set_status(engine, rejected["id"], "rejected")
    add_checkpoint(engine, item["id"], "Plain step", kind="task")
    add_checkpoint(engine, item["id"], "Mirror", kind="cc-task")
    pending = add_checkpoint(engine, item["id"], "Code review", kind="review")
    msg = _refusal(engine, item["id"])
    assert int(PREFIX.match(msg).group(2)) == 1
    assert _named(msg, pending)
    for cp_text in ("Already validated", "Not applicable", "Plain step", "Mirror"):
        assert cp_text not in msg


def test_refusal_says_how_to_handle_gates_in_client_neutral_terms(engine, item):
    _gates(engine, item["id"], 1)
    msg = _refusal(engine, item["id"])
    assert "complete_checkpoint" in msg
    assert "reject_checkpoint" in msg
    assert "dct checkpoint done <id>" in msg
    assert "dct checkpoint reject <id>" in msg
    assert "user's decision" in msg


def test_refusal_never_points_at_claude_md(engine, item):
    _gates(engine, item["id"], GATE_REFUSAL_LISTED + 1)
    assert "CLAUDE.md" not in _refusal(engine, item["id"])


@pytest.mark.parametrize("status", ("resolved", "wont_fix"))
def test_update_item_and_resolve_item_refuse_with_the_same_message(engine, item, status):
    _gates(engine, item["id"], GATE_REFUSAL_LISTED + 2)
    with pytest.raises(ValueError) as via_update:
        update_item(engine, item["id"], status=status)
    with pytest.raises(ValueError) as via_resolve:
        resolve_item(engine, item["id"], status=status)
    assert str(via_update.value) == str(via_resolve.value)
    assert PREFIX.match(str(via_update.value))


# ── K-63: a missing or soft-deleted item is "not found", never a gate refusal ─


def _ledger(engine, item_id):
    from dct.core.checkpoints import list_checkpoints
    from dct.db import items

    with engine.connect() as conn:
        row = conn.execute(items.select().where(items.c.id == item_id)).mappings().one()
    return dict(row), [dict(c) for c in list_checkpoints(engine, item_id)]


@pytest.mark.parametrize("status", ("resolved", "wont_fix"))
def test_a_soft_deleted_item_with_gates_is_not_found_and_nothing_is_written(engine, item, status):
    from dct.core.items import delete_item

    _gates(engine, item["id"], 2)
    assert delete_item(engine, item["id"])
    before = _ledger(engine, item["id"])
    assert resolve_item(engine, item["id"], status=status) is None
    assert update_item(engine, item["id"], status=status) is None
    assert _ledger(engine, item["id"]) == before


@pytest.mark.parametrize("status", ("resolved", "wont_fix"))
def test_mcp_resolve_and_update_agree_on_missing_deleted_and_gated_items(srv, engine, status):
    from dct.core.items import delete_item

    gated = srv.create_item("feature", "Live gated", project="test")
    gone = srv.create_item("feature", "Deleted gated", project="test")
    for it in (gated, gone):
        srv.add_checkpoint(it["id"], "Owner validates", kind="dogfood")
    delete_item(engine, gone["id"])
    for item_id in (gone["id"], 987654):
        expected = {"error": f"Item #{item_id} not found"}
        assert srv.resolve_item(item_id, status=status) == expected
        assert srv.update_item(item_id, status=status) == expected
    via_resolve = srv.resolve_item(gated["id"], status=status)
    assert PREFIX.match(via_resolve["error"])
    assert srv.update_item(gated["id"], status=status) == via_resolve


# ── the message's instructions actually work (round trip through the CLI) ────


@pytest.mark.parametrize("verb, expected", (("done", "done"), ("reject", "rejected")))
def test_cli_command_named_in_the_refusal_unblocks_the_item(
    cli_engine, monkeypatch, capsys, verb, expected,
):
    item = create_item(cli_engine, "proj-a", "feature", "CLI gated")
    gate = add_checkpoint(cli_engine, item["id"], "Owner validates", kind="dogfood")
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["resolve", str(item["id"])])
    err = capsys.readouterr().err
    assert _named(err, gate)
    # The command template the message gives, filled with the gate's id.
    template = re.search(rf"dct (checkpoint {verb}) <id>", err)
    assert template, err
    argv = template.group(1).split() + [str(gate["id"])]
    run_cli(monkeypatch, argv)
    assert get_checkpoint(cli_engine, gate["id"])["status"] == expected
    run_cli(monkeypatch, ["resolve", str(item["id"])])
    assert get_item(cli_engine, item["id"])["status"] == "resolved"


# ── MCP layer ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ("resolved", "wont_fix"))
def test_mcp_resolve_and_update_return_the_core_message(srv, engine, status):
    item = srv.create_item("feature", "G", project="test")
    for i in range(GATE_REFUSAL_LISTED + 1):
        srv.add_checkpoint(item["id"], f"gate {i}", kind="dogfood")
    with pytest.raises(ValueError) as core:
        resolve_item(engine, item["id"], status=status)
    assert srv.resolve_item(item["id"], status=status) == {"error": str(core.value)}
    assert srv.update_item(item["id"], status=status) == {"error": str(core.value)}


def _descriptions() -> dict[str, str]:
    from dct.mcp.server import mcp

    return {t.name: t.description or "" for t in asyncio.run(mcp.list_tools())}


# The policy's load-bearing clauses (K-54, K-64). Each is a regex over the
# whitespace-flattened text: wording may change, the rule may not. A gate
# whose condition is a person's act is closed only on that person's word; a
# rejection only on the user's decision; consent already given is not re-asked.
POLICY_CLAUSES = {
    "gate definition": re.compile(
        r"gate is any kind (?:except|but|other than) '?task'? (?:and|or|/) '?cc-task'?",
        re.IGNORECASE),
    "unknown kinds gate": re.compile(r"unknown kinds? (?:are|is) (?:treated as )?gates?",
                                     re.IGNORECASE),
    "complete only when met": re.compile(
        r"complet\w*[^.;]{0,60}only (?:when|once|after) (?:its|the|that) (?:recorded )?condition"
        r"[^.;]{0,20}(?:is )?met", re.IGNORECASE),
    "human act needs that person's confirmation": re.compile(
        r"person's act[^.]{0,160}dogfood[^.]{0,120}approval[^.]{0,120}"
        r"only on that person's confirmation", re.IGNORECASE),
    "no stand-in for the confirmation": re.compile(
        r"implementation[^.;]{0,40}(?:green )?tests?[^.;]{0,60}clos\w* the item"
        r"[^.;]{0,30}(?:do|does) not replace", re.IGNORECASE),
    # #917 K-81: the condition, not the kind, says who performs a gate.
    "the kind alone does not decide who performs it": re.compile(
        r"kind alone does not decide[^.]{0,40}who[^.]{0,200}only on that person's confirmation",
        re.IGNORECASE),
    "reject only on the user's decision": re.compile(
        r"reject\w*[^.;]{0,160}only on the user's decision", re.IGNORECASE),
    "given consent not re-asked": re.compile(
        r"(?:consent|confirmation|decision) the user already gave[^.;]{0,40}is not asked again",
        re.IGNORECASE),
}
# The refusal carries the handling clauses (what a gate IS lives in the policy).
REFUSAL_CLAUSES = (
    "complete only when met", "human act needs that person's confirmation",
    "no stand-in for the confirmation", "reject only on the user's decision",
    "given consent not re-asked",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _missing(flat: str, names) -> list[str]:
    return [n for n in names if not POLICY_CLAUSES[n].search(flat)]


def test_add_checkpoint_description_carries_the_whole_policy():
    desc = _descriptions()["add_checkpoint"]
    flat = _flat(desc)
    assert _missing(flat, POLICY_CLAUSES) == [], flat
    for kind in ("dogfood", "security", "approval", "review"):
        assert f"'{kind}'" in flat, kind
    # The dogfood rule — neutral examples (K-74) — and all three exceptions.
    assert re.search(r"user-facing feature.*kind='dogfood'", flat), flat
    assert re.search(r"screen, command, endpoint, skill or MCP tool", flat), flat
    assert re.search(r"internal refactors?/docs/CI", flat), flat
    assert re.search(r"bug fix(es)? with a regression test", flat), flat
    assert re.search(r"not-yet-enabled flag", flat), flat
    assert re.search(r"when in doubt,? add the gate", flat, re.IGNORECASE), flat
    # What closing does while a gate is pending.
    assert "resolve_item" in flat and "update_item" in flat
    assert "CLAUDE.md" not in desc and "AGENTS.md" not in desc


def test_the_refusal_carries_the_handling_clauses(engine, item):
    _gates(engine, item["id"], 1)
    flat = _flat(_refusal(engine, item["id"]))
    assert _missing(flat, REFUSAL_CLAUSES) == [], flat


@pytest.mark.parametrize("clause", sorted(POLICY_CLAUSES))
def test_the_policy_guard_notices_a_dropped_clause(clause):
    """Mutation check: deleting any one clause from the real description must
    turn the guard red — a clause matched only by incidental words (e.g. the
    `kind` argument text) would survive its own deletion."""
    flat = _flat(_descriptions()["add_checkpoint"])
    mutated = POLICY_CLAUSES[clause].sub("", flat)
    assert mutated != flat
    assert clause in _missing(mutated, POLICY_CLAUSES)


@pytest.mark.parametrize("clause", REFUSAL_CLAUSES)
def test_the_refusal_guard_notices_a_dropped_clause(engine, item, clause):
    _gates(engine, item["id"], 1)
    flat = _flat(_refusal(engine, item["id"]))
    mutated = POLICY_CLAUSES[clause].sub("", flat)
    assert mutated != flat and clause in _missing(mutated, REFUSAL_CLAUSES)


def test_closing_and_checkpoint_tools_carry_the_handling_rule():
    descs = {name: _flat(d) for name, d in _descriptions().items()}
    for tool in ("resolve_item", "update_item", "complete_checkpoint"):
        assert re.search(r"person's act[^.]{0,200}that person's confirmation", descs[tool]), tool
    for tool in ("resolve_item", "update_item", "reject_checkpoint"):
        assert re.search(r"only on the user's decision", descs[tool]), tool


@pytest.mark.parametrize(
    "tool",
    (
        "add_checkpoint", "resolve_item", "update_item",
        "complete_checkpoint", "reject_checkpoint", "reopen_checkpoint",
    ),
)
def test_gate_tool_descriptions_never_point_at_claude_md(tool):
    assert "CLAUDE.md" not in _descriptions()[tool]


@pytest.mark.parametrize("tool", ("resolve_item", "update_item"))
def test_closing_tool_descriptions_quote_the_refusal_and_point_to_the_policy(tool):
    flat = " ".join(_descriptions()[tool].split())
    assert "Cannot resolve item #N: K gate checkpoint(s) pending" in flat
    assert "add_checkpoint" in flat
    # The documented bound is the code's bound, and the tail names the full list.
    assert f"up to {GATE_REFUSAL_LISTED} " in flat
    assert "get_item(N)" in flat and "dct show N" in flat


def test_the_mcp_tools_catalog_row_states_the_codes_bound_and_tail():
    docs = (Path(__file__).resolve().parent.parent / "docs" / "mcp-tools.md").read_text(
        encoding="utf-8")
    row = next(line for line in docs.splitlines() if line.startswith("| `resolve_item` |"))
    assert f"up to {GATE_REFUSAL_LISTED} " in row, row
    numbers = {int(n) for n in re.findall(r"up to (\d+)", row)}
    assert numbers == {GATE_REFUSAL_LISTED}, row
    assert "get_item(N)" in row and "dct show N" in row, row


# ── K-61: packaged texts never point at a repo-only instruction-file section ─
#
# A package consumer gets the gate policy from MCP (add_checkpoint) or the
# packaged reference — never from the dct repository's own AGENTS.md (or the
# CLAUDE.md that preceded it). Caught: both file names, either word order,
# section / anchor forms, any quoting, a line break anywhere in between.
# Allowed: every other mention of those names (the user's global
# ~/.claude/CLAUDE.md, the packaged pointer block) and words such as
# "consent-gated".

_Q = r"""[\s'"`“”‘’«»„:,(]*"""
_FILE = r"(?<![\w/.-])(?:CLAUDE|AGENTS)\.md\b"
_TOPIC = (r"(?<![\w-])(?:gates?(?:[\s-]+checkpoints?)?|terminal[\s-]+invariants?)"
          r"(?![\w-])")
REPO_ONLY_POINTER = re.compile(
    rf"{_FILE}(?:\s*#\s*|{_Q}(?:(?:section|heading|rules?)\b{_Q})?){_TOPIC}"
    rf"|{_TOPIC}{_Q}(?:(?:section|heading|rules?)\b{_Q})?"
    rf"(?:in|of|from|under)\s+(?:the\s+)?(?:(?:repo|repository|project)\s+)?{_FILE}",
    re.IGNORECASE,
)
PACKAGE = Path(__file__).resolve().parent.parent / "dct"


@pytest.mark.parametrize(
    "text",
    (
        "See CLAUDE.md 'Gate checkpoints'.",
        'See CLAUDE.md "Gate\n    checkpoints" section.',
        "see CLAUDE.md terminal invariant",
        "See CLAUDE.md section “Gate checkpoints”.",
        "Policy: CLAUDE.md#gate-checkpoints",
        "Policy: AGENTS.md#gate-checkpoints",
        "AGENTS.md “Gate checkpoints”",
        "AGENTS.md\n  section 'Gate checkpoints'",
        "the “Gate checkpoints” section of AGENTS.md",
        "Gate checkpoints in the repo\nAGENTS.md",
        "gates (see the gate rules in CLAUDE.md)",
        "per the terminal invariant in AGENTS.md",
    ),
)
def test_repo_only_pointer_pattern_catches_known_shapes(text):
    assert REPO_ONLY_POINTER.search(text), text


@pytest.mark.parametrize(
    "text",
    (
        "# Attach the CLAUDE.md pointer (idempotent; consent-gated — a short block",
        "the dct pointer block in ~/.claude/CLAUDE.md names ~/.claude/dct-reference.md",
        "Gates are described by ~/.claude/CLAUDE.md's pointer? no — by add_checkpoint",
        "Codex reads AGENTS.md; Claude Code imports it from CLAUDE.md.",
        "a pending gate stops the close; CLAUDE.md is not consulted",
        "AGENTS.md-gated install",
        "the project's AGENTS.md lists its gates",
    ),
)
def test_repo_only_pointer_pattern_spares_legitimate_mentions(text):
    assert not REPO_ONLY_POINTER.search(text), text


def test_no_packaged_file_points_at_a_repo_only_instruction_file_section():
    offenders = []
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix not in {".py", ".md", ".sh"} or not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        for m in REPO_ONLY_POINTER.finditer(body):
            rel = path.relative_to(PACKAGE)
            offenders.append(f"dct/{rel}:{body.count(chr(10), 0, m.start()) + 1}")
    assert offenders == []


def test_the_scan_covers_the_packaged_assets():
    scanned = {p.relative_to(PACKAGE).parts[0] for p in PACKAGE.rglob("*.md")}
    assert "assets" in scanned and "skills" in scanned


# ── K-55: no packaged shortcut drops the gate exception of reject_checkpoint ─

# The one sentence (wording pinned by these regexes, not byte for byte): a
# plain checkpoint may be rejected within the agreed scope, a gate only on the
# user's decision, and add_checkpoint holds the full policy.
GATE_REJECT_EXCEPTION = re.compile(
    r"gate[^.]{0,40}only on the user's decision[^.]{0,80}add_checkpoint", re.IGNORECASE,
)
ASSETS = PACKAGE / "assets"


# Match checkpoint rejection actions, not a tool rejecting an invalid request.
_CHECKPOINT_REJECTION = re.compile(
    r"reject_checkpoint\b|dct\s+checkpoint\s+reject\b"
    r"|\breject(?:ing)?\b[^.;\n]{0,80}?\b(?:checkpoints?|gates?|steps?|ones)\b"
    r"|\b(?:checkpoints?|gates?)\b[^.;\n]{0,40}?\b(?:are|should be|must be)\s+rejected\b",
    re.IGNORECASE,
)
_TASK_REJECTION = re.compile(r"\b(?:task|cc-task)\s+checkpoints?\b", re.IGNORECASE)


def _reject_shortcut_offenders(text: str) -> list[str]:
    """Checkpoint-rejection instructions require the gate exception.

    A request refusal is not a checkpoint mutation. A rejection explicitly
    restricted to non-gate task checkpoints does not waive a gate either;
    mixed targets still require the exception.
    """
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |\d+\. |#))", text)
    offenders = []
    for block in blocks:
        flat = _flat(block)
        if (not re.search(r"irrelevant|abandoned|scope|not apply|N/A", flat, re.IGNORECASE)
                or GATE_REJECT_RESTRICTED.search(flat)):
            continue
        for action in _CHECKPOINT_REJECTION.finditer(flat):
            tail = flat[action.end():].split('.', 1)[0]
            if (_TASK_REJECTION.search(action.group())
                    and not re.search(r"\b(?:checkpoints?|gates?|ones)\b", tail, re.IGNORECASE)):
                continue
            offenders.append(flat)
            break
    return offenders


# Any wording that keeps the restriction on a gate counts in the scan; the
# canonical sentence (GATE_REJECT_EXCEPTION) is pinned separately.
GATE_REJECT_RESTRICTED = re.compile(
    r"gate[^.]{0,60}(?:only on the user's decision|without the user's)", re.IGNORECASE,
)


def test_server_instructions_carry_the_gate_exception():
    from dct.mcp.server import mcp

    workflow = mcp.instructions.split("WORKFLOW:", 1)[1].split("\n\n", 1)[0]
    flat = _flat(workflow)
    assert re.search(r"reject_checkpoint irrelevant ones", flat), flat
    assert GATE_REJECT_EXCEPTION.search(flat), flat
    assert _reject_shortcut_offenders(workflow) == []


@pytest.mark.parametrize("name", ("dct-reference.md", "codex-track.md"))
def test_packaged_references_carry_the_same_gate_exception(name):
    text = (ASSETS / name).read_text(encoding="utf-8")
    assert GATE_REJECT_EXCEPTION.search(_flat(text)), name


@pytest.mark.parametrize("path", sorted(ASSETS.glob("*.md")), ids=lambda p: p.name)
def test_no_packaged_asset_rejects_checkpoints_without_the_gate_exception(path):
    assert _reject_shortcut_offenders(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize('text', [
    'The draft changes scope, and handoff_work rejects an empty replace.',
    'Complete checkpoints when proven, reject an obsolete task checkpoint on the scoped owner decision, and preserve ledger history.',
    'The command rejects a request outside its supported scope; checkpoint history is unchanged.',
])
def test_rejection_scan_spares_request_refusals_and_explicit_non_gate_tasks(text):
    """Request refusal and a scoped task decision never authorize rejecting a gate."""
    assert _reject_shortcut_offenders(text) == []


@pytest.mark.parametrize('text', [
    'Reject irrelevant checkpoints within the agreed scope.',
    'Use reject_checkpoint for abandoned scope.',
    'Run dct checkpoint reject <id> when the check does not apply.',
    'Reject an irrelevant gate so the item can close.',
    'Complete current checkpoints and reject irrelevant ones within scope.',
    'Checkpoints should be rejected for abandoned scope.',
    'Reject obsolete task checkpoints and irrelevant gates within scope.',
])
def test_rejection_scan_catches_unauthorized_checkpoint_shortcuts(text):
    """Narrowing request-refusal detection must preserve the actual gate guard."""
    assert _reject_shortcut_offenders(text) == [_flat(text)]


def test_rejection_scan_notices_removed_gate_exception():
    """Removing only the gate exception makes a real checkpoint instruction unsafe."""
    text = "Reject irrelevant checkpoints within scope; a gate only on the user's decision (full gate policy: add_checkpoint)."
    assert _reject_shortcut_offenders(text) == []
    mutated = GATE_REJECT_RESTRICTED.sub('', text)
    assert _reject_shortcut_offenders(mutated) == [_flat(mutated)]
