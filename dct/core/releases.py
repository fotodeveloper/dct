"""Durable release records (K-41, K-60, K-61) — the ONE implementation.

A release of a project version leaves a record in `releases` — the release
LEDGER. It holds the exact entry set (`entry_ids`, [] for a version-only
release), the mode, the commit the release was cut from (`candidate_sha`,
IMMUTABLE once written — K-61), the checkout the release runs in
(`checkout_root`), the release commit (`release_commit_sha`, set once), the
expected artifacts with their content hashes (`manifest`, write-once per
key), the proof map (`proof`: entry id → proving commits, K-70) and a
`status` that follows ONE state machine (TRANSITIONS):

    stamped → files_written → committed → tagged → pushed → completed
        └──────────┴─────────────┴──────────┴────────┴──→ abandoned

Forward moves may skip steps (the workflow verified the intermediate
effects); 'completed' and 'abandoned' are terminal; a completed release is
never reclassified as abandoned. 'abandoned' is reached ONLY through
abandon_release (explicit confirmation + a reason), never through
advance_release. 'pushed' (K-97) = the release commit and the tag are on the
push target; a push failure leaves the record 'tagged' and is resumed from
there (never a new bump, commit or tag).

K-94 B1: the record carries its verified RELEASE MODE (`release_mode`:
'git' | 'without_git'; NULL = recorded before the field existed —
unverified). A NULL candidate_sha never implies "no git": only a
'without_git' record may complete without a release commit; it never
reaches 'committed', 'tagged' or 'pushed' and completes only with its
verified artifacts in the manifest. Every other record needs its
release_commit_sha from 'committed' on (also to reach 'completed').

Further ledger fields (docs/release-workflow.md, "The release ledger"):
`executor` ('dct' | 'own', K-85 — copied from the project's
release_executor at stamp time, immutable: a later project-setting change
never takes over an unfinished release), `consent` (what the owner approved,
K-97 F4), `proposal` (the approved version-file proposal, K-76) and `render`
(the changelog render settings, K-90) — each JSON, validated, write-once.

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
its readers and writers. So does the PENDING version-file proposal (K-76:
`projects.release_proposal`, written by `dct release prepare` through
set_pending_proposal, read by get_pending_proposal) — consumed by the stamp
that names its digest (changelog.release_entries consume_proposal), which
records it as the record's `proposal` and its paths as the contract
(store_version_files) in the stamp transaction.
"""

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.version import ensure_version_fits, normalize_version, same_version
from dct.db import project_transaction, projects, releases

# ── the state machine (ONE definition, used everywhere) ──────────────────────

RELEASE_FLOW = ("stamped", "files_written", "committed", "tagged", "pushed", "completed")
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

# K-94 B1: the verified release mode — whether the release has git effects.
GIT_MODES = ("git", "without_git")
WITHOUT_GIT = "without_git"
# Statuses only a release WITH git can reach (they record git effects).
GIT_ONLY_STATUSES = frozenset({"committed", "tagged", "pushed"})

# K-85: who performs the release's effects — dct's effects layer, or the
# project's own release tool. The project setting is projects.release_executor.
EXECUTORS = ("dct", "own")
DEFAULT_EXECUTOR = "dct"
# Who records PROGRESS on a record (advance_release `actor`, K-85 / K-96
# D10): 'dct' = the effects layer (after verifying each effect itself),
# 'ledger' = a caller reporting what IT verified (MCP advance_release, `dct
# clog release-advance` — the path of a project's own tool).
ACTORS = ("dct", "ledger")

_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}", re.ASCII)
_COMMITTED = RELEASE_FLOW.index("committed")
_FILES_WRITTEN = RELEASE_FLOW.index("files_written")

# Size caps for the JSON ledger fields — refused, never truncated (#792).
# K-92 / K-94 B4: a JSON cap is BYTES of the field's CANONICAL JSON (sorted
# keys, compact separators, UTF-8 — json_size), applied to the value that is
# actually stored (for the manifest: the MERGED manifest). The abandon
# reason is capped in characters.
MANIFEST_MAX_BYTES = 65536
PROOF_MAX_BYTES = 262144
CONSENT_MAX_BYTES = 65536
PROPOSAL_MAX_BYTES = 65536
MANIFEST_KEY_MAX = 64
ABANDON_REASON_MAX = 2000
VERSION_FILES_MAX = 200
VERSION_FILE_PATH_MAX = 500

RECORD_FIELDS = (
    "id", "version", "mode", "entry_ids", "candidate_sha", "checkout_root",
    "release_commit_sha", "manifest", "proof", "status", "created_at",
    "updated_at", "completed_at", "abandoned_at", "abandon_reason",
    "release_mode", "executor", "consent", "proposal", "render",
)
# JSON-encoded record columns (decoded by record_to_dict).
_JSON_FIELDS = ("manifest", "proof", "consent", "proposal", "render")


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


def json_size(value) -> int:
    """Bytes of `value`'s canonical JSON (sorted keys, compact separators,
    UTF-8) — the ONE unit of every JSON size cap of the ledger (K-92)."""
    return len(_canonical(value).encode("utf-8"))


def ensure_json_fits(value, field: str, max_bytes: int) -> None:
    """ValueError unless json_size(value) <= max_bytes (refused, never truncated)."""
    size = json_size(value)
    if size > max_bytes:
        raise ValueError(
            f"{field} too long: {size} bytes of canonical JSON, max {max_bytes} — "
            f"refused, never truncated; nothing written."
        )


def validate_manifest(manifest: Mapping | None) -> dict | None:
    """None / {} → None; otherwise a JSON object whose keys are non-empty
    strings (≤ MANIFEST_KEY_MAX) and whose values are JSON values other than
    null — the manifest's content is the workflow's (K-49/K-73: e.g.
    {"changelog": {...}, "version_files": [...], "tree": "<sha>"}); the
    ledger only enforces the shape and write-once per key. TypeError for a
    non-object, ValueError for any other violation (also when its canonical
    JSON exceeds MANIFEST_MAX_BYTES — the MERGED manifest is checked again
    before it is stored, see merge_manifest)."""
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
    ensure_json_fits(out, "manifest", MANIFEST_MAX_BYTES)
    return json.loads(encoded)


def merge_manifest(stored: dict | None, incoming: dict | None) -> tuple[dict | None, bool]:
    """Write-once merge: a new key is added, a key already set must carry the
    SAME value (a confirmation), a different value raises ValueError. The
    MERGED manifest must fit MANIFEST_MAX_BYTES (K-92: two patches that fit
    alone cannot jointly exceed the cap) — ValueError otherwise.
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
    if changed:
        ensure_json_fits(merged, "manifest (merged)", MANIFEST_MAX_BYTES)
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


PROOF_SOURCES = ("own_merge", "introducing_merge")
ASSOCIATION_SOURCES = (*PROOF_SOURCES, "subject_suffix")
ASSOCIATIONS_MAX = 50
_PROOF_KEYS = frozenset({"commits", "merge_commit", "pr", "source", "ambiguous", "associations"})


def _pr_number(value, field: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError(f"{field} must be a positive integer, got {value!r}")
    return value


def _associations(raw, entry: int) -> list[dict]:
    field = f"proof[{entry}].associations"
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise TypeError(f"{field} must be a list")
    if len(raw) > ASSOCIATIONS_MAX:
        raise ValueError(f"{field} holds more than {ASSOCIATIONS_MAX} items")
    out = []
    for i, item in enumerate(raw):
        where = f"{field}[{i}]"
        _object(item, where, {"merge_commit", "pr", "source", "certain"},
                {"merge_commit", "pr", "source", "certain"})
        if item["source"] not in ASSOCIATION_SOURCES:
            raise ValueError(f"{where}.source must be one of {', '.join(ASSOCIATION_SOURCES)}")
        if not isinstance(item["certain"], bool):
            raise TypeError(f"{where}.certain must be true or false")
        out.append({"merge_commit": validate_sha(item["merge_commit"], f"{where}.merge_commit"),
                    "pr": _pr_number(item["pr"], f"{where}.pr"),
                    "source": item["source"], "certain": item["certain"]})
    return out


def validate_proof(proof: Mapping | None) -> dict | None:
    """The proof map (K-70, K-89) in its canonical form, or None for None / {}.

    Input: {entry id (int or digit string): <proof>} where <proof> is either
    a non-empty list of proving commit SHAs (shorthand) or an object
    {"commits": [sha, ...] (non-empty), "merge_commit": sha | null,
    "pr": positive int | null} with the optional provenance keys of the
    release plan (K-89): "source" ("own_merge" — the proving commit is
    itself the merge | "introducing_merge" | null; the merge_commit / pr
    pair comes from that ONE recognition), "ambiguous" (bool — several
    different merge associations: merge_commit and pr must then be null)
    and "associations" ([{merge_commit, pr, source ('own_merge' |
    'introducing_merge' | 'subject_suffix'), certain (bool)}], at most
    ASSOCIATIONS_MAX — every recognition, a `(#N)` subject suffix listed as
    uncertain, never as `pr`). Stored as {"<id>": {"commits": [sorted,
    deduplicated, lowercased], "merge_commit": ..., "pr": ...[, the optional
    keys given]}}. Any other shape — or a contradiction (ambiguous with a
    merge_commit / pr, a source without a merge_commit) — raises ValueError.
    Entries without a commit proof (e.g. attested) are simply absent from
    the map. A non-object map / proof value / association, a non-list
    `associations`, a non-bool `ambiguous` / `certain` or a bool key raises
    TypeError."""
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
        unknown = set(raw) - _PROOF_KEYS
        if unknown:
            raise ValueError(f"proof[{entry}] has unknown keys: {', '.join(sorted(unknown))}")
        commits = raw.get("commits")
        if isinstance(commits, str) or not isinstance(commits, (list, tuple)) or not commits:
            raise ValueError(f"proof[{entry}].commits must be a non-empty list of commit SHAs")
        shas = sorted({validate_sha(sha, f"proof[{entry}].commits") or "" for sha in commits})
        if "" in shas:
            raise ValueError(f"proof[{entry}].commits must not contain an empty SHA")
        if str(entry) in out:
            raise ValueError(f"proof names entry {entry} twice")
        item = {
            "commits": shas,
            "merge_commit": validate_sha(raw.get("merge_commit"), f"proof[{entry}].merge_commit"),
            "pr": _pr_number(raw.get("pr"), f"proof[{entry}].pr"),
        }
        if "source" in raw:
            if raw["source"] is not None and raw["source"] not in PROOF_SOURCES:
                raise ValueError(f"proof[{entry}].source must be one of "
                                 f"{', '.join(PROOF_SOURCES)} or null")
            if raw["source"] is not None and item["merge_commit"] is None:
                raise ValueError(f"proof[{entry}].source names a merge but merge_commit is null")
            item["source"] = raw["source"]
        if "ambiguous" in raw:
            if not isinstance(raw["ambiguous"], bool):
                raise TypeError(f"proof[{entry}].ambiguous must be true or false")
            if raw["ambiguous"] and (item["merge_commit"] or item["pr"] or raw.get("source")):
                raise ValueError(f"proof[{entry}] is ambiguous but names a merge_commit / pr / "
                                 f"source — contradiction")
            item["ambiguous"] = raw["ambiguous"]
        if "associations" in raw:
            item["associations"] = _associations(raw["associations"], entry)
        out[str(entry)] = item
    if not out:
        return None
    ensure_json_fits(out, "proof", PROOF_MAX_BYTES)
    return out


def proof_pr(proof: Mapping | None, entry_id: int) -> int | None:
    """The PR number the changelog may render for `entry_id` (K-90): the
    stored map's `pr` when it is set and the map does not mark the entry
    ambiguous — never a heuristic number (the plan never stores one as
    `pr`, K-95 C4)."""
    item = (proof or {}).get(str(entry_id))
    if not isinstance(item, Mapping) or item.get("ambiguous"):
        return None
    pr = item.get("pr")
    return pr if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0 else None


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


def validate_release_mode(mode: str | None) -> str | None:
    """None / "" → None (not stated); 'git' | 'without_git' → itself; else
    ValueError. The caller states the mode it VERIFIED (K-94 B1: a project
    confirmed outside git → 'without_git'; a detection error is never one)."""
    if not mode:
        return None
    if mode not in GIT_MODES:
        raise ValueError(f"release_mode must be one of {', '.join(GIT_MODES)}, got {mode!r}")
    return mode


def validate_executor(executor: str) -> str:
    """'dct' | 'own' (K-85) → itself; anything else → ValueError."""
    if executor not in EXECUTORS:
        raise ValueError(
            f"release executor must be one of {', '.join(EXECUTORS)}, got {executor!r}"
        )
    return executor


def progress_owner(record: Mapping) -> str:
    """Who executes the release of `record` (K-85, the RECORD decides —
    never today's project setting): 'dct' when its executor is 'dct' (NULL
    on records older than the field counts as 'dct') AND it carries the
    owner's consent (it was stamped by `dct release apply`); else 'ledger' —
    an 'own' record, or one stamped outside the effects layer (MCP
    release_changelog, older records, a partial reconstruction): its
    effects are performed and verified by whoever reports them through the
    ledger. `record` is a record dict (get_release) or a releases row."""
    get = record.get if isinstance(record, Mapping) else lambda k: getattr(record, k)
    executor = get("executor") or DEFAULT_EXECUTOR
    return "dct" if executor == "dct" and get("consent") is not None else "ledger"


def check_progress_actor(record: Mapping, actor: str) -> None:
    """K-85 / K-96 D10 — refuse a progress write by the wrong actor
    (ACTORS): a release dct executes is advanced only by the effects layer
    (`dct release resume` verifies each effect), any other only through the
    ledger path. ValueError otherwise."""
    if actor not in ACTORS:
        raise ValueError(f"actor must be one of {', '.join(ACTORS)}, got {actor!r}")
    get = record.get if isinstance(record, Mapping) else lambda k: getattr(record, k)
    owner = progress_owner(record)
    if actor == owner:
        return
    if owner == "dct":
        raise ValueError(
            f"Release {get('version')} is executed by dct (stamped by `dct release apply` "
            f"with the owner's consent) — its progress is recorded only by `dct release "
            f"resume`, which verifies each effect; never by hand; nothing written."
        )
    raise ValueError(
        f"Release {get('version')} is not executed by dct (executor "
        f"'{get('executor') or DEFAULT_EXECUTOR}', "
        f"{'no recorded consent' if get('consent') is None else 'own tool'}) — dct records "
        f"no progress on it; nothing written."
    )


_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _object(value, field: str, allowed: set[str], required: set[str] = frozenset()) -> Mapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a JSON object, got {type(value).__name__}")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} has unknown keys: {', '.join(sorted(map(str, unknown)))}")
    absent = required - set(value)
    if absent:
        raise ValueError(f"{field} needs the keys: {', '.join(sorted(absent))}")
    return value


def _text(value, field: str, limit: int = 500, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a non-empty, unpadded string of at most {limit} chars")
    return value


def _digest(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase hex SHA-256 (64 chars), got {value!r}")
    return value


RENDER_KEYS = frozenset({"pr_refs"})


def validate_render(render: Mapping | None) -> dict | None:
    """The changelog render settings persisted per release (K-90) —
    {"pr_refs": bool} (render `(#N)` PR references from the stored proof
    map); None / {} → None. Unknown keys → ValueError, a non-object or a
    non-bool value → TypeError."""
    if render is None:
        return None
    _object(render, "render", set(RENDER_KEYS))
    if not render:
        return None
    pr_refs = render.get("pr_refs")
    if not isinstance(pr_refs, bool):
        raise TypeError(f"render.pr_refs must be true or false, got {pr_refs!r}")
    return {"pr_refs": pr_refs}


PROPOSAL_KEYS = frozenset({
    "paths", "base_blobs", "expected_blobs", "script", "candidate_sha",
    "version", "plan_digest", "release_mode", "changelog",
})
PROPOSAL_CHANGELOG_KEYS = frozenset({"path", "base_blob", "base_sha256"})


def validate_proposal(proposal: Mapping | None) -> dict | None:
    """The approved version-file proposal (K-76) in canonical form, or None.

    {"paths": [repo-relative paths] (normalize_version_files; [] = an
    approved empty contract), "base_blobs": {path: blob id | null (absent
    before the sync)}, "expected_blobs": {path: blob id}, "script": null |
    {"identity": str, "sha256": hex SHA-256 | null}, "candidate_sha": sha |
    null} plus the release context the proposal is bound to (each optional,
    null when not given): "version" (normalized V), "plan_digest" (the
    approved plan's snapshot_digest, hex SHA-256), "release_mode" ('git' |
    'without_git' — how the blobs are addressed: git blob ids, or the
    SHA-256 of the content kept in dct's release blob store), "changelog"
    (the changelog file's BASELINE before the release: {"path" (one
    normalize_version_files path), "base_blob" (blob id | null = absent),
    "base_sha256" (hex SHA-256 | null = absent)}, or null when the project
    has no managed changelog file). base_blobs and expected_blobs name
    EXACTLY `paths`. Blob ids are hex 7-64 chars (lowercased). ≤
    PROPOSAL_MAX_BYTES of canonical JSON. ValueError / TypeError
    otherwise."""
    if proposal is None:
        return None
    _object(proposal, "proposal", set(PROPOSAL_KEYS), {"paths", "base_blobs", "expected_blobs"})
    paths = normalize_version_files(proposal["paths"])
    blobs = {}
    for key, nullable in (("base_blobs", True), ("expected_blobs", False)):
        raw = _object(proposal[key], f"proposal.{key}", set(paths))
        if set(raw) != set(paths):
            raise ValueError(f"proposal.{key} must name exactly the proposal's paths")
        blobs[key] = {}
        for path in paths:
            sha = raw[path]
            if sha is None and nullable:
                blobs[key][path] = None
                continue
            if not sha:
                raise ValueError(f"proposal.{key}[{path!r}] needs a blob id")
            blobs[key][path] = validate_sha(sha, f"proposal.{key}[{path!r}]")
    script = proposal.get("script")
    if script is not None:
        _object(script, "proposal.script", {"identity", "sha256"}, {"identity"})
        script = {"identity": _text(script["identity"], "proposal.script.identity", 2000),
                  "sha256": _digest(script.get("sha256"), "proposal.script.sha256")}
    version = proposal.get("version")
    if version is not None:
        version = normalize_version(_text(version, "proposal.version", 50))
    changelog = proposal.get("changelog")
    if changelog is not None:
        _object(changelog, "proposal.changelog", set(PROPOSAL_CHANGELOG_KEYS),
                set(PROPOSAL_CHANGELOG_KEYS))
        [path] = normalize_version_files([changelog["path"]])
        changelog = {
            "path": path,
            "base_blob": validate_sha(changelog["base_blob"], "proposal.changelog.base_blob"),
            "base_sha256": _digest(changelog["base_sha256"], "proposal.changelog.base_sha256"),
        }
        if (changelog["base_blob"] is None) != (changelog["base_sha256"] is None):
            raise ValueError("proposal.changelog: base_blob and base_sha256 are both set "
                             "(the file existed) or both null (it was absent)")
    out = {
        "paths": paths,
        **blobs,
        "script": script,
        "candidate_sha": validate_sha(proposal.get("candidate_sha"), "proposal.candidate_sha"),
        "version": version,
        "plan_digest": _digest(proposal.get("plan_digest"), "proposal.plan_digest"),
        "release_mode": validate_release_mode(proposal.get("release_mode")),
        "changelog": changelog,
    }
    ensure_json_fits(out, "proposal", PROPOSAL_MAX_BYTES)
    return out


def proposal_digest(proposal: Mapping) -> str:
    """SHA-256 of the canonical JSON of a validated proposal — what a consent
    names (consent.proposal_sha256) so a changed proposal voids it."""
    import hashlib

    return hashlib.sha256(_canonical(validate_proposal(proposal)).encode("utf-8")).hexdigest()


CONSENT_KEYS = frozenset({
    "candidate_sha", "entry_ids", "version", "proposal_sha256", "render", "push_target",
})
PUSH_TARGET_KEYS = frozenset({"remote", "url", "branch", "tag"})


def validate_consent(consent: Mapping | None) -> dict | None:
    """What the owner approved for this release (K-97 F4) in canonical form,
    or None — so a resume can tell whether the scope changed.

    {"version": V (normalized, REQUIRED), "entry_ids": [ids] (REQUIRED,
    sorted, deduplicated), "candidate_sha": sha | null, "proposal_sha256":
    proposal_digest of the approved version-file proposal | null, "render":
    validate_render | null, "push_target": null | {"remote" (required),
    "url", "branch", "tag"} (strings)}. ≤ CONSENT_MAX_BYTES of canonical
    JSON. ValueError / TypeError otherwise."""
    if consent is None:
        return None
    _object(consent, "consent", set(CONSENT_KEYS), {"version", "entry_ids"})
    raw_ids = consent["entry_ids"]
    if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, (list, tuple)):
        raise TypeError("consent.entry_ids must be a list of entry ids")
    ids = sorted({_entry_id(i) for i in raw_ids})
    push = consent.get("push_target")
    if push is not None:
        _object(push, "consent.push_target", set(PUSH_TARGET_KEYS), {"remote"})
        push = {key: _text(push.get(key), f"consent.push_target.{key}", optional=key != "remote")
                for key in sorted(PUSH_TARGET_KEYS)}
    out = {
        "version": normalize_version(_text(consent["version"], "consent.version", 50)),
        "entry_ids": ids,
        "candidate_sha": validate_sha(consent.get("candidate_sha"), "consent.candidate_sha"),
        "proposal_sha256": _digest(consent.get("proposal_sha256"), "consent.proposal_sha256"),
        "render": validate_render(consent.get("render")),
        "push_target": push,
    }
    ensure_json_fits(out, "consent", CONSENT_MAX_BYTES)
    return out


def check_unchanged(field: str, stored, given, *, may_fill: bool = True) -> bool:
    """Write-once rule of a ledger field: `given` None = not passed (True,
    nothing to write); equal to `stored` = confirmation (True); `stored` None
    and `may_fill` = first write (False — the caller writes it); a DIFFERENT
    value — or, with may_fill=False, any value for a field the record does
    not have — raises ValueError (K-94 B2: a contradicting value is refused,
    never silently ignored). Values compare as canonical JSON."""
    if given is None:
        return True
    if stored is None and may_fill:
        return False
    if stored is not None and _canonical(stored) == _canonical(given):
        return True
    recorded = "is not recorded on this release" if stored is None else \
        "is already recorded with a different value"
    raise ValueError(
        f"{field} {recorded} — it is immutable; the given value contradicts the "
        f"record; nothing written."
    )


# ── record I/O ────────────────────────────────────────────────────────────────


def _load_json(text: str | None):
    return None if text is None else json.loads(text)


def record_to_dict(row) -> dict:
    """The public record shape (RECORD_FIELDS); `entry_ids` decoded to
    list[int], the JSON fields (`manifest`, `proof`, `consent`, `proposal`,
    `render`) decoded (None when not recorded; proof keys are entry ids as
    strings, as in JSON)."""
    data = dict(row._mapping)
    record = {field: data.get(field) for field in RECORD_FIELDS}
    record["entry_ids"] = json.loads(data["entry_ids"])
    for field in _JSON_FIELDS:
        record[field] = _load_json(data.get(field))
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


def project_release_settings(conn: sa.Connection, project_id: int) -> dict:
    """The project's release settings read on the caller's connection:
    {executor ('dct' | 'own', K-85), render ({"pr_refs": bool}, K-90)}."""
    row = conn.execute(
        sa.select(projects.c.release_executor, projects.c.changelog_pr_refs)
        .where(projects.c.id == project_id)
    ).first()
    return {
        "executor": row.release_executor or DEFAULT_EXECUTOR,
        "render": {"pr_refs": bool(row.changelog_pr_refs)},
    }


def check_mode_consistency(release_mode: str | None, candidate_sha: str | None,
                           release_commit_sha: str | None, status: str) -> None:
    """K-94 B1 — a 'without_git' record has no candidate, no release commit
    and never a git-only status; ValueError otherwise."""
    if release_mode != WITHOUT_GIT:
        return
    if candidate_sha or release_commit_sha:
        raise ValueError(
            "a release without git has no candidate_sha / release_commit_sha; "
            "nothing written."
        )
    if status in GIT_ONLY_STATUSES:
        raise ValueError(
            f"a release without git has no commit, tag or push — status '{status}' "
            f"is refused; nothing written."
        )


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
    release_mode: str | None = None,
    consent: dict | None = None,
    proposal: dict | None = None,
    render: dict | None = None,
) -> dict:
    """Insert a record on the caller's (stamping) connection.

    Callers have validated every value and checked find_record under the same
    project write lock; the partial unique index still refuses a duplicate.
    The version must fit the schema (ensure_version_fits — the value actually
    stored, K-87). `executor` is copied from the project's release_executor
    (K-85) — never a parameter. A record inserted as 'completed'
    (reconstruction) gets completed_at = now.
    """
    if mode not in RELEASE_MODES:
        raise ValueError(f"Invalid release mode: '{mode}'")
    if status not in RELEASE_FLOW:
        raise ValueError(f"Invalid initial release status: '{status}'")
    ensure_version_fits(version)
    check_mode_consistency(release_mode, candidate_sha, release_commit_sha, status)
    settings = project_release_settings(conn, project_id)

    def encoded(value):
        return None if value is None else _canonical(value)

    row = conn.execute(
        releases.insert().values(
            project_id=project_id,
            version=version,
            mode=mode,
            entry_ids=json.dumps(sorted(entry_ids)),
            candidate_sha=candidate_sha,
            checkout_root=checkout_root,
            release_commit_sha=release_commit_sha,
            manifest=encoded(manifest),
            proof=encoded(proof),
            status=status,
            release_mode=release_mode,
            executor=settings["executor"],
            consent=encoded(consent),
            proposal=encoded(proposal),
            render=encoded(render),
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


def check_entry_set(conn: sa.Connection, project_id: int, row) -> None:
    """K-94 B3 — the record's entry set must equal EXACTLY the live entries
    carrying its version (under the version contract). ValueError naming
    the difference otherwise; the record is never extended or shrunk."""
    from dct.core.changelog import _entries_of_version

    recorded = sorted(json.loads(row.entry_ids))
    carrying = _entries_of_version(conn, project_id, row.version)
    if recorded == carrying:
        return
    extra = sorted(set(carrying) - set(recorded))
    lost = sorted(set(recorded) - set(carrying))
    parts = []
    if extra:
        parts.append(f"carrying {row.version} but not in the record: "
                     f"{', '.join(f'#{i}' for i in extra)}")
    if lost:
        parts.append(f"in the record but no longer live with {row.version}: "
                     f"{', '.join(f'#{i}' for i in lost)}")
    raise ValueError(
        f"release {row.version}: the database's entry set no longer matches the record "
        f"({'; '.join(parts)}) — a record is never extended or shrunk to fit; resolve "
        f"the entries (or abandon the release) before any further effect; nothing done."
    )


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
            row = _latest_unfinished(conn, pid)
        return record_to_dict(row) if row is not None else None


def _latest_unfinished(conn: sa.Connection, pid: int):
    return conn.execute(
        sa.select(releases).where(
            releases.c.project_id == pid,
            releases.c.deleted_at.is_(None),
            releases.c.status.not_in(sorted(TERMINAL_STATUSES)),
        ).order_by(releases.c.created_at.desc(), releases.c.id.desc()).limit(1)
    ).first()


def verify_release_entry_set(
    engine: sa.Engine, project_slug: str, version: str | None = None
) -> dict:
    """K-94 B3 — the pre-effect gate of a resume / retry: the record of
    `version` (default: the latest unfinished one) whose entry set equals
    EXACTLY the live entries carrying its version. Returns the record;
    ValueError when there is no such record or the sets differ (check_entry_set
    — the record is never extended). Read-only: one consistent snapshot."""
    if version:
        version = normalize_version(version)
    with project_transaction(engine, project_slug) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version) if version else \
            _latest_unfinished(conn, pid)
        if row is None:
            raise ValueError(f"No unfinished release record in '{project_slug}'.")
        check_entry_set(conn, pid, row)
        return record_to_dict(row)


# ── writers ───────────────────────────────────────────────────────────────────


def advance_release(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    status: str,
    *,
    release_commit_sha: str | None = None,
    manifest: Mapping | None = None,
    release_mode: str | None = None,
    consent: Mapping | None = None,
    proposal: Mapping | None = None,
    render: Mapping | None = None,
    actor: str | None = None,
) -> dict:
    """Record verified progress: move the release record of `version` along
    the state machine and/or record the effects the workflow verified.

    Inside project_transaction(write=True); nothing is written on error, and
    every check runs before the single UPDATE (a refusal leaves status,
    manifest and updated_at unchanged).
    - `status` must be a RELEASE_FLOW status: several steps at once are
      allowed (the workflow verified the intermediate effects); the stored
      status again is a no-op; a lower one raises ValueError ("backwards");
      'abandoned' is refused (abandon_release is the only way there); a
      record in a terminal status accepts nothing new. Reaching 'completed'
      stamps completed_at.
    - `release_commit_sha` (hex, 7-64 chars, lowercased) is set ONCE, on the
      move into 'committed' or later; the stored value again is a
      confirmation, a different one raises ValueError. K-94 B1: unless the
      record's release_mode is 'without_git', no record reaches 'committed'
      or later (also 'completed') without it — a NULL candidate_sha does not
      mean "no git". A 'without_git' record refuses a release commit and the
      git-only statuses (committed / tagged / pushed) and completes only with
      a manifest (its verified artifacts). The candidate_sha itself is
      immutable — never accepted here.
    - `manifest` (JSON object, see validate_manifest) is merged write-once
      per key (merge_manifest); the MERGED manifest must fit
      MANIFEST_MAX_BYTES (K-92).
    - `release_mode`, `consent`, `proposal`, `render` (validate_*) are
      write-once: recorded when the record has none, the stored value again
      is a confirmation, a different one raises ValueError (check_unchanged).
      The executor is fixed at stamp time and never accepted here.
    - `actor` (K-85 / K-96 D10, ACTORS): 'dct' (the effects layer) or
      'ledger' (MCP advance_release, `dct clog release-advance` — a caller
      reporting its own verified effects); checked against the RECORD
      (check_progress_actor) before anything else, also for a repeat: a
      record dct executes (progress_owner 'dct') accepts only 'dct', every
      other record only 'ledger'. None = not checked (core callers and
      tests that drive the ledger directly).

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
    once = {
        "release_mode": validate_release_mode(release_mode),
        "consent": validate_consent(consent),
        "proposal": validate_proposal(proposal),
        "render": validate_render(render),
    }
    if actor is not None and actor not in ACTORS:
        raise ValueError(f"actor must be one of {', '.join(ACTORS)}, got {actor!r}")
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version)
        if actor is not None:
            check_progress_actor(row, actor)
        moves = check_transition(row.status, status)
        final = status if moves else row.status
        values: dict = {}
        if moves:
            values["status"] = status

        for field, given in once.items():
            stored = getattr(row, field)
            stored = _load_json(stored) if field in _JSON_FIELDS else stored
            if not check_unchanged(field, stored, given):
                values[field] = given if field not in _JSON_FIELDS else _canonical(given)
        mode = values.get("release_mode", row.release_mode)

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
        check_mode_consistency(mode, row.candidate_sha, commit, final)
        if mode != WITHOUT_GIT and commit is None and _rank(final) >= _COMMITTED and (
                moves or "release_mode" in values):
            raise ValueError(
                f"Release {row.version} cannot be '{final}' without its release commit "
                f"— pass release_commit_sha (the verified commit); only a release "
                f"recorded with release_mode 'without_git' completes without one; "
                f"nothing written."
            )

        merged, changed = merge_manifest(_load_json(row.manifest), incoming)
        if changed:
            values["manifest"] = _canonical(merged)
        if mode == WITHOUT_GIT and final == "completed" and not merged and (
                moves or "release_mode" in values):
            raise ValueError(
                f"Release {row.version} (without git) completes only with the manifest "
                f"of its verified artifacts; nothing written."
            )

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
    version. Repeating the abandonment with the SAME reason returns the
    record unchanged (`updated` False); K-94 B5: a DIFFERENT reason is not a
    compatible repeat — ValueError naming the recorded reason (the first
    reason and time are kept). Stamps abandoned_at.

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
            if row.abandon_reason == reason:
                return {"release": record_to_dict(row), "updated": False}
            raise ValueError(
                f"Release {row.version} was already abandoned at {row.abandoned_at} with "
                f"the reason {row.abandon_reason!r} — the first reason stays; a different "
                f"one is not recorded; nothing written."
            )
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
      parseable current_version or an unparseable current_version →
      refused.
    - The record stores the AUTHORITATIVE spelling (K-86 / K-93 A3 —
      changelog.resolve_version_spelling: the first-stamped entry's
      spelling, earliest released_at, NULL last, ties → lowest id; else
      current_version), normalized; the requested spelling is only compared
      (same_version). Equivalent legacy spellings (1.1 and 1.1.0) are no
      longer refused; they stay stored as they are. K-87: that FINAL value
      must fit the schema (ensure_version_fits, widths read from the
      schema) — ValueError "version too long" otherwise.
    - No live record for the version may exist.
    - `status` ∈ RELEASE_FLOW (never 'abandoned'): the furthest effect the
      workflow verified. `manifest` (validate_manifest) is required from
      'files_written' on.
    - Git evidence: `candidate_sha` is REQUIRED; `release_commit_sha` is
      required from 'committed' on and refused below it. A project without
      git passes `without_git=True` instead — then both SHAs must be absent
      and 'committed' / 'tagged' / 'pushed' are refused (no git effects
      exist).
    - `checkout_root` (optional, ABSOLUTE, normalize_worktree).

    The record gets mode 'reconstructed' (its provenance — the original
    stamp mode is unknown), release_mode 'git' / 'without_git' (from
    without_git), the project's current release executor (K-85), no consent,
    proposal or render (unknown for a legacy release) and completed_at = now
    when status is 'completed'. Returns the record.
    """
    from dct.core.changelog import (
        _dedupe_ids,
        _entries_of_version,
        _parses,
        normalize_worktree,
        resolve_version_spelling,
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
        if status in GIT_ONLY_STATUSES:
            raise ValueError(
                f"a release without git has no commit, tag or push — status '{status}' "
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
        if not carrying and not same_version(current, version):
            raise ValueError(
                f"no entry carries {version} and it is not the current version "
                f"({current}) — the database does not prove this release; "
                f"nothing written."
            )
        spelling = resolve_version_spelling(conn, pid, version)
        final = normalize_version(spelling["authoritative"])
        return insert_record(
            conn, pid, version=final, mode="reconstructed", entry_ids=ids,
            candidate_sha=candidate, checkout_root=root, now=datetime.now(UTC),
            status=status, release_commit_sha=commit, manifest=recorded_manifest,
            release_mode=WITHOUT_GIT if without_git else "git",
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
        updated = store_version_files(conn, pid, files, datetime.now(UTC))
        return {**_contract(_contract_row(conn, pid)), "updated": updated}


def store_version_files(conn: sa.Connection, pid: int, files: list[str], now: datetime) -> bool:
    """Write the contract `files` (already normalized) on the caller's
    connection — set_version_files' write, shared with the stamp that
    approves a proposal (changelog.release_entries consume_proposal, K-76):
    the same set again is a no-op (False); a different set replaces the
    contract and appends the previous one to the history ledger (True)."""
    current = _contract(_contract_row(conn, pid))
    if current["version_files"] == files:
        return False
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
    return True


# ── the project's PENDING proposal (K-76) ────────────────────────────────────


def _pending(text: str | None) -> dict | None:
    if text is None:
        return None
    proposal = json.loads(text)
    return {"proposal": proposal, "proposal_sha256": proposal_digest(proposal)}


def set_pending_proposal(engine: sa.Engine, project_slug: str, proposal: Mapping) -> dict:
    """Store `proposal` (validate_proposal — ValueError / TypeError, nothing
    written) as the project's PENDING version-file proposal — the result of
    `dct release prepare`, waiting for the owner's approval. A new prepare
    REPLACES an earlier pending proposal (only the latest one can be
    approved; its digest differs whenever its content does). Being pending
    is not an approval: only the stamp that names its digest consumes it
    (changelog.release_entries consume_proposal). Returns {"proposal",
    "proposal_sha256"}."""
    canonical = validate_proposal(proposal)
    if canonical is None:
        raise TypeError("proposal must be a JSON object")
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        conn.execute(projects.update().where(projects.c.id == pid).values(
            release_proposal=_canonical(canonical), updated_at=datetime.now(UTC)))
    return {"proposal": canonical, "proposal_sha256": proposal_digest(canonical)}


def get_pending_proposal(engine: sa.Engine, project_slug: str) -> dict | None:
    """The project's pending proposal {"proposal", "proposal_sha256"} or
    None (nothing prepared, or consumed by a stamp). Read-only."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        return read_pending_proposal(conn, pid)


def read_pending_proposal(conn: sa.Connection, pid: int) -> dict | None:
    """get_pending_proposal on the caller's connection (the stamp reads it
    inside its own transaction)."""
    return _pending(conn.execute(
        sa.select(projects.c.release_proposal).where(projects.c.id == pid)).scalar())


def clear_pending_proposal(conn: sa.Connection, pid: int) -> None:
    """Drop the pending proposal on the caller's connection (the stamp that
    consumed it)."""
    conn.execute(projects.update().where(projects.c.id == pid).values(release_proposal=None))
