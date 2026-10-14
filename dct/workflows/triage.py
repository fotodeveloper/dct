"""Triage findings ledger (#909) — one implementation for MCP, CLI and skills.

A triage cycle (`triage_cycles`, one review round of a project) owns findings
(`findings`); a finding owns its fix options (`finding_options`). A finding
carries the rubric v2 fields (stored under full names, accepted and exported
under the template's short keys too), the class derived by rubric.classify()
plus an optional coordinator override with its reason, an agent's suggestion
(the template's SUGGESTED — a proposal, never a decision), the owner's
decision with the comment VERBATIM, an optional promotion to an item and the
recurrence link by normalised fingerprint (first_seen_id, project-wide).

Contracts:
- Core shape: `engine` first, dicts out, EVERY write one transaction — a
  batch with one invalid entry writes nothing.
- Open-cycle contract: record / decide / suggest require an open cycle and
  close_triage closes it; all four take db.lock_cycle_row, so a finding or a
  decision lands before the close reads the cycle or is refused after it —
  never written into a closed cycle.
- Explicit row locks (db.lock_row: FOR NO KEY UPDATE on PostgreSQL, the
  whole database on SQLite), per operation, in the order taken; every
  operation follows the one order project row → cycle row → finding row →
  item row, skipping the rows it does not lock:
    record_findings          project → cycle
    decide_findings          cycle → item of each promoted finding decided
                             reject / note (_refuse_open_promotion); also
                             decide_findings_from_text and
                             accept_suggestions (one shared step,
                             _decide_locked)
    suggest_findings         cycle
    close_triage             cycle
    update_triage            cycle
    promote_finding_to_item  cycle → finding (the cycle id is read first,
                             unlocked, to find which cycle to lock)
  Only record_findings locks the project row (first_seen and the seq
  allocation are project-wide). Implicit FK locks: an INSERT of a finding,
  an option or an item takes FOR KEY SHARE on the rows it references (the
  cycle, first_seen's finding, the project); FOR NO KEY UPDATE does not
  conflict with KEY SHARE (K-21), so a promotion inserting an item of a
  project whose row a recorder holds, or a recorder linking first_seen to a
  finding a promotion holds, waits for neither lock. Every check reads the
  state AFTER its locks, in the transaction that writes.
- Cycle addressing (K-13): every function taking a cycle accepts its id (an
  int, global) or its code (a str, always together with the project) —
  resolve_cycle is the one resolver; a numeric string is a code, never
  re-read as an id.
- Widths: every VARCHAR value goes through ensure_fits (refused, never
  truncated); value domains (rubric enums, decisions, classes) are checked
  here, not by CHECK constraints.
- Export: CARDS / CLOSED / SUGGESTED use the template's field names exactly;
  the page itself is built from the export only, by
  dct.workflows.triage_page.build_triage_page.
- Reviewer identity (#930 §2a): `src` stays the free-text display value;
  the canonical reviewer tokens of a finding live in finding_reviewers and
  are never guessed from `src`. reviewer_precision counts per token.
- Owner decisions arrive as a dict (decide_findings), as the page's pasted
  decision text (decide_findings_from_text — parse_decision_batch is the one
  parser of it: skipped "— (brak decyzji)" cards, K-31, and the prefill
  line stored in decision_text_origin, K-40) or as an acceptance of an
  agent's proposals (accept_suggestions, bound to the suggestion_digest of
  the export the owner saw, K-32); all three validate and write through one
  connection-level step under the cycle lock, and change ONLY the findings
  they address.
- JSON: every returned value is JSON-serialisable (datetimes as ISO strings);
  page_constants_json is the one serializer of those values into a page.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.items import TERMINAL_STATUSES, create_item_conn
from dct.core.limits import ensure_fits
from dct.core.projects import resolve_project_id
from dct.core.provenance import get_provenance
from dct.core.sprint import _json_safe, _parse_since_conn, _to_cols_rows
from dct.db import (
    finding_options,
    finding_reviewers,
    findings,
    items,
    lock_cycle_row,
    lock_item_row,
    lock_row,
    projects,
    triage_cycles,
)
from dct.workflows import rubric

# ── input vocabulary ─────────────────────────────────────────────────────────

# record_findings: accepted key → canonical field. Each field has the short
# key of the reviewer output / template CARD and its full (column) name; one
# finding may use either spelling of a field, never both.
_FINDING_SPELLINGS: dict[str, tuple[str, ...]] = {
    "seq": ("seq",),
    "title": ("title",),
    "fingerprint": ("fp", "fingerprint"),
    "impact": ("S", "impact"),
    "reachability": ("O", "reachability"),
    "evidence": ("E", "evidence"),
    "fix_cost": ("K", "fix_cost"),
    "origin": ("origin",),
    "finding_type": ("type", "finding_type"),
    "area": ("area",),
    "source": ("src", "source"),
    "context": ("ctx", "context"),
    "flags": ("flags",),
    "class_override": ("cls", "class_override"),
    "class_reason": ("clsWhy", "class_reason"),
    "options": ("opts", "options"),
    "reviewers": ("reviewers",),
}
_OPTION_SPELLINGS: dict[str, tuple[str, ...]] = {
    "key": ("k", "key"),
    "text": ("t", "text"),
    "why": ("why",),
    "is_root": ("root", "is_root"),
    "is_recommended": ("rec", "is_recommended"),
}
_FLAG_SPELLINGS: dict[str, tuple[str, ...]] = {
    "conflict": ("conflict",),
    "violation": ("violation",),
    "dispute": ("dispute",),
    "open_design": ("openDesign", "open_design"),
}
_DECISION_SPELLINGS: dict[str, tuple[str, ...]] = {
    "dec": ("dec",),
    "opt": ("opt",),
    "note": ("note",),
}

_SEQ_KEY = re.compile(r"[Kk]-([0-9]+)")
# Stripped from the end of a normalised fingerprint, together with spaces,
# until nothing more goes (K-9).
_TRAILING_STRIP = ".,;:!?… "

# record_findings also takes the exported CARD shape (K-13): `id` ('K-<n>')
# for seq and the §4 flags at the top level instead of inside `flags`.
_CARD_FLAG_KEYS = ("conflict", "violation", "dispute", "openDesign")

# Value ranges of the integer columns (K-8), checked before any SQL: SQLite
# stores any int, PostgreSQL refuses with a raw driver error.
INT_MAX = 2_147_483_647  # INTEGER: seq and every id
RUBRIC_VERSION_MAX = 32_767  # SMALLINT: triage_cycles.rubric_version

# The promotion outcomes (K-2 / K-12): only these decisions become items;
# undecided, discuss, reject and note never do.
PROMOTABLE_DECISIONS = ("fix", "verify", "backlog")
# Decisions that keep a finding in the ledger only: refused while the
# finding's promoted item is still open (K-2).
LEDGER_ONLY_DECISIONS = ("reject", "note")

# list_findings brief view (spec #909 §3).
FINDING_COLS = [
    "id", "cycle_code", "seq", "S", "O", "E", "K", "origin", "type", "class",
    "decision", "decision_opt", "item_id", "first_seen_id", "source", "title",
]
HISTORY_COLS = ["id", "cycle_code", "opened_at", "seq", "decision", "decision_opt", "item_id"]
PRECISION_COLS = ["reviewer", "decision", "n"]

# #930 §2a: a canonical reviewer token (lowercased on write, then matched).
REVIEWER_TOKEN = re.compile(r"[a-z0-9][a-z0-9:._-]*")
# reviewer_precision's bucket of decided findings that carry no token — so
# nothing is silently dropped from the count. Not a valid token (parentheses).
UNATTRIBUTED = "(unattributed)"


def _now() -> datetime:
    return datetime.now(UTC)


# Characters that end or break an inline <script> or an HTML parse when a
# JSON constant is embedded in a page (K-1), and their JSON escapes.
_SCRIPT_UNSAFE = str.maketrans({
    "<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
    "\u2028": "\\u2028", "\u2029": "\\u2029",
})


def page_constants_json(value, *, indent: int | None = 2) -> str:
    """`value` as JSON safe to embed in an inline HTML <script> (K-1) — the
    one serializer of the triage page's constants (CARDS / CLOSED /
    SUGGESTED) for the CLI and any page generator.

    Non-ASCII stays readable (ensure_ascii=False); `<`, `>`, `&`, U+2028 and
    U+2029 are written as \\uXXXX escapes, so no text (a finding quoting
    `</script>` or `<!--`) can close the script or open markup, and the
    result is valid JSON and JavaScript alike: json.loads gives back `value`
    exactly. Those characters occur in JSON only inside strings, so the
    escape never touches the structure. The data itself is never escaped:
    export_triage and the MCP tools keep the original texts."""
    return json.dumps(value, ensure_ascii=False, indent=indent).translate(_SCRIPT_UNSAFE)


def _normalize_once(text: str) -> str:
    norm = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).lower())
    return " ".join(norm.split()).rstrip(_TRAILING_STRIP)


def normalize_fingerprint(fingerprint: str) -> str:
    """The recurrence identity of a fingerprint (K-9): Unicode NFC, lower()
    (NOT casefold — 'ß' and 'ss' stay different fingerprints), whitespace
    collapsed to single spaces, trailing punctuation and spaces stripped
    until none is left ('… by Z. .' → '… by z'). Idempotent: applied to its
    own result it returns it unchanged. Wording beyond that is NOT
    normalised — a rephrasing is a different fingerprint. Never truncated:
    record_findings refuses a fingerprint whose normal form outgrows the
    column (lower() can lengthen a text, e.g. 'İ')."""
    norm = _normalize_once(fingerprint)
    while (again := _normalize_once(norm)) != norm:
        norm = again
    return norm


def _canonical(raw: Mapping, spellings: dict[str, tuple[str, ...]], where: str) -> dict:
    """Map `raw` onto canonical field names. Unknown keys are refused naming
    them (#823 rule); two spellings of one field are refused."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected an object, got {type(raw).__name__}")  # noqa: TRY004 — every refusal is a ValueError
    known = {alias: field for field, aliases in spellings.items() for alias in aliases}
    unknown = [str(k) for k in raw if k not in known]
    if unknown:
        raise ValueError(
            f"{where}: unknown keys: {', '.join(sorted(unknown))} "
            f"(accepted: {', '.join(a for aliases in spellings.values() for a in aliases)})"
        )
    out: dict = {}
    for field, aliases in spellings.items():
        present = [a for a in aliases if a in raw]
        if len(present) > 1:
            raise ValueError(
                f"{where}: {' and '.join(present)} name the same field — pass one of them"
            )
        if present:
            out[field] = raw[present[0]]
    return out


def _required_text(value, label: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: {label} is required (a non-empty string)")
    return value


def _optional_text(value, label: str, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}: {label} must be a string")  # noqa: TRY004 — every refusal is a ValueError
    return value


def _level(value, label: str, where: str) -> int:
    """An S / E level 0-3: an int (never a bool) or its one-digit string."""
    if isinstance(value, str) and value in ("0", "1", "2", "3"):
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value in rubric.IMPACT_LEVELS:
        return value
    raise ValueError(f"{where}: {label} must be 0-3, got {value!r}")


def _reachability(value, where: str) -> str:
    """O: 0-3 (int or digit string) or '?' — stored as its one-char string."""
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1, 2, 3):
        return str(value)
    if isinstance(value, str) and value in rubric.REACHABILITY_LEVELS:
        return value
    raise ValueError(f"{where}: O (reachability) must be 0-3 or '?', got {value!r}")


def _enum(value, valid, label: str, where: str) -> str:
    if value not in valid:
        raise ValueError(f"{where}: {label} must be one of {', '.join(valid)}, got {value!r}")
    return value


def _flag(value, label: str, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {label} must be true or false, got {value!r}")  # noqa: TRY004 — every refusal is a ValueError
    return value


def _context(value, where: str) -> str | None:
    """ctx: a string or a list of paragraph strings ("\\n\\n"-joined)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, list) and all(isinstance(p, str) for p in value):
        return "\n\n".join(value) or None
    raise ValueError(f"{where}: ctx must be a string or a list of strings")


def _int_in_range(value, label: str, high: int, where: str = "") -> int:
    """An int (never a bool) in 1..high — the column's range (K-8), checked
    before any SQL; the refusal names the field."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= high:
        return value
    prefix = f"{where}: " if where else ""
    raise ValueError(f"{prefix}{label} must be an integer from 1 to {high}, got {value!r}")


def _seq_value(value, where: str) -> int:
    return _int_in_range(value, "seq", INT_MAX, where)


def _card_shape(raw, where: str) -> dict:
    """Accept the exported CARD shape on input (K-13): `id` 'K-<n>' becomes
    seq; the §4 flags at the top level join `flags`. Each field still has one
    spelling per finding — id with seq, or a flag both at the top level and
    inside flags, is refused. Everything else passes through to _canonical
    (unknown keys are refused there)."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected an object, got {type(raw).__name__}")  # noqa: TRY004 — every refusal is a ValueError
    out = dict(raw)
    if "id" in out:
        if "seq" in out:
            raise ValueError(f"{where}: id and seq name the same field — pass one of them")
        card_id = out.pop("id")
        m = _SEQ_KEY.fullmatch(card_id) if isinstance(card_id, str) else None
        if m is None:
            raise ValueError(f"{where}: id must be 'K-<n>' (the CARD id), got {card_id!r}")
        out["seq"] = int(m.group(1))
    top = {k: out.pop(k) for k in _CARD_FLAG_KEYS if k in out}
    if top:
        nested = {} if out.get("flags") is None else _canonical(
            out["flags"], _FLAG_SPELLINGS, f"{where}.flags")
        both = sorted(set(nested) & set(_canonical(top, _FLAG_SPELLINGS, where)))
        if both:
            raise ValueError(
                f"{where}: {', '.join(both)} given both at the top level and in flags "
                "— pass one of them")
        out["flags"] = {**top, **nested}
    return out


def _parse_options(value, where: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where}: opts must be a list")  # noqa: TRY004 — every refusal is a ValueError
    options: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(value):
        at = f"{where}.opts[{i}]"
        opt = _canonical(raw, _OPTION_SPELLINGS, at)
        key = _required_text(opt.get("key"), "k (option key)", at)
        if key in seen:
            raise ValueError(f"{where}: duplicate option key {key!r}")
        seen.add(key)
        row = {
            "key": key,
            "text": _required_text(opt.get("text"), "t (option text)", at),
            "why": _optional_text(opt.get("why"), "why", at),
            "is_root": _flag(opt.get("is_root", False), "root", at),
            "is_recommended": _flag(opt.get("is_recommended", False), "rec", at),
        }
        ensure_fits(finding_options, row, {"key": f"{at}: k"})
        options.append(row)
    if sum(o["is_recommended"] for o in options) > 1:
        raise ValueError(f"{where}: more than one recommended option (rec) — at most one")
    return options


def reviewer_token(value, where: str = "reviewer") -> str:
    """One canonical reviewer token (#930 §2a): a string, lowercased, then
    matching ^[a-z0-9][a-z0-9:._-]*$ and at most the column's 50 chars —
    refused otherwise (never stripped, never truncated)."""
    if not isinstance(value, str):
        raise ValueError(f"{where}: a reviewer token must be a string, got {value!r}")  # noqa: TRY004 — every refusal is a ValueError
    token = value.lower()
    if REVIEWER_TOKEN.fullmatch(token) is None:
        raise ValueError(
            f"{where}: reviewer token {value!r} is not a token — lowercase letters, "
            "digits and : . _ - , starting with a letter or digit (e.g. 'codex', "
            "'panel:security')")
    ensure_fits(finding_reviewers, {"reviewer": token}, {"reviewer": f"{where}: reviewer"})
    return token


def _reviewer_tokens(value, where: str) -> list[str]:
    """`reviewers` of one finding: absent / None = no tokens; otherwise a
    non-empty list of tokens (reviewer_token), duplicates (after
    lowercasing) refused. Input order is kept."""
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where}: reviewers must be a non-empty list of reviewer tokens")
    tokens: list[str] = []
    for i, raw in enumerate(value):
        token = reviewer_token(raw, f"{where}.reviewers[{i}]")
        if token in tokens:
            raise ValueError(f"{where}: reviewer {token!r} is listed twice")
        tokens.append(token)
    return tokens


def _parse_finding(raw, index: int) -> dict:
    """Validate one finding of a batch; returns the column values (seq may be
    None = auto), its options and the card used by classify()."""
    where = f"findings[{index}]"
    f = _canonical(_card_shape(raw, where), _FINDING_SPELLINGS, where)
    fingerprint = _required_text(f.get("fingerprint"), "fp (fingerprint)", where)
    fingerprint_norm = normalize_fingerprint(fingerprint)
    if not fingerprint_norm:
        raise ValueError(f"{where}: fp (fingerprint) is empty after normalisation")
    norm_width = findings.c.fingerprint_norm.type.length
    if len(fingerprint_norm) > norm_width:
        # The identity is never cut (K-9): two findings sharing a prefix
        # would become one recurrence.
        raise ValueError(
            f"{where}: fp (fingerprint) too long after normalisation: "
            f"{len(fingerprint_norm)} chars, max {norm_width} — refused, never truncated")
    for field, short in (("impact", "S"), ("reachability", "O"), ("evidence", "E"),
                         ("fix_cost", "K"), ("origin", "origin"),
                         ("finding_type", "type")):
        if f.get(field) is None:
            label = short if short == field else f"{short} ({field})"
            raise ValueError(f"{where}: {label} is required")
    values = {
        "seq": None if f.get("seq") is None else _seq_value(f["seq"], where),
        "title": _required_text(f.get("title"), "title", where),
        "fingerprint": fingerprint,
        "fingerprint_norm": fingerprint_norm,
        "impact": _level(f["impact"], "S (impact)", where),
        "reachability": _reachability(f["reachability"], where),
        "evidence": _level(f["evidence"], "E (evidence)", where),
        "fix_cost": _enum(f["fix_cost"], rubric.FIX_COSTS, "K (fix_cost)", where),
        "origin": _enum(f["origin"], rubric.ORIGINS, "origin", where),
        "finding_type": _enum(f["finding_type"], rubric.FINDING_TYPES, "type", where),
        "area": _optional_text(f.get("area"), "area", where),
        "reviewer": _required_text(f.get("source"), "src (source)", where),
        "context": _context(f.get("context"), where),
    }
    flags_raw = f.get("flags")
    flags = {} if flags_raw is None else _canonical(flags_raw, _FLAG_SPELLINGS, f"{where}.flags")
    values["conflict"] = _flag(flags.get("conflict", False), "conflict", where)
    values["violation"] = _flag(flags.get("violation", True), "violation", where)
    values["dispute"] = _flag(flags.get("dispute", False), "dispute", where)
    values["open_design"] = _flag(flags.get("open_design", False), "openDesign", where)

    override = f.get("class_override")
    reason = f.get("class_reason")
    if override is not None:
        _enum(override, rubric.CLASS_ORDER, "cls (class override)", where)
        _required_text(reason, "clsWhy (the override's reason)", where)
    elif reason is not None:
        raise ValueError(f"{where}: clsWhy without cls — a reason needs an override")
    values["class_override"] = override
    values["class_reason"] = reason
    values["class_rec"] = rubric.classify(_card_fields(values))
    ensure_fits(findings, values, {k: f"{where}: {k}" for k in values})
    return {"values": values, "options": _parse_options(f.get("options"), where),
            "reviewers": _reviewer_tokens(f.get("reviewers"), where)}


def _card_fields(row: Mapping) -> dict:
    """The rubric fields of a findings row (or its pending values) under the
    template's card keys — the input of classify() / priority()."""
    o = row["reachability"]
    card = {
        "S": row["impact"],
        "O": o if o == "?" else int(o),
        "E": row["evidence"],
        "K": row["fix_cost"],
        "origin": row["origin"],
        "type": row["finding_type"],
    }
    if row["conflict"]:
        card["conflict"] = True
    if not row["violation"]:
        card["violation"] = False
    if row["dispute"]:
        card["dispute"] = True
    if row["open_design"]:
        card["openDesign"] = True
    return card


def _effective_class(row: Mapping) -> str:
    return row["class_override"] or row["class_rec"]


# ── cycle access ─────────────────────────────────────────────────────────────


def _lock_open_cycle(conn: sa.Connection, cycle_id: int) -> sa.RowMapping:
    """Take the cycle row lock (db.lock_cycle_row) and return the cycle;
    refuses a missing or closed cycle. The first explicit lock of decide /
    suggest / close; record_findings takes the project row before it (lock
    order: module docstring). The cycle is resolved (_resolve_cycle) before
    the lock, unlocked — the state checks read it again after the lock."""
    if not lock_cycle_row(conn, cycle_id):
        raise ValueError(f"Triage cycle #{cycle_id} not found")
    cycle = _cycle_row(conn, cycle_id)
    if cycle["closed_at"] is not None:
        raise ValueError(
            f"Triage cycle #{cycle_id} ({cycle['code']}) is closed — "
            "findings, decisions and suggestions are refused after the close"
        )
    return cycle


def _cycle_row(conn: sa.Connection, cycle_id: int) -> sa.RowMapping:
    row = conn.execute(
        sa.select(triage_cycles).where(
            triage_cycles.c.id == cycle_id, triage_cycles.c.deleted_at.is_(None))
    ).mappings().first()
    if row is None:
        raise ValueError(f"Triage cycle #{cycle_id} not found")
    return row


def _live_findings(conn: sa.Connection, cycle_id: int) -> list[sa.RowMapping]:
    return list(conn.execute(
        sa.select(findings).where(
            findings.c.cycle_id == cycle_id, findings.c.deleted_at.is_(None)
        ).order_by(findings.c.seq)
    ).mappings())


def _live_options(conn: sa.Connection, finding_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {fid: [] for fid in finding_ids}
    if not finding_ids:
        return out
    rows = conn.execute(
        sa.select(finding_options).where(
            finding_options.c.finding_id.in_(finding_ids),
            finding_options.c.deleted_at.is_(None),
        ).order_by(finding_options.c.finding_id, finding_options.c.id)
    ).mappings()
    for r in rows:
        out[r["finding_id"]].append(dict(r))
    return out


def _project_id(conn: sa.Connection, project: str) -> int:
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project is required (a project slug)")
    return resolve_project_id(conn, project)


def _resolve_cycle(conn: sa.Connection, project: str | None, ref) -> tuple[int, int]:
    """resolve_cycle on a connection → (cycle_id, project_id). A plain read
    (no lock): a writer locks the cycle afterwards and re-reads it there."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        cycle_id = _int_in_range(ref, "cycle id", INT_MAX)
        row = conn.execute(
            sa.select(triage_cycles.c.id, triage_cycles.c.project_id).where(
                triage_cycles.c.id == cycle_id, triage_cycles.c.deleted_at.is_(None))
        ).first()
        if row is None:
            raise ValueError(f"Triage cycle #{cycle_id} not found")
        if project is not None and _project_id(conn, project) != row.project_id:
            raise ValueError(f"Triage cycle #{cycle_id} does not belong to project '{project}'")
        return row.id, row.project_id
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(
            f"cycle must be a cycle id (an integer) or a cycle code (a string), got {ref!r}")
    if project is None:
        raise ValueError(
            f"cycle code '{ref}' needs its project — a code is unique only within a project")
    project_id = _project_id(conn, project)
    cycle_id = conn.execute(
        sa.select(triage_cycles.c.id).where(
            triage_cycles.c.project_id == project_id,
            triage_cycles.c.code == ref,
            triage_cycles.c.deleted_at.is_(None),
        )
    ).scalar()
    if cycle_id is None:
        raise ValueError(f"No triage cycle '{ref}' in project '{project}'")
    return cycle_id, project_id


def resolve_cycle(engine_or_conn: sa.Engine | sa.Connection, project: str | None, ref) -> int:
    """The ONE cycle resolver (K-13) → the id of a live triage cycle.

    `ref` an int = the cycle's global id; with `project` given, the cycle
    must belong to that project (else ValueError). `ref` a str = the cycle's
    code, which is unique only within a project, so `project` is required.
    A numeric string is a code like any other — never re-read as an id (a
    caller holding an id passes the int). Refusals (ValueError): a missing
    or soft-deleted cycle or project, a cycle of another project, a code
    without its project, an id outside 1..2147483647, any other type."""
    if isinstance(engine_or_conn, sa.Connection):
        return _resolve_cycle(engine_or_conn, project, ref)[0]
    with engine_or_conn.connect() as conn:
        return _resolve_cycle(conn, project, ref)[0]


# ── open ─────────────────────────────────────────────────────────────────────


def open_triage(
    engine: sa.Engine,
    project: str,
    code: str,
    topic: str,
    created_by: str,
    *,
    scope: str | None = None,
    base_commit: str | None = None,
    rubric_version: int = 2,
    artifact_url: str | None = None,
) -> dict:
    """Open a triage cycle of `project` → {cycle_id, code, project}.

    `code` is unique among the project's live cycles (a duplicate is refused);
    code / topic / created_by are required; every VARCHAR is width-checked.
    """
    where = "open_triage"
    values = {
        "code": _required_text(code, "code", where),
        "topic": _required_text(topic, "topic", where),
        "created_by": _required_text(created_by, "created_by", where),
        "scope": _optional_text(scope, "scope", where),
        "base_commit": _optional_text(base_commit, "base_commit", where),
        "artifact_url": _optional_text(artifact_url, "artifact_url", where),
    }
    _int_in_range(rubric_version, "rubric_version", RUBRIC_VERSION_MAX, where)
    ensure_fits(triage_cycles, values)
    now = _now()
    try:
        with engine.begin() as conn:
            project_id = _project_id(conn, project)
            duplicate = conn.execute(
                sa.select(triage_cycles.c.id).where(
                    triage_cycles.c.project_id == project_id,
                    triage_cycles.c.code == code,
                    triage_cycles.c.deleted_at.is_(None),
                )
            ).first()
            if duplicate is not None:
                raise ValueError(
                    f"Triage cycle '{code}' already exists in project '{project}' "
                    f"(#{duplicate.id})")
            cycle_id = conn.execute(
                triage_cycles.insert().values(
                    project_id=project_id, rubric_version=rubric_version,
                    opened_at=now, created_at=now, updated_at=now, **values,
                )
            ).inserted_primary_key[0]
    except sa.exc.IntegrityError as exc:
        # A concurrent open of the same code won the live-unique index.
        raise ValueError(
            f"Triage cycle '{code}' already exists in project '{project}'") from exc
    return {"cycle_id": cycle_id, "code": code, "project": project}


# ── record ───────────────────────────────────────────────────────────────────


def _first_seen_id(conn: sa.Connection, project_id: int, fingerprint_norm: str) -> int | None:
    """The earliest live finding of the project (in a live cycle) with this
    normalised fingerprint; None = the new finding is the first."""
    return conn.execute(
        sa.select(sa.func.min(findings.c.id))
        .select_from(findings.join(triage_cycles, triage_cycles.c.id == findings.c.cycle_id))
        .where(
            triage_cycles.c.project_id == project_id,
            triage_cycles.c.deleted_at.is_(None),
            findings.c.deleted_at.is_(None),
            findings.c.fingerprint_norm == fingerprint_norm,
        )
    ).scalar()


def record_findings(
    engine: sa.Engine, cycle: int | str, findings_in: list[dict], *, project: str | None = None,
) -> dict:
    """Record a batch of findings in an OPEN cycle, atomically →
    {cycle_id, recorded: [{seq, finding_id, first_seen_id}]}.

    `cycle` = the cycle's id, or its code with `project` (resolve_cycle).
    Keys per finding (either spelling of a field, never both): seq|id
    (optional — next free; id is the CARD's 'K-<n>'), title, fp|fingerprint,
    S|impact, O|reachability, E|evidence, K|fix_cost, origin,
    type|finding_type, area, src|source (the reviewer as free display
    text, required), reviewers (optional, #930 §2a: a non-empty list of
    canonical reviewer tokens, lowercased, ^[a-z0-9][a-z0-9:._-]*$, ≤ 50
    chars, no duplicates — never derived from src; absent = no tokens),
    ctx|context (str or list of paragraphs), flags {conflict, violation, dispute,
    openDesign|open_design} — or those flags at the top level, as an
    exported CARD carries them — cls|class_override + clsWhy|class_reason,
    opts|options [{k|key, t|text, why, root|is_root, rec|is_recommended}].
    So an exported CARD records back unchanged (a new finding; decisions
    are never imported). Unknown keys, bad enums, missing fp / title / src /
    S / O / E / K / origin / type, a seq outside 1..2147483647 (explicit or
    auto), a fingerprint whose normal form outgrows its column, two
    recommended options, a duplicate option key or a duplicate seq refuse
    the WHOLE batch (ValueError, nothing written). class_rec =
    rubric.classify(); first_seen_id = the earliest live finding of the
    project with the same normalised fingerprint (earlier entries of the
    same batch included) — writers of one project serialise on the project
    row, so two cycles recording the same new fingerprint at once link the
    second to the first. Locks: the project row, then the cycle row (FOR NO
    KEY UPDATE on PostgreSQL); the inserts take FOR KEY SHARE on the cycle
    and on first_seen's finding, which a promotion's lock on that finding
    does not block (K-21).
    """
    if not isinstance(findings_in, list) or not findings_in:
        raise ValueError("findings must be a non-empty list")
    parsed = [_parse_finding(raw, i) for i, raw in enumerate(findings_in)]
    explicit = [p["values"]["seq"] for p in parsed if p["values"]["seq"] is not None]
    if len(explicit) != len(set(explicit)):
        dupes = sorted({s for s in explicit if explicit.count(s) > 1})
        raise ValueError(f"duplicate seq in the batch: {', '.join(map(str, dupes))}")

    now = _now()
    with engine.begin() as conn:
        cycle_id, project_id = _resolve_cycle(conn, project, cycle)
        # K-6: first_seen is project-wide, so every recorder of the project
        # queues on the project row BEFORE the cycle lock and the lookup.
        # Only recorders take this lock; a promotion inserting an item of
        # this project needs only KEY SHARE on the row, which the NO KEY
        # UPDATE row lock does not block (K-21).
        if not lock_row(conn, projects, project_id):
            raise ValueError(f"Project #{project_id} of triage cycle #{cycle_id} not found")
        cycle_row = _lock_open_cycle(conn, cycle_id)
        taken = set(conn.execute(
            sa.select(findings.c.seq).where(
                findings.c.cycle_id == cycle_id, findings.c.deleted_at.is_(None))
        ).scalars())
        clash = sorted(set(explicit) & taken)
        if clash:
            raise ValueError(
                f"seq already recorded in cycle {cycle_row['code']}: "
                + ", ".join(f"K-{s}" for s in clash))
        next_seq = max(taken | set(explicit), default=0) + 1
        batch = []
        for p in parsed:
            values = dict(p["values"])
            if values["seq"] is None:
                values["seq"] = next_seq
                next_seq += 1
            batch.append((values, p["options"], p["reviewers"]))
        if next_seq - 1 > INT_MAX:
            raise ValueError(
                f"seq exhausted in cycle {cycle_row['code']}: the next free seq passes "
                f"{INT_MAX} — nothing recorded (pass explicit seqs)")
        recorded = []
        for values, options, reviewers in batch:
            values["first_seen_id"] = _first_seen_id(
                conn, project_id, values["fingerprint_norm"])
            finding_id = conn.execute(
                findings.insert().values(
                    cycle_id=cycle_id, created_at=now, updated_at=now, **values)
            ).inserted_primary_key[0]
            if options:
                conn.execute(finding_options.insert(), [
                    {"finding_id": finding_id, "created_at": now, "updated_at": now, **o}
                    for o in options
                ])
            if reviewers:
                conn.execute(finding_reviewers.insert(), [
                    {"finding_id": finding_id, "reviewer": token, "created_at": now}
                    for token in reviewers
                ])
            recorded.append({"seq": values["seq"], "finding_id": finding_id,
                             "first_seen_id": values["first_seen_id"]})
    return {"cycle_id": cycle_id, "recorded": recorded}


# ── decide / suggest ─────────────────────────────────────────────────────────


def _seq_of_key(key) -> int:
    if isinstance(key, int) and not isinstance(key, bool) and key >= 1:
        return key
    if isinstance(key, str):
        m = _SEQ_KEY.fullmatch(key.strip())
        if m:
            return int(m.group(1))
        if key.strip().isdigit() and int(key) >= 1:
            return int(key)
    raise ValueError(f"finding key {key!r}: expected 'K-<n>' or a seq number")


def _resolve_answers(conn: sa.Connection, cycle_id: int, answers, label: str) -> list[tuple]:
    """Validate a decisions / suggestions map against the cycle's live
    findings → [(finding row, dec, opt, note)]. Atomic by construction: any
    bad entry raises before the caller writes."""
    if not isinstance(answers, Mapping) or not answers:
        raise ValueError(f"{label} must be a non-empty object keyed by 'K-<n>' or seq")
    live = {r["seq"]: r for r in _live_findings(conn, cycle_id)}
    options = _live_options(conn, [r["id"] for r in live.values()])
    resolved: list[tuple] = []
    seen: set[int] = set()
    for key, raw in answers.items():
        where = f"{label}[{key!r}]"
        seq = _seq_of_key(key)
        if seq in seen:
            raise ValueError(f"{where}: K-{seq} is addressed twice")
        seen.add(seq)
        if seq not in live:
            raise ValueError(f"{where}: no live finding K-{seq} in this cycle")
        entry = _canonical(raw, _DECISION_SPELLINGS, where)
        dec = _enum(entry.get("dec"), rubric.DECISIONS, "dec", where)
        opt = entry.get("opt")
        if opt == "":
            opt = None
        if opt is not None:
            keys = [o["key"] for o in options[live[seq]["id"]]]
            if opt not in keys:
                raise ValueError(
                    f"{where}: opt {opt!r} is not an option of K-{seq}"
                    + (f" (options: {', '.join(keys)})" if keys else " (it has no options)"))
        note = _optional_text(entry.get("note"), "note", where)
        resolved.append((live[seq], dec, opt, note))
    return resolved


def _undecided(conn: sa.Connection, cycle_id: int) -> list[int]:
    return list(conn.execute(
        sa.select(findings.c.seq).where(
            findings.c.cycle_id == cycle_id,
            findings.c.deleted_at.is_(None),
            findings.c.decision.is_(None),
        ).order_by(findings.c.seq)
    ).scalars())


def _refuse_open_promotion(conn: sa.Connection, row: Mapping, dec: str) -> None:
    """K-2: a finding promoted to a still-open item cannot be re-decided
    reject / note — the owner closes that item first (resolve / wont_fix),
    deliberately; it is never closed here. The item row is locked (lock
    order cycle → item) so a concurrent reopen cannot slip in between. A
    closed or deleted item stays linked as history."""
    if dec not in LEDGER_ONLY_DECISIONS or row["item_id"] is None:
        return
    item_id = row["item_id"]
    if not lock_item_row(conn, item_id):
        return  # soft-deleted: no active work left
    status = conn.execute(sa.select(items.c.status).where(items.c.id == item_id)).scalar_one()
    if status not in TERMINAL_STATUSES:
        raise ValueError(
            f"decisions['K-{row['seq']}']: K-{row['seq']} is promoted to item #{item_id}, "
            f"still {status} — close item #{item_id} first (resolve or wont_fix), "
            f"then decide '{dec}'")


def decide_findings(
    engine: sa.Engine,
    cycle: int | str,
    decisions: dict[str, dict],
    *,
    decided_by: str,
    decision_source: str,
    project: str | None = None,
) -> dict:
    """Record the OWNER's decisions in an OPEN cycle, atomically →
    {decided: n, undecided: [seq…]}.

    `cycle` = the cycle's id, or its code with `project` (resolve_cycle).
    Keys: 'K-<n>' or the seq; values {dec, opt?, note?} — dec one of
    rubric.DECISIONS, opt an option key of that finding ('' / None = none),
    note the owner's comment VERBATIM. `decided_by` (who decided — the
    owner) and `decision_source` (where the owner's words are recorded,
    e.g. 'owner chat 2026-10-13') are required, ≤ 200 chars each, and are
    written with decided_at on every decided finding (K-5). They are an
    audit record of what the caller states, not an authentication of the
    owner's consent; an agent's own proposals go to suggest_findings. The
    same UPDATE writes the provenance of the decision's write (K-23) from
    dct.core.provenance.get_provenance(): decision_actor_type,
    decision_actor_client, decision_session_id, decision_actor_source — the
    last decision's writer, per Q1; audit, not authentication. The
    finding's creation provenance (the client that recorded it) is never
    touched. A decision overwrites a previous one while the cycle is open
    (decided_at, decided_by, decision_source and all four decision_* writer
    fields refreshed — an unknown one becomes NULL, never left from the
    previous client); a closed cycle refuses. 'reject' / 'note' on a
    finding whose promoted item is still open is refused, naming the item
    to close first (K-2). Locks: the cycle row, then the promoted item of
    each such reject / note (lock order: module docstring). `undecided`
    lists the live findings still without a decision.
    """
    stamp = _decision_stamp(decided_by, decision_source, "decide_findings")
    now = _now()
    with engine.begin() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        _lock_open_cycle(conn, cycle_id)
        return _decide_locked(conn, cycle_id, decisions, stamp, now)


def _decision_stamp(decided_by, decision_source, where: str) -> dict:
    """The audit columns written with every owner decision: decided_by /
    decision_source (K-5, required, width-checked), the K-23 writer
    provenance — all four, every time (an unknown field is written as NULL,
    so an overwrite never keeps the previous client's value) — and
    decision_text_origin (K-40), NULL here; decide_findings_from_text puts
    the text's prefill line into it. Never written by suggest_findings."""
    stamp = {
        "decided_by": _required_text(decided_by, "decided_by", where),
        "decision_source": _required_text(decision_source, "decision_source", where),
    }
    ensure_fits(findings, stamp)
    writer = get_provenance()
    stamp.update(
        decision_actor_type=writer.actor_type,
        decision_actor_client=writer.actor_client,
        decision_session_id=writer.session_id,
        decision_actor_source=writer.source,
        # K-40: only decide_findings_from_text sets it (the text's prefill
        # line); every other decision write resets it, so it always
        # describes the write that produced the current decision.
        decision_text_origin=None,
    )
    return stamp


def _decide_locked(
    conn: sa.Connection, cycle_id: int, decisions, stamp: dict, now: datetime,
) -> dict:
    """The ONE connection-level decision step (decide_findings,
    decide_findings_from_text, accept_suggestions): the caller holds the
    cycle row lock of an open cycle (_lock_open_cycle) in this transaction.
    Reads the live findings and options, validates EVERY entry
    (_resolve_answers, then _refuse_open_promotion — which locks the item of
    each promoted reject / note: lock order cycle → item) before the first
    write, so one bad entry writes nothing; then writes each addressed
    finding's decision with `stamp` (_decision_stamp). Findings not
    addressed keep their stored state. → {decided: n, undecided: [seq…]}
    with `undecided` read after the writes."""
    resolved = _resolve_answers(conn, cycle_id, decisions, "decisions")
    for row, dec, _opt, _note in resolved:
        _refuse_open_promotion(conn, row, dec)
    for row, dec, opt, note in resolved:
        conn.execute(findings.update().where(findings.c.id == row["id"]).values(
            decision=dec, decision_opt=opt, decision_note=note,
            decided_at=now, updated_at=now, **stamp))
    return {"decided": len(resolved), "undecided": _undecided(conn, cycle_id)}


def suggest_findings(
    engine: sa.Engine,
    cycle: int | str,
    by: str,
    suggestions: dict[str, dict],
    *,
    project: str | None = None,
) -> dict:
    """Record an agent's proposals (the template's SUGGESTED) in an OPEN
    cycle, atomically → {suggested: n}. `cycle` = id, or code with
    `project` (resolve_cycle). Same keys and validation as decide_findings;
    writes only suggested_* (never the decision, its origin or the
    decision_* writer provenance); `by` names the proposer (e.g. 'Codex').
    Locks: the cycle row."""
    by = _required_text(by, "by", "suggest_findings")
    ensure_fits(findings, {"suggested_by": by})
    now = _now()
    with engine.begin() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        _lock_open_cycle(conn, cycle_id)
        resolved = _resolve_answers(conn, cycle_id, suggestions, "suggestions")
        for row, dec, opt, note in resolved:
            conn.execute(findings.update().where(findings.c.id == row["id"]).values(
                suggested_decision=dec, suggested_opt=opt, suggested_note=note,
                suggested_by=by, suggested_at=now, updated_at=now))
    return {"suggested": len(resolved)}


# ── decision text / acceptance / cycle update (#930) ─────────────────────────

# The decision labels of the triage page — the template's DECISIONS, in
# Polish because the page emits them — and the English decision tokens,
# each → its rubric decision. Matched case-insensitively after
# _label_key(); the page's "— (brak decyzji)" marks an undecided card.
DECISION_LABELS: dict[str, str] = {
    "Fix teraz": "fix",
    "Zweryfikuj": "verify",
    "Backlog → dct": "backlog",
    "Odrzuć / zaakceptuj stan": "reject",
    "Odnotuj": "note",
    "Do dyskusji": "discuss",
    **{token: token for token in rubric.DECISIONS},
}
NO_DECISION_LABEL = "— (brak decyzji)"
# The page's header line ("Triage <topic> (<project>) — decyzje ownera
# (<stamp>), rubric v<n>:") and its optional prefill line ("Punkt wyjścia:
# propozycje <by> …"), both skipped before the first card line.
_TEXT_HEADER = re.compile(r"Triage\b.*:")
_TEXT_PREFILL = "Punkt wyjścia:"
_TEXT_CARD = re.compile(r"[Kk]-([0-9]+)\s*:\s*(.*)")
_TEXT_NOTE = " · uwagi:"
_TEXT_SEGMENT = " · "
_TEXT_VARIANT = re.compile(r"wariant \(([^()]*)\)", re.IGNORECASE)
_TEXT_ANNOTATION = re.compile(r"\s*\[[^\]]*\]")
# Separators a label may be written with: → / -> / — / – / "/" are one.
_LABEL_SEPARATOR = re.compile(r"\s*(?:→|->|—|–|/)\s*")
# The note prefix of an accepted proposal (the template's closing rule).
ACCEPTED_PREFIX = "[zaakceptowana propozycja {by}]"


def _label_key(label: str) -> str:
    return " | ".join(
        " ".join(part.split()).casefold() for part in _LABEL_SEPARATOR.split(label.strip()))


_LABEL_KEYS = {_label_key(label): dec for label, dec in DECISION_LABELS.items()}


def parse_decision_batch(text: str, *, cards: list[dict]) -> dict:
    """Parse the page's generated decision text ("Tekst do wklejenia") →
    {"decisions": {"K-<n>": {dec, opt, note}}, "skipped": ["K-<n>"…],
    "prefill_line": str | None} — a pure function, the one parser of that
    text (decide_findings_from_text, the CLI and MCP adapters;
    parse_decision_text returns only its decisions).

    Grammar (frozen, #930 §8), line by line (\\n, \\r\\n, \\r or any other
    str.splitlines boundary; trailing whitespace of a line ignored; blank
    lines skipped):
    - before the first card line, a header line `Triage … :` and a line
      starting `Punkt wyjścia:` are skipped (each at most once); the
      latter is returned verbatim (stripped) as `prefill_line` (K-40: it
      proves the form was seeded from proposals, not who wrote each note),
      None without one;
    - every other non-blank line is a card line
      `K-<n>: <label>[ · wariant (<k>)][ [annotation]…][ · uwagi: <note>]`.
      K-<n> is canonicalised (K-01 = K-1) and must name a card of `cards`
      (the export's CARDS: `id`, `opts[].k`); a card addressed twice is
      refused. <label> is one of DECISION_LABELS — the page's Polish labels
      or the English tokens fix / verify / backlog / reject / note /
      discuss — case-insensitive, with → / -> / — / – / "/" interchangeable
      as separators and whitespace collapsed. `wariant (<k>)` (optional)
      must be an option key of that card. Bracketed annotations before the
      note are ignored. The note is everything after the FIRST ` · uwagi:`
      with one leading space dropped — brackets, `·` and a repeated
      `uwagi:` inside it kept verbatim; an empty note is None.
    - K-31: a card line whose label is "— (brak decyzji)" (the page's
      undecided card) with an empty note is NOT addressed: listed in
      `skipped` (in text order), absent from `decisions` — the card keeps
      its stored state. Its key, duplicates and `wariant (<k>)` are still
      validated. The same line WITH a non-empty note refuses the whole
      text (the owner wrote a comment without choosing a decision).
    Any other non-blank line, a malformed card line, an unknown K-<n> or
    option, or a text without a card line (empty, header only) →
    ValueError naming the 1-based line. A text of skipped lines only is
    valid: decisions = {}. A card not mentioned is in neither list."""
    if not isinstance(text, str):
        raise ValueError(f"decision text must be a string, got {type(text).__name__}")  # noqa: TRY004 — every refusal is a ValueError
    options: dict[int, list[str]] = {}
    for card in cards:
        m = _SEQ_KEY.fullmatch(str(card.get("id", "")))
        if m is None:
            raise ValueError(f"cards: card id {card.get('id')!r} is not 'K-<n>'")
        options[int(m.group(1))] = [o["k"] for o in card.get("opts") or []]
    decisions: dict[str, dict] = {}
    skipped: list[str] = []
    prefill_line: str | None = None
    header_seen = prefill_seen = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped:
            continue
        where = f"line {number}"
        card = _TEXT_CARD.fullmatch(stripped)
        first = not decisions and not skipped
        if card is None:
            if first and not header_seen and not prefill_seen \
                    and _TEXT_HEADER.fullmatch(stripped):
                header_seen = True
                continue
            if first and not prefill_seen and stripped.startswith(_TEXT_PREFILL):
                prefill_seen = True
                prefill_line = stripped
                continue
            raise ValueError(
                f"{where}: not a decision line — expected 'K-<n>: <decision>"
                f"[ · wariant (<k>)][ · uwagi: <note>]', got {stripped!r}")
        seq = int(card.group(1))
        key = f"K-{seq}"
        where = f"{where} ({key})"
        if seq not in options:
            raise ValueError(f"{where}: no card {key} in this cycle")
        if key in decisions or key in skipped:
            raise ValueError(f"{where}: {key} is addressed twice")
        rest = card.group(2)
        note = None
        at = rest.find(_TEXT_NOTE)
        if at >= 0:
            note = rest[at + len(_TEXT_NOTE):].removeprefix(" ") or None
            rest = rest[:at]
        segments = _TEXT_ANNOTATION.sub("", rest).strip().split(_TEXT_SEGMENT)
        label = segments[0].strip()
        undecided = _label_key(label) == _label_key(NO_DECISION_LABEL)
        dec = None if undecided else _LABEL_KEYS.get(_label_key(label))
        if dec is None and not undecided:
            raise ValueError(
                f"{where}: unknown decision {label!r} (accepted: "
                + ", ".join(DECISION_LABELS) + ")")
        opt = None
        for segment in segments[1:]:
            variant = _TEXT_VARIANT.fullmatch(segment.strip())
            if variant is None or opt is not None:
                raise ValueError(
                    f"{where}: unexpected part {segment.strip()!r} — after the decision "
                    "only ' · wariant (<k>)', [annotations] and ' · uwagi: <note>'")
            opt = variant.group(1)
            if opt not in options[seq]:
                raise ValueError(
                    f"{where}: wariant ({opt}) is not an option of {key}"
                    + (f" (options: {', '.join(options[seq])})" if options[seq]
                       else " (it has no options)"))
        if undecided:
            if note is not None:
                raise ValueError(
                    f"{where}: {key} has a note but no decision ('{NO_DECISION_LABEL}') "
                    "— choose a decision for it or remove the note; nothing was recorded")
            skipped.append(key)
            continue
        decisions[key] = {"dec": dec, "opt": opt, "note": note}
    if not decisions and not skipped:
        raise ValueError("decision text has no card line ('K-<n>: <decision> …')")
    return {"decisions": decisions, "skipped": skipped, "prefill_line": prefill_line}


def parse_decision_text(text: str, *, cards: list[dict]) -> dict[str, dict]:
    """The decisions of parse_decision_batch only → {"K-<n>": {dec, opt,
    note}} (skipped "— (brak decyzji)" lines and the prefill line are
    dropped; an all-skipped text gives {})."""
    return parse_decision_batch(text, cards=cards)["decisions"]


def _parse_cards(conn: sa.Connection, cycle_id: int) -> list[dict]:
    """The cycle's live cards as parse_decision_text needs them (id, opts.k)."""
    rows = _live_findings(conn, cycle_id)
    options = _live_options(conn, [r["id"] for r in rows])
    return [{"id": f"K-{r['seq']}", "opts": [{"k": o["key"]} for o in options[r["id"]]]}
            for r in rows]


def decide_findings_from_text(
    engine: sa.Engine,
    cycle: int | str,
    text: str,
    *,
    decided_by: str,
    decision_source: str,
    project: str | None = None,
) -> dict:
    """Record the OWNER's decisions pasted as the page's decision text →
    {decided: n, undecided: [seq…], parsed: n, skipped: ['K-<n>'…]}.

    `cycle` = id, or code with `project` (resolve_cycle). The text is parsed
    (parse_decision_batch, against the cycle's live cards read first)
    BEFORE the write transaction — a parse error writes and locks nothing;
    the entries are then validated again and written in ONE transaction
    under the cycle lock through the step decide_findings uses
    (_decide_locked: same stamp, provenance, K-2 refusal and atomicity —
    the first invalid entry refuses the whole batch). Only the findings the
    text decides are changed: a "— (brak decyzji)" line without a note is
    skipped (K-31, listed in `skipped`, its stored decision kept); a text of
    skipped lines only still locks and checks the open cycle, then writes
    nothing (decided 0). The text's "Punkt wyjścia:" line, when present, is
    stored verbatim in decision_text_origin of every finding it decides
    (K-40 — never in decision_note / decision_source; a later decision
    without it resets the field to NULL). `undecided` is the post-write
    state. Locks: as decide_findings."""
    stamp = _decision_stamp(decided_by, decision_source, "decide_findings_from_text")
    with engine.connect() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        cards = _parse_cards(conn, cycle_id)
    batch = parse_decision_batch(text, cards=cards)
    decisions = batch["decisions"]
    stamp["decision_text_origin"] = batch["prefill_line"]
    now = _now()
    with engine.begin() as conn:
        _lock_open_cycle(conn, cycle_id)
        if decisions:
            out = _decide_locked(conn, cycle_id, decisions, stamp, now)
        else:
            out = {"decided": 0, "undecided": _undecided(conn, cycle_id)}
    return {**out, "parsed": len(decisions), "skipped": batch["skipped"]}


def accept_suggestions(
    engine: sa.Engine,
    cycle: int | str,
    *,
    expected_by: str,
    decided_by: str,
    decision_source: str,
    expected_digest: str | None = None,
    overwrite: bool = False,
    project: str | None = None,
) -> dict:
    """The OWNER accepts the agent's proposals as final → {decided: n,
    undecided: [seq…], without_suggestion: ['K-<n>'…], already_decided:
    ['K-<n>'…]}.

    `cycle` = id, or code with `project` (resolve_cycle). Every live finding
    carrying a suggestion is addressed: decision = suggested_decision, opt =
    suggested_opt, note = "[zaakceptowana propozycja <by>] " + the suggested
    note (just the bracketed prefix when the proposal has no note), <by>
    being THAT finding's suggested_by. Bound to the proposals the owner saw:
    refused when any addressed finding's suggested_by differs from
    `expected_by` (required), and — K-32 — when `expected_digest` (the
    SUGGESTED.digest of the export the owner saw; None skips the check in
    this workflow, the MCP / CLI / skill adapters always pass it) differs
    from suggestion_digest recomputed from the rows read under the cycle
    lock: a proposal revised (even by the same author), added or withdrawn,
    or a card recorded or changed since that export refuses the whole
    acceptance. Refused when no finding has a suggestion, and
    when an addressed finding already has a decision unless `overwrite`
    (then it is replaced and still listed in already_decided). Findings
    without a suggestion are left untouched (without_suggestion). One
    transaction under the cycle lock; suggestions are read after the lock
    and written through the decide_findings step (_decide_locked: stamp,
    provenance, K-2 refusal, atomicity). Locks: as decide_findings."""
    where = "accept_suggestions"
    expected_by = _required_text(expected_by, "expected_by", where)
    if not isinstance(overwrite, bool):
        raise ValueError(f"{where}: overwrite must be true or false, got {overwrite!r}")  # noqa: TRY004 — every refusal is a ValueError
    if expected_digest is not None:
        expected_digest = _required_text(expected_digest, "expected_digest", where)
    stamp = _decision_stamp(decided_by, decision_source, where)
    now = _now()
    with engine.begin() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        cycle_row = _lock_open_cycle(conn, cycle_id)
        rows = _live_findings(conn, cycle_id)
        addressed = [r for r in rows if r["suggested_decision"] is not None]
        if not addressed:
            raise ValueError(
                f"{where}: no finding of triage cycle {cycle_row['code']} has a suggestion "
                "— nothing to accept")
        foreign = [r for r in addressed if r["suggested_by"] != expected_by]
        if foreign:
            raise ValueError(
                f"{where}: the proposals are not all by {expected_by!r} — "
                + ", ".join(f"K-{r['seq']} by {r['suggested_by']!r}" for r in foreign)
                + " (nothing accepted; show the owner the current proposals first)")
        if expected_digest is not None:
            options = _live_options(conn, [r["id"] for r in rows])
            current = _rows_digest(cycle_id, rows, options)
            if current != expected_digest:
                raise ValueError(
                    f"{where}: the proposals or cards of triage cycle {cycle_row['code']} "
                    f"changed since the export the owner saw (digest {expected_digest!r}, "
                    f"now {current!r}) — nothing accepted; rebuild the page from the "
                    "current export and show it to the owner first")
        already = [f"K-{r['seq']}" for r in addressed if r["decision"] is not None]
        if already and not overwrite:
            raise ValueError(
                f"{where}: {', '.join(already)} already decided — nothing accepted "
                "(overwrite=true replaces those decisions with the proposals)")
        decisions = {}
        for r in addressed:
            prefix = ACCEPTED_PREFIX.format(by=r["suggested_by"])
            note = r["suggested_note"]
            decisions[f"K-{r['seq']}"] = {
                "dec": r["suggested_decision"], "opt": r["suggested_opt"],
                "note": f"{prefix} {note}" if note else prefix,
            }
        out = _decide_locked(conn, cycle_id, decisions, stamp, now)
    return {
        **out,
        "without_suggestion": [f"K-{r['seq']}" for r in rows
                               if r["suggested_decision"] is None],
        "already_decided": already,
    }


class _Omitted:
    """The type of OMITTED — an argument the caller did not pass."""

    def __repr__(self) -> str:
        return "OMITTED"


# update_triage: a field passed as OMITTED is left unchanged; None clears it.
OMITTED = _Omitted()


def update_triage(
    engine: sa.Engine,
    cycle: int | str,
    *,
    artifact_url: str | None | _Omitted = OMITTED,
    project: str | None = None,
) -> dict:
    """Update an OPEN cycle's page link → {cycle_id, code, artifact_url}.

    `cycle` = id, or code with `project` (resolve_cycle). `artifact_url`:
    OMITTED (the default) = unchanged, None = cleared, a non-empty string =
    set (≤ 500 chars, refused never truncated). Nothing to change (OMITTED)
    is refused; so is a closed or missing cycle. Locks: the cycle row."""
    where = "update_triage"
    if artifact_url is OMITTED:
        raise ValueError(f"{where}: nothing to update — pass artifact_url (None clears it)")
    if artifact_url is not None:
        artifact_url = _required_text(artifact_url, "artifact_url", where)
        ensure_fits(triage_cycles, {"artifact_url": artifact_url})
    now = _now()
    with engine.begin() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        cycle_row = _lock_open_cycle(conn, cycle_id)
        conn.execute(triage_cycles.update().where(triage_cycles.c.id == cycle_id).values(
            artifact_url=artifact_url, updated_at=now))
    return {"cycle_id": cycle_id, "code": cycle_row["code"], "artifact_url": artifact_url}


# ── close ────────────────────────────────────────────────────────────────────


def _answers(rows: list[Mapping], prefix: str) -> dict:
    return {
        f"K-{r['seq']}": {
            "dec": r[f"{prefix}decision"],
            "opt": r[f"{prefix}opt"] or "",
            "note": r[f"{prefix}note"] or "",
        }
        for r in rows
    }


def _decision_rows(rows: list[Mapping]) -> list[dict]:
    return [{"seq": r["seq"], "decision": r["decision"], "opt": r["decision_opt"],
             "note": r["decision_note"]} for r in rows]


def _closed_payload(cycle: Mapping, rows: list[Mapping]) -> dict:
    """The template's CLOSED constant from a closed cycle and its findings."""
    notes = cycle["closed_notes"]
    return {
        "date": cycle["closed_at"].strftime("%Y-%m-%d"),
        "source": cycle["closed_source"],
        "rubric": cycle["rubric_version"],
        "notes": notes.split("\n\n") if notes else [],
        "answers": _answers(_decision_rows(rows), ""),
    }


def close_triage(
    engine: sa.Engine, cycle: int | str, *, source: str, notes=None, project: str | None = None,
) -> dict:
    """Close an open cycle with the owner's answers →
    {cycle_id, closed_at, closed: CLOSED-payload}. `cycle` = id, or code
    with `project` (resolve_cycle).

    Refused while any live finding is undecided (the message lists them); a
    'discuss' decision counts as decided (recorded as open, owner rule
    2026-09-28). `source` = where the answers are recorded (CLOSED.source);
    `notes` = a string or a list of paragraphs (CLOSED.notes). Locks: the
    cycle row.
    """
    where = "close_triage"
    source = _required_text(source, "source", where)
    if notes is None:
        notes_text = None
    elif isinstance(notes, str):
        notes_text = notes or None
    elif isinstance(notes, list) and all(isinstance(n, str) for n in notes):
        notes_text = "\n\n".join(notes) or None
    else:
        raise ValueError(f"{where}: notes must be a string or a list of strings")
    ensure_fits(triage_cycles, {"closed_source": source}, {"closed_source": "source"})
    now = _now()
    with engine.begin() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        cycle_row = _lock_open_cycle(conn, cycle_id)
        undecided = _undecided(conn, cycle_id)
        if undecided:
            raise ValueError(
                f"Cannot close triage cycle {cycle_row['code']}: {len(undecided)} finding(s) "
                "undecided — " + ", ".join(f"K-{s}" for s in undecided)
                + " (record the owner's answer, 'discuss' for one left open)")
        conn.execute(triage_cycles.update().where(triage_cycles.c.id == cycle_id).values(
            closed_at=now, closed_source=source, closed_notes=notes_text, updated_at=now))
        closed_cycle = _cycle_row(conn, cycle_id)
        payload = _closed_payload(closed_cycle, _live_findings(conn, cycle_id))
    return {"cycle_id": cycle_id, "closed_at": _json_safe(closed_cycle["closed_at"]),
            "closed": payload}


# ── promote ──────────────────────────────────────────────────────────────────


def _promotion_priority(row: Mapping) -> str:
    """Rubric §4 outcome table: Fix now unfinished → P1; Verify not
    confirmed → P1 if it is Fix now once confirmed, else P2 (the template's
    prio); Backlog → P2 (S≥2) or P3. Only PROMOTABLE_DECISIONS get here."""
    if row["decision"] == "fix":
        return "P1"
    return rubric.priority(_card_fields(row), row["decision"])


def _promotion_description(
    row: Mapping, cycle_code: str, options: list[dict], unconfirmed: Mapping | None,
) -> str:
    parts = [
        f"Triage {cycle_code} K-{row['seq']} (finding #{row['id']}), source: {row['reviewer']}",
        f"Fingerprint: {row['fingerprint']}",
    ]
    if row["area"]:
        parts.append(f"Where: {row['area']}")
    if row["context"]:
        parts.append(row["context"])
    chosen = next((o for o in options if o["key"] == row["decision_opt"]), None)
    if chosen is not None:
        line = f"Chosen option ({chosen['key']}): {chosen['text']}"
        if chosen["why"]:
            line += f" — {chosen['why']}"
        parts.append(line)
    if row["decision_note"]:
        parts.append(f"Owner's comment: {row['decision_note']}")
    if unconfirmed is not None:
        parts.append("Verification attempted: NOT reproduced (unconfirmed).")
        parts.append(f"Conditions checked: {unconfirmed['checked_conditions']}")
        parts.append(f"Reopen when: {unconfirmed['reopen_condition']}")
    return "\n\n".join(parts)


def _unconfirmed_result(unconfirmed, checked_conditions, reopen_condition) -> dict | None:
    """The explicit result of a failed verification attempt (K-12), or None.
    `unreproduced` is never inferred from the decision: 'verify' orders a
    check, it does not report one."""
    where = "promote_finding_to_item"
    if not isinstance(unconfirmed, bool):
        raise ValueError(f"{where}: unconfirmed must be true or false, got {unconfirmed!r}")  # noqa: TRY004 — every refusal is a ValueError
    if not unconfirmed:
        if checked_conditions is not None or reopen_condition is not None:
            raise ValueError(
                f"{where}: checked_conditions / reopen_condition describe an unconfirmed "
                "verification — pass them with unconfirmed=true")
        return None
    return {
        "checked_conditions": _required_text(
            checked_conditions, "checked_conditions (what the attempt checked)", where),
        "reopen_condition": _required_text(
            reopen_condition, "reopen_condition (when to reopen)", where),
    }


def promote_finding_to_item(
    engine: sa.Engine,
    finding_id: int,
    *,
    item_type: str = "issue",
    priority: str | None = None,
    component: str | None = None,
    unconfirmed: bool = False,
    checked_conditions: str | None = None,
    reopen_condition: str | None = None,
) -> dict:
    """Create an item from a decided finding and back-link it
    (findings.item_id) in ONE transaction → the created item's dict
    (create_item shape).

    Only a finding decided fix / verify / backlog is promoted; undecided,
    discuss, reject and note are refused (K-2), as are a missing finding and
    one already promoted. The item is created through create_item_conn —
    the item-creation path shared with create_item and the plan
    promotions (K-17 / K-20): a soft-deleted project, a bad
    item_type / priority or an over-long component is refused there, with
    no item and no link. Project = the finding's cycle's project; title =
    the finding's title; description = cycle + K-n, fingerprint, where,
    context, the chosen option and the owner's comment. `priority` None =
    the rubric outcome (fix → P1; verify → P1 when Fix now once confirmed,
    else P2; backlog → P2 for S≥2, else P3). Tags (from the tag
    dictionary, no similarity bypass): `security` for a security finding;
    `unreproduced` ONLY with `unconfirmed=True` — the explicit result of a
    verification attempt that did not reproduce a 'verify' finding — which
    requires `checked_conditions` and `reopen_condition` (both go into the
    description). Locks: the finding's cycle id is read unlocked, then the
    cycle row and the finding row are locked (cycle → finding; the cycle is
    decide_findings' first lock too), so a promotion and a re-decision of
    one finding serialise and each checks the other's committed state. The
    project row is NOT locked: the item INSERT takes only FOR KEY SHARE on
    it, which a recorder's NO KEY UPDATE lock on that row does not block
    (K-21). An adapter's optional `project` check runs before this
    transaction, not inside it.
    """
    finding_id = _int_in_range(finding_id, "finding_id", INT_MAX, "promote_finding_to_item")
    unconfirmed_result = _unconfirmed_result(unconfirmed, checked_conditions, reopen_condition)
    now = _now()
    with engine.begin() as conn:
        cycle_id = conn.execute(
            sa.select(findings.c.cycle_id).where(
                findings.c.id == finding_id, findings.c.deleted_at.is_(None))
        ).scalar()
        # Serialises with decide_findings (cycle row) and with a second
        # promotion of the same finding (finding row): one item, never two.
        if (cycle_id is None or not lock_cycle_row(conn, cycle_id)
                or not lock_row(conn, findings, finding_id)):
            raise ValueError(f"Finding #{finding_id} not found")
        row = conn.execute(
            sa.select(findings, triage_cycles.c.code.label("cycle_code"),
                      triage_cycles.c.project_id)
            .select_from(findings.join(triage_cycles, triage_cycles.c.id == findings.c.cycle_id))
            .where(findings.c.id == finding_id, triage_cycles.c.deleted_at.is_(None))
        ).mappings().first()
        if row is None:
            raise ValueError(f"Finding #{finding_id} not found")
        if row["item_id"] is not None:
            raise ValueError(
                f"Finding #{finding_id} is already promoted to item #{row['item_id']}")
        if row["decision"] not in PROMOTABLE_DECISIONS:
            state = "undecided" if row["decision"] is None else f"decided '{row['decision']}'"
            raise ValueError(
                f"Finding #{finding_id} (K-{row['seq']}) is {state} — only a finding decided "
                f"{' / '.join(PROMOTABLE_DECISIONS)} becomes an item")
        if unconfirmed_result is not None and row["decision"] != "verify":
            raise ValueError(
                f"Finding #{finding_id} is decided '{row['decision']}' — unconfirmed "
                "(unreproduced) applies to a 'verify' finding only")
        options = _live_options(conn, [finding_id])[finding_id]
        tags = []
        if row["finding_type"] == "security":
            tags.append("security")
        if unconfirmed_result is not None:
            tags.append("unreproduced")
        item = create_item_conn(
            conn, row["project_id"], item_type, row["title"],
            description=_promotion_description(
                row, row["cycle_code"], options, unconfirmed_result),
            priority=priority or _promotion_priority(row),
            component=component,
            tags=tags,
            now=now,
        )
        conn.execute(findings.update().where(findings.c.id == finding_id).values(
            item_id=item["id"], updated_at=now))
        return item


# ── reads ────────────────────────────────────────────────────────────────────


def _o_value(o: str):
    return o if o == "?" else int(o)


def _brief(row: Mapping) -> dict:
    return {
        "id": row["id"], "cycle_code": row["cycle_code"], "seq": row["seq"],
        "S": row["impact"], "O": _o_value(row["reachability"]), "E": row["evidence"],
        "K": row["fix_cost"], "origin": row["origin"], "type": row["finding_type"],
        "class": _effective_class(row), "decision": row["decision"],
        "decision_opt": row["decision_opt"], "item_id": row["item_id"],
        "first_seen_id": row["first_seen_id"], "source": row["reviewer"],
        "title": row["title"],
    }


_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = _ASCII_UPPER.lower()
_ASCII_FOLD = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)


def _ascii_lower_sql(expr, dialect_name: str):
    """`expr` with ASCII A-Z folded to a-z and nothing else (K-7) — the same
    fold on both backends: SQLite's built-in lower() folds ASCII only;
    PostgreSQL's lower() would fold all of Unicode, so translate() there."""
    if dialect_name == "postgresql":
        return sa.func.translate(expr, _ASCII_UPPER, _ASCII_LOWER, type_=sa.String())
    return sa.func.lower(expr)


def _project_findings_query(project_id: int):
    return (
        sa.select(findings, triage_cycles.c.code.label("cycle_code"),
                  triage_cycles.c.opened_at)
        .select_from(findings.join(triage_cycles, triage_cycles.c.id == findings.c.cycle_id))
        .where(
            triage_cycles.c.project_id == project_id,
            triage_cycles.c.deleted_at.is_(None),
            findings.c.deleted_at.is_(None),
        )
    )


def list_findings(
    engine: sa.Engine,
    project: str,
    *,
    cycle: str | int | None = None,
    fingerprint: str | None = None,
    file: str | None = None,
    cls: str | None = None,
    decision: str | None = None,
    source: str | None = None,
    reviewer: str | None = None,
    limit: int = 50,
) -> dict:
    """The project's live findings as a brief {cols, rows} table (FINDING_COLS),
    newest cycle first, then by seq. Filters: cycle (resolve_cycle: an int
    id, which must belong to the project, or a code; an unknown cycle is
    refused), fingerprint (matched on the normalised form), file (a
    substring of area, case-insensitive for ASCII letters only — other
    characters match exactly, the same on SQLite and PostgreSQL; % and _
    are literal), cls (effective class: override or derived), decision,
    source (the free-text src, exact), reviewer (#930 §2a: findings
    carrying this canonical reviewer token — lowercased and validated like a
    recorded token, an invalid one refused). limit 0 = no limit."""
    if cls is not None:
        _enum(cls, rubric.CLASS_ORDER, "cls", "list_findings")
    if decision is not None:
        _enum(decision, rubric.DECISIONS, "decision", "list_findings")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("list_findings: limit must be a non-negative integer")
    if reviewer is not None:
        reviewer = reviewer_token(reviewer, "list_findings")
    with engine.connect() as conn:
        q = _project_findings_query(_project_id(conn, project))
        if cycle is not None:
            q = q.where(triage_cycles.c.id == _resolve_cycle(conn, project, cycle)[0])
        if fingerprint is not None:
            q = q.where(findings.c.fingerprint_norm == normalize_fingerprint(fingerprint))
        if file:
            q = q.where(_ascii_lower_sql(findings.c.area, conn.dialect.name).contains(
                file.translate(_ASCII_FOLD), autoescape=True))
        if cls is not None:
            q = q.where(sa.func.coalesce(findings.c.class_override, findings.c.class_rec) == cls)
        if decision is not None:
            q = q.where(findings.c.decision == decision)
        if source is not None:
            q = q.where(findings.c.reviewer == source)
        if reviewer is not None:
            q = q.where(sa.exists().where(
                finding_reviewers.c.finding_id == findings.c.id,
                finding_reviewers.c.reviewer == reviewer,
                finding_reviewers.c.deleted_at.is_(None),
            ))
        q = q.order_by(triage_cycles.c.id.desc(), findings.c.seq)
        if limit:
            q = q.limit(limit)
        rows = [_brief(r) for r in conn.execute(q).mappings()]
    return _to_cols_rows(rows, FINDING_COLS)


def finding_history(engine: sa.Engine, project: str, fingerprint: str) -> dict:
    """Every live finding of the project with this (normalised) fingerprint,
    chronologically across cycles — the recurrence / regression view →
    {cols: HISTORY_COLS, rows}."""
    fingerprint = _required_text(fingerprint, "fingerprint", "finding_history")
    with engine.connect() as conn:
        q = (_project_findings_query(_project_id(conn, project))
             .where(findings.c.fingerprint_norm == normalize_fingerprint(fingerprint))
             .order_by(triage_cycles.c.opened_at, findings.c.id))
        rows = [{"id": r["id"], "cycle_code": r["cycle_code"], "opened_at": r["opened_at"],
                 "seq": r["seq"], "decision": r["decision"],
                 "decision_opt": r["decision_opt"], "item_id": r["item_id"]}
                for r in conn.execute(q).mappings()]
    return _to_cols_rows(rows, HISTORY_COLS)


def reviewer_precision(engine: sa.Engine, project: str, *, since: str | None = None) -> dict:
    """Decided findings counted per reviewer token and decision →
    {cols: [reviewer, decision, n], rows} (#930 §2a). A finding with two
    reviewer tokens counts once for each; a decided finding without tokens
    counts under UNATTRIBUTED ("(unattributed)"), so nothing is silently
    dropped — the sum of n can therefore exceed the decided findings.
    `since` limits the count to the cohort of findings RECORDED since an
    anchor — findings.created_at, not decided_at (the snapshot vocabulary:
    last_release | all | <N>h | ISO date); None = all time. Undecided
    findings are not counted."""
    with engine.connect() as conn:
        project_id = _project_id(conn, project)
        anchor = None
        if since is not None:
            anchor, _label = _parse_since_conn(conn, project_id, since)
        # Grouped on the raw token (NULL = no reviewer row) and named in
        # Python: a bound literal in a coalesce() would be two different
        # parameters in SELECT and GROUP BY on PostgreSQL; the sort is done
        # here too, so NULL ordering does not differ between backends.
        q = (
            sa.select(finding_reviewers.c.reviewer, findings.c.decision,
                      sa.func.count().label("n"))
            .select_from(
                findings.join(triage_cycles, triage_cycles.c.id == findings.c.cycle_id)
                .outerjoin(finding_reviewers, sa.and_(
                    finding_reviewers.c.finding_id == findings.c.id,
                    finding_reviewers.c.deleted_at.is_(None))))
            .where(
                triage_cycles.c.project_id == project_id,
                triage_cycles.c.deleted_at.is_(None),
                findings.c.deleted_at.is_(None),
                findings.c.decision.is_not(None),
            )
            .group_by(finding_reviewers.c.reviewer, findings.c.decision)
        )
        if anchor is not None:
            q = q.where(findings.c.created_at >= anchor)
        rows = sorted(
            ({"reviewer": r.reviewer or UNATTRIBUTED, "decision": r.decision, "n": r.n}
             for r in conn.execute(q)),
            key=lambda r: (r["reviewer"], r["decision"]))
    return _to_cols_rows(rows, PRECISION_COLS)


# ── suggestion digest (#930 K-32) ────────────────────────────────────────────

# The card fields a proposal is judged against: what the owner reads on the
# page next to it. Options: key, text, why, recommended, root.
_DIGEST_CARD_FIELDS = ("id", "title", "fp", "S", "O", "E", "K", "origin", "type")


def suggestion_digest(cycle_id: int, cards: list[dict], suggested_answers: dict) -> str:
    """The canonical hash of the proposals a reader saw → sha256 hex — the
    ONE implementation shared by export_triage (SUGGESTED.digest) and
    accept_suggestions(expected_digest=…) (K-32).

    Input: the cycle id; the cycle's CARDS in page order (export_triage's
    cards — id, title, fp, S / O / E / K / origin / type and every option's
    k / t / why / rec / root count; other keys are ignored); and per card
    with a proposal {"K-<n>": {dec, opt, note, by}} (opt / note None and ""
    alike; by = THAT card's proposer). Canonical JSON (sorted keys, no
    whitespace, UTF-8) of all three, so any change to a proposal, its
    author, a card's rubric fields or wording, an option, or the card set
    itself yields another digest — a revision by the same author included."""
    payload = {
        "v": 1,
        "cycle": int(cycle_id),
        "cards": [
            {**{f: c.get(f) for f in _DIGEST_CARD_FIELDS},
             "opts": [{"k": o.get("k"), "t": o.get("t"), "why": o.get("why") or "",
                       "rec": bool(o.get("rec")), "root": bool(o.get("root"))}
                      for o in c.get("opts") or []]}
            for c in cards
        ],
        "answers": {
            str(key): {"dec": a.get("dec"), "opt": a.get("opt") or "",
                       "note": a.get("note") or "", "by": a.get("by")}
            for key, a in suggested_answers.items()
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest_answers(rows: list[Mapping]) -> dict:
    """suggestion_digest's per-card proposals of the findings rows."""
    return {
        f"K-{r['seq']}": {"dec": r["suggested_decision"], "opt": r["suggested_opt"],
                          "note": r["suggested_note"], "by": r["suggested_by"]}
        for r in rows if r["suggested_decision"] is not None
    }


def _rows_digest(cycle_id: int, rows: list[Mapping], options: dict[int, list[dict]]) -> str:
    """suggestion_digest of a cycle's live findings rows (by seq) and their
    options — what export_triage publishes and accept_suggestions recomputes."""
    return suggestion_digest(
        cycle_id, [_card(r, options[r["id"]], []) for r in rows], _digest_answers(rows))


# ── export ───────────────────────────────────────────────────────────────────


def _live_reviewers(conn: sa.Connection, finding_ids: list[int]) -> dict[int, list[str]]:
    """The live reviewer tokens per finding, in recorded order."""
    out: dict[int, list[str]] = {fid: [] for fid in finding_ids}
    if not finding_ids:
        return out
    rows = conn.execute(
        sa.select(finding_reviewers.c.finding_id, finding_reviewers.c.reviewer).where(
            finding_reviewers.c.finding_id.in_(finding_ids),
            finding_reviewers.c.deleted_at.is_(None),
        ).order_by(finding_reviewers.c.finding_id, finding_reviewers.c.id)
    )
    for r in rows:
        out[r.finding_id].append(r.reviewer)
    return out


def _card(row: Mapping, options: list[dict], reviewers: list[str]) -> dict:
    """One template CARD — field names exactly as the template's CARDS, plus
    `reviewers` (#930 §2a, the canonical tokens) when the finding has any;
    like the optional flags it is absent otherwise, so an exported CARD
    records back unchanged (record_findings refuses an empty list)."""
    card = {
        "id": f"K-{row['seq']}",
        "title": row["title"],
        "area": row["area"] or "",
        "src": row["reviewer"],
        **({"reviewers": list(reviewers)} if reviewers else {}),
        **{k: v for k, v in _card_fields(row).items()
           if k in ("S", "O", "E", "K", "origin", "type")},
        "fp": row["fingerprint"],
        "ctx": row["context"].split("\n\n") if row["context"] else [],
        "opts": [{"k": o["key"], "rec": bool(o["is_recommended"]), "root": bool(o["is_root"]),
                  "t": o["text"], "why": o["why"] or ""} for o in options],
    }
    for flag, value in _card_fields(row).items():
        if flag in ("conflict", "violation", "dispute", "openDesign"):
            card[flag] = value
    if row["class_override"]:
        card["cls"] = row["class_override"]
        card["clsWhy"] = row["class_reason"]
    return card


def _suggested_payload(rows: list[Mapping], digest: str) -> dict | None:
    with_suggestion = [r for r in rows if r["suggested_decision"] is not None]
    if not with_suggestion:
        return None
    latest = max(with_suggestion, key=lambda r: (r["suggested_at"], r["id"]))
    return {
        "by": latest["suggested_by"],
        "digest": digest,
        "date": latest["suggested_at"].strftime("%Y-%m-%d"),
        "answers": _answers(
            [{"seq": r["seq"], "suggested_decision": r["suggested_decision"],
              "suggested_opt": r["suggested_opt"], "suggested_note": r["suggested_note"]}
             for r in with_suggestion], "suggested_"),
    }


def export_triage(engine: sa.Engine, cycle: int | str, *, project: str | None = None) -> dict:
    """The page constants of a cycle → {cycle, cards, closed, suggested}.

    `cycle` = id, or code with `project` (resolve_cycle). cards = the
    template's CARDS (by seq; `reviewers` — the canonical tokens — next to
    `src` when the finding has any); closed = its CLOSED when the cycle is closed,
    else None; suggested = its SUGGESTED {by, digest, date, answers} when any
    finding carries a suggestion (by / date of the latest; digest =
    suggestion_digest of the cards and every proposal with its author —
    the value accept_suggestions(expected_digest=…) takes back), else None. The
    values are the stored texts, unescaped; embed them in a page only
    through page_constants_json (K-1).
    """
    with engine.connect() as conn:
        cycle_id, _project = _resolve_cycle(conn, project, cycle)
        cycle = _cycle_row(conn, cycle_id)
        slug = conn.execute(
            sa.select(projects.c.slug).where(projects.c.id == cycle["project_id"])
        ).scalar_one()
        rows = _live_findings(conn, cycle_id)
        options = _live_options(conn, [r["id"] for r in rows])
        reviewers = _live_reviewers(conn, [r["id"] for r in rows])
    cycle_out = {
        "id": cycle["id"], "project": slug, "code": cycle["code"], "topic": cycle["topic"],
        "scope": cycle["scope"], "base_commit": cycle["base_commit"],
        "rubric_version": cycle["rubric_version"], "artifact_url": cycle["artifact_url"],
        "created_by": cycle["created_by"], "opened_at": cycle["opened_at"],
        "closed_at": cycle["closed_at"],
    }
    return {
        "cycle": _json_safe(cycle_out),
        "cards": [_card(r, options[r["id"]], reviewers[r["id"]]) for r in rows],
        "closed": _closed_payload(cycle, rows) if cycle["closed_at"] is not None else None,
        "suggested": _suggested_payload(rows, _rows_digest(cycle_id, rows, options)),
    }
