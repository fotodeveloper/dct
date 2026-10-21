"""Tests for configuration save/load roundtrip and path-resolution chain."""

import tomllib

import pytest

import dct.config as config_module
from dct import paths
from dct.config import Config, load_config, save_config


@pytest.fixture(autouse=True)
def _no_env_database_override(monkeypatch):
    """These tests assert what the FILE says; the suite-wide throwaway
    DCT_DATABASE_URL (conftest._suite_isolation) would win over it."""
    monkeypatch.delenv("DCT_DATABASE_URL", raising=False)


def test_save_load_roundtrip(tmp_path):
    config_path = tmp_path / "dct.toml"
    original = Config(database_url="postgresql://localhost:5432/dct")
    save_config(original, config_path)
    loaded = load_config(config_path)
    assert loaded.database_url == original.database_url
    # dct_root is retired (ADR #52): never persisted, auto-detected on load.
    assert loaded.dct_root != ""


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
    """Verify the TOML file has the canonical post-ADR-#52 section order —
    and that the retired dct_root never reappears."""
    config_path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///x.db", dct_root="/my/path"), config_path)
    content = config_path.read_text()
    assert "dct_root" not in content
    assert (
        content.index("[database]")
        < content.index("[plans]")
        < content.index("[classification]")
        < content.index("[integrations.claude]")
        < content.index("[web]")
        < content.index("[update]")
    )


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


# ── ADR #52 key renames — one-shot 1.2.0 window (CP #1259) ────────────────────


def test_new_sections_roundtrip(tmp_path):
    config_path = tmp_path / "dct.toml"
    cfg = Config(database_url="sqlite:///x.db")
    cfg.integrations.claude.task_mirror = "ask"
    cfg.classification.model = "custom-model"
    save_config(cfg, config_path)
    text = config_path.read_text()
    assert "[integrations.claude]" in text
    assert "[classification]" in text
    assert "task_to_dct" not in text          # legacy keys never written…
    assert "classify_model" not in text
    assert "dct_root" not in text             # …and dct_root is retired
    loaded = load_config(config_path)
    assert loaded.integrations.claude.task_mirror == "ask"
    assert loaded.classification.model == "custom-model"


def test_legacy_keys_alias_into_new_shape(tmp_path):
    """A pre-1.2 file keeps working: [mirror].task_to_dct and the
    plans.classify_* keys read as permanent aliases."""
    config_path = tmp_path / "dct.toml"
    config_path.write_text(
        '[database]\nurl = "sqlite:///x.db"\n'
        '[mirror]\ntask_to_dct = "off"\n'
        '[plans]\nclassify_model = "legacy-model"\nclassify_timeout_seconds = 7\n'
    )
    cfg = load_config(config_path)
    assert cfg.integrations.claude.task_mirror == "off"
    assert cfg.mirror.task_to_dct == "off"    # compat view: `dct config get`
    assert cfg.classification.model == "legacy-model"
    assert cfg.classification.timeout_seconds == 7


def test_new_keys_beat_legacy_aliases(tmp_path):
    config_path = tmp_path / "dct.toml"
    config_path.write_text(
        '[database]\nurl = "sqlite:///x.db"\n'
        '[mirror]\ntask_to_dct = "off"\n'
        '[integrations.claude]\ntask_mirror = "ask"\n'
        '[plans]\nclassify_model = "old"\n'
        '[classification]\nmodel = "new"\n'
    )
    cfg = load_config(config_path)
    assert cfg.integrations.claude.task_mirror == "ask"
    assert cfg.classification.model == "new"


def test_dct_root_read_but_never_written(tmp_path):
    config_path = tmp_path / "dct.toml"
    config_path.write_text('dct_root = "/legacy/checkout"\n[database]\nurl = "sqlite:///x.db"\n')
    cfg = load_config(config_path)
    assert cfg.dct_root == "/legacy/checkout"  # old files still load
    out = tmp_path / "out.toml"
    save_config(cfg, out)
    assert "dct_root" not in out.read_text()   # retired from the write path


def test_dead_plans_keys_are_gone(tmp_path):
    """surface_on_session_start and discovery_include/exclude had ZERO
    consumers (round-1 audit) — deleted, not aliased forward."""
    config_path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///x.db"), config_path)
    text = config_path.read_text()
    for dead in ("surface_on_session_start", "discovery_include", "discovery_exclude"):
        assert dead not in text
    cfg = load_config(config_path)
    assert not hasattr(cfg.plans, "surface_on_session_start")
    assert not hasattr(cfg.plans, "discovery_include")


def test_classification_defaults():
    cfg = Config(database_url="sqlite:///x.db")
    assert cfg.classification.provider == "rules"
    assert cfg.classification.command == ["claude", "--print"]
    assert cfg.integrations.claude.task_mirror == "auto"


# ── dct config path (CP #1258) ────────────────────────────────────────────────


def test_cmd_config_path_prints_resolved_locations(fake_home, capsys):
    import argparse

    from dct import cli

    _write_config(fake_home / ".claude" / "dct.toml", "sqlite:///x.db")
    cli.cmd_config_path(argparse.Namespace())
    out = capsys.readouterr().out
    assert str(fake_home / ".claude" / "dct.toml") in out   # resolved config
    assert "legacy" in out                                   # …with its source
    assert str(paths.data_dir()) in out or str(fake_home / ".claude" / "dct.db") in out
    assert str(paths.cache_dir()) in out
    assert str(paths.state_dir()) in out


@pytest.mark.parametrize('section,expected', [
    ('', 'rules'),
    ('[classification]\ncommand = ["custom", "--classify"]\n', 'command'),
    ('[plans]\nclassify_model = "legacy"\n', 'command'),
    ('[classification]\nprovider = "rules"\ncommand = ["custom"]\n', 'rules'),
    ('[classification]\nprovider = "disabled"\nmodel = "legacy"\n', 'disabled'),
])
def test_classification_explicit_provider_and_legacy_aliases(tmp_path, section, expected):
    path = tmp_path / 'dct.toml'
    path.write_text('[database]\nurl = "sqlite:///x.db"\n' + section)
    assert load_config(path).classification.provider == expected


def test_classification_rejects_string_argv(tmp_path):
    path = tmp_path / 'dct.toml'
    path.write_text('[classification]\ncommand = "custom --classify"\n')
    with pytest.raises(ValueError, match='array of strings'):
        load_config(path)


# ── [triage] template (#930 §8 K-1) ──────────────────────────────────────────


def test_triage_template_defaults_to_unset_and_writes_no_section(tmp_path):
    path = tmp_path / "dct.toml"
    cfg = Config(database_url="sqlite:///x.db")
    assert cfg.triage.template is None
    save_config(cfg, path)
    assert "[triage]" not in path.read_text()
    assert load_config(path).triage.template is None


@pytest.mark.parametrize("template", [
    "/tmp/proj-a/triage.html",
    "~/templates/triage page.html",
    'C:\\Work\\triage "v2".html',  # backslashes and quotes survive the TOML
    "/tmp/szablon-żółw.html",
])
def test_triage_template_round_trips(tmp_path, template):
    path = tmp_path / "dct.toml"
    cfg = Config(database_url="sqlite:///x.db")
    cfg.triage.template = template
    save_config(cfg, path)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["triage"] == {"template": template}
    loaded = load_config(path)
    assert loaded.triage.template == template
    # a rewrite (any other command saving the config) keeps it
    save_config(loaded, path)
    assert load_config(path).triage.template == template


def test_triage_template_section_comes_last_and_other_sections_survive(tmp_path):
    path = tmp_path / "dct.toml"
    cfg = Config(database_url="sqlite:///x.db")
    cfg.triage.template = "/tmp/t.html"
    cfg.web.port = 9999
    save_config(cfg, path)
    content = path.read_text()
    assert content.index("[update]") < content.index("[triage]")
    assert load_config(path).web.port == 9999


def test_an_empty_triage_template_reads_as_unset(tmp_path):
    path = tmp_path / "dct.toml"
    path.write_text('[triage]\ntemplate = ""\n')
    assert load_config(path).triage.template is None


@pytest.mark.parametrize("body", ['[triage]\ntemplate = 5\n', '[triage]\ntemplate = ["a"]\n',
                                  'triage = "x"\n'])
def test_a_malformed_triage_section_is_refused(tmp_path, body):
    path = tmp_path / "dct.toml"
    path.write_text(body)
    with pytest.raises(ValueError, match="triage"):
        load_config(path)


# ── [triage] language (#991 R-5) ─────────────────────────────────────────────


def test_triage_language_defaults_to_unset(tmp_path):
    path = tmp_path / "dct.toml"
    save_config(Config(database_url="sqlite:///x.db"), path)
    assert load_config(path).triage.language is None


def test_triage_language_alone_survives_repeated_saves_and_other_changes(tmp_path):
    """The section is written for the language even without a template, and a
    rewrite by any other command keeps it."""
    path = tmp_path / "dct.toml"
    cfg = Config(database_url="sqlite:///x.db")
    cfg.triage.language = "pl"
    save_config(cfg, path)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["triage"] == {"language": "pl"}
    for port in (9001, 9002):
        loaded = load_config(path)
        loaded.web.port = port
        save_config(loaded, path)
    final = load_config(path)
    assert (final.triage.language, final.triage.template, final.web.port) == ("pl", None, 9002)


def test_triage_template_and_language_round_trip_together(tmp_path):
    path = tmp_path / "dct.toml"
    cfg = Config(database_url="sqlite:///x.db")
    cfg.triage.template, cfg.triage.language = "/tmp/t.html", "en"
    save_config(cfg, path)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["triage"] == {
        "template": "/tmp/t.html", "language": "en"}
    loaded = load_config(path)
    assert (loaded.triage.template, loaded.triage.language) == ("/tmp/t.html", "en")


@pytest.mark.parametrize("value", ['"xx"', "5", '""'])
def test_an_unknown_triage_language_is_refused_naming_the_shipped_ones(tmp_path, value):
    path = tmp_path / "dct.toml"
    path.write_text(f"[triage]\nlanguage = {value}\n")
    with pytest.raises(ValueError, match=r"triage\.language: unknown language .*en, pl"):
        load_config(path)


def test_a_triage_language_is_normalized_on_load(tmp_path):
    path = tmp_path / "dct.toml"
    path.write_text('[triage]\nlanguage = " PL "\n')
    assert load_config(path).triage.language == "pl"
