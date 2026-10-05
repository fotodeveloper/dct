"""Shared validators for project-scoped ledger rows (K-48).

One public home for checks several core modules apply to the rows they write
or reference — sprint membership, selective changelog release and (next)
changelog item links all validate ids the same way.
"""

from __future__ import annotations

from collections.abc import Collection

import sqlalchemy as sa


def validate_project_rows(
    conn: sa.Connection,
    table: sa.Table,
    project_id: int,
    ids: Collection[int],
    noun: str,
    consequence: str = "nothing changed.",
) -> set[int]:
    """STRICT id validation for any project-scoped ledger table (K-8, #413).

    Return the set of ids that are live rows of `table` in this project; raise
    ValueError "Unknown <noun> id(s) for this project: [...] — <consequence>"
    listing any that are not (foreign, unknown, soft-deleted). `table` must
    carry `project_id` and `deleted_at` columns. Runs on the caller's
    connection, so the check and the write it guards share one transaction.
    Empty `ids` → empty set, no query.
    """
    if not ids:
        return set()
    valid = set(
        conn.execute(
            sa.select(table.c.id).where(
                table.c.project_id == project_id,
                table.c.id.in_(list(ids)),
                table.c.deleted_at.is_(None),
            )
        ).scalars()
    )
    unknown = set(ids) - valid
    if unknown:
        raise ValueError(
            f"Unknown {noun} id(s) for this project: {sorted(unknown)} — {consequence}"
        )
    return valid
