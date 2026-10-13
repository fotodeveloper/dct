"""`dct triage export` — the CLI twin of the export_triage MCP tool (#909).

Driven through `cli.main()` on a patched argv (parser wiring + dispatch end to
end) against the fixture engine (`cli_engine`, project 'proj-a'). Expectations:
the --json output parses and equals the workflow export; the default output is
three constant declarations whose values parse as JSON (JSON is valid
JavaScript, so they paste into the page template) and stay inert inside an
inline <script> whatever a finding's text holds (K-1); the positional cycle is
always a code within the project, an id goes through --cycle-id (K-13).
"""

from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest
from conftest import run_cli

from dct import cli
from dct.core.projects import create_project
from dct.workflows.triage import (
    close_triage,
    decide_findings,
    export_triage,
    open_triage,
    record_findings,
)


def _finding(**over):
    base = {"title": "Lost update on concurrent edit", "fp": "last write wins at save by a race",
            "S": 2, "O": 1, "E": 2, "K": "M", "origin": "pre", "type": "correctness",
            "src": "codex", "ctx": ["Zażółć gęślą jaźń."],
            "opts": [{"k": "a", "t": "Row lock", "rec": True, "root": True}]}
    base.update(over)
    return base


@pytest.fixture
def cycle(cli_engine):
    cycle_id = open_triage(cli_engine, "proj-a", "rev-1", "review", "claude-code")["cycle_id"]
    record_findings(cli_engine, cycle_id, [_finding()])
    return cycle_id


def _export(monkeypatch, capsys, *argv):
    run_cli(monkeypatch, ["triage", "export", *argv])
    return capsys.readouterr().out


def test_parser_wiring():
    args = cli.build_parser().parse_args(["triage", "export", "rev-1", "--project", "p", "--json"])
    assert (args.command, args.triage_command, args.cycle, args.project, args.json) == (
        "triage", "export", "rev-1", "p", True)


def test_json_output_equals_the_workflow_export(monkeypatch, capsys, cli_engine, cycle):
    decide_findings(cli_engine, cycle, {"K-1": {"dec": "fix", "opt": "a", "note": "ok"}},
                    decided_by="owner", decision_source="chat 2026-10-13")
    close_triage(cli_engine, cycle, source="chat")
    out = json.loads(_export(monkeypatch, capsys, "rev-1", "--project", "proj-a", "--json"))
    assert out == json.loads(json.dumps(export_triage(cli_engine, cycle)))
    assert out["closed"]["answers"]["K-1"]["dec"] == "fix"


def test_default_output_is_three_parseable_constants(monkeypatch, capsys, cli_engine, cycle):
    text = _export(monkeypatch, capsys, "rev-1", "--project", "proj-a")
    expected = export_triage(cli_engine, cycle)
    found = {}
    for name in ("CLOSED", "SUGGESTED", "CARDS"):
        start = text.index(f"const {name} = ") + len(f"const {name} = ")
        end = text.index(";\n", start)
        found[name] = json.loads(text[start:end])
    assert found == {"CLOSED": expected["closed"], "SUGGESTED": expected["suggested"],
                     "CARDS": expected["cards"]}
    assert "Zażółć gęślą jaźń." in text  # readable, not \\u-escaped


def test_cycle_id_is_accepted_with_or_without_project(monkeypatch, capsys, cycle):
    for extra in ((), ("--project", "proj-a")):
        out = json.loads(_export(monkeypatch, capsys, "--cycle-id", str(cycle), *extra, "--json"))
        assert out["cycle"]["code"] == "rev-1"


def test_a_digit_only_cycle_is_a_code_never_an_id(monkeypatch, capsys, cli_engine, cycle):
    """Cycle #1 exists; the positional "1" is a code — found when a cycle is
    CODED "1", refused when none is, never guessed to mean id #1."""
    assert cycle == 1
    with pytest.raises(SystemExit):
        _export(monkeypatch, capsys, "1", "--project", "proj-a")
    assert "No triage cycle '1'" in capsys.readouterr().err
    coded = open_triage(cli_engine, "proj-a", "1", "digit code", "me")["cycle_id"]
    out = json.loads(_export(monkeypatch, capsys, "1", "--project", "proj-a", "--json"))
    assert out["cycle"]["id"] == coded


@pytest.mark.parametrize(("ref", "message"), [
    (("rev-1",), "No triage cycle 'rev-1' in project 'proj-b'"),
    (("--cycle-id", "1"), "does not belong to project 'proj-b'"),
])
def test_another_projects_cycle_is_refused(monkeypatch, capsys, cli_engine, cycle, ref, message):
    create_project(cli_engine, slug="proj-b", name="B", path="/tmp/proj-b")
    with pytest.raises(SystemExit) as exc:
        _export(monkeypatch, capsys, *ref, "--project", "proj-b")
    assert exc.value.code == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ("nope", "--project", "proj-a"),
    ("rev-1", "--project", "zzz"),
    ("--cycle-id", "999"),
    ("--cycle-id", "0"),
    ("rev-1", "--cycle-id", "1", "--project", "proj-a"),   # both
    ("--project", "proj-a"),                               # neither
])
def test_unknown_cycle_or_project_exits_1(monkeypatch, capsys, cycle, argv):
    with pytest.raises(SystemExit) as exc:
        _export(monkeypatch, capsys, *argv)
    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("✗ Error:")


def test_group_without_subcommand_prints_help(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["triage"])
    assert exc.value.code == 0
    assert "export" in capsys.readouterr().out


# ── K-1: the constants are inert inside the page's <script> ─────────────────

HOSTILE = {
    "title": "Closes the page: </script><script>alert(1)</script>",
    "ctx": ["<!-- opens a comment", "a & b < c > d", "line\u2028separator\u2029para"],
    "opt": "</SCRIPT >&lt;",
}


class _ScriptCounter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.starts: list[str] = []
        self.comments = 0

    def handle_starttag(self, tag, attrs):
        self.starts.append(tag)

    def handle_comment(self, data):
        self.comments += 1


@pytest.fixture
def hostile_cycle(cli_engine):
    cycle_id = open_triage(cli_engine, "proj-a", "rev-h", "review", "claude-code")["cycle_id"]
    record_findings(cli_engine, cycle_id, [_finding(
        title=HOSTILE["title"], ctx=HOSTILE["ctx"],
        opts=[{"k": "a", "t": HOSTILE["opt"], "why": "<b>bold</b>", "rec": True}])])
    decide_findings(cli_engine, cycle_id, {"K-1": {"dec": "fix", "opt": "a",
                                                   "note": "</script> verbatim"}},
                    decided_by="owner", decision_source="chat")
    close_triage(cli_engine, cycle_id, source="chat </script>", notes=["<!-- n -->"])
    return cycle_id


def test_hostile_texts_cannot_leave_the_script(monkeypatch, capsys, cli_engine, hostile_cycle):
    text = _export(monkeypatch, capsys, "rev-h", "--project", "proj-a")
    assert "</" not in text and "<!--" not in text
    assert "\u2028" not in text and "\u2029" not in text

    page = _ScriptCounter()
    page.feed(f"<html><body><script>\n{text}</script></body></html>")
    page.close()
    assert page.starts == ["html", "body", "script"] and page.comments == 0


def test_hostile_texts_parse_back_to_the_recorded_originals(
        monkeypatch, capsys, cli_engine, hostile_cycle):
    text = _export(monkeypatch, capsys, "rev-h", "--project", "proj-a")
    expected = export_triage(cli_engine, hostile_cycle)
    found = {}
    for name in ("CLOSED", "SUGGESTED", "CARDS"):
        start = text.index(f"const {name} = ") + len(f"const {name} = ")
        end = text.index(";\n", start)
        found[name] = json.loads(text[start:end])
    assert found == {"CLOSED": expected["closed"], "SUGGESTED": expected["suggested"],
                     "CARDS": expected["cards"]}
    (card,) = found["CARDS"]
    assert card["title"] == HOSTILE["title"] and card["ctx"] == HOSTILE["ctx"]
    assert card["opts"][0]["t"] == HOSTILE["opt"]
    assert found["CLOSED"]["answers"]["K-1"]["note"] == "</script> verbatim"


def test_json_output_keeps_the_texts_as_recorded(monkeypatch, capsys, hostile_cycle):
    """--json is data, not a page fragment: the owner's condition — no
    literal escape sequences written into the data."""
    out = json.loads(_export(monkeypatch, capsys, "rev-h", "--project", "proj-a", "--json"))
    assert out["cards"][0]["title"] == HOSTILE["title"]
    assert out["closed"]["source"] == "chat </script>"
