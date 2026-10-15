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
# Skills/hooks may carry a "Claude Code " label (K-57: they are CC-only and
# the copy says so) — the prefix is optional so both renderings verify.
SITE_COUNTS = [
    (r"(\d+) MCP tools", "tools", False),
    (r"(\d+) (?:Claude Code )?skills", "skills", False),
    (r"(\d+) (?:Claude Code )?hooks", "hooks", False),
]

# README deliberately renders no volatile number. The test count used to sit in
# a pytest comment, where it told the reader nothing and drifted on every commit
# that added a test — which would have made this gate cry wolf in CI. What is
# left changes only when a tool is added or removed, which is exactly the drift
# worth failing a build over.
README_PATTERNS = [(r"(\d+) (?:MCP tools|tools over stdio)", "tools", False)]

# The generated concise reference carries no volatile MCP total. Its catalog
# is validated by render_skills.py --check against the canonical manifest.

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
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", collected.stdout)
    if not match:
        sys.exit(f"cannot derive the test count from pytest:\n{collected.stdout[-500:]}")

    import asyncio

    from dct.mcp.server import mcp
    from dct.skill_render import skill_names

    return {
        "version": pyproject["project"]["version"],
        "tests": match.group(1),
        "tools": str(len(asyncio.run(mcp.list_tools()))),
        "skills": str(len(skill_names(repo))),
        # A sourced *.lib.sh (K-48) sits beside the hooks but is not one.
        "hooks": str(sum(1 for p in (repo / "dct/hooks").glob("*.sh")
                         if not p.name.endswith(".lib.sh"))),
    }


def render(value: str, approximate: bool) -> str:
    """How a derived number should be written into a page.

    Approximate targets lose their last digits rather than their tilde: the
    "~" belongs to the surrounding prose (and to the pattern), so only the
    digits are ever rewritten.
    """
    return str(int(value) // 10 * 10) if approximate else value


def fix_file(path: Path, patterns, derived: dict[str, str]) -> list[str]:
    """Rewrite drifted numbers in place. Returns one line per rewrite.

    Only the capture group's span is replaced, so every other byte — including
    the rest of the matched line — survives untouched. A pattern that matches
    nothing raises before anything is written: a fix that silently covers only
    part of a page is worse than none.
    """
    text = path.read_text(encoding="utf-8")

    missing = [quantity for pattern, quantity, _ in patterns if not re.search(pattern, text)]
    if missing:
        raise ValueError(
            f"{path}: nothing matches the {', '.join(missing)} pattern(s) — "
            "refusing to write a partial fix"
        )

    rewrites: list[str] = []
    for pattern, quantity, approximate in patterns:
        want = render(derived[quantity], approximate)
        scanned = text

        def swap(match, quantity=quantity, want=want, scanned=scanned):
            found = match.group(1)
            if found == want:
                return match.group(0)
            line = scanned.count("\n", 0, match.start()) + 1
            rewrites.append(f"{path}:{line}: {quantity} {found} → {want}")
            start, end = (offset - match.start() for offset in match.span(1))
            return match.group(0)[:start] + want + match.group(0)[end:]

        text = re.sub(pattern, swap, text)

    if rewrites:
        path.write_text(text, encoding="utf-8")
    return rewrites


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
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite drifted numbers in place, then report what remains",
    )
    args = parser.parse_args()

    derived = derive(REPO)
    print("derived: " + " · ".join(f"{k}={v}" for k, v in sorted(derived.items())))

    targets = [
        (REPO / "README.md", README_PATTERNS),
    ]
    if args.site:
        targets += [(args.site / rel, pats) for rel, pats in SITE_PATTERNS.items()]

    if args.fix:
        try:
            rewrites = [line for path, pats in targets if path.exists() for line in fix_file(path, pats, derived)]
        except ValueError as refusal:
            return sys.exit(str(refusal))
        print("\n".join(rewrites) if rewrites else "nothing to rewrite")

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
