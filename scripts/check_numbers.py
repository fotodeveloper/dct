#!/usr/bin/env python3
"""Drift check for the numbers dct advertises about itself (item #407).

Every number quoted in README.md and on dctools.dev has a source of truth in
this repo: the version lives in pyproject, the test count comes from pytest,
the tool count from the MCP registry, the skill/hook counts from the packaged
asset dirs. Hand-copying them drifts — a 2026-07-18 audit found six stale
spots (48 vs 49 tools, v1.0.0 vs 1.1.0, ~670 vs 823 tests), and the site was
hand-corrected twice in a row after that.

Two tiers, because the two surfaces move at different speeds:

* in-repo (README) numbers are EXACT — they are edited in the same commit
  that changes them, so anything else is drift;
* published (dctools.dev) numbers that churn are APPROXIMATE — the site is
  deployed once per release, so an exact test count there is stale by the
  next commit. The tilde in "~940 tests" is load-bearing: this script
  requires it and accepts anything within TOLERANCE of the true count.

The site lives in a separate, remote-less repo, so CI can only police README.
Pass --site <dc-tools checkout> at release time to cover the published pages:

    python scripts/check_numbers.py --site ~/dev/dc-tools

Every pattern below is also a presence assertion: a number that disappears
from a page (or a page restructured past recognition) fails just like a wrong
one, so the check can never pass by quietly matching nothing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TOLERANCE = 0.05

# (pattern, quantity, approximate). One capture group each. The site renders
# dcg's "17 tools" beside dct's "49 MCP tools", so site patterns require the
# MCP prefix; only README, which has no dcg section, adds the naked variant.
SITE_COUNTS = [
    (r"(\d+) MCP tools", "tools", False),
    (r"(\d+) skills", "skills", False),
    (r"(\d+) hooks", "hooks", False),
]

README_PATTERNS = [
    (r"(\d+) MCP tools", "tools", False),
    (r"(\d+) tools", "tools", False),
    (r"All tests \((\d+)\)", "tests", False),
]

# Per-file contracts: exactly the numbers that page is expected to render.
SITE_PATTERNS = {
    "public/index.html": SITE_COUNTS
    + [
        (r'"softwareVersion": "([\d.]+)"', "version", False),
        (r">v([\d.]+) · available", "version", False),
        (r"~(\d+) tests", "tests", True),
    ],
    "public/llms.txt": SITE_COUNTS + [(r"dct v([\d.]+) —", "version", False)],
    "public/llms-full.txt": SITE_COUNTS
    + [
        (r"\(v([\d.]+), available\)", "version", False),
        (r"~(\d+) tests", "tests", True),
    ],
}


@dataclass(frozen=True)
class Claim:
    """A number as rendered somewhere, with the quantity it claims to be."""

    path: Path
    line: int
    quantity: str
    rendered: str
    approximate: bool = False

    def verdict(self, derived: dict[str, str]) -> str | None:
        """None when the claim holds; otherwise what the source says."""
        expected = derived[self.quantity]
        if self.rendered == expected:
            return None
        if self.approximate and _within_tolerance(self.rendered, expected):
            return None
        return expected


def _within_tolerance(rendered: str, expected: str) -> bool:
    if not (rendered.isdigit() and expected.isdigit()):
        return False
    return abs(int(rendered) - int(expected)) <= int(expected) * TOLERANCE


def find_claims(text: str, patterns, path: Path) -> list[Claim]:
    """Every rendered number in `text`, with 1-indexed line numbers."""
    claims = []
    for pattern, quantity, approximate in patterns:
        for match in re.finditer(pattern, text):
            line = text.count("\n", 0, match.start()) + 1
            claims.append(Claim(path, line, quantity, match.group(1), approximate))
    return sorted(claims, key=lambda c: (c.line, c.quantity))


def derive(repo: Path) -> dict[str, str]:
    """The numbers as their sources of truth report them."""
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+) tests? collected", collected.stdout)
    if not match:
        sys.exit(f"cannot derive the test count from pytest:\n{collected.stdout[-500:]}")

    from dct.mcp.server import mcp

    return {
        "version": pyproject["project"]["version"],
        "tests": match.group(1),
        "tools": str(len(mcp._tool_manager._tools)),
        "skills": str(sum(1 for p in (repo / "dct/skills").iterdir() if p.is_dir())),
        "hooks": str(len(list((repo / "dct/hooks").glob("*.sh")))),
    }


def check_file(path: Path, patterns, derived: dict[str, str]) -> tuple[int, list[str]]:
    """Returns (claims checked, problems) for one rendered file."""
    if not path.exists():
        return 0, [f"{path}: missing"]

    text = path.read_text(encoding="utf-8")
    problems = [
        f"{path}: nothing matches /{pattern}/ — the {quantity} number vanished "
        f"or the page was restructured"
        for pattern, quantity, _ in patterns
        if not re.search(pattern, text)
    ]

    claims = find_claims(text, patterns, path)
    problems += [
        f"{path}:{c.line}: {c.quantity} rendered as {c.rendered}, source says {expected}"
        for c in claims
        if (expected := c.verdict(derived))
    ]
    return len(claims), problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site",
        type=Path,
        help="dc-tools checkout; without it only README.md is checked",
    )
    args = parser.parse_args()

    derived = derive(REPO)
    print("derived: " + " · ".join(f"{k}={v}" for k, v in sorted(derived.items())))

    targets = [(REPO / "README.md", README_PATTERNS)]
    if args.site:
        targets += [(args.site / rel, pats) for rel, pats in SITE_PATTERNS.items()]

    problems, checked = [], 0
    for path, patterns in targets:
        count, found = check_file(path, patterns, derived)
        checked += count
        problems += found

    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(f"\n{len(problems)} problem(s) in {checked} rendered number(s)", file=sys.stderr)
        return 1

    print(f"{checked} rendered number(s) agree with their sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
