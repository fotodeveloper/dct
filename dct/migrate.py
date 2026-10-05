"""dct migrate-home — move a legacy ~/.claude installation to the
platform-neutral locations (ADR #52, CP #1234).

Planner/executor split: ``plan_migration()`` inspects the filesystem and
returns the steps; ``execute_migration()`` performs them. The CLI implements
``--dry-run`` by printing the plan without executing it.

Execution is **copy-then-swap**, hardened across three Codex review rounds
(b6efa5c, d4f3c4c, 0b67d18):

- Executions are serialized by an exclusive non-blocking file lock in the
  CONFIG dir (every migration touches it; a config-only run must not require
  a writable data dir). Lock-setup failures abort gracefully, never crash.
- The database unit is integrity-first: a file is treated as sqlite only on
  a positive header match; genuine databases are snapshotted with the SQLite
  backup API FROM a connection holding ``BEGIN EXCLUSIVE`` on the source —
  held through the cutover, so a live writer gets an explicit BUSY (and the
  migration aborts with "in use") instead of silently losing its commit. A
  backup failure on a genuine database aborts the unit — never a raw copy
  that would drop WAL'd rows. Snapshots preserve the source file mode. A
  byte-identical target leftover with a non-empty source WAL is
  re-snapshotted, not blessed. Sidecars are consolidated, never copied; a
  pre-existing target sidecar is a hard conflict.
- The config file is published LAST; legacy sources are unlinked only after
  every copy landed. Copies commit atomically via hard-link (EEXIST instead
  of clobbering); tmp-cleanup failures after a successful link are noise,
  never a publish failure.
- The ``[database].url`` rewrite is anchored INSIDE the ``[database]``
  section (trailing header comments allowed) and accepts both basic and
  literal TOML strings; an inapplicable rewrite cancels the database move.
- A **fixup** plan branch converges stranded states (new config exists, url
  still legacy). A replaced orphan target that later fails its config update
  is KEPT — its predecessor is unrecoverable, and the staged snapshot lets
  the next run converge; the message says so honestly.
- ``DCT_CONFIG_PATH`` pins the config (nothing config/db is migrated);
  ``DCT_DATABASE_URL`` pins the database (no database step is ever planned —
  runtime precedence makes the env target authoritative, so the config url
  alone can never prove a target file dead).

Guarantees: idempotent re-runs, never overwrites a live target, breadcrumb
at ``~/.claude/dct.toml.migrated``.
"""

import filecmp
import os
import re
import shutil
import sqlite3
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dct import paths

_SQLITE_MAGIC = b"SQLite format 3\x00"
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")

# Phase-2 lock-release policy seam (tests flip it): on Windows the sqlite
# handles must close BEFORE the legacy files are unlinked — sqlite does not
# open with FILE_SHARE_DELETE, so unlinking under a held handle is a sharing
# violation and the legacy db would be orphaned forever (the flipped config
# means no later plan ever cleans it up). NOT used for the lock mechanics in
# _migration_lock, which must always match the real platform.
_IS_WINDOWS = os.name == "nt"


def _release_locks(held: list) -> None:
    for con in held:
        try:
            con.close()
        except sqlite3.Error:
            pass
    held.clear()


@dataclass(frozen=True)
class MigrationStep:
    kind: str  # "config" | "config_fixup" | "database" | "cache" | "state"
    src: Path
    dst: Path


# ── planning ─────────────────────────────────────────────────────────────────


def _config_url(config_file: Path) -> str | None:
    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f).get("database", {}).get("url", "") or None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _legacy_db_path(config_file: Path) -> Path | None:
    """The sqlite db referenced by a config, when that db lives under
    ~/.claude. Anything else (postgres, sqlite elsewhere) → None."""
    url = _config_url(config_file)
    # K-99: parsed as the engine parses it (paths.sqlite_database_path) —
    # never by slicing the prefix (SQLAlchemy 2.1 percent-decodes the path).
    database = paths.sqlite_database_path(url) if url else None
    if database is None:
        return None
    db = Path(database)
    if not paths.is_legacy(db):
        return None
    return db


def _identical(a: Path, b: Path) -> bool:
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def plan_migration() -> list[MigrationStep]:
    legacy = paths.legacy_dir()
    steps: list[MigrationStep] = []
    # Env pins: DCT_CONFIG_PATH makes the config unmovable (env resolution
    # wins unconditionally); DCT_DATABASE_URL makes the database unmovable —
    # the env target is what runtime actually uses, so no config url can
    # prove any file dead while it is set.
    env_config = bool(os.environ.get("DCT_CONFIG_PATH"))
    env_db = bool(os.environ.get("DCT_DATABASE_URL"))

    legacy_cfg = legacy / "dct.toml"
    new_cfg = paths.config_dir() / "dct.toml"
    if env_config:
        pass
    elif legacy_cfg.exists() and not new_cfg.exists():
        steps.append(MigrationStep("config", legacy_cfg, new_cfg))
        if not env_db:
            db = _legacy_db_path(legacy_cfg)
            if db is not None and db.exists():
                steps.append(MigrationStep("database", db, paths.data_dir() / db.name))
    elif new_cfg.exists() and not env_db:
        # Fixup branch: an earlier run published the config but a cancel
        # path left the database stranded under ~/.claude — finish the job.
        db = _legacy_db_path(new_cfg)
        if db is not None and db.exists():
            steps.append(MigrationStep("database", db, paths.data_dir() / db.name))
            steps.append(MigrationStep("config_fixup", new_cfg, new_cfg))

    cache_src = legacy / "dct-update-check.json"
    cache_dst = paths.cache_dir() / cache_src.name
    if cache_src.exists() and (not cache_dst.exists() or _identical(cache_src, cache_dst)):
        steps.append(MigrationStep("cache", cache_src, cache_dst))

    for name in ("dct-web.pid", "dct-web.log"):
        src = legacy / name
        dst = paths.state_dir() / name
        if src.exists() and (not dst.exists() or _identical(src, dst)):
            steps.append(MigrationStep("state", src, dst))

    return steps


# ── staging primitives ───────────────────────────────────────────────────────


def _commit(tmp: Path, dst: Path) -> bool:
    """Publish tmp as dst atomically without clobbering (os.link raises
    FileExistsError instead of replacing). A failing cleanup of tmp AFTER a
    successful link is noise, never a publish failure — reporting it as one
    would trigger a rollback of files the published config already uses."""
    try:
        os.link(tmp, dst)
        linked = True
    except FileExistsError:
        linked = False
    try:
        tmp.unlink()
    except OSError:
        pass
    return linked


def _stage_copy(src: Path, dst: Path) -> str | None:
    """Copy src to dst with an atomic no-clobber commit. Returns "created",
    "resumed" (dst already byte-identical — leftover of a previous run), or
    None on conflict/failure."""
    try:
        if dst.exists():
            return "resumed" if _identical(src, dst) else None
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".migrating")
        shutil.copy2(src, tmp)
        if _commit(tmp, dst):
            return "created"
        return "resumed" if _identical(src, dst) else None
    except OSError:
        return None


def _stage_text(content: str, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".migrating")
        tmp.write_text(content, encoding="utf-8")
        return _commit(tmp, dst)
    except OSError:
        return False


def _replace_text(content: str, dst: Path) -> bool:
    """In-place atomic update of a file WE own (fixup of the new config)."""
    try:
        tmp = dst.with_name(dst.name + ".migrating")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, dst)
        return True
    except OSError:
        return False


# ── database unit ────────────────────────────────────────────────────────────


def _is_sqlite(path: Path) -> bool:
    """Positive identification only — anything without the magic header is
    NOT a database and may be raw-copied; anything with it must go through
    the backup API or not migrate at all."""
    try:
        with open(path, "rb") as f:
            return f.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _run_backup(src_con: sqlite3.Connection, dst_con: sqlite3.Connection) -> None:
    src_con.backup(dst_con)


def _quiesce_sqlite(path: Path) -> sqlite3.Connection | None:
    """Exclusive-locking handle on a sqlite file, or None when busy/broken.

    locking_mode=EXCLUSIVE + BEGIN IMMEDIATE/COMMIT retains the file lock
    with NO open transaction (an open BEGIN EXCLUSIVE would deadlock the
    backup API against itself). Writers get BUSY until the handle closes."""
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(path, timeout=0.5)
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        con.execute("BEGIN IMMEDIATE")
        con.execute("COMMIT")
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
        return None


def _stage_raw_db(src: Path, dst: Path, allow_replace: bool) -> str | None:
    """Raw-copy staging for a non-sqlite 'database' file (empty/garbage)."""
    try:
        if dst.exists():
            if _identical(src, dst):
                return "resumed"
            # A genuine sqlite database at the target is NEVER clobbered by
            # a non-database source, fixup license or not (R4-P1).
            if not allow_replace or _is_sqlite(dst):
                return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".migrating")
        shutil.copy2(src, tmp)
        if dst.exists():
            if _is_sqlite(dst):
                tmp.unlink(missing_ok=True)
                return None
            os.replace(tmp, dst)
            return "replaced"
        if _commit(tmp, dst):
            return "created"
        return "resumed" if _identical(src, dst) else None
    except OSError:
        return None


def _stage_db(
    src: Path, dst: Path, allow_replace: bool
) -> tuple[str | None, sqlite3.Connection | None]:
    """Stage a database snapshot at dst. Returns (status, lock_connection).

    status: "created" | "replaced" | "resumed" | "busy" | None (conflict/
    failure). For a genuine sqlite file the returned connection holds
    ``BEGIN EXCLUSIVE`` on the source — the caller must keep it until after
    the legacy files are unlinked (writers stay quiesced through cutover)
    and close it afterwards.
    """
    for suffix in _DB_SIDECAR_SUFFIXES:
        if dst.with_name(dst.name + suffix).exists():
            return None, None
    if not _is_sqlite(src):
        return _stage_raw_db(src, dst, allow_replace), None

    lock_con = _quiesce_sqlite(src)
    if lock_con is None:
        return "busy", None

    try:
        wal = src.with_name(src.name + "-wal")
        wal_dirty = wal.exists() and wal.stat().st_size > 0
        dst_exists = dst.exists()
        refresh_leftover = dst_exists and _identical(src, dst)
        if dst_exists and not allow_replace and not refresh_leftover:
            lock_con.close()
            return None, None
        if refresh_leftover and not wal_dirty and not allow_replace:
            return "resumed", lock_con  # nothing newer than the leftover
        # Snapshot needed: fresh target, leftover with a dirty WAL, or an
        # orphan replacement licensed by fixup.
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".migrating")
        tmp.unlink(missing_ok=True)
        try:
            dst_con = sqlite3.connect(tmp)
            try:
                _run_backup(lock_con, dst_con)
            finally:
                dst_con.close()
            shutil.copymode(src, tmp)
        except (sqlite3.Error, OSError):
            # Genuine database, failed backup: abort the unit — a raw copy
            # here would silently drop WAL'd rows.
            tmp.unlink(missing_ok=True)
            lock_con.close()
            return None, None
        if dst.exists():
            # A target can be activated and written between the planning
            # comparison and this point (R4-P1): quiesce it and re-verify
            # the replace license — byte-identity with the source main —
            # under that lock. Anything divergent or busy is refused.
            dst_lock = _quiesce_sqlite(dst) if _is_sqlite(dst) else None
            if _is_sqlite(dst) and dst_lock is None:
                tmp.unlink(missing_ok=True)
                lock_con.close()
                return "busy", None
            try:
                if dst_lock is not None and not _identical(src, dst):
                    tmp.unlink(missing_ok=True)
                    lock_con.close()
                    return None, None
                os.replace(tmp, dst)
            finally:
                if dst_lock is not None:
                    dst_lock.close()
            return "replaced", lock_con
        if _commit(tmp, dst):
            return "created", lock_con
        status = "resumed" if _identical(src, dst) else None
        if status is None:
            lock_con.close()
            return None, None
        return status, lock_con
    except OSError:
        lock_con.close()
        return None, None


def _rewrite_db_url(content: str, old_url: str, new_url: str) -> str | None:
    """Rewrite the url assignment INSIDE the [database] section — a quoted
    copy of the URL in a comment or another section must never be the one
    replaced. The header may carry a trailing comment; both basic ("…") and
    literal ('…') strings are accepted; a basic string is always written
    back. Returns None when nothing matches."""
    header = re.search(r"(?m)^\s*\[database\]\s*(?:#.*)?$", content)
    if header is None:
        return None
    start = header.end()
    next_section = re.search(r"(?m)^\s*\[", content[start:])
    end = start + next_section.start() if next_section else len(content)
    section = content[start:end]
    pattern = re.compile(
        r"(?m)^(\s*url\s*=\s*)([\"'])" + re.escape(old_url) + r"\2"
    )
    rewritten, count = pattern.subn(
        lambda m: m.group(1) + '"' + new_url + '"', section, count=1
    )
    if count != 1:
        return None
    return content[:start] + rewritten + content[end:]


@contextmanager
def _migration_lock():
    """Exclusive, non-blocking lock serializing migrate-home executions.

    Lives in the CONFIG dir — every migration touches the config, while a
    config-only (PostgreSQL) run must not require a writable data dir.
    Yields "ok", "busy", or "error: …" — setup failures degrade to a
    message, never an uncaught exception."""
    handle = None
    try:
        try:
            lock_dir = paths.config_dir()
            lock_dir.mkdir(parents=True, exist_ok=True)
            # Suppression rationale: the lock handle must outlive this try
            # block — it is closed in the finally after the flock is done.
            handle = open(lock_dir / ".migrate-home.lock", "w")  # noqa: SIM115
        except OSError as exc:
            yield f"error: {exc}"
            return
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield "busy"
            return
        yield "ok"
    finally:
        if handle is not None:
            handle.close()


# ── execution ────────────────────────────────────────────────────────────────


def execute_migration(steps: list[MigrationStep]) -> list[str]:
    with _migration_lock() as lock_status:
        if lock_status == "busy":
            return [
                ("another dct migrate-home appears to be running — aborted, "
                "nothing changed")
            ]
        if lock_status != "ok":
            return [
                (f"cannot acquire the migration lock ({lock_status}) — "
                "aborted, nothing changed")
            ]
        return _execute_locked(steps)


def _execute_locked(steps: list[MigrationStep]) -> list[str]:
    # The db lock connection (BEGIN EXCLUSIVE on the source) must survive
    # until AFTER phase 2 unlinked the legacy files — writers stay quiesced
    # through the whole cutover window.
    held_locks: list[sqlite3.Connection] = []
    try:
        return _run_phases(steps, held_locks)
    finally:
        for con in held_locks:
            try:
                con.close()
            except sqlite3.Error:
                pass


def _run_phases(
    steps: list[MigrationStep], held_locks: list[sqlite3.Connection]
) -> list[str]:
    messages: list[str] = []
    config_step = next((s for s in steps if s.kind == "config"), None)
    fixup_step = next((s for s in steps if s.kind == "config_fixup"), None)
    db_step = next((s for s in steps if s.kind == "database"), None)
    to_unlink: list[Path] = []
    created: list[Path] = []  # files THIS run created, for abort rollback

    # Decide the config content up front: the rewrite must be provably
    # applicable BEFORE any file is copied, else the db move is cancelled.
    config_content: str | None = None
    rewrite_source = config_step or fixup_step
    if rewrite_source is not None:
        try:
            config_content = rewrite_source.src.read_text(encoding="utf-8")
        except OSError as exc:
            messages.append(f"error: cannot read {rewrite_source.src}: {exc}")
            return messages
    if db_step is not None and rewrite_source is not None and config_content is not None:
        old_url = _config_url(rewrite_source.src) or ""
        rewritten = (
            _rewrite_db_url(config_content, old_url, paths.sqlite_url(db_step.dst))
            if old_url
            else None
        )
        if rewritten is None:
            messages.append(
                "warning: could not rewrite [database].url — "
                "database left at its legacy location"
            )
            db_step = None
        else:
            config_content = rewritten

    def _cancel_db_unit() -> None:
        nonlocal db_step, config_content
        db_step = None
        config_content = (
            rewrite_source.src.read_text(encoding="utf-8")
            if rewrite_source is not None
            else None
        )

    # Phase 1a — stage the database snapshot (consistent + writer-quiesced).
    db_staged = False
    if db_step is not None:
        result, lock_con = _stage_db(
            db_step.src, db_step.dst, allow_replace=fixup_step is not None
        )
        if lock_con is not None:
            held_locks.append(lock_con)
        if result == "busy":
            messages.append(
                f"database {db_step.src} is in use — close dct sessions and "
                "daemons, then re-run; database left in place"
            )
            _cancel_db_unit()
        elif result is None:
            messages.append(
                f"conflict/failure staging {db_step.dst} — database move "
                "cancelled, nothing moved"
            )
            _cancel_db_unit()
        else:
            db_staged = True
            if result == "created":
                created.append(db_step.dst)
            to_unlink.append(db_step.src)
            for suffix in _DB_SIDECAR_SUFFIXES:
                side = db_step.src.with_name(db_step.src.name + suffix)
                if side.exists():
                    to_unlink.append(side)
            messages.append(f"moved {db_step.src} -> {db_step.dst}")

    # Phase 1b — cache and web state (independent, per-file).
    for step in steps:
        if step.kind not in ("cache", "state"):
            continue
        result = _stage_copy(step.src, step.dst)
        if result is None:
            messages.append(f"skip {step.src} — target already exists: {step.dst}")
            continue
        if result == "created":
            created.append(step.dst)
        to_unlink.append(step.src)
        messages.append(f"moved {step.src} -> {step.dst}")

    # Phase 1c — config LAST: its presence (or its rewritten url, in fixup
    # mode) is what flips resolution to the new home.
    config_published = False
    if config_step is not None and config_content is not None:
        if _stage_text(config_content, config_step.dst):
            messages.append(f"moved {config_step.src} -> {config_step.dst}")
            to_unlink.append(config_step.src)
            config_published = True
        else:
            for p in created:
                p.unlink(missing_ok=True)
            messages.append(
                f"conflict: {config_step.dst} appeared during migration — "
                "nothing changed; re-run dct migrate-home"
            )
            return messages
    elif fixup_step is not None and db_staged and config_content is not None:
        if _replace_text(config_content, fixup_step.dst):
            messages.append(f"updated {fixup_step.dst} — database url now neutral")
        else:
            # Do NOT roll back the staged snapshot: a replaced orphan's
            # predecessor is unrecoverable, and keeping the snapshot lets
            # the next run converge. Legacy stays authoritative meanwhile.
            messages.append(
                f"error: could not update {fixup_step.dst}; the database "
                f"snapshot is staged at {db_step.dst if db_step else '?'} — "
                "legacy left in place, re-run dct migrate-home to converge"
            )
            return messages

    # Phase 2 — the new home is complete; remove legacy sources. Failures
    # here leave harmless duplicates, reported, never raised. On POSIX the
    # db lock (if any) is still held here, so no writer can commit into the
    # legacy database between the snapshot and its removal; on Windows the
    # handles must close first (no FILE_SHARE_DELETE) — the config has
    # already flipped, so the writer window is closed either way.
    if _IS_WINDOWS:
        _release_locks(held_locks)
    for src in to_unlink:
        try:
            src.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            messages.append(f"warning: could not remove legacy {src}: {exc}")

    if config_published and config_step is not None:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        crumb = paths.legacy_dir() / "dct.toml.migrated"
        crumb.write_text(
            f"dct config moved to {config_step.dst} on {stamp}\n"
            "This breadcrumb can be deleted once nothing references ~/.claude.\n",
            encoding="utf-8",
        )
        messages.append(f"breadcrumb left at {crumb}")

    return messages
