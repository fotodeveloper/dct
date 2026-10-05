"""Durable release records (K-41) — the ONE implementation.

A release of a project version leaves a record in `releases`: the exact entry
set (`entry_ids`, [] for a version-only release), the mode, the pinned
candidate commit (`candidate_sha`), the checkout the release runs in
(`checkout_root`) and a forward-only `status`:

    stamped → files_written → committed → tagged → completed

The record is INSERTED by changelog.release_entries in the same transaction
as the stamp (insert_record — never a stamp without a record, never a record
without a stamp) and ADVANCED by the release workflow (advance_release) after
it verified each effect. get_release reads it back, also "the latest
unfinished release" so an interrupted /release can resume.

At most one LIVE record per (project, version) — compared under the version
contract (1.5 == 1.5.0), enforced here under the project write lock, with a
partial unique index as the last guard.
"""

import json
import re
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.projects import resolve_project_id
from dct.core.version import normalize_version, same_version
from dct.db import project_transaction, releases

RELEASE_STATUSES = ("stamped", "files_written", "committed", "tagged", "completed")
RELEASE_MODES = ("all", "selected", "version_only")

_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}", re.ASCII)
_COMMITTED = RELEASE_STATUSES.index("committed")

RECORD_FIELDS = (
    "id", "version", "mode", "entry_ids", "candidate_sha", "checkout_root",
    "status", "created_at", "updated_at", "completed_at",
)


def validate_candidate_sha(sha: str | None) -> str | None:
    """None / "" → None; a hex commit id of 7-64 chars → lowercased; else ValueError."""
    if not sha:
        return None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise ValueError(
            f"candidate_sha must be a hex commit id of 7-64 characters "
            f"(`git rev-parse HEAD`), got {sha!r}"
        )
    return sha.lower()


def _validate_status(status: str) -> int:
    if status not in RELEASE_STATUSES:
        raise ValueError(
            f"Invalid release status: '{status}'. Valid (in order): {', '.join(RELEASE_STATUSES)}"
        )
    return RELEASE_STATUSES.index(status)


def record_to_dict(row) -> dict:
    """The public record shape (RECORD_FIELDS); `entry_ids` decoded to list[int]."""
    data = dict(row._mapping)
    record = {field: data[field] for field in RECORD_FIELDS}
    record["entry_ids"] = json.loads(data["entry_ids"])
    return record


def find_record(conn: sa.Connection, project_id: int, version: str):
    """The LIVE record of `version` (version contract equality), or None."""
    rows = conn.execute(
        sa.select(releases).where(
            releases.c.project_id == project_id, releases.c.deleted_at.is_(None)
        ).order_by(releases.c.id)
    ).all()
    for row in rows:
        if same_version(row.version, version):
            return row
    return None


def insert_record(
    conn: sa.Connection,
    project_id: int,
    *,
    version: str,
    mode: str,
    entry_ids: list[int],
    candidate_sha: str | None,
    checkout_root: str | None,
    now: datetime,
) -> dict:
    """Insert a 'stamped' record on the caller's (stamping) connection.

    Callers have validated every value and checked find_record under the same
    project write lock; the partial unique index still refuses a duplicate.
    """
    if mode not in RELEASE_MODES:
        raise ValueError(f"Invalid release mode: '{mode}'")
    row = conn.execute(
        releases.insert().values(
            project_id=project_id,
            version=version,
            mode=mode,
            entry_ids=json.dumps(sorted(entry_ids)),
            candidate_sha=candidate_sha,
            checkout_root=checkout_root,
            status=RELEASE_STATUSES[0],
            created_at=now,
            updated_at=now,
        ).returning(releases)
    ).first()
    return record_to_dict(row)


def get_release(engine: sa.Engine, project_slug: str, version: str | None = None) -> dict | None:
    """The live release record of `version`, or None.

    Without `version`: the most recent live record whose status is not
    'completed' — an unfinished release a workflow should resume — else None.
    `version` follows the version contract (ValueError "Unsupported version").
    Read-only.
    """
    if version:
        version = normalize_version(version)
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        if version:
            row = find_record(conn, pid, version)
        else:
            row = conn.execute(
                sa.select(releases).where(
                    releases.c.project_id == pid,
                    releases.c.deleted_at.is_(None),
                    releases.c.status != "completed",
                ).order_by(releases.c.created_at.desc(), releases.c.id.desc()).limit(1)
            ).first()
        return record_to_dict(row) if row is not None else None


def advance_release(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    status: str,
    *,
    candidate_sha: str | None = None,
) -> dict:
    """Move the release record of `version` FORWARD to `status`.

    Inside project_transaction(write=True). Several steps at once are allowed
    (stamped → committed when the intermediate effect is verified as already
    done); the same status again is an idempotent no-op; a lower status
    raises ValueError ("backwards") and so do an unknown status and a version
    without a live record. Reaching 'completed' stamps completed_at.

    `candidate_sha` (optional, validated like on release) re-pins the
    candidate commit while the STORED status is below 'committed' — the
    workflow pins its own release commit on the move into 'committed'; once
    the record is 'committed' or later, a different SHA is refused (passing
    the stored one is fine). Nothing is written on error.

    Returns {"release": <record>, "updated": bool} — `updated` False when
    nothing changed (idempotent repeat).
    """
    target = _validate_status(status)
    version = normalize_version(version)
    sha = validate_candidate_sha(candidate_sha)
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        row = find_record(conn, pid, version)
        if row is None:
            raise ValueError(
                f"No release record for version {version} in '{project_slug}' — "
                f"release_changelog writes it when it stamps the version."
            )
        stored = RELEASE_STATUSES.index(row.status)
        if target < stored:
            raise ValueError(
                f"Release {row.version} is already '{row.status}' — status moves "
                f"forward only ({' → '.join(RELEASE_STATUSES)}); refusing to go "
                f"backwards to '{status}'."
            )
        values: dict = {}
        if target > stored:
            values["status"] = status
        if sha is not None and sha != row.candidate_sha:
            if stored >= _COMMITTED:
                raise ValueError(
                    f"candidate_sha cannot change once release {row.version} is "
                    f"'{row.status}' (pinned {row.candidate_sha}); nothing written."
                )
            values["candidate_sha"] = sha
        if not values:
            return {"release": record_to_dict(row), "updated": False}
        now = datetime.now(UTC)
        values["updated_at"] = now
        if status == "completed" and row.completed_at is None:
            values["completed_at"] = now
        updated = conn.execute(
            releases.update().where(releases.c.id == row.id).values(**values)
            .returning(releases)
        ).first()
        return {"release": record_to_dict(updated), "updated": True}
