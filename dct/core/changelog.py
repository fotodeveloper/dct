"""Changelog CRUD operations with soft deletes."""

from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.projects import resolve_project_id
from dct.db import changelog_entries, projects, row_to_dict

VALID_CATEGORIES = {"added", "changed", "fixed", "removed"}


def add_entry(
    engine: sa.Engine,
    project_slug: str,
    category: str,
    entry: str,
    version: str | None = None,
    component: str | None = None,
    source_file: str | None = None,
    source_lines: str | None = None,
    related_item_id: int | None = None,
) -> dict:
    if not entry or not entry.strip():
        raise ValueError("Changelog entry cannot be empty")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: '{category}'. Valid: {', '.join(sorted(VALID_CATEGORIES))}")

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        result = conn.execute(
            changelog_entries.insert()
            .values(
                project_id=project_id,
                version=version,
                category=category,
                entry=entry,
                component=component,
                source_file=source_file,
                source_lines=source_lines,
                related_item_id=related_item_id,
            )
            .returning(changelog_entries)
        )
        return row_to_dict(result.first())


def list_entries(
    engine: sa.Engine,
    project_slug: str,
    version: str | None = None,
    unreleased_only: bool = False,
) -> list[dict]:
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        q = (
            sa.select(changelog_entries)
            .where(changelog_entries.c.project_id == pid, changelog_entries.c.deleted_at.is_(None))
            .order_by(changelog_entries.c.created_at.desc())
        )
        if unreleased_only:
            q = q.where(changelog_entries.c.version.is_(None))
        elif version:
            q = q.where(changelog_entries.c.version == version)
        return [row_to_dict(r) for r in conn.execute(q)]


def count_unreleased(engine: sa.Engine, project_slug: str) -> int:
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        result = conn.execute(
            sa.select(sa.func.count())
            .select_from(changelog_entries)
            .where(
                changelog_entries.c.project_id == pid,
                changelog_entries.c.version.is_(None),
                changelog_entries.c.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0


def release_entries(engine: sa.Engine, project_slug: str, version: str) -> int:
    """Stamp all unreleased entries with a version and update project's current_version."""
    now = datetime.now(UTC)
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        result = conn.execute(
            changelog_entries.update()
            .where(
                changelog_entries.c.project_id == pid,
                changelog_entries.c.version.is_(None),
                changelog_entries.c.deleted_at.is_(None),
            )
            .values(version=version, released_at=now, updated_at=now)
        )
        conn.execute(
            projects.update()
            .where(projects.c.id == pid)
            .values(current_version=version, updated_at=now)
        )
        return result.rowcount


def delete_entry(engine: sa.Engine, entry_id: int) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            changelog_entries.update()
            .where(changelog_entries.c.id == entry_id, changelog_entries.c.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC))
        )
        return result.rowcount > 0
