"""Sanity tests for hooks/dct-task-mirror.sh (v0.4.0 — item #34).

Mirrors the shape of test_hook_plan_autoscan.py. We do NOT fire the hook
end-to-end (that needs CC harness + real dct + jq); instead we verify
file presence, syntax, and contract compliance.
"""

import subprocess
from pathlib import Path


HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "dct-task-mirror.sh"


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


def test_hook_consumes_stdin():
    """Rule 2: hook must drain stdin; otherwise downstream hangs."""
    src = HOOK_PATH.read_text()
    assert "input=$(cat)" in src


def test_hook_graceful_on_missing_deps():
    """Rule 3: `command -v` guard for dct and jq."""
    src = HOOK_PATH.read_text()
    assert "command -v dct" in src
    assert "command -v jq" in src


def test_hook_filters_to_task_tools():
    """Only TaskCreate/TodoWrite trigger the mirror."""
    src = HOOK_PATH.read_text()
    assert "TaskCreate" in src
    assert "TodoWrite" in src


def test_hook_respects_config_gate():
    """[mirror].task_to_dct = off|ask must short-circuit."""
    src = HOOK_PATH.read_text()
    assert "dct config get mirror.task_to_dct" in src
    assert "off|ask" in src


def test_hook_uses_fire_and_forget():
    """Rule for latency: nohup + disown so the hook returns in ms."""
    src = HOOK_PATH.read_text()
    assert "nohup" in src
    assert "disown" in src


def test_hook_always_exits_zero():
    """Every exit path is `exit 0` — never block the tool call."""
    src = HOOK_PATH.read_text()
    # All explicit exits should be 0
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("exit"):
            # Skip shell syntax like `exit "$status"` (not present but guard)
            assert stripped in ("exit 0",), f"non-zero exit found: {stripped}"


def test_hook_noop_on_empty_stdin():
    """Empty/garbage stdin must not crash — exit 0 cleanly."""
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input="",
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_hook_noop_on_non_task_tool():
    """Payload for unrelated tool → no work, exit 0."""
    payload = '{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}'
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
