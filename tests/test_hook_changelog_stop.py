"""Tests for hooks/check-changelog-on-stop.sh (#354 — latency rework).

Two layers:
  * source-inspection — cheap invariants (git-first ordering, cache wiring)
    that hold even where git/dct are unavailable;
  * behavioral — drive the shell against a throwaway git repo, asserting the
    zero-Python fast paths and the per-project verdict cache (hit, expiry,
    corruption tolerance).

The reminder text and exempt-pattern behavior are unchanged; the reordering
just moves the cheap git change-detection ahead of any Python cold start.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from dct.setup import HOOKS_DIR

HOOK_PATH = HOOKS_DIR / "check-changelog-on-stop.sh"

_HAS_GIT = shutil.which("git") is not None
_HAS_DCT = shutil.which("dct") is not None


# ── source-inspection (no external tools needed) ──────────────────────────────

def test_hook_file_exists():
    assert HOOK_PATH.is_file()


def test_hook_bash_syntax_clean():
    r = subprocess.run(["bash", "-n", str(HOOK_PATH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_consumes_stdin():
    """Rule 2 — Stop-hook JSON arrives on stdin; drain it."""
    assert "$(cat" in HOOK_PATH.read_text()


def test_graceful_on_missing_deps():
    """Rule 3 — git and dct guarded before any real work."""
    src = HOOK_PATH.read_text()
    assert "command -v git" in src
    assert "command -v dct" in src


def test_git_change_detection_runs_before_python():
    """#354 — the git change-detection block must precede any dct/Python call
    so the common 'nothing to remind' turn spends zero Python cold starts."""
    src = HOOK_PATH.read_text()
    first_git_diff = src.index("git diff --name-only")
    first_dct_detect = src.index("dct project detect")
    assert first_git_diff < first_dct_detect, "git detection must come first"


def test_verdict_cache_wiring_present():
    """Cache keyed by a checksum of the project-root path, TTL via find -mmin."""
    src = HOOK_PATH.read_text()
    assert "cksum" in src
    assert "dct-changelog-verdict-" in src
    assert "-mmin" in src
    assert "${TMPDIR:-/tmp}" in src


# ── behavioral (needs git; some also need dct) ────────────────────────────────

@pytest.fixture(autouse=True)
def _requires_hook():
    if not HOOK_PATH.exists():
        pytest.skip("hook script missing")


@pytest.fixture
def repo(tmp_path):
    if not _HAS_GIT:
        pytest.skip("git not available")
    r = tmp_path / "proj"
    r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    return r


@pytest.fixture
def cache_dir(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    return d


def _run(repo, cache_dir):
    env = {**os.environ, "TMPDIR": str(cache_dir)}
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        cwd=str(repo), input="{}", capture_output=True, text=True,
        env=env, timeout=15,
    )


def _cache_file(repo, cache_dir) -> Path:
    """Reproduce the hook's cache path: cksum of the git top-level dir."""
    top = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip()
    key = subprocess.run(
        ["cksum"], input=top, capture_output=True, text=True,
    ).stdout.split()[0]
    return Path(cache_dir) / f"dct-changelog-verdict-{key}"


def test_no_changes_no_reminder(repo, cache_dir):
    """Fast path 1: clean tree → exit 0, no output, no cache written."""
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert not _cache_file(repo, cache_dir).exists()


def test_exempt_only_no_reminder(repo, cache_dir):
    """Fast path 2: only exempt files (docs/) changed → no reminder, no Python."""
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("hi")
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    assert not _cache_file(repo, cache_dir).exists()


def test_changelog_touched_no_reminder(repo, cache_dir):
    """Fast path 3: changelog itself changed → satisfied, no reminder."""
    (repo / "app.py").write_text("x = 1")
    (repo / "changelog_unreleased.md").write_text("- did a thing")
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_cached_skip_suppresses_reminder(repo, cache_dir):
    """A fresh 'skip:' verdict short-circuits before any detection: app file
    changed but the project is cached as not-managed → no reminder."""
    (repo / "app.py").write_text("x = 1")
    cf = _cache_file(repo, cache_dir)
    cf.write_text("skip:\n")
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


@pytest.mark.skipif(not _HAS_DCT, reason="dct CLI required for the reminder path")
def test_cached_manages_is_honored(repo, cache_dir):
    """A fresh 'manages:<slug>' verdict is honored: the hook proceeds to the
    reminder path (the ghost slug has no unreleased entries → count 0), proving
    the cache is read rather than recomputed (recompute in an unregistered repo
    would yield 'skip' and suppress the reminder)."""
    (repo / "app.py").write_text("x = 1")
    cf = _cache_file(repo, cache_dir)
    cf.write_text("manages:ghostproj\n")
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert "Reminder" in r.stdout
    assert "ghostproj" in r.stdout


@pytest.mark.skipif(not _HAS_DCT, reason="dct CLI required to recompute verdict")
def test_stale_cache_is_recomputed(repo, cache_dir):
    """A verdict older than the TTL (3600s) is ignored and recomputed. The
    stale 'manages:ghostproj' would have triggered a reminder if honored;
    recomputation in an unregistered repo yields 'skip' → no reminder and the
    cache file is overwritten."""
    (repo / "app.py").write_text("x = 1")
    cf = _cache_file(repo, cache_dir)
    cf.write_text("manages:ghostproj\n")
    old = time.time() - 7200  # 2h ago > 1h TTL
    os.utime(cf, (old, old))
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert "Reminder" not in r.stdout
    assert cf.read_text().strip() == "skip:"


def test_corrupted_cache_tolerated(repo, cache_dir):
    """A garbage cache file must never crash the hook. It is treated as a miss
    (recomputed when dct is present, or short-circuited when dct is absent);
    either way the hook exits 0 without error."""
    (repo / "app.py").write_text("x = 1")
    cf = _cache_file(repo, cache_dir)
    cf.write_bytes(b"\x00\x01\xff not-a-verdict\n\x00garbage")
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert r.stderr.strip() == ""


def test_symlink_source_invariants():
    """Source-inspection: the hook removes a symlinked cache before reading and
    writes verdicts through mktemp + `mv -f` (never a through-write `>`)."""
    src = HOOK_PATH.read_text()
    assert '[ -L "$cache_file" ] && rm -f "$cache_file"' in src
    assert "mktemp" in src and "mv -f" in src
    # no through-write to the cache file remains on the recompute path
    assert '>"$cache_file"' not in src


@pytest.mark.skipif(not _HAS_DCT, reason="dct CLI required to reach the cache path")
def test_symlinked_cache_not_followed(repo, cache_dir, tmp_path):
    """Security (FIX 3): a pre-planted symlink at the cache path must be removed,
    not followed — a through-write would otherwise truncate the link target
    (any user-writable file)."""
    (repo / "app.py").write_text("x = 1")
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS")
    cf = _cache_file(repo, cache_dir)
    cf.symlink_to(victim)
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert victim.read_text() == "PRECIOUS"   # target untouched
    assert not cf.is_symlink()                # symlink removed, treated as miss
    assert cf.is_file()                       # recompute wrote a real verdict


@pytest.mark.skipif(not _HAS_DCT, reason="dct CLI required to recompute verdict")
def test_atomic_write_leaves_valid_cache(repo, cache_dir):
    """The recompute path writes a valid verdict atomically (mktemp + mv), leaving
    a regular file whose content is a well-formed verdict line."""
    (repo / "app.py").write_text("x = 1")
    cf = _cache_file(repo, cache_dir)
    r = _run(repo, cache_dir)
    assert r.returncode == 0
    assert cf.is_file() and not cf.is_symlink()
    verdict = cf.read_text().strip()
    assert verdict == "skip:" or verdict.startswith("manages:")
    # mv renamed the temp away — no stray sibling temp files (they carry a
    # random dot-suffix; the real cache file has none).
    assert list(Path(cache_dir).glob("dct-changelog-verdict-*.*")) == []
