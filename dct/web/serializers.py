"""Serialization helpers for the dct web viewer.

JSON-native conversions shared by every blueprint:
- datetimes -> ISO 8601 strings,
- the JSON-in-TEXT `tags` column -> a real list,
- markdown prose -> sanitized HTML (raw HTML escaped, never executed).
"""

import json
import re
from datetime import datetime

import markdown as _markdown
import markupsafe


def to_iso(dt) -> str | None:
    """datetime -> ISO 8601 string; None passes through; other types stringified."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def parse_tags(raw) -> list[str]:
    """Tolerant parse of the `tags` field.

    Accepts JSON-encoded TEXT (``'["a","b"]'``), ``None``, an already-decoded
    list, or a bare string. Always returns ``list[str]`` and never raises.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return [s]
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
        return [str(parsed)]
    return []


# Markdown link syntax can emit dangerous-scheme URIs (e.g. `[x](javascript:alert(1))`)
# even though raw HTML is escaped upstream. Rewrite such href/src values to "#" so a
# malicious description/note can't smuggle a javascript:/data: link into the page.
_UNSAFE_URI = re.compile(
    r"""(href|src)\s*=\s*(["'])\s*(?:javascript|vbscript|data|file)\s*:[^"']*\2""",
    re.IGNORECASE,
)


def _neutralize_unsafe_links(html: str) -> str:
    return _UNSAFE_URI.sub(lambda m: f"{m.group(1)}={m.group(2)}#{m.group(2)}", html)


# --- readability: paragraph-split long unstructured prose ------------------
# Many dct descriptions/notes are stored as ONE continuous block (zero newlines)
# and render as an unreadable wall. When a block has no paragraph breaks and no
# markdown block syntax, we split it on sentence boundaries and regroup so the
# renderer emits real <p> breaks. Conservative: structured/short text is left alone.
_ABBREV = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "al.", "inc.", "approx.", "no.", "fig.")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(`])")
_BLOCK_MD = re.compile(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)")


def _paragraphize(text: str, group: int = 2) -> str:
    if "\n\n" in text or _BLOCK_MD.search(text) or len(text) <= 300:
        return text
    parts = _SENTENCE_SPLIT.split(text.strip())
    sentences: list[str] = []
    for part in parts:
        if sentences and sentences[-1].rstrip().lower().endswith(_ABBREV):
            sentences[-1] = f"{sentences[-1]} {part}"
        else:
            sentences.append(part)
    if len(sentences) < 3:
        return text
    return "\n\n".join(
        " ".join(sentences[i:i + group]) for i in range(0, len(sentences), group)
    )


def render_markdown(text: str | None) -> markupsafe.Markup:
    """Render markdown to HTML with raw HTML escaped (defense-in-depth).

    We escape the source *before* handing it to python-markdown, so any raw
    ``<script>`` / ``<img onerror=...>`` becomes inert text while ordinary
    markdown syntax (emphasis, lists, code fences, tables) still renders.
    Long unstructured blocks are paragraph-split first for readability.
    Returns a ``Markup`` so Jinja2 treats it as safe to emit verbatim.
    """
    if not text:
        return markupsafe.Markup("")
    text = _paragraphize(text)
    escaped = str(markupsafe.escape(text))
    html = _markdown.markdown(
        escaped,
        extensions=["fenced_code", "tables"],
        output_format="html",
    )
    html = _neutralize_unsafe_links(html)
    return markupsafe.Markup(html)
