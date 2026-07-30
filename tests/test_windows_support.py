"""Windows-support fixes W4-W10 + W7 (ADR #52 scope, CP #1253-#1256).

Windows semantics that cannot execute on POSIX are tested through
injectable seams (ntpath.normcase, _IS_WINDOWS flags, spied helpers) —
the dispatch and argument shapes are locked here; the platform-only
syscalls (OpenProcess, DETACHED_PROCESS) are exercised only on Windows.
"""

import argparse
import json
import ntpath

from dct import cli
from dct.core import projects as projects_mod
from dct.integrations import claude as claude_mod

# ── W4: CWD project auto-detection (core/projects.py) ────────────────────────


def test_path_containment_windows_semantics():
    """Hardcoded '/' + case-sensitive compare silently disabled auto-detection
    on Windows: every subdir failed to resolve, no-oping hooks and project=''
    MCP calls. normcase fixes separator AND case in one call."""
    within = projects_mod._path_is_within
    assert within(r"C:\Proj\Sub", "c:/proj", normcase=ntpath.normcase, sep="\\")
    assert within(r"c:\proj", r"C:\PROJ", normcase=ntpath.normcase, sep="\\")
    assert not within(r"C:\projX", r"C:\proj", normcase=ntpath.normcase, sep="\\")


def test_path_containment_posix_unchanged():
    within = projects_mod._path_is_within
    assert within("/a/b/c", "/a/b")
    assert within("/a/b", "/a/b")
    assert not within("/a/bc", "/a/b")      # prefix ≠ containment
    assert not within("/a/B", "/a/b")       # POSIX stays case-sensitive


# ── W7: UTF-8 on host-file I/O (integrations/claude.py) ──────────────────────


def test_register_mcp_fallback_preserves_utf8(tmp_path, monkeypatch):
    """~/.claude.json holds the user's whole Claude Code config — a cp1252
    re-encode of non-ASCII content would corrupt it (W7)."""
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"żółć": "gęśla jaźń"}, ensure_ascii=False),
                  encoding="utf-8")
    monkeypatch.setattr(claude_mod, "CLAUDE_JSON", cj)
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: None)

    ok, _how = claude_mod.register_mcp(None)

    assert ok
    data = json.loads(cj.read_bytes().decode("utf-8"))
    assert data["żółć"] == "gęśla jaźń"          # existing content intact
    assert data["mcpServers"]["dct"]["command"] == "dct"


# ── W6: claude .cmd shim must not crash dct init ──────────────────────────────


def test_register_mcp_survives_unexecutable_shim(tmp_path, monkeypatch):
    """On Windows shutil.which finds claude.cmd via PATHEXT but CreateProcess
    cannot exec it from an argv list → FileNotFoundError killed dct init
    before the ~/.claude.json fallback was ever reached (W6)."""
    cj = tmp_path / ".claude.json"
    monkeypatch.setattr(claude_mod, "CLAUDE_JSON", cj)
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _: r"C:\bin\claude.cmd")

    def boom(*a, **k):
        raise FileNotFoundError("CreateProcess cannot run .cmd from argv")

    monkeypatch.setattr(claude_mod.subprocess, "run", boom)

    ok, how = claude_mod.register_mcp(None)
    assert ok
    assert "direct" in how
    assert json.loads(cj.read_text(encoding="utf-8"))["mcpServers"]["dct"]


# ── W5: symlink → copy fallback (no Developer Mode on Windows) ────────────────


def _fail_symlink(monkeypatch):
    from pathlib import Path

    def boom(self, target, *a, **k):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)


def test_install_hook_falls_back_to_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(claude_mod, "CLAUDE_HOOKS_DIR", tmp_path / "hooks")
    (tmp_path / "hooks").mkdir()
    _fail_symlink(monkeypatch)

    assert claude_mod.install_hook(None) is True
    installed = list((tmp_path / "hooks").iterdir())
    assert installed and all(not p.is_symlink() for p in installed)
    assert "copie" in capsys.readouterr().out  # drift warning printed


def test_install_skills_falls_back_to_copytree(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(claude_mod, "CLAUDE_SKILLS_DIR", tmp_path / "skills")
    (tmp_path / "skills").mkdir()
    _fail_symlink(monkeypatch)

    assert claude_mod.install_skills(None) is True
    installed = [p for p in (tmp_path / "skills").iterdir() if p.is_dir()]
    assert installed and all(not p.is_symlink() for p in installed)
    assert (installed[0] / "SKILL.md").exists()  # real copied content


# ── W9: liveness probe must never signal on Windows ──────────────────────────


def test_pid_alive_dispatches_to_windows_probe(monkeypatch):
    """os.kill(pid, 0) on Windows TERMINATES the probed process — the
    Windows branch must use OpenProcess, never os.kill (W9)."""
    calls = []
    monkeypatch.setattr(cli, "_IS_WINDOWS", True)
    monkeypatch.setattr(cli, "_pid_alive_windows", lambda pid: calls.append(pid) or True)

    def forbidden(*a, **k):
        raise AssertionError("os.kill must not run on the Windows path")

    monkeypatch.setattr(cli.os, "kill", forbidden)
    assert cli._pid_alive(4242) is True
    assert calls == [4242]


# ── W8: daemonization without fork on Windows ─────────────────────────────────


def test_web_start_windows_uses_detached_popen(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(cli, "_IS_WINDOWS", True)
    monkeypatch.setattr(cli, "_port_free", lambda port, host="127.0.0.1": True)

    def forbidden_fork():
        raise AssertionError("os.fork must not run on the Windows path")

    monkeypatch.setattr(cli.os, "fork", forbidden_fork, raising=False)

    captured = {}

    class FakeProc:
        pid = 7777

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["creationflags"] = kwargs.get("creationflags", 0)
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    cli.cmd_web_start(argparse.Namespace(port=9797, no_fallback=True))

    assert "_web-serve" in captured["argv"]
    DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200
    assert captured["creationflags"] & DETACHED_PROCESS
    assert captured["creationflags"] & CREATE_NEW_PROCESS_GROUP
    assert cli._read_pidfile() == (7777, "http://localhost:9797")


def test_internal_web_serve_subcommand_wired():
    parser = cli.build_parser()
    args = parser.parse_args(["_web-serve", "--host", "127.0.0.1", "--port", "9001"])
    assert args.command == "_web-serve"
    assert args.port == 9001


# ── W10: stop must survive Windows exception types ────────────────────────────


def test_web_stop_handles_windows_oserror(tmp_path, monkeypatch, capsys):
    """Windows raises plain OSError (not ProcessLookupError) for a missing
    pid — stop must clear the pidfile instead of crashing (W10)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    cli._write_pidfile(31337, "http://localhost:8787")

    def winerr(pid, sig):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(cli.os, "kill", winerr)
    cli.cmd_web_stop(argparse.Namespace())
    assert cli._read_pidfile() is None            # stale pidfile cleared
    assert "not alive" in capsys.readouterr().out
