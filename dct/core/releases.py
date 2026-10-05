"""Durable release records (K-41, K-60, K-61) — the ONE implementation.

A release of a project version leaves a record in `releases` — the release
LEDGER. It holds the exact entry set (`entry_ids`, [] for a version-only
release), the mode, the commit the release was cut from (`candidate_sha`,
IMMUTABLE once written — K-61), the checkout the release runs in
(`checkout_root`), the release commit (`release_commit_sha`, set once), the
expected artifacts with their content hashes (`manifest`, write-once per
key), the proof map (`proof`: entry id → proving commits, K-70) and a
`status` that follows ONE state machine (TRANSITIONS):

    stamped → files_written → committed → tagged → completed
        └──────────┴─────────────┴──────────┴──→ abandoned

Forward moves may skip steps (the workflow verified the intermediate
effects); 'completed' and 'abandoned' are terminal; a completed release is
never reclassified as abandoned. 'abandoned' is reached ONLY through
abandon_release (explicit confirmation + a reason), never through
advance_release.

The record is INSERTED by changelog.release_entries in the same transaction
as the stamp (insert_record — never a stamp without a record, never a record
without a stamp) or, for a release made before records existed, by
reconstruct_release (no re-stamp: the record is checked against the stamps
already in the database). It is ADVANCED by the release workflow
(advance_release) after it verified each effect. get_release reads it back,
also "the latest unfinished release" so an interrupted /release can resume.

The ledger only stores and cross-checks what the workflow reports; it never
reads files or git (the local workflow does — K-51/K-72).

At most one LIVE record per (project, version) — compared under the version
contract (1.5 == 1.5.0), enforced here under the project write lock, with a
partial unique index as the last guard.

The project's version-file contract (K-73: which repo files a release must
change) lives on the project row; get_version_files / set_version_files are
its readers and writers.
"""

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.version import normalize_version, same_version
from dct.db import project_transaction, projects, releases

# ── the state machine (ONE definition, used everywhere) ──────────────────────

RELEASE_FLOW = ("stamped", "files_written", "committed", "tagged", "completed")
ABANDONED = "abandoned"
RELEASE_STATUSES = (*RELEASE_FLOW, ABANDONED)
TERMINAL_STATUSES = frozenset({"completed", ABANDONED})


def _build_transitions() -> dict[str, frozenset[str]]:
    """status → the statuses it may move to (the same status is a no-op, not
    a transition). Forward along RELEASE_FLOW (multi-step jumps allowed),
    'abandoned' from every non-terminal status, nothing out of a terminal one."""
    table: dict[str, frozenset[str]] = {}
    for index, status in enumerate(RELEASE_FLOW):
        targets = set(RELEASE_FLOW[index + 1:])
        if status not in TERMINAL_STATUSES:
            targets.add(ABANDONED)
        table[status] = frozenset(targets)
    table[ABANDONED] = frozenset()
    return table


TRANSITIONS = _build_transitions()

RELEASE_MODES = ("all", "selected", "version_only", "reconstructed")

_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}", re.ASCII)
_COMMITTED = RELEASE_FLOW.index("committed")
_FILES_WRITTEN = RELEASE_FLOW.index("files_written")

# Size caps for the JSON ledger fields — refused, never truncated (#792).
MANIFEST_MAX_CHARS = 65536
PROOF_MAX_CHARS = 262144
MANIFEST_KEY_MAX = 64
ABANDON_REASON_MAX = 2000
VERSION_FILES_MAX = 200
VERSION_FILE_PATH_MAX = 500

RECORD_FIELDS = (
    "id", "version", "mode", "entry_ids", "candidate_sha", "checkout_root",
    "release_commit_sha", "manifest", "proof", "status", "created_at",
    "updated_at", "completed_at", "abandoned_at", "abandon_reason",
)


def check_transition(current: str, target: str) -> bool:
    """True when `current` → `target` is a transition of the state machine,
    False when target == current (an idempotent no-op); ValueError otherwise
    (unknown status, a move out of a terminal status, a backwards move)."""
    _validate_status(target)
    if target == current:
        return False
    if target in TRANSITIONS.get(current, frozenset()):
        return True
    if current in TERMINAL_STATUSES:
        raise ValueError(
            f"Release is '{current}' — a terminal status; refusing to move it "
            f"to '{target}'."
        )
    raise ValueError(
        f"Release is already '{current}' — status moves forward only "
        f"({' → '.join(RELEASE_FLOW)}); refusing to go backwards to '{target}'."
    )


def _validate_status(status: str) -> None:
    if status not in RELEASE_STATUSES:
        raise ValueError(
            f"Invalid release status: '{status}'. Valid: {', '.join(RELEASE_FLOW)} "
            f"(in order), or '{ABANDONED}' (abandon_release only)"
        )


def _rank(status: str) -> int:
    return RELEASE_FLOW.index(status)


# ── value validation (before any read or write) ─────────────────────────────


def validate_sha(sha: str | None, field: str = "candidate_sha") -> str | None:
    """None / "" → None; a hex commit id of 7-64 chars → lowercased; else ValueError."""
    if not sha:
        return None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise ValueError(
            f"{field} must be a hex commit id of 7-64 characters "
            f"(`git rev-parse <commit>`), got {sha!r}"
        )
    return sha.lower()


def validate_candidate_sha(sha: str | None) -> str | None:
    """validate_sha for the candidate commit (kept for its callers)."""
    return validate_sha(sha, "candidate_sha")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_manifest(manifest: Mapping | None) -> dict | None:
    """None / {} → None; otherwise a JSON object whose keys are non-empty
    strings (≤ MANIFEST_KEY_MAX) and whose values are JSON values other than
    null — the manifest's content is the workflow's (K-49/K-73: e.g.
    {"changelog": {...}, "version_files": [...], "tree": "<sha>"}); the
    ledger only enforces the shape and write-once per key. TypeError for a
    non-object, ValueError for any other violation (also when the canonical
    JSON exceeds MANIFEST_MAX_CHARS)."""
    if manifest is None:
        return None
    if not isinstance(manifest, Mapping):
        raise TypeError(f"manifest must be a JSON object, got {type(manifest).__name__}")
    if not manifest:
        return None
    out: dict = {}
    for key, value in manifest.items():
        if not isinstance(key, str) or not key.strip() or len(key) > MANIFEST_KEY_MAX:
            raise ValueError(
                f"manifest keys must be non-empty strings of at most "
                f"{MANIFEST_KEY_MAX} characters, got {key!r}"
            )
        if value is None:
            raise ValueError(f"manifest[{key!r}] is null — omit a key that has no value")
        out[key] = value
    try:
        encoded = _canonical(out)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"manifest must be JSON-serializable: {exc}") from None
    if len(encoded) > MANIFEST_MAX_CHARS:
        raise ValueError(
            f"manifest too long: {len(encoded)} chars of JSON, max "
            f"{MANIFEST_MAX_CHARS} — refused, never truncated"
        )
    return json.loads(encoded)


def merge_manifest(stored: dict | None, incoming: dict | None) -> tuple[dict | None, bool]:
    """Write-once merge: a new key is added, a key already set must carry the
    SAME value (a confirmation), a different value raises ValueError.
    Returns (merged manifest, changed)."""
    merged = dict(stored or {})
    changed = False
    for key, value in (incoming or {}).items():
        if key in merged:
            if _canonical(merged[key]) != _canonical(value):
                raise ValueError(
                    f"manifest[{key!r}] is already recorded with a different value "
                    f"— a recorded artifact can be confirmed, never replaced; "
                    f"nothing written."
                )
            continue
        merged[key] = value
        changed = True
    return (merged or None), changed


def _entry_id(raw) -> int:
    if isinstance(raw, bool):
        raise TypeError(f"proof keys must be changelog entry ids, got {raw!r}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit():
        value = int(raw)
    else:
        raise ValueError(f"proof keys must be changelog entry ids, got {raw!r}")
    if value <= 0:
        raise ValueError(f"proof keys must be positive entry ids, got {raw!r}")
    return value


def validate_proof(proof: Mapping | None) -> dict | None:
    """The proof map (K-70) in its canonical form, or None for None / {}.

    Input: {entry id (int or digit string): <proof>} where <proof> is either
    a non-empty list of proving commit SHAs (shorthand) or an object
    {"commits": [sha, ...] (non-empty), "merge_commit": sha | null,
    "pr": positive int | null}. Stored as {"<id>": {"commits": [sorted,
    deduplicated, lowercased], "merge_commit": ..., "pr": ...}}. Any other
    shape raises ValueError. Entries without a commit proof (e.g. attested)
    are simply absent from the map. A non-object map / proof value or a
    bool key raises TypeError."""
    if proof is None:
        return None
    if not isinstance(proof, Mapping):
        raise TypeError(f"proof must be a JSON object, got {type(proof).__name__}")
    out: dict[str, dict] = {}
    for raw_id, raw in proof.items():
        entry = _entry_id(raw_id)
        if isinstance(raw, (list, tuple)):
            raw = {"commits": list(raw)}
        if not isinstance(raw, Mapping):
            raise TypeError(
                f"proof[{entry}] must be a list of commit SHAs or an object "
                f"{{commits, merge_commit, pr}}"
            )
        unknown = set(raw) - {"commits", "merge_commit", "pr"}
        if unknown:
            raise ValueError(f"proof[{entry}] has unknown keys: {', '.join(sorted(unknown))}")
        commits = raw.get("commits")
        if isinstance(commits, str) or not isinstance(commits, (list, tuple)) or not commits:
            raise ValueError(f"proof[{entry}].commits must be a non-empty list of commit SHAs")
        shas = sorted({validate_sha(sha, f"proof[{entry}].commits") or "" for sha in commits})
        if "" in shas:
            raise ValueError(f"proof[{entry}].commits must not contain an empty SHA")
        pr = raw.get("pr")
        if pr is not None and (isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0):
            raise ValueError(f"proof[{entry}].pr must be a positive integer, got {pr!r}")
        if str(entry) in out:
            raise ValueError(f"proof names entry {entry} twice")
        out[str(entry)] = {
            "commits": shas,
            "merge_commit": validate_sha(raw.get("merge_commit"), f"proof[{entry}].merge_commit"),
            "pr": pr,
        }
    if not out:
        return None
    if len(_canonical(out)) > PROOF_MAX_CHARS:
        raise ValueError(f"proof too long, max {PROOF_MAX_CHARS} chars of JSON — refused")
    return out


def check_proof_scope(proof: dict | None, entry_ids: Iterable[int]) -> None:
    """Every entry the proof map names must belong to the release set."""
    if not proof:
        return
    outside = sorted(int(key) for key in proof if int(key) not in set(entry_ids))
    if outside:
        raise ValueError(
            f"proof names entries outside the release set: "
            f"{', '.join(f'#{i}' for i in outside)}; nothing released."
        )


# ── record I/O ────────────────────────────────────────────────────────────────


def _load_json(text: str | None):
    return None if text is None else json.loads(text)


def record_to_dict(row) -> dict:
    """The public record shape (RECORD_FIELDS); `entry_ids` decoded to
    list[int], `manifest` / `proof` decoded (None when not recorded; proof
    keys are entry ids as strings, as in JSON)."""
    data = dict(row._mapping)
    record = {field: data.get(field) for field in RECORD_FIELDS}
    record["entry_ids"] = json.loads(data["entry_ids"])
    record["manifest"] = _load_json(data.get("manifest"))
    record["proof"] = _load_json(data.get("proof"))
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
    proof: dict | None = None,
    status: str = RELEASE_FLOW[0],
    release_commit_sha: str | None = None,
    manifest: dict | None = None,
) -> dict:
    """Insert a record on the caller's (stamping) connection.

    Callers have validated every value and checked find_record under the same
    project write lock; the partial unique index still refuses a duplicate.
    A record inserted as 'completed' (reconstruction) gets completed_at = now.
    """
    if mode not in RELEASE_MODES:
        raise ValueError(f"Invalid release mode: '{mode}'")
    if status not in RELEASE_FLOW:
        raise ValueError(f"Invalid initial release status: '{status}'")
    row = conn.execute(
        releases.insert().values(
            project_id=project_id,
            version=version,
            mode=mode,
            entry_ids=json.dumps(sorted(entry_ids)),
            candidate_sha=candidate_sha,
            checkout_root=checkout_root,
            release_commit_sha=release_commit_sha,
            manifest=None if manifest is None else _canonical(manifest),
            proof=None if proof is None else _canonical(proof),
            status=status,
            created_at=now,
            updated_at=now,
            completed_at=now if status == "completed" else None,
        ).returning(releases)
    ).first()
    return record_to_dict(row)


def _require_record(conn: sa.Connection, pid: int, project_slug: str, version: str):
    row = find_record(conn, pid, version)
    if row is None:
        raise ValueError(
            f"No release record for version {version} in '{project_slug}' — "
            f"release_changelog writes it when it stamps the version."
        )
    return row


# ── readers ───────────────────────────────────────────────────────────────────


def get_release(engine: sa.Engine, project_slug: str, version: str | None = None) -> dict | None:
    """The live release record of `version` — whatever its status, also
    'completed' or 'abandoned' — or None.

    Without `version`: the most recent live record whose status is NOT
    terminal (neither 'completed' nor 'abandoned') — an unfinished release a
    workflow should resume — else None. `version` follows the version
    contract (ValueError "Unsupported version"). Read-only.
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
                    releases.c.status.not_in(sorted(TERMINAL_STATUSES)),
                ).order_by(releases.c.created_at.desc(), releases.c.id.desc()).limit(1)
            ).first()
        return record_to_dict(row) if row is not None else None


# ── writers ───────────────────────────────────────────────────────────────────


def advance_release(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    status: str,
    *,
    release_commit_sha: str | None = None,
    manifest: Mapping | None = None,
) -> dict:
    """Record verified progress: move the release record of `version` along
    the state machine and/or record the effects the workflow verified.

    Inside project_transaction(write=True); nothing is written on error.
    - `status` must be a RELEASE_FLOW status: several steps at once are
      allowed (the workflow verified the intermediate effects); the stored
      status again is a no-op; a lower one raises ValueError ("backwards");
      'abandoned' is refused (abandon_release is the only way there); a
      record in a terminal status accepts nothing new. Reaching 'completed'
      stamps completed_at.
    - `release_commit_sha` (hex, 7-64 chars, lowercased) is set ONCE, on the
      move into 'committed' or later; the stored value again is a
      confirmation, a different one raises ValueError. A record with a
      candidate_sha (a git release) cannot reach 'committed' or later
      without it. The candidate_sha itself is immutable — never accepted here.
    - `manifest` (JSON object, see validate_manifest) is merged write-once
      per key (merge_manifest).

    Returns {"release": <record>, "updated": bool} — `updated` False when
    nothing changed (idempotent repeat).
    """
    _validate_status(status)
    if status == ABANDONED:
        raise ValueError(
            "advance_release never abandons a release — use abandon_release "
            "(explicit confirmation and a reason)."
        )
    version = normalize_version(version)
    sha = validate_sha(release_commit_sha, "release_commit_sha")
    incoming = validate_manifest(manifest)
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version)
        moves = check_transition(row.status, status)
        final = status if moves else row.status
        values: dict = {}
        if moves:
            values["status"] = status

        commit = row.release_commit_sha
        if sha is not None and sha != commit:
            if commit is not None:
                raise ValueError(
                    f"release_commit_sha of release {row.version} is already recorded "
                    f"({commit}) and cannot change; nothing written."
                )
            if _rank(final) < _COMMITTED:
                raise ValueError(
                    f"release_commit_sha is recorded on the move to 'committed' or "
                    f"later, not at '{final}'; nothing written."
                )
            values["release_commit_sha"] = commit = sha
        if (moves and row.candidate_sha is not None and commit is None
                and _rank(final) >= _COMMITTED):
            raise ValueError(
                f"Release {row.version} cannot be '{final}' without its release commit "
                f"— pass release_commit_sha (the verified commit); nothing written."
            )

        merged, changed = merge_manifest(_load_json(row.manifest), incoming)
        if changed:
            values["manifest"] = _canonical(merged)

        if not values:
            return {"release": record_to_dict(row), "updated": False}
        if row.status in TERMINAL_STATUSES:
            raise ValueError(
                f"Release {row.version} is '{row.status}' — a terminal status; "
                f"nothing can be recorded on it any more; nothing written."
            )
        now = datetime.now(UTC)
        values["updated_at"] = now
        if final == "completed" and row.completed_at is None:
            values["completed_at"] = now
        updated = conn.execute(
            releases.update().where(releases.c.id == row.id).values(**values)
            .returning(releases)
        ).first()
        return {"release": record_to_dict(updated), "updated": True}


def abandon_release(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    reason: str,
    *,
    confirm: bool = False,
) -> dict:
    """End an unfinished release as 'abandoned' (K-60) — ledger only.

    Requires `confirm=True` and a non-empty `reason` (stripped, at most
    ABANDON_REASON_MAX characters); ValueError otherwise. Allowed from every
    non-terminal status; a 'completed' release is refused (it is never
    reclassified). The record and ALL its effects stay: current_version is not
    rewound, no entry is unstamped or unlinked, and the record keeps blocking
    the version — a re-release of V with the same set returns this abandoned
    record (no-op), a different set is refused like for any recorded
    version. An already-abandoned record is returned unchanged (`updated`
    False — the first reason and time are kept). Stamps abandoned_at.

    Returns {"release": <record>, "updated": bool}.
    """
    if confirm is not True:
        raise ValueError(
            "abandon_release needs explicit confirmation (confirm=True): the release "
            "stays recorded as abandoned for good; nothing written."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("abandon_release needs a non-empty reason; nothing written.")
    reason = reason.strip()
    if len(reason) > ABANDON_REASON_MAX:
        raise ValueError(
            f"reason too long: {len(reason)} chars, max {ABANDON_REASON_MAX} — "
            f"refused, never truncated"
        )
    version = normalize_version(version)
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version)
        if row.status == ABANDONED:
            return {"release": record_to_dict(row), "updated": False}
        if row.status == "completed":
            raise ValueError(
                f"Release {row.version} is completed — a completed release is never "
                f"reclassified as abandoned; nothing written."
            )
        check_transition(row.status, ABANDONED)
        now = datetime.now(UTC)
        updated = conn.execute(
            releases.update().where(releases.c.id == row.id).values(
                status=ABANDONED, abandoned_at=now, abandon_reason=reason, updated_at=now,
            ).returning(releases)
        ).first()
        return {"release": record_to_dict(updated), "updated": True}


def reconstruct_release(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    *,
    entry_ids: Iterable[int],
    candidate_sha: str | None,
    release_commit_sha: str | None,
    manifest: Mapping | None,
    status: str,
    confirm: bool = False,
    checkout_root: str | None = None,
    without_git: bool = False,
) -> dict:
    """Record a release made BEFORE release records existed (K-60) — without
    re-stamping anything. Ledger part only: the evidence (SHAs, manifest,
    status) is gathered and verified by the local workflow (R6b-2); this
    function refuses anything that does not agree with the database and
    never fills a gap by guessing.

    Every check runs before the single INSERT (nothing written on error):
    - `confirm=True` (ValueError otherwise).
    - `entry_ids` is REQUIRED (a list, [] for a version-only release) and
      must equal EXACTLY the live entries already carrying `version` under
      the version contract.
    - `version` must be a release the database proves: entries carry it, or
      (version-only) it is the project's current_version. Higher than a
      parseable current_version, an unparseable current_version, or
      stored spellings that disagree (e.g. both 1.1 and 1.1.0) → refused as
      ambiguous. The record stores the stored spelling (normalized).
    - No live record for the version may exist.
    - `status` ∈ RELEASE_FLOW (never 'abandoned'): the furthest effect the
      workflow verified. `manifest` (validate_manifest) is required from
      'files_written' on.
    - Git evidence: `candidate_sha` is REQUIRED; `release_commit_sha` is
      required from 'committed' on and refused below it. A project without
      git passes `without_git=True` instead — then both SHAs must be absent
      and 'committed' / 'tagged' are refused (no git effects exist).
    - `checkout_root` (optional, ABSOLUTE, normalize_worktree).

    The record gets mode 'reconstructed' (its provenance — the original
    stamp mode is unknown) and completed_at = now when status is 'completed'.
    Returns the record.
    """
    from dct.core.changelog import (
        _dedupe_ids,
        _entries_of_version,
        _parses,
        normalize_worktree,
        stored_spellings,
    )
    from dct.core.version import compare_versions

    if confirm is not True:
        raise ValueError(
            "reconstruct_release needs explicit confirmation (confirm=True); nothing written."
        )
    if entry_ids is None or isinstance(entry_ids, (str, bytes)):
        raise ValueError(
            "reconstruct_release needs the explicit entry set (entry_ids; [] for a "
            "version-only release) — the set is never guessed; nothing written."
        )
    ids = sorted(_dedupe_ids(entry_ids))
    _validate_status(status)
    if status == ABANDONED:
        raise ValueError("a release is reconstructed with a RELEASE_FLOW status, not 'abandoned'")
    version = normalize_version(version)
    candidate = validate_sha(candidate_sha, "candidate_sha")
    commit = validate_sha(release_commit_sha, "release_commit_sha")
    recorded_manifest = validate_manifest(manifest)
    root = normalize_worktree(checkout_root)
    ensure_fits(releases, {"checkout_root": root})
    rank = _rank(status)
    if without_git:
        if candidate or commit:
            raise ValueError(
                "without_git=True records a release of a project without git — "
                "candidate_sha / release_commit_sha must be absent; nothing written."
            )
        if status in ("committed", "tagged"):
            raise ValueError(
                f"a release without git has no commit or tag — status '{status}' "
                f"is not reconstructible; nothing written."
            )
    else:
        if candidate is None:
            raise ValueError(
                "reconstruct_release needs the candidate_sha the release was cut from "
                "(or without_git=True for a project without git) — never guessed; "
                "nothing written."
            )
        if rank >= _COMMITTED and commit is None:
            raise ValueError(
                f"status '{status}' needs the verified release_commit_sha; nothing written."
            )
        if rank < _COMMITTED and commit is not None:
            raise ValueError(
                f"release_commit_sha contradicts status '{status}' (no release commit "
                f"before 'committed'); nothing written."
            )
    if rank >= _FILES_WRITTEN and recorded_manifest is None:
        raise ValueError(
            f"status '{status}' needs the manifest of the verified artifacts; nothing written."
        )

    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        if find_record(conn, pid, version) is not None:
            raise ValueError(
                f"version {version} of '{project_slug}' already has a release record "
                f"— nothing to reconstruct; nothing written."
            )
        current = conn.execute(
            sa.select(projects.c.current_version).where(projects.c.id == pid)
        ).scalar()
        if current and not _parses(current):
            raise ValueError(
                f"The current version {current!r} of '{project_slug}' is outside the "
                f"version contract — the reconstruction cannot be ordered against it; "
                f"nothing written."
            )
        if current and compare_versions(version, current) > 0:
            raise ValueError(
                f"version {version} is higher than the current version {current} of "
                f"'{project_slug}' — it was never released; nothing written."
            )
        carrying = _entries_of_version(conn, pid, version)
        if ids != carrying:
            raise ValueError(
                f"entry set mismatch for {version}: the database has "
                f"{carrying} carrying it, requested {ids} — a reconstruction "
                f"records exactly the stamped set; nothing written."
            )
        spellings = {normalize_version(v) for v in stored_spellings(conn, pid, version)}
        if not carrying:
            if not same_version(current, version):
                raise ValueError(
                    f"no entry carries {version} and it is not the current version "
                    f"({current}) — the database does not prove this release; "
                    f"nothing written."
                )
            spellings = {normalize_version(current)}
        if len(spellings) != 1:
            raise ValueError(
                f"version {version} is stored under several spellings "
                f"({', '.join(sorted(spellings))}) — ambiguous; nothing written."
            )
        return insert_record(
            conn, pid, version=spellings.pop(), mode="reconstructed", entry_ids=ids,
            candidate_sha=candidate, checkout_root=root, now=datetime.now(UTC),
            status=status, release_commit_sha=commit, manifest=recorded_manifest,
        )


# ── the project's version-file contract (K-73) ───────────────────────────────


_DRIVE_RE = re.compile(r"[A-Za-z]:")


def normalize_version_files(paths: Iterable[str]) -> list[str]:
    """Validate repo-relative paths → sorted, deduplicated list (ValueError).

    Each path: a non-empty string without surrounding whitespace, forward
    slashes only, not absolute (no leading '/', no drive letter), no empty,
    '.' or '..' segment, at most VERSION_FILE_PATH_MAX characters; at most
    VERSION_FILES_MAX paths. Paths are never rewritten — a path that does not
    satisfy this is refused, not repaired. [] is valid (learned: none)."""
    if paths is None or isinstance(paths, (str, bytes)) or not isinstance(paths, Iterable):
        raise ValueError("version_files must be a list of repo-relative paths")
    out: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or not path or path != path.strip():
            raise ValueError(f"version file path must be a non-empty, unpadded string: {path!r}")
        if len(path) > VERSION_FILE_PATH_MAX:
            raise ValueError(f"version file path too long (max {VERSION_FILE_PATH_MAX}): {path!r}")
        if "\\" in path:
            raise ValueError(f"version file path must use forward slashes: {path!r}")
        if path.startswith("/") or _DRIVE_RE.match(path):
            raise ValueError(f"version file path must be repo-relative: {path!r}")
        if any(seg in ("", ".", "..") for seg in path.split("/")):
            raise ValueError(f"version file path has an empty, '.' or '..' segment: {path!r}")
        out.add(path)
    if len(out) > VERSION_FILES_MAX:
        raise ValueError(f"too many version files: {len(out)}, max {VERSION_FILES_MAX}")
    return sorted(out)


def _contract(row) -> dict:
    return {
        "version_files": _load_json(row.version_files),
        "confirmed_at": row.version_files_confirmed_at,
        "history": _load_json(row.version_files_history) or [],
    }


def _contract_row(conn: sa.Connection, pid: int):
    return conn.execute(
        sa.select(
            projects.c.version_files,
            projects.c.version_files_confirmed_at,
            projects.c.version_files_history,
        ).where(projects.c.id == pid)
    ).first()


def get_version_files(engine: sa.Engine, project_slug: str) -> dict:
    """The project's version-file contract (K-73): {version_files (sorted
    list, or None = not learned yet), confirmed_at (datetime | None),
    history (earlier contracts, oldest first: [{version_files,
    confirmed_at, replaced_at} ISO strings])}. Read-only."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        return _contract(_contract_row(conn, pid))


def set_version_files(
    engine: sa.Engine,
    project_slug: str,
    paths: Iterable[str],
    *,
    confirm: bool = False,
) -> dict:
    """Store the project's version-file contract — the owner-confirmed set
    of repo-relative paths every release's version sync must change (K-73).

    Requires `confirm=True` (ValueError otherwise) — for the first contract
    and for every change. Paths pass normalize_version_files. The same set
    again is a no-op (`updated` False, confirmed_at kept). A DIFFERENT set
    replaces the contract and appends the previous one to the history
    ledger ({version_files, confirmed_at, replaced_at}); history is never
    rewritten. Returns get_version_files' shape plus `updated`.
    """
    if confirm is not True:
        raise ValueError(
            "set_version_files needs explicit confirmation (confirm=True): the "
            "list becomes the project's release contract; nothing written."
        )
    files = normalize_version_files(paths)
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        current = _contract(_contract_row(conn, pid))
        if current["version_files"] == files:
            return {**current, "updated": False}
        now = datetime.now(UTC)
        history = list(current["history"])
        if current["version_files"] is not None:
            confirmed = current["confirmed_at"]
            history.append({
                "version_files": current["version_files"],
                "confirmed_at": confirmed.isoformat() if confirmed else None,
                "replaced_at": now.isoformat(),
            })
        conn.execute(
            projects.update().where(projects.c.id == pid).values(
                version_files=json.dumps(files),
                version_files_confirmed_at=now,
                version_files_history=json.dumps(history) if history else None,
                updated_at=now,
            )
        )
        return {**_contract(_contract_row(conn, pid)), "updated": True}
