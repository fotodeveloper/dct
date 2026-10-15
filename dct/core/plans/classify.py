"""Rules-first plan classification with an optional configured command.

Unknown headings remain uncertain unless the user explicitly enables a command.
The command receives the classification prompt as its final argument, with no
shell interpolation. Legacy Claude commands additionally receive --model.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dct.core.plans.parser import classify_heading

VALID_SECTION_TYPES: frozenset[str] = frozenset({
    "sprint", "phase", "task", "block", "milestone",
    "decision", "prose", "changelog", "adr_section",
    "reference", "generic",
})

@dataclass(frozen=True)
class ClassificationResult:
    """Successful provenance, or a non-secret failure reason."""
    section_type: str | None = None
    source: str | None = None
    provider: str | None = None
    model: str | None = None
    reason: str | None = None
    confidence: str = "high"


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


def command_configuration(model: str, command: list[str] | None) -> tuple[list[str], str | None]:
    """Resolve command argv and its actual known model without executing anything.

    Explicit Claude --model flags override the legacy configured default;
    custom commands have unknown model provenance. Return a fresh argv copy.
    """
    argv = ["claude", "--print"] if command is None else command
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(part, str) and part and "\0" not in part for part in argv)):
        raise ValueError("Invalid classification command")
    argv = list(argv)
    known_model = None
    if Path(argv[0]).name.lower() in {"claude", "claude.exe", "claude.cmd"}:
        if "--model" not in argv and not any(part.startswith("--model=") for part in argv):
            argv.extend(["--model", model])
        for index, part in enumerate(argv):
            if part == "--model":
                if index + 1 >= len(argv):
                    raise ValueError("Missing classification command model")
                known_model = argv[index + 1]
            elif part.startswith("--model="):
                known_model = part.partition("=")[2]
    return argv, known_model


def classify_result(
    heading: str,
    parent_heading: str | None = None,
    preview: str = "",
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    provider: str = "rules",
    command: list[str] | None = None,
) -> ClassificationResult:
    """Reuse parser rules, optionally enriching unknown headings via a command.

    Rules are authoritative. With provider='rules' (default), unknown headings
    return a no_rule result. 'disabled' and unknown providers never execute commands.
    For provider='command', argv is preserved and the prompt is appended; only
    a Claude executable receives legacy --model flags (unless already supplied).
    Missing commands, invalid output, timeouts and process errors carry a reason.
    """
    if provider not in {"rules", "command"}:
        return ClassificationResult(reason="disabled")
    label, confidence = classify_heading(heading)
    if confidence == "high" and label in VALID_SECTION_TYPES:
        return ClassificationResult(label, "rules", "rules")
    if provider != "command":
        return ClassificationResult(reason="no_rule")
    try:
        argv, known_model = command_configuration(model, command)
    except ValueError:
        return ClassificationResult(reason="invalid_command")
    if not shutil.which(argv[0]):
        return ClassificationResult(reason="missing_command")
    argv.append(_build_prompt(heading, parent_heading, preview))
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        return ClassificationResult(reason="timeout")
    except (OSError, ValueError):
        return ClassificationResult(reason="command_error")
    if result.returncode != 0:
        return ClassificationResult(reason="command_failed")
    label = _sanitize_response(result.stdout)
    if label is None:
        return ClassificationResult(reason="invalid_output")
    return ClassificationResult(label, "command", "command", known_model)


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
    """Compatibility wrapper preserving the existing label-or-None API."""
    return classify_result(heading, parent_heading, preview, model, timeout_seconds,
                           provider=provider, command=command).section_type
