"""Column-length guard shared by every core writer (#792).

Policy: a value longer than its VARCHAR(N) column is REFUSED with a readable
ValueError, never truncated. PostgreSQL (the primary backend) would raise
StringDataRightTruncation and SQLite would silently store it; checking here,
before any statement runs, makes the contract dialect-independent and keeps
driver text out of MCP errors. Limits are always read from the schema
(`column.type.length`), never typed in, so widening a column moves the guard.
"""

from collections.abc import Mapping

import sqlalchemy as sa


def ensure_fits(
    table: sa.Table,
    values: Mapping[str, object],
    labels: Mapping[str, str] | None = None,
) -> None:
    """Raise ValueError if a str in `values` exceeds its String(N) column.

    Keys that are not columns of `table`, columns without a String length, and
    None / non-str values are ignored. Length is counted in characters (as
    PostgreSQL's VARCHAR does), not bytes. `labels` maps a key to the name used
    in the message (default: the key itself).
    """
    for key, value in values.items():
        if not isinstance(value, str) or key not in table.c:
            continue
        col_type = table.c[key].type
        limit = col_type.length if isinstance(col_type, sa.String) else None
        if limit is None or len(value) <= limit:
            continue
        label = (labels or {}).get(key, key)
        raise ValueError(
            f"{label} too long: {len(value)} chars, max {limit} — refused, never truncated"
        )
