"""Tests for configuration save/load roundtrip and path-resolution chain."""

import tomllib

import pytest

import dct.config as config_module
from dct import paths
from dct.config import Config, save_config, load_config


def test_save_load_roundtrip(tmp_path):
    config_path = tmp_path / "dct.toml"
    original = Config(database_url="postgresql://localhost:5432/dct", dct_root="/tmp/dct-test")
    save_config(original, config_path)
    loaded = load_config(config_path)
    assert loaded.database_url == original.database_url
    assert loaded.dct_root == original.dct_root


def test_save_load_sqlite_default(tmp_path):
    config_path = tmp_path / "dct.toml"
    original = Config(database_url="sqlite:///test.db")
    save_config(original, config_path)
    loaded = load_config(config_path)
    assert loaded.database_url == "sqlite:///test.db"


def test_save_load_no_dct_root(tmp_path):
    """dct_root should fallback to detected path when not in config."""
    config_path = tmp_path / "dct.toml"
    original = Config(database_url="sqlite:///test.db", dct_root="")
    save_config(original, config_path)
    loaded = load_config(config_path)
    # dct_root should be auto-detected, not empty
    assert loaded.dct_root != ""


def test_missing_config_file_uses_defaults(tmp_path):
    config_path = tmp_path / "nonexistent.toml"
    config = load_config(config_path)
    assert "sqlite" in config.database_url
    assert config.dct_root != ""


def test_env_var_overrides_config(tmp_path, monkeypatch):
    config_path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///file.db"), config_path)
    monkeypatch.setenv("DCT_DATABASE_URL", "postgresql://override:5432/dct")
    loaded = load_config(config_path)
    assert loaded.database_url == "postgresql://override:5432/dct"


def test_toml_structure(tmp_path):
    """Verify the TOML file has correct section structure."""
    config_path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///x.db", dct_root="/my/path"), config_path)
    content = config_path.read_text()
    # dct_root should be before [database] section (top-level key)
    assert content.index("dct_root") < content.index("[database]")


# ── Path-resolution chain (ADR #52: platformdirs move, legacy fallback) ──────


@pytest.fixture
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
    monkeypatch.setattr(config_module, "_legacy_notice_emitted", False, raising=False)
    return home


def _write_config(path, url):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[database]\nurl = "{url}"\n')
    return path


def test_load_config_falls_back_to_legacy_file(fake_home):
    _write_config(fake_home / ".claude" / "dct.toml", "postgresql://legacy:5432/dct")
    cfg = load_config()
    assert cfg.database_url == "postgresql://legacy:5432/dct"


def test_load_config_prefers_new_over_legacy(fake_home):
    _write_config(fake_home / ".claude" / "dct.toml", "postgresql://legacy:5432/dct")
    _write_config(paths.config_dir() / "dct.toml", "postgresql://new:5432/dct")
    cfg = load_config()
    assert cfg.database_url == "postgresql://new:5432/dct"


def test_legacy_config_emits_migrate_notice_once(fake_home, capsys, monkeypatch):
    monkeypatch.setattr(config_module, "_stderr_is_tty", lambda: True)
    _write_config(fake_home / ".claude" / "dct.toml", "sqlite:///x.db")
    load_config()
    load_config()
    err = capsys.readouterr().err
    assert err.count("dct migrate-home") == 1


def test_legacy_notice_silent_without_tty(fake_home, capsys, monkeypatch):
    """Hooks and the MCP server (no tty) must not be spammed — the notice
    targets a human running the CLI, once per interactive process."""
    monkeypatch.setattr(config_module, "_stderr_is_tty", lambda: False)
    _write_config(fake_home / ".claude" / "dct.toml", "sqlite:///x.db")
    load_config()
    assert "migrate-home" not in capsys.readouterr().err


def test_no_notice_for_new_location(fake_home, capsys):
    _write_config(paths.config_dir() / "dct.toml", "sqlite:///x.db")
    load_config()
    assert "migrate-home" not in capsys.readouterr().err


def test_no_notice_for_explicit_config_path(fake_home, capsys):
    """An explicitly passed path is the caller's choice — never warn."""
    legacy = _write_config(fake_home / ".claude" / "dct.toml", "sqlite:///x.db")
    load_config(legacy)
    assert "migrate-home" not in capsys.readouterr().err


def test_fresh_install_defaults_db_to_data_dir(fake_home):
    cfg = Config()
    assert cfg.database_url == paths.sqlite_url(paths.data_dir() / "dct.db")


def test_existing_legacy_db_stays_default(fake_home):
    """Non-breaking: an install with only ~/.claude/dct.db keeps hitting it."""
    legacy_db = fake_home / ".claude" / "dct.db"
    legacy_db.parent.mkdir(parents=True)
    legacy_db.write_text("")
    cfg = Config()
    assert cfg.database_url == paths.sqlite_url(legacy_db)


def test_save_config_default_targets_legacy_when_only_legacy(fake_home):
    """No split-brain: a legacy-only install keeps saving where it loads from."""
    legacy = _write_config(fake_home / ".claude" / "dct.toml", "sqlite:///x.db")
    cfg = load_config()
    cfg.web.port = 9999
    written = save_config(cfg)
    assert written == legacy
    assert "9999" in legacy.read_text()


def test_save_config_default_targets_new_on_fresh_install(fake_home):
    written = save_config(Config(database_url="sqlite:///x.db"))
    assert written == paths.config_dir() / "dct.toml"


# ── save_config Windows correctness (W1: TOML escaping, W2: utf-8) ───────────


def test_save_config_roundtrips_backslashes(tmp_path):
    r"""W1: a C:\-style value must survive save→tomllib→load byte-identical
    (today it either raises TOMLDecodeError or silently corrupts \n, \t)."""
    config_path = tmp_path / "dct.toml"
    url = r"sqlite:///C:\new\table\dct.db"
    save_config(Config(database_url=url, dct_root=r"C:\Users\alice\dev\dct"), config_path)
    with open(config_path, "rb") as f:
        parsed = tomllib.load(f)
    assert parsed["database"]["url"] == url
    assert load_config(config_path).database_url == url


def test_save_config_escapes_quotes(tmp_path):
    config_path = tmp_path / "dct.toml"
    url = 'postgresql://u:pa"ss@host:5432/dct'
    save_config(Config(database_url=url), config_path)
    assert load_config(config_path).database_url == url


def test_save_config_writes_utf8(tmp_path):
    """W2: config bytes must be UTF-8 regardless of locale (TOML mandates it)."""
    config_path = tmp_path / "dct.toml"
    url = "postgresql://michał@host:5432/dct"
    save_config(Config(database_url=url), config_path)
    assert "michał" in config_path.read_bytes().decode("utf-8")
    assert load_config(config_path).database_url == url
