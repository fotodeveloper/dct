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
kernel releases it when the holder dies — no stale-lock cleanup, the file is
never deleted. It is NOT git's index.lock: editors and other git processes
are never blocked by it; it coordinates dct's own release operations only.
No SQL transaction is held around the effects (the ledger writes stay
short, K-96 D7).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

from dct import paths as dct_paths

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


def read_holder(path: str) -> dict | None:
    """What the current holder recorded (best effort)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_HOLDER_MAX_BYTES)
        holder = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return holder if isinstance(holder, dict) else None


@contextlib.contextmanager
def operation_lock(path: str, action: str) -> Iterator[str]:
    """Hold the operation lock `path` for `action` — OperationLocked at once
    when another operation holds it (never waits). The holder record
    {pid, action, host, started_at} is written into the file for the
    refusal message of a contender; the file stays (deleting a locked file
    would split the lock)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        if not _try_lock(fd):
            holder = read_holder(path)
            who = (f"pid {holder.get('pid')}, `{holder.get('action')}` since "
                   f"{holder.get('started_at')}") if holder else "holder unknown"
            raise OperationLocked(
                f"another dct release operation is running on this repository ({who}; lock "
                f"{path}) — wait for it to finish, then run the command again; nothing done.",
                path=path, holder=holder)
        try:
            record = json.dumps({
                "pid": os.getpid(), "action": action, "host": socket.gethostname(),
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, record)
            yield path
        finally:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
            _unlock(fd)
    finally:
        os.close(fd)
