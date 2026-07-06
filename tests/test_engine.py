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
