"""dct migrate-home — move a legacy ~/.claude installation to the
platform-neutral locations (ADR #52, CP #1234).

Planner/executor split: ``plan_migration()`` inspects the filesystem and
returns the steps; ``execute_migration()`` performs them. The CLI implements
``--dry-run`` by printing the plan without executing it.

Execution is **copy-then-swap** (Codex review of b6efa5c, findings P1-2..4):
everything is first COPIED to its target — the database unit (db + WAL/SHM
sidecars, all-or-nothing) before anything else, the config file LAST, because
its presence is what flips path resolution to the new home. Only after every
copy has landed are the legacy sources unlinked. A crash at any point leaves
a consistent installation: either legacy is still authoritative (config not
yet published) or the new home is complete. Copies commit atomically via
hard-link (``os.link`` fails with EEXIST instead of clobbering), so a target
appearing mid-run is never overwritten; a byte-identical target left over
from a crashed run is treated as already staged, which makes re-runs converge.

The sqlite database moves only when the legacy config's ``[database].url``
points at a file under ``~/.claude`` — a PostgreSQL URL (or a sqlite file
kept elsewhere) means only the config file moves, byte-identical. When the
db moves, the url is rewritten by a regex anchored to the ``url = "..."``
assignment line (never a parse/re-serialize round-trip, and never a blind
first-occurrence replacement — a quoted copy of the URL in a comment must
not swallow the rewrite). If the rewrite cannot be applied, the database
move is cancelled and the config migrates unchanged, still pointing at the
intact legacy database.

Guarantees: idempotent re-runs, never overwrites an existing target, and a
breadcrumb file at ``~/.claude/dct.toml.migrated`` points at the new home.
"""

import filecmp
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dct import paths

_SQLITE_PREFIX = "sqlite:///"
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")


@dataclass(frozen=True)
class MigrationStep:
    kind: str  # "config" | "database" | "cache" | "state"
    src: Path
    dst: Path


def _legacy_config_url(config_file: Path) -> str | None:
    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f).get("database", {}).get("url", "") or None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _legacy_db_path(config_file: Path) -> Path | None:
    """The sqlite db referenced by a legacy config, when that db itself lives
    under ~/.claude. Anything else (postgres, sqlite elsewhere) → None."""
    url = _legacy_config_url(config_file)
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
    if legacy_cfg.exists() and not new_cfg.exists():
        steps.append(MigrationStep("config", legacy_cfg, new_cfg))
        db = _legacy_db_path(legacy_cfg)
        if db is not None and db.exists():
            dst_db = paths.data_dir() / db.name
            # A byte-identical target is a leftover of a crashed run — plan
            # the step so the executor can resume and converge.
            if not dst_db.exists() or _identical(db, dst_db):
                steps.append(MigrationStep("database", db, dst_db))

    cache_src = legacy / "dct-update-check.json"
    cache_dst = paths.cache_dir() / cache_src.name
    if cache_src.exists() and not cache_dst.exists():
        steps.append(MigrationStep("cache", cache_src, cache_dst))

    for name in ("dct-web.pid", "dct-web.log"):
        src = legacy / name
        dst = paths.state_dir() / name
        if src.exists() and not dst.exists():
            steps.append(MigrationStep("state", src, dst))

    return steps


def _commit(tmp: Path, dst: Path) -> bool:
    """Publish tmp as dst atomically without clobbering: os.link raises
    FileExistsError instead of replacing, unlike write_text/shutil.move."""
    try:
        os.link(tmp, dst)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _stage_copy(src: Path, dst: Path) -> str | None:
    """Copy src to dst with an atomic no-clobber commit. Returns "created",
    "resumed" (dst already holds byte-identical content — a leftover of a
    previous run), or None on conflict/failure."""
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


def _rewrite_db_url(content: str, old_url: str, new_url: str) -> str | None:
    """Rewrite the [database] url assignment — anchored to the ``url = "…"``
    line so a quoted copy of the URL elsewhere (e.g. a comment) is never the
    one replaced. Returns None when no assignment matches."""
    pattern = re.compile(r'(?m)^(\s*url\s*=\s*)"' + re.escape(old_url) + '"')
    rewritten, count = pattern.subn(
        lambda m: m.group(1) + '"' + new_url + '"', content, count=1
    )
    return rewritten if count == 1 else None


def execute_migration(steps: list[MigrationStep]) -> list[str]:
    messages: list[str] = []
    config_step = next((s for s in steps if s.kind == "config"), None)
    db_step = next((s for s in steps if s.kind == "database"), None)
    to_unlink: list[Path] = []
    created: list[Path] = []  # files THIS run created, for abort rollback

    # Decide the config content up front: the rewrite must be provably
    # applicable BEFORE any file is copied, else the db move is cancelled.
    config_content: str | None = None
    if config_step is not None:
        try:
            config_content = config_step.src.read_text(encoding="utf-8")
        except OSError as exc:
            messages.append(f"error: cannot read {config_step.src}: {exc}")
            return messages
    if db_step is not None and config_step is not None and config_content is not None:
        old_url = _legacy_config_url(config_step.src) or ""
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

    # Phase 1a — stage the database unit (db + sidecars), all-or-nothing:
    # a WAL sidecar may hold committed transactions, so any conflict or
    # failure cancels the WHOLE unit and the url rewrite with it.
    if db_step is not None:
        unit = [(db_step.src, db_step.dst)]
        for suffix in _DB_SIDECAR_SUFFIXES:
            side = db_step.src.with_name(db_step.src.name + suffix)
            if side.exists():
                unit.append((side, db_step.dst.with_name(db_step.dst.name + suffix)))
        unit_created: list[Path] = []
        ok = True
        for src, dst in unit:
            result = _stage_copy(src, dst)
            if result is None:
                messages.append(
                    f"conflict/failure staging {dst} — database unit cancelled, "
                    "nothing moved"
                )
                ok = False
                break
            if result == "created":
                unit_created.append(dst)
        if ok:
            created += unit_created
            to_unlink += [src for src, _ in unit]
            for src, dst in unit:
                messages.append(f"moved {src} -> {dst}")
        else:
            for p in unit_created:
                p.unlink(missing_ok=True)
            db_step = None
            # Re-derive config content without the rewrite: the db stays put.
            config_content = (
                config_step.src.read_text(encoding="utf-8")
                if config_step is not None
                else None
            )

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

    # Phase 1c — config LAST: its presence is what flips resolution to the
    # new home, so it must land only when everything else already has.
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
