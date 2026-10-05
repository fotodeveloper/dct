"""Sanity tests for hooks/dct-task-mirror.sh (v0.4.0 — item #34).

Mirrors the shape of test_hook_plan_autoscan.py. We do NOT fire the shell
hook end-to-end through the CC harness; we verify file presence, syntax and
contract compliance, and (K-93 A5) that the hook really PARSES the payload:
with jq present it hands a task-tool payload to the `dct _task-mirror`
worker (a fake `dct` on PATH records the hand-off) and never spawns it for
any other tool — those tests need jq and use the shared `hook_tools`
sentinel (skip locally, FAIL under CI). Pure syntax / fail-open checks need
no jq. The worker's gating logic (#354 — config gate + silent project
autodetect moved off the shell fast-path) IS exercised directly against the
in-memory engine.
"""

import argparse
import io
import os
import subprocess
import time

import pytest

from dct.integrations.claude import HOOKS_DIR

HOOK_PATH = HOOKS_DIR / "dct-task-mirror.sh"


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


def test_shell_defers_gate_and_detection_to_worker():
    """#354: the shell is a lean dispatcher. The config gate and project
    autodetect moved into `dct _task-mirror`, so NO synchronous Python or
    config call runs before the worker is backgrounded — that is what keeps
    the fast path in single-digit milliseconds."""
    src = HOOK_PATH.read_text()
    assert "dct config get" not in src
    assert "dct project detect" not in src
    # The backgrounded worker is the only dct invocation on the hot path.
    assert "dct _task-mirror" in src


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


# ── K-93 A5: the hook really parses the payload (needs jq) ───────────────────


def _fake_dct(tmp_path):
    """A `dct` on PATH that appends `<args>\t<stdin>` to calls.log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    script = bin_dir / "dct"
    fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write('#!/usr/bin/env bash\nprintf "%s\\t%s\\n" "$*" "$(cat)" >> "' + str(log) + '"\n')
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "TMPDIR": str(tmp_path)}
    return env, log


def _run_hook(payload, env):
    return subprocess.run(["bash", str(HOOK_PATH)], input=payload, capture_output=True,
                          text=True, timeout=10, env=env)


def _wait_lines(log, count, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and len(log.read_text().splitlines()) >= count:
            break
        time.sleep(0.05)
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.usefixtures("hook_tools")
@pytest.mark.parametrize("tool", ["TaskCreate", "TodoWrite"])
def test_hook_hands_a_task_payload_to_the_worker(tmp_path, tool):
    env, log = _fake_dct(tmp_path)
    payload = '{"tool_name":"' + tool + '","tool_input":{"subject":"x"},"session_id":"s-1"}'
    assert _run_hook(payload, env).returncode == 0
    lines = _wait_lines(log, 1)
    assert lines == [f"_task-mirror\t{payload}"]


@pytest.mark.usefixtures("hook_tools")
def test_hook_never_spawns_the_worker_for_another_tool(tmp_path):
    """The parse decides: a non-task payload exits before any spawn. A task
    payload sent AFTER it proves the fake works and bounds the wait."""
    env, log = _fake_dct(tmp_path)
    other = '{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}'
    assert _run_hook(other, env).returncode == 0
    assert _run_hook('{"tool_name":"TodoWrite","tool_input":{}}', env).returncode == 0
    _wait_lines(log, 1)
    time.sleep(0.3)  # a stray spawn of the first call would have landed by now
    lines = log.read_text().splitlines()
    assert [line.split("\t", 1)[1] for line in lines] == ['{"tool_name":"TodoWrite","tool_input":{}}']


# ── Worker gating (#354): config gate + silent project autodetect ─────────────

def _run_worker(engine, monkeypatch, payload, *, mode="auto", cwd=None, project=""):
    """Drive `cmd_internal_task_mirror` against the in-memory engine.

    Patches the worker's engine factory and config loader; feeds `payload`
    (already-serialized JSON) on stdin. `cwd` overrides os.getcwd() so the
    silent-autodetect branch is exercised deterministically.
    """
    from dct import cli
    from dct.config import ClaudeIntegrationConfig, Config, IntegrationsConfig

    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(
        "dct.config.load_config",
        lambda: Config(integrations=IntegrationsConfig(
            claude=ClaudeIntegrationConfig(task_mirror=mode))),
    )
    if cwd is not None:
        monkeypatch.setattr("os.getcwd", lambda: cwd)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    args = argparse.Namespace(session_id="s1", project=project)
    cli.cmd_internal_task_mirror(args)


def _cc_items(engine):
    from dct.core.items import list_items

    return [
        r for r in list_items(engine, project_slug="test", search="CC task list",
                              status=None, limit=100)
        if r.get("title", "").startswith("CC task list")
    ]


PAYLOAD = (
    '{"tool_name":"TodoWrite","session_id":"s1",'
    '"tool_input":{"todos":[{"content":"alpha"},{"content":"beta"}]}}'
)


def test_worker_mirrors_when_auto(engine, seed_project, monkeypatch):
    """auto mode + resolvable project → item created with cc-task checkpoints."""
    from dct.core.items import get_item

    _run_worker(engine, monkeypatch, PAYLOAD, mode="auto", project="test")

    items = _cc_items(engine)
    assert len(items) == 1
    full = get_item(engine, items[0]["id"])
    cps = [c for c in full["checkpoints"] if c["kind"] == "cc-task"]
    assert len(cps) == 2
    assert all(c["text"].startswith("[cc] ") for c in cps)


@pytest.mark.parametrize("mode", ["off", "ask", "bogus"])
def test_worker_config_gate_blocks_non_auto(engine, seed_project, monkeypatch, mode):
    """#354: only 'auto' mirrors — off/ask/unknown are silent no-ops that do
    NOT touch the database."""
    _run_worker(engine, monkeypatch, PAYLOAD, mode=mode, project="test")
    assert _cc_items(engine) == []


def test_worker_silent_when_no_project(engine, monkeypatch):
    """auto mode but CWD is not a registered project → silent no-op, no crash,
    no item created (autodetect moved into the worker)."""
    _run_worker(
        engine, monkeypatch, PAYLOAD,
        mode="auto", project="", cwd="/nonexistent/unregistered/path",
    )
    from dct.core.items import list_items

    assert list_items(engine, project_slug=None, search="CC task list",
                      status=None, limit=100) == []


def test_worker_reads_session_id_from_payload(engine, seed_project, monkeypatch):
    """session_id travels in the payload now (shell no longer extracts it);
    an empty --session-id arg falls back to payload .session_id."""
    from dct import cli
    from dct.config import ClaudeIntegrationConfig, Config, IntegrationsConfig

    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(
        "dct.config.load_config",
        lambda: Config(integrations=IntegrationsConfig(
            claude=ClaudeIntegrationConfig(task_mirror="auto"))),
    )
    payload = (
        '{"tool_name":"TodoWrite","session_id":"from-payload",'
        '"tool_input":{"todos":[{"content":"x"}]}}'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    cli.cmd_internal_task_mirror(
        argparse.Namespace(session_id="", project="test")
    )
    titles = [r["title"] for r in _cc_items(engine)]
    assert "CC task list — session from-payload" in titles


def test_worker_preserves_legacy_empty_mode_fallback(engine, seed_project, monkeypatch):
    _run_worker(engine, monkeypatch, PAYLOAD, mode="", project="test")
    assert len(_cc_items(engine)) == 1
