"""Tests for platform-neutral path resolution (dct/paths.py — ADR #52).

POSIX (macOS + Linux uniformly): XDG conventions, env-overridable, family
dir "dctools". Windows: delegated to platformdirs (%LOCALAPPDATA%\\dctools,
appauthor=False, roaming=False) — the delegation seam is tested, platformdirs'
own per-OS output is not re-tested here.

Legacy fallback (non-breaking 1.2.0): ~/.claude/dct.toml and ~/.claude/dct.db
keep working when the new locations are absent.
"""

import sys
import types
from pathlib import Path, PureWindowsPath

import pytest

from dct import paths


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """Isolate HOME and clear every env var the resolver consults."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "DCT_CONFIG_PATH",
        "DCT_DATA_DIR",
        "DCT_DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paths, "_IS_WINDOWS", False)
    return home


# --- XDG dir resolution (POSIX) ---

@pytest.mark.parametrize(
    "func,env_var",
    [
        (paths.config_dir, "XDG_CONFIG_HOME"),
        (paths.data_dir, "XDG_DATA_HOME"),
        (paths.cache_dir, "XDG_CACHE_HOME"),
        (paths.state_dir, "XDG_STATE_HOME"),
    ],
)
def test_dir_honors_xdg_env(tmp_path, monkeypatch, func, env_var):
    custom = tmp_path / "custom-xdg"
    monkeypatch.setenv(env_var, str(custom))
    assert func() == custom / "dctools"


@pytest.mark.parametrize(
    "func,default_suffix",
    [
        (paths.config_dir, ".config"),
        (paths.data_dir, ".local/share"),
        (paths.cache_dir, ".cache"),
        (paths.state_dir, ".local/state"),
    ],
)
def test_dir_defaults_without_xdg_env(fake_home, func, default_suffix):
    assert func() == fake_home / default_suffix / "dctools"


def test_dirs_resolve_lazily_not_at_import(fake_home, monkeypatch):
    """Env changes after import must be honored (no import-time constants)."""
    first = paths.config_dir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / "elsewhere"))
    second = paths.config_dir()
    assert first != second
    assert second == fake_home / "elsewhere" / "dctools"


# --- config file resolution chain ---

def test_default_config_path_env_wins(fake_home, monkeypatch, tmp_path):
    override = tmp_path / "custom" / "my.toml"
    monkeypatch.setenv("DCT_CONFIG_PATH", str(override))
    # Even with both new and legacy present, the env var wins.
    _touch(paths.config_dir() / "dct.toml")
    _touch(fake_home / ".claude" / "dct.toml")
    assert paths.default_config_path() == override


def test_default_config_path_prefers_new_when_exists(fake_home):
    new = _touch(paths.config_dir() / "dct.toml")
    _touch(fake_home / ".claude" / "dct.toml")
    assert paths.default_config_path() == new


def test_default_config_path_falls_back_to_legacy(fake_home):
    legacy = _touch(fake_home / ".claude" / "dct.toml")
    assert paths.default_config_path() == legacy


def test_default_config_path_fresh_install_uses_new(fake_home):
    assert paths.default_config_path() == paths.config_dir() / "dct.toml"


# --- database file resolution chain ---

def test_default_db_path_data_dir_env_wins(fake_home, monkeypatch, tmp_path):
    data = tmp_path / "data-override"
    monkeypatch.setenv("DCT_DATA_DIR", str(data))
    _touch(paths.data_dir() / "dct.db")
    _touch(fake_home / ".claude" / "dct.db")
    assert paths.default_db_path() == data / "dct.db"


def test_default_db_path_prefers_new_when_exists(fake_home):
    new = _touch(paths.data_dir() / "dct.db")
    _touch(fake_home / ".claude" / "dct.db")
    assert paths.default_db_path() == new


def test_default_db_path_falls_back_to_legacy(fake_home):
    legacy = _touch(fake_home / ".claude" / "dct.db")
    assert paths.default_db_path() == legacy


def test_default_db_path_fresh_install_uses_new(fake_home):
    assert paths.default_db_path() == paths.data_dir() / "dct.db"


# --- legacy detection (feeds the one-time deprecation notice) ---

def test_is_legacy_true_for_dot_claude(fake_home):
    assert paths.is_legacy(fake_home / ".claude" / "dct.toml")


def test_is_legacy_false_for_new_location(fake_home):
    assert not paths.is_legacy(paths.config_dir() / "dct.toml")


# --- sqlite URL construction (Windows defect W3) ---

def test_sqlite_url_posix():
    assert paths.sqlite_url(Path("/srv/u/dct.db")) == "sqlite:////srv/u/dct.db"


def test_sqlite_url_windows_uses_forward_slashes():
    """The flavour guarantee: a Windows path keeps its drive and uses forward
    slashes. Asserted on what SQLAlchemy READS back — the rendered text may
    percent-encode the drive colon on SQLAlchemy 2.1 (K-99)."""
    url = paths.sqlite_url(PureWindowsPath(r"C:\Work\alice\dct.db"))
    assert "\\" not in url
    assert paths.sqlite_database_path(url) == "C:/Work/alice/dct.db"


# --- K-99: SQLite URLs independent of the SQLAlchemy version ---
# SQLAlchemy 2.1 percent-decodes the database part of a URL, 2.0 does not:
# a hand-built "sqlite:///" + path names ANOTHER file on 2.1 when the path
# holds a '%'. sqlite_url / sqlite_database_path are the one builder and the
# one parser; both go through SQLAlchemy's own URL object.

_AWKWARD_NAMES = [
    "plain.db",
    "we#ird%20name.db",   # '#' (URI fragment) and a literal percent escape
    "a b.db",
    "100%.db",
    "q?x=1.db",           # '?' would start the query string
    "zażółć.db",
]


def _url_or_skip(path):
    """sqlite_url(path) — or skip when the SQLAlchemy in use cannot express
    the path ('?' on the 2.0 line); the refusal itself has its own test."""
    try:
        return paths.sqlite_url(path)
    except ValueError:
        if "?" not in str(path):
            raise
        pytest.skip("this SQLAlchemy cannot express '?' in a SQLite URL (refused)")


def test_sqlite_url_never_names_another_file(tmp_path):
    """A path with '?': either it round-trips (SQLAlchemy 2.1) or the builder
    refuses — it never returns a URL that opens a different file."""
    import sqlalchemy as sa

    path = tmp_path / "q?x=1.db"
    try:
        url = paths.sqlite_url(path)
    except ValueError as exc:
        assert "cannot be written as a SQLite URL" in str(exc)
        return
    assert sa.engine.make_url(url).database == str(path)


@pytest.mark.parametrize("name", _AWKWARD_NAMES)
def test_sqlite_url_round_trips_through_sqlalchemy(tmp_path, name):
    import sqlalchemy as sa

    path = tmp_path / name
    url = _url_or_skip(path)
    assert sa.engine.make_url(url).database == str(path)
    assert paths.sqlite_database_path(url) == str(path)


@pytest.mark.parametrize("name", _AWKWARD_NAMES)
def test_engine_from_sqlite_url_opens_exactly_that_file(tmp_path, name):
    import sqlalchemy as sa

    path = tmp_path / name
    engine = sa.create_engine(_url_or_skip(path))
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE t (x INTEGER)"))
    finally:
        engine.dispose()
    assert [p.name for p in tmp_path.iterdir() if p.name != "home"] == [name]


@pytest.mark.parametrize("url", [
    "sqlite://", "sqlite:///:memory:", "postgresql://localhost:5432/dct", "", "not a url",
])
def test_sqlite_database_path_is_none_without_a_file(url):
    assert paths.sqlite_database_path(url) is None


# --- Windows branch delegates to platformdirs (seam test) ---

def test_windows_branch_delegates_to_platformdirs(monkeypatch):
    captured = {}

    class FakePlatformDirs:
        def __init__(self, appname, appauthor=None, roaming=None):
            captured["args"] = (appname, appauthor, roaming)
            self.user_data_dir = r"C:\Users\u\AppData\Local\dctools"
            self.user_cache_dir = r"C:\Users\u\AppData\Local\dctools\Cache"

    fake = types.ModuleType("platformdirs")
    fake.PlatformDirs = FakePlatformDirs
    monkeypatch.setitem(sys.modules, "platformdirs", fake)
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)

    result = paths.config_dir()

    assert captured["args"] == ("dctools", False, False)
    assert result == Path(r"C:\Users\u\AppData\Local\dctools")


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_is_legacy_resolves_symlinked_home(tmp_path, monkeypatch):
    """P2-1: canonical path vs symlinked HOME (macOS /var → /private/var
    classic) must still be recognized as legacy."""
    real = tmp_path / "realhome"
    real.mkdir()
    link = tmp_path / "homelink"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))
    (real / ".claude").mkdir()
    assert paths.is_legacy(real / ".claude" / "dct.db")
