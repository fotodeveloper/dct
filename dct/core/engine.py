"""Engine bootstrap — the single config→engine→(optional)DDL helper.

DRY extraction of the duplicated `load_config() -> get_engine() -> create_tables()`
block that previously lived in both dct/server.py and dct/cli.py.

`create=False` returns a usable Engine WITHOUT running any DDL. The v1 web
viewer is strictly read-only and relies on this to guarantee it never mutates
the schema (no create_tables, only SELECTs).
"""

import sqlalchemy as sa

from dct.config import load_config
from dct.db import create_tables, ensure_sqlite_dir, get_engine


def bootstrap_engine(create: bool = True) -> sa.Engine:
    """Build a SQLAlchemy Engine from dct config.

    Args:
        create: when True (default) provision the parent directory for
            file-backed sqlite URLs (first run on a pristine machine, #360)
            and run idempotent DDL via create_tables so the schema exists.
            When False, skip ALL provisioning — no DDL, no directory creation;
            caller guarantees the DB already exists (read-only consumers such
            as the web viewer).

    Returns:
        A configured sqlalchemy.Engine.
    """
    cfg = load_config()
    engine = get_engine(cfg.database_url)
    if create:
        ensure_sqlite_dir(cfg.database_url)
        create_tables(engine)  # idempotent
    return engine
