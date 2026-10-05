"""The PROJECT OPERATION LOCK of the release effects layer (K-96 D7).

Every MUTATING release path — `dct release prepare / apply / resume /
attest / abandon / reconstruct --confirm` and the `dct clog release` alias
(they all go through dct.workflows.release.effects) — holds this lock from
BEFORE its pre-checks / stamp until it returns. A second mutating command on
the same repository refuses immediately (OperationLocked, with the holder's
pid / action / start time) instead of interleaving its effects; nothing
waits.

Key (lock_path): one lock per REPOSITORY, not per checkout or project —
`<git common dir>/dct-release.lock` for a project in git (shared by the
main checkout and every linked worktree, and by every dct project whose
path lies in that repository: they share refs, tags and the object database),
`<data dir>/release-locks/<sha256 of the realpath>.lock` for a project
CONFIRMED outside git (dct.paths.release_lock_dir).

Mechanism: an ADVISORY file lock (`fcntl.flock`, LOCK_EX | LOCK_NB; on
Windows `msvcrt.locking` of the first byte) on an open file description, so
two threads of one process exclude each other as two processes do, and the
kernel releases it when the holder dies — the file is never deleted. It is
NOT git's index.lock: editors and other git processes are never blocked by
it; it coordinates dct's own release operations only. No SQL transaction is
held around the effects (the ledger writes stay short, K-96 D7).

OWNERSHIP MODEL (K-108) — the protection lasts until the CONFIRMED end of
every process the operation started, not merely until dct lets go:

  - `operation_lock` is also the scope of the SUPERVISED operation
    (dct.core.procs.supervision): SIGINT / SIGTERM / SIGHUP unwind it as
    exceptions, every process runs through the supervised runner, and the
    runner stops and REAPS a process group before the exception that ends
    the operation can reach this module — so the lock is released only
    after the processes are gone. The signal handlers are restored after
    the release.
  - The lock FILE carries the holder's record {pid, action, host,
    started_at, spawning, groups}: `groups` are the process groups the
    runner started and has not yet confirmed dead, `spawning` is true
    between "about to start a process" and "its group is recorded". Each
    update is ONE write of a fixed-size record (no truncate-then-write
    window).
  - A clean release empties the file. A holder that DIED (SIGKILL, a crash
    — nothing in the process can handle that) leaves its record behind,
    and so does a release with a group that could not be confirmed dead.
    The kernel has dropped the flock by then; the record is what is left
    of the protection.
  - The next operation takes the flock, reads that record BEFORE writing
    its own, and REFUSES (LeftoverProcesses, a kind of OperationLocked;
    the record is left as it is) while a recorded group still has a live
    process — or when it cannot tell: `spawning` was true, the record is
    of another host, or it is unreadable. A record whose groups are all
    gone is simply replaced.

Deliberately NOT part of the model: the lock's file descriptor is never
inherited by the processes (Python opens it close-on-exec). An inherited
descriptor proves nothing — the holder's own unlock releases the shared
lock whatever the children hold, and a descendant may close it — and it
would hand the lock to every daemon a hook or a transport leaves behind
(an ssh ControlMaster, gpg-agent). Limits: a process that left its group
(`setsid`) is not recorded; a group id reused by an unrelated process after
a hard death refuses until that process ends or the owner empties the lock
file (the refusal says how); without process groups (Windows) nothing is
recorded.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

from dct import paths as dct_paths
from dct.core import procs

LOCK_NAME = "dct-release.lock"
_HOLDER_MAX_BYTES = 4096


class OperationLocked(RuntimeError):
    """Another dct release operation holds the lock of this repository /
    project directory. `holder` is what it recorded ({pid, action, host,
    started_at}, best effort — None when unreadable), `path` the lock file."""

    def __init__(self, message: str, *, path: str, holder: dict | None):
        super().__init__(message)
        self.path = path
        self.holder = holder


class LeftoverProcesses(OperationLocked):
    """The lock is free, but the record of its last holder — one that died
    or could not stop a process group — names process groups that are
    STILL RUNNING (`groups`), or cannot be verified (`groups` empty). The
    record is left untouched."""

    def __init__(self, message: str, *, path: str, holder: dict | None,
                 groups: list[int] | None = None):
        super().__init__(message, path=path, holder=holder)
        self.groups = groups or []


def lock_path(*, git_common_dir: str | None = None, project_path: str | None = None) -> str:
    """The lock file of a repository (`git_common_dir`) or, for a project
    CONFIRMED outside git, of its directory (`project_path`)."""
    if git_common_dir:
        return os.path.join(os.path.realpath(git_common_dir), LOCK_NAME)
    if not project_path:
        raise ValueError("a lock needs a git common dir or a project path")
    key = hashlib.sha256(os.path.realpath(project_path).encode("utf-8")).hexdigest()
    return os.path.join(str(dct_paths.release_lock_dir()), f"{key}.lock")


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


class _UnreadableRecord(ValueError):
    """The lock file holds something that is not a holder record."""


def _parse(data: bytes) -> dict | None:
    """The record in `data` — None when empty; ValueError when unreadable
    (not UTF-8, not JSON, not an object)."""
    text = data.decode("utf-8").strip().strip("\0")
    if not text:
        return None
    record = json.loads(text)
    if not isinstance(record, dict):
        raise _UnreadableRecord("the holder record is not an object")
    return record


def read_holder(path: str) -> dict | None:
    """What the current (or the last, if it died) holder recorded — best
    effort: None when the file is empty, missing or unreadable."""
    try:
        with open(path, "rb") as fh:
            return _parse(fh.read(_HOLDER_MAX_BYTES))
    except (OSError, ValueError):
        return None


def _clear_hint(path: str) -> str:
    return (f"if no such process exists any more, empty the lock file "
            f"(`: > {path}`) and run the command again")


def _check_leftover(fd: int, path: str) -> None:
    """Refuse (LeftoverProcesses) when the record the LAST holder left in
    the lock file names process groups that still run or cannot be
    verified — see OWNERSHIP MODEL. Called with the flock held."""
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, _HOLDER_MAX_BYTES)
    try:
        record = _parse(data)
    except ValueError:
        raise LeftoverProcesses(
            f"the release lock {path} holds a record that cannot be read, so it cannot be "
            f"verified that no process of an earlier dct release operation is still running "
            f"on this repository — nothing done. Check for leftover processes; "
            f"{_clear_hint(path)}.", path=path, holder=None) from None
    if record is None:
        return
    groups = record.get("groups") or []
    if not isinstance(groups, list) or not all(type(g) is int and g > 1 for g in groups):
        groups, unreadable = [], True
    else:
        unreadable = False
    who = (f"pid {record.get('pid')}, `{record.get('action')}` since "
           f"{record.get('started_at')}, host {record.get('host')}")
    if unreadable or record.get("spawning"):
        raise LeftoverProcesses(
            f"an earlier dct release operation ({who}) died while it was starting a process "
            f"whose process group was not recorded yet, so it cannot be verified that "
            f"nothing of it is still running on this repository — nothing done. Check for "
            f"leftover processes (the version-sync script, a git hook); "
            f"{_clear_hint(path)}.", path=path, holder=record)
    if not groups:
        return
    if record.get("host") != socket.gethostname():
        raise LeftoverProcesses(
            f"an earlier dct release operation ({who}) left process groups "
            f"{', '.join(map(str, groups))} running on ANOTHER host — from here it cannot be "
            f"verified that they ended; nothing done. Check on that host; "
            f"{_clear_hint(path)}.", path=path, holder=record)
    if not hasattr(os, "killpg"):  # pragma: no cover - groups are never recorded there
        return
    alive = [pgid for pgid in groups if procs.group_alive(pgid)]
    if alive:
        listed = ", ".join(map(str, alive))
        raise LeftoverProcesses(
            f"a process group started by an earlier dct release operation ({who}) that did "
            f"not finish is still running: {listed} — it may still be writing to this "
            f"repository, so nothing is done. Wait for it or stop it (`kill -TERM -- "
            f"-{alive[0]}`; inspect with `ps -o pid,pgid,command -g {alive[0]}` / `ps -eo "
            f"pid,pgid,command`), then run the command again. If that group id now belongs "
            f"to an unrelated process: {_clear_hint(path)}.",
            path=path, holder=record, groups=alive)


class Held:
    """The held operation lock: `path`, and the ledger of the process
    groups the operation started (dct.core.procs calls spawning / started
    / spawn_failed / ended) — kept in the lock file's holder record."""

    def __init__(self, fd: int, path: str, action: str):
        self.path = path
        self._fd = fd
        self._mutex = threading.Lock()
        self._record = {
            "pid": os.getpid(), "action": action, "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "spawning": False, "groups": [],
        }
        self._spawning = 0
        self._write()

    @property
    def groups(self) -> list[int]:
        """Process groups started and not confirmed dead."""
        return list(self._record["groups"])

    def _write(self) -> None:
        # ONE write of a fixed-size record: a holder killed at any moment
        # leaves either the previous or the new record, never a mix.
        data = json.dumps(self._record).encode("utf-8")
        if len(data) >= _HOLDER_MAX_BYTES:
            raise RuntimeError("the release lock's holder record is too large")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data.ljust(_HOLDER_MAX_BYTES - 1) + b"\n")

    def _update(self, *, spawning: int = 0, add: int | None = None,
                remove: int | None = None) -> None:
        with self._mutex:
            self._spawning += spawning
            groups = self._record["groups"]
            if add is not None and add not in groups:
                groups.append(add)
            if remove is not None and remove in groups:
                groups.remove(remove)
            self._record["spawning"] = self._spawning > 0
            self._write()

    def spawning(self) -> None:
        """A process is about to be started."""
        self._update(spawning=1)

    def started(self, pgid: int) -> None:
        """…and leads the process group `pgid`."""
        self._update(spawning=-1, add=pgid)

    def spawn_failed(self) -> None:
        """…but could not be started."""
        self._update(spawning=-1)

    def ended(self, pgid: int) -> None:
        """The group `pgid` is CONFIRMED gone."""
        self._update(remove=pgid)

    def _release(self) -> None:
        """Empty the record — unless a group was never confirmed dead: then
        the record stays for the next operation to check."""
        with self._mutex:
            if self._record["groups"] or self._spawning > 0:
                return
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, 0)


@contextlib.contextmanager
def operation_lock(path: str, action: str) -> Iterator[Held]:
    """Hold the operation lock `path` for `action`, as ONE supervised
    operation (OWNERSHIP MODEL in the module docstring) — yields the Held
    lock. Refuses at once, never waits: OperationLocked when another
    operation holds it, LeftoverProcesses when the last holder's record
    names process groups that still run (or cannot be verified). The
    holder record is written into the file for a contender's refusal
    message; the file stays (deleting a locked file would split the lock).

    While it is held, SIGINT / SIGTERM / SIGHUP on the main thread end the
    operation as exceptions (KeyboardInterrupt / procs.Interrupted) and the
    process groups the runner starts are recorded; the handlers are
    restored — and a SIGTERM / SIGHUP delivered again — after the lock is
    released."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with procs.supervision() as scope:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if not _try_lock(fd):
                holder = read_holder(path)
                who = (f"pid {holder.get('pid')}, `{holder.get('action')}` since "
                       f"{holder.get('started_at')}") if holder else "holder unknown"
                raise OperationLocked(
                    f"another dct release operation is running on this repository ({who}; "
                    f"lock {path}) — wait for it to finish, then run the command again; "
                    f"nothing done.", path=path, holder=holder)
            try:
                _check_leftover(fd, path)
                held = Held(fd, path, action)
                scope.groups = held
                try:
                    yield held
                finally:
                    held._release()
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
