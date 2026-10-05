"""Project CRUD operations."""

import os
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.db import projects, row_to_dict


def create_project(
    engine: sa.Engine,
    slug: str,
    name: str,
    path: str | None = None,
    description: str | None = None,
    current_version: str | None = None,
) -> dict:
    """Create (or undelete) a project row.

    An explicit `current_version` passes the version write contract
    (core.version.storable_version — normalized, refused with ValueError when
    unsupported or too long; nothing is written then). When it is not
    supplied and `path` is set, attempts best-effort version detection from
    VERSION / pyproject.toml / package.json / Cargo.toml /
    src-tauri/tauri.conf.json; a detected version is normalized the same way.
    The returned dict carries a transient `_detected_source` key naming the
    file that produced the stored version — callers (CLI) may surface it to
    the user; it is not persisted. A DETECTED version the contract refuses
    is never stored and never substituted: current_version stays NULL and
    the transient `_detected_unsupported` = {version (raw), source, reason}
    reports it, so the caller can say so explicitly.
    """
    if not slug or not slug.strip():
        raise ValueError("Project slug cannot be empty")
    if not name or not name.strip():
        raise ValueError("Project name cannot be empty")

    # #227: store the canonical (symlink-resolved) path so CWD detection — which
    # passes os.getcwd() (physical) — always matches what we persisted here.
    if path is not None:
        path = os.path.realpath(path)

    from dct.core.version import detect_version, storable_version

    detected_source: str | None = None
    detected_unsupported: dict | None = None
    if current_version is not None:
        current_version = storable_version(current_version)
    elif path:
        detected_version, detected_source = detect_version(path)
        if detected_version:
            try:
                current_version = storable_version(detected_version)
            except ValueError as exc:
                detected_unsupported = {
                    "version": detected_version, "source": detected_source,
                    "reason": str(exc),
                }
                detected_source = None
        else:
            detected_source = None  # no detection → nothing to report

    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(projects).where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
        ).first()
        if existing:
            raise ValueError(f"Project '{slug}' already exists")

        deleted = conn.execute(
            sa.select(projects).where(projects.c.slug == slug, projects.c.deleted_at.isnot(None))
        ).first()
        if deleted:
            conn.execute(
                projects.update()
                .where(projects.c.id == deleted._mapping["id"])
                .values(name=name, path=path, description=description, current_version=current_version, deleted_at=None, updated_at=datetime.now(UTC))
            )
            row = conn.execute(sa.select(projects).where(projects.c.id == deleted._mapping["id"])).first()
            assert row is not None, "undelete must return its row"
            return _with_detection(row_to_dict(row), detected_source, detected_unsupported)

        inserted = conn.execute(
            projects.insert()
            .values(slug=slug, name=name, path=path, description=description, current_version=current_version)
            .returning(projects)
        ).first()
        assert inserted is not None, "INSERT RETURNING must return a row"
        return _with_detection(row_to_dict(inserted), detected_source, detected_unsupported)


def _with_detection(row: dict, source: str | None, unsupported: dict | None) -> dict:
    """Attach the transient detection report keys (never persisted)."""
    if source:
        row["_detected_source"] = source
    if unsupported:
        row["_detected_unsupported"] = unsupported
    return row


def list_projects(engine: sa.Engine, include_deleted: bool = False) -> list[dict]:
    with engine.begin() as conn:
        q = sa.select(projects).order_by(projects.c.slug)
        if not include_deleted:
            q = q.where(projects.c.deleted_at.is_(None))
        return [row_to_dict(r) for r in conn.execute(q)]


def get_project(engine: sa.Engine, slug: str) -> dict | None:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(projects).where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
        ).first()
        return row_to_dict(row) if row else None


def _path_is_within(target: str, stored: str, *, normcase=os.path.normcase,
                    sep=os.sep) -> bool:
    """Case- and separator-aware path containment (W4, ADR #52).

    The previous hardcoded '/' + case-sensitive compare silently disabled
    CWD auto-detection on Windows — every project subdir failed to resolve,
    no-oping the hooks and every project='' MCP call. os.path.normcase is a
    no-op on POSIX and lowercases + flips '/'→'\\\\' on Windows, fixing both
    mismatches in one call. normcase/sep are injectable so the Windows
    semantics are testable from POSIX (ntpath.normcase + '\\\\')."""
    t, s = normcase(target), normcase(stored)
    return t == s or t.startswith(s.rstrip(sep) + sep)


def get_project_by_path(engine: sa.Engine, path: str) -> dict | None:
    """Find the project whose registered path is a prefix of `path`.

    Both the query path and every stored path are canonicalized with
    os.path.realpath before comparison (#227). Rationale: `dct project add`
    historically stored os.path.abspath (symlinks intact) while
    `dct project detect` passes os.getcwd(), which returns the PHYSICAL path
    (symlinks resolved) on macOS. A raw-string prefix compare therefore made a
    project registered under a symlinked path (e.g. /tmp → /private/tmp, or a
    repo under a symlinked root) silently undetectable, which turned the
    changelog hook OFF for that repo. realpath on both sides closes the gap and
    is idempotent for already-canonical paths.
    """
    target = os.path.realpath(path)
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(projects).where(projects.c.deleted_at.is_(None), projects.c.path.isnot(None))
        ).fetchall()
        # Match longest stored path first (most specific project).
        matches = []
        for r in rows:
            stored = os.path.realpath(r._mapping["path"])
            if _path_is_within(target, stored):
                matches.append((r, len(stored)))
        if not matches:
            return None
        matches.sort(key=lambda x: x[1], reverse=True)
        return row_to_dict(matches[0][0])


def delete_project(engine: sa.Engine, slug: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC))
        )
        return result.rowcount > 0


def set_version(engine: sa.Engine, slug: str, version: str) -> dict | None:
    """Set the project's current_version; returns the row, None if unknown.

    The version passes the ONE write contract (core.version.storable_version,
    K-54): `v1.6.0` is stored as `1.6.0`; an unsupported format or a value
    wider than a version column raises ValueError and nothing is written.
    """
    from dct.core.version import storable_version

    version = storable_version(version)
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
            .values(current_version=version, updated_at=datetime.now(UTC))
        )
        if result.rowcount == 0:
            return None
    return get_project(engine, slug)


def set_manages_changelog(engine: sa.Engine, slug: str, value: bool) -> dict | None:
    """Opt a project in/out of dct changelog-hook enforcement (#222).

    Returns the updated project row, or None when the slug does not resolve to
    a live project.
    """
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
            .values(manages_changelog=value, updated_at=datetime.now(UTC))
        )
        if result.rowcount == 0:
            return None
    return get_project(engine, slug)


def project_manages_changelog(engine: sa.Engine, slug: str) -> bool:
    """True iff the project opts into dct changelog enforcement (#222).

    False when the project does not exist — the changelog hook treats an
    unknown/unresolved project as 'not managed' (skip), so it never blocks.
    """
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(projects.c.manages_changelog).where(
                projects.c.slug == slug, projects.c.deleted_at.is_(None)
            )
        ).first()
        return bool(row._mapping["manages_changelog"]) if row else False


def set_changelog_path(engine: sa.Engine, slug: str, relpath: str) -> dict | None:
    """Set a project's changelog file location, relative to its root (#225).

    Default is 'changelog.md' (repo root). A project whose changelog lives in a
    subdirectory (e.g. a monorepo package) points it there, and /release writes
    {project.path}/{changelog_path}. The value is normalized and constrained to
    the project root — a leading '/' is stripped and any '..' escape is rejected
    — so /release can only ever write inside the project tree.

    Returns the updated project row, or None when the slug does not resolve to a
    live project. Raises ValueError on an empty path or a '..' escape.
    """
    if relpath is None or not relpath.strip():
        raise ValueError("changelog_path cannot be empty")
    rel = os.path.normpath(relpath.strip().lstrip("/"))
    if rel.startswith("..") or os.path.isabs(rel):
        raise ValueError("changelog_path must stay within the project root (no '..' escape)")
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
            .values(changelog_path=rel, updated_at=datetime.now(UTC))
        )
        if result.rowcount == 0:
            return None
    return get_project(engine, slug)


def _set_project_value(engine: sa.Engine, slug: str, **values) -> dict | None:
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
            .values(**values, updated_at=datetime.now(UTC))
        )
        if result.rowcount == 0:
            return None
    return get_project(engine, slug)


def set_release_executor(engine: sa.Engine, slug: str, executor: str) -> dict | None:
    """Set who performs the project's release effects (K-85): 'dct' (the
    effects layer — the default) or 'own' (the project's own release tool).

    Copied onto each NEW release record at stamp time (releases.executor);
    an unfinished release keeps the executor it was stamped with. Returns the
    updated project row, or None when the slug does not resolve to a live
    project. ValueError for any other value."""
    from dct.core.releases import validate_executor

    return _set_project_value(engine, slug, release_executor=validate_executor(executor))


def set_changelog_pr_refs(engine: sa.Engine, slug: str, enabled: bool) -> dict | None:
    """Opt the project's changelog render into `(#N)` PR references (K-90;
    default off). Persisted per release at stamp time (releases.render), so
    a change never alters a release already stamped. Returns the updated
    project row, or None when the slug does not resolve to a live project.
    TypeError unless `enabled` is a bool."""
    if not isinstance(enabled, bool):
        raise TypeError(f"changelog_pr_refs must be true or false, got {enabled!r}")
    return _set_project_value(engine, slug, changelog_pr_refs=enabled)


def resolve_changelog_path(project: dict) -> str | None:
    """Absolute path to a project's changelog file: {path}/{changelog_path} (#225).

    Returns None when the project has no registered path (nothing to resolve
    against). changelog_path falls back to 'changelog.md' if missing/empty so a
    row predating the column still resolves to the repo-root default.
    """
    path = project.get("path")
    if not path:
        return None
    return os.path.join(path, project.get("changelog_path") or "changelog.md")


def update_project(
    engine: sa.Engine,
    slug: str,
    new_slug: str | None = None,
    new_name: str | None = None,
    new_description: str | None = None,
    new_path: str | None = None,
) -> dict | None:
    """Update one or more mutable fields on a project row.

    Any non-None kwarg overwrites. Returns the updated row, or None if the
    source `slug` does not resolve to a live project. Raises ValueError when:
      - no fields are supplied (nothing to do — caller bug)
      - `new_slug` or `new_name` is an empty/whitespace string
      - `new_slug` collides with an existing live project

    project_id stays stable: items/changelog/checkpoints/handoffs keep their
    FK without any cascade needed — rename is purely cosmetic at the DB level.
    """
    updates: dict = {}
    if new_slug is not None:
        if not new_slug.strip():
            raise ValueError("new_slug cannot be empty")
        updates["slug"] = new_slug
    if new_name is not None:
        if not new_name.strip():
            raise ValueError("new_name cannot be empty")
        updates["name"] = new_name
    if new_description is not None:
        updates["description"] = new_description
    if new_path is not None:
        updates["path"] = new_path

    if not updates:
        raise ValueError("update_project: no fields supplied (pass at least one new_* kwarg)")

    updates["updated_at"] = datetime.now(UTC)

    with engine.begin() as conn:
        current = conn.execute(
            sa.select(projects).where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
        ).first()
        if not current:
            return None

        if new_slug is not None and new_slug != slug:
            collision = conn.execute(
                sa.select(projects.c.id).where(
                    projects.c.slug == new_slug, projects.c.deleted_at.is_(None)
                )
            ).first()
            if collision:
                raise ValueError(f"Project slug '{new_slug}' already exists")

        conn.execute(
            projects.update()
            .where(projects.c.id == current._mapping["id"])
            .values(**updates)
        )

    return get_project(engine, new_slug or slug)


def resolve_project_id(conn: sa.Connection, slug: str) -> int:
    """Get project ID by slug. Raises ValueError if not found.

    Shared helper used by items / changelog / sprint / handoff core modules
    inside an already-open connection, so they avoid opening nested engine
    transactions. Public (no underscore) because its callers live outside
    this module.
    """
    row = conn.execute(
        sa.select(projects.c.id).where(projects.c.slug == slug, projects.c.deleted_at.is_(None))
    ).first()
    if not row:
        raise ValueError(f"Project '{slug}' not found")
    return row._mapping["id"]
