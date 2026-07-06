"""Identifier helpers — ULID generation for plans/sections/checkpoints/decisions.

Why ULID (not UUID4): time-sortable at millisecond granularity (newer rows
sort later naturally in DB), shorter stringified form (26 chars vs 36),
Crockford base32 so no I/L/O/U ambiguity. Strict ordering within one
millisecond is NOT guaranteed — intra-plan ordering uses explicit sort_order
columns instead.
"""

import re

from ulid import ULID

# Crockford base32 alphabet without I, L, O, U; 26 chars total.
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def new_ulid() -> str:
    """Generate a fresh ULID as a 26-char uppercase Crockford-base32 string."""
    return str(ULID())


def is_valid_ulid(value: str) -> bool:
    """True iff `value` has the exact shape of a ULID — 26 Crockford-base32 chars."""
    return bool(ULID_RE.fullmatch(value))
