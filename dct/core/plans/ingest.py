"""Ingest pipeline — glue from markdown file to DB state.

Flow per spec §9.1.3:
  1. Read + parse (parser.parse_plan).
  2. Ensure every parsed element has a ULID; fill missing parents.
  3. If any ULID was generated → write file atomically.
  4. Compute sha256 → source_hash.
  5. first-import (no plans row, or imported_at=NULL) vs re-sync.
  6. Upsert plans + sections + checkpoints (parent FK resolution via ulid→id map).
  7. On first import only: respect MD checkbox `[x]` as `status='done'`.
  8. Optional: resolve uncertain sections via the configured provider.
  9. Stamp imported_at on first import; always update source_hash.

Deliberately keeps no global state — all intermediate maps are local.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dct.config import load_config
from dct.core.ids import new_ulid
from dct.core.plans import classify as classify_mod
from dct.core.plans.crud import (
    add_plan_checkpoint,
    add_plan_section,
    create_plan,
    get_plan,
    get_plan_by_ulid,
    get_plan_checkpoint_by_ulid,
    get_plan_section_by_ulid,
    set_plan_imported,
    update_plan,
    update_plan_section,
)
from dct.core.plans.parser import ParseResult, parse_plan, slugify


@dataclass
class IngestReport:
    plan_id: int
    plan_ulid: str
    is_first_import: bool
    source_hash: str
    sections_created: int = 0
    sections_updated: int = 0
    checkpoints_created: int = 0
    uncertain_resolved: int = 0
    uncertain_remaining: int = 0
    ulids_injected: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "plan_ulid": self.plan_ulid,
            "is_first_import": self.is_first_import,
            "source_hash": self.source_hash,
            "sections_created": self.sections_created,
            "sections_updated": self.sections_updated,
            "checkpoints_created": self.checkpoints_created,
            "uncertain_resolved": self.uncertain_resolved,
            "uncertain_remaining": self.uncertain_remaining,
            "ulids_injected": self.ulids_injected,
            "warnings": self.warnings,
        }


# ── ULID + parent fill ────────────────────────────────────────────────────────

def ensure_ulids_and_parents(result: ParseResult) -> bool:
    """Generate missing ULIDs on result in-place; fill missing parents via nesting.

    Returns True iff any ULID was newly generated (caller must write-back).
    """
    changed = False
    if result.plan_ulid is None:
        result.plan_ulid = new_ulid()
        changed = True

    # Section ULIDs first, so parent lookups resolve.
    for s in result.sections:
        if s.ulid is None:
            s.ulid = new_ulid()
            changed = True

    # Parent lookups via depth-stack walk in file order.
    stack: list[tuple[int, str]] = []  # (depth, ulid)
    for s in result.sections:
        while stack and stack[-1][0] >= s.depth:
            stack.pop()
        if s.parent_ulid is None and stack:
            s.parent_ulid = stack[-1][1]
            changed = True
        stack.append((s.depth, s.ulid))  # type: ignore[arg-type]

    # Checkpoint ULIDs + parent section ULID from the nearest preceding heading.
    sections_in_order = sorted(result.sections, key=lambda s: s.heading_line)
    for cp in result.checkpoints:
        if cp.ulid is None:
            cp.ulid = new_ulid()
            changed = True
        if cp.parent_section_ulid is None:
            preceding = [s for s in sections_in_order if 0 < s.heading_line < cp.checkbox_line]
            if preceding:
                cp.parent_section_ulid = preceding[-1].ulid
                changed = True

    return changed


# ── Write-back ────────────────────────────────────────────────────────────────

CHECKBOX_PATTERN = re.compile(r"^(?P<prefix>\s*-\s+\[[ xX]\]\s+)(?P<rest>.*)$")


def write_back_anchors(content: str, result: ParseResult) -> str:
    """Return `content` augmented with missing ULID HTML anchors.

    Inserts at these points:
    - Plan anchor at top of file if `result.plan_anchor_line is None`.
    - Section anchor on line AFTER heading_line when `anchor_line is None`.
    - Checkpoint anchor inline (between `[ ]` marker and text) on each checkbox
      line missing an existing cp anchor.

    Idempotent: lines that already carry the anchor are left untouched.
    Preserves trailing newline if the source had one.
    """
    ends_with_newline = content.endswith("\n")
    lines = content.splitlines()

    # --- Checkpoint anchors: replace lines in-place (no line count change). ---
    for cp in result.checkpoints:
        if cp.ulid is None or cp.checkbox_line <= 0 or cp.checkbox_line > len(lines):
            continue
        idx = cp.checkbox_line - 1
        original = lines[idx]
        if "<!-- dct:cp:" in original:
            continue  # already has an anchor
        m = CHECKBOX_PATTERN.match(original)
        if not m:
            continue
        parent_part = f" parent={cp.parent_section_ulid}" if cp.parent_section_ulid else ""
        lines[idx] = (
            f"{m.group('prefix')}<!-- dct:cp:{cp.ulid}{parent_part} --> "
            f"{m.group('rest').lstrip()}"
        )

    # --- Section anchors: INSERT new line after heading_line (descending, so
    #     earlier insertions don't shift later line numbers). Collect first.
    insertions: list[tuple[int, str]] = []  # (insert_at_0based, line)
    for s in result.sections:
        if s.ulid is None or s.anchor_line is not None:
            continue
        parent_part = f" parent={s.parent_ulid}" if s.parent_ulid else ""
        line = f"<!-- dct:section:{s.ulid}{parent_part} -->"
        # heading_line is 1-based → insert AFTER it → 0-based index = heading_line
        insertions.append((s.heading_line, line))

    # --- Plan anchor at top of file if missing. ---
    if result.plan_anchor_line is None and result.plan_ulid:
        insertions.append((0, f"<!-- dct:plan:{result.plan_ulid} -->"))

    # Apply insertions in DESCENDING order of target position.
    insertions.sort(key=lambda x: x[0], reverse=True)
    for at_idx, new_line in insertions:
        lines.insert(at_idx, new_line)

    output = "\n".join(lines)
    if ends_with_newline and not output.endswith("\n"):
        output += "\n"
    return output


def write_file_atomic(path: Path, content: str) -> None:
    """Atomic write: temp file in same dir, then os.replace."""
    tmp = path.with_suffix(path.suffix + ".dct-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ── Kind inference from path ──────────────────────────────────────────────────

def infer_kind_from_path(path: Path) -> str:
    """Map a filesystem path to a plan `kind`.

    Order matters — specific dirs first, then filename-based heuristics, then
    a conservative default of 'plan'.
    """
    parts = [p.lower() for p in path.parts]
    name = path.name.lower()

    if "adr" in parts or name.startswith("adr-"):
        return "adr"
    if "specs" in parts or name.endswith("-design.md") or "design-spec" in name:
        return "spec"
    if "plans" in parts:
        return "plan"
    if "retros" in parts or name.startswith("retro-") or "retrospective" in name:
        return "retro"
    if "roadmap" in name:
        return "roadmap"
    if name in {"todos.md", "future.md"} or "backlog" in name:
        return "backlog"
    return "plan"


# ── Main ingest function ─────────────────────────────────────────────────────

def ingest_file(
    engine,
    file_path: str | Path,
    project_slug: str,
    auto_resolve_uncertain: bool | None = None,
) -> dict:
    """Ingest a markdown plan file into dct.

    `auto_resolve_uncertain`: when True, any section classified as 'uncertain'
    is sent through rules-first `classify_uncertain()`. None = read
    the default from the dct config file's `[plans].auto_resolve_uncertain`
    (default true; `dct config path` shows the file's location).
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Plan file not found: {path}")

    cfg = load_config()
    if auto_resolve_uncertain is None:
        auto_resolve_uncertain = cfg.plans.auto_resolve_uncertain
    if cfg.classification.provider not in {"rules", "command"}:
        auto_resolve_uncertain = False

    content = path.read_text(encoding="utf-8")
    result = parse_plan(content)

    ulids_injected = ensure_ulids_and_parents(result)
    if ulids_injected:
        content = write_back_anchors(content, result)
        write_file_atomic(path, content)
        # `plan_anchor_missing` was a legitimate complaint about the file
        # state at parse time, but we just fixed it by injecting the anchor.
        # Keep the report honest — drop that warning so callers don't see a
        # false "missing" after we've visibly written the anchor to disk.
        result.warnings = [w for w in result.warnings if w != "plan_anchor_missing"]

    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Look up existing plan by ULID (stable across rename / move).
    assert result.plan_ulid is not None  # ensure_ulids guarantees this
    existing = get_plan_by_ulid(engine, result.plan_ulid)
    is_first_import = existing is None or existing.get("imported_at") is None

    # Derive plan title + slug on first creation.
    if existing is None:
        title = _infer_title(result, path)
        slug = _pick_unique_slug(engine, project_slug, slugify(path.stem))
        kind = infer_kind_from_path(path)
        plan = create_plan(
            engine, project_slug, slug=slug, title=title, kind=kind,
            source_file=str(path), source_hash=source_hash, ulid=result.plan_ulid,
        )
    else:
        update_plan(engine, existing["id"], source_hash=source_hash)
        plan = get_plan(engine, existing["id"]) or existing

    report = IngestReport(
        plan_id=plan["id"], plan_ulid=result.plan_ulid,
        is_first_import=is_first_import, source_hash=source_hash,
        ulids_injected=ulids_injected, warnings=list(result.warnings),
    )

    # ── Sections ────────────────────────────────────────────────────────────
    section_ulid_to_id: dict[str, int] = {}
    for s in result.sections:
        assert s.ulid is not None

        # Resolve uncertain if requested.
        section_type = s.section_type
        confidence = s.section_type_confidence
        needs_review = section_type == "uncertain"
        if needs_review and auto_resolve_uncertain:
            resolved = classify_mod.classify_uncertain(
                s.heading,
                parent_heading=_find_parent_heading(s, result.sections),
                preview="",
                model=cfg.classification.model,
                timeout_seconds=cfg.classification.timeout_seconds,
                provider=cfg.classification.provider,
                command=cfg.classification.command,
            )
            if resolved:
                section_type = resolved
                confidence = "high"
                needs_review = False
                report.uncertain_resolved += 1
            else:
                report.uncertain_remaining += 1
        elif needs_review:
            report.uncertain_remaining += 1

        parent_id = (
            section_ulid_to_id.get(s.parent_ulid) if s.parent_ulid else None
        )
        existing_section = get_plan_section_by_ulid(engine, s.ulid)
        if existing_section is None:
            new_section = add_plan_section(
                engine, plan["id"], s.heading, s.depth,
                section_type=section_type,
                section_type_confidence=confidence,
                needs_llm_review=needs_review,
                ulid=s.ulid,
                parent_section_id=parent_id,
                sort_order=s.sort_order,
            )
            section_ulid_to_id[s.ulid] = new_section["id"]
            report.sections_created += 1
        else:
            updates: dict = {}
            if existing_section["heading"] != s.heading:
                updates["heading"] = s.heading
            if existing_section["section_type"] != section_type:
                # Sticky resolved: a row that was previously classified (e.g.
                # by LLM to 'reference', or by a user via /plan-resolve-uncertain)
                # must NOT be downgraded back to 'uncertain' on a later rescan
                # where auto_resolve_uncertain is off or the LLM happens to
                # fail. The parser always re-infers from heading text alone,
                # so its 'uncertain' verdict is weaker than any resolved DB
                # state. Only overwrite when the new classification is itself
                # non-uncertain (i.e. parser found a better match, or the LLM
                # resolved on this run).
                if section_type == "uncertain" and existing_section["section_type"] != "uncertain":
                    pass  # keep the resolved DB state
                else:
                    updates["section_type"] = section_type
                    updates["section_type_confidence"] = confidence
                    updates["needs_llm_review"] = needs_review
            if existing_section["sort_order"] != s.sort_order:
                updates["sort_order"] = s.sort_order
            if updates:
                update_plan_section(engine, existing_section["id"], **updates)
                report.sections_updated += 1
            section_ulid_to_id[s.ulid] = existing_section["id"]

    # ── Checkpoints ─────────────────────────────────────────────────────────
    for cp in result.checkpoints:
        assert cp.ulid is not None
        if cp.parent_section_ulid is None:
            continue  # orphan checkpoint — warning already in result.warnings
        section_id = section_ulid_to_id.get(cp.parent_section_ulid)
        if section_id is None:
            continue  # parent not tracked (shouldn't happen after upsert)
        existing_cp = get_plan_checkpoint_by_ulid(engine, cp.ulid)
        if existing_cp is None:
            initial_status = "pending"
            if is_first_import and cp.initial_state == "done":
                initial_status = "done"
            add_plan_checkpoint(
                engine, section_id, cp.text,
                status=initial_status, ulid=cp.ulid,
                sort_order=cp.sort_order,
            )
            report.checkpoints_created += 1
        # Re-sync path: state is DB-only, MD brackets ignored. We DO NOT
        # update cp.text on re-sync; treating text edit as a new checkpoint
        # would lose state. Future work: add a `dct plan cp rename` command.

    # ── Finalize ────────────────────────────────────────────────────────────
    if is_first_import:
        set_plan_imported(engine, plan["id"], source_hash=source_hash)

    return report.as_dict()


# ── Small helpers ────────────────────────────────────────────────────────────

def _infer_title(result: ParseResult, path: Path) -> str:
    """Prefer the first H1 heading; fall back to the file stem."""
    for s in result.sections:
        if s.depth == 1:
            return s.heading
    return path.stem.replace("-", " ").replace("_", " ").title()


def _pick_unique_slug(engine, project_slug: str, base_slug: str) -> str:
    """Find a slug not already taken in this project.

    Appends `-2`, `-3`, … if needed. Caller is responsible for the race with
    concurrent ingest (unlikely — importer is typically single-invocation).
    """
    from dct.core.plans.crud import get_plan_by_slug
    if not get_plan_by_slug(engine, project_slug, base_slug):
        return base_slug
    i = 2
    while True:
        candidate = f"{base_slug}-{i}"
        if not get_plan_by_slug(engine, project_slug, candidate):
            return candidate
        i += 1


def _find_parent_heading(
    section, sections: list
) -> str | None:
    """Given a ParsedSection, return the heading text of its structural parent."""
    parent_ulid = section.parent_ulid
    if not parent_ulid:
        return None
    for s in sections:
        if s.ulid == parent_ulid:
            return s.heading
    return None
