"""Changelog CRUD operations with soft deletes.

Branch metadata (#801): every entry may carry the git `branch` / `worktree` it
was written on and a `branch_source` ('client' | 'server') naming who supplied
them. `resolve_branch_fields` is the ONE detection rule for a new entry (used
by the MCP server and the CLI alike), `decide_branch_fields` the ONE
provenance rule for every write (add and update, K-42) and
`normalize_worktree` the ONE path contract for stored values and the
`worktree` filter; `release_entries` can stamp an explicit set of entry ids
instead of every unreleased entry.

A 'server' source is UNCERTAIN (K-36): it names the MCP server's own
checkout, not a place the client confirmed. Filters therefore follow one
explicit contract (see _branch_conditions): a plain `branch` filter matches
the STORED branch exactly whatever its source, while `include_unbranched`
treats a server-detected row like an unbranched one.
"""

import hashlib
import json
import ntpath
import posixpath
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core import records
from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.text import item_label
from dct.core.version import (
    SUPPORTED_FORMATS,
    bump,
    compare_versions,
    ensure_version_fits,
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


_WINDOWS_DRIVE_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]", re.ASCII)
_WINDOWS_UNC = re.compile(r"\\\\[^\\/]+[\\/][^\\/]+")


def normalize_worktree(path: str | None) -> str | None:
    """The ONE worktree path contract (K-42, K-64) — stored values and the filter.

    None / "" → None. Anything else must be an ABSOLUTE path in the CLIENT's
    syntax, detected from the text itself (the server may run on another OS
    than the client):
      - POSIX: starts with `/` → posixpath.normpath;
      - Windows drive: `C:\\...` or `C:/...` → ntpath.normpath (separators
        become `\\`);
      - Windows UNC: `\\\\server\\share...` → ntpath.normpath.
    Everything else raises ValueError ("worktree must be an absolute path
    ..."): a relative or `~` path, a drive-relative `C:x`, a rooted path
    without a drive (`\\x`) or a UNC prefix without a share are ambiguous on
    the server. Normalization is purely textual (it also drops a trailing
    separator, except for a root) with NO filesystem access: a client path
    may live in another filesystem namespace than this server, so it is
    never resolved with realpath, never checked for existence. (Values the
    server detects itself come from gitinfo, already realpath-canonical on
    this machine; a release checkout is verified separately, K-52.)
    Historical rows are never rewritten; only new writes and filter
    arguments pass through here.
    """
    if not path:
        return None
    if path.startswith("/"):
        return posixpath.normpath(path)
    if _WINDOWS_DRIVE_ABSOLUTE.match(path) or _WINDOWS_UNC.match(path):
        return ntpath.normpath(path)
    raise ValueError(
        f"worktree must be an absolute path (the checkout toplevel, "
        f"`git rev-parse --show-toplevel`), got {path!r}"
    )


def decide_branch_fields(
    provided: Mapping[str, str | None],
    existing: Mapping[str, str | None] | None = None,
    source: str = "client",
) -> dict:
    """The ONE provenance rule for `{branch, worktree, branch_source}` (K-42).

    `provided` holds only the branch / worktree keys the caller ACTUALLY
    passed (None value = an explicit clear); omitting a key is never a clear.
    `existing` is the row's current triple (None for a new entry), `source`
    the provenance to record for the resulting values.
      - A field not provided keeps its existing value — EXCEPT when the
        existing row is server-detected: a server value is unconfirmed, so a
        caller who confirms or changes only ONE field drops the other
        (cleared to NULL) unless it is provided too. A 'server' triple is
        never relabelled 'client' without the client supplying it.
      - Resulting branch and worktree both NULL → branch_source NULL;
        otherwise `source`.
    The worktree passes through normalize_worktree (relative → ValueError).
    Returns all three keys.
    """
    if source not in BRANCH_SOURCES:
        raise ValueError(f"Invalid branch_source: '{source}'. Valid: {', '.join(BRANCH_SOURCES)}")
    base = {"branch": None, "worktree": None}
    if existing is not None and existing.get("branch_source") != "server":
        base = {"branch": existing.get("branch"), "worktree": existing.get("worktree")}
    result = {**base}
    if "branch" in provided:
        result["branch"] = provided["branch"] or None
    if "worktree" in provided:
        result["worktree"] = normalize_worktree(provided["worktree"])
    no_value = result["branch"] is None and result["worktree"] is None
    return {**result, "branch_source": None if no_value else source}


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
      (the worktree through normalize_worktree — textual only, a relative
      path is refused) with branch_source='client'; an over-long explicit
      value is REFUSED
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
        values = {"branch": branch or None, "worktree": normalize_worktree(worktree)}
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
    the detection rule callers apply first) under decide_branch_fields:
    the worktree is normalized (normalize_worktree — a relative path is
    refused), `branch_source` defaults to 'client' and is NULL when both
    values are NULL. `related_item_id` (None = no link) must be a live item
    of the SAME project — checked in the insert transaction (K-37); an
    unknown, foreign or soft-deleted id raises ValueError and nothing is
    written. Over-long String(N) values are refused.
    """
    if not entry or not entry.strip():
        raise ValueError("Changelog entry cannot be empty")
    _validate_category(category)
    values = {
        "version": version,
        "category": category,
        "entry": entry,
        "component": component,
        "source_file": source_file,
        "source_lines": source_lines,
        "related_item_id": related_item_id,
        **decide_branch_fields(
            {"branch": branch, "worktree": worktree}, source=branch_source or "client"
        ),
    }
    ensure_fits(changelog_entries, values)

    with engine.begin() as conn:
        project_id = resolve_project_id(conn, project_slug)
        _validate_related_item(conn, project_id, related_item_id)
        result = conn.execute(
            changelog_entries.insert()
            .values(project_id=project_id, **values)
            .returning(changelog_entries)
        )
        return row_to_dict(result.first())


def _validate_related_item(conn: sa.Connection, project_id: int, item_id: int | None) -> None:
    """K-37: a link must name a live item of this project (None = no link).

    Runs on the writer's connection, so the check and the write share one
    transaction. Non-positive ids are refused the same way (never sent to the
    FK as a raw IntegrityError).
    """
    if item_id is None:
        return
    records.validate_project_rows(
        conn, items, project_id, [item_id], "item", "nothing written."
    )


def _branch_conditions(
    branch: str | None, worktree: str | None, include_unbranched: bool
) -> list:
    """WHERE fragments of the branch / worktree filters (#801 C4, K-36, K-42).

    The ONE filter contract:
      - No `branch` → no branch condition at all (include_unbranched is
        ignored), so unfiltered callers see exactly what they saw before #801.
      - Plain `branch` → the STORED branch equals it exactly, whatever its
        branch_source (a server-detected 'main' row matches branch='main').
      - `branch` + include_unbranched → additionally every row whose branch is
        UNCERTAIN: branch NULL, or branch_source='server' (the MCP server's
        own checkout, never confirmed by the client) whatever branch it
        stores. Rows keep their stored branch / worktree / source.
      - `worktree` → exact match on the stored path after normalize_worktree
        of the argument (relative → ValueError); combines with either.
    """
    c = changelog_entries.c
    conditions = []
    if branch:
        cond = c.branch == branch
        if include_unbranched:
            cond = sa.or_(cond, c.branch.is_(None), c.branch_source == "server")
        conditions.append(cond)
    if worktree:
        conditions.append(c.worktree == normalize_worktree(worktree))
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

    `unreleased_only` wins over `version`; `version` selects every stored
    spelling of that version (stored_spellings — 1.1 == 1.1.0 == v1.1, K-55);
    the rows keep their stored value. Branch filters: see
    _branch_conditions. Each row also carries `item_short_title` (K-38) —
    item_label() of the related item when it is a LIVE item of the same
    project, else None (no link, or a dangling historical id — the stored
    related_item_id itself is never rewritten).
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
                    items,
                    sa.and_(
                        items.c.id == changelog_entries.c.related_item_id,
                        items.c.project_id == changelog_entries.c.project_id,
                        items.c.deleted_at.is_(None),
                    ),
                )
            )
            .where(changelog_entries.c.project_id == pid, changelog_entries.c.deleted_at.is_(None))
            .order_by(changelog_entries.c.created_at.desc(), changelog_entries.c.id.desc())
        )
        if unreleased_only:
            q = q.where(changelog_entries.c.version.is_(None))
        elif version:
            q = q.where(changelog_entries.c.version.in_(stored_spellings(conn, pid, version)))
        for cond in _branch_conditions(branch, worktree, include_unbranched):
            q = q.where(cond)
        rows = []
        for r in conn.execute(q):
            row = row_to_dict(r)
            short_title = row.pop("_item_short_title")
            title = row.pop("_item_title")
            row["item_short_title"] = item_label(short_title, title)
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


# The release-material state of an entry (K-80/K-81): what the changelog
# renders plus whether it is released. A change of any of these between an
# approved plan and its stamp requires a new plan.
ENTRY_STATE_FIELDS = ("id", "category", "entry", "component", "version")


def entry_state_digest(entry: Mapping) -> str:
    """SHA-256 (hex) of the canonical JSON of ENTRY_STATE_FIELDS of one
    entry (a list_entries dict or a row mapping) — the ONE fingerprint the
    release plan's snapshot and the stamp transaction compare."""
    state = {name: entry.get(name) for name in ENTRY_STATE_FIELDS}
    data = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


_DIGEST_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _validate_expected_entries(expected: Mapping | None) -> dict[int, str] | None:
    if expected is None:
        return None
    if not isinstance(expected, Mapping):
        raise TypeError("expected_entries must be an object {entry id: state digest}")
    out: dict[int, str] = {}
    for raw, digest in expected.items():
        key = str(raw)
        if isinstance(raw, bool) or not key.isascii() or not key.isdigit() or int(key) <= 0:
            raise ValueError(f"expected_entries keys must be entry ids, got {raw!r}")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"expected_entries[{key}] must be a lowercase hex SHA-256")
        out[int(key)] = digest
    return out


def _check_plan_binding(
    conn: sa.Connection, pid: int, to_stamp: list[int],
    stampable: list[int] | None, expected: dict[int, str] | None,
) -> None:
    """K-80/K-81, inside the stamp transaction and before any write: the
    live unreleased entries must still be EXACTLY the ones the approved plan
    saw, in the same state (`expected`), and only `stampable` ids (approved
    AND proven by that plan) may receive a new stamp. An entry already
    carrying the version is not stamped again (the legacy set — checked by
    the caller as a whole)."""
    if expected is not None:
        c = changelog_entries.c
        live = {
            row.id: entry_state_digest(row._mapping)
            for row in conn.execute(
                sa.select(*(getattr(c, name) for name in ENTRY_STATE_FIELDS)).where(
                    c.project_id == pid, c.version.is_(None), c.deleted_at.is_(None)))
        }
        added = sorted(set(live) - set(expected))
        removed = sorted(set(expected) - set(live))
        changed = sorted(i for i in set(live) & set(expected) if live[i] != expected[i])
        if added or removed or changed:
            parts = [f"{label}: {', '.join(f'#{i}' for i in ids)}"
                     for label, ids in (("added", added), ("changed", changed),
                                        ("removed or released", removed)) if ids]
            raise ValueError(
                f"the unreleased changelog entries changed since the approved plan "
                f"({'; '.join(parts)}) — run the plan again and approve it; nothing released."
            )
    if stampable is not None:
        outside = sorted(set(to_stamp) - set(stampable))
        if outside:
            raise ValueError(
                f"entries the approved plan does not prove: "
                f"{', '.join(f'#{i}' for i in outside)} — only approved, proven entries get a "
                f"new stamp; run the plan again; nothing released."
            )


def release_entries(
    engine: sa.Engine,
    project_slug: str,
    version: str | None = None,
    *,
    bump_type: str | None = None,
    entry_ids: Iterable[int] | None = None,
    candidate_sha: str | None = None,
    checkout_root: str | None = None,
    proof: Mapping | None = None,
    release_mode: str | None = None,
    consent: Mapping | None = None,
    proposal: Mapping | None = None,
    render: Mapping | None = None,
    stampable_ids: Iterable[int] | None = None,
    expected_entries: Mapping | None = None,
) -> dict:
    """Stamp changelog entries with a version, set the project's current_version
    and write the durable release record (K-41) — ONE transaction.

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
    A NEW release with no entries must be explicit (K-31, K-62 — every
    caller: MCP, CLI, direct core): mode 'all' with nothing unreleased that
    is NOT a no-op raises ValueError ("No unreleased entries ... must be
    explicit") instead of silently performing a version-only release — that
    one needs entry_ids=[]. Decided inside the release transaction (no
    earlier count a concurrent writer could invalidate), AFTER the no-op
    checks, so an existing release (recorded or historical) still resumes.
    An explicit [] states version-only INTENT; it is not proof that a human
    consented (the skills still ask).
    `candidate_sha` (hex commit id, 7-64 chars, stored lowercased) and
    `checkout_root` (ABSOLUTE path, normalize_worktree) are pinned in the
    record; both are validated BEFORE any read or write (ValueError).
    K-61: the candidate_sha of a record is IMMUTABLE — no later call
    re-pins it (core.releases.advance_release records the release commit
    separately as release_commit_sha).
    `proof` (K-70, optional): the proof map {entry id: [proving commit
    SHAs] or {commits, merge_commit, pr}} — shape checked before any read
    (core.releases.validate_proof); every entry it names must be in the
    resolved set (else ValueError); stored in the NEW record only.
    K-94 / K-85 / K-76 / K-90 / K-97 ledger fields of the NEW record, all
    validated before any read (core.releases.validate_*): `release_mode`
    ('git' | 'without_git' — the mode the caller VERIFIED; None = not
    stated, then the record needs a release commit to complete),
    `consent`, `proposal`, `render` (JSON objects; `render` defaults to the
    project's changelog_pr_refs setting at stamp time). The record's
    `executor` is copied from the project's release_executor — never a
    parameter. K-94 B2: a re-run that returns an EXISTING record compares
    every given `proof` / `release_mode` / `consent` / `proposal` / `render`
    with the stored value — identical (or not given) is the no-op, a
    contradicting one raises ValueError (immutable, never silently
    ignored; a re-run never fills a field: a given proof for a record
    without one is refused too). The other four are not compared when the
    record predates them (stored NULL — nothing to contradict; such a
    record stays unverified). candidate_sha is not re-checked (K-61: never
    re-pinned).
    K-80/K-81 plan binding (the effects layer; None = not bound):
    `stampable_ids` — the ids an approved plan selected AND proved; any
    other id that would receive a NEW stamp raises ValueError (an entry
    already carrying V is the legacy set, not stamped again). A missing id
    is never "left to core": it needs a new plan. `expected_entries` —
    {entry id: core.changelog.entry_state_digest} of EVERY live unreleased
    entry the plan saw; the transaction recomputes it and any entry added,
    edited, released or removed since raises ValueError. Both are checked
    inside the stamp transaction, before any write.

    The call's RESOLVED SET: 'selected' → the given ids; 'version_only' → [];
    'all' → the live entries already carrying V plus every unreleased entry.

    Order of checks: (0) V has a live release record (core.releases): the
    resolved set equal to the record's AND to the live entries carrying V
    (K-94 B3, core.releases.check_entry_set) and no contradicting given
    field (B2) → whole-operation no-op that RETURNS the record (resume —
    also when V is lower than current_version, also for a version-only
    release; the record is not re-pinned); a different set → ValueError
    "version V was released with a different set: ..." (for an abandoned
    record the message says so — K-94 B5) and nothing is written. Without a record (an unrecorded legacy release, or
    none yet) — K-20: (1) nothing left to stamp → no-op, NO write at all:
    every selected entry already carries V ('selected'); current_version
    already V ('version_only'); nothing unreleased AND V is current_version
    or a version live entries of the project already carry — a historical
    retry ('all'); `release` is None then. A historical no-op leaves
    current_version untouched, so the result's `current_version` differs
    from `version`. (2) a non-empty current_version that does not parse
    under the contract (a legacy value such as `2.0.0-rc1`) → refused:
    without a parseable baseline the never-rewind guard cannot run (K-54);
    a bump from it is refused the same way. (2b) K-61 — the record's set
    must be EXACTLY every live entry carrying V: in modes 'selected' and
    'version_only' an entry that already carries V (a legacy stamp) but is
    missing from the request → ValueError naming it, BEFORE stamping —
    never a silent extension of the selection ('all' includes them by
    construction). (3) V lower than current_version
    → refused, releases never rewind. (4) mode 'all' with nothing to stamp →
    refused (see above). (5) stamp the
    not-yet-stamped entries (version, released_at, updated_at), set
    current_version = V and insert the record (status 'stamped', entry_ids =
    the resolved set). Versions are equal when their components match after
    dropping trailing zeros (1.5 == 1.5.0).

    The final version (explicit after normalization, or bumped) must fit
    EVERY version column (core.version.ensure_version_fits, K-58 — widths
    from the schema) before anything is written: ValueError "version too
    long" otherwise.

    Returns {version (requested, normalized), current_version (after the
    call), mode, entry_ids, released (count newly stamped), already_released
    (selected ids that already had V), release (the record — see
    core.releases.RECORD_FIELDS — or None for a legacy no-op)}, plus
    `warning` in mode 'all' when the stamped entries span several branches
    (NULL counts as its own value).
    """
    from dct.core import releases as release_records

    if bool(version) == bool(bump_type):
        raise ValueError("Provide exactly one of version or bump_type (patch/minor/major)")
    if version:
        version = normalize_version(version)
    sha = release_records.validate_candidate_sha(candidate_sha)
    proof_map = release_records.validate_proof(proof)
    given = {
        "proof": proof_map,
        "release_mode": release_records.validate_release_mode(release_mode),
        "consent": release_records.validate_consent(consent),
        "proposal": release_records.validate_proposal(proposal),
        "render": release_records.validate_render(render),
    }
    root = normalize_worktree(checkout_root)
    ensure_fits(release_records.releases, {"checkout_root": root})
    stampable = None if stampable_ids is None else _dedupe_ids(stampable_ids)
    expected = _validate_expected_entries(expected_entries)
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
            if not _parses(current):
                raise _unsupported_current(project_slug, current)
            version = bump(current, bump_type)
        assert version is not None
        ensure_version_fits(version)
        is_current = same_version(current, version)
        record = release_records.find_record(conn, pid, version)

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
            carrying = _entries_of_version(conn, pid, version)
            resolved = sorted(set(carrying) | set(to_stamp))
            nothing_to_do = not to_stamp and (is_current or bool(carrying))
        elif mode == "version_only":
            to_stamp, selected, resolved = [], [], []
            nothing_to_do = is_current
        else:
            records.validate_project_rows(
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
            resolved = sorted(ids)
            nothing_to_do = not to_stamp

        result = {
            "version": version,
            "current_version": current,
            "mode": mode,
            "entry_ids": list(selected),
            "released": 0,
            "already_released": already,
            "release": None,
        }
        if record is not None:
            recorded = release_records.record_to_dict(record)
            if set(resolved) != set(recorded["entry_ids"]):
                abandoned = recorded["status"] == release_records.ABANDONED
                raise ValueError(
                    f"version {recorded['version']} was released with a different set: "
                    f"recorded {recorded['entry_ids']} ({recorded['mode']}), requested "
                    f"{resolved} ({mode}) — "
                    + (f"that release was ABANDONED ({recorded['abandon_reason']}); "
                       f"release the work under a NEW version"
                       if abandoned else "a released version is history")
                    + "; nothing released."
                )
            release_records.check_entry_set(conn, pid, record)
            for field, value in given.items():
                if recorded[field] is None and field != "proof":
                    continue  # a record older than the field: nothing to contradict
                release_records.check_unchanged(field, recorded[field], value, may_fill=False)
            result["release"] = recorded
            return result
        if nothing_to_do:
            return result
        if current and not _parses(current):
            raise _unsupported_current(project_slug, current)
        if mode != "all":
            omitted = sorted(set(_entries_of_version(conn, pid, version)) - set(resolved))
            if omitted:
                raise ValueError(
                    f"Changelog entries already carrying {version} are missing from the "
                    f"requested set: {', '.join(f'#{i}' for i in omitted)} — a release "
                    f"record holds EVERY entry of its version; request them too; "
                    f"nothing released."
                )
        release_records.check_proof_scope(proof_map, resolved)
        _check_plan_binding(conn, pid, to_stamp, stampable, expected)
        if current and compare_versions(version, current) < 0:
            raise ValueError(
                f"Version {version} is lower than the current version {current} of "
                f"'{project_slug}' — releases never rewind; nothing released."
            )
        if mode == "all" and not to_stamp:
            raise ValueError(
                f"No unreleased entries for '{project_slug}' — a version-only "
                f"release of {version} must be explicit (entry_ids=[] / "
                f"--no-entries); nothing released."
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
        if given["render"] is None:
            given["render"] = release_records.project_release_settings(conn, pid)["render"]
        result["release"] = release_records.insert_record(
            conn, pid, version=version, mode=mode, entry_ids=resolved,
            candidate_sha=sha, checkout_root=root, now=now, proof=proof_map,
            release_mode=given["release_mode"], consent=given["consent"],
            proposal=given["proposal"], render=given["render"],
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


def _unsupported_current(project_slug: str, current: str) -> ValueError:
    """The controlled refusal for a stored current_version outside the
    contract (K-54): without a parseable baseline the never-rewind guard
    cannot run, so nothing new is stamped."""
    return ValueError(
        f"The current version {current!r} of '{project_slug}' is not a supported "
        f"version ({SUPPORTED_FORMATS}) — releases cannot be ordered against it; "
        f"set a supported one with `dct project set-version`; nothing released."
    )


def _parses(version: str) -> bool:
    try:
        parse_version(version)
    except ValueError:
        return False
    return True


def stored_spellings(conn: sa.Connection, project_id: int, version: str) -> list[str]:
    """Every STORED spelling of `version` among the project's live entries —
    the ONE version selector (K-55): equality is the version contract's
    (same_version: 1.1 == 1.1.0 == v1.1; an unparseable value matches only
    itself), so a filter never misses a legacy spelling. Sorted."""
    c = changelog_entries.c
    stored = conn.execute(
        sa.select(c.version).distinct().where(
            c.project_id == project_id, c.version.is_not(None), c.deleted_at.is_(None)
        )
    ).scalars()
    return sorted(v for v in stored if same_version(v, version))


def _stamp_order(entry: Mapping) -> tuple:
    """Sort key of the legacy spelling rule: earliest released_at first (a
    NULL released_at — time unknown — after every known time), then the
    lowest id. Datetimes compare as naive UTC (SQLite returns naive)."""
    released = entry.get("released_at")
    if released is not None and getattr(released, "tzinfo", None) is not None:
        released = released.astimezone(UTC).replace(tzinfo=None)
    # (False, t, id) for a known time, (True, None, id) for an unknown one:
    # the first element differs between the two, so None never meets a time.
    return (released is None, released, entry["id"])


def spelling_rule(record_versions: Iterable[str], entries: Iterable[Mapping]) -> str | None:
    """The ONE deterministic rule for the authoritative spelling of a version
    (K-86 / K-93 A3), shared by the export, the reconstruction and the
    resolver below — pure, no I/O.

    `entries`: the live entries carrying the version ({id, version,
    released_at}); `record_versions`: versions of live release records.
    1. a live release record of the version → ITS spelling;
    2. else the stored spelling of the FIRST-STAMPED entry: earliest
       released_at (NULL = unknown time, after every known time), ties →
       the lowest entry id;
    3. else None (the caller may fall back to current_version).
    Nothing is rewritten: the other spellings stay as stored."""
    entries = list(entries)
    if not entries:
        return None
    stored = entries[0]["version"]
    for candidate in record_versions:
        if same_version(candidate, stored):
            return candidate
    return min(entries, key=_stamp_order)["version"]


def resolve_version_spelling(conn: sa.Connection, project_id: int, version: str) -> dict:
    """The shared legacy-version resolver (K-86 / K-93 A3): which stored
    spelling is AUTHORITATIVE for `version` (compared under the version
    contract) and which spellings are equivalent to it.

    Returns {requested (normalized), authoritative (stored spelling, or
    None when the database knows no such release), source ("record" |
    "entries" | "current_version" | None), record (the live record's
    version or None), current_version (the project's current_version when
    it equals the version, else None), spellings ({stored spelling: [entry
    ids]} of the live entries carrying it — the equivalence map), equivalent
    (sorted distinct spellings across record, entries and current_version),
    several (bool: more than one spelling exists)}.

    Rule (spelling_rule): the record's spelling, else the first-stamped
    entry's (earliest released_at, NULL last, then lowest id), else
    current_version. The resolver never picks silently among CONFLICTING
    artifacts: callers compare files, sections and tags against
    `equivalent` / `authoritative` and refuse a contradiction. Read-only."""
    from dct.db import releases

    requested = normalize_version(version)
    c = changelog_entries.c
    matching = stored_spellings(conn, project_id, requested)
    rows = [] if not matching else [dict(r._mapping) for r in conn.execute(
        sa.select(c.id, c.version, c.released_at).where(
            c.project_id == project_id, c.version.in_(matching), c.deleted_at.is_(None)
        ).order_by(c.id)
    )]
    record = next((v for v in conn.execute(
        sa.select(releases.c.version).where(
            releases.c.project_id == project_id, releases.c.deleted_at.is_(None)
        ).order_by(releases.c.id)
    ).scalars() if same_version(v, requested)), None)
    current = conn.execute(
        sa.select(projects.c.current_version).where(projects.c.id == project_id)
    ).scalar()
    current = current if same_version(current, requested) else None
    spellings: dict[str, list[int]] = {}
    for row in rows:
        spellings.setdefault(row["version"], []).append(row["id"])
    authoritative = spelling_rule([record] if record else [], rows)
    source = None
    if authoritative is not None:
        source = "record" if record is not None else "entries"
    elif record is not None:
        authoritative, source = record, "record"
    elif current is not None:
        authoritative, source = current, "current_version"
    equivalent = sorted({*spellings, *(v for v in (record, current) if v)})
    return {
        "requested": requested,
        "authoritative": authoritative,
        "source": source,
        "record": record,
        "current_version": current,
        "spellings": {k: spellings[k] for k in sorted(spellings)},
        "equivalent": equivalent,
        "several": len(equivalent) > 1,
    }


def _entries_of_version(conn: sa.Connection, project_id: int, version: str) -> list[int]:
    """Ids of the live entries of the project that already carry `version`
    (under the version contract — a legacy `v1.1.0` row matches `1.1.0`)."""
    c = changelog_entries.c
    matching = stored_spellings(conn, project_id, version)
    if not matching:
        return []
    return list(conn.execute(
        sa.select(c.id).where(
            c.project_id == project_id, c.version.in_(matching), c.deleted_at.is_(None)
        ).order_by(c.id)
    ).scalars())


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


def _load_unreleased_entry(conn: sa.Connection, project_id: int, entry_id: int):
    """The live, unreleased entry of the project under the write lock, or a
    ValueError naming why not (see _entry_state_error)."""
    c = changelog_entries.c
    row = conn.execute(
        sa.select(c.branch, c.worktree, c.branch_source).where(
            c.id == entry_id,
            c.project_id == project_id,
            c.version.is_(None),
            c.deleted_at.is_(None),
        )
    ).first()
    if row is None:
        raise _entry_state_error(conn, project_id, entry_id)
    return row


def update_entry(engine: sa.Engine, project_slug: str, entry_id: int, **fields) -> dict:
    """Amend an UNRELEASED entry of the project in place and return the row.

    Accepted fields: entry, category (validated), component, source_file,
    source_lines, related_item_id, branch, worktree. A value of None CLEARS a
    nullable field (all but entry / category); an omitted field is unchanged.
    `related_item_id` must be a live item of the same project (K-37; None
    unlinks). branch / worktree follow decide_branch_fields (K-42): the
    passed ones are stored with branch_source='client'; a field NOT passed
    is kept — unless the row was server-detected, whose unconfirmed other
    field is CLEARED; both NULL afterwards → branch_source NULL. The worktree
    is normalized (normalize_worktree; relative → refused).

    Runs in project_transaction(write=True) (K-39): on PostgreSQL the
    project's advisory lock is taken BEFORE the snapshot, so an edit and a
    release of the same project serialize — an edit committed first is
    included in the release, an edit arriving after it gets the controlled
    "already released" refusal (the conditional unreleased-only UPDATE stays
    as the last guard). Raises ValueError when no field is given, on an
    empty entry, an invalid category, an over-long value, a relative
    worktree, a bad related item, an unknown / foreign / deleted entry, or
    an already released one (distinct messages). Nothing is written on error.
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
    branch_given = {k: values.pop(k) for k in ("branch", "worktree") if k in values}
    if "worktree" in branch_given:
        branch_given["worktree"] = normalize_worktree(branch_given["worktree"])
    ensure_fits(changelog_entries, {**values, **branch_given})

    c = changelog_entries.c
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        current = _load_unreleased_entry(conn, pid, entry_id)
        if "related_item_id" in values:
            _validate_related_item(conn, pid, values["related_item_id"])
        if branch_given:
            values.update(decide_branch_fields(branch_given, current._mapping))
        values["updated_at"] = datetime.now(UTC)
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

    One conditional UPDATE (deleted_at, updated_at) inside
    project_transaction(write=True), serialized with release exactly like
    update_entry (K-39). Raises ValueError for an unknown / foreign / already
    deleted entry and, separately, for a released one — released entries are
    history.
    """
    now = datetime.now(UTC)
    c = changelog_entries.c
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
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
