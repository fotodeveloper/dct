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
never takes over an unfinished release; K-106: the stamp compares the
executor its caller expects with the project's setting in ITS transaction —
check_expected_executor), `consent` (what the owner approved,
K-97 F4), `proposal` (the approved version-file proposal, K-76) and `render`
(the changelog render settings, K-90) — each JSON, validated, write-once.

K-115 — a status is never proof. `tag_proof` (the VERIFIED identity of the
release tag: name, the object the ref resolves to, the commit it targets,
annotated or lightweight) and `push_proof` (the verified push target, the
commit its tag resolved to, its branch tip) are write-once JSON the workflow
reports and the ledger stamps with `recorded_at` — they describe the state
at verification time, not a guarantee about later. A record WITH git cannot
be 'tagged', 'pushed' or 'completed' without its tag proof, nor 'pushed'
without its push proof, nor 'completed' without the push proof when its
consent approved a push (check_proof_requirements — every executor, also a
reconstruction). The explicit other paths: a consent with push_target null
(a local release) and a 'without_git' record (no tag, no push). A field
that is absent is never taken as proof (G3): a record older than a field
gets it with its next progress write or stays unproven — nothing is added
to old history after the fact.

The fields of ONE record must describe the SAME release
(check_record_consistency, G5 / G9): consent ↔ record (version, entry set,
candidate, render, executor, the digest of the RECORDED proposal), proposal
↔ record (version, candidate, release mode), the proofs ↔ the release commit
and the consented push target — checked before every write, also a repeat.

Who may write (ACTORS, K-85 / K-119 M5 / M6): every progress write and
every abandon names its actor and the actor is ALWAYS checked (default
'ledger' — there is no unchecked actor): a release dct's effects layer
executes (progress_owner 'dct') is advanced and abandoned only by 'dct' —
the local commands that hold the repository's operation lock.

The record is INSERTED by changelog.release_entries in the same transaction
as the stamp (insert_record — never a stamp without a record, never a record
without a stamp) or, for a release made before records existed, by
reconstruct_release (no re-stamp: the record is checked against the stamps
already in the database; its executor is the project's CURRENT setting — an
explicitly assigned responsibility for the record's further handling, not
historical provenance, G8). It is ADVANCED by the release workflow
(advance_release) after it verified each effect. get_release reads it back,
also "the latest unfinished release" so an interrupted /release can resume.

A record WITHOUT a consent (a reconstruction that ended before
'completed', a stamp through MCP release_changelog, an older record) is
never executed by dct automatically. It keeps the ledger path, and it can
be TAKEN OVER (take_over_release, K-119 M3 / K-117 J4): the owner's consent
— naming what was verified, `consent.takeover` — and the proposal derived
from the verified artifacts are recorded in one transaction, write-once,
without a re-stamp. That is the ONLY way a record becomes one dct executes
after its stamp: advance_release refuses a first consent on a 'dct' record.

A reconstruction records the SCOPE of its verification
(manifest["verification"], K-110): the version-file contract as it was —
null = UNKNOWN, a list = confirmed, [] = confirmed "no version files" — is
never merged; without git a recorded scope with a confirmed contract is
required (`version_files: []` alone is not evidence of a release).

The ledger only stores and cross-checks what the workflow reports; it never
reads files or git (the local workflow does — K-51/K-72).

At most one LIVE record per (project, version) — compared under the version
contract (1.5 == 1.5.0), enforced here under the project write lock, with a
partial unique index as the last guard. K-115 G6: a ledger that nevertheless
holds several live records of one SEMANTIC version (older data, raw SQL) is
AMBIGUOUS — every write refuses (unique_record: advance, abandon, the stamp
and its re-run, reconstruct, the history import, the pre-effect gate) and
never picks the first one; diagnostic reads keep working and name the
duplicates (get_release → `duplicates`).

K-102: the stamp is the ONLY writer of versions — changelog.add_entry
refuses a version and takes the same project lock; advance_release checks
the FULL entry set (check_entry_set) in its own transaction on every call.

The project's version-file contract (K-73: which repo files a release must
change) lives on the project row; get_version_files / set_version_files are
its readers and writers. So does the PENDING version-file proposal (K-76:
`projects.release_proposal`, written by `dct release prepare` through
set_pending_proposal, read by get_pending_proposal) — consumed by the stamp
that names its digest (changelog.release_entries consume_proposal), which
records it as the record's `proposal` and its paths as the contract
(store_version_files) in the stamp transaction. Beside it the PREVIEW slot
(`projects.release_preview`, set_preview / get_preview / discard_preview /
record_preview_progress): the consent views of the one-dialog release
(`dct release prepare --preview`), each keyed by its digest C
(consent_view_digest); a preview and a prepare replace each other's slot.
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
_TAGGED = RELEASE_FLOW.index("tagged")
_PUSHED = RELEASE_FLOW.index("pushed")

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
TAG_NAME_MAX = 255
ABANDON_REASON_MAX = 2000
VERSION_FILES_MAX = 200
VERSION_FILE_PATH_MAX = 500

RECORD_FIELDS = (
    "id", "version", "mode", "entry_ids", "candidate_sha", "checkout_root",
    "release_commit_sha", "manifest", "proof", "status", "created_at",
    "updated_at", "completed_at", "abandoned_at", "abandon_reason",
    "release_mode", "executor", "consent", "proposal", "render",
    "tag_proof", "push_proof",
)
# JSON-encoded record columns (decoded by record_to_dict).
_JSON_FIELDS = ("manifest", "proof", "consent", "proposal", "render", "tag_proof", "push_proof")
# The ledger's own stamp inside a stored tag_proof / push_proof: WHEN the
# proof was recorded (the state it describes is the state at verification
# time — never a guarantee about later). Never accepted from a caller.
PROOF_STAMP = "recorded_at"


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
# K-116 H7 — where a proof's `pr` number comes from: "git" = the release
# plan recognised it in the repository (the `Merge pull request #N` subject
# of the entry's merge); "client" = the caller DECLARED it (an attestation —
# no git read and no hosting query backs it). The WRITER states it, never
# the map: see validate_proof.
PR_SOURCES = ("git", "client")
_PROOF_KEYS = frozenset({"commits", "merge_commit", "pr", "pr_source", "source", "ambiguous",
                         "associations"})


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
        if item["source"] == "subject_suffix" and item["certain"]:
            raise ValueError(f"{where}: a subject_suffix number is a heuristic (it may be an "
                             f"issue reference) — it is never certain")
        out.append({"merge_commit": validate_sha(item["merge_commit"], f"{where}.merge_commit"),
                    "pr": _pr_number(item["pr"], f"{where}.pr"),
                    "source": item["source"], "certain": item["certain"]})
    return out


def validate_proof(proof: Mapping | None, pr_source: str = "client") -> dict | None:
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
    'introducing_merge' | 'subject_suffix'), certain (bool; never true for
    'subject_suffix')}], at most ASSOCIATIONS_MAX — a longer list REFUSES,
    it is never cut; every recognition, a `(#N)` subject suffix listed as
    uncertain, never as `pr`).

    K-116 H7 — `pr_source` (PR_SOURCES) is the origin of EVERY `pr` number
    of this map, stated by the WRITER, not by the map: 'client' (default —
    every caller that does not read the repository itself: MCP
    release_changelog, a direct core call) = a declared number, a client
    attestation; 'git' (the release plan and the effects layer that stamps
    its approved snapshot) = recognised in the repository. Each object with
    a `pr` is stored with "pr_source": <that value>:
      - 'client': whatever "pr_source" the map states is REPLACED (a map
        copied from `dct release plan` still arrives through a surface that
        verified nothing);
      - 'git': the object must carry the recognition the number comes from
        — "source" own_merge / introducing_merge with its merge_commit — and
        must not state "pr_source": "client"; ValueError otherwise. A
        heuristic `(#N)` suffix can therefore never be stored as a
        recognised PR.
    An object without a `pr` has no origin: a non-null "pr_source" there
    raises ValueError; a null one is kept as given.

    Stored as {"<id>": {"commits": [sorted, deduplicated, lowercased],
    "merge_commit": ..., "pr": ...[, "pr_source", the optional keys
    given]}}. Any other shape — or a contradiction (ambiguous with a
    merge_commit / pr, a source without a merge_commit) — raises ValueError.
    Entries without a commit proof (e.g. attested) are simply absent from
    the map. A non-object map / proof value / association, a non-list
    `associations`, a non-bool `ambiguous` / `certain` or a bool key raises
    TypeError."""
    if pr_source not in PR_SOURCES:
        raise ValueError(f"pr_source must be one of {', '.join(PR_SOURCES)}, got {pr_source!r}")
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
        stated = raw.get("pr_source")
        if stated is not None and stated not in PR_SOURCES:
            raise ValueError(f"proof[{entry}].pr_source must be one of "
                             f"{', '.join(PR_SOURCES)} or null")
        if item["pr"] is None:
            if stated is not None:
                raise ValueError(f"proof[{entry}].pr_source names an origin but pr is null")
            if "pr_source" in raw:
                item["pr_source"] = None
        else:
            if pr_source == "git" and (stated == "client" or item.get("source") is None):
                raise ValueError(
                    f"proof[{entry}].pr_source: a PR number recognised in git comes from "
                    f"the entry's merge (source own_merge / introducing_merge with its "
                    f"merge_commit), never from a client declaration or a heuristic")
            item["pr_source"] = pr_source
        out[str(entry)] = item
    if not out:
        return None
    ensure_json_fits(out, "proof", PROOF_MAX_BYTES)
    return out


def proof_pr(proof: Mapping | None, entry_id: int) -> int | None:
    """The PR number the changelog may render for `entry_id` (K-90): the
    stored map's `pr` when it is set and the map does not mark the entry
    ambiguous — never a heuristic number (the plan never stores one as
    `pr`, K-95 C4). Its origin (recognised in git, or a client attestation)
    is `pr_source` of the same object — see proof_pr_origins."""
    item = (proof or {}).get(str(entry_id))
    if not isinstance(item, Mapping) or item.get("ambiguous"):
        return None
    pr = item.get("pr")
    return pr if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0 else None


def proof_pr_origins(proof: Mapping | None) -> dict[int, dict]:
    """entry id → {"pr", "pr_source" ('git' | 'client' | None = a map stored
    before the origin was recorded)} for every entry whose
    stored proof names a `pr` (K-116 H7) — what a surface SHOWS next to the
    number, so a declared PR is never read as one recognised in git."""
    out = {}
    for key, item in (proof or {}).items():
        if isinstance(item, Mapping) and item.get("pr") is not None:
            out[int(key)] = {"pr": item["pr"], "pr_source": item.get("pr_source")}
    return dict(sorted(out.items()))


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
    if not isinstance(executor, str) or executor not in EXECUTORS:
        raise ValueError(
            f"release executor must be one of {', '.join(EXECUTORS)}, got {executor!r}"
        )
    return executor


def progress_owner(record: Mapping) -> str:
    """Who executes the release of `record` (K-85, the RECORD decides —
    never today's project setting): 'dct' when its executor is 'dct' (NULL
    on records older than the field counts as 'dct') AND it carries the
    owner's consent (it was stamped by `dct release apply`, or handed to
    dct by take_over_release — K-119 M3); else 'ledger' —
    an 'own' record, or one stamped outside the effects layer (MCP
    release_changelog, older records, a partial reconstruction): its
    effects are performed and verified by whoever reports them through the
    ledger. `record` is a record dict (get_release) or a releases row."""
    get = record.get if isinstance(record, Mapping) else lambda k: getattr(record, k)
    executor = get("executor") or DEFAULT_EXECUTOR
    return "dct" if executor == "dct" and get("consent") is not None else "ledger"


def validate_actor(actor: str) -> str:
    """'dct' | 'ledger' (ACTORS) → itself; anything else — None included
    (K-119 M5: there is no "unchecked" actor) — raises ValueError."""
    if not isinstance(actor, str) or actor not in ACTORS:
        raise ValueError(f"actor must be one of {', '.join(ACTORS)}, got {actor!r}")
    return actor


def check_progress_actor(record: Mapping, actor: str) -> None:
    """K-85 / K-96 D10 — refuse a progress write by the wrong actor
    (ACTORS): a release dct executes is advanced only by the effects layer
    (`dct release resume` verifies each effect), any other only through the
    ledger path. ValueError otherwise."""
    validate_actor(actor)
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


def check_abandon_actor(record: Mapping, actor: str) -> None:
    """K-119 M6 — who may END a release as abandoned: a release dct's
    effects layer executes (progress_owner 'dct') only by the actor 'dct' —
    the LOCAL `dct release abandon`, which holds the repository's operation
    lock, so no apply / resume of that release can be running; a caller
    without that lock (actor 'ledger': MCP abandon_release, a direct core
    call) is refused. A record the ledger path owns is abandoned by either
    actor. ValueError otherwise."""
    validate_actor(actor)
    if actor == "dct" or progress_owner(record) != "dct":
        return
    get = record.get if isinstance(record, Mapping) else lambda k: getattr(record, k)
    raise ValueError(
        f"Release {get('version')} is executed by dct's effects layer (stamped by `dct "
        f"release apply` with the owner's consent) — it is abandoned only by the local "
        f"command, which holds the release operation lock of its repository (an abandon "
        f"from here could not stop a running apply / resume): run `dct release abandon "
        f"--version {get('version')} --reason \"…\" --confirm` in the project's checkout; "
        f"nothing written."
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
    "version", "plan_digest", "release_mode", "changelog", "id",
})
PROPOSAL_ID_RE = re.compile(r"[0-9A-Za-z]{1,64}", re.ASCII)
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
    EXACTLY `paths`. Blob ids are hex 7-64 chars (lowercased).

    "id" (optional; K-117) — the STABLE identity `dct release prepare`
    gives each proposal it builds (a ULID: letters and digits, ≤ 64): it
    stays the same from the pending proposal to the record that consumed
    it, and names the resources kept for it (the ref pinning its blobs).
    Neither the version nor the content: two prepares of the same content
    have different ids (and so different digests). A proposal stored before
    the field has none — the key is then ABSENT from the canonical form, so
    its digest is unchanged.

    ≤ PROPOSAL_MAX_BYTES of canonical JSON. ValueError / TypeError
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
    identity = proposal.get("id")
    if identity is not None:
        if not isinstance(identity, str) or not PROPOSAL_ID_RE.fullmatch(identity):
            raise ValueError(f"proposal.id must be 1-64 letters / digits, got {identity!r}")
        out["id"] = identity
    ensure_json_fits(out, "proposal", PROPOSAL_MAX_BYTES)
    return out


def proposal_digest(proposal: Mapping) -> str:
    """SHA-256 of the canonical JSON of a validated proposal — what a consent
    names (consent.proposal_sha256) so a changed proposal voids it."""
    import hashlib

    return hashlib.sha256(_canonical(validate_proposal(proposal)).encode("utf-8")).hexdigest()


CONSENT_KEYS = frozenset({
    "candidate_sha", "entry_ids", "version", "proposal_sha256", "render", "push_target",
    "executor", "takeover", "content_sha256", "changelog_sha256",
})
PUSH_TARGET_KEYS = frozenset({"remote", "url", "branch", "tag"})
# K-119 M3: what a TAKEOVER consent names as verified besides the consent's
# own version / entry set / candidate / push target (take_over_release).
TAKEOVER_KEYS = frozenset({"release_commit_sha", "tree", "artifacts_sha256"})
# The manifest keys that describe the release's artifacts (artifacts_digest).
ARTIFACT_KEYS = ("tree", "changelog", "version_files")


def _push_target(target, field: str) -> dict | None:
    """A push target {"remote" (required), "url", "branch", "tag"} in
    canonical form (all four keys, null when not given), or None."""
    if target is None:
        return None
    _object(target, field, set(PUSH_TARGET_KEYS), {"remote"})
    return {key: _text(target.get(key), f"{field}.{key}", optional=key != "remote")
            for key in sorted(PUSH_TARGET_KEYS)}


TAG_PROOF_KEYS = frozenset({"name", "object", "commit", "annotated"})
PUSH_PROOF_KEYS = frozenset({"target", "tag_commit", "branch_tip"})


def _full_sha(value, field: str) -> str:
    sha = validate_sha(value, field) if isinstance(value, str) else None
    if sha is None:
        raise ValueError(f"{field} must be a hex commit id of 7-64 characters, got {value!r}")
    return sha


def validate_tag_proof(tag: Mapping | None) -> dict | None:
    """The VERIFIED identity of a release tag (K-115 G4) in canonical form,
    or None. The caller that verified the repository reports it; the ledger
    enforces its consistency with the record (check_record_consistency).

    {"name": the tag name as `git tag` lists it (unpadded, no whitespace, at
    most TAG_NAME_MAX chars, never a `refs/...` path), "object": what
    `refs/tags/<name>` resolves to (the tag OBJECT of an annotated tag, the
    commit of a lightweight one), "commit": the commit the tag targets
    (peeled), "annotated": bool} — all four required, ids hex 7-64 chars
    (lowercased; report FULL ids — they are compared with the record's
    release commit). A lightweight tag has object == commit, an annotated
    one object != commit. Unknown keys (also `recorded_at` — the ledger's
    own stamp) raise ValueError; a non-object or a non-bool `annotated`
    raises TypeError."""
    if tag is None:
        return None
    _object(tag, "tag_proof", set(TAG_PROOF_KEYS), set(TAG_PROOF_KEYS))
    name = tag["name"]
    if (not isinstance(name, str) or not name or len(name) > TAG_NAME_MAX
            or name.startswith("refs/") or any(ch.isspace() for ch in name)):
        raise ValueError(
            f"tag_proof.name must be the tag name as `git tag` lists it (no `refs/tags/` "
            f"prefix, no whitespace, at most {TAG_NAME_MAX} chars), got {name!r}"
        )
    if not isinstance(tag["annotated"], bool):
        raise TypeError(f"tag_proof.annotated must be true or false, got {tag['annotated']!r}")
    out = {"name": name, "object": _full_sha(tag["object"], "tag_proof.object"),
           "commit": _full_sha(tag["commit"], "tag_proof.commit"),
           "annotated": tag["annotated"]}
    if out["annotated"] and out["object"] == out["commit"]:
        raise ValueError("tag_proof: an annotated tag resolves to its tag OBJECT, never to "
                         "the commit itself (object == commit)")
    if not out["annotated"] and out["object"] != out["commit"]:
        raise ValueError("tag_proof: a lightweight tag resolves to the commit itself "
                         "(object must equal commit)")
    return out


def validate_push_proof(push: Mapping | None) -> dict | None:
    """The VERIFIED push of a release (K-115 G4) in canonical form, or None:
    {"target": {"remote" (required), "url", "branch", "tag" (required)} —
    the push target that was read back, shaped like consent.push_target,
    "tag_commit": the commit the target's tag resolved to (required),
    "branch_tip": the commit the target's branch showed, or null}. Ids hex
    7-64 chars (lowercased; FULL ids). ValueError / TypeError otherwise."""
    if push is None:
        return None
    _object(push, "push_proof", set(PUSH_PROOF_KEYS), {"target", "tag_commit"})
    target = _push_target(push["target"], "push_proof.target")
    if target is None or target["tag"] is None:
        raise ValueError("push_proof.target needs the remote and the tag that were verified")
    tip = push.get("branch_tip")
    return {"target": target,
            "tag_commit": _full_sha(push["tag_commit"], "push_proof.tag_commit"),
            "branch_tip": None if tip is None else _full_sha(tip, "push_proof.branch_tip")}


def _unstamped(proof: Mapping | None) -> dict | None:
    """A stored tag_proof / push_proof without the ledger's recorded_at."""
    if proof is None:
        return None
    return {key: value for key, value in proof.items() if key != PROOF_STAMP}


def _is_version_tag(name: str, version: str) -> bool:
    try:
        return same_version(normalize_version(name), version)
    except ValueError:
        return False


def check_record_consistency(state: Mapping) -> None:
    """K-115 G4 / G5 / G9 — the fields of ONE record must describe the SAME
    release; each JSON being valid alone is not enough. `state` is a record
    dict (get_release) or the post-state a write is about to store. Checked
    before every ledger write (insert_record, advance_release — also a
    repeat of the current status) and by the effects layer before any
    effect; ValueError naming the first contradiction, nothing repaired:

    - consent ↔ record: the same version (version contract), EXACTLY the
      record's entry_ids, the same candidate_sha; `render` equal when both
      are recorded; `executor` (when the consent names one) = the record's
      (null counts as 'dct'); `proposal_sha256` = proposal_digest of the
      RECORDED proposal — a digest without a proposal, or a proposal with a
      consent that names none, is a contradiction; `takeover` (K-119 M3,
      when the consent carries one) names the record's release commit, the
      manifest's tree and artifacts_digest of the recorded manifest
      (`state["manifest"]`); `content_sha256` (the approved consent view)
      only beside a `proposal_sha256` — the view binds a proposal;
      `changelog_sha256` (the approved composition) only beside a recorded
      proposal that approves a changelog file (its `changelog` baseline);
    - proposal ↔ record: its `version`, `candidate_sha` and `release_mode`
      (each when bound) are the record's;
    - tag_proof: only on a record with git; its `commit` is the record's
      release_commit_sha; for a release dct executes its `name` is a
      spelling of the version (an own tool's tag name is its own format);
    - push_proof: only on a record with git; `tag_commit` is the release
      commit; with a consent its `target` is EXACTLY consent.push_target —
      and a consent that approved no push (push_target null) has none.
    A field that is absent proves nothing and is never treated as a match
    for a recorded one (G3)."""
    version = state["version"]

    def refuse(what: str) -> ValueError:
        return ValueError(
            f"release {version}: {what} — the record, its consent, proposal, render and "
            f"proofs must describe the same release; nothing written."
        )

    consent, proposal, render = state.get("consent"), state.get("proposal"), state.get("render")
    candidate, commit = state.get("candidate_sha"), state.get("release_commit_sha")
    mode = state.get("release_mode")
    if consent is not None:
        if consent.get("content_sha256") is not None and consent.get("proposal_sha256") is None:
            raise refuse("consent.content_sha256 names an approved consent view but the "
                         "consent names no proposal (proposal_sha256 is null)")
        if consent.get("changelog_sha256") is not None and (proposal is None or
                                                            proposal.get("changelog") is None):
            raise refuse("consent.changelog_sha256 binds a changelog composition but the "
                         "recorded proposal approves no changelog file")
        if not same_version(consent["version"], version):
            raise refuse(f"consent.version {consent['version']} is not the record's version")
        if sorted(consent["entry_ids"]) != sorted(state["entry_ids"]):
            raise refuse(f"consent.entry_ids {sorted(consent['entry_ids'])} differ from the "
                         f"record's {sorted(state['entry_ids'])}")
        if consent.get("candidate_sha") != candidate:
            raise refuse(f"consent.candidate_sha {consent.get('candidate_sha')} is not the "
                         f"record's candidate {candidate}")
        if consent.get("render") is not None and render is not None \
                and _canonical(consent["render"]) != _canonical(render):
            raise refuse(f"consent.render {consent['render']} differs from the record's "
                         f"render {render}")
        executor = state.get("executor") or DEFAULT_EXECUTOR
        if consent.get("executor") is not None and consent["executor"] != executor:
            raise refuse(f"consent.executor '{consent['executor']}' is not the record's "
                         f"executor '{executor}'")
        digest = consent.get("proposal_sha256")
        if digest is not None and (proposal is None or proposal_digest(proposal) != digest):
            raise refuse("consent.proposal_sha256 does not name the recorded proposal")
        if digest is None and proposal is not None:
            raise refuse("consent.proposal_sha256 is null but a proposal is recorded")
        taken = consent.get("takeover")
        if taken is not None:
            manifest = state.get("manifest") or {}
            if taken["release_commit_sha"] != commit:
                raise refuse(f"consent.takeover names the release commit "
                             f"{taken['release_commit_sha']}, the record's release commit is "
                             f"{commit}")
            if taken["tree"] != manifest.get("tree"):
                raise refuse(f"consent.takeover.tree {taken['tree']} is not the manifest's "
                             f"tree {manifest.get('tree')}")
            if taken["artifacts_sha256"] != artifacts_digest(manifest):
                raise refuse("consent.takeover.artifacts_sha256 does not name the recorded "
                             "artifacts (manifest tree / changelog / version_files)")
    if proposal is not None:
        if proposal.get("version") is not None and not same_version(proposal["version"], version):
            raise refuse(f"proposal.version {proposal['version']} is not the record's version")
        if proposal.get("candidate_sha") is not None and proposal["candidate_sha"] != candidate:
            raise refuse(f"proposal.candidate_sha {proposal['candidate_sha']} is not the "
                         f"record's candidate {candidate}")
        if proposal.get("release_mode") is not None and mode is not None \
                and proposal["release_mode"] != mode:
            raise refuse(f"proposal.release_mode '{proposal['release_mode']}' is not the "
                         f"record's release_mode '{mode}'")
    tag, push = state.get("tag_proof"), state.get("push_proof")
    if mode == WITHOUT_GIT and (tag is not None or push is not None):
        raise refuse("a release without git has no tag and no push (tag_proof / push_proof)")
    if tag is not None:
        if commit is None or tag["commit"] != commit:
            raise refuse(f"tag_proof.commit {tag['commit']} is not the release commit {commit}")
        if progress_owner(state) == "dct" and not _is_version_tag(tag["name"], version):
            raise refuse(f"tag_proof.name {tag['name']!r} is not a tag of {version}")
    if push is not None:
        if commit is None or push["tag_commit"] != commit:
            raise refuse(f"push_proof.tag_commit {push['tag_commit']} is not the release "
                         f"commit {commit}")
        if consent is not None:
            approved = consent.get("push_target")
            if approved is None:
                raise refuse("push_proof contradicts the consent: no push was approved "
                             "(push_target null — an explicit local release)")
            if _canonical(push["target"]) != _canonical(approved):
                raise refuse(f"push_proof.target {push['target']} is not the approved push "
                             f"target {approved}")


def check_proof_requirements(state: Mapping, status: str) -> None:
    """K-115 G4 — a status is never proof: what a record WITH git must carry
    to be at `status` (checked on every move into it, at reconstruction
    too, for every executor). ValueError otherwise.

    - 'tagged' and later ('pushed', 'completed'): `tag_proof`, the verified
      tag identity targeting the release commit;
    - 'pushed': `push_proof`;
    - 'completed' when the recorded scope includes a push
      (consent.push_target set): `push_proof` for exactly that target.
    Separate, explicit paths: a consent with push_target null (an explicit
    local release, `--no-push`) and a record without a consent (its push
    scope is not recorded) complete from 'tagged' without a push; a
    'without_git' record has no tag and no push at all."""
    if state.get("release_mode") == WITHOUT_GIT or status not in RELEASE_FLOW:
        return
    version, rank = state["version"], _rank(status)
    if rank >= _TAGGED and state.get("tag_proof") is None:
        raise ValueError(
            f"Release {version} cannot be '{status}' without its tag_proof — the VERIFIED "
            f"identity of its tag ({{name, object, commit, annotated}}, the commit being the "
            f"release commit); a status alone is never proof that the tag exists; nothing "
            f"written."
        )
    approved = (state.get("consent") or {}).get("push_target")
    if state.get("push_proof") is None and (
            status == "pushed" or (status == "completed" and approved is not None)):
        why = "its consent approved a push" if approved is not None else \
            "'pushed' records a verified push"
        raise ValueError(
            f"Release {version} cannot be '{status}' without its push_proof ({why}) — the "
            f"VERIFIED push target ({{target, tag_commit, branch_tip}}); a release without a "
            f"push completes from 'tagged' (an explicit local release); nothing written."
        )


def artifacts_digest(manifest: Mapping | None) -> str:
    """SHA-256 of the canonical JSON of the manifest's ARTIFACT keys
    (ARTIFACT_KEYS: `tree`, `changelog`, `version_files` — those present) —
    what a takeover consent names as the verified artifact hashes
    (consent.takeover.artifacts_sha256). Other manifest keys (the untracked
    baseline, the verification scope) are not artifacts."""
    import hashlib

    artifacts = {key: (manifest or {})[key] for key in ARTIFACT_KEYS if key in (manifest or {})}
    return hashlib.sha256(_canonical(artifacts).encode("utf-8")).hexdigest()


def _takeover(value) -> dict:
    _object(value, "consent.takeover", set(TAKEOVER_KEYS), set(TAKEOVER_KEYS))
    digest = _digest(value["artifacts_sha256"], "consent.takeover.artifacts_sha256")
    if digest is None:
        raise ValueError("consent.takeover.artifacts_sha256 must be a hex SHA-256")
    return {
        "release_commit_sha": _full_sha(value["release_commit_sha"],
                                        "consent.takeover.release_commit_sha"),
        "tree": _full_sha(value["tree"], "consent.takeover.tree"),
        "artifacts_sha256": digest,
    }


def validate_consent(consent: Mapping | None) -> dict | None:
    """What the owner approved for this release (K-97 F4) in canonical form,
    or None — so a resume can tell whether the scope changed.

    {"version": V (normalized, REQUIRED), "entry_ids": [ids] (REQUIRED,
    sorted, deduplicated), "candidate_sha": sha | null, "proposal_sha256":
    proposal_digest of the approved version-file proposal | null, "render":
    validate_render | null, "push_target": null | {"remote" (required),
    "url", "branch", "tag"} (strings)} plus the OPTIONAL "executor" ('dct' |
    'own', K-106 — who the owner approved to perform the release; the stamp
    compares it with the project's setting in its transaction; the key is
    kept only when given, so consents recorded before it compare
    unchanged) and the OPTIONAL "takeover" (K-119 M3, kept only when given —
    the consent of take_over_release: what was VERIFIED when the owner
    handed an existing release to dct, {"release_commit_sha": the verified
    release commit, "tree": its tree, "artifacts_sha256": artifacts_digest
    of the verified manifest} — all three required, FULL ids) and the
    OPTIONAL "content_sha256" (kept only when given — the digest C of the
    consent VIEW the owner approved on the one-dialog screen, `dct release
    prepare --preview`; hex SHA-256, lowercased; a consent that names it
    must also name its proposal_sha256, check_record_consistency) and the
    OPTIONAL "changelog_sha256" (kept only when given — the sha256 of the
    composed changelog file in its work-tree form the owner approved on that
    screen, consent view `changelog.composed_sha256`; the file write after
    the stamp and every resume refuse another composition — so released
    history imported between the stamp and the write never lands unseen;
    consents recorded before it, and those that approved no changelog file,
    carry none and are not checked). ≤ CONSENT_MAX_BYTES of canonical JSON. ValueError / TypeError
    otherwise."""
    if consent is None:
        return None
    _object(consent, "consent", set(CONSENT_KEYS), {"version", "entry_ids"})
    raw_ids = consent["entry_ids"]
    if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, (list, tuple)):
        raise TypeError("consent.entry_ids must be a list of entry ids")
    ids = sorted({_entry_id(i) for i in raw_ids})
    push = _push_target(consent.get("push_target"), "consent.push_target")
    out = {
        "version": normalize_version(_text(consent["version"], "consent.version", 50)),
        "entry_ids": ids,
        "candidate_sha": validate_sha(consent.get("candidate_sha"), "consent.candidate_sha"),
        "proposal_sha256": _digest(consent.get("proposal_sha256"), "consent.proposal_sha256"),
        "render": validate_render(consent.get("render")),
        "push_target": push,
    }
    if consent.get("executor") is not None:
        out["executor"] = validate_executor(consent["executor"])
    if consent.get("takeover") is not None:
        out["takeover"] = _takeover(consent["takeover"])
    for key in ("content_sha256", "changelog_sha256"):
        value = consent.get(key)
        if value is not None:
            out[key] = _digest(value.lower() if isinstance(value, str) else value,
                               f"consent.{key}")
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


def live_records(conn: sa.Connection, project_id: int) -> list:
    """Every LIVE release record of the project, oldest id first."""
    return list(conn.execute(
        sa.select(releases).where(
            releases.c.project_id == project_id, releases.c.deleted_at.is_(None)
        ).order_by(releases.c.id)
    ).all())


def record_versions(engine: sa.Engine, project_slug: str) -> list[str]:
    """The version of EVERY live release record of the project, whatever its
    status — `abandoned` included (an abandoned version stays taken) —
    oldest id first, as stored. Read-only; the release plan's version
    suggestion skips these versions."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        return list(conn.execute(
            sa.select(releases.c.version).where(
                releases.c.project_id == pid, releases.c.deleted_at.is_(None)
            ).order_by(releases.c.id)
        ).scalars())


def find_records(conn: sa.Connection, project_id: int, version: str) -> list:
    """EVERY live record of `version` under the version contract (1.1 ==
    1.1.0 == v1.1), oldest id first. More than one is an AMBIGUOUS ledger
    (K-115 G6): the unique index compares the stored text only, and data
    written before it existed may violate even that."""
    return [row for row in live_records(conn, project_id) if same_version(row.version, version)]


def find_record(conn: sa.Connection, project_id: int, version: str):
    """The LIVE record of `version` (version contract equality), or None —
    for READS: with duplicates it returns the oldest one (the caller names
    the others, see get_release). Writers use unique_record."""
    rows = find_records(conn, project_id, version)
    return rows[0] if rows else None


def describe_duplicates(rows) -> list[dict]:
    """[{id, version, status}] of the given record rows (oldest first)."""
    return [{"id": row.id, "version": row.version, "status": row.status} for row in rows]


def unique_record(conn: sa.Connection, project_id: int, project_slug: str, version: str):
    """The ONE live record of `version`, or None — for every WRITE (advance,
    abandon, the stamp and its re-run, reconstruct, the history import, the
    pre-effect gate). K-115 G6: several live records of the same SEMANTIC
    version make the ledger ambiguous — a write never picks the first one;
    ValueError naming every duplicate, nothing written."""
    rows = find_records(conn, project_id, version)
    if len(rows) > 1:
        listing = ", ".join(f"#{d['id']} ({d['version']}, {d['status']})"
                            for d in describe_duplicates(rows))
        raise ValueError(
            f"the release ledger of '{project_slug}' holds {len(rows)} live records of the "
            f"same version: {listing} — a write never picks one of them. Inspect them "
            f"(`dct release status --version {rows[0].version} --json` lists `duplicates`), "
            f"soft-delete the wrong row(s) by hand (releases.deleted_at), then run the "
            f"command again; nothing written."
        )
    return rows[0] if rows else None


def project_release_settings(conn: sa.Connection, project_id: int) -> dict:
    """The project's release settings read on the caller's connection:
    {executor ('dct' | 'own', K-85), render ({"pr_refs": bool}, K-90),
    changelog (the settings that choose the changelog file —
    core.changelog_file.choice_settings: {managed, explicit, path})}."""
    from dct.core.changelog_file import choice_settings

    row = conn.execute(
        sa.select(projects.c.release_executor, projects.c.changelog_pr_refs,
                  projects.c.manages_changelog, projects.c.changelog_path,
                  projects.c.changelog_path_explicit)
        .where(projects.c.id == project_id)
    ).first()
    return {
        "executor": row.release_executor or DEFAULT_EXECUTOR,
        "render": {"pr_refs": bool(row.changelog_pr_refs)},
        "changelog": choice_settings(row._mapping),
    }


def check_expected_executor(conn: sa.Connection, project_id: int, project_slug: str,
                            expected: str | None) -> None:
    """K-106 — inside the STAMP transaction, before any write: the executor
    the caller's plan / consent expects (`expected`; None = not bound) must
    be the project's CURRENT release_executor (read on the stamp's
    connection, under the project lock set_release_executor also takes).
    ValueError otherwise — nothing stamped, no record, no contract change."""
    if expected is None:
        return
    current = project_release_settings(conn, project_id)["executor"]
    if current != expected:
        raise ValueError(
            f"the approved release expects the executor '{expected}', but the release "
            f"executor of '{project_slug}' is now '{current}' — the setting changed since "
            f"the plan; plan again under the current executor (`dct project "
            f"release-executor`); nothing released."
        )


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
    tag_proof: dict | None = None,
    push_proof: dict | None = None,
) -> dict:
    """Insert a record on the caller's (stamping) connection.

    Callers have validated every value and checked unique_record under the
    same project write lock; the partial unique index still refuses a duplicate.
    The version must fit the schema (ensure_version_fits — the value actually
    stored, K-87). `executor` is copied from the project's release_executor
    (K-85) — never a parameter. A record inserted as 'completed'
    (reconstruction) gets completed_at = now. K-115: the record's fields
    must agree with EACH OTHER (check_record_consistency) and carry the
    proofs its status needs (check_proof_requirements; `tag_proof` /
    `push_proof` — validated — are stamped with recorded_at = now) —
    ValueError before the INSERT otherwise.
    """
    if mode not in RELEASE_MODES:
        raise ValueError(f"Invalid release mode: '{mode}'")
    if status not in RELEASE_FLOW:
        raise ValueError(f"Invalid initial release status: '{status}'")
    ensure_version_fits(version)
    check_mode_consistency(release_mode, candidate_sha, release_commit_sha, status)
    settings = project_release_settings(conn, project_id)
    _check_proof_timing(tag_proof, push_proof, status)
    state = {
        "version": version, "entry_ids": list(entry_ids), "candidate_sha": candidate_sha,
        "release_commit_sha": release_commit_sha, "release_mode": release_mode,
        "executor": settings["executor"], "consent": consent, "proposal": proposal,
        "render": render, "tag_proof": tag_proof, "push_proof": push_proof,
        "manifest": manifest,
    }
    check_record_consistency(state)
    check_proof_requirements(state, status)
    stamp = {PROOF_STAMP: now.astimezone(UTC).isoformat()}
    tag_proof = None if tag_proof is None else {**tag_proof, **stamp}
    push_proof = None if push_proof is None else {**push_proof, **stamp}

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
            tag_proof=encoded(tag_proof),
            push_proof=encoded(push_proof),
            created_at=now,
            updated_at=now,
            completed_at=now if status == "completed" else None,
        ).returning(releases)
    ).first()
    return record_to_dict(row)


def _check_proof_timing(tag_proof, push_proof, status: str) -> None:
    """A tag proof belongs to 'tagged' or later, a push proof to 'pushed' or
    later — ValueError for a premature one."""
    rank = _rank(status)
    if tag_proof is not None and rank < _TAGGED:
        raise ValueError(
            f"tag_proof is recorded on the move to 'tagged' or later, not at '{status}'; "
            f"nothing written."
        )
    if push_proof is not None and rank < _PUSHED:
        raise ValueError(
            f"push_proof is recorded on the move to 'pushed' or later, not at '{status}'; "
            f"nothing written."
        )


def _require_record(conn: sa.Connection, pid: int, project_slug: str, version: str):
    """The ONE live record of `version` for a write (unique_record — an
    ambiguous ledger refuses), ValueError when there is none."""
    row = unique_record(conn, pid, project_slug, version)
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

    K-115 G6 — a diagnostic read keeps working on an AMBIGUOUS ledger:
    when several live records hold the same semantic version (1.1 and
    1.1.0), the oldest one is returned with the extra key `duplicates`
    ([{id, version, status}] of ALL of them, oldest first). The key is
    absent on a healthy ledger. Every WRITE refuses such a version
    (unique_record).
    """
    if version:
        version = normalize_version(version)
    with engine.begin() as conn:
        return _read_release(conn, resolve_project_id(conn, project_slug), version)


def _read_release(conn: sa.Connection, pid: int, version: str | None) -> dict | None:
    """get_release on the caller's connection (`version` already normalized)."""
    row = find_record(conn, pid, version) if version else _latest_unfinished(conn, pid)
    if row is None:
        return None
    record = record_to_dict(row)
    same = find_records(conn, pid, row.version)
    if len(same) > 1:
        record["duplicates"] = describe_duplicates(same)
    return record


def release_routing(engine: sa.Engine, project_slug: str) -> tuple[dict, dict | None]:
    """(routing, unfinished record) — who performs a release of the project,
    read in ONE transaction so a dialog routes from a single result (K-119,
    K-111). Read-only.

    routing = {"project": the project's CURRENT release_executor ('dct' |
    'own') — it decides only a NEW release; "record": None, or for the
    unfinished record (get_release without a version — returned as the
    second element) {"version", "status", "executor" (the value STORED on
    the record, immutable; null on a record older than the field, which
    counts as 'dct'), "progress_owner" ('dct' = stamped by `dct release
    apply`, only `dct release resume` / `abandon` continue it | 'ledger' =
    advanced through the ledger path by whoever performs its effects —
    progress_owner), "consent" (bool: the record carries the owner's
    consent)}}. An unfinished release is routed by its RECORD, never by
    today's project setting."""
    with engine.begin() as conn:
        pid = resolve_project_id(conn, project_slug)
        record = _read_release(conn, pid, None)
        routing = {"project": project_release_settings(conn, pid)["executor"], "record": None}
    if record is not None:
        routing["record"] = {
            "version": record["version"], "status": record["status"],
            "executor": record["executor"], "progress_owner": progress_owner(record),
            "consent": record["consent"] is not None,
        }
    return routing, record


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
    ValueError when there is no such record, the sets differ (check_entry_set
    — the record is never extended) or the ledger holds several live
    records of that version (K-115 G6 — the gate of every effect is a write
    gate: unique_record). Read-only: one consistent snapshot."""
    if version:
        version = normalize_version(version)
    with project_transaction(engine, project_slug) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version) if version else \
            _latest_unfinished(conn, pid)
        if row is None:
            raise ValueError(f"No unfinished release record in '{project_slug}'.")
        row = _require_record(conn, pid, project_slug, row.version)
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
    actor: str = "ledger",
    tag_proof: Mapping | None = None,
    push_proof: Mapping | None = None,
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
      other record only 'ledger'. K-119 M5: ALWAYS checked — the default
      is 'ledger' (a caller that names no actor never advances a release
      dct executes); any other value, None included, raises ValueError.
    - `tag_proof` / `push_proof` (K-115 G4, validate_tag_proof /
      validate_push_proof): the VERIFIED tag identity and push target —
      write-once, stored with the ledger's `recorded_at` (the state at
      verification time, not a guarantee about later); the stored value
      again is a confirmation, a different one raises ValueError; a tag
      proof is accepted from 'tagged' on, a push proof from 'pushed' on. A
      status is never proof: unless the record is 'without_git', no move
      into 'tagged' / 'pushed' / 'completed' succeeds without the tag
      proof, none into 'pushed' without the push proof, and none into
      'completed' without the push proof when the consent approved a push
      (check_proof_requirements) — for every executor.
    - K-115 G5 / G9: the POST-state must be one consistent release
      (check_record_consistency: record ↔ consent ↔ proposal ↔ render ↔
      proofs) — checked on every call, also a repeat.
    - K-115 G6: several live records of the same semantic version refuse
      the write (unique_record).
    - K-102: the record's entry set must equal EXACTLY the live entries
      carrying its version (check_entry_set, in this transaction) — also for
      a repeat of the current status; a record is never extended or shrunk
      to fit, and no progress is recorded on an inconsistent one.

    - K-119 M3 / the open point of K-106: a FIRST consent on a record whose
      executor is 'dct' would hand the release to dct's effects layer
      (progress_owner) — that is the controlled TAKEOVER, not a progress
      write: refused here for every actor (take_over_release, `dct release
      takeover`). A consent on an 'own' record, and the stored consent
      again, are accepted as before.

    Returns {"release": <record>, "updated": bool} — `updated` False when
    nothing changed (idempotent repeat).
    """
    return _record_progress(
        engine, project_slug, version, status, release_commit_sha=release_commit_sha,
        manifest=manifest, release_mode=release_mode, consent=consent, proposal=proposal,
        render=render, actor=actor, tag_proof=tag_proof, push_proof=push_proof)


def check_takeover(row, consent: Mapping | None) -> bool:
    """K-119 M3 / K-117 J4 — may THIS record be taken over by dct with
    `consent` (validated)? True when the record already carries exactly this
    consent (an idempotent repeat), False when the takeover is to be
    written; ValueError otherwise: a terminal record, an 'own' record (its
    tool finishes it through the ledger), a consent that does not name what
    was verified (`takeover`) or does not approve dct as the executor, or a
    record that already has a DIFFERENT consent (immutable)."""
    version = row.version
    if row.status in TERMINAL_STATUSES:
        raise ValueError(
            f"Release {version} is '{row.status}' — a terminal status; there is nothing to "
            f"take over; nothing written."
        )
    if (row.executor or DEFAULT_EXECUTOR) != "dct":
        raise ValueError(
            f"Release {version} is executed by the project's own release tool (executor "
            f"'{row.executor}', recorded at its stamp) — dct never takes it over; its tool "
            f"finishes it through the ledger path (advance_release); nothing written."
        )
    if consent is None or consent.get("takeover") is None:
        raise ValueError(
            "a takeover needs the owner's consent naming what was VERIFIED (consent.takeover: "
            "release_commit_sha, tree, artifacts_sha256); nothing written."
        )
    if consent.get("executor") != "dct":
        raise ValueError(
            "a takeover consent approves dct as the executor (consent.executor 'dct'); "
            "nothing written."
        )
    stored = _load_json(row.consent)
    if stored is None:
        return False
    if _canonical(stored) == _canonical(consent):
        return True
    raise ValueError(
        f"Release {version} already carries a consent (dct executes it) — it is immutable "
        f"and differs from the given one; `dct release resume` continues it; nothing written."
    )


def take_over_release(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    *,
    consent: Mapping,
    release_commit_sha: str,
    manifest: Mapping,
    candidate_sha: str | None = None,
    checkout_root: str | None = None,
    proposal: Mapping | None = None,
    confirm: bool = False,
) -> dict:
    """The controlled TAKEOVER of an existing release by dct (K-119 M3,
    K-117 J4) — ledger part only: the missing consent / proposal of a
    record that has none (a reconstruction that ended at 'committed', a
    record stamped through MCP release_changelog, an older record) are
    supplied FROM VERIFIED ARTIFACTS, so that `dct release resume` may
    perform the effects that are still missing. No new bump, no re-stamp
    (no entry and no current_version is touched) and no recorded value is
    changed: every field is write-once — filled when absent, confirmed when
    equal, refused when different. The local workflow gathers and verifies
    the evidence (effects.takeover); this function cross-checks it against
    the record and the database.

    Requires `confirm=True`. In ONE transaction (project write lock), every
    check before the single UPDATE:
    - the record: live, unique (K-115 G6), not terminal, executor 'dct'
      (an 'own' record is never taken over), no consent yet — the SAME
      consent again is a no-op (`updated` False), a different one refused
      (check_takeover);
    - `consent` (validate_consent): executor 'dct' and `takeover`
      {release_commit_sha, tree, artifacts_sha256} — what was verified;
      with the record's version, EXACT entry set and candidate, its
      push_target (or null = an explicit local release), and
      proposal_sha256 = proposal_digest of `proposal` (the record's
      proposal when it already has one);
    - `release_commit_sha` (required) and `manifest` (required, the
      verified artifacts — merged write-once per key) as in
      advance_release; `candidate_sha` / `checkout_root` FILL a record that
      has none (stamped without them) and must equal recorded ones;
      release_mode is recorded as 'git' (a record without git is never
      taken over);
    - the status moves to 'committed' when the record is below it (where
      the verified evidence starts); a record at 'committed' / 'tagged' /
      'pushed' keeps its status and its proofs;
    - the full entry set (check_entry_set), the mutual consistency of the
      post-state (check_record_consistency — also consent.takeover against
      the release commit and the manifest) and the proofs its status needs.

    Returns {"release": <record>, "updated": bool}. ValueError / TypeError
    otherwise; nothing written."""
    if confirm is not True:
        raise ValueError(
            "take_over_release needs explicit confirmation (confirm=True): dct becomes the "
            "executor of the release; nothing written."
        )
    if release_commit_sha is None or manifest is None:
        raise ValueError(
            "a takeover needs the verified release commit and the manifest of its artifacts; "
            "nothing written."
        )
    return _record_progress(
        engine, project_slug, version, None, release_commit_sha=release_commit_sha,
        manifest=manifest, release_mode="git", consent=consent, proposal=proposal,
        render=None, actor="dct", tag_proof=None, push_proof=None,
        takeover={"candidate_sha": validate_sha(candidate_sha, "candidate_sha"),
                  "checkout_root": checkout_root})


def _record_progress(
    engine: sa.Engine,
    project_slug: str,
    version: str,
    status: str | None,
    *,
    release_commit_sha: str | None,
    manifest: Mapping | None,
    release_mode: str | None,
    consent: Mapping | None,
    proposal: Mapping | None,
    render: Mapping | None,
    actor: str,
    tag_proof: Mapping | None,
    push_proof: Mapping | None,
    takeover: Mapping | None = None,
) -> dict:
    """The ONE write of advance_release and take_over_release (`takeover`:
    {candidate_sha, checkout_root} — then `status` is None and derived from
    the record). See both docstrings."""
    from dct.core.changelog import normalize_worktree

    if takeover is None:
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
    proofs = {
        "tag_proof": validate_tag_proof(tag_proof),
        "push_proof": validate_push_proof(push_proof),
    }
    validate_actor(actor)
    if takeover is not None:
        root = normalize_worktree(takeover["checkout_root"])
        ensure_fits(releases, {"checkout_root": root})
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version)
        if takeover is None:
            check_progress_actor(row, actor)
            if (once["consent"] is not None and row.consent is None
                    and (row.executor or DEFAULT_EXECUTOR) == "dct"):
                raise ValueError(
                    f"Release {row.version}: a first consent would make dct the executor "
                    f"of a release it did not stamp — that is the controlled takeover "
                    f"(take_over_release; `dct release takeover --version {row.version}`), "
                    f"never a progress write; nothing written."
                )
        else:
            if check_takeover(row, once["consent"]):
                return {"release": record_to_dict(row), "updated": False}
            # Where the verified evidence starts; a record further along
            # keeps its status (and its proofs).
            status = row.status if _rank(row.status) >= _COMMITTED else "committed"
        # K-102: the FULL entry set, in THIS transaction — also for a retry
        # of the current status: no progress on a record whose version other
        # entries carry (or lost), and the record is never extended to fit.
        check_entry_set(conn, pid, row)
        moves = check_transition(row.status, status)
        final = status if moves else row.status
        values: dict = {}
        if moves:
            values["status"] = status
        candidate = row.candidate_sha
        if takeover is not None:
            # Fill-once: a record stamped without its candidate / checkout
            # gets the verified ones; recorded ones are never replaced.
            for field, given in (("candidate_sha", takeover["candidate_sha"]),
                                 ("checkout_root", root)):
                stored = getattr(row, field)
                if given is None or stored == given:
                    continue
                if stored is not None:
                    raise ValueError(
                        f"{field} of release {row.version} is recorded as {stored} — the "
                        f"verified {given} contradicts it; a takeover changes nothing that "
                        f"is recorded; nothing written."
                    )
                values[field] = given
            candidate = values.get("candidate_sha", candidate)

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
        check_mode_consistency(mode, candidate, commit, final)
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

        # K-115 G4: the tag / push proofs — write-once, stamped by the ledger.
        now = datetime.now(UTC)
        new_proofs = {}
        for field, given in proofs.items():
            if given is None:
                continue
            stored = _load_json(getattr(row, field))
            if stored is None:
                new_proofs[field] = given
            elif _canonical(_unstamped(stored)) != _canonical(given):
                raise ValueError(
                    f"{field} is already recorded with a different value — it is immutable "
                    f"(it describes what was verified then); nothing written."
                )
        _check_proof_timing(new_proofs.get("tag_proof"), new_proofs.get("push_proof"), final)
        for field, given in new_proofs.items():
            values[field] = _canonical({**given, PROOF_STAMP: now.isoformat()})

        # K-115 G5 / G9: the POST-state must be one consistent release — also
        # for a repeat (a contradiction seeded outside the API blocks progress).
        state = {
            "version": row.version, "entry_ids": json.loads(row.entry_ids),
            "candidate_sha": candidate, "release_commit_sha": commit,
            "release_mode": mode, "executor": row.executor, "manifest": merged,
            **{field: json.loads(values[field]) if field in values
               else _load_json(getattr(row, field))
               for field in ("consent", "proposal", "render", "tag_proof", "push_proof")},
        }
        check_record_consistency(state)
        if moves:
            check_proof_requirements(state, final)

        if not values:
            return {"release": record_to_dict(row), "updated": False}
        if row.status in TERMINAL_STATUSES:
            raise ValueError(
                f"Release {row.version} is '{row.status}' — a terminal status; "
                f"nothing can be recorded on it any more; nothing written."
            )
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
    actor: str = "ledger",
) -> dict:
    """End an unfinished release as 'abandoned' (K-60) — ledger only.

    `actor` (ACTORS, K-119 M5/M6 — always checked, default 'ledger'; any
    other value, None included, raises ValueError): a release dct's effects
    layer executes (progress_owner 'dct') is abandoned ONLY by 'dct' — the
    local `dct release abandon` (effects.abandon), which holds the
    repository's operation lock; 'ledger' (MCP abandon_release, a direct
    core call — no lock, a running apply / resume could go on) is refused
    with the local command (check_abandon_actor). A record the ledger path
    owns is abandoned by either actor.

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
    validate_actor(actor)
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        row = _require_record(conn, pid, project_slug, version)
        check_abandon_actor(row, actor)
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
    tag_proof: Mapping | None = None,
    push_proof: Mapping | None = None,
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
    - K-115 G4 — the same proof rules as advance_release: with git, a
      status of 'tagged' or later needs `tag_proof` (validate_tag_proof —
      the tag the workflow VERIFIED, targeting release_commit_sha), 'pushed'
      needs `push_proof` (validate_push_proof); both are refused below
      their status and with without_git=True. Nothing is invented for old
      history: a release whose tag was not verified is reconstructed at
      'committed'. A reconstructed record carries no consent, so
      'completed' needs no push proof (its push scope is not recorded).
    - K-115 G6: several live records of the version → refused (the ledger
      is ambiguous), as is any existing record.
    - K-110 — `manifest["verification"]` is the recorded SCOPE of the
      verification (a JSON object the workflow writes; TypeError for any
      other type). `without_git=True` REQUIRES it, with `contract` a list:
      the project's CONFIRMED version-file contract ([] = confirmed "no
      version files"). A null / absent contract is UNKNOWN and refused —
      without a commit or a tag, `version_files: []` alone never proves
      that a release exists.

    The record gets mode 'reconstructed' (its provenance — the original
    stamp mode is unknown), release_mode 'git' / 'without_git' (from
    without_git), the project's current release executor (K-85; G8: an
    explicitly ASSIGNED responsibility for the record's further handling —
    who may record progress on it or abandon it — NOT provenance: it does
    not say who made the historical release), no consent, proposal or
    render (unknown for a legacy release) and completed_at = now when
    status is 'completed'. Returns the record.
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
    tag = validate_tag_proof(tag_proof)
    push = validate_push_proof(push_proof)
    root = normalize_worktree(checkout_root)
    ensure_fits(releases, {"checkout_root": root})
    rank = _rank(status)
    if without_git:
        if tag is not None or push is not None:
            raise ValueError(
                "a release without git has no tag and no push — tag_proof / push_proof "
                "must be absent; nothing written."
            )
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
    # K-110: the SCOPE of the verification is part of the evidence.
    scope = (recorded_manifest or {}).get("verification")
    if scope is not None and not isinstance(scope, Mapping):
        raise TypeError("manifest.verification must be a JSON object (the scope of the "
                        "verification)")
    if without_git:
        if scope is None:
            raise ValueError(
                "a reconstruction without git records the scope of its verification "
                "(manifest.verification) — without a commit or a tag, `version_files: []` "
                "alone is not evidence that a release exists; nothing written."
            )
        if not isinstance(scope.get("contract"), list):
            raise ValueError(
                "manifest.verification.contract is null or absent — the version-file "
                "contract is unknown; an unknown contract is not a confirmed \"no version "
                "files\" ([]): confirm the contract first; nothing written."
            )

    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        if unique_record(conn, pid, project_slug, version) is not None:
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
            tag_proof=tag, push_proof=push,
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
    (changelog.release_entries consume_proposal). The SAME transaction
    clears the project's preview slot (`dct release prepare --preview`,
    set_preview): a prepare and a preview replace each other. Returns
    {"proposal", "proposal_sha256"} plus "replaced_preview" (the cleared
    slot, for the caller to release its resources) when there was one."""
    canonical = validate_proposal(proposal)
    if canonical is None:
        raise TypeError("proposal must be a JSON object")
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        replaced = read_preview(conn, pid)
        conn.execute(projects.update().where(projects.c.id == pid).values(
            release_proposal=_canonical(canonical), release_preview=None,
            updated_at=datetime.now(UTC)))
    out = {"proposal": canonical, "proposal_sha256": proposal_digest(canonical)}
    if replaced is not None:
        out["replaced_preview"] = replaced
    return out


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


def live_proposals(engine: sa.Engine) -> list[dict]:
    """Every proposal that is still NEEDED, across ALL projects (K-117 —
    dct's release blob store is shared by them): each live project's
    PENDING proposal, the proposal content of every variant of its preview
    slot (set_preview — no id, no candidate) and the proposal of every live
    release record that is not terminal (completed / abandoned). →
    [{"project" (slug), "version" (None for a pending proposal or a
    preview), "proposal"}]. Read-only. What a cleanup of a finished
    proposal's resources must keep: anything one of these still names."""
    out: list[dict] = []
    with engine.begin() as conn:
        for slug, pending, preview in conn.execute(
                sa.select(projects.c.slug, projects.c.release_proposal,
                          projects.c.release_preview).where(
                    projects.c.deleted_at.is_(None),
                    sa.or_(projects.c.release_proposal.isnot(None),
                           projects.c.release_preview.isnot(None)))):
            if pending is not None:
                out.append({"project": slug, "version": None, "proposal": json.loads(pending)})
            if preview is not None:
                out.extend({"project": slug, "version": None,
                            "proposal": variant["proposal_content"]}
                           for variant in json.loads(preview)["variants"].values())
        for slug, version, text in conn.execute(
                sa.select(projects.c.slug, releases.c.version, releases.c.proposal)
                .select_from(releases.join(projects, releases.c.project_id == projects.c.id))
                .where(releases.c.deleted_at.is_(None), releases.c.proposal.isnot(None),
                       releases.c.status.notin_(sorted(TERMINAL_STATUSES)))):
            out.append({"project": slug, "version": version, "proposal": json.loads(text)})
    return out


def clear_pending_proposal(conn: sa.Connection, pid: int) -> None:
    """Drop the pending proposal on the caller's connection (the stamp that
    consumed it)."""
    conn.execute(projects.update().where(projects.c.id == pid).values(release_proposal=None))



# ── the project's PREVIEW slot (`dct release prepare --preview`) ─────────────
#
# The one-dialog release: `prepare --preview` computes, for each variant it
# offers ("shown", and "all" — the shown one plus every waiting entry
# attested), the CONSENT VIEW — exactly the release content the owner
# approves on one screen — and stores the variants here
# (projects.release_preview), keyed by the view's digest C
# (consent_view_digest). Being stored is not an approval: `apply --consent
# C` re-derives the view and stamps only when it is still C. A preview and
# a prepare replace each other's slot (set_preview clears the pending
# proposal; set_pending_proposal clears the slot).

PREVIEW_MAX_BYTES = 262144
PREVIEW_KEYS = frozenset({"id", "created_at", "base", "work", "variants", "pin", "progress"})
PREVIEW_BASE_KEYS = frozenset({"head", "ref", "checkout_root"})
PREVIEW_WORK_KEYS = frozenset({"index_before", "paths_state", "untracked_included", "message"})
PREVIEW_VARIANT_KEYS = frozenset({"name", "consent", "proposal_content"})
PREVIEW_VARIANTS = ("shown", "all")
PREVIEW_PROGRESS_KEYS = frozenset({"work_commit", "attest_commit", "consent"})
# validate_proposal's shape minus what binds a proposal to one candidate
# (the preview's candidate is synthetic) and its identity.
PROPOSAL_CONTENT_KEYS = PROPOSAL_KEYS - {"id", "candidate_sha", "plan_digest"}
CONSENT_VIEW_FORMAT = 1
CONSENT_VIEW_KEYS = frozenset({
    "format", "project", "executor", "release_mode", "checkout_root", "project_root",
    "version", "base", "work", "attest", "candidate_tree", "range", "entries", "changelog",
    "version_files", "push",
})
VIEW_WORK_KEYS = frozenset({"tree", "subject", "message_sha256", "proves", "paths"})
VIEW_RANGE_KEYS = frozenset({"base_tag", "base_sha", "approximate", "since", "notice_code"})
VIEW_ENTRIES_KEYS = frozenset({"selected", "state", "waiting", "proof"})
VIEW_CHANGELOG_KEYS = frozenset({"path", "managed", "base_blob", "composed_sha256",
                                 "section_sha256", "date", "pr_refs"})
VIEW_VERSION_FILES_KEYS = frozenset({"paths", "base_blobs", "expected_blobs", "script",
                                     "contract"})
VIEW_PUSH_KEYS = frozenset({"mode", "remote", "branch", "url", "tag"})
LOCAL_PUSH_REASONS = ("requested", "no_upstream", "upstream_invalid", "detached", "several_urls",
                      "url_not_literal")
TREE_MODES = ("100644", "100755", "120000")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_ID_KEY_RE = re.compile(r"[1-9][0-9]*", re.ASCII)


def _exact_sha(value, field: str, *, optional: bool = False) -> str | None:
    """A lowercase hex object id (7-64), checked — never rewritten (the
    view's validator returns what it was given)."""
    if value is None and optional:
        return None
    if not isinstance(value, str) or validate_sha(value, field) != value:
        raise ValueError(f"{field} must be a lowercase hex object id, got {value!r}")
    return value


def _required_digest(value, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} must be a lowercase hex SHA-256 (64 chars), got None")
    return _digest(value, field)


def _string(value, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {value!r}")
    return value


def _bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be true or false, got {value!r}")
    return value


def _ids(value, field: str) -> list[int]:
    if not isinstance(value, list) or any(isinstance(i, bool) or not isinstance(i, int)
                                          or i < 1 for i in value) \
            or value != sorted(set(value)):
        raise ValueError(f"{field} must be an ascending list of distinct entry ids")
    return value


def _id_map(value, field: str) -> Mapping:
    if not isinstance(value, Mapping) or any(
            not isinstance(k, str) or not _ID_KEY_RE.fullmatch(k) for k in value):
        raise ValueError(f"{field} must be an object keyed by entry ids")
    return value


def _copy(value):
    return json.loads(_canonical(value))


def validate_consent_view(view: Mapping) -> dict:
    """The CONSENT VIEW of one preview variant — the exact release content
    the owner approves (docs/release-workflow.md, "The consent view") —
    CHECKED against its closed key sets and returned unchanged (a copy):
    {format (1), project, executor ('dct'), release_mode, checkout_root,
    project_root, version {current, target}, base {head, ref}, work (null |
    {tree, subject, message_sha256, proves, paths {path: null | {mode,
    blob}}}), attest (null | {ids}), candidate_tree, range (null | {base_tag,
    base_sha, approximate, since, notice_code}), entries {selected, state
    {id: digest}, waiting {id: reason}, proof {id: object}}, changelog
    {managed, path, base_blob, composed_sha256, section_sha256, date,
    pr_refs} (ALWAYS an object: the section, its UTC date and the render
    are bound also when dct writes no changelog file — managed false, then
    path, base_blob and composed_sha256 are null), version_files {paths, base_blobs, expected_blobs, script,
    contract {confirmed, change}}, push (null | {mode 'push', remote,
    branch, url, tag} | {mode 'local', reason})}. Every key is required;
    an unknown key, a bad digest or object id, a wrong type → ValueError
    (TypeError for a non-object)."""
    _object(view, "consent view", set(CONSENT_VIEW_KEYS), set(CONSENT_VIEW_KEYS))
    if view["format"] != CONSENT_VIEW_FORMAT or isinstance(view["format"], bool):
        raise ValueError(f"consent view format must be {CONSENT_VIEW_FORMAT}, got "
                         f"{view['format']!r}")
    _text(view["project"], "consent view.project", 200)
    if view["executor"] != DEFAULT_EXECUTOR:
        raise ValueError(f"consent view.executor must be 'dct', got {view['executor']!r}")
    if view["release_mode"] not in GIT_MODES:
        raise ValueError(f"consent view.release_mode must be one of {GIT_MODES}")
    _string(view["checkout_root"], "consent view.checkout_root", optional=True)
    _string(view["project_root"], "consent view.project_root")
    version = _object(view["version"], "consent view.version", {"current", "target"},
                      {"current", "target"})
    _string(version["current"], "consent view.version.current", optional=True)
    _text(version["target"], "consent view.version.target", 50)
    base = _object(view["base"], "consent view.base", {"head", "ref"}, {"head", "ref"})
    _exact_sha(base["head"], "consent view.base.head", optional=True)
    _string(base["ref"], "consent view.base.ref", optional=True)
    work = view["work"]
    if work is not None:
        _object(work, "consent view.work", set(VIEW_WORK_KEYS), set(VIEW_WORK_KEYS))
        _exact_sha(work["tree"], "consent view.work.tree")
        _string(work["subject"], "consent view.work.subject")
        _required_digest(work["message_sha256"], "consent view.work.message_sha256")
        _ids(work["proves"], "consent view.work.proves")
        if not isinstance(work["paths"], Mapping) or not work["paths"]:
            raise ValueError("consent view.work.paths must be a non-empty object")
        for path, entry in work["paths"].items():
            if entry is None:
                continue
            _object(entry, f"consent view.work.paths[{path!r}]", {"mode", "blob"},
                    {"mode", "blob"})
            if entry["mode"] not in TREE_MODES:
                raise ValueError(f"consent view.work.paths[{path!r}].mode must be one of "
                                 f"{TREE_MODES}")
            _exact_sha(entry["blob"], f"consent view.work.paths[{path!r}].blob")
    attest = view["attest"]
    if attest is not None:
        _object(attest, "consent view.attest", {"ids"}, {"ids"})
        if not _ids(attest["ids"], "consent view.attest.ids"):
            raise ValueError("consent view.attest.ids must not be empty")
    _exact_sha(view["candidate_tree"], "consent view.candidate_tree", optional=True)
    rng = view["range"]
    if rng is not None:
        _object(rng, "consent view.range", set(VIEW_RANGE_KEYS), set(VIEW_RANGE_KEYS))
        _string(rng["base_tag"], "consent view.range.base_tag", optional=True)
        _exact_sha(rng["base_sha"], "consent view.range.base_sha", optional=True)
        _bool(rng["approximate"], "consent view.range.approximate")
        _string(rng["since"], "consent view.range.since", optional=True)
        _string(rng["notice_code"], "consent view.range.notice_code", optional=True)
    entries = _object(view["entries"], "consent view.entries", set(VIEW_ENTRIES_KEYS),
                      set(VIEW_ENTRIES_KEYS))
    _ids(entries["selected"], "consent view.entries.selected")
    for key, digest in _id_map(entries["state"], "consent view.entries.state").items():
        _required_digest(digest, f"consent view.entries.state[{key}]")
    for key, reason in _id_map(entries["waiting"], "consent view.entries.waiting").items():
        _text(reason, f"consent view.entries.waiting[{key}]", 50)
    for key, proof in _id_map(entries["proof"], "consent view.entries.proof").items():
        _object(proof, f"consent view.entries.proof[{key}]", set(_PROOF_KEYS))
    # The release's section, its date and its PR-reference render are bound
    # whether or not dct writes a changelog file (`managed`): the screen
    # shows the dated section either way, and the record and the export
    # carry them.
    changelog = _object(view["changelog"], "consent view.changelog", set(VIEW_CHANGELOG_KEYS),
                        set(VIEW_CHANGELOG_KEYS))
    if _bool(changelog["managed"], "consent view.changelog.managed"):
        _string(changelog["path"], "consent view.changelog.path")
        _exact_sha(changelog["base_blob"], "consent view.changelog.base_blob", optional=True)
        _required_digest(changelog["composed_sha256"], "consent view.changelog.composed_sha256")
    elif any(changelog[key] is not None for key in ("path", "base_blob", "composed_sha256")):
        raise ValueError("consent view.changelog: path, base_blob and composed_sha256 must be "
                         "null when dct writes no changelog file (managed false)")
    _required_digest(changelog["section_sha256"], "consent view.changelog.section_sha256")
    if not isinstance(changelog["date"], str) or not _DATE_RE.fullmatch(changelog["date"]):
        raise ValueError("consent view.changelog.date must be YYYY-MM-DD")
    _bool(changelog["pr_refs"], "consent view.changelog.pr_refs")
    files = _object(view["version_files"], "consent view.version_files",
                    set(VIEW_VERSION_FILES_KEYS), set(VIEW_VERSION_FILES_KEYS))
    paths = files["paths"]
    if not isinstance(paths, list) or normalize_version_files(paths) != paths:
        raise ValueError("consent view.version_files.paths must be normalized version-file "
                         "paths")
    for key, nullable in (("base_blobs", True), ("expected_blobs", False)):
        blobs = files[key]
        if not isinstance(blobs, Mapping) or set(blobs) != set(paths):
            raise ValueError(f"consent view.version_files.{key} must name exactly its paths")
        for path, blob in blobs.items():
            _exact_sha(blob, f"consent view.version_files.{key}[{path!r}]", optional=nullable)
    script = files["script"]
    if script is not None:
        _object(script, "consent view.version_files.script", {"identity", "sha256"},
                {"identity", "sha256"})
        _string(script["identity"], "consent view.version_files.script.identity")
        _digest(script["sha256"], "consent view.version_files.script.sha256")
    contract = _object(files["contract"], "consent view.version_files.contract",
                       {"confirmed", "change"}, {"confirmed", "change"})
    if contract["confirmed"] is not None and not isinstance(contract["confirmed"], list):
        raise ValueError("consent view.version_files.contract.confirmed must be a list or null")
    _bool(contract["change"], "consent view.version_files.contract.change")
    push = view["push"]
    if push is not None:
        if not isinstance(push, Mapping):
            raise TypeError("consent view.push must be a JSON object")
        if push.get("mode") == "push":
            _object(push, "consent view.push", set(VIEW_PUSH_KEYS), set(VIEW_PUSH_KEYS))
            for key in ("remote", "branch", "url", "tag"):
                _text(push[key], f"consent view.push.{key}", 2000)
        elif push.get("mode") == "local":
            _object(push, "consent view.push", {"mode", "reason"}, {"mode", "reason"})
            if push["reason"] not in LOCAL_PUSH_REASONS:
                raise ValueError(f"consent view.push.reason must be one of {LOCAL_PUSH_REASONS}")
        else:
            raise ValueError("consent view.push.mode must be 'push' or 'local'")
    return _copy(view)


def consent_view_digest(view: Mapping) -> str:
    """C — SHA-256 of the canonical JSON of a valid consent view (sorted
    keys, compact separators, UTF-8): what the preview returns per variant
    and `apply --consent` names."""
    import hashlib

    return hashlib.sha256(_canonical(validate_consent_view(view)).encode("utf-8")).hexdigest()


def proposal_content(proposal: Mapping) -> dict:
    """A proposal's CONTENT (validate_proposal minus id, candidate_sha and
    plan_digest) — what a preview variant stores: the version files, their
    blobs, the script, the version, the mode and the changelog baseline."""
    canonical = validate_proposal(proposal)
    if canonical is None:
        raise TypeError("proposal content must be a JSON object")
    return {key: canonical[key] for key in sorted(PROPOSAL_CONTENT_KEYS)}


def validate_preview(preview: Mapping) -> dict:
    """The PREVIEW slot (projects.release_preview) CHECKED against its
    closed key sets and returned unchanged (a copy):

    {"id": the preview's ULID, "created_at": ISO time, "base": {"head",
    "ref", "checkout_root"} (null outside git), "work": null | {"index_before"
    {path: null | {mode, blob, stage}} (the user's index entries before),
    "paths_state" {path: sha256 of the work-tree bytes | null (deleted)},
    "untracked_included": [path], "message": the cleaned message},
    "variants": {C: {"name" ('shown' | 'all'), "consent" (validate_consent_view,
    C = consent_view_digest of it), "proposal_content" (proposal_content)}},
    "pin": the ref pinning its blobs | null, "progress": {"work_commit",
    "attest_commit" (null until apply made them), "consent" (the C of the
    variant whose commits they are — one of `variants`, written with the
    first commit; null before)}}.
    Every key is required; ≤ PREVIEW_MAX_BYTES of canonical JSON (refused
    with "too long", never truncated). ValueError / TypeError otherwise."""
    _object(preview, "preview", set(PREVIEW_KEYS), set(PREVIEW_KEYS))
    if not isinstance(preview["id"], str) or not PROPOSAL_ID_RE.fullmatch(preview["id"]):
        raise ValueError(f"preview.id must be 1-64 letters / digits, got {preview['id']!r}")
    _text(preview["created_at"], "preview.created_at", 64)
    base = _object(preview["base"], "preview.base", set(PREVIEW_BASE_KEYS),
                   set(PREVIEW_BASE_KEYS))
    _exact_sha(base["head"], "preview.base.head", optional=True)
    _string(base["ref"], "preview.base.ref", optional=True)
    _string(base["checkout_root"], "preview.base.checkout_root", optional=True)
    work = preview["work"]
    if work is not None:
        _object(work, "preview.work", set(PREVIEW_WORK_KEYS), set(PREVIEW_WORK_KEYS))
        if not isinstance(work["index_before"], Mapping):
            raise TypeError("preview.work.index_before must be a JSON object")
        for path, entry in work["index_before"].items():
            if entry is None:
                continue
            _object(entry, f"preview.work.index_before[{path!r}]", {"mode", "blob", "stage"},
                    {"mode", "blob", "stage"})
            _string(entry["mode"], f"preview.work.index_before[{path!r}].mode")
            _exact_sha(entry["blob"], f"preview.work.index_before[{path!r}].blob")
            _string(entry["stage"], f"preview.work.index_before[{path!r}].stage")
        if not isinstance(work["paths_state"], Mapping):
            raise TypeError("preview.work.paths_state must be a JSON object")
        for path, digest in work["paths_state"].items():
            _digest(digest, f"preview.work.paths_state[{path!r}]")
        included = work["untracked_included"]
        if not isinstance(included, list) or any(not isinstance(p, str) for p in included):
            raise ValueError("preview.work.untracked_included must be a list of paths")
        _string(work["message"], "preview.work.message")
    variants = preview["variants"]
    if not isinstance(variants, Mapping) or not variants:
        raise ValueError("preview.variants must be a non-empty object keyed by consent digest")
    names = []
    for digest, item in variants.items():
        _required_digest(digest, "preview.variants key")
        _object(item, "preview.variants[…]", set(PREVIEW_VARIANT_KEYS),
                set(PREVIEW_VARIANT_KEYS))
        if item["name"] not in PREVIEW_VARIANTS:
            raise ValueError(f"preview variant name must be one of {PREVIEW_VARIANTS}")
        names.append(item["name"])
        if consent_view_digest(item["consent"]) != digest:
            raise ValueError(f"preview variant {digest} is not the digest of its consent view")
        content = item["proposal_content"]
        _object(content, "preview.variants[…].proposal_content", set(PROPOSAL_CONTENT_KEYS),
                set(PROPOSAL_CONTENT_KEYS))
        if _canonical(proposal_content(content)) != _canonical(content):
            raise ValueError("preview.variants[…].proposal_content is not in canonical form")
    if len(set(names)) != len(names):
        raise ValueError("preview.variants: each variant name at most once")
    _string(preview["pin"], "preview.pin", optional=True)
    progress = _object(preview["progress"], "preview.progress", set(PREVIEW_PROGRESS_KEYS),
                       set(PREVIEW_PROGRESS_KEYS))
    for key in ("work_commit", "attest_commit"):
        _exact_sha(progress[key], f"preview.progress.{key}", optional=True)
    if _digest(progress["consent"], "preview.progress.consent") is not None \
            and progress["consent"] not in variants:
        raise ValueError("preview.progress.consent must be the digest of one of its variants")
    ensure_json_fits(preview, "preview", PREVIEW_MAX_BYTES)
    return _copy(preview)


def read_preview(conn: sa.Connection, pid: int) -> dict | None:
    """The project's preview slot on the caller's connection, or None."""
    text = conn.execute(
        sa.select(projects.c.release_preview).where(projects.c.id == pid)).scalar()
    return None if text is None else json.loads(text)


def get_preview(engine: sa.Engine, project_slug: str) -> dict | None:
    """The project's preview slot (validate_preview shape) or None. Read-only."""
    with engine.begin() as conn:
        return read_preview(conn, resolve_project_id(conn, project_slug))


def clear_preview(conn: sa.Connection, pid: int) -> None:
    """Drop the preview slot on the caller's connection."""
    conn.execute(projects.update().where(projects.c.id == pid).values(release_preview=None))


def set_preview(engine: sa.Engine, project_slug: str, preview: Mapping) -> dict:
    """Store `preview` (validate_preview — ValueError / TypeError, nothing
    written) as the project's preview slot, in ONE transaction that also
    clears the pending proposal (a preview and a prepare replace each
    other). → {"preview", "replaced_preview", "replaced_proposal"} — the
    replaced slot / pending proposal (or None), whose resources the caller
    releases after this commit."""
    canonical = validate_preview(preview)
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        replaced = read_preview(conn, pid)
        pending = read_pending_proposal(conn, pid)
        conn.execute(projects.update().where(projects.c.id == pid).values(
            release_preview=_canonical(canonical), release_proposal=None,
            updated_at=datetime.now(UTC)))
    return {"preview": canonical, "replaced_preview": replaced,
            "replaced_proposal": None if pending is None else pending["proposal"]}


def discard_preview(engine: sa.Engine, project_slug: str) -> dict | None:
    """Clear the project's preview slot (`prepare --discard-preview`: a
    screen the owner declined can no longer be applied) → the discarded
    slot, or None when it was empty (nothing written)."""
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        found = read_preview(conn, pid)
        if found is not None:
            clear_preview(conn, pid)
    return found


def record_preview_progress(engine: sa.Engine, project_slug: str, preview_id: str, *,
                            work_commit: str | None = None,
                            attest_commit: str | None = None,
                            consent: str | None = None) -> dict:
    """Record what `apply --consent` made for the preview `preview_id`
    BEFORE it goes on (the work-in-progress commit, the attestation
    commit) and under which consent (`consent`: the C applied — one of the
    slot's variants; apply writes it with its first commit, so a progress
    two variants share still names the screen the owner approved), so a
    re-run adopts them. Each field is WRITE-ONCE (the same value again is a
    confirmation; another one → ValueError, nothing written); a slot that
    is empty or holds another preview → ValueError. Returns the updated
    slot."""
    given = {"work_commit": work_commit, "attest_commit": attest_commit, "consent": consent}
    with project_transaction(engine, project_slug, write=True) as (conn, pid):
        slot = read_preview(conn, pid)
        if slot is None or slot["id"] != preview_id:
            raise ValueError(
                f"the preview slot of '{project_slug}' holds "
                f"{'nothing' if slot is None else 'preview ' + slot['id']}, not {preview_id} — "
                f"it was replaced or discarded; nothing written.")
        progress = {"consent": None, **slot["progress"]}  # a slot of an older dct: no consent
        for field, value in given.items():
            if value is None:
                continue
            value = _digest(value, f"progress.{field}") if field == "consent" \
                else _full_sha(value, f"progress.{field}")
            if not check_unchanged(f"preview progress.{field}", progress[field], value):
                progress[field] = value
        if progress != slot["progress"]:
            slot = validate_preview({**slot, "progress": progress})
            conn.execute(projects.update().where(projects.c.id == pid).values(
                release_preview=_canonical(slot), updated_at=datetime.now(UTC)))
    return slot
