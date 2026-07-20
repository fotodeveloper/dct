"""Tests for the passive update check + dct upgrade support (#356).

Covers version comparison, cache TTL/staleness, opt-out, offline silence,
install-method detection, dangling-symlink cleanup, and the CLI nudge
suppression matrix.
"""

import argparse
import json
import os
import stat
import time

import pytest

import dct.cli as cli
import dct.integrations.claude as setup
import dct.update as update
from dct.config import Config, UpdateConfig, save_config, load_config


# ── version comparison ───────────────────────────────────────────────────────

@pytest.mark.parametrize("latest,current,expected", [
    ("1.0.1", "1.0.0", True),
    ("1.0.0", "1.0.0", False),
    ("0.9.0", "1.0.0", False),
    ("1.10.0", "1.2.0", True),    # numeric, not lexicographic
    ("1.2.0", "1.10.0", False),
    ("2.0.0", "1.9.9", True),
    ("1.0", "1.0.0", False),      # zero-padded equal
    ("1.0.0", "1.0", False),
    ("1.0.1", "1.0", True),
    # PEP 440-lite pre-release ordering on an EQUAL numeric release:
    ("1.2.3rc1", "1.2.3", False),  # a pre-release is NOT newer than its final
    ("1.2.3", "1.2.3rc1", True),   # the final release supersedes its pre-release
    ("1.2.3", "1.2.3.dev1", True),
    ("1.2.3", "1.2.3b2", True),
    ("1.2.3-alpha", "1.2.3", False),
    ("1.2.3rc1", "1.2.3rc1", False),  # two pre-releases of equal release → equal
    ("1.2.4rc1", "1.2.3", True),      # higher release wins even as a pre-release
    ("", "1.0.0", False),          # unparseable latest → never newer
    ("garbage", "1.0.0", False),
])
def test_is_newer(latest, current, expected):
    assert update._is_newer(latest, current) is expected


@pytest.mark.parametrize("value,expected", [
    ("1.2.3", False),
    ("1.2.3rc1", True),
    ("1.2.3.dev1", True),
    ("1.2.3-alpha", True),
    ("1.2.3b2", True),
    ("1.2.3beta", True),
    ("v1.2.3", False),
    ("1.0", False),
])
def test_is_prerelease(value, expected):
    assert update._is_prerelease(value) is expected


def test_parse_version_best_effort():
    assert update._parse_version("1.2.3") == (1, 2, 3)
    assert update._parse_version("1.2.3rc1") == (1, 2, 3)  # leading digits per chunk
    assert update._parse_version("1.2.x") == (1, 2)        # non-numeric chunk stops
    assert update._parse_version("v1") == ()               # leading non-digit → empty
    assert update._parse_version("") == ()


# ── cached notice (no network) ───────────────────────────────────────────────

def _write_cache(path, latest, checked_at=None, current="1.0.0"):
    path.write_text(json.dumps({
        "checked_at": time.time() if checked_at is None else checked_at,
        "latest": latest,
        "current": current,
    }))


def test_get_cached_notice_when_newer(tmp_path):
    cache = tmp_path / "cache.json"
    _write_cache(cache, latest="2.0.0")
    notice = update.get_cached_notice(cache, current="1.0.0")
    assert notice is not None
    assert "2.0.0" in notice and "1.0.0" in notice
    assert "dct upgrade" in notice


def test_get_cached_notice_when_not_newer(tmp_path):
    cache = tmp_path / "cache.json"
    _write_cache(cache, latest="1.0.0")
    assert update.get_cached_notice(cache, current="1.0.0") is None


def test_get_cached_notice_missing_cache(tmp_path):
    assert update.get_cached_notice(tmp_path / "nope.json", current="1.0.0") is None


def test_get_cached_notice_malformed_cache(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text("{not json")
    assert update.get_cached_notice(cache, current="1.0.0") is None


def test_get_cached_notice_no_latest_key(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"checked_at": time.time()}))
    assert update.get_cached_notice(cache, current="1.0.0") is None


def test_get_cached_notice_opt_out(tmp_path):
    """[update] check=false suppresses the notice even with a fresh cache
    advertising a strictly-newer release — the opt-out disables BOTH the
    background probe and the notice (module docstring)."""
    cache = tmp_path / "cache.json"
    _write_cache(cache, latest="2.0.0")
    off = Config(update=UpdateConfig(check=False))
    assert update.get_cached_notice(cache, current="1.0.0", config=off) is None
    # sanity: the same cache DOES yield a notice when the opt-in is active,
    # proving the suppression is the opt-out and not a stale/not-newer cache.
    on = Config(update=UpdateConfig(check=True))
    assert update.get_cached_notice(cache, current="1.0.0", config=on) is not None


# ── cache TTL / staleness ────────────────────────────────────────────────────

def test_cache_is_fresh_true_when_recent():
    now = 1_000_000.0
    assert update._cache_is_fresh({"checked_at": now - 10}, now) is True


def test_cache_is_fresh_false_when_stale():
    now = 1_000_000.0
    assert update._cache_is_fresh({"checked_at": now - update.TTL_SECONDS - 1}, now) is False


def test_cache_is_fresh_false_on_missing_or_bad():
    assert update._cache_is_fresh(None, 1.0) is False
    assert update._cache_is_fresh({}, 1.0) is False
    assert update._cache_is_fresh({"checked_at": "nan"}, 1.0) is False


def test_check_for_update_skips_when_fresh(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    now = 1_000_000.0
    _write_cache(cache, latest="1.0.0", checked_at=now)
    called = []
    monkeypatch.setattr(update, "_refresh_cache", lambda **kw: called.append(kw))
    thread = update.check_for_update(Config(update=UpdateConfig(check=True)),
                                     cache_path=cache, now=now)
    assert thread is None
    assert called == []


def test_check_for_update_spawns_when_stale(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    now = 1_000_000.0
    _write_cache(cache, latest="1.0.0", checked_at=now - update.TTL_SECONDS - 1)
    called = []
    monkeypatch.setattr(update, "_refresh_cache", lambda **kw: called.append(kw))
    thread = update.check_for_update(Config(update=UpdateConfig(check=True)),
                                     cache_path=cache, now=now)
    assert thread is not None
    thread.join(timeout=5)
    assert called and called[0]["cache_path"] == cache


def test_check_for_update_spawns_when_cache_missing(tmp_path, monkeypatch):
    cache = tmp_path / "nope.json"
    called = []
    monkeypatch.setattr(update, "_refresh_cache", lambda **kw: called.append(kw))
    thread = update.check_for_update(Config(update=UpdateConfig(check=True)),
                                     cache_path=cache, now=1.0)
    assert thread is not None
    thread.join(timeout=5)
    assert called


# ── opt-out ──────────────────────────────────────────────────────────────────

def test_check_for_update_opt_out(tmp_path, monkeypatch):
    cache = tmp_path / "nope.json"
    called = []
    monkeypatch.setattr(update, "_refresh_cache", lambda **kw: called.append(kw))
    thread = update.check_for_update(Config(update=UpdateConfig(check=False)),
                                     cache_path=cache, now=1.0)
    assert thread is None
    assert called == []


def test_config_update_roundtrip(tmp_path):
    path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///x.db", update=UpdateConfig(check=False)), path)
    assert "[update]" in path.read_text()
    assert load_config(path).update.check is False


def test_config_update_defaults_true(tmp_path):
    path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///x.db"), path)
    assert load_config(path).update.check is True


# ── offline silence / refresh ────────────────────────────────────────────────

def test_refresh_cache_silent_when_offline(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(update, "urlopen", boom)
    # must not raise, must not create the cache
    update._refresh_cache(cache_path=cache)
    assert not cache.exists()


def test_write_cache_is_atomic(tmp_path):
    """Cache write leaves a valid JSON file and no stray temp file behind."""
    cache = tmp_path / "cache.json"
    update._write_cache(cache, {"latest": "1.2.3", "checked_at": 1.0})
    assert json.loads(cache.read_text())["latest"] == "1.2.3"
    assert list(tmp_path.glob("*.tmp")) == []  # no leftover temp files


def test_read_cache_tolerates_partial_write(tmp_path):
    """A truncated/corrupt cache file is treated as a miss, never raises (FIX 4)."""
    cache = tmp_path / "cache.json"
    cache.write_text('{"checked_at": 123, "lat')  # partial write
    assert update._read_cache(cache) is None
    # and the notice layer treats it as no-cache, silently
    assert update.get_cached_notice(cache, current="1.0.0") is None


def test_refresh_cache_writes_on_success(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(update, "_fetch_latest_version", lambda **k: "9.9.9")
    update._refresh_cache(cache_path=cache, current="1.0.0")
    data = json.loads(cache.read_text())
    assert data["latest"] == "9.9.9"
    assert data["current"] == "1.0.0"
    assert isinstance(data["checked_at"], (int, float))


def test_fetch_latest_version_parses_pypi_payload(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"info": {"version": "3.1.4"}}).encode()

    # json.load reads .read() off the response object
    monkeypatch.setattr(update, "urlopen", lambda *a, **k: FakeResp())
    assert update._fetch_latest_version() == "3.1.4"


# ── install-method detection ─────────────────────────────────────────────────

def test_detect_editable_via_dir_info(monkeypatch):
    monkeypatch.setattr(update, "_read_direct_url",
                        lambda *a: {"dir_info": {"editable": True}})
    assert update.detect_install_method() == "editable"


def test_detect_file_url_without_flag_is_pip(monkeypatch):
    """A ``file:`` direct_url WITHOUT dir_info.editable (a local wheel or
    ``pip install .``) is NOT editable — it upgrades like any pip install."""
    monkeypatch.setattr(update, "_read_direct_url",
                        lambda *a: {"url": "file:///home/x/dev/dct", "dir_info": {}})
    import sys
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    assert update.detect_install_method() == "pip"


def test_detect_non_dict_dir_info_degrades(monkeypatch):
    """Corrupt metadata: ``dir_info`` that is valid JSON but not an object must
    degrade to "not editable" — never AttributeError inside ``dct upgrade``."""
    monkeypatch.setattr(update, "_read_direct_url",
                        lambda *a: {"url": "file:///home/x/dev/dct", "dir_info": "bogus"})
    import sys
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    assert update.detect_install_method() == "pip"


def test_read_direct_url_non_object_json_is_none(monkeypatch):
    """PEP 610 mandates an object; a corrupt ``direct_url.json`` holding a bare
    JSON array/string must read as "no metadata" (None), not leak downstream."""
    class _FakeDist:
        def read_text(self, name):
            return "[1, 2]"

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _FakeDist())
    assert update._read_direct_url() is None


def test_detect_no_direct_url_falls_to_prefix(monkeypatch):
    """No direct_url.json → classification comes purely from the prefix probe
    (here: a vanilla prefix → pip)."""
    monkeypatch.setattr(update, "_read_direct_url", lambda *a: None)
    import sys
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    assert update.detect_install_method() == "pip"


def test_detect_uv(monkeypatch):
    monkeypatch.setattr(update, "_read_direct_url", lambda *a: None)
    import sys
    monkeypatch.setattr(sys, "prefix", "/home/x/.local/share/uv/tools/dctracker")
    monkeypatch.setattr(sys, "executable", "/home/x/.local/share/uv/tools/dctracker/bin/python")
    assert update.detect_install_method() == "uv"


def test_detect_pipx(monkeypatch):
    monkeypatch.setattr(update, "_read_direct_url", lambda *a: None)
    import sys
    monkeypatch.setattr(sys, "prefix", "/home/x/.local/pipx/venvs/dctracker")
    monkeypatch.setattr(sys, "executable", "/home/x/.local/pipx/venvs/dctracker/bin/python")
    assert update.detect_install_method() == "pipx"


def test_detect_pip_fallback(monkeypatch):
    monkeypatch.setattr(update, "_read_direct_url", lambda *a: None)
    import sys
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    assert update.detect_install_method() == "pip"


def test_upgrade_command_shapes():
    import sys
    assert update.upgrade_command("uv") == ["uv", "tool", "upgrade", "dctracker"]
    assert update.upgrade_command("pipx") == ["pipx", "upgrade", "dctracker"]
    assert update.upgrade_command("pip") == [
        sys.executable, "-m", "pip", "install", "-U", "dctracker"
    ]
    assert update.upgrade_command("editable") is None


# ── dangling-symlink cleanup ─────────────────────────────────────────────────

def test_prune_dangling_symlinks(tmp_path, monkeypatch):
    pkg_skills = tmp_path / "pkg" / "skills"
    pkg_hooks = tmp_path / "pkg" / "hooks"
    claude_skills = tmp_path / "claude" / "skills"
    claude_hooks = tmp_path / "claude" / "hooks"
    for d in (pkg_skills, pkg_hooks, claude_skills, claude_hooks):
        d.mkdir(parents=True)

    monkeypatch.setattr(setup, "SKILLS_DIR", pkg_skills)
    monkeypatch.setattr(setup, "HOOKS_DIR", pkg_hooks)
    monkeypatch.setattr(setup, "CLAUDE_SKILLS_DIR", claude_skills)
    monkeypatch.setattr(setup, "CLAUDE_HOOKS_DIR", claude_hooks)

    # live link → an existing skill in the package (must be kept)
    (pkg_skills / "alive").mkdir()
    live = claude_skills / "alive"
    live.symlink_to(pkg_skills / "alive")

    # dangling link → a skill that no longer ships in the package (must be removed)
    dangling = claude_skills / "gone"
    dangling.symlink_to(pkg_skills / "gone")

    # dangling link → outside the package, a foreign tool (must be kept)
    foreign = claude_skills / "foreign"
    foreign.symlink_to(tmp_path / "elsewhere" / "thing")

    # dangling hook link into the package (must be removed)
    dead_hook = claude_hooks / "dct-old-hook.sh"
    dead_hook.symlink_to(pkg_hooks / "old-hook.sh")

    removed = setup.prune_dangling_symlinks()

    assert str(dangling) in removed
    assert str(dead_hook) in removed
    assert not dangling.is_symlink()
    assert not dead_hook.is_symlink()
    assert live.is_symlink()      # live link untouched
    assert foreign.is_symlink()   # foreign dangling link untouched


def test_prune_dangling_symlinks_no_dirs(tmp_path, monkeypatch):
    """Absent ~/.claude/skills or hooks dirs are tolerated (returns [])."""
    monkeypatch.setattr(setup, "CLAUDE_SKILLS_DIR", tmp_path / "missing-skills")
    monkeypatch.setattr(setup, "CLAUDE_HOOKS_DIR", tmp_path / "missing-hooks")
    assert setup.prune_dangling_symlinks() == []


def test_prune_reverifies_islink_before_unlink(tmp_path, monkeypatch):
    """TOCTOU guard (FIX 5): if the entry stops being a symlink between the scan
    and the unlink (a regular file swapped in), it must NOT be removed. Simulated
    by patching the dedicated ``setup._still_symlink`` recheck seam — NOT the
    global ``os.path.islink``, which since Python 3.13 also backs
    ``Path.is_symlink`` (patching it globally would blind the scan filter and
    the test's own survival assertion alike)."""
    pkg_skills = tmp_path / "pkg" / "skills"
    pkg_hooks = tmp_path / "pkg" / "hooks"
    claude_skills = tmp_path / "claude" / "skills"
    claude_hooks = tmp_path / "claude" / "hooks"
    for d in (pkg_skills, pkg_hooks, claude_skills, claude_hooks):
        d.mkdir(parents=True)
    monkeypatch.setattr(setup, "SKILLS_DIR", pkg_skills)
    monkeypatch.setattr(setup, "HOOKS_DIR", pkg_hooks)
    monkeypatch.setattr(setup, "CLAUDE_SKILLS_DIR", claude_skills)
    monkeypatch.setattr(setup, "CLAUDE_HOOKS_DIR", claude_hooks)

    dangling = claude_skills / "gone"
    dangling.symlink_to(pkg_skills / "gone")  # dangling link into the package

    # The pre-unlink recheck sees a regular file (the swap); the scan filter
    # still sees the genuine symlink, so the entry reaches the recheck.
    monkeypatch.setattr(setup, "_still_symlink", lambda p: False)

    removed = setup.prune_dangling_symlinks()
    assert str(dangling) not in removed
    # Survival asserted via lstat, independent of any symlink-predicate patching.
    assert stat.S_ISLNK(os.lstat(dangling).st_mode)  # survived — never unlinked


# ── CLI nudge suppression ────────────────────────────────────────────────────

def _ns(**kw):
    return argparse.Namespace(**kw)


@pytest.mark.parametrize("args,suppressed", [
    (_ns(command="list"), False),
    (_ns(command="show"), False),
    (_ns(command="add"), False),
    (_ns(command="_task-mirror"), True),                # underscore command
    (_ns(command=None), True),                          # no command
    (_ns(command="project", project_command="detect"), True),
    (_ns(command="project", project_command="manages-changelog"), True),
    (_ns(command="project", project_command="list"), False),
    (_ns(command="clog", clog_command="count"), True),
    (_ns(command="clog", clog_command="list"), False),
    (_ns(command="config", config_command="get"), True),
])
def test_nudge_suppressed(args, suppressed):
    assert cli._nudge_suppressed(args) is suppressed


def test_maybe_nudge_silent_when_not_tty(monkeypatch, capsys):
    class FakeStderr:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stderr", FakeStderr())
    calls = []
    monkeypatch.setattr(update, "check_for_update", lambda *a, **k: calls.append("cfu"))
    monkeypatch.setattr(update, "get_cached_notice", lambda *a, **k: calls.append("gcn"))
    # Should return without printing anything AND without touching the update
    # machinery at all (the non-tty gate short-circuits before either call).
    cli._maybe_nudge(_ns(command="list"))
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert calls == []


def test_maybe_nudge_registers_bounded_probe_join(tmp_path, monkeypatch):
    """FIX 2: a short command's background probe still populates the cache — the
    daemon thread is joined (bounded) at interpreter exit via an atexit hook."""
    cache = tmp_path / "cache.json"  # missing → stale → probe spawns

    class FakeStderr:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stderr", FakeStderr())
    monkeypatch.setattr(update, "_fetch_latest_version", lambda **k: "9.9.9")

    registered = []
    monkeypatch.setattr(cli.atexit, "register",
                        lambda fn, *a: registered.append((fn, a)))

    # Drive check_for_update + join registration exactly as _maybe_nudge does.
    thread = update.check_for_update(Config(update=UpdateConfig(check=True)),
                                     cache_path=cache)
    cli._register_probe_join(thread)
    assert registered, "a started probe must register a bounded atexit join"
    fn, args = registered[0]
    assert args == (cli.NUDGE_JOIN_TIMEOUT,)
    fn(*args)  # run the join now — simulates interpreter exit
    assert json.loads(cache.read_text())["latest"] == "9.9.9"


def test_register_probe_join_noop_when_opted_out(tmp_path, monkeypatch):
    """Opted-out config starts no probe → nothing to join, nothing registered."""
    cache = tmp_path / "cache.json"
    thread = update.check_for_update(Config(update=UpdateConfig(check=False)),
                                     cache_path=cache)
    assert thread is None
    registered = []
    monkeypatch.setattr(cli.atexit, "register",
                        lambda *a, **k: registered.append(a))
    cli._register_probe_join(thread)
    assert registered == []


def test_upgrade_warns_when_relink_fails(monkeypatch, capsys):
    """FIX 7: when the skill/hook re-link raises, print a warning naming the error
    and suggesting `dct init` — NOT the success line."""
    monkeypatch.setattr(update, "detect_install_method", lambda *a, **k: "editable")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(setup, "install_skills", boom)
    cli.cmd_upgrade(_ns())
    out = capsys.readouterr()
    assert "disk full" in out.err
    assert "dct init" in out.err
    assert "Skills and hooks re-linked" not in out.out


def test_maybe_nudge_prints_notice_on_tty(monkeypatch, capsys):
    printed = []

    class FakeStderr:
        def isatty(self):
            return True
        def write(self, s):
            printed.append(s)
        def flush(self):
            pass

    monkeypatch.setattr(cli.sys, "stderr", FakeStderr())
    monkeypatch.setattr(update, "check_for_update", lambda *a, **k: None)
    monkeypatch.setattr(update, "get_cached_notice",
                        lambda *a, **k: "dct 9.9.9 is available (installed 1.0.0) — run: dct upgrade")
    cli._maybe_nudge(_ns(command="list"))
    assert any("dct 9.9.9 is available" in s for s in printed)


def test_cache_path_uses_platform_cache_dir(tmp_path, monkeypatch):
    """ADR #52: the update-check cache moves out of ~/.claude with the rest."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert update._cache_path() == home / ".cache" / "dctools" / "dct-update-check.json"
