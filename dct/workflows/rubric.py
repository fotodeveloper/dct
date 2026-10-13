"""Review rubric v2 §4 — the class rule of the triage ledger (#909).

ONE Python port of the triage page template's `classify()` and of its
DECISIONS / CLASSES / CLASS_ORDER constants. The template is the reference
implementation the owner reads on every triage page; this module must give
the same class for the same fields, so the class the ledger stores
(`findings.class_rec`) is the class the page shows. Keep both in sync: a
change of the rule is a change of the rubric, never a local tweak.

Inputs use the template's CARD keys (S, O, E, K, type and the §4 flags
conflict / violation / dispute / openDesign); O is an int 0-3 or the string
'?' (unresolved). First matching rule wins.
"""

from __future__ import annotations

from collections.abc import Mapping

# The owner's decisions, in the page's order (template DECISIONS).
DECISIONS: tuple[str, ...] = ("fix", "verify", "backlog", "reject", "note", "discuss")

# Rubric §4 classes in display order; `dec` = the decision the class
# recommends (template CLASSES).
CLASSES: dict[str, dict[str, str]] = {
    "fix": {"label": "Fix now", "dec": "fix"},
    "verify": {"label": "Verify", "dec": "verify"},
    "quick": {"label": "Quick win", "dec": "fix"},
    "decide": {"label": "Decide", "dec": "discuss"},
    "backlog": {"label": "Backlog", "dec": "backlog"},
    "reject": {"label": "Reject", "dec": "reject"},
    "note": {"label": "Note", "dec": "note"},
}
CLASS_ORDER: tuple[str, ...] = tuple(CLASSES)

# Rubric §1 value domains.
IMPACT_LEVELS = (0, 1, 2, 3)
REACHABILITY_LEVELS = ("0", "1", "2", "3", "?")
EVIDENCE_LEVELS = (0, 1, 2, 3)
FIX_COSTS = ("S", "M", "L")
ORIGINS = ("diff", "pre", "latent")
FINDING_TYPES = (
    "security", "spec", "spec-gap", "correctness", "robustness", "perf",
    "maintainability", "opinion",
)


def classify(card: Mapping) -> str:
    """The rubric §4 class of one finding — first matching rule wins.

    `card` carries S, O, E, K, type and optionally conflict, violation,
    dispute, openDesign (and the template's `defect`, never stored by the
    ledger: absent means a real defect). Returns one CLASSES key.
    """
    s, e, k = card["S"], card["E"], card["K"]
    o_unresolved = card["O"] == "?"
    o = -1 if o_unresolved else int(card["O"])
    ftype = card.get("type")
    if ftype == "spec-gap" or card.get("conflict"):  # 1
        return "decide"
    if ftype == "opinion":  # 2
        return "note"
    if o == 0 or card.get("violation") is False:  # 3
        return "reject"
    if card.get("dispute"):  # 4
        return "verify"
    if s >= 2 and (e < 2 or o_unresolved):  # 5
        return "verify"
    if e >= 2 and ((s == 3 and o >= 1) or (s == 2 and o >= 2)):  # 6
        return "fix"
    if (e >= 2 and o >= 1 and k == "S" and card.get("defect") is not False
            and not card.get("openDesign")):  # 7
        return "quick"
    if s >= 2:  # 8 → P2
        return "backlog"
    if s == 1 and o >= 2 and e >= 2:  # 9 → P3
        return "backlog"
    return "note"  # 10


def priority(card: Mapping, cls: str) -> str:
    """The template's prio(): the dct priority a Backlog / Verify class
    implies ('' for every other class). `cls` is the effective class
    (override or derived)."""
    s, o = card["S"], card["O"]
    if cls == "backlog":
        return "P2" if s >= 2 else "P3"
    if cls == "verify":
        return "P1" if (s == 3 or (s == 2 and o != "?" and int(o) >= 2)) else "P2"
    return ""
