"""Tests for dct.core.plans.classify — mocked subprocess boundary.

These tests never actually exec `claude`; we monkeypatch `shutil.which` and
`subprocess.run` to control the whole subprocess surface deterministically.
"""

import subprocess

import pytest

from dct.core.plans import classify
from dct.core.plans.classify import (
    VALID_SECTION_TYPES,
    _build_prompt,
    _sanitize_response,
    classify_uncertain,
)


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── _build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_includes_heading_and_labels():
    prompt = _build_prompt("Sprint 3", None, "")
    assert "Sprint 3" in prompt
    for label in VALID_SECTION_TYPES:
        assert label in prompt


def test_build_prompt_includes_parent_when_given():
    prompt = _build_prompt("Task 1", "Phase B", "impl steps")
    assert "Phase B" in prompt
    assert "impl steps" in prompt


def test_build_prompt_omits_parent_section_when_none():
    prompt = _build_prompt("Task 1", None, "")
    assert "Parent heading" not in prompt


# ── _sanitize_response ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["sprint", "SPRINT", " sprint ", "sprint\n", "`sprint`", '"sprint"'])
def test_sanitize_accepts_valid_with_wrappers(raw):
    assert _sanitize_response(raw) == "sprint"


def test_sanitize_rejects_unknown_label():
    assert _sanitize_response("some_other_label") is None


def test_sanitize_rejects_empty():
    assert _sanitize_response("") is None
    assert _sanitize_response("   \n\n") is None


def test_sanitize_takes_first_word_only():
    """Model sometimes adds a short explanation — take the first word."""
    assert _sanitize_response("sprint because of date prefix") == "sprint"


def test_sanitize_rejects_multiword_invalid():
    assert _sanitize_response("not a valid label") is None


# ── classify_uncertain — the full flow, subprocess + shutil mocked ───────────

def test_classify_returns_none_when_claude_not_in_path(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: None)
    assert classify_uncertain("Sprint 1") is None


def test_classify_happy_path(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        classify.subprocess, "run",
        lambda *a, **kw: _FakeResult(stdout="sprint\n"),
    )
    assert classify_uncertain("Sprint 1") == "sprint"


def test_classify_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        classify.subprocess, "run",
        lambda *a, **kw: _FakeResult(returncode=1, stderr="boom"),
    )
    assert classify_uncertain("Sprint 1") is None


def test_classify_returns_none_on_empty_stdout(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        classify.subprocess, "run",
        lambda *a, **kw: _FakeResult(stdout=""),
    )
    assert classify_uncertain("Sprint 1") is None


def test_classify_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw.get("timeout", 30))

    monkeypatch.setattr(classify.subprocess, "run", _raise_timeout)
    assert classify_uncertain("Sprint 1") is None


def test_classify_returns_none_on_oserror(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")

    def _raise_oserror(*a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(classify.subprocess, "run", _raise_oserror)
    assert classify_uncertain("Sprint 1") is None


def test_classify_rejects_invalid_label_from_model(monkeypatch):
    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        classify.subprocess, "run",
        lambda *a, **kw: _FakeResult(stdout="rubbish_label"),
    )
    assert classify_uncertain("Weird heading") is None


def test_classify_passes_model_and_timeout_to_subprocess(monkeypatch):
    """The model + timeout args should reach `subprocess.run` verbatim."""
    captured = {}

    def _capture(cmd, capture_output, text, timeout, check):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return _FakeResult(stdout="task")

    monkeypatch.setattr(classify.shutil, "which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(classify.subprocess, "run", _capture)
    result = classify_uncertain(
        "Task M0.1", model="claude-sonnet-4-6", timeout_seconds=5
    )
    assert result == "task"
    assert captured["cmd"][:4] == ["claude", "--print", "--model", "claude-sonnet-4-6"]
    assert captured["timeout"] == 5


# ── Config wiring (light smoke — PlansConfig defaults reach classify) ────────

def test_plans_config_defaults_align_with_classify_defaults():
    """PlansConfig.classify_model must match classify.DEFAULT_MODEL.

    Guards against drift — if one is bumped, the other must follow. This
    is defense against the trap where the config says 'haiku-4-5' but
    classify falls back to a different model when called with defaults.
    """
    from dct.config import PlansConfig
    cfg = PlansConfig()
    assert cfg.classify_model == classify.DEFAULT_MODEL
    assert cfg.classify_timeout_seconds == classify.DEFAULT_TIMEOUT_SECONDS
