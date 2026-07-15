"""Tests for dct.core.engine.bootstrap_engine — the DRY config→engine helper."""

import sqlalchemy as sa

from dct.config import Config
import dct.core.engine as engine_mod
from dct.core.engine import bootstrap_engine


def test_bootstrap_engine_create_true_makes_tables(tmp_path, monkeypatch):
    """create=True must run DDL so a fresh sqlite URL ends up with dct tables."""
    db_file = tmp_path / "fresh.db"
    cfg = Config(database_url=f"sqlite:///{db_file}")
    monkeypatch.setattr(engine_mod, "load_config", lambda: cfg)

    engine = bootstrap_engine(create=True)
    assert isinstance(engine, sa.Engine)

    insp = sa.inspect(engine)
    names = set(insp.get_table_names())
    # Core tables must exist after a create=True bootstrap.
    assert "projects" in names
    assert "items" in names


def test_bootstrap_engine_create_true_provisions_parent_dirs(tmp_path, monkeypatch):
    """First run on a pristine HOME (#360): the default sqlite path's parent
    directory does not exist yet — create=True must provision it instead of
    crashing with a raw 'unable to open database file' OperationalError."""
    db_file = tmp_path / "not" / "yet" / "there" / "dct.db"
    cfg = Config(database_url=f"sqlite:///{db_file}")
    monkeypatch.setattr(engine_mod, "load_config", lambda: cfg)

    engine = bootstrap_engine(create=True)

    insp = sa.inspect(engine)
    assert "projects" in set(insp.get_table_names())
    assert db_file.parent.is_dir()


def test_bootstrap_engine_memory_urls_skip_dir_provisioning(monkeypatch):
    """Memory sqlite URLs have no file path — provisioning must not crash on
    them ('sqlite://' has no database, 'sqlite:///:memory:' a pseudo-path)."""
    for url in ("sqlite://", "sqlite:///:memory:"):
        cfg = Config(database_url=url)
        monkeypatch.setattr(engine_mod, "load_config", lambda cfg=cfg: cfg)
        engine = bootstrap_engine(create=True)
        insp = sa.inspect(engine)
        assert "projects" in set(insp.get_table_names())


def test_bootstrap_engine_create_false_runs_no_ddl(tmp_path, monkeypatch):
    """create=False must return a usable Engine WITHOUT invoking create_tables.

    v1 web viewer is read-only and must never issue DDL. We assert the helper
    never calls create_tables, and that no tables get materialised on a fresh DB.
    """
    db_file = tmp_path / "untouched.db"
    cfg = Config(database_url=f"sqlite:///{db_file}")
    monkeypatch.setattr(engine_mod, "load_config", lambda: cfg)

    calls = {"n": 0}

    def _spy_create_tables(engine):
        calls["n"] += 1

    monkeypatch.setattr(engine_mod, "create_tables", _spy_create_tables)

    engine = bootstrap_engine(create=False)
    assert isinstance(engine, sa.Engine)
    assert calls["n"] == 0  # NO DDL when create=False

    # And the fresh DB really has no dct tables.
    insp = sa.inspect(engine)
    assert "projects" not in set(insp.get_table_names())


def test_bootstrap_engine_create_false_provisions_nothing(tmp_path, monkeypatch):
    """create=False must not even create directories — the read-only web viewer
    relies on bootstrap_engine(create=False) never mutating the filesystem."""
    db_file = tmp_path / "ro" / "dct.db"
    cfg = Config(database_url=f"sqlite:///{db_file}")
    monkeypatch.setattr(engine_mod, "load_config", lambda: cfg)

    bootstrap_engine(create=False)
    assert not (tmp_path / "ro").exists()
