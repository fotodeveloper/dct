"""The ONE supervised process runner (dct.core.procs) and the lock ownership
model built on it (dct.workflows.release.oplock) — K-108, K-113.

Real processes only: a FAMILY (leader → child → grandchild, each appending
ticks to a file, optionally ignoring SIGTERM / SIGHUP / SIGINT) run through
the runner, and a HELPER process standing for dct itself (it takes the
operation lock and supervises the family) that the tests signal or kill.

- K-108: on a timeout, an interruption (SIGINT, SIGTERM, SIGHUP — the two
  latter converted to an exception while an operation is supervised) or an
  error in the caller the WHOLE process group is terminated and reaped
  BEFORE resources are removed and BEFORE the operation lock is released;
  the temporary signal handlers are restored; a hard death of the
  supervisor (SIGKILL) leaves a holder record whose still-running group
  makes the next operation refuse.
- K-113: stdout and stderr are drained concurrently into bounded buffers
  (discarded bytes counted, a cut UTF-8 sequence never decoded as garbage);
  output that is PROOF is complete or refused, never cut.

Barriers are FIFOs (the family reports "ready" through one). Liveness is
asserted the moment the supervised call returned — never polled for.

Cleanup never signals a number read from a .pid file: once a member is
gone its number may name any process on the machine, and a check before
the signal would still race with its reuse. A family is ended by asking
its members to leave (they all honour a stop file and cap their own
lifetime); a test signals only what it started itself and still holds
(an unreaped Popen and the group it leads).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dct.core import procs
from dct.workflows.release import oplock

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are a POSIX mechanism"),
]

ROOT = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024

# leader → child → grandchild. Each writes <name>.pid into DIR and appends a
# tick to DIR/ticks every 10 ms. argv: DIR FIFO MODE — FIFO ("" = none)
# receives "ready" once all three pid files exist; MODE is a comma list:
# "ignore" makes the child and the grandchild ignore TERM / HUP / INT, "cwd"
# makes every member also append to ./ticks of the directory it runs in.
# Every member holds DIR/presence (a FIFO the test reads) open for writing
# for as long as it lives, and leaves BY ITSELF once DIR/stop exists, once
# nobody reads DIR/presence any more, or after LIFETIME seconds — so the
# test ends a family without ever signalling a number from a .pid file.
FAMILY = r'''
import os, signal, sys, time
d, fifo, mode = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
targets = [os.path.join(d, "ticks")] + (["ticks"] if "cwd" in mode else [])
deadline = time.monotonic() + 600  # LIFETIME: a family outlives no test run

def running():
    return not os.path.exists(os.path.join(d, "stop")) and time.monotonic() < deadline

def register(name):
    try:  # never written to; the kernel closes it when this process ends
        os.open(os.path.join(d, "presence"), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:  # no reader: the test that started the family is gone
        os._exit(0)
    with open(os.path.join(d, name + ".pid.tmp"), "w") as fh:
        fh.write(str(os.getpid()))
    os.replace(os.path.join(d, name + ".pid.tmp"), os.path.join(d, name + ".pid"))

def loop(name):
    if "ignore" in mode:
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(s, signal.SIG_IGN)
    register(name)
    while running():
        for target in targets:
            try:
                with open(target, "a") as fh:
                    fh.write(name[0])
            except OSError:
                pass
        time.sleep(0.01)
    os._exit(0)

if os.fork() == 0:
    if os.fork() == 0:
        loop("grandchild")
    loop("child")
with open(os.path.join(d, "cwd"), "w") as fh:
    fh.write(os.getcwd())
register("leader")
while running() and not all(os.path.exists(os.path.join(d, n + ".pid"))
                            for n in ("child", "grandchild")):
    time.sleep(0.005)
if fifo:
    try:  # non-blocking: a reader that is gone must not hang the leader
        ready = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        pass
    else:
        os.write(ready, b"ready\n")
        os.close(ready)
while running():
    time.sleep(0.01)
'''

NAMES = ("leader", "child", "grandchild")


class Family:
    """The family's scratch directory: pid files, ticks, the ready FIFO.

    The .pid files serve observations only (`alive`, `pids`): they outlive
    the family, and a number in them may by then name anybody's process.
    Ending the family therefore never signals such a number — `stop` asks
    the members to leave and watches the presence FIFO they hold open.
    """

    def __init__(self, root: Path):
        self.dir = root / "family"
        self.dir.mkdir()
        self.script = root / "family.py"
        self.script.write_text(FAMILY)
        self.fifo = root / "ready.fifo"
        os.mkfifo(self.fifo)
        # Opened for reading FIRST (non-blocking), so the family's open for
        # writing never blocks and a dead helper cannot hang the test.
        self._ready = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.mkfifo(self.dir / "presence")
        self._presence = os.open(self.dir / "presence", os.O_RDONLY | os.O_NONBLOCK)

    def argv(self, mode: str = "plain", *, barrier: bool = True) -> list[str]:
        return [sys.executable, str(self.script), str(self.dir),
                str(self.fifo) if barrier else "", mode]

    def wait_ready(self, *watch: subprocess.Popen, timeout: float = 60) -> None:
        """Block until the family reported "ready" through the FIFO."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self._ready], [], [], 0.1)
            if readable:
                if os.read(self._ready, 16):
                    return
                time.sleep(0.01)  # no writer yet: a FIFO then reads as "end of file"
            for proc in watch:
                # A helper that died BEFORE the family was ready is a broken test
                # setup — fail loudly instead of waiting for the deadline.
                if proc.poll() is not None and not self.pids():
                    raise AssertionError(f"the helper exited ({proc.returncode}) before the "
                                         f"family started: {proc.stderr.read() if proc.stderr else ''}")
        raise AssertionError("the family never became ready")

    def pids(self) -> dict[str, int]:
        out = {}
        for name in NAMES:
            with contextlib.suppress(OSError, ValueError):
                out[name] = int((self.dir / f"{name}.pid").read_text())
        return out

    def alive(self) -> list[str]:
        """Names of the family members that still exist RIGHT NOW."""
        out = []
        for name, pid in self.pids().items():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            out.append(name)
        return out

    def ticks(self) -> int:
        try:
            return (self.dir / "ticks").stat().st_size
        except OSError:
            return 0

    def assert_started(self) -> None:
        assert sorted(self.pids()) == sorted(NAMES), "the family never started"

    def assert_silent(self) -> None:
        """Nobody is left AND nothing is written any more (an observation
        window of several tick periods, not a synchronisation)."""
        assert self.alive() == []
        size = self.ticks()
        time.sleep(0.15)
        assert self.ticks() == size

    def present(self) -> bool:
        """Whether any member still holds the presence FIFO open. Members
        never write to it, so an empty read means nobody holds it (the
        kernel closed it when each one ended, reaped or not)."""
        try:
            return os.read(self._presence, 1) != b""
        except BlockingIOError:  # a writer holds it, nothing to read
            return True

    def stop(self, timeout: float = 10) -> None:
        """End every member WITHOUT a signal: each one leaves by itself once
        DIR/stop exists. Returns when none of them is present any more; a
        member that never registered (not started yet) finds the stop file
        or the closed FIFO and leaves at once."""
        (self.dir / "stop").touch()
        deadline = time.monotonic() + timeout
        while self.present():
            assert time.monotonic() < deadline, "a family member did not stop"
            time.sleep(0.01)

    def close(self) -> None:
        try:
            self.stop()
        finally:
            os.close(self._ready)
            os.close(self._presence)


@pytest.fixture
def family(tmp_path):
    fam = Family(tmp_path)
    try:
        yield fam
    finally:
        fam.close()


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    """A family member that ignores SIGTERM costs the whole grace."""
    monkeypatch.setattr(procs, "TERM_GRACE_SECONDS", 0.3)


@pytest.fixture
def foreign(tmp_path):
    """A process the family has nothing to do with — started by the test, so
    its number stays reserved (unreaped) until this fixture waits for it."""
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def assert_untouched(proc: subprocess.Popen) -> None:
    """`proc` is still running half a second later (no signal ended it)."""
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=0.5)


def forge_pid_files(family: Family, pid: int) -> None:
    """Every .pid file now names `pid` — what a reused number looks like."""
    for name in NAMES:
        (family.dir / f"{name}.pid").write_text(str(pid))


def test_the_cleanup_never_signals_a_number_a_pid_file_holds(tmp_path, foreign):
    """The .pid files outlive the family: once it is gone the numbers in them
    can name anybody's process. The cleanup must not signal them."""
    family = Family(tmp_path)
    forge_pid_files(family, foreign.pid)
    family.close()
    assert_untouched(foreign)


def test_the_cleanup_stops_a_live_family_without_signalling_its_pid_files(tmp_path, foreign):
    """A family that runs (two members refuse SIGTERM) while its .pid files
    name a foreign process: the family ends by itself, the foreign process
    lives on."""
    family = Family(tmp_path)
    leader = subprocess.Popen(family.argv("ignore"), start_new_session=True,
                              stdin=subprocess.DEVNULL)
    try:
        family.wait_ready(leader)
        forge_pid_files(family, foreign.pid)
        family.close()
        assert_untouched(foreign)
        assert leader.wait(timeout=10) == 0  # it left by itself, not by a signal
        size = family.ticks()  # … and so did the child and the grandchild
        time.sleep(0.15)
        assert family.ticks() == size
    finally:
        if leader.returncode is None:
            # Never reaped, so its number still names the group we started.
            os.killpg(leader.pid, signal.SIGKILL)
            leader.wait()


def wait_until_gone(pgid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while procs.group_alive(pgid):
        assert time.monotonic() < deadline, f"group {pgid} never ended"
        time.sleep(0.01)


# ── K-108: the group ends before the runner returns ──────────────────────────


@pytest.mark.parametrize("mode", ["plain", "ignore"])
def test_a_timeout_stops_the_child_and_the_grandchild_before_it_is_reported(family, mode):
    """`ignore`: the child and the grandchild refuse SIGTERM — SIGKILL after
    the grace ends them; nothing is alive when the timeout is raised."""
    started = time.monotonic()
    with pytest.raises(procs.TimedOut) as exc:
        procs.run(family.argv(mode, barrier=False), timeout=1.5)
    family.assert_started()
    assert family.alive() == []
    assert exc.value.timeout == 1.5 and exc.value.result.returncode is None
    assert time.monotonic() - started < 10
    family.assert_silent()


def test_processes_left_behind_by_a_normal_exit_are_stopped(tmp_path):
    """A command that exits 0 but left a background process (holding the
    output pipes): the runner returns promptly, the leftover is gone."""
    pidfile = tmp_path / "bg.pid"
    started = time.monotonic()
    out = procs.run(["bash", "-c", f'sleep 300 & echo $! > "{pidfile}"; echo done'],
                    timeout=60)
    assert time.monotonic() - started < 20
    assert (out.returncode, out.stdout.text(), out.stopped) == (0, "done\n", True)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_a_command_that_cannot_start_is_reported(tmp_path):
    with pytest.raises(procs.StartFailed):
        procs.run([str(tmp_path / "no-such-binary")], timeout=5)


def test_a_consumer_that_stops_reading_a_stream_stops_the_group(family):
    """An error in the CALLER while it iterates a streamed read."""
    script = (f'echo first; exec "{sys.executable}" "{family.script}" '
              f'"{family.dir}" "" ignore')
    lines = procs.stream_lines(["bash", "-c", script], timeout=60)
    with pytest.raises(RuntimeError, match="caller failed"):
        for line in lines:
            assert line == b"first"
            while sorted(family.pids()) != sorted(NAMES):
                time.sleep(0.005)
            raise RuntimeError("caller failed")
    lines.close()
    assert family.alive() == []


def test_a_streamed_read_times_out_with_its_whole_group(family):
    lines = procs.stream_lines(family.argv("ignore", barrier=False), timeout=1.5)
    with pytest.raises(procs.TimedOut):
        list(lines)
    family.assert_started()
    assert family.alive() == []


def test_a_streamed_read_is_complete_only_after_exit_zero():
    ok = procs.stream_lines(["bash", "-c", "printf 'a\\nb\\nc'"], timeout=30)
    assert list(ok) == [b"a", b"b", b"c"]
    failing = procs.stream_lines(["bash", "-c", "echo a; echo boom >&2; exit 3"], timeout=30)
    with pytest.raises(procs.Failed, match="boom") as exc:
        list(failing)
    assert exc.value.returncode == 3


def test_a_streamed_line_over_the_limit_is_refused_not_cut():
    endless = ["bash", "-c", "while :; do printf 'xxxxxxxxxxxxxxxx'; done"]
    with pytest.raises(procs.OutputLimitExceeded):
        list(procs.stream_lines(endless, timeout=30, max_line_bytes=4096))


# ── K-108: the supervisor is interrupted ─────────────────────────────────────

# Stands for dct: it takes the operation lock (which supervises the
# operation), creates a resource, runs the family inside it and removes the
# resource on the way out — logging who was still alive at each point.
HELPER = r'''
import json, os, shutil, signal, sys
from dct.core import procs
from dct.workflows.release import oplock

cfg = json.loads(sys.argv[1])
procs.TERM_GRACE_SECONDS = 0.3

def alive():
    out = []
    for name in ("leader", "child", "grandchild"):
        try:
            pid = int(open(os.path.join(cfg["dir"], name + ".pid")).read())
            os.kill(pid, 0)
        except (OSError, ValueError):
            continue
        out.append(name)
    return out

def note(**event):
    with open(cfg["log"], "a") as fh:
        fh.write(json.dumps(event) + "\n")

with oplock.operation_lock(cfg["lock"], "helper") as held:
    if cfg.get("die_at_started"):
        held.started = lambda pgid: os.kill(os.getpid(), signal.SIGKILL)
    resource = os.path.join(cfg["dir"], "resource")
    os.mkdir(resource)
    try:
        procs.run(cfg["argv"], cwd=resource, timeout=600)
    finally:
        note(event="resource_removed", alive=alive())
        shutil.rmtree(resource)
'''


# The dispositions of a dct started from a terminal, where Ctrl-C, SIGTERM
# and SIGHUP reach it. A test that interrupts dct relies on them, but a
# process INHERITS an ignored signal: a shell without job control starts a
# background job (`pytest … &` in a script, a CI step, a parallel shard) with
# SIGINT and SIGQUIT ignored, and dct rightly leaves an ignored signal
# ignored (test_an_ignored_signal_stays_ignored) — the interrupted dct would
# then never stop. So the precondition is established, never inherited.
TERMINAL_DISPOSITIONS = {signal.SIGINT: signal.default_int_handler,
                         signal.SIGTERM: signal.SIG_DFL, signal.SIGHUP: signal.SIG_DFL}

_TERMINAL_PRELUDE = r'''
import signal as _signal
_signal.signal(_signal.SIGINT, _signal.default_int_handler)
_signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
_signal.signal(_signal.SIGHUP, _signal.SIG_DFL)
'''


@pytest.fixture
def terminal_dispositions():
    """TERMINAL_DISPOSITIONS in THIS process for the test, restored after."""
    saved = {signum: signal.getsignal(signum) for signum in TERMINAL_DISPOSITIONS}
    try:
        for signum, handler in TERMINAL_DISPOSITIONS.items():
            signal.signal(signum, handler)
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def start_interruptible(script: Path, source: str, config: dict) -> subprocess.Popen:
    """Run `source` (written to `script`) as a stand-in for dct in its own
    process, with the TERMINAL_DISPOSITIONS whatever this run inherited."""
    script.write_text(_TERMINAL_PRELUDE + source)
    return subprocess.Popen([sys.executable, str(script), json.dumps(config)], cwd=ROOT,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def start_helper(tmp_path: Path, family: Family, lock: Path, **cfg) -> subprocess.Popen:
    config = {"dir": str(family.dir), "log": str(tmp_path / "events.log"), "lock": str(lock),
              "argv": family.argv(cfg.pop("mode", "ignore")), **cfg}
    return start_interruptible(tmp_path / "helper.py", HELPER, config)


def events(tmp_path: Path) -> list[dict]:
    log = tmp_path / "events.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_an_interrupted_operation_reaps_the_group_before_resources_and_the_lock(
        tmp_path, family, signum):
    """Ctrl-C, SIGTERM and SIGHUP alike: the family (two members refuse
    SIGTERM) is gone BEFORE the resource is removed and BEFORE the lock is
    released (its record is emptied only by a release); the handlers are
    restored — the process then dies from the very signal it received, by
    its ORIGINAL disposition."""
    lock = tmp_path / "op.lock"
    helper = start_helper(tmp_path, family, lock)
    try:
        family.wait_ready(helper)
        helper.send_signal(signum)
        stderr = helper.communicate(timeout=60)[1]
    finally:
        helper.kill()
    assert family.alive() == [], stderr
    assert events(tmp_path) == [{"event": "resource_removed", "alive": []}], stderr
    assert not (family.dir / "resource").exists()
    assert oplock.read_holder(str(lock)) is None  # released, not dropped by a death
    assert helper.returncode == -signum, stderr
    family.assert_silent()
    with oplock.operation_lock(str(lock), "next"):  # free, and nothing left to detect
        pass


def test_a_killed_supervisor_leaves_a_record_that_refuses_the_next_operation(tmp_path, family):
    """SIGKILL of dct itself cannot be handled: the kernel drops its lock
    while the family runs on. The holder record names the group, and the
    next operation refuses for as long as the group lives."""
    lock = tmp_path / "op.lock"
    helper = start_helper(tmp_path, family, lock)
    try:
        family.wait_ready(helper)
        helper.kill()
        helper.wait(60)
    finally:
        helper.kill()
    pgid = family.pids()["leader"]
    assert oplock.read_holder(str(lock))["groups"] == [pgid]
    before = family.ticks()
    with pytest.raises(oplock.LeftoverProcesses, match="still running") as exc, \
            oplock.operation_lock(str(lock), "second"):
        pytest.fail("a second operation ran next to the leftover group")
    assert exc.value.groups == [pgid] and str(pgid) in str(exc.value)
    assert sorted(family.alive()) == sorted(NAMES)  # refused, not stopped by the contender
    while family.ticks() == before:  # … and it really is still writing
        time.sleep(0.01)
    # The refusal keeps the dead holder's record: every later attempt refuses too.
    with pytest.raises(oplock.LeftoverProcesses), oplock.operation_lock(str(lock), "third"):
        pytest.fail("the record was lost by the refused attempt")
    family.stop()
    wait_until_gone(pgid)
    with oplock.operation_lock(str(lock), "after") as held:
        assert oplock.read_holder(str(lock))["action"] == "after"
        assert held.path == str(lock)
        family.assert_silent()  # no write once the protection admitted the next operation
    assert oplock.read_holder(str(lock)) is None


def test_a_supervisor_killed_inside_the_spawn_window_is_refused_as_unverifiable(
        tmp_path, family):
    """Between the fork and the record of its group nothing names the group:
    the record says a process was being started, and the next operation
    refuses (it cannot prove nothing is running) until the owner clears it."""
    lock = tmp_path / "op.lock"
    helper = start_helper(tmp_path, family, lock, die_at_started=True)
    try:
        family.wait_ready()  # the helper is dead by then — by design
        assert helper.wait(60) == -signal.SIGKILL
    finally:
        helper.kill()
    holder = oplock.read_holder(str(lock))
    assert holder["spawning"] is True and holder["groups"] == []
    assert sorted(family.alive()) == sorted(NAMES)
    with pytest.raises(oplock.LeftoverProcesses, match="cannot be verified") as exc, \
            oplock.operation_lock(str(lock), "second"):
        pytest.fail("ran although a started process could not be accounted for")
    assert exc.value.groups == []
    family.stop()
    # The documented recovery: the owner checks, then empties the lock file.
    with open(lock, "w"):
        pass
    with oplock.operation_lock(str(lock), "after"):
        pass


def test_a_group_that_cannot_be_confirmed_dead_keeps_the_protection(
        tmp_path, family, monkeypatch):
    """The escalation ladder is cut to SIGTERM (standing for a group that
    survives every signal): the runner reports it, the lock's record keeps
    the group after the release, and the next operation refuses."""
    monkeypatch.setattr(procs, "STOP_SIGNALS", ((signal.SIGTERM, 0.3),))
    lock = tmp_path / "op.lock"
    with oplock.operation_lock(str(lock), "one"):
        with pytest.raises(procs.GroupSurvived) as exc:
            procs.run(family.argv("ignore", barrier=False), timeout=1.5)
        pgid = exc.value.pgid
        assert procs.survivors() == [pgid]
    assert sorted(family.alive()) == ["child", "grandchild"] and pgid == family.pids()["leader"]
    assert oplock.read_holder(str(lock))["groups"] == [pgid]
    with pytest.raises(oplock.LeftoverProcesses), oplock.operation_lock(str(lock), "two"):
        pytest.fail("ran next to a group that was never confirmed dead")
    family.stop()
    wait_until_gone(pgid)
    with oplock.operation_lock(str(lock), "three"):
        pass


def test_a_record_of_another_host_with_groups_cannot_be_verified(tmp_path):
    lock = tmp_path / "op.lock"
    lock.write_text(json.dumps({"pid": 1, "action": "apply", "host": "another-host.invalid",
                                "started_at": "2026-01-01T00:00:00+00:00", "groups": [4242]}))
    with pytest.raises(oplock.LeftoverProcesses, match="another-host.invalid"), \
            oplock.operation_lock(str(lock), "second"):
        pytest.fail("a group on another host cannot be checked from here")


@pytest.mark.parametrize("content", ["", '{"pid": 1, "action": "apply", "host": "h"}',
                                     '{"pid": 1, "groups": [], "spawning": false}'])
def test_a_record_without_running_groups_never_blocks(tmp_path, content):
    """An empty file, a record written before groups were recorded, and a
    record of a holder that died between two processes."""
    lock = tmp_path / "op.lock"
    lock.write_text(content)
    with oplock.operation_lock(str(lock), "next"):
        assert oplock.read_holder(str(lock))["pid"] == os.getpid()


def test_an_unreadable_record_is_refused_not_guessed(tmp_path):
    lock = tmp_path / "op.lock"
    lock.write_text('{"pid": 1, "groups": [77')
    with pytest.raises(oplock.LeftoverProcesses, match="cannot be verified"), \
            oplock.operation_lock(str(lock), "second"):
        pytest.fail("ran on a record nobody could read")


def test_the_record_lists_a_group_only_while_it_runs(tmp_path):
    lock = tmp_path / "op.lock"
    seen = {}
    with oplock.operation_lock(str(lock), "one"):
        lines = procs.stream_lines(
            ["bash", "-c", "echo $$; sleep 300"], timeout=60)
        seen["pgid"] = int(next(lines))
        seen["during"] = oplock.read_holder(str(lock))["groups"]
        lines.close()
        seen["after"] = oplock.read_holder(str(lock))
    assert seen["during"] == [seen["pgid"]]
    assert seen["after"]["groups"] == [] and seen["after"]["spawning"] is False
    assert not procs.group_alive(seen["pgid"])


# ── K-108: the temporary signal handlers ─────────────────────────────────────


def _handlers() -> dict:
    return {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}


def test_signal_handlers_are_temporary_and_restored(terminal_dispositions):
    before = _handlers()
    with procs.supervision():
        during = _handlers()
        with procs.supervision():  # nested: the outer scope owns the handlers
            assert _handlers() == during
        assert _handlers() == during
    assert all(during[s] is not before[s] for s in before)
    assert _handlers() == before


def test_sigterm_becomes_an_exception_and_is_delivered_again_afterwards(monkeypatch):
    """Inside the scope SIGTERM unwinds the operation as procs.Interrupted;
    once the scope is left the ORIGINAL disposition gets the signal (here a
    recording handler; by default the process dies from it)."""
    received = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
    try:
        with pytest.raises(procs.Interrupted) as exc, procs.supervision():
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(1000):  # the handler runs between two bytecodes
                time.sleep(0.001)
            pytest.fail("SIGTERM did not interrupt the supervised operation")
        assert exc.value.signum == signal.SIGTERM
        assert received == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_an_ignored_signal_stays_ignored():
    """`nohup dct …`: SIGHUP is ignored by the caller's choice — the scope
    does not turn it back into an interruption."""
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with procs.supervision():
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(0.05)
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_off_the_main_thread_no_handler_is_installed_and_the_runner_still_supervises(family):
    """Signals are delivered to the main thread only: a scope entered on
    another thread installs nothing; a timeout there still reaps the group."""
    before = _handlers()
    box = {}

    def work():
        try:
            with procs.supervision():
                box["during"] = _handlers()
                procs.run(family.argv("ignore", barrier=False), timeout=1.5)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            box["error"] = exc
            box["alive"] = family.alive()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(60)
    assert box["during"] == before and _handlers() == before
    assert isinstance(box["error"], procs.TimedOut) and box["alive"] == []


# ── K-113: bounded, concurrent capture ───────────────────────────────────────

MEASURE = r'''
import json, resource, sys
from dct.core import procs

def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024

cfg = json.loads(sys.argv[1])
before = peak()
out = {}
try:
    done = procs.run(cfg["argv"], timeout=cfg["timeout"],
                     stdout=procs.bounded(cfg["head"], cfg["tail"]),
                     stderr=procs.bounded(cfg["head"], cfg["tail"]))
except procs.TimedOut as exc:
    done, out["timed_out"] = exc.result, True
out.update(
    returncode=done.returncode,
    stdout=[len(done.stdout.head), len(done.stdout.tail), done.stdout.total,
            done.stdout.dropped],
    stderr=[len(done.stderr.head), len(done.stderr.tail), done.stderr.total,
            done.stderr.dropped],
    grown=peak() - before, pgid_alive=procs.group_alive(done.pgid))
print(json.dumps(out))
'''

# 64 MiB to stdout and 64 MiB to stderr, alternating in 1 MiB writes: a
# reader that drains one stream at a time deadlocks on the other pipe.
BOTH_STREAMS = ("import os\nblock = b'x' * (1 << 20)\n"
                "for _ in range(64):\n    os.write(1, block)\n    os.write(2, block)\n")
ENDLESS = "import os\nblock = b'x' * 65536\nwhile True:\n    os.write(1, block)\n"


def measure(tmp_path: Path, **cfg) -> dict:
    script = tmp_path / "measure.py"
    script.write_text(MEASURE)
    out = subprocess.run([sys.executable, str(script), json.dumps(cfg)], cwd=ROOT,
                         capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_large_output_on_both_streams_costs_bounded_memory(tmp_path):
    """128 MiB flow through; the supervising process grows by a fraction of
    it and keeps exactly the head and the tail of each stream."""
    out = measure(tmp_path, argv=[sys.executable, "-c", BOTH_STREAMS], timeout=240,
                  head=4096, tail=1024)
    assert out["returncode"] == 0
    assert out["stdout"] == [4096, 1024, 64 * MIB, 64 * MIB - 5120]
    assert out["stderr"] == [4096, 1024, 64 * MIB, 64 * MIB - 5120]
    assert out["grown"] < 24 * MIB, out


def test_an_endless_output_is_drained_bounded_until_the_timeout_stops_it(tmp_path):
    """Continuous output never fills a pipe or a buffer: it is counted and
    dropped until the timeout stops the group."""
    out = measure(tmp_path, argv=[sys.executable, "-c", ENDLESS], timeout=2, head=4096,
                  tail=0)
    assert out["timed_out"] is True and out["returncode"] is None
    head, tail, total, dropped = out["stdout"]
    assert (head, tail) == (4096, 0) and total > 8 * MIB and dropped == total - 4096
    assert out["pgid_alive"] is False
    assert out["grown"] < 24 * MIB, out


def _printing(data: bytes) -> list[str]:
    return [sys.executable, "-c", f"import os; os.write(1, bytes.fromhex('{data.hex()}'))"]


def test_a_multibyte_character_split_at_the_cap_is_not_decoded_as_garbage():
    two = procs.run(_printing("é".encode() * 100), timeout=30, stdout=procs.bounded(51)).stdout
    assert two.head == "é".encode() * 25 and (two.total, two.dropped) == (200, 150)
    assert "�" not in two.text() and two.text().startswith("é" * 25)
    # Four-byte characters, a head AND a tail that both cut inside one.
    four = procs.run(_printing("😀".encode() * 100), timeout=30,
                     stdout=procs.bounded(26, 25)).stdout
    assert four.head == "😀".encode() * 6 and four.tail == "😀".encode() * 6
    assert (four.total, four.dropped) == (400, 352)
    text = four.text()
    assert "�" not in text and text.count("😀") == 12 and "352 bytes" in text


def test_bytes_that_are_really_invalid_are_still_shown_as_such():
    """Only the CUT is repaired — an invalid byte the process printed, or a
    sequence it left unfinished at its very end, stays visible."""
    inside = procs.run(_printing(b"ab\xffcd"), timeout=30).stdout
    assert inside.text() == "ab�cd" and inside.dropped == 0
    at_end = procs.run(_printing(b"ab\xc3"), timeout=30, stdout=procs.bounded(3)).stdout
    assert at_end.text() == "ab�" and at_end.dropped == 0


def test_output_under_the_cap_is_kept_whole_across_head_and_tail():
    out = procs.run(_printing(b"0123456789"), timeout=30, stdout=procs.bounded(6, 6)).stdout
    assert (out.data, out.dropped, out.text()) == (b"0123456789", 0, "0123456789")


def test_merged_streams_share_one_buffer():
    out = procs.run(["bash", "-c", "echo out; echo err >&2"], timeout=30, merge_stderr=True)
    assert sorted(out.stdout.text().split()) == ["err", "out"] and out.stderr.total == 0


def test_proof_output_over_its_limit_is_refused_and_the_writer_stopped(family):
    """`complete`: the whole output or an error — never a cut result. The
    process that was still writing is stopped with its group."""
    script = (f'"{sys.executable}" "{family.script}" "{family.dir}" "" ignore & '
              f'while [ ! -f "{family.dir}/grandchild.pid" ]; do sleep 0.01; done; '
              "while :; do printf 'xxxxxxxxxxxxxxxx'; done")
    with pytest.raises(procs.OutputLimitExceeded, match="4096"):
        procs.run(["bash", "-c", script], timeout=60, stdout=procs.complete(4096))
    family.assert_started()
    assert family.alive() == []
    whole = procs.run(_printing(b"x" * 4096), timeout=30, stdout=procs.complete(4096))
    assert whole.stdout.data == b"x" * 4096 and whole.stdout.dropped == 0
