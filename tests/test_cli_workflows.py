"""CLI `dct pickup` + `dct handoff create` — the G1 session ops (#413).

Same harness as test_cli_sprints.py: `cli._get_engine` is monkeypatched to the
in-memory fixture engine and commands are driven through `cli.main()` with a
patched argv, so parser wiring and dispatch are exercised end to end. The DB is
verified through the core layer — never through the CLI's own output.
"""

import io
import json
import sys

import pytest
from conftest import run_cli

from dct import cli
from dct.core.handoff import create_handoff, list_handoffs
from dct.core.items import create_item
from dct.core.sprint import list_sprint_items, set_sprint

WHO = "tester @ 2026-07-28T12:00Z"


def _seed_item(engine, title="Ship the thing"):
    return create_item(engine, "proj-a", "todo", title)["id"]


# ── parser wiring ─────────────────────────────────────────────────────────────

def test_pickup_subparser_wired_with_defaults():
    args = cli.build_parser().parse_args(["pickup"])
    assert args.command == "pickup"
    assert args.consume == "never"  # K-32: pickup is a pure read by default
    assert args.since == "last_release"
    assert args.include is None
    assert args.handoff_id is None
    assert args.consumed_by is None
    assert args.json is False


def test_pickup_flags_parse():
    args = cli.build_parser().parse_args([
        "pickup", "--project", "proj-a", "--consume", "never",
        "--include", "sprint,gates", "--handoff-id", "7", "--scope", "g1",
        "--consumed-by", WHO, "--since", "24h", "--json",
    ])
    assert args.project == "proj-a"
    assert args.consume == "never"
    assert args.include == "sprint,gates"
    assert args.handoff_id == 7
    assert args.scope == "g1"
    assert args.consumed_by == WHO
    assert args.since == "24h"
    assert args.json is True


def test_handoff_create_subparser_wired_with_defaults():
    args = cli.build_parser().parse_args(["handoff", "create", "--prompt", "go"])
    assert args.command == "handoff"
    assert args.handoff_command == "create"
    assert args.prompt == "go"
    # No item flag present → the sprint must be left untouched (K-7).
    assert args.items is None
    assert args.add_items is None
    assert args.remove_items is None
    assert args.created_by is None


def test_handoff_create_replace_and_additive_are_mutually_exclusive(cli_engine, monkeypatch, capsys):
    """Exclusivity is enforced by the workflow layer (one contract for MCP and
    CLI), not by argparse — the CLI surfaces it as ✗ + exit 1."""
    item_id = _seed_item(cli_engine)
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "go", "--items", str(item_id),
                              "--add-items", str(item_id)])
    assert exc.value.code == 1
    assert "cannot be combined" in capsys.readouterr().err


# ── dct pickup ────────────────────────────────────────────────────────────────

LONG_PROMPT = "Resume CP1242. " + "detail " * 60  # > the 200-char snapshot preview


def test_pickup_human_output_consumes_and_prints_prompt(cli_engine, monkeypatch, capsys):
    item_id = _seed_item(cli_engine)
    set_sprint(cli_engine, "proj-a", [item_id])
    create_handoff(cli_engine, "proj-a", LONG_PROMPT, "prev session")

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                          "--consumed-by", WHO])

    out = capsys.readouterr().out
    assert "✓ Picked up handoff #" in out
    assert WHO in out                       # resolved identity is echoed
    assert "Resume CP1242." in out          # full prompt, not a preview
    assert "detail" in out
    assert "Ship the thing" in out          # sprint table rendered
    assert "handoffs pending: 0" in out     # POST-consume summary
    assert not list_handoffs(cli_engine, "proj-a")


def test_pickup_json_is_parseable_with_serialized_datetimes(cli_engine, monkeypatch, capsys):
    create_handoff(cli_engine, "proj-a", LONG_PROMPT, "prev session")

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                          "--consumed-by", WHO, "--json"])

    data = json.loads(capsys.readouterr().out)
    assert data["consumed"] is True
    assert data["picked_up"]["prompt"] == LONG_PROMPT      # untruncated
    assert isinstance(data["picked_up"]["created_at"], str)
    assert isinstance(data["picked_up"]["consumed_at"], str)
    assert data["picked_up"]["consumed_by"] == WHO
    assert data["summary"]["handoffs_pending"] == 0


def test_pickup_consume_never_is_a_pure_read(cli_engine, monkeypatch, capsys):
    create_handoff(cli_engine, "proj-a", "still mine", "prev session")

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "never"])

    captured = capsys.readouterr()
    assert "— no handoff consumed (1 pending)" in captured.out
    assert "✗" not in captured.out and "✗" not in captured.err
    assert len(list_handoffs(cli_engine, "proj-a")) == 1   # untouched


def test_pickup_never_with_handoff_id_is_rejected(cli_engine, monkeypatch, capsys):
    h = create_handoff(cli_engine, "proj-a", "still mine", "prev session")
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["pickup", "--project", "proj-a",
                              "--consume", "never", "--handoff-id", str(h["id"])])
    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert len(list_handoffs(cli_engine, "proj-a")) == 1


def test_pickup_include_none_skips_sections(cli_engine, monkeypatch, capsys):
    _seed_item(cli_engine)
    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--include", "none",
                          "--consume", "never", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["sections"] == []
    assert "sprint" not in data and "gates" not in data
    assert data["summary"]["total_open_in_project"] == 1


def test_pickup_unknown_include_section_exits_nonzero(cli_engine, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["pickup", "--project", "proj-a",
                              "--include", "sprint,bogus", "--consume", "never"])
    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err


# ── dct handoff create ────────────────────────────────────────────────────────

def test_handoff_create_sets_sprint_and_creates_handoff(cli_engine, monkeypatch, capsys):
    item_id = _seed_item(cli_engine)

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt", "Pick up CP1242", "--items", str(item_id),
                          "--created-by", WHO, "--scope", "g1"])

    out = capsys.readouterr().out
    assert "✓ Handoff #" in out
    assert "scope: g1" in out
    assert WHO in out
    assert "1 items" in out

    rows = list_handoffs(cli_engine, "proj-a")
    assert [r["prompt"] for r in rows] == ["Pick up CP1242"]
    assert rows[0]["created_by"] == WHO
    assert [i["id"] for i in list_sprint_items(cli_engine, "proj-a")] == [item_id]


def test_handoff_create_without_item_flags_leaves_sprint_alone(cli_engine, monkeypatch, capsys):
    item_id = _seed_item(cli_engine)
    set_sprint(cli_engine, "proj-a", [item_id])

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go"])

    assert "sprint untouched" in capsys.readouterr().out
    assert [i["id"] for i in list_sprint_items(cli_engine, "proj-a")] == [item_id]


def test_handoff_create_remove_items_cuts_scope(cli_engine, monkeypatch, capsys):
    """--clear-sprint is gone (K-27b); scope cuts go through --remove-items."""
    item_id = _seed_item(cli_engine)
    set_sprint(cli_engine, "proj-a", [item_id])

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt", "fresh planning",
                          "--remove-items", str(item_id)])

    assert "0 items" in capsys.readouterr().out
    assert list_sprint_items(cli_engine, "proj-a") == []


def test_handoff_create_empty_items_flag_rejected(cli_engine, monkeypatch, capsys):
    """K-27: `--items \"$IDS\"` with an unset shell var must fail loudly."""
    item_id = _seed_item(cli_engine)
    set_sprint(cli_engine, "proj-a", [item_id])

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "oops", "--items", ""])
    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert [i["id"] for i in list_sprint_items(cli_engine, "proj-a")] == [item_id]


def test_handoff_create_json_output(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt", "go", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["project"] == "proj-a"
    assert data["handoff"]["prompt"] == "go"
    assert isinstance(data["handoff"]["created_at"], str)
    assert data["sprint"] is None


def test_handoff_create_prompt_file(cli_engine, monkeypatch, capsys, tmp_path):
    f = tmp_path / "handoff.md"
    f.write_text("From a file\nsecond line\n")

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt-file", str(f)])

    assert "✓ Handoff #" in capsys.readouterr().out
    assert list_handoffs(cli_engine, "proj-a")[0]["prompt"] == "From a file\nsecond line\n"


def test_handoff_create_prompt_file_stdin(cli_engine, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped prompt"))

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt-file", "-"])

    assert "✓ Handoff #" in capsys.readouterr().out
    assert list_handoffs(cli_engine, "proj-a")[0]["prompt"] == "piped prompt"


def test_handoff_create_missing_prompt_file_reports_error(cli_engine, monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt-file", str(tmp_path / "nope.md")])
    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert list_handoffs(cli_engine, "proj-a") == []


def test_handoff_create_blank_prompt_rejected(cli_engine, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "   "])
    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert list_handoffs(cli_engine, "proj-a") == []


def test_handoff_create_unparsable_items_leaves_everything_alone(cli_engine, monkeypatch, capsys):
    item_id = _seed_item(cli_engine)
    set_sprint(cli_engine, "proj-a", [item_id])

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "go", "--items", "5x,7"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "✗" in err and "--items" in err
    assert list_handoffs(cli_engine, "proj-a") == []
    assert [i["id"] for i in list_sprint_items(cli_engine, "proj-a")] == [item_id]


def test_handoff_create_unknown_item_id_rolls_sprint_back(cli_engine, monkeypatch, capsys):
    """A strict-membership failure must leave neither a handoff nor a sprint
    write behind (handoff_work is one transaction, K-8)."""
    item_id = _seed_item(cli_engine)
    set_sprint(cli_engine, "proj-a", [item_id])

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "go", "--items", "999999"])

    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert list_handoffs(cli_engine, "proj-a") == []
    assert [i["id"] for i in list_sprint_items(cli_engine, "proj-a")] == [item_id]


def test_handoff_create_defaults_created_by_and_echoes_it(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go"])

    out = capsys.readouterr().out
    identity = list_handoffs(cli_engine, "proj-a")[0]["created_by"]
    assert identity and identity.strip()
    assert "@" in identity          # "<client> @ <UTC minute>"
    assert identity in out          # never a silent attribution


def test_pickup_defaults_consumed_by_and_echoes_it(cli_engine, monkeypatch, capsys):
    create_handoff(cli_engine, "proj-a", "resume me", "prev session")

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto"])

    out = capsys.readouterr().out
    stored = list_handoffs(cli_engine, "proj-a", unconsumed_only=False)[0]["consumed_by"]
    assert stored and "@" in stored     # "<client> @ <UTC minute>"
    assert stored in out                # never a silent attribution


# ── K-41: DB text is sanitized before it reaches the terminal ─────────────────

def test_pickup_strips_control_chars_from_prompt_and_titles(cli_engine, monkeypatch, capsys):
    """A stored escape sequence must not drive the reader's terminal — ESC,
    BEL and friends are dropped; tabs and newlines survive as formatting."""
    item_id = create_item(
        cli_engine, "proj-a", "todo", "Title \x1b]0;pwned\x07with escapes"
    )["id"]
    set_sprint(cli_engine, "proj-a", [item_id])
    create_handoff(
        cli_engine, "proj-a",
        "Line one\x1b[2J\x07\x00 and\ttab\nline two", "prev session",
    )

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                          "--consumed-by", WHO])

    out = capsys.readouterr().out
    for bad in ("\x1b", "\x07", "\x00"):
        assert bad not in out
    assert "Line one[2J and\ttab" in out   # tab kept, ESC/BEL/NUL gone
    assert "line two" in out               # newline kept (rendered as a line)
    # Title survives as inert text — only the control bytes are gone.
    assert "]0;pwnedwith escapes" in out


def test_strip_controls_keeps_tab_and_newline_only():
    assert cli._strip_controls("a\tb\nc") == "a\tb\nc"
    assert cli._strip_controls("a\x1bb\x07c\x7fd\x00e") == "abcde"
    assert cli._strip_controls(None) == ""
    assert cli._strip_controls("") == ""


def test_handoff_prompt_file_reads_utf8_regardless_of_locale(cli_engine, monkeypatch, capsys, tmp_path):
    """Prompts carry '✓' and em dashes — the platform default encoding must
    never decide whether they survive."""
    f = tmp_path / "handoff.md"
    f.write_bytes("✓ done — next: żółw\n".encode())

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt-file", str(f)])

    capsys.readouterr()
    assert list_handoffs(cli_engine, "proj-a")[0]["prompt"] == "✓ done — next: żółw\n"


def test_handoff_prompt_stdin_decodes_utf8_bytes(cli_engine, monkeypatch, capsys):
    """Real stdin exposes .buffer — we decode those bytes as UTF-8 ourselves
    instead of trusting a possibly-ASCII locale wrapper."""
    class _Stdin:
        buffer = io.BytesIO("piped ✓ żółw".encode())

    monkeypatch.setattr(sys, "stdin", _Stdin())

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt-file", "-"])

    capsys.readouterr()
    assert list_handoffs(cli_engine, "proj-a")[0]["prompt"] == "piped ✓ żółw"


# ── invalid `since`: ✗ + exit 1 on both session commands ─────────────────────

@pytest.mark.parametrize("since", ["garbage", "7d", "yesterday"])
def test_pickup_invalid_since_exits_nonzero(cli_engine, monkeypatch, capsys, since):
    """A bad --since must be a loud failure, never a silent 'all time' read."""
    create_handoff(cli_engine, "proj-a", "resume me", "prev session")

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["pickup", "--project", "proj-a",
                              "--consume", "never", "--since", since])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "✗" in err and "Invalid `since`" in err


def test_pickup_invalid_since_does_not_consume(cli_engine, monkeypatch, capsys):
    """K-21 at the CLI: --consume auto + a bad --since rolls the consume back."""
    create_handoff(cli_engine, "proj-a", "still mine", "prev session")

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                              "--consumed-by", WHO, "--since", "garbage"])

    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert len(list_handoffs(cli_engine, "proj-a")) == 1  # still pending


# ── --sprint selector ────────────────────────────────────────────────────────

def test_handoff_create_sprint_selector_targets_that_sprint(cli_engine, monkeypatch, capsys):
    from dct.core.sprints import create_sprint, list_items_in_sprint

    a = _seed_item(cli_engine, "Into s1")
    b = _seed_item(cli_engine, "Into s2")
    s1 = create_sprint(cli_engine, "proj-a", "S1", code="s1", status="active")
    s2 = create_sprint(cli_engine, "proj-a", "S2", code="s2", status="active")

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt", "go", "--items", str(a), "--sprint", "s1"])
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt", "go", "--items", str(b),
                          "--sprint", str(s2["id"])])

    capsys.readouterr()
    assert [m["id"] for m in list_items_in_sprint(cli_engine, s1["id"])] == [a]
    assert [m["id"] for m in list_items_in_sprint(cli_engine, s2["id"])] == [b]


def test_handoff_create_sprint_selector_not_active_exits_nonzero(cli_engine, monkeypatch, capsys):
    from dct.core.sprints import create_sprint

    item_id = _seed_item(cli_engine)
    create_sprint(cli_engine, "proj-a", "Planned", code="pl")  # planned, not active

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "go", "--items", str(item_id),
                              "--sprint", "pl"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "✗" in err and "not an active sprint" in err
    assert list_handoffs(cli_engine, "proj-a") == []


def test_handoff_create_several_active_sprints_demands_a_selector(cli_engine, monkeypatch, capsys):
    from dct.core.sprints import create_sprint

    item_id = _seed_item(cli_engine)
    create_sprint(cli_engine, "proj-a", "S1", code="s1", status="active")
    create_sprint(cli_engine, "proj-a", "S2", code="s2", status="active")

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "go", "--items", str(item_id)])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "2 active sprints" in err
    assert "'s1'" in err and "'s2'" in err          # candidates listed
    assert err.strip().count("\n") == 0             # single-line ✗ convention
    assert list_handoffs(cli_engine, "proj-a") == []


def test_handoff_create_sprint_id_code_collision_exits_nonzero(cli_engine, monkeypatch, capsys):
    from dct.core.sprints import create_sprint

    item_id = _seed_item(cli_engine)
    s1 = create_sprint(cli_engine, "proj-a", "First", status="active")
    create_sprint(cli_engine, "proj-a", "Second", code=str(s1["id"]), status="active")

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "go", "--items", str(item_id),
                              "--sprint", str(s1["id"])])

    assert exc.value.code == 1
    assert "ambiguous" in capsys.readouterr().err
    assert list_handoffs(cli_engine, "proj-a") == []


# ── human renders that are easy to break ─────────────────────────────────────

def test_pickup_renders_pending_gates_table(cli_engine, monkeypatch, capsys):
    """A pending gate is the reason a session cannot close an item — it has to
    be VISIBLE in the human view, not only in --json."""
    from dct.core.checkpoints import add_checkpoint

    item_id = _seed_item(cli_engine, "Gated feature")
    add_checkpoint(cli_engine, item_id, "Plain step", kind="task")
    add_checkpoint(cli_engine, item_id, "Validate in a fresh session", kind="dogfood")
    set_sprint(cli_engine, "proj-a", [item_id])

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "never"])

    out = capsys.readouterr().out
    assert "Pending gates:" in out
    assert "kind" in out and "dogfood" in out          # header + the gate kind
    assert "Validate in a fresh session" in out        # the gate text
    assert "Plain step" not in out                     # a task never gates
    assert "gates: 1" in out                           # summary counter agrees


def test_pickup_include_none_human_render_prints_summary(cli_engine, monkeypatch, capsys):
    """--include none drops every section; the human renderer guards each lookup
    and must still print the summary line instead of crashing on a missing key."""
    _seed_item(cli_engine)
    create_handoff(cli_engine, "proj-a", "not mine yet", "prev session")

    run_cli(monkeypatch, ["pickup", "--project", "proj-a",
                          "--include", "none", "--consume", "never"])

    captured = capsys.readouterr()
    assert "— no handoff consumed (1 pending)" in captured.out
    assert "Sprint: size 0, done 0/0" in captured.out
    assert "open in project: 1" in captured.out
    assert "handoffs pending: 1" in captured.out
    assert "Sprint items" not in captured.out and "Pending gates" not in captured.out
    assert captured.err == ""


def test_pickup_include_none_still_consumes_and_prints_prompt(cli_engine, monkeypatch, capsys):
    """Consumption is independent of the sections — the prompt is what a fresh
    session needs, so it must print even with everything else switched off."""
    create_handoff(cli_engine, "proj-a", LONG_PROMPT, "prev session")

    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--include", "none",
                          "--consume", "auto", "--consumed-by", WHO])

    out = capsys.readouterr().out
    assert "✓ Picked up handoff #" in out
    assert "Resume CP1242." in out
    assert not list_handoffs(cli_engine, "proj-a")


# ── _read_text_arg: the --prompt / --prompt-file pair ────────────────────────

def test_read_text_arg_file_wins_over_inline_text(tmp_path):
    f = tmp_path / "handoff.md"
    f.write_text("from the file", encoding="utf-8")
    assert cli._read_text_arg("from the flag", str(f)) == "from the file"


def test_read_text_arg_returns_none_when_neither_given():
    """None (not '') so callers can tell "flag absent" from "explicitly empty"."""
    assert cli._read_text_arg(None, None) is None
    assert cli._read_text_arg(None, "") is None
    assert cli._read_text_arg("inline", None) == "inline"


def test_read_text_arg_empty_file_yields_empty_string(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    assert cli._read_text_arg("fallback text", str(f)) == ""


def test_handoff_create_prompt_file_wins_over_prompt_flag(cli_engine, monkeypatch, capsys, tmp_path):
    """Documented precedence, end to end: the file's text is what gets stored."""
    f = tmp_path / "handoff.md"
    f.write_text("FILE WINS", encoding="utf-8")

    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt", "flag loses", "--prompt-file", str(f)])

    capsys.readouterr()
    assert list_handoffs(cli_engine, "proj-a")[0]["prompt"] == "FILE WINS"


@pytest.mark.parametrize("content", ["", "   \n", "\n\n"])
def test_handoff_create_empty_prompt_file_rejected_even_with_prompt_flag(
    cli_engine, monkeypatch, capsys, tmp_path, content
):
    """The file wins even when it is EMPTY — precedence must not quietly fall
    back to --prompt, so this is a loud ✗ and no handoff row."""
    f = tmp_path / "empty.md"
    f.write_text(content, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt", "would have been stored",
                              "--prompt-file", str(f)])

    assert exc.value.code == 1
    assert "✗" in capsys.readouterr().err
    assert list_handoffs(cli_engine, "proj-a") == []


# ── K-1/K-26: foreign vs unknown item id are indistinguishable ───────────────

def test_handoff_create_foreign_and_unknown_item_id_report_identically(
    cli_engine, monkeypatch, capsys
):
    """No existence leak through the CLI either — same message modulo the id,
    and never the foreign item's title."""
    from dct.core.projects import create_project

    create_project(cli_engine, slug="proj-b", name="Project B", path="/tmp/proj-b")
    foreign = create_item(cli_engine, "proj-b", "todo", "SECRET foreign title")["id"]

    messages = {}
    for label, bad_id in (("foreign", foreign), ("unknown", 999999)):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                                  "--prompt", "go", "--items", str(bad_id)])
        assert exc.value.code == 1
        messages[label] = capsys.readouterr().err.replace(str(bad_id), "<ID>")

    assert messages["foreign"] == messages["unknown"]
    assert "SECRET" not in messages["foreign"]
    assert list_handoffs(cli_engine, "proj-a") == []


def test_pickup_since_help_lists_only_supported_forms():
    """The parser advertised '7d', which parse_since rejects."""
    action = next(
        a for a in cli.build_parser()._subparsers._group_actions[0].choices["pickup"]._actions
        if a.dest == "since"
    )
    assert "7d" not in action.help
    assert "last_release" in action.help and "<N>h" in action.help
