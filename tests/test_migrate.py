"""Tests for dct migrate-home (dct/migrate.py — ADR #52, CP #1234).

Moves a legacy ~/.claude installation (config, sqlite db + sidecars, update
cache, web pid/log) to the platform-neutral locations. Idempotent, dry-run
capable, never overwrites an existing target, leaves a breadcrumb in
~/.claude pointing at the new home.
"""

import pytest

from dct import migrate, paths
from dct.config import load_config


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
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
    return home


def _legacy_install(home, url=None, with_db=False, with_cache=False, with_web=False):
    legacy = home / ".claude"
    legacy.mkdir(parents=True, exist_ok=True)
    db_path = legacy / "dct.db"
    if url is None:
        url = f"sqlite:///{db_path}"
    (legacy / "dct.toml").write_text(f'[database]\nurl = "{url}"\n')
    if with_db:
        db_path.write_text("sqlite-bytes")
    if with_cache:
        (legacy / "dct-update-check.json").write_text("{}")
    if with_web:
        (legacy / "dct-web.pid").write_text("4242\nhttp://localhost:8787\n")
        (legacy / "dct-web.log").write_text("log line\n")
    return legacy


# ── planner ──────────────────────────────────────────────────────────────────


def test_plan_empty_on_fresh_system(fake_home):
    assert migrate.plan_migration() == []


def test_plan_moves_config_only_for_pg_install(fake_home):
    _legacy_install(fake_home, url="postgresql://localhost:5432/dct")
    steps = migrate.plan_migration()
    assert [s.kind for s in steps] == ["config"]
    assert steps[0].src == fake_home / ".claude" / "dct.toml"
    assert steps[0].dst == paths.config_dir() / "dct.toml"


def test_plan_includes_sqlite_db_when_it_lives_in_legacy(fake_home):
    _legacy_install(fake_home, with_db=True)
    kinds = [s.kind for s in migrate.plan_migration()]
    assert kinds == ["config", "database"]


def test_plan_includes_cache_and_web_state(fake_home):
    _legacy_install(fake_home, url="postgresql://h/db", with_cache=True, with_web=True)
    kinds = sorted(s.kind for s in migrate.plan_migration())
    assert kinds == ["cache", "config", "state", "state"]


def test_plan_skips_config_when_target_exists(fake_home):
    """Never overwrite: a new-location config wins, legacy is left untouched."""
    _legacy_install(fake_home, url="postgresql://h/db")
    new = paths.config_dir() / "dct.toml"
    new.parent.mkdir(parents=True)
    new.write_text('[database]\nurl = "postgresql://new/db"\n')
    assert migrate.plan_migration() == []


# ── executor ─────────────────────────────────────────────────────────────────


def test_execute_moves_config_db_and_rewrites_url(fake_home):
    _legacy_install(fake_home, with_db=True)
    messages = migrate.execute_migration(migrate.plan_migration())
    new_config = paths.config_dir() / "dct.toml"
    new_db = paths.data_dir() / "dct.db"
    assert new_config.exists() and new_db.exists()
    assert new_db.read_text() == "sqlite-bytes"
    assert not (fake_home / ".claude" / "dct.toml").exists()
    assert not (fake_home / ".claude" / "dct.db").exists()
    assert load_config().database_url == paths.sqlite_url(new_db)
    assert any("dct.db" in m for m in messages)


def test_execute_moves_db_sidecars(fake_home):
    legacy = _legacy_install(fake_home, with_db=True)
    (legacy / "dct.db-wal").write_text("wal")
    (legacy / "dct.db-shm").write_text("shm")
    migrate.execute_migration(migrate.plan_migration())
    assert (paths.data_dir() / "dct.db-wal").read_text() == "wal"
    assert (paths.data_dir() / "dct.db-shm").read_text() == "shm"
    assert not (legacy / "dct.db-wal").exists()


def test_execute_writes_breadcrumb(fake_home):
    _legacy_install(fake_home, url="postgresql://h/db")
    migrate.execute_migration(migrate.plan_migration())
    crumb = fake_home / ".claude" / "dct.toml.migrated"
    assert crumb.exists()
    assert str(paths.config_dir() / "dct.toml") in crumb.read_text()


def test_execute_is_idempotent(fake_home):
    _legacy_install(fake_home, with_db=True, with_cache=True, with_web=True)
    migrate.execute_migration(migrate.plan_migration())
    assert migrate.plan_migration() == []  # second run: nothing left to do
    migrate.execute_migration([])  # and executing the empty plan is a no-op


def test_execute_moves_cache_and_web_state(fake_home):
    legacy = _legacy_install(
        fake_home, url="postgresql://h/db", with_cache=True, with_web=True
    )
    migrate.execute_migration(migrate.plan_migration())
    assert (paths.cache_dir() / "dct-update-check.json").exists()
    assert (paths.state_dir() / "dct-web.pid").exists()
    assert (paths.state_dir() / "dct-web.log").exists()
    assert not (legacy / "dct-update-check.json").exists()


def test_pg_url_left_untouched(fake_home):
    """A postgres [database].url must survive the config move byte-identical."""
    _legacy_install(fake_home, url="postgresql://user:pass@host:5432/dct")
    migrate.execute_migration(migrate.plan_migration())
    assert load_config().database_url == "postgresql://user:pass@host:5432/dct"


def test_cli_dry_run_changes_nothing(fake_home, capsys):
    import argparse

    from dct import cli

    _legacy_install(fake_home, with_db=True)
    cli.cmd_migrate_home(argparse.Namespace(dry_run=True))
    out = capsys.readouterr().out
    assert "would move" in out
    assert (fake_home / ".claude" / "dct.toml").exists()  # untouched
    assert not (paths.config_dir() / "dct.toml").exists()
