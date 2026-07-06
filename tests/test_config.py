"""Tests for configuration save/load roundtrip."""


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
