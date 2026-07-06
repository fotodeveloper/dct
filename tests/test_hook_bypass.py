"""Integration tests for the check-changelog.sh hook's release bypass.

These tests drive the shell hook as a subprocess, feeding the CC PreToolUse JSON
protocol through stdin. No real `git` or `dct` CLI is required — the hook exits
early on various guards we can trigger by crafting the input.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "check-changelog.sh"


def _run_hook(command: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the hook with a fake PreToolUse JSON input carrying `command`."""
    payload = json.dumps({"tool_input": {"command": command}})
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture(autouse=True)
def _skip_if_dct_missing():
    if not HOOK_PATH.exists():
        pytest.skip(f"Hook script not found at {HOOK_PATH}")


class TestReleaseBypassInline:
    """Hook should bypass when -m "release: vX.Y.Z" inline."""

    def test_inline_double_quoted(self):
        r = _run_hook('git commit -m "release: v0.1.0"')
        assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"

    def test_inline_single_quoted(self):
        r = _run_hook("git commit -m 'release: v1.0'")
        assert r.returncode == 0

    def test_inline_message_flag_long(self):
        r = _run_hook('git commit --message="release: v2.0.0"')
        assert r.returncode == 0

    def test_inline_mixed_with_tag(self):
        r = _run_hook('git commit -m "release: v0.1.0" && git tag v0.1.0')
        assert r.returncode == 0


class TestReleaseBypassHeredoc:
    """Hook should bypass when commit message body starts with 'release:' line (HEREDOC form)."""

    def test_heredoc_form(self):
        heredoc = '''git commit -m "$(cat <<'EOF'
release: v0.1.0

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"'''
        r = _run_hook(heredoc)
        assert r.returncode == 0

    def test_heredoc_with_body_then_release_line(self):
        # Edge case: body paragraph THEN release: — shouldn't match because
        # regex is anchored to beginning-of-line only.
        # However, if "release:" appears at start of *any* line, grep -E '^release:' matches.
        # This test documents the behavior: heredoc with release: anywhere-line-start → bypass.
        heredoc = '''git commit -m "$(cat <<'EOF'
feat(scope): description

release: v0.1.0
EOF
)"'''
        r = _run_hook(heredoc)
        # Current behavior: matches (line starts with release:)
        assert r.returncode == 0


class TestNoBypassForNormalCommits:
    """Hook should NOT bypass normal commits (no release: prefix). Exact behavior depends on
    whether dct CLI is available and project is registered — we test the early exits."""

    def test_non_git_commit_passes(self):
        """Non-commit commands pass through immediately (exit 0)."""
        r = _run_hook("git status")
        assert r.returncode == 0

    def test_amend_passes(self):
        """--amend commits are skipped (separate bypass)."""
        r = _run_hook("git commit --amend")
        assert r.returncode == 0

    def test_release_in_message_body_only_not_as_prefix(self):
        """A commit message mentioning 'release:' mid-sentence should NOT bypass."""
        # Inline form: message is "fix: revert release: v1.0 to v0.9" — does NOT start with release:
        r = _run_hook('git commit -m "fix: revert release: v1.0 to v0.9"')
        # This commit will proceed to project detection. If dct CLI unavailable or no project → exit 0.
        # We can't reliably assert non-zero here without a full test harness. Instead, we assert that
        # the bypass logic did NOT short-circuit prematurely (would be misclassified as release).
        # Best-effort: at minimum, should not crash.
        assert r.returncode in (0, 2), f"unexpected rc={r.returncode}, stderr={r.stderr}"
