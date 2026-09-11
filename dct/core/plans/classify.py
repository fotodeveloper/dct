"""Rules-first plan classification with an optional configured command.

Unknown headings remain uncertain unless the user explicitly enables a command.
The command receives the classification prompt as its final argument, with no
shell interpolation. Legacy Claude commands additionally receive --model.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from dct.core.plans.parser import classify_heading

VALID_SECTION_TYPES: frozenset[str] = frozenset({
    "sprint", "phase", "task", "block", "milestone",
    "decision", "prose", "changelog", "adr_section",
    "reference", "generic",
})

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_SECONDS = 30


def _build_prompt(heading: str, parent_heading: str | None, preview: str) -> str:
    """Assemble the classification prompt sent to `claude --print`."""
    labels = ", ".join(sorted(VALID_SECTION_TYPES))
    parts = [
        "Classify this plan section.",
        f"Heading: {heading!r}.",
    ]
    if parent_heading:
        parts.append(f"Parent heading: {parent_heading!r}.")
    if preview:
        parts.append(f"First line: {preview!r}.")
    parts.append(f"Respond with exactly one label from: {labels}.")
    parts.append("Respond with just the label — no prose, no quotes, no markdown.")
    return " ".join(parts)


def _sanitize_response(raw: str) -> str | None:
    """Normalize model output and return a valid section_type or None.

    Handles: whitespace, trailing newlines, wrapping quotes/backticks,
    A response containing explanations or multiple labels is rejected.
    """
    stripped = raw.strip().strip("`\"' ")
    if not stripped:
        return None
    label = stripped.lower()
    return label if label in VALID_SECTION_TYPES else None


def classify_uncertain(
    heading: str,
    parent_heading: str | None = None,
    preview: str = "",
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    provider: str = "rules",
    command: list[str] | None = None,
) -> str | None:
    """Reuse parser rules, optionally enriching unknown headings via a command.

    Rules are authoritative. With provider='rules' (default), unknown headings
    return None. 'disabled' and unknown providers never execute commands.
    For provider='command', argv is preserved and the prompt is appended; only
    a Claude executable receives legacy --model flags (unless already supplied).
    Missing commands, invalid output, timeouts and process errors return None.
    """
    if provider not in {"rules", "command"}:
        return None
    label, confidence = classify_heading(heading)
    if confidence == "high" and label in VALID_SECTION_TYPES:
        return label
    if provider != "command":
        return None
    argv = ["claude", "--print"] if command is None else command
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(part, str) and part and "\0" not in part for part in argv)):
        return None
    if not shutil.which(argv[0]):
        return None
    argv = list(argv)
    if (Path(argv[0]).name.lower() in {"claude", "claude.exe", "claude.cmd"}
            and "--model" not in argv and not any(part.startswith("--model=") for part in argv)):
        argv.extend(["--model", model])
    argv.append(_build_prompt(heading, parent_heading, preview))
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return _sanitize_response(result.stdout)
