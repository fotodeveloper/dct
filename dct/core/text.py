"""Free-text helpers shared by every surface.

ONE implementation for the whole product (K-35, #413): the MCP list views, the
session snapshot and the sprint dashboards all compact text here, so a
documented "N-char preview" means the same ceiling everywhere. The item-label
rule (item_label) and the free-text clear sentinel (CLEAR, K-48) live here too.
"""

from __future__ import annotations

PREVIEW_CHARS = 200

# The literal a client passes in a free-text update parameter to NULL a
# nullable field (update_item, update_changelog). ONE constant (K-48): every
# boundary that maps it compares against this name, never a retyped literal.
CLEAR = "__clear__"

# Budget of an item label computed from a title — matches the width of the
# items.short_title column (VARCHAR(50)), so a stored label and a fallback
# label have the same ceiling.
ITEM_LABEL_CHARS = 50


def preview(text: str | None, limit: int = PREVIEW_CHARS) -> str | None:
    """Compact free text to at most `limit` chars, ELLIPSIS INCLUDED.

    The documented "200-char preview" is a hard ceiling: a string of exactly
    `limit` chars comes back untouched (no marker), a longer one becomes
    `limit - 1` chars plus '…' — so len(result) <= limit always. Non-str input
    (None) passes through. Single implementation, shared by the snapshot
    (handoff.others, decision summaries) and the MCP list views (K-35).
    """
    if not isinstance(text, str):
        return text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def item_label(short_title: str | None, title: str | None) -> str | None:
    """The ONE rule for a row that REFERENCES an item (#817, B4).

    Returns `short_title` when it is a string with visible content, otherwise
    the title compacted to ITEM_LABEL_CHARS (ellipsis included). An empty or
    whitespace-only short_title (one stored before K-43 normalized writes)
    counts as missing. None title with no short_title → None.
    """
    if isinstance(short_title, str) and short_title.strip():
        return short_title
    return preview(title, ITEM_LABEL_CHARS)
