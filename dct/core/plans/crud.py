"""CRUD operations for plans, plan_sections, plan_checkpoints.

Follows the same patterns as dct.core.items / dct.core.projects:
  * Each function takes `engine` first.
  * All returns are dicts (row_to_dict wrapped).
  * Soft-delete via `deleted_at`; no hard deletes exposed.
  * ULIDs are the stable cross-cut identifier (MD anchors → DB).

Imports from dct.core.plans.parser + classify are DELIBERATELY not pulled
in here — CRUD must be usable without the ingest pipeline for
programmatic roadmap creation (e.g. `dct roadmap create`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.ids import new_ulid
from dct.core.projects import resolve_project_id
from dct.db import items as items_tbl
from dct.db import (
    lock_row,
    plan_checkpoints,
    plan_sections,
    plans,
    row_to_dict,
)

VALID_PLAN_KINDS = {"plan", "spec", "roadmap", "adr", "retro", "backlog"}
VALID_PLAN_STATUSES = {"draft", "active", "completed", "archived"}

VALID_SECTION_STATUSES = {"pending", "in_progress", "done", "rejected", "na"}
VALID_SECTION_TYPES = {
    "sprint", "phase", "task", "block", "milestone",
    "decision", "prose", "changelog", "adr_section",
    "reference", "generic", "uncertain",
}

VALID_CP_STATUSES = {"pending", "done", "rejected"}


def _validate(value: str, valid: set[str], field: str) -> str:
    if value not in valid:
        raise ValueError(f"Invalid {field}: '{value}'. Valid: {', '.join(sorted(valid))}")
    return value


# ══ Plans ════════════════════════════════════════════════════════════════════

def create_plan(
    engine: sa.Engine,
    project_slug: str,
    slug: str,
    title: str,
    kind: str,
    source_file: str | None = None,
    source_hash: str | None = None,
    sprint_id: int | None = None,
    ulid: str | None = None,
    status: str = "active",
) -> dict:
    _validate(kind, VALID_PLAN_KINDS, "kind")
    _validate(status, VALID_PLAN_STATUSES, "status")
    if not slug or not slug.strip():
        raise ValueError("Plan slug cannot be empty")
    if not title or not title.strip():
        raise ValueError("Plan title cannot be empty")

    plan_ulid = ulid or new_ulid()
    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        duplicate = conn.execute(
            sa.select(plans).where(
                plans.c.project_id == project_id,
                plans.c.slug == slug,
                plans.c.deleted_at.is_(None),
            )
        ).first()
        if duplicate:
            raise ValueError(f"Plan with slug '{slug}' already exists in project")

        row = conn.execute(
            plans.insert()
            .values(
                ulid=plan_ulid,
                project_id=project_id,
                slug=slug.strip(),
                title=title.strip(),
                kind=kind,
                source_file=source_file,
                source_hash=source_hash,
                sprint_id=sprint_id,
                status=status,
            )
            .returning(plans)
        ).first()
        assert row is not None, "INSERT RETURNING must return a row"
        return row_to_dict(row)


def list_plans(
    engine: sa.Engine,
    project_slug: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(plans).order_by(plans.c.created_at.desc())
        if project_slug:
            q = q.where(plans.c.project_id == resolve_project_id(conn, project_slug))
        if kind:
            q = q.where(plans.c.kind == kind)
        if status:
            q = q.where(plans.c.status == status)
        if not include_deleted:
            q = q.where(plans.c.deleted_at.is_(None))
        return [row_to_dict(r) for r in conn.execute(q)]


def get_plan(engine: sa.Engine, plan_id: int) -> dict | None:
    """Return plan + nested sections + checkpoints + progress rollup.

    Shape: {..plan columns..,
            'sections': [{..section columns.., 'checkpoints': [...]}, ...],
            'progress': {'total': N, 'done': N, 'rejected': N, 'pending': N,
                         'active': N (total minus rejected), 'percent': 0..100}}
    """
    with engine.begin() as conn:
        plan_row = conn.execute(
            sa.select(plans).where(plans.c.id == plan_id, plans.c.deleted_at.is_(None))
        ).first()
        if not plan_row:
            return None
        plan_dict = row_to_dict(plan_row)

        section_rows = conn.execute(
            sa.select(plan_sections)
            .where(plan_sections.c.plan_id == plan_id, plan_sections.c.deleted_at.is_(None))
            .order_by(plan_sections.c.sort_order, plan_sections.c.heading_line if hasattr(plan_sections.c, "heading_line") else plan_sections.c.id)
        ).fetchall()

        cp_rows = conn.execute(
            sa.select(plan_checkpoints)
            .join(plan_sections, plan_checkpoints.c.plan_section_id == plan_sections.c.id)
            .where(plan_sections.c.plan_id == plan_id, plan_checkpoints.c.deleted_at.is_(None))
            .order_by(plan_checkpoints.c.sort_order, plan_checkpoints.c.id)
        ).fetchall()

        sections_by_id: dict[int, dict] = {}
        for row in section_rows:
            d = row_to_dict(row)
            d["checkpoints"] = []
            sections_by_id[d["id"]] = d

        total = done = rejected = pending = 0
        for cp_row in cp_rows:
            cp_d = row_to_dict(cp_row)
            parent = sections_by_id.get(cp_d["plan_section_id"])
            if parent is not None:
                parent["checkpoints"].append(cp_d)
            total += 1
            if cp_d["status"] == "done":
                done += 1
            elif cp_d["status"] == "rejected":
                rejected += 1
            else:
                pending += 1

        active = total - rejected
        percent = round(100 * done / active) if active else 0
        plan_dict["sections"] = list(sections_by_id.values())
        plan_dict["progress"] = {
            "total": total, "done": done, "rejected": rejected,
            "pending": pending, "active": active, "percent": percent,
        }
        return plan_dict


def get_plan_by_ulid(engine: sa.Engine, ulid: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(plans.c.id).where(
                plans.c.ulid == ulid, plans.c.deleted_at.is_(None)
            )
        ).first()
        if not row:
            return None
    return get_plan(engine, row.id)


def get_plan_by_slug(engine: sa.Engine, project_slug: str, slug: str) -> dict | None:
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        row = conn.execute(
            sa.select(plans.c.id).where(
                plans.c.project_id == pid,
                plans.c.slug == slug,
                plans.c.deleted_at.is_(None),
            )
        ).first()
        if not row:
            return None
    return get_plan(engine, row.id)


def update_plan(engine: sa.Engine, plan_id: int, **kwargs) -> dict | None:
    allowed = {"title", "kind", "source_file", "source_hash", "status", "sprint_id"}
    values: dict = {}
    for k, v in kwargs.items():
        if k not in allowed or v is None:
            continue
        if k == "kind":
            _validate(v, VALID_PLAN_KINDS, "kind")
        elif k == "status":
            _validate(v, VALID_PLAN_STATUSES, "status")
        values[k] = v
    if not values:
        raise ValueError("No valid fields to update")

    values["updated_at"] = datetime.now(UTC)
    if values.get("status") == "archived":
        values["archived_at"] = values["updated_at"]

    with engine.begin() as conn:
        result = conn.execute(
            plans.update()
            .where(plans.c.id == plan_id, plans.c.deleted_at.is_(None))
            .values(**values)
        )
        if result.rowcount == 0:
            return None
    return get_plan(engine, plan_id)


def set_plan_imported(
    engine: sa.Engine, plan_id: int, source_hash: str | None = None
) -> dict | None:
    """Stamp `imported_at=now()` and optionally update `source_hash`.

    Called once at the end of the first ingest to close the first-import
    gate (§9.3). Subsequent rescans are re-imports; they may update
    source_hash but must not touch imported_at.
    """
    now = datetime.now(UTC)
    values: dict = {"imported_at": now, "updated_at": now}
    if source_hash is not None:
        values["source_hash"] = source_hash
    with engine.begin() as conn:
        result = conn.execute(
            plans.update()
            .where(plans.c.id == plan_id, plans.c.deleted_at.is_(None))
            .values(**values)
        )
        if result.rowcount == 0:
            return None
    return get_plan(engine, plan_id)


def delete_plan(engine: sa.Engine, plan_id: int) -> bool:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        result = conn.execute(
            plans.update()
            .where(plans.c.id == plan_id, plans.c.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        return result.rowcount > 0


# ══ Plan sections ════════════════════════════════════════════════════════════

def add_plan_section(
    engine: sa.Engine,
    plan_id: int,
    heading: str,
    depth: int,
    section_type: str = "generic",
    ulid: str | None = None,
    parent_section_id: int | None = None,
    slug: str | None = None,
    sort_order: int = 0,
    sprint_id: int | None = None,
    needs_llm_review: bool = False,
    section_type_confidence: str = "high",
    source_lines: str | None = None,
    status: str = "pending",
    classification_result=None,
) -> dict:
    """Create a section, optionally atomically carrying known classification.

    Generic creation keeps unknown provenance. Ingest supplies a typed semantic
    result; its coherent fields are validated by the same classification writer.
    Provider execution must finish before this function opens its transaction.
    """
    classification_values = {}
    if classification_result is not None:
        from dct.core.plans.classification import _values
        classification_values = _values(classification_result)
        section_type = classification_values.pop("section_type")
        section_type_confidence = classification_values.pop("section_type_confidence")
        needs_llm_review = classification_values.pop("needs_llm_review")
        classification_values.update(classified_at=datetime.now(UTC), classification_revision=0)
    _validate(section_type, VALID_SECTION_TYPES, "section_type")
    _validate(status, VALID_SECTION_STATUSES, "status")
    if depth < 1 or depth > 6:
        raise ValueError(f"Invalid depth: {depth}. Must be 1-6.")
    if section_type_confidence not in {"high", "low", "uncertain"}:
        raise ValueError(
            f"Invalid section_type_confidence: '{section_type_confidence}'. "
            "Valid: high, low, uncertain."
        )

    section_ulid = ulid or new_ulid()
    with engine.begin() as conn:
        row = conn.execute(
            plan_sections.insert()
            .values(
                ulid=section_ulid,
                plan_id=plan_id,
                parent_section_id=parent_section_id,
                heading=heading,
                depth=depth,
                slug=slug,
                sort_order=sort_order,
                section_type=section_type,
                section_type_confidence=section_type_confidence,
                needs_llm_review=needs_llm_review,
                status=status,
                source_lines=source_lines,
                sprint_id=sprint_id,
                **classification_values,
            )
            .returning(plan_sections)
        ).first()
        assert row is not None, "INSERT RETURNING must return a row"
        return row_to_dict(row)


def list_plan_sections(
    engine: sa.Engine,
    plan_id: int,
    parent_section_id: int | None = None,
    section_type: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(plan_sections).where(plan_sections.c.plan_id == plan_id)
        if parent_section_id is not None:
            q = q.where(plan_sections.c.parent_section_id == parent_section_id)
        if section_type:
            q = q.where(plan_sections.c.section_type == section_type)
        if not include_deleted:
            q = q.where(plan_sections.c.deleted_at.is_(None))
        q = q.order_by(plan_sections.c.sort_order, plan_sections.c.id)
        return [row_to_dict(r) for r in conn.execute(q)]


def get_plan_section(engine: sa.Engine, section_id: int) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(plan_sections).where(
                plan_sections.c.id == section_id,
                plan_sections.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def get_plan_section_by_ulid(engine: sa.Engine, ulid: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(plan_sections).where(
                plan_sections.c.ulid == ulid,
                plan_sections.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def update_plan_section(engine: sa.Engine, section_id: int, **kwargs) -> dict | None:
    if {"section_type", "section_type_confidence", "needs_llm_review",
        "classification_source", "classification_provider", "classification_model",
        "classified_at", "classification_revision"}.intersection(kwargs):
        raise ValueError("Unsupported direct classification update "
                         "(section_type, section_type_confidence, needs_llm_review); "
                         "use set_plan_section_type")
    allowed = {
        "heading", "depth", "slug", "sort_order", "section_type",
        "section_type_confidence", "needs_llm_review", "status",
        "source_lines", "sprint_id", "parent_section_id",
    }
    values: dict = {}
    for k, v in kwargs.items():
        if k not in allowed or (v is None and k != "parent_section_id"):
            continue
        if k == "section_type":
            _validate(v, VALID_SECTION_TYPES, "section_type")
        elif k == "status":
            _validate(v, VALID_SECTION_STATUSES, "status")
        elif k == "section_type_confidence" and v not in {"high", "low", "uncertain"}:
            raise ValueError(f"Invalid section_type_confidence: '{v}'")
        elif k == "depth" and (v < 1 or v > 6):
            raise ValueError(f"Invalid depth: {v}")
        values[k] = v
    if not values:
        raise ValueError("No valid fields to update")
    values["updated_at"] = datetime.now(UTC)

    with engine.begin() as conn:
        result = conn.execute(
            plan_sections.update()
            .where(
                plan_sections.c.id == section_id,
                plan_sections.c.deleted_at.is_(None),
            )
            .values(**values)
        )
        if result.rowcount == 0:
            return None
    return get_plan_section(engine, section_id)


def delete_plan_section(engine: sa.Engine, section_id: int) -> bool:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        result = conn.execute(
            plan_sections.update()
            .where(
                plan_sections.c.id == section_id,
                plan_sections.c.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        return result.rowcount > 0


# ══ Plan checkpoints ═════════════════════════════════════════════════════════

def add_plan_checkpoint(
    engine: sa.Engine,
    plan_section_id: int,
    text: str,
    status: str = "pending",
    ulid: str | None = None,
    sort_order: int = 0,
    source_lines: str | None = None,
) -> dict:
    _validate(status, VALID_CP_STATUSES, "status")
    if not text or not text.strip():
        raise ValueError("Checkpoint text cannot be empty")
    cp_ulid = ulid or new_ulid()
    with engine.begin() as conn:
        row = conn.execute(
            plan_checkpoints.insert()
            .values(
                ulid=cp_ulid,
                plan_section_id=plan_section_id,
                text=text.strip(),
                status=status,
                sort_order=sort_order,
                source_lines=source_lines,
            )
            .returning(plan_checkpoints)
        ).first()
        assert row is not None, "INSERT RETURNING must return a row"
        return row_to_dict(row)


def list_plan_checkpoints(
    engine: sa.Engine,
    plan_section_id: int | None = None,
    plan_id: int | None = None,
    status: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(plan_checkpoints)
        if plan_section_id is not None:
            q = q.where(plan_checkpoints.c.plan_section_id == plan_section_id)
        if plan_id is not None:
            q = q.join(
                plan_sections, plan_checkpoints.c.plan_section_id == plan_sections.c.id
            ).where(plan_sections.c.plan_id == plan_id)
        if status:
            q = q.where(plan_checkpoints.c.status == status)
        if not include_deleted:
            q = q.where(plan_checkpoints.c.deleted_at.is_(None))
        q = q.order_by(plan_checkpoints.c.sort_order, plan_checkpoints.c.id)
        return [row_to_dict(r) for r in conn.execute(q)]


def get_plan_checkpoint_by_ulid(engine: sa.Engine, ulid: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(plan_checkpoints).where(
                plan_checkpoints.c.ulid == ulid,
                plan_checkpoints.c.deleted_at.is_(None),
            )
        ).first()
        return row_to_dict(row) if row else None


def _set_checkpoint_status(
    engine: sa.Engine, checkpoint_id: int, new_status: str
) -> dict | None:
    _validate(new_status, VALID_CP_STATUSES, "status")
    now = datetime.now(UTC)
    with engine.begin() as conn:
        result = conn.execute(
            plan_checkpoints.update()
            .where(
                plan_checkpoints.c.id == checkpoint_id,
                plan_checkpoints.c.deleted_at.is_(None),
            )
            .values(status=new_status, updated_at=now)
        )
        if result.rowcount == 0:
            return None
        row = conn.execute(
            sa.select(plan_checkpoints).where(plan_checkpoints.c.id == checkpoint_id)
        ).first()
        return row_to_dict(row) if row else None


def complete_plan_checkpoint(engine: sa.Engine, checkpoint_id: int) -> dict | None:
    return _set_checkpoint_status(engine, checkpoint_id, "done")


def reject_plan_checkpoint(engine: sa.Engine, checkpoint_id: int) -> dict | None:
    return _set_checkpoint_status(engine, checkpoint_id, "rejected")


def reopen_plan_checkpoint(engine: sa.Engine, checkpoint_id: int) -> dict | None:
    """Revert a checkpoint to pending (undo done/rejected)."""
    return _set_checkpoint_status(engine, checkpoint_id, "pending")


# ══ Cross-entity: promote section/checkpoint → standalone item ═══════════════

def promote_section_to_item(
    engine: sa.Engine,
    section_id: int,
    item_type: str,
    priority: str = "P2",
    title: str | None = None,
    short_title: str | None = None,
) -> dict:
    """Create an `items` row from a plan_section and back-link via converted_to_item_id.

    Used when a section deserves its own lifecycle (e.g. "Sprint 3" becomes
    a tracked sprint-feature). The section remains in the plan as a
    read-only pointer — `converted_to_item_id` marks the promotion.
    `short_title` is passed through to create_item_conn (≤ 50 chars, refused
    not truncated; None/"" = no label). ONE transaction (#909 K-24), as
    promote_checkpoint_to_item: the section row is locked first (lock_row —
    also the not-found check, a soft-deleted section is refused), the item
    is created by create_item_conn (the full create_item validation, live
    project included) and the back-link is written; any refusal or failure
    leaves neither an item nor a back-link. Each call creates a new item —
    a repeated promotion is not refused (the back-link then names the
    latest item).
    """
    from dct.core.items import MAX_TITLE_LENGTH, create_item_conn
    with engine.begin() as conn:
        if not lock_row(conn, plan_sections, section_id):
            raise ValueError(f"Plan section #{section_id} not found")
        section_row = conn.execute(
            sa.select(plan_sections.c.plan_id, plan_sections.c.heading)
            .where(plan_sections.c.id == section_id)
        ).first()
        assert section_row is not None
        plan_row = conn.execute(
            sa.select(plans.c.project_id).where(plans.c.id == section_row.plan_id)
        ).first()
        if plan_row is None:
            raise ValueError(f"Plan #{section_row.plan_id} not found for section")

        now = datetime.now(UTC)
        effective_title = title or section_row.heading[:MAX_TITLE_LENGTH]
        item = create_item_conn(
            conn, plan_row.project_id, item_type, effective_title,
            priority=priority, short_title=short_title,
        )
        conn.execute(
            plan_sections.update()
            .where(plan_sections.c.id == section_id)
            .values(converted_to_item_id=item["id"], updated_at=now)
        )
    return item


def promote_section_to_sprint(
    engine: sa.Engine,
    section_id: int,
    code: str,
    goal: str | None = None,
    name: str | None = None,
    kind: str = "sprint",
) -> dict:
    """Materialize a plan_section as a first-class sprint row.

    Contrast with `promote_section_to_item`: that one converts a section
    into a trackable `items` row. This one promotes a section that *is*
    a sprint-in-prose (`## Sprint 1 — Hub Redesign`) into a queryable
    `sprints` entry with lifecycle + goal + unique project-scoped `code`.

    Back-link via `plan_sections.sprint_id` (FK already present in schema,
    no migration needed). The section itself stays in the plan.

    `code` must be unique within the project. Heading-based auto-codes are
    a bad idea because plan A's "Sprint 1" collides with plan B's "Sprint 1";
    caller (CLI / skill) picks the namespaced code, e.g. `v0.2-hub`.

    `name` defaults to section.heading; `goal` is optional free-text.
    """
    from dct.core.sprints import create_sprint
    section = get_plan_section(engine, section_id)
    if section is None:
        raise ValueError(f"Plan section #{section_id} not found")

    # Derive project_slug via plan → project
    with engine.begin() as conn:
        plan_row = conn.execute(
            sa.select(plans.c.project_id).where(plans.c.id == section["plan_id"])
        ).first()
        if plan_row is None:
            raise ValueError(f"Plan #{section['plan_id']} not found for section")
        from dct.db import projects as projects_tbl
        project_row = conn.execute(
            sa.select(projects_tbl.c.slug).where(projects_tbl.c.id == plan_row.project_id)
        ).first()
        assert project_row is not None
        project_slug = project_row.slug

    effective_name = name or section["heading"]
    sprint = create_sprint(
        engine, project_slug, name=effective_name,
        code=code, kind=kind, goal=goal,
    )
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            plan_sections.update()
            .where(plan_sections.c.id == section_id)
            .values(sprint_id=sprint["id"], updated_at=now)
        )
    return sprint


def promote_checkpoint_to_item(
    engine: sa.Engine,
    checkpoint_id: int,
    item_type: str,
    priority: str = "P2",
    title: str | None = None,
    short_title: str | None = None,
) -> dict:
    """Create an items row from a plan_checkpoint and back-link.

    `short_title` is passed through to create_item_conn (≤ 50 chars, refused
    not truncated; None/"" = no label). ONE transaction (#909 K-17): the
    checkpoint row is locked first (lock_row — concurrent promotions of the
    same checkpoint queue on it), the item is created by create_item_conn
    (the full create_item validation, live project included) and the
    back-link is written; any refusal leaves neither an item nor a
    back-link. Each call creates a new item — a repeated promotion is not
    refused (the back-link then names the latest item).
    """
    from dct.core.items import MAX_TITLE_LENGTH, create_item_conn
    with engine.begin() as conn:
        if not lock_row(conn, plan_checkpoints, checkpoint_id):
            raise ValueError(f"Plan checkpoint #{checkpoint_id} not found")
        cp_row = conn.execute(
            sa.select(plan_checkpoints).where(plan_checkpoints.c.id == checkpoint_id)
        ).first()
        assert cp_row is not None
        cp = row_to_dict(cp_row)

        section_row = conn.execute(
            sa.select(plan_sections.c.plan_id).where(
                plan_sections.c.id == cp["plan_section_id"]
            )
        ).first()
        assert section_row is not None
        plan_row = conn.execute(
            sa.select(plans.c.project_id).where(plans.c.id == section_row.plan_id)
        ).first()
        assert plan_row is not None

        now = datetime.now(UTC)
        effective_title = title or cp["text"][:MAX_TITLE_LENGTH]
        item = create_item_conn(
            conn, plan_row.project_id, item_type, effective_title,
            priority=priority, short_title=short_title,
        )
        conn.execute(
            plan_checkpoints.update()
            .where(plan_checkpoints.c.id == checkpoint_id)
            .values(converted_to_item_id=item["id"], updated_at=now)
        )
    return item


# Unused import placeholder — keeps the linter quiet that items_tbl is imported
# but not referenced directly. Core modules (resolve_item etc.) sometimes expect
# the table object to be importable from this module.
_ = items_tbl
