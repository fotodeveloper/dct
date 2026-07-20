"""dct migrate-home — move a legacy ~/.claude installation to the
platform-neutral locations (ADR #52, CP #1234).

Planner/executor split: ``plan_migration()`` inspects the filesystem and
returns the steps; ``execute_migration()`` performs them. The CLI implements
``--dry-run`` by printing the plan without executing it.

Execution is **copy-then-swap**, hardened across two Codex review rounds
(b6efa5c, d4f3c4c):

- Executions are serialized by an exclusive file lock — a second concurrent
  ``migrate-home`` aborts instead of racing the first one's rollback.
- The sqlite database is snapshotted with the SQLite backup API (reads
  through an un-checkpointed WAL, so committed rows are never lost), falling
  back to a raw copy when the file is not a real database. Sidecars are
  consolidated into the snapshot, never copied; a pre-existing target
  sidecar is a hard conflict that cancels the whole database unit.
- The config file is published LAST, because its presence is what flips
  path resolution to the new home; legacy sources are unlinked only after
  every copy has landed. A crash at any point leaves a consistent install.
- Copies commit atomically via hard-link (``os.link`` raises EEXIST instead
  of clobbering); tmp-cleanup failures after a successful link are cleanup
  noise, never a publish failure. Byte-identical leftovers of a crashed run
  are treated as already staged, so re-runs converge — for cache/state the
  planner re-plans them too.
- The ``[database].url`` rewrite is anchored to the url assignment INSIDE
  the ``[database]`` section and accepts both basic ("…") and literal ('…')
  TOML strings. If it cannot be applied, the database move is cancelled and
  the config migrates unchanged, still pointing at the intact legacy db.
- A **fixup** plan branch converges stranded states: when the new config
  exists but its url still points at a legacy sqlite db (any earlier cancel
  path), the db move + in-place config rewrite are planned; a non-identical
  target leftover is safe to replace there, because the config url proves it
  never became the live database.
- When ``DCT_CONFIG_PATH`` is set, the config (and db) are never migrated —
  env resolution wins unconditionally, so moving the pinned file would break
  the supported configuration mechanism.

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
from datetime import datetime, timezone
from pathlib import Path

from dct import paths

_SQLITE_PREFIX = "sqlite:///"
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")


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
    if not url or not url.startswith(_SQLITE_PREFIX):
        return None
    db = Path(url[len(_SQLITE_PREFIX):])
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

    legacy_cfg = legacy / "dct.toml"
    new_cfg = paths.config_dir() / "dct.toml"
    if os.environ.get("DCT_CONFIG_PATH"):
        # Env resolution wins unconditionally — migrating the config from
        # under a pinned path would break the supported mechanism, and the
        # db url would then resolve against a missing file. Hands off both.
        pass
    elif legacy_cfg.exists() and not new_cfg.exists():
        steps.append(MigrationStep("config", legacy_cfg, new_cfg))
        db = _legacy_db_path(legacy_cfg)
        if db is not None and db.exists():
            steps.append(MigrationStep("database", db, paths.data_dir() / db.name))
    elif new_cfg.exists():
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
    """In-place atomic update of a file WE own (fixup of the new config) —
    os.replace, not the no-clobber link commit."""
    try:
        tmp = dst.with_name(dst.name + ".migrating")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, dst)
        return True
    except OSError:
        return False


def _snapshot_db(src: Path, tmp: Path) -> bool:
    """Write a consistent snapshot of src at tmp. Real sqlite files go
    through the backup API (reads through an un-checkpointed WAL); anything
    else falls back to a raw copy."""
    try:
        src_con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            dst_con = sqlite3.connect(tmp)
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
        finally:
            src_con.close()
        return True
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)
        try:
            shutil.copy2(src, tmp)
            return True
        except OSError:
            return False


def _stage_db(src: Path, dst: Path, allow_replace: bool) -> str | None:
    """Stage a database snapshot at dst. A pre-existing target sidecar is a
    hard conflict (a stale WAL next to a fresh db would be replayed into it).
    allow_replace lets fixup mode overwrite a non-identical dst — safe there,
    because the config url proves that file never went live."""
    for suffix in _DB_SIDECAR_SUFFIXES:
        if dst.with_name(dst.name + suffix).exists():
            return None
    try:
        if dst.exists() and not allow_replace:
            return "resumed" if _identical(src, dst) else None
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".migrating")
        if not _snapshot_db(src, tmp):
            tmp.unlink(missing_ok=True)
            return None
        if dst.exists():  # allow_replace: overwrite the dead orphan atomically
            os.replace(tmp, dst)
            return "created"
        if _commit(tmp, dst):
            return "created"
        return "resumed" if _identical(src, dst) else None
    except OSError:
        return None


def _rewrite_db_url(content: str, old_url: str, new_url: str) -> str | None:
    """Rewrite the url assignment INSIDE the [database] section — a quoted
    copy of the URL in a comment or another section must never be the one
    replaced. Accepts basic ("…") and literal ('…') TOML strings; always
    writes back a basic string. Returns None when nothing matches."""
    header = re.search(r"(?m)^\s*\[database\]\s*$", content)
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
    """Exclusive, non-blocking lock serializing migrate-home executions."""
    lock_dir = paths.data_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / ".migrate-home.lock", "w")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


# ── execution ────────────────────────────────────────────────────────────────


def execute_migration(steps: list[MigrationStep]) -> list[str]:
    with _migration_lock() as acquired:
        if not acquired:
            return [
                "another dct migrate-home appears to be running — aborted, "
                "nothing changed"
            ]
        return _execute_locked(steps)


def _execute_locked(steps: list[MigrationStep]) -> list[str]:
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

    # Phase 1a — stage the database snapshot (consistent via the sqlite
    # backup API; sidecars consolidated, never copied).
    db_staged = False
    if db_step is not None:
        result = _stage_db(db_step.src, db_step.dst, allow_replace=fixup_step is not None)
        if result is None:
            messages.append(
                f"conflict/failure staging {db_step.dst} — database move "
                "cancelled, nothing moved"
            )
            db_step = None
            config_content = (
                rewrite_source.src.read_text(encoding="utf-8")
                if rewrite_source is not None
                else None
            )
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
            for p in created:
                p.unlink(missing_ok=True)
            messages.append(
                f"error: could not update {fixup_step.dst} — nothing changed; "
                "re-run dct migrate-home"
            )
            return messages

    # Phase 2 — the new home is complete; remove legacy sources. Failures
    # here leave harmless duplicates, reported, never raised.
    for src in to_unlink:
        try:
            src.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            messages.append(f"warning: could not remove legacy {src}: {exc}")

    if config_published and config_step is not None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        crumb = paths.legacy_dir() / "dct.toml.migrated"
        crumb.write_text(
            f"dct config moved to {config_step.dst} on {stamp}\n"
            "This breadcrumb can be deleted once nothing references ~/.claude.\n",
            encoding="utf-8",
        )
        messages.append(f"breadcrumb left at {crumb}")

    return messages
