r"""Mechanical markdown parser for dct plans.

Pure — takes file content (str), returns a ParseResult dataclass. No I/O,
no DB, no LLM. Deterministic regex + state machine, code-fence-aware.

Grammar per spec §7:
  1. YAML frontmatter  — skipped for now (reserved for future use).
  2. Plan ULID anchor  — first ``<!-- dct:plan:<ULID> -->`` on any line.
  3. Code fences       — ``` ``` ``` or ``~~~`` open/close; skip rules inside.
  4. Headings          — `^(#{1,6})\s+(.+)$` outside fences.
  5. Section anchor    — next non-empty line after heading may be
                          ``<!-- dct:section:<ULID> parent=<ULID> -->``.
  6. Section type      — regex heuristic on heading text (sprint/task/...).
  7. Checkboxes        — `^(\s*)-\s+\[([ x])\]\s+(.*)$`.
  8. Checkpoint anchor — ``<!-- dct:cp:<ULID> parent=<ULID> -->`` between
                          ``[x/' ']`` and the text.
  9. Checkpoint state  — reported as 'done' / 'pending' based on
                          ``x`` / ``' '``. Callers decide whether to respect
                          or ignore it (first-import vs rescan, §9.3).

Returns all sections/checkpoints even when they lack ULID anchors — missing
ULIDs are reported as `None`, so callers (ingest.py) can generate and
write-back synchronously (§9.1.3 step 2).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal


ULID_PATTERN = r"[0-9A-HJKMNP-TV-Z]{26}"

RE_PLAN_ANCHOR = re.compile(
    rf"<!--\s*dct:plan:(?P<ulid>{ULID_PATTERN})\s*-->"
)
RE_SECTION_ANCHOR = re.compile(
    rf"<!--\s*dct:section:(?P<ulid>{ULID_PATTERN})"
    rf"(?:\s+parent=(?P<parent>{ULID_PATTERN}))?\s*-->"
)
RE_CP_ANCHOR = re.compile(
    rf"<!--\s*dct:cp:(?P<ulid>{ULID_PATTERN})"
    rf"(?:\s+parent=(?P<parent>{ULID_PATTERN}))?\s*-->"
)

RE_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")
RE_CHECKBOX = re.compile(
    r"^(?P<indent>\s*)-\s+\[(?P<state>[ xX])\]\s+(?P<rest>.*)$"
)
RE_FENCE = re.compile(r"^(?P<indent>\s*)(?P<marker>```+|~~~+)(?P<info>.*)$")

# Confidence-high patterns for section_type classification.
# Ordering matters — first match wins. All case-insensitive.
SECTION_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("sprint", re.compile(r"^sprint\s+[\d.]+", re.IGNORECASE)),
    ("phase", re.compile(r"^phase\s+([\d]+|[a-z])\b", re.IGNORECASE)),
    ("task", re.compile(r"^task\s+[\w.-]+", re.IGNORECASE)),
    ("block", re.compile(r"^block\s+[a-z]\d*", re.IGNORECASE)),
    ("milestone", re.compile(r"^milestone\s+[ma-z]\d+", re.IGNORECASE)),
    ("decision", re.compile(r"^adr[- ]\d+", re.IGNORECASE)),
    ("changelog", re.compile(r"^(added|changed|fixed|removed)\s*$", re.IGNORECASE)),
    ("adr_section", re.compile(r"^(context|decision|consequences|alternatives)\s*$", re.IGNORECASE)),
    ("reference", re.compile(r"^(appendix|references?|open\s+questions?)\b", re.IGNORECASE)),
    # Numbered spec/plan headings like "1. Problem", "4.1 Glossary",
    # "9.1.1 Discovery", and "9.1 `/dct-import` — discover". These are
    # narrative chapters (prose) — not sprints, not tasks, not references.
    # Placed last so specific patterns above (sprint/phase/task/etc.) still
    # win when the heading starts with e.g. "1. Sprint Foo" (matches sprint
    # pattern via the `Sprint` word). The trailing `\S` (non-whitespace)
    # requirement excludes lone numbers like "1." with no title while
    # accepting titles that start with backticks, brackets, parens, etc.
    ("prose", re.compile(r"^\d+(\.\d+)*\.?\s+\S", re.IGNORECASE)),
]


@dataclass
class ParsedSection:
    """A heading-anchored section in the plan. `ulid`/`parent_ulid`=None → caller must mint."""

    heading: str
    depth: int
    slug: str
    sort_order: int
    section_type: str
    section_type_confidence: Literal["high", "low"] = "high"
    ulid: str | None = None
    parent_ulid: str | None = None
    # Line positions — 1-based, used by writer to inject ULIDs.
    heading_line: int = -1
    anchor_line: int | None = None  # existing anchor line if present
    source_lines: str | None = None  # "5-87" span; filled on finalize


@dataclass
class ParsedCheckpoint:
    """A `- [ ]` / `- [x]` item under a section."""

    text: str
    initial_state: Literal["pending", "done"]
    sort_order: int
    ulid: str | None = None
    parent_section_ulid: str | None = None
    checkbox_line: int = -1
    source_lines: str | None = None


@dataclass
class ParseResult:
    plan_ulid: str | None = None
    plan_anchor_line: int | None = None  # 1-based line where the plan anchor sits, if any
    sections: list[ParsedSection] = field(default_factory=list)
    checkpoints: list[ParsedCheckpoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def slugify(text: str) -> str:
    """Produce a URL-friendly kebab-case slug from heading text.

    - Strip NFKD combining marks (so `Łąka` → `laka`).
    - Lowercase.
    - Replace non-alphanumeric runs with a single hyphen.
    - Trim edge hyphens; clamp to 200 chars.
    """
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    lower = stripped.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")
    return slug[:200]


def classify_heading(text: str) -> tuple[str, Literal["high", "low"]]:
    """Return (section_type, confidence) for a heading text.

    High-confidence patterns in SECTION_TYPE_RULES win first-match.
    Everything else falls back to ('uncertain', 'low') for LLM review
    downstream (or 'generic' if the caller disables classification).
    """
    for kind, pattern in SECTION_TYPE_RULES:
        if pattern.match(text):
            return kind, "high"
    return "uncertain", "low"


def parse_plan(content: str) -> ParseResult:
    """Parse a markdown plan file body and return structured sections/checkpoints.

    Pure function — no I/O. `content` is the full file content (UTF-8 string).
    Line numbers in the result are 1-based relative to `content`. Thread-safe:
    all state lives in local variables, no module-level mutation.
    """
    result = ParseResult()
    lines = content.splitlines()

    in_fence = False
    fence_marker: str | None = None

    # Pending anchor from a comment that immediately precedes / follows a heading.
    # We attach the NEXT non-empty line's anchor to the heading above.
    last_heading_index: int | None = None  # index into result.sections

    # Stack of (depth, section_index) for parent derivation via nesting.
    heading_stack: list[tuple[int, int]] = []

    # Structural parent derived from nesting — kept local per call (thread-safe).
    section_parent_index: dict[int, int | None] = {}

    section_counter = 0
    checkpoint_counter = 0

    for i, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\n")

        # 1. Fence tracking — toggles open/close; skip most rules inside.
        fence_match = RE_FENCE.match(line)
        if fence_match:
            marker = fence_match.group("marker")[:3]  # normalize to first 3 backticks/tildes
            if not in_fence:
                in_fence = True
                fence_marker = marker
            else:
                # Close only if same fence kind (``` closes ```, not ~~~)
                if fence_marker and marker == fence_marker:
                    in_fence = False
                    fence_marker = None
            continue

        if in_fence:
            continue

        # 2. Plan anchor — first one wins.
        plan_match = RE_PLAN_ANCHOR.search(line)
        if plan_match and result.plan_ulid is None:
            result.plan_ulid = plan_match.group("ulid")
            result.plan_anchor_line = i
            # Don't `continue` — an anchor can share a line with other content,
            # though the convention is its own line. Keep processing heading/etc.

        # 3. Heading?
        heading_match = RE_HEADING.match(line)
        if heading_match:
            hashes = heading_match.group("hashes")
            text = heading_match.group("text").strip()
            depth = len(hashes)
            section_type, confidence = classify_heading(text)
            section = ParsedSection(
                heading=text,
                depth=depth,
                slug=slugify(text),
                sort_order=section_counter * 10,
                section_type=section_type,
                section_type_confidence=confidence,
                heading_line=i,
            )
            section_counter += 1

            # Parent derivation: pop stack until we find a depth < ours.
            while heading_stack and heading_stack[-1][0] >= depth:
                heading_stack.pop()
            # We store ULID parents only after anchors are resolved (§ below);
            # for now, remember the parent section index (for post-pass fill-in).
            parent_section_index = heading_stack[-1][1] if heading_stack else None

            result.sections.append(section)
            current_index = len(result.sections) - 1
            heading_stack.append((depth, current_index))
            last_heading_index = current_index

            # Stash parent index in a local sidecar for the post-pass.
            section_parent_index[current_index] = parent_section_index
            continue

        # 4. Section anchor (HTML comment attached to the most recent heading).
        section_anchor_match = RE_SECTION_ANCHOR.search(line)
        if section_anchor_match and last_heading_index is not None:
            target = result.sections[last_heading_index]
            if target.ulid is None:
                target.ulid = section_anchor_match.group("ulid")
                target.parent_ulid = section_anchor_match.group("parent")
                target.anchor_line = i
            else:
                result.warnings.append(
                    f"duplicate_section_anchor at line {i} for heading "
                    f"'{target.heading}' — first anchor preserved"
                )
            continue

        # 5. Checkbox?
        checkbox_match = RE_CHECKBOX.match(line)
        if checkbox_match:
            state_char = checkbox_match.group("state").lower()
            rest = checkbox_match.group("rest")

            # Checkpoint anchor is embedded inside `rest`, before the text.
            cp_anchor_match = RE_CP_ANCHOR.search(rest)
            if cp_anchor_match:
                cp_ulid = cp_anchor_match.group("ulid")
                cp_parent = cp_anchor_match.group("parent")
                text = rest[cp_anchor_match.end():].strip()
            else:
                cp_ulid = None
                cp_parent = None
                text = rest.strip()

            if last_heading_index is None:
                result.warnings.append(
                    f"orphan_checkpoint at line {i}: no preceding heading"
                )
                # Emit it anyway so the caller can still fix the source.
                parent_section_ulid = None
            else:
                parent_section_ulid = result.sections[last_heading_index].ulid
                # parent_section_ulid may still be None if the section has
                # no anchor yet; the post-pass / writer will fill it in.

            # Cross-check: if the anchor embeds a parent that disagrees with
            # the structural parent, warn (possible cross-plan copy).
            if (
                cp_parent is not None
                and parent_section_ulid is not None
                and cp_parent != parent_section_ulid
            ):
                result.warnings.append(
                    f"parent_mismatch at line {i}: checkpoint parent={cp_parent} "
                    f"but enclosing section={parent_section_ulid}"
                )

            result.checkpoints.append(ParsedCheckpoint(
                text=text,
                initial_state="done" if state_char == "x" else "pending",
                sort_order=checkpoint_counter * 10,
                ulid=cp_ulid,
                parent_section_ulid=cp_parent or parent_section_ulid,
                checkbox_line=i,
            ))
            checkpoint_counter += 1

    # Post-pass: fill `parent_ulid` on sections that have an anchored parent.
    for idx, section in enumerate(result.sections):
        parent_idx = section_parent_index.get(idx)
        if parent_idx is not None:
            parent_section = result.sections[parent_idx]
            # Only set if the anchor itself didn't specify a different parent.
            if section.parent_ulid is None:
                section.parent_ulid = parent_section.ulid  # may still be None

    # Warnings for missing anchors
    if result.plan_ulid is None:
        result.warnings.append("plan_anchor_missing")

    return result
