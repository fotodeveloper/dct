"""dct migrate-home — move a legacy ~/.claude installation to the
platform-neutral locations (ADR #52, CP #1234).

Planner/executor split: ``plan_migration()`` inspects the filesystem and
returns the steps; ``execute_migration()`` performs them. The CLI implements
``--dry-run`` by printing the plan without executing it.

Guarantees: idempotent (a second run plans nothing), never overwrites an
existing target (a conflicting target drops the step at plan time and is
re-checked at execute time), and leaves a breadcrumb file at
``~/.claude/dct.toml.migrated`` pointing at the new home.

The sqlite database moves only when the legacy config's ``[database].url``
points at a file under ``~/.claude`` — a PostgreSQL URL (or a sqlite file
kept elsewhere) means only the config file moves, byte-identical. When the
db moves, the url inside the migrated config is rewritten by exact textual
replacement of the quoted value — never a parse/re-serialize round-trip, so
every other byte of the user's config survives.
"""

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
            if not dst_db.exists():
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


def execute_migration(steps: list[MigrationStep]) -> list[str]:
    messages: list[str] = []
    db_step = next((s for s in steps if s.kind == "database"), None)
    config_moved_to: Path | None = None
    db_move_cancelled = False

    for step in steps:
        if step.dst.exists():
            messages.append(f"skip {step.src} — target already exists: {step.dst}")
            continue
        step.dst.parent.mkdir(parents=True, exist_ok=True)

        if step.kind == "config" and db_step is not None:
            # Rewrite [database].url to the db's new home while copying —
            # exact textual replacement of the quoted value, nothing else.
            content = step.src.read_text(encoding="utf-8")
            old_url = _legacy_config_url(step.src) or ""
            quoted_old = f'"{old_url}"'
            if old_url and quoted_old in content:
                quoted_new = f'"{paths.sqlite_url(db_step.dst)}"'
                step.dst.write_text(
                    content.replace(quoted_old, quoted_new, 1), encoding="utf-8"
                )
                step.src.unlink()
            else:
                db_move_cancelled = True
                messages.append(
                    "warning: could not rewrite [database].url — "
                    "database left at its legacy location"
                )
                shutil.move(str(step.src), str(step.dst))
        elif step.kind == "database" and db_move_cancelled:
            continue
        else:
            shutil.move(str(step.src), str(step.dst))

        messages.append(f"moved {step.src} -> {step.dst}")
        if step.kind == "config":
            config_moved_to = step.dst
        if step.kind == "database":
            for suffix in _DB_SIDECAR_SUFFIXES:
                sidecar = step.src.with_name(step.src.name + suffix)
                sidecar_dst = step.dst.with_name(step.dst.name + suffix)
                if sidecar.exists() and not sidecar_dst.exists():
                    shutil.move(str(sidecar), str(sidecar_dst))
                    messages.append(f"moved {sidecar} -> {sidecar_dst}")

    if config_moved_to is not None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        crumb = paths.legacy_dir() / "dct.toml.migrated"
        crumb.write_text(
            f"dct config moved to {config_moved_to} on {stamp}\n"
            "This breadcrumb can be deleted once nothing references ~/.claude.\n",
            encoding="utf-8",
        )
        messages.append(f"breadcrumb left at {crumb}")

    return messages
