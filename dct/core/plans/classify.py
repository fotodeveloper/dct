"""LLM classification of uncertain plan sections — headless Claude CLI wrapper.

We do NOT take an Anthropic SDK dependency; instead we delegate to whichever
`claude` binary the user already has in PATH (i.e. Claude Code's CLI). This
reuses the user's credentials, billing, and model configuration with zero
extra setup. Hidden cost: CC prepends its system prompt + CLAUDE.md, adding
a few k input tokens per call. Haiku at current prices makes this noise
($0.001/classification).

If `claude` is not in PATH, the function returns None — callers treat that
as "leave the section uncertain; no classification possible".
"""

from __future__ import annotations

import shutil
import subprocess

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
    accidental multi-word output (we take the first word). Case-insensitive.
    """
    stripped = raw.strip().strip("`\"' ")
    if not stripped:
        return None
    # First "word" — splits on any whitespace including newlines.
    first = stripped.split()[0].strip("`\"',.")
    label = first.lower()
    return label if label in VALID_SECTION_TYPES else None


def classify_uncertain(
    heading: str,
    parent_heading: str | None = None,
    preview: str = "",
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Classify an uncertain plan section via `claude --print` headless mode.

    Returns a label from VALID_SECTION_TYPES, or None if:
    - `claude` binary is not in PATH,
    - the subprocess fails / times out / exits non-zero,
    - the model response cannot be sanitized to a valid label.

    Never raises — the caller treats None as "leave uncertain".
    """
    if not shutil.which("claude"):
        return None

    prompt = _build_prompt(heading, parent_heading, preview)
    try:
        result = subprocess.run(
            ["claude", "--print", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    return _sanitize_response(result.stdout)
