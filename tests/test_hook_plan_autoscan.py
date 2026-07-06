"""Sanity tests for hooks/dct-plan-autoscan.sh.

We don't fire the real hook (that needs CC, dct, jq all wired up).
Instead: verify file exists, has the shebang + executable bit, passes
bash syntax check, and respects its exit-0-on-no-op contract.
"""

import subprocess
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "dct-plan-autoscan.sh"


def test_hook_file_exists():
    assert HOOK_PATH.is_file()


def test_hook_has_shebang():
    first_line = HOOK_PATH.read_text().splitlines()[0]
    assert first_line.startswith("#!")
    assert "bash" in first_line


def test_hook_is_executable():
    mode = HOOK_PATH.stat().st_mode
    assert mode & 0o111, f"hook not executable: mode={oct(mode)}"


def test_hook_bash_syntax_clean():
    result = subprocess.run(
        ["bash", "-n", str(HOOK_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"syntax: {result.stderr}"


def test_hook_consumes_stdin_even_if_not_used():
    """Rule 2: hook must drain stdin; otherwise downstream git calls hang."""
    src = HOOK_PATH.read_text()
    assert "input=$(cat)" in src, "hook must read stdin per rule 2"


def test_hook_graceful_on_missing_deps():
    """Rule 3: `command -v` guard for dct and jq."""
    src = HOOK_PATH.read_text()
    assert "command -v dct" in src
    assert "command -v jq" in src


def test_hook_exits_early_on_non_edit_tool():
    """Feed a non-Edit tool payload — hook should exit 0 quickly."""
    payload = '{"tool_name": "Bash", "tool_input": {"command": "ls"}}'
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        timeout=10,
    )
    assert result.returncode == 0


def test_hook_exits_on_non_plan_file(tmp_path):
    """Edit of a non-plan file — hook must no-op."""
    unrelated = tmp_path / "src" / "unrelated.ts"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("export {};")
    payload = f'{{"tool_name": "Edit", "tool_input": {{"file_path": "{unrelated}"}}}}'
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        timeout=10,
    )
    assert result.returncode == 0


def test_hook_exits_on_missing_file(tmp_path):
    """If matched-path file no longer exists, skip — no error."""
    missing = tmp_path / "docs" / "plans" / "gone.md"
    payload = f'{{"tool_name": "Edit", "tool_input": {{"file_path": "{missing}"}}}}'
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        timeout=10,
    )
    assert result.returncode == 0


@pytest.mark.skipif(
    subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
    reason="jq not installed — integration aspect unverifiable",
)
def test_hook_no_stdout_leak_on_quick_exit():
    """Hook should print nothing on the no-op paths (tests CC UI cleanliness)."""
    payload = '{"tool_name": "Bash", "tool_input": {"command": "ls"}}'
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        timeout=10,
    )
    assert result.stdout == ""
    # stderr may contain "command not found" for dct/jq in CI — not fatal here.
