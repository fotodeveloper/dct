"""The approved context summary survives the CLI round trip (#877 K-58).

/handoff prints a draft prompt AND a draft context_summary; the owner's
"Save" approves both. The CLI path (`dct handoff create`, the fallback's
command) must store the summary EXACTLY as approved — every line, every
non-ASCII character, nothing shortened — and the next session's read
(`dct pickup`, JSON and human view) must give it back. A multi-line text is
the case that detects loss: a flag that only keeps the first line, a strip,
or a render that prints the prompt but not the summary.

Same harness as test_cli_workflows.py: `cli_engine` + `run_cli`, verified
through the core layer, never only through the CLI's own output.
"""

import io
import json
import sys

import pytest
from conftest import run_cli

from dct.core.handoff import list_handoffs

WHO = "tester @ 2026-10-12T03:00Z"

# Several lines, a blank line inside, indentation, a bullet, quotes, a
# shell-hostile `$(...)`, non-ASCII prose and trailing whitespace — every
# shape a summary approved on a printed draft can take.
SUMMARY = (
    "State: #22 in progress — CP 3/5 done.\n"
    "\n"
    "  - gate #17 (dogfood) still pending\n"
    "  - open question: keep the \"legacy\" path? `$(not run)`\n"
    "Zażółć gęślą jaźń ✓\n"
    "last line with trailing spaces   \n"
)


def _only_handoff(engine) -> dict:
    [row] = list_handoffs(engine, "proj-a")
    return row


def test_inline_context_summary_is_stored_verbatim(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go",
                          "--context-summary", SUMMARY])
    assert "✓ Handoff #" in capsys.readouterr().out
    assert _only_handoff(cli_engine)["context_summary"] == SUMMARY


def test_context_summary_file_is_stored_verbatim(cli_engine, monkeypatch, capsys, tmp_path):
    f = tmp_path / "summary.md"
    f.write_text(SUMMARY, encoding="utf-8")
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go",
                          "--context-summary-file", str(f)])
    assert "✓ Handoff #" in capsys.readouterr().out
    assert _only_handoff(cli_engine)["context_summary"] == SUMMARY


def test_context_summary_file_reads_stdin_beside_a_prompt_file(
        cli_engine, monkeypatch, capsys, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("the prompt\nline two\n", encoding="utf-8")
    # A real stdin: text over UTF-8 bytes (the CLI decodes `buffer` itself).
    monkeypatch.setattr(sys, "stdin",
                        io.TextIOWrapper(io.BytesIO(SUMMARY.encode("utf-8")), encoding="ascii"))
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                          "--prompt-file", str(prompt), "--context-summary-file", "-"])
    row = _only_handoff(cli_engine)
    assert row["prompt"] == "the prompt\nline two\n"
    assert row["context_summary"] == SUMMARY


def test_both_texts_from_stdin_are_refused_before_any_write(cli_engine, monkeypatch, capsys):
    """stdin can be read once: two flags on '-' would give the second one an
    EMPTY text — a silent loss. Refused before anything is written."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped"))
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a",
                              "--prompt-file", "-", "--context-summary-file", "-"])
    assert exc.value.code == 1
    assert "stdin" in capsys.readouterr().err
    assert list_handoffs(cli_engine, "proj-a") == []


def test_the_next_session_reads_the_summary_back_as_json(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go",
                          "--context-summary", SUMMARY])
    capsys.readouterr()
    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                          "--consumed-by", WHO, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["picked_up"]["context_summary"] == SUMMARY


def test_the_next_session_human_view_prints_every_line_of_the_summary(
        cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go",
                          "--context-summary", SUMMARY])
    capsys.readouterr()
    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                          "--consumed-by", WHO])
    out = capsys.readouterr().out
    for line in (ln.strip() for ln in SUMMARY.splitlines()):
        if line:
            assert line in out, f"the pickup view lost a summary line: {line!r}\n{out}"


def test_a_handoff_without_a_summary_prints_no_summary_block(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go"])
    capsys.readouterr()
    assert _only_handoff(cli_engine)["context_summary"] is None
    run_cli(monkeypatch, ["pickup", "--project", "proj-a", "--consume", "auto",
                          "--consumed-by", WHO])
    assert "Context summary" not in capsys.readouterr().out


def test_update_context_summary_file_is_stored_verbatim(cli_engine, monkeypatch, capsys, tmp_path):
    run_cli(monkeypatch, ["handoff", "create", "--project", "proj-a", "--prompt", "go"])
    hid = _only_handoff(cli_engine)["id"]
    f = tmp_path / "summary.md"
    f.write_text(SUMMARY, encoding="utf-8")
    run_cli(monkeypatch, ["handoff", "update", str(hid), "--context-summary-file", str(f)])
    assert _only_handoff(cli_engine)["context_summary"] == SUMMARY
