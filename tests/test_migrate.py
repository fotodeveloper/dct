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


# ── Codex review findings (commit b6efa5c) — regression scenarios ────────────


def test_rewrite_anchored_to_url_assignment(fake_home):
    """P1-1: a quoted copy of the URL earlier in the file (e.g. a comment)
    must NOT swallow the replacement — only the url = assignment rewrites."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("sqlite-bytes")
    (legacy / "dct.toml").write_text(
        f'# backup of old url = "sqlite:///{db}"\n[database]\nurl = "sqlite:///{db}"\n'
    )
    migrate.execute_migration(migrate.plan_migration())
    content = (paths.config_dir() / "dct.toml").read_text()
    assert f'# backup of old url = "sqlite:///{db}"' in content  # comment intact
    new_url = paths.sqlite_url(paths.data_dir() / "dct.db")
    assert f'url = "{new_url}"' in content
    assert load_config().database_url == new_url


def test_db_copy_failure_cancels_unit_keeps_state_consistent(fake_home, monkeypatch):
    """P1-2: a failure while copying the db must cancel the unit — the config
    may still migrate but with the URL unrewritten, pointing at the intact
    legacy db. No state may reference a file that does not exist."""
    _legacy_install(fake_home, with_db=True)
    monkeypatch.setattr(
        migrate.shutil, "copy2",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    migrate.execute_migration(migrate.plan_migration())
    legacy_db = fake_home / ".claude" / "dct.db"
    assert legacy_db.exists()                       # db untouched
    assert not (paths.data_dir() / "dct.db").exists()
    cfg = load_config()
    assert cfg.database_url == paths.sqlite_url(legacy_db)  # url still legacy


def test_sidecar_conflict_cancels_whole_db_unit(fake_home):
    """P1-3: a conflicting destination sidecar must cancel the WHOLE db unit
    (WAL may hold committed transactions) — nothing moves, target untouched."""
    legacy = _legacy_install(fake_home, with_db=True)
    (legacy / "dct.db-wal").write_text("wal-with-committed-transactions")
    conflicting = paths.data_dir() / "dct.db-wal"
    conflicting.parent.mkdir(parents=True)
    conflicting.write_text("stale-target")
    migrate.execute_migration(migrate.plan_migration())
    assert (legacy / "dct.db").exists()
    assert (legacy / "dct.db-wal").read_text() == "wal-with-committed-transactions"
    assert not (paths.data_dir() / "dct.db").exists()
    assert conflicting.read_text() == "stale-target"
    assert load_config().database_url == paths.sqlite_url(legacy / "dct.db")


def test_no_clobber_survives_target_appearing_after_planning(fake_home):
    """P1-4: a target created between plan and execute must never be
    truncated or replaced (atomic hard-link commit, not check-then-write)."""
    _legacy_install(fake_home, url="postgresql://h/db")
    steps = migrate.plan_migration()
    victim = paths.config_dir() / "dct.toml"
    victim.parent.mkdir(parents=True)
    victim.write_text("someone else's file")
    migrate.execute_migration(steps)
    assert victim.read_text() == "someone else's file"
    assert (fake_home / ".claude" / "dct.toml").exists()  # source preserved


def test_rerun_after_partial_stage_converges(fake_home):
    """P1-2 rerun-recovery: a byte-identical leftover copy from a crashed run
    must be treated as already-staged — the rerun completes the migration."""
    import shutil as _shutil

    legacy = _legacy_install(fake_home, with_db=True)
    leftover = paths.data_dir() / "dct.db"
    leftover.parent.mkdir(parents=True)
    _shutil.copy2(legacy / "dct.db", leftover)
    migrate.execute_migration(migrate.plan_migration())
    assert load_config().database_url == paths.sqlite_url(leftover)
    assert not (legacy / "dct.db").exists()      # converged: legacy cleaned
    assert not (legacy / "dct.toml").exists()
