"""Platform-neutral config/data/cache/state locations (ADR #52).

Family directory "dctools" shared by the dc tools suite, with per-tool
filenames inside (dct.toml, dct.db). POSIX — macOS and Linux uniformly —
follows XDG conventions with env overrides; this deliberately diverges from
the native macOS Library/Application Support because dct's audience is
terminal developers managing dotfiles. Windows delegates to platformdirs:
%LOCALAPPDATA%\\dctools with roaming=False (a WAL SQLite db on a roaming
profile risks corruption) and appauthor=False (avoids dctools\\dctools
path doubling).

Legacy fallback (non-breaking 1.2.0): existing installs keep
~/.claude/dct.toml and ~/.claude/dct.db working with zero action —
resolution prefers the new locations only when they exist.

All resolution happens at call time (no import-time constants), so env
changes and test monkeypatching are always honored.
"""

import os
import sys
from pathlib import Path, PurePath

FAMILY = "dctools"

_IS_WINDOWS = sys.platform == "win32"


def _xdg(env_var: str, default: str) -> Path:
    base = os.environ.get(env_var)
    return (Path(base) if base else Path.home() / default) / FAMILY


def _windows_dirs():
    from platformdirs import PlatformDirs

    return PlatformDirs(FAMILY, appauthor=False, roaming=False)


def config_dir() -> Path:
    if _IS_WINDOWS:
        return Path(_windows_dirs().user_data_dir)
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    if _IS_WINDOWS:
        return Path(_windows_dirs().user_data_dir)
    return _xdg("XDG_DATA_HOME", ".local/share")


def cache_dir() -> Path:
    if _IS_WINDOWS:
        return Path(_windows_dirs().user_cache_dir)
    return _xdg("XDG_CACHE_HOME", ".cache")


def state_dir() -> Path:
    if _IS_WINDOWS:
        return Path(_windows_dirs().user_data_dir)
    return _xdg("XDG_STATE_HOME", ".local/state")


def legacy_dir() -> Path:
    return Path.home() / ".claude"


def is_legacy(path: PurePath | str) -> bool:
    """True when path lives directly under ~/.claude. Compares realpaths so a
    symlinked HOME (macOS /var → /private/var) and a canonical path in a
    config URL still recognize each other."""
    parent = Path(os.path.realpath(Path(path).parent))
    return parent == Path(os.path.realpath(legacy_dir()))


def default_config_path() -> Path:
    env = os.environ.get("DCT_CONFIG_PATH")
    if env:
        return Path(env)
    new = config_dir() / "dct.toml"
    if new.exists():
        return new
    legacy = legacy_dir() / "dct.toml"
    if legacy.exists():
        return legacy
    return new


def default_db_path() -> Path:
    env = os.environ.get("DCT_DATA_DIR")
    if env:
        return Path(env) / "dct.db"
    new = data_dir() / "dct.db"
    if new.exists():
        return new
    legacy = legacy_dir() / "dct.db"
    if legacy.exists():
        return legacy
    return new


def sqlite_url(path: PurePath | str) -> str:
    return f"sqlite:///{PurePath(path).as_posix()}"
