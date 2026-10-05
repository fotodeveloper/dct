"""The ONE supervised process runner (K-108, K-113).

Every external process the release workflow starts — the version-sync
script of `prepare`, every strict git command (so also the repository's
hooks and the push with its transport) — runs through `run` or
`stream_lines`. The git layer (dct.core.gitinfo: _strict_git, git_read,
git_read_lines, git_write) and the effects layer
(dct.workflows.release.effects) add their own policy on top; nothing there
starts a process itself.

SUPERVISION (K-108). A process is started in its OWN SESSION, so it leads
its own process group: its children and grandchildren are in that group,
and the terminal's Ctrl-C reaches only dct. The runner owns the group's end:

  - after a NORMAL exit, anything the command left running in its group is
    stopped before the call returns;
  - on a TIMEOUT, an INTERRUPTION (KeyboardInterrupt, SystemExit, a signal
    turned into an exception — see below) or ANY other exception in the
    caller, the whole group is stopped FIRST: SIGTERM, a grace
    (TERM_GRACE_SECONDS), then SIGKILL — and the runner WAITS until the
    group is gone (the leader reaped, `killpg(pgid, 0)` → no such process).
    Only then does the exception leave the runner, so whatever the caller
    removes or releases while unwinding (a temporary worktree, the
    operation lock) is removed / released after the processes ended;
  - a group that is still there after the last signal (a member no signal
    of this user can reach) is REPORTED: GroupSurvived, the group stays in
    `survivors()` and in the operation lock's record (below), and callers
    keep the resources it may still write to.

What supervision cannot do: a process that LEAVES the group (`setsid`, a
daemon — gpg-agent, an ssh ControlMaster, git's background gc) is not a
member any more; it is neither stopped nor waited for nor detected.
SIGKILL of dct itself cannot be handled by anything in this process — that
case is covered by the record the group left in the lock file. Without
process groups (Windows) only the started process itself is killed and
nothing is recorded.

Because the process has no controlling terminal and its stdin is closed, it
can never prompt: a hook, a credential helper or an ssh that needs a
passphrase fails (use an agent / a helper / askpass) instead of waiting on
a terminal nobody watches.

SIGNALS → EXCEPTIONS. `supervision()` is the scope of one supervised
operation (dct.workflows.release.oplock enters it around the operation
lock). On the MAIN thread it installs temporary handlers for SIGINT,
SIGTERM and SIGHUP (one that the process ignores — `nohup` — is left
alone): SIGINT raises KeyboardInterrupt, SIGTERM / SIGHUP raise
Interrupted, so the operation unwinds like any exception — through the
runner (which reaps the group) and through every `finally`. Leaving the
scope restores the previous handlers and then DELIVERS a received SIGTERM /
SIGHUP again, to the original disposition: by default dct still dies from
the signal, after the cleanup. While the runner starts or stops a process a
signal is DEFERRED (recorded, raised as soon as the group is reaped), so no
interruption can strike between the fork and the bookkeeping or abort the
reaping; after one interruption was raised, a further signal is deferred
until the runner has next finished stopping a group (or the scope ends).
Off the main thread Python delivers no signals: the scope installs nothing
there (whoever owns the main thread keeps its handlers) and still records
the groups; timeouts and exceptions are supervised on every thread.

GROUP RECORD. The scope may carry a `groups` ledger (the held operation
lock): the runner calls `spawning()` before a process is started,
`started(pgid)` once its group is known (`spawn_failed()` when it never
started) and `ended(pgid)` after the group is CONFIRMED gone — never for a
survivor. That record is what lets the next operation refuse while a group
outlives a dct that was killed.

CAPTURE (K-113). stdout and stderr are drained CONCURRENTLY and
continuously (a full pipe never blocks the process, however much it
prints) into memory bounded by the capture policy of each stream — never
into a file:

  - `bounded(head, tail)` — DIAGNOSTIC text: the first `head` and the last
    `tail` bytes are kept, everything between is counted and dropped
    (`Captured.dropped`). A UTF-8 sequence split by the cut is dropped
    with the cut (never decoded as U+FFFD); bytes the process itself
    printed wrong stay visible as U+FFFD;
  - `complete(limit)` — output that is DATA (git protocol output used as
    proof): everything, or OutputLimitExceeded once it exceeds `limit`
    (the group is stopped) — never a cut result presented as complete;
  - `stream_lines` — complete output of any size, one line at a time; a
    single line over `max_line_bytes` is refused the same way.

Memory per process is at most the two policies' sizes (a tail may
transiently hold twice its size) plus one read chunk (64 KiB).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

# The default cap of DIAGNOSTIC output, per stream.
DIAGNOSTIC_MAX_BYTES = 65536
# The grace a stopped group gets between SIGTERM and SIGKILL, and how long
# the runner waits for a SIGKILLed group to disappear before it reports a
# survivor. STOP_SIGNALS = () means exactly that ladder; a non-empty tuple
# of (signal, seconds to wait) replaces it.
TERM_GRACE_SECONDS = 3.0
KILL_CONFIRM_SECONDS = 5.0
STOP_SIGNALS: tuple[tuple[int, float], ...] = ()
# One line of a streamed read.
LINE_MAX_BYTES = 1024 * 1024

_CHUNK = 65536
_POLL_SECONDS = 0.05
_HAS_GROUPS = hasattr(os, "killpg")


class ProcessError(RuntimeError):
    """A supervised process could not be run to a usable result."""


class StartFailed(ProcessError):
    """The command could not be started (missing binary, bad cwd, …)."""


class TimedOut(ProcessError):
    """The process exceeded its timeout; its group was stopped first.
    `result` holds what run() captured (returncode None; None for a
    streamed read)."""

    def __init__(self, message: str, *, timeout: float, result: Result | None = None):
        super().__init__(message)
        self.timeout = timeout
        self.result = result


class OutputLimitExceeded(ProcessError):
    """Output that must be complete exceeded `limit`; the group was stopped
    and NO partial result is returned."""

    def __init__(self, message: str, *, limit: int):
        super().__init__(message)
        self.limit = limit


class Failed(ProcessError):
    """A streamed read ended with a non-zero exit code (`returncode`;
    `stderr` is its bounded diagnostic text)."""

    def __init__(self, message: str, *, returncode: int | None, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class GroupSurvived(ProcessError):
    """The process group `pgid` could not be confirmed dead after the last
    stop signal — it may still be running and writing."""

    def __init__(self, message: str, *, pgid: int):
        super().__init__(message)
        self.pgid = pgid


class Interrupted(BaseException):
    """SIGTERM / SIGHUP (`signum`) received during a supervised operation.
    A BaseException, like KeyboardInterrupt: no `except Exception` on the
    way swallows it."""

    def __init__(self, signum: int):
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum


# ── capture ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capture:
    """The capture policy of one stream — build with bounded() / complete()."""

    head: int
    tail: int = 0
    must_be_complete: bool = False


def bounded(head: int = DIAGNOSTIC_MAX_BYTES, tail: int = 0) -> Capture:
    """Keep the first `head` and the last `tail` bytes; count the rest."""
    return Capture(max(0, head), max(0, tail))


def complete(limit: int) -> Capture:
    """Keep everything; more than `limit` bytes → OutputLimitExceeded."""
    return Capture(max(0, limit), 0, True)


def _unfinished_suffix(data: bytearray) -> int:
    """Length of an UNFINISHED UTF-8 sequence at the end of `data` (0-3)."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:  # a continuation byte — its lead is further back
            continue
        need = 4 if byte >= 0xF0 else 3 if byte >= 0xE0 else 2 if byte >= 0xC0 else 1
        return back if back < need else 0
    return 0


def _continuation_prefix(data: bytearray) -> int:
    """Number of leading UTF-8 continuation bytes of `data` (0-3)."""
    count = 0
    while count < min(3, len(data)) and data[count] & 0xC0 == 0x80:
        count += 1
    return count


@dataclass(frozen=True)
class Captured:
    """What was kept of one stream: `head` + `tail` (cut on character
    boundaries), `total` bytes the process wrote, `dropped` of them not
    kept. `dropped == 0` ⇔ `data` is the complete output."""

    head: bytes = b""
    tail: bytes = b""
    total: int = 0

    @property
    def dropped(self) -> int:
        return self.total - len(self.head) - len(self.tail)

    @property
    def data(self) -> bytes:
        return self.head + self.tail

    def text(self, errors: str = "replace") -> str:
        """The kept bytes as UTF-8 text; where bytes were dropped, a line
        `[… N bytes of output dropped …]` stands for them."""
        if not self.dropped:
            return self.data.decode("utf-8", errors)
        head, tail = self.head.decode("utf-8", errors), self.tail.decode("utf-8", errors)
        gap = f"[… {self.dropped} bytes of output dropped …]"
        return (head + ("" if not head or head.endswith("\n") else "\n") + gap
                + ("\n" + tail if tail else ""))


class _Sink:
    """Bounded accumulation of one stream (see Capture). Fed by the pump
    thread, read after that thread finished."""

    def __init__(self, spec: Capture):
        self.spec = spec
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.overflow = False  # a must-be-complete stream exceeded its limit
        self.eof = False       # the pipe was read to its end

    def feed(self, chunk: bytes) -> bool:
        """Account `chunk`; False once a must-be-complete stream overflowed."""
        self.total += len(chunk)
        room = self.spec.head - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return True
        if self.spec.must_be_complete:
            self.overflow = True
            return False
        keep = self.spec.tail
        if keep:
            self.tail += chunk[-2 * keep:]
            if len(self.tail) > 2 * keep:
                del self.tail[:-keep]
        return True

    def captured(self) -> Captured:
        head = self.head
        tail = self.tail[-self.spec.tail:] if self.spec.tail else bytearray()
        if self.total > len(head) + len(tail):  # bytes were cut out after the head
            head = head[:len(head) - _unfinished_suffix(head)]
            tail = tail[_continuation_prefix(tail):]
        return Captured(bytes(head), bytes(tail), self.total)


class _Pump:
    """Drains pipes into sinks on ONE background thread (a selector over all
    of them — both streams at once, so a full pipe never blocks the
    process). `done` is set once every pipe reached EOF or a
    must-be-complete sink overflowed. Without selectable pipes (Windows)
    one blocking thread per pipe does the same."""

    def __init__(self, pairs: Sequence[tuple[object, _Sink]]):
        self.pairs = [(stream, sink) for stream, sink in pairs if stream is not None]
        self.done = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._open = len(self.pairs)
        self._lock = threading.Lock()
        if not self.pairs:
            self.done.set()

    @property
    def overflow(self) -> bool:
        return any(sink.overflow for _, sink in self.pairs)

    def start(self) -> None:
        if not self.pairs:
            return
        if _HAS_GROUPS:
            targets = [(self._select_loop, ())]
        else:  # pragma: no cover - exercised on Windows only
            targets = [(self._blocking_loop, pair) for pair in self.pairs]
        for target, args in targets:
            thread = threading.Thread(target=target, args=args, daemon=True,
                                      name="dct-procs-pump")
            thread.start()
            self._threads.append(thread)

    def _select_loop(self) -> None:
        import selectors

        try:
            with selectors.DefaultSelector() as selector:
                for stream, sink in self.pairs:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, sink)
                while selector.get_map() and not self._stop.is_set():
                    for key, _ in selector.select(4 * _POLL_SECONDS):
                        try:
                            chunk = os.read(key.fd, _CHUNK)
                        except BlockingIOError:
                            continue
                        except OSError:
                            chunk = b""
                        if not chunk:
                            key.data.eof = True
                            selector.unregister(key.fileobj)
                        elif not key.data.feed(chunk):
                            return
        finally:
            self.done.set()

    def _blocking_loop(self, stream, sink: _Sink) -> None:  # pragma: no cover - Windows
        try:
            while True:
                chunk = os.read(stream.fileno(), _CHUNK)
                if not chunk:
                    sink.eof = True
                    return
                if not sink.feed(chunk):
                    return
        except OSError:
            return
        finally:
            with self._lock:
                self._open -= 1
                if self._open <= 0 or sink.overflow:
                    self.done.set()

    def finish(self, timeout: float) -> None:
        """Wait up to `timeout` for EOF (with the group gone it comes at
        once, unless a process OUTSIDE the group still holds a pipe), then
        stop the thread — the pipes may be closed afterwards."""
        self.done.wait(timeout)
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0 if _HAS_GROUPS else 0)


# ── the supervised operation: signals and the group record ───────────────────


@dataclass
class Scope:
    """One supervised operation: `groups` — the ledger its process groups
    are recorded in (None = not recorded); `survivors` — groups that could
    not be confirmed dead."""

    groups: object | None = None
    survivors: list[int] = field(default_factory=list)


@dataclass
class _Signals:
    """Process-wide state of the temporary handlers (main thread only)."""

    thread: int
    previous: dict[int, object]
    armed: bool = True  # the handler may raise now
    pending: list[int] = field(default_factory=list)   # deferred, not raised yet
    received: list[int] = field(default_factory=list)  # SIGTERM / SIGHUP raised


_SCOPE: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "dct_procs_scope", default=None)
_signals: _Signals | None = None


def _interruption(state: _Signals, signum: int) -> BaseException:
    if signum == signal.SIGINT:
        return KeyboardInterrupt()
    state.received.append(signum)
    return Interrupted(signum)


def _on_signal(signum: int, frame) -> None:
    state = _signals
    if state is None:
        return
    if state.armed:
        # Disarmed in the same step as the raise: a second signal cannot
        # strike before the runner's cleanup has shielded itself.
        state.armed = False
        raise _interruption(state, signum)
    state.pending.append(signum)


def _shield() -> bool:
    """Defer signals from here on; True when this thread runs under the
    temporary handlers (what the calls below need to know)."""
    state = _signals
    if state is None or state.thread != threading.get_ident():
        return False
    state.armed = False
    return True


def _interruptible(active: bool) -> None:
    """Open the runner's WAIT to signals; a deferred one is raised now."""
    state = _signals
    if not active or state is None:
        return
    if state.pending:
        raise _interruption(state, state.pending.pop(0))
    state.armed = True


def _deferred(active: bool) -> None:
    """Close the wait again (idempotent)."""
    state = _signals
    if active and state is not None:
        state.armed = False


def _release(active: bool) -> None:
    """End a _shield(): signals interrupt again; a deferred one is raised."""
    state = _signals
    if not active or state is None:
        return
    state.armed = True
    if state.pending:
        raise _interruption(state, state.pending.pop(0))


def _supervised_signals() -> list[int]:
    return [s for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None))
            if s is not None]


@contextlib.contextmanager
def supervision(groups: object | None = None) -> Iterator[Scope]:
    """The scope of ONE supervised operation (module docstring): temporary
    SIGINT / SIGTERM / SIGHUP handlers on the main thread, restored on the
    way out — where a received SIGTERM / SIGHUP is then delivered again to
    the original disposition. `groups` (also settable later as
    `scope.groups`) is the ledger every process group of the operation is
    recorded in: an object with spawning(), started(pgid), spawn_failed(),
    ended(pgid). A nested scope shares the outer one's handlers; off the
    main thread no handler is installed."""
    global _signals
    scope = Scope(groups)
    token = _SCOPE.set(scope)
    owner = None
    if _signals is None and threading.current_thread() is threading.main_thread():
        previous = {}
        for signum in _supervised_signals():
            current = signal.getsignal(signum)
            if current is signal.SIG_IGN or current is None:
                continue  # ignored by the caller's choice / not Python's to replace
            previous[signum] = current
        owner = _signals = _Signals(threading.get_ident(), previous)
        for signum in previous:
            signal.signal(signum, _on_signal)
    failed = True
    try:
        yield scope
        failed = False
    finally:
        _SCOPE.reset(token)
        if owner is not None:
            owner.armed = False
            for signum, handler in owner.previous.items():
                signal.signal(signum, handler)
            _signals = None
            again = [s for s in owner.received + owner.pending if s != signal.SIGINT]
            for signum in dict.fromkeys(again):
                # The original disposition gets the signal: by default the
                # process ends here, from the signal it was sent.
                os.kill(os.getpid(), signum)
            if signal.SIGINT in owner.pending and not failed:
                raise KeyboardInterrupt


def survivors() -> list[int]:
    """Process groups of the current operation that could NOT be confirmed
    dead — a caller must not remove what they may still write to."""
    scope = _SCOPE.get()
    return list(scope.survivors) if scope else []


# ── stopping a group ─────────────────────────────────────────────────────────


def group_alive(pgid: int) -> bool:
    """Whether any process of the group `pgid` still exists (a member this
    user may not signal counts as alive)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ladder(term_grace: float | None) -> tuple[tuple[int, float], ...]:
    if STOP_SIGNALS:
        return STOP_SIGNALS
    grace = TERM_GRACE_SECONDS if term_grace is None else term_grace
    return ((signal.SIGTERM, grace), (signal.SIGKILL, KILL_CONFIRM_SECONDS))


def _stop_group(proc: subprocess.Popen, term_grace: float | None) -> tuple[bool, bool]:
    """Stop the whole process group of `proc` and reap `proc` →
    (something had to be stopped, the group is CONFIRMED gone)."""
    if not _HAS_GROUPS:  # pragma: no cover - exercised on Windows only
        stopped = proc.poll() is None
        if stopped:
            proc.kill()
        proc.wait()
        return stopped, True
    pgid = proc.pid

    def gone() -> bool:
        # poll() reaps the leader; the group id stays taken for as long as
        # any other member (a not yet reaped one included) exists.
        return proc.poll() is not None and not group_alive(pgid)

    if gone():
        return False, True
    for signum, wait in _ladder(term_grace):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signum)
        deadline, pause = time.monotonic() + wait, 0.001
        while not gone() and time.monotonic() < deadline:
            time.sleep(pause)
            pause = min(pause * 2, 0.02)
        if gone():
            return True, True
    return True, False


class _Group:
    """One started process and the bookkeeping of its group."""

    def __init__(self, argv: Sequence[str], *, cwd, env, stdout, stderr):
        self.scope = _SCOPE.get()
        self.ledger = self.scope.groups if self.scope and _HAS_GROUPS else None
        self.stopped = False
        self.confirmed: bool | None = None
        self._lock = threading.Lock()
        if self.ledger is not None:
            self.ledger.spawning()
        try:
            self.proc = subprocess.Popen(
                list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout,
                stderr=stderr, start_new_session=_HAS_GROUPS)
        except BaseException as exc:
            # Popen leaves no process behind when it raises.
            if self.ledger is not None:
                self.ledger.spawn_failed()
            if isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
                raise StartFailed(f"could not start {argv[0]}: {exc}") from exc
            raise
        self.pgid = self.proc.pid if _HAS_GROUPS else None
        if self.ledger is not None:
            try:
                self.ledger.started(self.pgid)
            except BaseException:
                # The group could not be recorded: it must not run unrecorded.
                self.ledger = None
                self.stop()
                self.close()
                raise

    def stop(self, term_grace: float | None = None) -> None:
        """Stop + reap the group (idempotent, any thread). A confirmed end
        leaves the ledger; an unconfirmed one stays there and is added to
        the scope's survivors."""
        with self._lock:
            if self.confirmed is not None:
                return
            self.stopped, self.confirmed = _stop_group(self.proc, term_grace)
            if self.confirmed:
                if self.ledger is not None:
                    self.ledger.ended(self.pgid)
            elif self.scope is not None:
                self.scope.survivors.append(self.pgid)

    def close(self) -> None:
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def survived(self) -> GroupSurvived:
        return GroupSurvived(
            f"the process group {self.pgid} is still running after it was told to stop — it "
            f"may keep writing; stop it (`kill -KILL -- -{self.pgid}`) before anything else "
            f"is done here", pgid=self.pgid)


def _timeout_error(argv: Sequence[str], timeout: float | None, result=None) -> TimedOut:
    return TimedOut(f"{argv[0]} timed out after {timeout}s and was stopped with its whole "
                    f"process group", timeout=timeout, result=result)


@dataclass(frozen=True)
class Result:
    """A supervised process that ran to its end: its exit code (None when a
    timeout stopped it), both captures, whether anything it left running
    had to be stopped, the wall-clock duration and its process group."""

    returncode: int | None
    stdout: Captured
    stderr: Captured
    stopped: bool
    duration: float
    pgid: int | None


def run(
    argv: Sequence[str], *, timeout: float | None, cwd: str | None = None,
    env: dict[str, str] | None = None, stdout: Capture | None = None,
    stderr: Capture | None = None, merge_stderr: bool = False,
    term_grace: float | None = None,
) -> Result:
    """Run `argv` (no shell, stdin closed, its own session / process group)
    to its end and return its Result — the ONE way the release workflow
    runs a process to completion.

    `timeout` — wall-clock seconds (None = none); `stdout` / `stderr` — the
    capture policy of each stream (default bounded(DIAGNOSTIC_MAX_BYTES));
    `merge_stderr` sends stderr into the stdout pipe (one stream, one
    policy); `term_grace` overrides TERM_GRACE_SECONDS.

    Raises StartFailed (not started), TimedOut (with the partial Result),
    OutputLimitExceeded (a complete() stream over its limit), GroupSurvived
    (the group could not be confirmed dead), ProcessError (a complete()
    stream that could not be read to its end: a process outside the group
    holds the pipe). In EVERY case — these, a normal return, an
    interruption, any other exception — the process group has been stopped
    and reaped before control leaves this function (or is reported as a
    survivor)."""
    sinks = (_Sink(stdout or bounded()), _Sink(stderr or bounded()))
    started = time.monotonic()
    deadline = None if timeout is None else started + timeout
    active = _shield()
    try:
        group = _Group(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE)
        proc = group.proc
        pump = _Pump([(proc.stdout, sinks[0]), (proc.stderr, sinks[1])])
        timed_out = False
        try:
            pump.start()
            _interruptible(active)
            while proc.poll() is None and not pump.overflow:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    timed_out = True
                    break
                step = _POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)
                if pump.done.wait(step) and not pump.overflow:
                    # Both pipes are closed: the exit follows at once (or the
                    # process let go of them and runs on, up to the timeout).
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=step)
        finally:
            _deferred(active)
            code = proc.returncode
            group.stop(term_grace)
            pump.finish(2.0 if group.confirmed else 0)
            group.close()
        result = Result(
            returncode=None if timed_out or pump.overflow else code,
            stdout=sinks[0].captured(), stderr=sinks[1].captured(), stopped=group.stopped,
            duration=time.monotonic() - started, pgid=group.pgid)
        if not group.confirmed:
            raise group.survived()
        for sink in sinks:
            if sink.overflow:
                raise OutputLimitExceeded(
                    f"{argv[0]} printed more than the limit of {sink.spec.head} bytes of "
                    f"output that must be read whole — stopped, nothing of it is used",
                    limit=sink.spec.head)
        if timed_out:
            raise _timeout_error(argv, timeout, result)
        streams = (proc.stdout, proc.stderr)
        if any(sink.spec.must_be_complete and stream is not None and not sink.eof
               for sink, stream in zip(sinks, streams, strict=True)):
            raise ProcessError(
                f"{argv[0]} exited, but a process outside its group still holds its output "
                f"— output that must be read whole cannot be read to its end")
        return result
    finally:
        _release(active)


def stream_lines(
    argv: Sequence[str], *, timeout: float | None, cwd: str | None = None,
    env: dict[str, str] | None = None, max_line_bytes: int | None = None,
    stderr: Capture | None = None, term_grace: float | None = None,
) -> Iterator[bytes]:
    """The stdout LINES of `argv` (bytes, without their newline), streamed —
    for output that must be complete but may be large. Same process setup
    and supervision as run(); stderr is captured by `stderr` (default
    bounded).

    Never a silently partial result: the generator finishes normally ONLY
    after the process exited 0. Failed (non-zero exit, with stderr),
    TimedOut (`timeout` seconds since the start), OutputLimitExceeded (one
    line longer than `max_line_bytes`, default LINE_MAX_BYTES) and
    GroupSurvived are raised AFTER the lines already yielded — a caller
    must not use them when the iteration raised. A consumer that stops
    early (close(), an exception while iterating) stops the group; nothing
    is left running."""
    limit = LINE_MAX_BYTES if max_line_bytes is None else max_line_bytes
    err = _Sink(stderr or bounded())
    active = _shield()
    try:
        group = _Group(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc = group.proc
        pump = _Pump([(proc.stderr, err)])
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            group.stop(term_grace)

        timer = threading.Timer(timeout, expire) if timeout is not None else None
        too_long = False
        try:
            pump.start()
            if timer is not None:
                timer.daemon = True
                timer.start()
            fd, rest = proc.stdout.fileno(), b""
            while not too_long:
                _interruptible(active)
                chunk = os.read(fd, _CHUNK)
                _deferred(active)
                if not chunk:
                    break
                *lines, rest = (rest + chunk).split(b"\n")
                too_long = len(rest) > limit or any(len(line) > limit for line in lines)
                if too_long:
                    break
                for line in lines:
                    # The consumer's code runs unshielded, like any caller's.
                    _release(active)
                    try:
                        yield line
                    finally:
                        active = _shield()
            if rest and not too_long:
                _release(active)
                try:
                    yield rest
                finally:
                    active = _shield()
            if not too_long:
                # EOF: the exit follows (bounded by the timer); whatever the
                # process left running is stopped below.
                _interruptible(active)
                proc.wait()
        finally:
            _deferred(active)
            if timer is not None:
                timer.cancel()
            code = proc.returncode
            group.stop(term_grace)
            pump.finish(2.0 if group.confirmed else 0)
            group.close()
        if not group.confirmed:
            raise group.survived()
        if expired.is_set():
            raise _timeout_error(argv, timeout)
        if too_long:
            raise OutputLimitExceeded(
                f"{argv[0]} printed a line longer than the limit of {limit} bytes — stopped, "
                f"nothing of its output is used", limit=limit)
        if code != 0:
            detail = err.captured().text().strip()
            raise Failed(detail or f"exit code {code}", returncode=code, stderr=detail)
    finally:
        _release(active)
