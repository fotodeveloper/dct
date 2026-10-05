"""Changelog CRUD operations with soft deletes.

Branch metadata (#801): every entry may carry the git `branch` / `worktree` it
was written on and a `branch_source` ('client' | 'server') naming who supplied
them. `resolve_branch_fields` is the ONE rule deciding those three values for a
new entry (used by the MCP server and the CLI alike); `release_entries` can
stamp an explicit set of entry ids instead of every unreleased entry.
"""

import os
from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.text import item_label
from dct.db import changelog_entries, items, project_transaction, projects, row_to_dict

VALID_CATEGORIES = {"added", "changed", "fixed", "removed"}
BRANCH_SOURCES = ("client", "server")

# Nullable fields update_entry may clear (pass None for them).
_NULLABLE_FIELDS = frozenset({
    "component", "source_file", "source_lines", "related_item_id", "branch", "worktree",
})
_UPDATABLE_FIELDS = _NULLABLE_FIELDS | {"entry", "category"}


def _validate_category(category: str) -> None:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: '{category}'. Valid: {', '.join(sorted(VALID_CATEGORIES))}")


def _project_path(engine: sa.Engine, project_slug: str) -> str | None:
    with engine.begin() as conn:
        return conn.execute(
            sa.select(projects.c.path).where(
                projects.c.slug == project_slug, projects.c.deleted_at.is_(None)
            )
        ).scalar()


def _fits(column: str, value: str | None) -> str | None:
    """`value` when it fits `changelog_entries.<column>`, else None."""
    try:
        ensure_fits(changelog_entries, {column: value})
    except ValueError:
        return None
    return value


def detect_checkout(engine: sa.Engine, project_slug: str, cwd: str) -> dict | None:
    """git_context(cwd) when its worktree lies inside the project's path, else None.

    Both sides are realpath-canonical. A project without a registered path, an
    unknown slug, a directory outside git, or a checkout OUTSIDE the project
    (the MCP server started elsewhere, a worktree created outside the repo)
    all give None — git context that cannot be tied to the project is unknown.
    """
    from dct.core.gitinfo import git_context
    from dct.core.projects import _path_is_within

    path = _project_path(engine, project_slug)
    if not path:
        return None
    context = git_context(cwd)
    if context is None:
        return None
    if not _path_is_within(os.path.realpath(context["worktree"]), os.path.realpath(path)):
        return None
    return context


def resolve_branch_fields(
    engine: sa.Engine,
    project_slug: str,
    *,
    branch: str | None = None,
    worktree: str | None = None,
    cwd: str | None = None,
    detected_source: str = "client",
) -> dict:
    """Decide `{branch, worktree, branch_source}` for a NEW entry (#801, C3).

    - Explicit `branch` and/or `worktree` from the caller win: stored as given
      with branch_source='client'; an over-long explicit value is REFUSED
      (ValueError from ensure_fits, never truncated). A value not given stays
      NULL — explicit values are never mixed with detected ones.
    - Neither given: detect via `detect_checkout(cwd)` (git context tied to
      the project's path) and mark it `detected_source` — 'server' for the
      MCP server's own cwd, 'client' for the CLI (which runs in the caller's
      directory). An auto-detected value that is too long becomes NULL (it
      never blocks the entry). No usable context → all three NULL.
    """
    if detected_source not in BRANCH_SOURCES:
        raise ValueError(f"Invalid branch_source: '{detected_source}'. Valid: {', '.join(BRANCH_SOURCES)}")
    if branch or worktree:
        values = {"branch": branch or None, "worktree": worktree or None}
        ensure_fits(changelog_entries, values)
        return {**values, "branch_source": "client"}
    context = detect_checkout(engine, project_slug, cwd) if cwd else None
    if context is None:
        return {"branch": None, "worktree": None, "branch_source": None}
    detected = {
        "branch": _fits("branch", context["branch"]),
        "worktree": _fits("worktree", context["worktree"]),
    }
    if detected["branch"] is None and detected["worktree"] is None:
        return {**detected, "branch_source": None}
    return {**detected, "branch_source": detected_source}


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
    branch: str | None = None,
    worktree: str | None = None,
    branch_source: str | None = None,
) -> dict:
    """Insert a changelog entry and return the full row.

    `branch` / `worktree` are stored as given (see resolve_branch_fields for
    the detection rule callers apply first); `branch_source` defaults to
    'client' when either is set. Over-long String(N) values are refused.
    """
    if not entry or not entry.strip():
        raise ValueError("Changelog entry cannot be empty")
    _validate_category(category)
    if branch_source is None and (branch or worktree):
        branch_source = "client"
    if branch_source is not None and branch_source not in BRANCH_SOURCES:
        raise ValueError(f"Invalid branch_source: '{branch_source}'. Valid: {', '.join(BRANCH_SOURCES)}")
    values = {
        "version": version,
        "category": category,
        "entry": entry,
        "component": component,
        "source_file": source_file,
        "source_lines": source_lines,
        "related_item_id": related_item_id,
        "branch": branch,
        "worktree": worktree,
        "branch_source": branch_source,
    }
    ensure_fits(changelog_entries, values)

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        result = conn.execute(
            changelog_entries.insert()
            .values(project_id=project_id, **values)
            .returning(changelog_entries)
        )
        return row_to_dict(result.first())


def _branch_conditions(
    branch: str | None, worktree: str | None, include_unbranched: bool
) -> list:
    """WHERE fragments of the branch / worktree filters (#801, C4).

    No `branch` → no branch condition at all (include_unbranched is ignored),
    so unfiltered callers see exactly what they saw before #801. `worktree`
    is an exact match and combines with either.
    """
    c = changelog_entries.c
    conditions = []
    if branch:
        cond = c.branch == branch
        if include_unbranched:
            cond = sa.or_(cond, c.branch.is_(None))
        conditions.append(cond)
    if worktree:
        conditions.append(c.worktree == worktree)
    return conditions


def list_entries(
    engine: sa.Engine,
    project_slug: str,
    version: str | None = None,
    unreleased_only: bool = False,
    *,
    branch: str | None = None,
    worktree: str | None = None,
    include_unbranched: bool = False,
) -> list[dict]:
    """Live entries of a project, newest first.

    `unreleased_only` wins over `version`. Branch filters: see
    _branch_conditions. Each row also carries `related_item_short_title` —
    item_label() of the related item, None when there is none.
    """
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        q = (
            sa.select(
                changelog_entries,
                items.c.short_title.label("_item_short_title"),
                items.c.title.label("_item_title"),
            )
            .select_from(
                changelog_entries.outerjoin(
                    items, items.c.id == changelog_entries.c.related_item_id
                )
            )
            .where(changelog_entries.c.project_id == pid, changelog_entries.c.deleted_at.is_(None))
            .order_by(changelog_entries.c.created_at.desc(), changelog_entries.c.id.desc())
        )
        if unreleased_only:
            q = q.where(changelog_entries.c.version.is_(None))
        elif version:
            q = q.where(changelog_entries.c.version == version)
        for cond in _branch_conditions(branch, worktree, include_unbranched):
            q = q.where(cond)
        rows = []
        for r in conn.execute(q):
            row = row_to_dict(r)
            short_title = row.pop("_item_short_title")
            title = row.pop("_item_title")
            row["related_item_short_title"] = (
                item_label(short_title, title) if row["related_item_id"] else None
            )
            rows.append(row)
        return rows


def count_unreleased(
    engine: sa.Engine,
    project_slug: str,
    *,
    branch: str | None = None,
    worktree: str | None = None,
    include_unbranched: bool = False,
) -> int:
    """Number of live unreleased entries; branch filters as in list_entries."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        result = conn.execute(
            sa.select(sa.func.count())
            .select_from(changelog_entries)
            .where(
                changelog_entries.c.project_id == pid,
                changelog_entries.c.version.is_(None),
                changelog_entries.c.deleted_at.is_(None),
                *_branch_conditions(branch, worktree, include_unbranched),
            )
        )
        return result.scalar() or 0


def _version_key(version: str) -> tuple[int, ...] | None:
    """Comparable form of a version (trailing zeros dropped), None if unparsable."""
    from dct.core.version import parse_version

    try:
        parts = list(parse_version(version))
    except (ValueError, AttributeError):
        return None
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _dedupe_ids(entry_ids: Iterable[int]) -> list[int]:
    ids: list[int] = []
    for raw in entry_ids:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError(f"entry_ids must be integers, got {raw!r}")
        if raw not in ids:
            ids.append(raw)
    return ids


def release_entries(
    engine: sa.Engine,
    project_slug: str,
    version: str | None = None,
    *,
    bump_type: str | None = None,
    entry_ids: Iterable[int] | None = None,
) -> dict:
    """Stamp changelog entries with a version and set the project's current_version.

    Exactly one of `version` / `bump_type` (patch | minor | major, bumped from
    current_version INSIDE the project write transaction). `entry_ids`:
      - None → every unreleased entry of the project (legacy, mode 'all');
      - []   → stamp nothing, only set the version (mode 'version_only');
      - list → exactly those entries (mode 'selected'); STRICT — an unknown,
        foreign or soft-deleted id, or one already released in ANOTHER
        version, raises ValueError and nothing is written; a non-int id raises
        TypeError. Duplicates are ignored.

    Order of checks (K-20): (1) nothing left to stamp — every selected entry
    already carries V ('selected'), current_version already V
    ('version_only'), or both ('all') → no-op, NO write at all; (2) V lower
    than current_version (compared only when both parse) → refused, releases
    never rewind; (3) stamp the not-yet-stamped entries (version, released_at,
    updated_at) and set current_version = V.

    Returns {version, current_version, mode, entry_ids, released (count newly
    stamped), already_released (selected ids that already had V)}, plus
    `warning` in mode 'all' when the stamped entries span several branches
    (NULL counts as its own value).
    """
    from dct.core.sprints import _validate_project_rows_conn
    from dct.core.version import bump

    if bool(version) == bool(bump_type):
        raise ValueError("Provide exactly one of version or bump_type (patch/minor/major)")
    ids = None if entry_ids is None else _dedupe_ids(entry_ids)
    mode = "all" if ids is None else ("version_only" if not ids else "selected")
    c = changelog_entries.c

    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        current = conn.execute(
            sa.select(projects.c.current_version).where(projects.c.id == pid)
        ).scalar()
        if bump_type:
            if not current:
                raise ValueError(
                    f"No current version for '{project_slug}'. Provide explicit version."
                )
            version = bump(current, bump_type)
        assert version is not None

        already: list[int] = []
        branches: set[str | None] = set()
        if mode == "all":
            rows = conn.execute(
                sa.select(c.id, c.branch)
                .where(c.project_id == pid, c.version.is_(None), c.deleted_at.is_(None))
                .order_by(c.id)
            ).all()
            to_stamp = [r.id for r in rows]
            branches = {r.branch for r in rows}
            selected = to_stamp
            nothing_to_do = not to_stamp and current == version
        elif mode == "version_only":
            to_stamp, selected = [], []
            nothing_to_do = current == version
        else:
            _validate_project_rows_conn(
                conn, changelog_entries, pid, ids, "changelog entry", "nothing released."
            )
            stamped = dict(conn.execute(
                sa.select(c.id, c.version).where(c.id.in_(ids))
            ).all())
            foreign = {i: stamped[i] for i in ids if stamped[i] not in (None, version)}
            if foreign:
                listing = ", ".join(f"#{i} ({v})" for i, v in sorted(foreign.items()))
                raise ValueError(
                    f"Changelog entries already released in another version: {listing} "
                    f"— released entries are history; nothing released."
                )
            already = [i for i in ids if stamped[i] == version]
            to_stamp = [i for i in ids if stamped[i] is None]
            selected = ids
            nothing_to_do = not to_stamp

        result = {
            "version": version,
            "current_version": current,
            "mode": mode,
            "entry_ids": list(selected),
            "released": 0,
            "already_released": already,
        }
        if nothing_to_do:
            return result

        new_key, current_key = _version_key(version), _version_key(current or "")
        if new_key is not None and current_key is not None and new_key < current_key:
            raise ValueError(
                f"Version {version} is lower than the current version {current} of "
                f"'{project_slug}' — releases never rewind; nothing released."
            )

        now = datetime.now(UTC)
        released = 0
        if to_stamp:
            released = conn.execute(
                changelog_entries.update()
                .where(
                    c.project_id == pid,
                    c.id.in_(to_stamp),
                    c.version.is_(None),
                    c.deleted_at.is_(None),
                )
                .values(version=version, released_at=now, updated_at=now)
            ).rowcount
        conn.execute(
            projects.update()
            .where(projects.c.id == pid)
            .values(current_version=version, updated_at=now)
        )
        result["released"] = released
        result["current_version"] = version
        if mode == "all" and len(branches) > 1:
            names = ", ".join(sorted(b if b is not None else "(unbranched)" for b in branches))
            result["warning"] = (
                f"Released entries span {len(branches)} branches: {names}. "
                f"Pass entry_ids to release only the entries proven to be in this release."
            )
        return result


def _entry_state_error(conn: sa.Connection, project_id: int, entry_id: int) -> ValueError:
    """Explain why a conditional write on an unreleased entry matched no row."""
    row = conn.execute(
        sa.select(changelog_entries.c.version).where(
            changelog_entries.c.id == entry_id,
            changelog_entries.c.project_id == project_id,
            changelog_entries.c.deleted_at.is_(None),
        )
    ).first()
    if row is None:
        return ValueError(
            f"Changelog entry #{entry_id} not found in this project (unknown, "
            f"another project's, or deleted)"
        )
    return ValueError(
        f"Changelog entry #{entry_id} is already released ({row.version}) — "
        f"released entries are history and cannot be changed"
    )


def update_entry(engine: sa.Engine, project_slug: str, entry_id: int, **fields) -> dict:
    """Amend an UNRELEASED entry of the project in place and return the row.

    Accepted fields: entry, category (validated), component, source_file,
    source_lines, related_item_id, branch, worktree. A value of None CLEARS a
    nullable field (all but entry / category). Setting branch or worktree
    also sets branch_source='client'. One conditional UPDATE (live,
    unreleased, this project) — raises ValueError when no field is given, on
    an empty entry, an invalid category, an over-long value, an unknown /
    foreign / deleted entry, or an already released one (distinct messages).
    """
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown changelog field(s): {', '.join(sorted(unknown))}")
    if not fields:
        raise ValueError(
            "Nothing to update — pass entry, category, component, source_file, "
            "source_lines, related_item_id, branch or worktree"
        )
    values = dict(fields)
    if "entry" in values and (not values["entry"] or not str(values["entry"]).strip()):
        raise ValueError("Changelog entry cannot be empty")
    if "category" in values:
        _validate_category(values["category"])
    if "branch" in values or "worktree" in values:
        values["branch_source"] = "client"
    ensure_fits(changelog_entries, values)
    values["updated_at"] = datetime.now(UTC)

    c = changelog_entries.c
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        row = conn.execute(
            changelog_entries.update()
            .where(
                c.id == entry_id,
                c.project_id == pid,
                c.version.is_(None),
                c.deleted_at.is_(None),
            )
            .values(**values)
            .returning(changelog_entries)
        ).first()
        if row is None:
            raise _entry_state_error(conn, pid, entry_id)
        return row_to_dict(row)


def delete_entry(engine: sa.Engine, project_slug: str, entry_id: int) -> bool:
    """Soft-delete an UNRELEASED entry of the project; True on success.

    One conditional UPDATE (deleted_at, updated_at). Raises ValueError for an
    unknown / foreign / already deleted entry and, separately, for a released
    one — released entries are history.
    """
    now = datetime.now(UTC)
    c = changelog_entries.c
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        result = conn.execute(
            changelog_entries.update()
            .where(
                c.id == entry_id,
                c.project_id == pid,
                c.version.is_(None),
                c.deleted_at.is_(None),
            )
            .values(deleted_at=now, updated_at=now)
        )
        if result.rowcount == 0:
            raise _entry_state_error(conn, pid, entry_id)
        return True
