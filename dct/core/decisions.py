"""Decision CRUD — ADRs, sprint amendments, inline plan decisions, open questions.

A decision is a first-class record of an architectural / scope choice with
its own lifecycle (proposed → accepted → superseded/rejected). It can be
standalone (classic ADR file in docs/adr/) or anchored to a plan, a plan
section, or a sprint — or several at once (multi-anchor via optional FKs).

Spec §4.1 + §5.1 + §9.5.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from dct.core.ids import new_ulid
from dct.core.projects import resolve_project_id
from dct.db import decisions, row_to_dict

VALID_KINDS = {"adr", "sprint_amendment", "inline_plan", "open_question"}
VALID_STATUSES = {"proposed", "accepted", "superseded", "rejected"}


def _validate(value: str, valid: set[str], field: str) -> str:
    if value not in valid:
        raise ValueError(f"Invalid {field}: '{value}'. Valid: {', '.join(sorted(valid))}")
    return value


def create_decision(
    engine: sa.Engine,
    project_slug: str,
    kind: str,
    title: str,
    context: str | None = None,
    decision: str | None = None,
    consequences: str | None = None,
    alternatives: str | None = None,
    status: str = "proposed",
    ref_code: str | None = None,
    plan_id: int | None = None,
    plan_section_id: int | None = None,
    sprint_id: int | None = None,
    source_file: str | None = None,
    source_lines: str | None = None,
    ulid: str | None = None,
) -> dict:
    _validate(kind, VALID_KINDS, "kind")
    _validate(status, VALID_STATUSES, "status")
    if not title or not title.strip():
        raise ValueError("Decision title cannot be empty")

    decision_ulid = ulid or new_ulid()
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        if ref_code:
            dup = conn.execute(
                sa.select(decisions).where(
                    decisions.c.project_id == project_id,
                    decisions.c.ref_code == ref_code,
                    decisions.c.deleted_at.is_(None),
                )
            ).first()
            if dup:
                raise ValueError(f"Decision with ref_code '{ref_code}' already exists in project")

        row = conn.execute(
            decisions.insert()
            .values(
                ulid=decision_ulid,
                project_id=project_id,
                kind=kind,
                ref_code=ref_code,
                title=title.strip(),
                context=context,
                decision=decision,
                consequences=consequences,
                alternatives=alternatives,
                status=status,
                source_file=source_file,
                source_lines=source_lines,
                plan_id=plan_id,
                plan_section_id=plan_section_id,
                sprint_id=sprint_id,
            )
            .returning(decisions)
        ).first()
        assert row is not None, "INSERT RETURNING must return a row"
        return row_to_dict(row)


def list_decisions(
    engine: sa.Engine,
    project_slug: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    plan_id: int | None = None,
    sprint_id: int | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(decisions).order_by(decisions.c.created_at.desc())
        if project_slug:
            q = q.where(decisions.c.project_id == resolve_project_id(conn, project_slug))
        if kind:
            q = q.where(decisions.c.kind == kind)
        if status:
            q = q.where(decisions.c.status == status)
        if plan_id is not None:
            q = q.where(decisions.c.plan_id == plan_id)
        if sprint_id is not None:
            q = q.where(decisions.c.sprint_id == sprint_id)
        if not include_deleted:
            q = q.where(decisions.c.deleted_at.is_(None))
        return [row_to_dict(r) for r in conn.execute(q)]


def _query_recent_decisions(
    conn: sa.Connection,
    project_id: int,
    statuses: tuple[str, ...] = ("accepted", "proposed"),
    limit: int = 10,
) -> list[dict]:
    """Newest-first live decisions a resuming agent must honor. Default filter
    keeps accepted (binding) + proposed (open questions to respect) and drops
    superseded/rejected noise. created_at DESC, id DESC tie-break."""
    rows = conn.execute(
        sa.select(decisions)
        .where(
            decisions.c.project_id == project_id,
            decisions.c.status.in_(list(statuses)),
            decisions.c.deleted_at.is_(None),
        )
        .order_by(decisions.c.created_at.desc(), decisions.c.id.desc())
        .limit(limit)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def list_recent_decisions(
    engine: sa.Engine,
    project_slug: str,
    statuses: tuple[str, ...] = ("accepted", "proposed"),
    limit: int = 10,
) -> list[dict]:
    """Public wrapper over _query_recent_decisions (slug → id, own txn).
    `list_decisions` stays the unfiltered/paged surface; this one is the
    snapshot-sized "what still binds" view."""
    with engine.begin() as conn:
        return _query_recent_decisions(
            conn, resolve_project_id(conn, project_slug), statuses, limit
        )


def get_decision(engine: sa.Engine, decision_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(decisions).where(
                decisions.c.id == decision_id, decisions.c.deleted_at.is_(None)
            )
        ).first()
        return row_to_dict(row) if row else None


def get_decision_by_ulid(engine: sa.Engine, ulid: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(decisions).where(
                decisions.c.ulid == ulid, decisions.c.deleted_at.is_(None)
            )
        ).first()
        return row_to_dict(row) if row else None


def get_decision_by_ref_code(
    engine: sa.Engine, project_slug: str, ref_code: str
) -> dict | None:
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        row = conn.execute(
            sa.select(decisions).where(
                decisions.c.project_id == pid,
                decisions.c.ref_code == ref_code,
                decisions.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def update_decision(engine: sa.Engine, decision_id: int, **kwargs) -> dict | None:
    allowed = {
        "title", "context", "decision", "consequences", "alternatives",
        "status", "kind", "ref_code",
        "plan_id", "plan_section_id", "sprint_id",
        "source_file", "source_lines",
    }
    values: dict = {}
    for k, v in kwargs.items():
        if k not in allowed or v is None:
            continue
        if k == "kind":
            _validate(v, VALID_KINDS, "kind")
        elif k == "status":
            _validate(v, VALID_STATUSES, "status")
        values[k] = v
    if not values:
        raise ValueError("No valid fields to update")
    values["updated_at"] = datetime.now(timezone.utc)

    with engine.begin() as conn:
        result = conn.execute(
            decisions.update()
            .where(
                decisions.c.id == decision_id, decisions.c.deleted_at.is_(None)
            )
            .values(**values)
        )
        if result.rowcount == 0:
            return None
    return get_decision(engine, decision_id)


def supersede_decision(
    engine: sa.Engine, old_decision_id: int, new_decision_id: int
) -> dict | None:
    """Mark an older decision as superseded by a newer one.

    Sets `status='superseded'` and `superseded_by=<new_id>` on the old row.
    Does NOT touch the new row — caller is responsible for its state
    (typically `status='accepted'` when the supersession happens).
    """
    if old_decision_id == new_decision_id:
        raise ValueError("A decision cannot supersede itself")
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        # Verify both rows exist
        old = conn.execute(
            sa.select(decisions.c.id).where(
                decisions.c.id == old_decision_id,
                decisions.c.deleted_at.is_(None),
            )
        ).first()
        if not old:
            raise ValueError(f"Decision #{old_decision_id} not found")
        new = conn.execute(
            sa.select(decisions.c.id).where(
                decisions.c.id == new_decision_id,
                decisions.c.deleted_at.is_(None),
            )
        ).first()
        if not new:
            raise ValueError(f"Decision #{new_decision_id} not found")

        result = conn.execute(
            decisions.update()
            .where(decisions.c.id == old_decision_id)
            .values(status="superseded", superseded_by=new_decision_id, updated_at=now)
        )
        if result.rowcount == 0:
            return None
    return get_decision(engine, old_decision_id)


def delete_decision(engine: sa.Engine, decision_id: int) -> bool:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            decisions.update()
            .where(
                decisions.c.id == decision_id, decisions.c.deleted_at.is_(None)
            )
            .values(deleted_at=now)
        )
        return result.rowcount > 0
