"""CLI `dct web` daemon helpers — port scan + pidfile lifecycle.

All filesystem-touching helpers take an injectable path so tests NEVER touch the
real ~/.claude/dct-web.pid (Task-9 contract).
"""

import os
import socket

from dct import cli


# ── _find_free_port / _port_free ────────────────────────────────────────────

def test_find_free_port_returns_preferred_when_free():
    # Grab an OS-assigned port then release it — now it is guaranteed free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert cli._find_free_port(free) == free


def test_find_free_port_skips_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        got = cli._find_free_port(busy)
        assert got != busy
        assert got > busy


def test_port_free_detects_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        assert cli._port_free(busy) is False


# ── pidfile lifecycle (injected path + fake pid) ─────────────────────────────

def test_pidfile_lifecycle(tmp_path):
    pidf = tmp_path / "dct-web.pid"
    assert cli._read_pidfile(pidf) is None
    cli._write_pidfile(4242, "http://localhost:8787", pidf)
    assert cli._read_pidfile(pidf) == (4242, "http://localhost:8787")
    cli._clear_pidfile(pidf)
    assert cli._read_pidfile(pidf) is None


def test_pidfile_malformed_returns_none(tmp_path):
    pidf = tmp_path / "dct-web.pid"
    pidf.write_text("not-a-pid\n")
    assert cli._read_pidfile(pidf) is None


def test_clear_pidfile_missing_is_noop(tmp_path):
    cli._clear_pidfile(tmp_path / "nope.pid")  # must not raise


def test_pid_alive_self_true():
    assert cli._pid_alive(os.getpid()) is True


def test_pid_alive_bogus_false():
    assert cli._pid_alive(2_000_000_000) is False


# ── parser wiring ────────────────────────────────────────────────────────────

def test_web_subparser_wired():
    parser = cli.build_parser()
    args = parser.parse_args(["web", "start", "--port", "9001", "--no-fallback"])
    assert args.command == "web"
    assert args.web_command == "start"
    assert args.port == 9001
    assert args.no_fallback is True


# ── daemon stdio detach (regression: `$(dct web start)` must not hang) ─────────

def test_detach_stdio_redirects_away_from_inherited_stdout(tmp_path):
    """The daemonized child must NOT keep writing to its inherited stdout — that
    is what made `$(dct web start)` block forever on the never-closing pipe.
    After _detach_stdio, fd 1 must point at the log file, leaving the inherited
    pipe with no open write end (parent reads EOF)."""
    log = tmp_path / "web.log"
    r, w = os.pipe()  # stand-in for the caller's captured stdout pipe
    pid = os.fork()
    if pid == 0:  # child
        os.dup2(w, 1)  # inherit the pipe as stdout, as a real `$(...)` would
        os.close(r)
        os.close(w)
        cli._detach_stdio(log)
        os.write(1, b"daemon-log-line\n")  # fd 1 is now the log file, not the pipe
        os._exit(0)
    os.close(w)
    leaked = os.read(r, 4096)  # empty == child closed the inherited write end
    os.close(r)
    os.waitpid(pid, 0)
    assert leaked == b""
    assert "daemon-log-line" in log.read_text()
