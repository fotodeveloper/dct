"""Changelog CRUD operations with soft deletes.

Branch metadata (#801): every entry may carry the git `branch` / `worktree` it
was written on and a `branch_source` ('client' | 'server') naming who supplied
them. `resolve_branch_fields` is the ONE rule deciding those three values for a
new entry (used by the MCP server and the CLI alike); `release_entries` can
stamp an explicit set of entry ids instead of every unreleased entry.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.text import item_label
from dct.core.version import (
    bump,
    compare_versions,
    normalize_version,
    parse_version,
    same_version,
)
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


def resolve_branch_fields(
    engine: sa.Engine,
    project_slug: str,
    *,
    branch: str | None = None,
    worktree: str | None = None,
    cwd: str | None = None,
    use_process_cwd: bool = False,
    detected_source: str = "client",
) -> dict:
    """Decide `{branch, worktree, branch_source}` for a NEW entry (#801, C3).

    - Explicit `branch` and/or `worktree` from the caller win: stored as given
      with branch_source='client'; an over-long explicit value is REFUSED
      (ValueError from ensure_fits, never truncated). A value not given stays
      NULL — explicit values are never mixed with detected ones.
    - Neither given: detect via gitinfo.resolve_checkout(project path, cwd)
      (K-34) — `cwd` must be a checkout of the SAME repository as the
      project's registered path (same git common dir: the main checkout or
      any linked worktree, also one outside the registered path; never a
      nested foreign repo or a submodule). Stored: `branch` of that checkout
      and `worktree` = its repo root (the checkout toplevel, also for a
      monorepo subproject). Marked `detected_source` — 'server' for the MCP
      server's own cwd, 'client' for the CLI (which runs in the caller's
      directory). An auto-detected value that is too long becomes NULL (it
      never blocks the entry). No usable context → all three NULL.
    - `cwd` None and `use_process_cwd` → the process cwd, read only when
      detection actually runs; a deleted cwd means no context. `cwd` None
      without the flag → no detection.
    The project itself is never inferred here: this ties git context to an
    ALREADY RESOLVED project slug.
    """
    if detected_source not in BRANCH_SOURCES:
        raise ValueError(f"Invalid branch_source: '{detected_source}'. Valid: {', '.join(BRANCH_SOURCES)}")
    if branch or worktree:
        values = {"branch": branch or None, "worktree": worktree or None}
        ensure_fits(changelog_entries, values)
        return {**values, "branch_source": "client"}
    context = None
    if cwd or use_process_cwd:
        from dct.core.gitinfo import resolve_checkout

        context = resolve_checkout(_project_path(engine, project_slug), cwd or None)
    if context is None:
        return {"branch": None, "worktree": None, "branch_source": None}
    detected = {
        "branch": _fits("branch", context["branch"]),
        "worktree": _fits("worktree", context["repo_root"]),
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
    require_entries: bool = False,
) -> dict:
    """Stamp changelog entries with a version and set the project's current_version.

    Exactly one of `version` / `bump_type` (patch | minor | major, bumped from
    current_version INSIDE the project write transaction). Versions follow the
    ONE contract of dct.core.version (K-44): X.Y or X.Y.Z, a leading `v` is
    accepted and NORMALIZED AWAY before any comparison and before storing
    (`v1.6.0` is stored as `1.6.0`); any other format raises ValueError
    ("Unsupported version ...") and nothing is written. `entry_ids`:
      - None → every unreleased entry of the project (legacy, mode 'all');
      - []   → stamp nothing, only set the version (mode 'version_only');
      - list → exactly those entries (mode 'selected'); STRICT — an unknown,
        foreign or soft-deleted id, or one already released in ANOTHER
        version, raises ValueError and nothing is written; a non-int id raises
        TypeError. Duplicates are ignored.
    `require_entries` (mode 'all' only — the CLI's consent rule, K-31): when
    nothing is unreleased and the call is not a no-op, raise ValueError
    instead of silently performing a version-only release (that one needs
    entry_ids=[] explicitly).

    Order of checks (K-20): (1) nothing left to stamp → no-op, NO write at
    all: every selected entry already carries V ('selected'); current_version
    already V ('version_only'); nothing unreleased AND V is current_version or
    a version live entries of the project already carry — a historical retry
    ('all'). A historical no-op leaves current_version untouched, so the
    result's `current_version` differs from `version`. (2) V lower than
    current_version → refused, releases never rewind (applied whenever
    current_version parses; a legacy unparseable current_version is not
    compared). (3) stamp the not-yet-stamped entries (version, released_at,
    updated_at) and set current_version = V. Versions are equal when their
    components match after dropping trailing zeros (1.5 == 1.5.0).

    Returns {version (requested, normalized), current_version (after the
    call), mode, entry_ids, released (count newly stamped), already_released
    (selected ids that already had V)}, plus `warning` in mode 'all' when the
    stamped entries span several branches (NULL counts as its own value).
    """
    from dct.core.records import validate_project_rows

    if bool(version) == bool(bump_type):
        raise ValueError("Provide exactly one of version or bump_type (patch/minor/major)")
    if version:
        version = normalize_version(version)
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
        is_current = same_version(current, version)

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
            nothing_to_do = not to_stamp and (
                is_current or _version_was_released(conn, pid, version)
            )
            if not to_stamp and not nothing_to_do and require_entries:
                raise ValueError(
                    f"No unreleased entries for '{project_slug}' — a version-only "
                    f"release of {version} must be explicit (entry_ids=[] / "
                    f"--no-entries); nothing released."
                )
        elif mode == "version_only":
            to_stamp, selected = [], []
            nothing_to_do = is_current
        else:
            validate_project_rows(
                conn, changelog_entries, pid, ids, "changelog entry", "nothing released."
            )
            stamped = dict(conn.execute(
                sa.select(c.id, c.version).where(c.id.in_(ids))
            ).all())
            foreign = {
                i: stamped[i] for i in ids
                if stamped[i] is not None and not same_version(stamped[i], version)
            }
            if foreign:
                listing = ", ".join(f"#{i} ({v})" for i, v in sorted(foreign.items()))
                raise ValueError(
                    f"Changelog entries already released in another version: {listing} "
                    f"— released entries are history; nothing released."
                )
            already = [i for i in ids if stamped[i] is not None]
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

        if current and _parses(current) and compare_versions(version, current) < 0:
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


def _parses(version: str) -> bool:
    try:
        parse_version(version)
    except ValueError:
        return False
    return True


def _version_was_released(conn: sa.Connection, project_id: int, version: str) -> bool:
    """Whether a live entry of the project already carries `version` (under
    the version contract — a legacy `v1.1.0` row matches `1.1.0`)."""
    c = changelog_entries.c
    released = conn.execute(
        sa.select(c.version).distinct().where(
            c.project_id == project_id, c.version.is_not(None), c.deleted_at.is_(None)
        )
    ).scalars()
    return any(same_version(v, version) for v in released)


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
