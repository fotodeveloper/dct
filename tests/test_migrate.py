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


def test_execute_consolidates_db_sidecars(fake_home):
    """Sidecars are consolidated into the snapshot, never copied: the target
    is a standalone db, and the legacy WAL/SHM files are removed with it."""
    legacy = _legacy_install(fake_home, with_db=True)
    (legacy / "dct.db-wal").write_text("wal")
    (legacy / "dct.db-shm").write_text("shm")
    migrate.execute_migration(migrate.plan_migration())
    assert (paths.data_dir() / "dct.db").exists()
    assert not (paths.data_dir() / "dct.db-wal").exists()
    assert not (paths.data_dir() / "dct.db-shm").exists()
    assert not (legacy / "dct.db-wal").exists()
    assert not (legacy / "dct.db-shm").exists()


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


# ── Codex re-review findings (commit d4f3c4c) — round-2 regression scenarios ─


def test_rewrite_limited_to_database_section(fake_home):
    """R2-P1: an identical url value in ANOTHER section (e.g. [backup]) must
    not swallow the rewrite — only [database].url changes."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("sqlite-bytes")
    old = f"sqlite:///{db}"
    (legacy / "dct.toml").write_text(
        f'[backup]\nurl = "{old}"\n\n[database]\nurl = "{old}"\n'
    )
    migrate.execute_migration(migrate.plan_migration())
    content = (paths.config_dir() / "dct.toml").read_text()
    new_url = paths.sqlite_url(paths.data_dir() / "dct.db")
    assert f'[backup]\nurl = "{old}"' in content          # other section intact
    assert f'[database]\nurl = "{new_url}"' in content
    assert load_config().database_url == new_url


def test_rewrite_handles_single_quoted_literal_url(fake_home):
    """R2-P2: a TOML literal string url = '…' parses fine, so the rewrite
    must handle it instead of stranding the database."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("sqlite-bytes")
    (legacy / "dct.toml").write_text(f"[database]\nurl = 'sqlite:///{db}'\n")
    migrate.execute_migration(migrate.plan_migration())
    assert load_config().database_url == paths.sqlite_url(paths.data_dir() / "dct.db")
    assert not db.exists()


def test_commit_success_survives_tmp_cleanup_failure(tmp_path, monkeypatch):
    """R2-P1: a failing unlink of the .migrating temp AFTER a successful
    os.link must still report success — else rollback deletes a database the
    already-published config points at."""
    from pathlib import Path as _P

    dst = tmp_path / "out.txt"
    tmp = tmp_path / "out.txt.migrating"
    tmp.write_text("content")
    real_unlink = _P.unlink

    def flaky_unlink(self, *a, **k):
        if self.name.endswith(".migrating"):
            raise OSError("file locked by antivirus")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(_P, "unlink", flaky_unlink)
    assert migrate._commit(tmp, dst) is True
    assert dst.read_text() == "content"


def test_parallel_migrations_are_serialized(fake_home):
    """R2-P1: a second migrate-home while one holds the lock must abort
    without touching anything."""
    import fcntl

    _legacy_install(fake_home, with_db=True)
    lock_path = paths.config_dir() / ".migrate-home.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        messages = migrate.execute_migration(migrate.plan_migration())
        assert any("another" in m for m in messages)
        assert (fake_home / ".claude" / "dct.toml").exists()   # untouched
        assert not (paths.config_dir() / "dct.toml").exists()
    finally:
        holder.close()


def test_env_pinned_config_is_never_migrated(fake_home, monkeypatch):
    """R2-P1: with DCT_CONFIG_PATH pointing at the legacy file, moving it
    would break resolution (env wins unconditionally) — the planner must not
    plan config/database steps at all."""
    legacy = _legacy_install(fake_home, with_db=True)
    monkeypatch.setenv("DCT_CONFIG_PATH", str(legacy / "dct.toml"))
    kinds = {s.kind for s in migrate.plan_migration()}
    assert "config" not in kinds and "database" not in kinds


def test_plan_resumes_cache_and_state_leftovers(fake_home):
    """R2-P2: a byte-identical cache/state target left by a crashed run must
    still be planned so the legacy source finally gets removed."""
    import shutil as _shutil

    legacy = _legacy_install(fake_home, url="postgresql://h/db", with_cache=True)
    leftover = paths.cache_dir() / "dct-update-check.json"
    leftover.parent.mkdir(parents=True)
    _shutil.copy2(legacy / "dct-update-check.json", leftover)
    migrate.execute_migration(migrate.plan_migration())
    assert not (legacy / "dct-update-check.json").exists()   # converged


def test_sqlite_snapshot_reads_through_wal(fake_home):
    """R2-P1: rows living only in an un-checkpointed WAL must reach the
    migrated database (consistent snapshot, not a raw file copy). The legacy
    db has NO open connections — the writer-quiesce lock refuses otherwise
    (see test_live_writer_blocks_db_migration)."""
    workdir = fake_home / "build"
    workdir.mkdir()
    legacy = fake_home / ".claude"
    legacy.mkdir()
    _build_wal_db(workdir, legacy)
    db = legacy / "dct.db"
    (legacy / "dct.toml").write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    migrate.execute_migration(migrate.plan_migration())

    # Sidecar check BEFORE opening the target — a read connection to a
    # WAL-mode db creates fresh -wal/-shm files itself.
    assert not (paths.data_dir() / "dct.db-wal").exists()  # consolidated
    assert _rows(paths.data_dir() / "dct.db") == {"row-1", "row-2-wal-only"}


def test_fixup_converges_stranded_db(fake_home):
    """Convergence: new config exists but its url still points at a legacy
    sqlite db (any earlier cancel path) — a re-run must finish the job."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("sqlite-bytes")
    new_cfg = paths.config_dir() / "dct.toml"
    new_cfg.parent.mkdir(parents=True)
    new_cfg.write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    kinds = [s.kind for s in migrate.plan_migration()]
    assert "database" in kinds and "config_fixup" in kinds
    migrate.execute_migration(migrate.plan_migration())

    new_db = paths.data_dir() / "dct.db"
    assert new_db.read_text() == "sqlite-bytes"
    assert not db.exists()
    assert load_config().database_url == paths.sqlite_url(new_db)


def test_fixup_replaces_stale_orphan_target(fake_home):
    """Convergence: a NON-identical dst leftover is safe to replace in fixup
    mode — the new config's url proves it never became the live database."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("current-bytes")
    stale = paths.data_dir() / "dct.db"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale-orphan-from-crashed-run")
    new_cfg = paths.config_dir() / "dct.toml"
    new_cfg.parent.mkdir(parents=True)
    new_cfg.write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    migrate.execute_migration(migrate.plan_migration())

    assert (paths.data_dir() / "dct.db").read_text() == "current-bytes"
    assert not db.exists()
    assert load_config().database_url == paths.sqlite_url(paths.data_dir() / "dct.db")


# ── Codex round-3 findings (commit 0b67d18) — db-unit integrity scenarios ────


def _real_db(path, rows):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    return con


def _rows(path):
    import sqlite3

    con = sqlite3.connect(path)
    got = {r[0] for r in con.execute("SELECT v FROM t")}
    con.close()
    return got


def _build_wal_db(workdir, legacy, checkpoint_first=False):
    """Create a legacy db with committed rows sitting ONLY in its WAL and no
    open connections — the realistic crashed-process state. Built at a
    scratch path with a live connection, copied (db + wal) to legacy, then
    the builder connection is closed (closing directly at legacy would
    checkpoint and delete the WAL)."""
    import shutil as _sh

    src = workdir / "dct.db"
    con = _real_db(src, ["row-1"])
    if checkpoint_first:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpointed_main = workdir / "main-snapshot"
        _sh.copy2(src, checkpointed_main)
    con.execute("INSERT INTO t VALUES ('row-2-wal-only')")
    con.commit()                             # lands in the WAL, main unchanged
    assert (workdir / "dct.db-wal").stat().st_size > 0
    _sh.copy2(src, legacy / "dct.db")
    _sh.copy2(workdir / "dct.db-wal", legacy / "dct.db-wal")
    con.close()
    return (workdir / "main-snapshot") if checkpoint_first else None


def test_resumed_db_with_fresh_wal_is_resnapshotted(fake_home):
    """R3-P1: dst byte-identical to the checkpointed MAIN file while newer
    committed rows sit only in the source WAL — 'resumed' must re-snapshot,
    not silently bless the stale copy before the WAL is deleted."""
    import shutil as _sh

    workdir = fake_home / "build"
    workdir.mkdir()
    legacy = fake_home / ".claude"
    legacy.mkdir()
    checkpointed_main = _build_wal_db(workdir, legacy, checkpoint_first=True)
    dst = paths.data_dir() / "dct.db"
    dst.parent.mkdir(parents=True)
    _sh.copy2(checkpointed_main, dst)        # identical-to-main leftover
    db = legacy / "dct.db"
    (legacy / "dct.toml").write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    migrate.execute_migration(migrate.plan_migration())
    assert _rows(paths.data_dir() / "dct.db") == {"row-1", "row-2-wal-only"}


def test_backup_failure_on_real_db_aborts_not_rawcopies(fake_home, monkeypatch):
    """R3-P1: an operational backup failure on a GENUINE sqlite db must abort
    the unit — a raw copy of the main file would lose WAL'd rows."""
    import sqlite3

    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    _real_db(db, ["row-1"]).close()
    (legacy / "dct.toml").write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    def boom(src_con, dst_con):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(migrate, "_run_backup", boom)
    migrate.execute_migration(migrate.plan_migration())
    assert db.exists()
    assert not (paths.data_dir() / "dct.db").exists()


def test_snapshot_preserves_source_permissions(fake_home):
    """R3-P1: a 0600 legacy database must not become 0644 under the umask."""
    import os as _os

    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    _real_db(db, ["row-1"]).close()
    _os.chmod(db, 0o600)
    (legacy / "dct.toml").write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    migrate.execute_migration(migrate.plan_migration())
    mode = (paths.data_dir() / "dct.db").stat().st_mode & 0o777
    assert mode == 0o600


def test_env_database_url_blocks_db_migration(fake_home, monkeypatch):
    """R3-P1: with DCT_DATABASE_URL set, runtime precedence makes the env
    target authoritative — the planner must never plan a database step (the
    config alone may still move; its url is inert at runtime)."""
    _legacy_install(fake_home, with_db=True)
    monkeypatch.setenv(
        "DCT_DATABASE_URL", paths.sqlite_url(paths.data_dir() / "dct.db")
    )
    kinds = [s.kind for s in migrate.plan_migration()]
    assert "config" in kinds
    assert "database" not in kinds and "config_fixup" not in kinds


def test_fixup_write_failure_keeps_staged_snapshot(fake_home, monkeypatch):
    """R3-P1: when fixup replaced a dead orphan target and the config update
    then fails, the staged snapshot must SURVIVE (no rollback-unlink of a
    file whose predecessor is unrecoverable) and legacy must stay intact."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("current-bytes")
    stale = paths.data_dir() / "dct.db"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale-orphan")
    new_cfg = paths.config_dir() / "dct.toml"
    new_cfg.parent.mkdir(parents=True)
    new_cfg.write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    monkeypatch.setattr(migrate, "_replace_text", lambda *a, **k: False)
    messages = migrate.execute_migration(migrate.plan_migration())

    assert (paths.data_dir() / "dct.db").read_text() == "current-bytes"  # staged kept
    assert db.exists()                                    # legacy untouched
    assert not any("nothing changed" in m for m in messages)


def test_live_writer_blocks_db_migration(fake_home):
    """R3-P1: an open write transaction on the source must abort the db unit
    (explicit BUSY) — never a snapshot that silently misses the commit."""
    import sqlite3

    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    _real_db(db, ["row-1"]).close()
    (legacy / "dct.toml").write_text(f'[database]\nurl = "sqlite:///{db}"\n')

    writer = sqlite3.connect(db, timeout=0.1)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES ('uncommitted')")
    try:
        messages = migrate.execute_migration(migrate.plan_migration())
        assert any("in use" in m for m in messages)
        assert db.exists()
        assert not (paths.data_dir() / "dct.db").exists()
    finally:
        writer.rollback()
        writer.close()


def test_rewrite_accepts_section_header_comment(fake_home):
    """R3-P2: '[database] # primary' is valid TOML — the rewrite must match
    it, else fixup re-plans and fails identically forever."""
    legacy = fake_home / ".claude"
    legacy.mkdir()
    db = legacy / "dct.db"
    db.write_text("sqlite-bytes")
    (legacy / "dct.toml").write_text(
        f'[database] # primary\nurl = "sqlite:///{db}"\n'
    )
    migrate.execute_migration(migrate.plan_migration())
    assert load_config().database_url == paths.sqlite_url(paths.data_dir() / "dct.db")
    assert not db.exists()


def test_config_only_migration_survives_unwritable_data_dir(fake_home):
    """R3-P2: a PG config-only migration must not require a writable data
    dir just for the lock — and lock setup failures must not crash the CLI."""
    _legacy_install(fake_home, url="postgresql://h/db")
    blocker = fake_home / ".local" / "share"
    blocker.mkdir(parents=True)
    (blocker / "dctools").write_text("a file where the data DIR should be")
    migrate.execute_migration(migrate.plan_migration())
    assert (paths.config_dir() / "dct.toml").exists()   # config still migrated
