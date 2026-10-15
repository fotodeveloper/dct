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


def dct_reference_path() -> Path:
    """Canonical integration reference, independent of the database config file."""
    return config_dir() / "dct-reference.md"


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


def release_store_dir() -> Path:
    """dct's content-addressed store of release artifacts for projects
    CONFIRMED outside git (K-76 / K-78: the approved version-file contents
    and the changelog baseline a resume compares against) —
    <data dir>/release-blobs; DCT_DATA_DIR wins, as for the database."""
    env = os.environ.get("DCT_DATA_DIR")
    return (Path(env) if env else data_dir()) / "release-blobs"


def release_lock_dir() -> Path:
    """Where the release OPERATION LOCK of a project CONFIRMED outside git
    lives (K-96 D7; a project in git locks in its git common dir instead) —
    <data dir>/release-locks; DCT_DATA_DIR wins, as for the database."""
    env = os.environ.get("DCT_DATA_DIR")
    return (Path(env) if env else data_dir()) / "release-locks"


def sqlite_url(path: PurePath | str) -> str:
    """The ONE builder of a file-backed SQLite URL (K-99) — every SQLite URL
    dct writes (the default config, `dct init`, migrate-home) comes from here.

    Rendered by SQLAlchemy's own URL object, never by string concatenation:
    SQLAlchemy 2.1 percent-decodes the database part of a URL when it parses
    one and 2.0 does not, so a hand-built "sqlite:///" + path names ANOTHER
    file on 2.1 as soon as the path holds a '%' (`a%20b.db` → `a b.db`).
    The rendered URL reads back as the same path on the SQLAlchemy that
    rendered it (sqlite_database_path / make_url / create_engine); its TEXT
    may differ between versions (2.1 percent-encodes ':', '#', '?', '%').
    A path the SQLAlchemy in use cannot express in a URL is REFUSED
    (ValueError) instead of naming another file: on the 2.0 line a '?' in
    the path starts the query string (2.1 handles it).
    """
    import sqlalchemy as sa

    # Honor the caller's flavour instead of re-wrapping: PurePath() re-flavours
    # a PureWindowsPath to PurePosixPath on POSIX hosts, where the drive then
    # survives as a literal "C:\\" segment on 3.11 (3.13+ normalizes it away) —
    # that made the forward-slash guarantee depend on the Python version.
    pure = path if isinstance(path, PurePath) else PurePath(path)
    database = pure.as_posix()
    url = sa.engine.URL.create("sqlite", database=database).render_as_string(
        hide_password=False)
    if sa.engine.make_url(url).database != database:
        raise ValueError(
            f"the database path {database!r} cannot be written as a SQLite URL with "
            f"SQLAlchemy {sa.__version__} (it would name another file) — move the "
            f"database to a path without '?', or upgrade SQLAlchemy to 2.1+"
        )
    return url


def sqlite_database_path(url: str) -> str | None:
    """The ONE parser of a SQLite URL (K-99): the database FILE path exactly
    as the engine resolves it for the same URL (SQLAlchemy's make_url — the
    version in use decides about percent-decoding), or None when `url` is
    not a file-backed SQLite URL (another dialect, an in-memory database,
    unparseable text). Never slices the "sqlite:///" prefix by hand."""
    import sqlalchemy as sa

    try:
        parsed = sa.engine.make_url(url)
    except sa.exc.ArgumentError:
        return None
    if parsed.get_backend_name() != "sqlite":
        return None
    if not parsed.database or parsed.database == ":memory:":
        return None
    return parsed.database
