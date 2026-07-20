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

def test_classification_config_defaults_align_with_classify_defaults():
    """ClassificationConfig defaults must match classify.DEFAULT_* (keys
    moved from [plans] in the ADR #52 rename window).

    Guards against drift — if one is bumped, the other must follow. This
    is defense against the trap where the config says 'haiku-4-5' but
    classify falls back to a different model when called with defaults.
    """
    from dct.config import ClassificationConfig
    cfg = ClassificationConfig()
    assert cfg.model == classify.DEFAULT_MODEL
    assert cfg.timeout_seconds == classify.DEFAULT_TIMEOUT_SECONDS


# ── CP #1257: config actually reaches the classifier (round-1 audit bug) ─────


def test_ingest_threads_classification_config(tmp_path, monkeypatch, engine):
    """plans.classify_* were parsed and documented but NEVER passed to
    classify_uncertain (ingest.py:283) — a user override was silently
    ignored. This asserts the config value flows into the call."""
    import dct.core.plans.ingest as ingest_mod
    from dct.config import Config, save_config
    from dct.core.projects import create_project

    captured = {}

    def fake_classify(heading, parent_heading=None, preview="",
                      model=None, timeout_seconds=None, **kw):
        captured["model"] = model
        captured["timeout_seconds"] = timeout_seconds
        return "prose"

    monkeypatch.setattr(ingest_mod.classify_mod, "classify_uncertain", fake_classify)

    cfg = Config(database_url="sqlite:///x.db")
    cfg.classification.model = "threaded-model"
    cfg.classification.timeout_seconds = 5
    cfg_path = tmp_path / "dct.toml"
    save_config(cfg, cfg_path)
    monkeypatch.setenv("DCT_CONFIG_PATH", str(cfg_path))

    create_project(engine, slug="cls", name="Cls", path=str(tmp_path))
    plan_md = tmp_path / "plan.md"
    plan_md.write_text("# Plan\n\n## zagadkowa sekcja bez sygnału\n\ntekst\n")
    ingest_mod.ingest_file(engine, str(plan_md), "cls", auto_resolve_uncertain=True)

    assert captured.get("model") == "threaded-model"
    assert captured.get("timeout_seconds") == 5


def test_provider_disabled_skips_classifier(tmp_path, monkeypatch, engine):
    import dct.core.plans.ingest as ingest_mod
    from dct.config import Config, save_config
    from dct.core.projects import create_project

    called = []
    monkeypatch.setattr(
        ingest_mod.classify_mod, "classify_uncertain",
        lambda *a, **k: called.append(1) or "prose",
    )
    cfg = Config(database_url="sqlite:///x.db")
    cfg.classification.provider = "disabled"
    cfg_path = tmp_path / "dct.toml"
    save_config(cfg, cfg_path)
    monkeypatch.setenv("DCT_CONFIG_PATH", str(cfg_path))

    create_project(engine, slug="cls2", name="Cls2", path=str(tmp_path))
    plan_md = tmp_path / "plan.md"
    plan_md.write_text("# Plan\n\n## zagadkowa sekcja bez sygnału\n\ntekst\n")
    ingest_mod.ingest_file(engine, str(plan_md), "cls2", auto_resolve_uncertain=True)

    assert called == []
