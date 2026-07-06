"""Unit tests for CLI prompt helpers — tty-safety contract."""

import sys

from dct.cli import _prompt_version


def test_prompt_version_returns_none_when_not_tty(monkeypatch):
    """Contract: non-interactive stdin → None (caller must handle)."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _prompt_version() is None


def test_prompt_version_empty_input_returns_default(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _prompt_version() == "0.1.0"


def test_prompt_version_custom_default(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _prompt_version(default="2.0.0") == "2.0.0"


def test_prompt_version_shortcut_1_pre_alpha(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "1")
    assert _prompt_version() == "0.0.1"


def test_prompt_version_shortcut_2_alpha(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "2")
    assert _prompt_version() == "0.1.0"


def test_prompt_version_shortcut_3_stable(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "3")
    assert _prompt_version() == "1.0.0"


def test_prompt_version_accepts_freeform(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "2.5.0")
    assert _prompt_version() == "2.5.0"


def test_prompt_version_strips_whitespace(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "  1.2.3  ")
    assert _prompt_version() == "1.2.3"
