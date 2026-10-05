"""Drift check for advertised numbers (item #407, scripts/check_numbers.py).

The script is maintainer tooling outside the package, so it is loaded by path.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_numbers.py"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_numbers", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules, which a path-loaded
    # module is absent from until registered.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DERIVED = {"version": "1.2.1", "tests": "946", "tools": "49", "skills": "13", "hooks": "4"}

STATS_LINE = "Stats: 49 MCP tools · 13 skills · 4 hooks · ~940 tests.\n"


@pytest.fixture
def full_page(checker, tmp_path):
    """A llms-full.txt whose every rendered number is correct."""

    def _write(body):
        page = tmp_path / "llms-full.txt"
        page.write_text(body, encoding="utf-8")
        return page, checker.SITE_PATTERNS["public/llms-full.txt"]

    return _write


# --- extraction ---

def test_find_claims_reports_quantity_and_line(checker):
    text = "intro\n\n" + STATS_LINE
    claims = checker.find_claims(text, checker.SITE_COUNTS, Path("llms-full.txt"))

    assert [(c.quantity, c.rendered) for c in claims] == [
        ("hooks", "4"),
        ("skills", "13"),
        ("tools", "49"),
    ]
    assert {c.line for c in claims} == {3}


def test_dcg_naked_tool_count_is_not_read_as_dcts(checker):
    """The site quotes dcg's "17 tools" beside dct's "49 MCP tools"."""
    text = "49 MCP tools, 13 skills, 4 hooks\ndcg: queryable over MCP; 17 tools\n"
    tools = [c.rendered for c in checker.find_claims(text, checker.SITE_COUNTS, Path("x")) if c.quantity == "tools"]

    assert tools == ["49"]


def test_claude_code_labelled_skill_and_hook_counts_still_verify(checker):
    """K-57 renders "13 Claude Code skills · 4 Claude Code hooks" on the site —
    the optional label must not blind the presence assertions."""
    text = "52 MCP tools · 13 Claude Code skills · 4 Claude Code hooks · ~1180 tests\n"
    claims = checker.find_claims(text, checker.SITE_COUNTS, Path("x"))

    assert [(c.quantity, c.rendered) for c in claims] == [
        ("hooks", "4"),
        ("skills", "13"),
        ("tools", "52"),
    ]


# --- exact vs approximate ---

def test_correct_page_produces_no_problems(checker, full_page):
    page, patterns = full_page("### dct — Deterministic Context Tracker (v1.2.1, available)\n" + STATS_LINE)

    checked, problems = checker.check_file(page, patterns, DERIVED)

    assert problems == []
    assert checked == 5


def test_approximate_test_count_within_tolerance_passes(checker, full_page):
    """The site is deployed once per release; an exact count is stale at once."""
    page, patterns = full_page("(v1.2.1, available)\n" + STATS_LINE.replace("~940", "~900"))

    _, problems = checker.check_file(page, patterns, DERIVED)

    assert problems == []


def test_approximate_test_count_beyond_tolerance_fails(checker, full_page):
    page, patterns = full_page("(v1.2.1, available)\n" + STATS_LINE.replace("~940", "~820"))

    _, problems = checker.check_file(page, patterns, DERIVED)

    assert len(problems) == 1
    assert "rendered as 820" in problems[0] and "source says 946" in problems[0]


def test_exact_readme_number_tolerates_nothing(checker, tmp_path):
    """No tilde, no tolerance: an in-repo number is edited with the code."""
    readme = tmp_path / "README.md"
    readme.write_text("Built for Claude Code (skills, hooks, 48 MCP tools).\n", encoding="utf-8")

    _, problems = checker.check_file(readme, checker.README_PATTERNS, DERIVED)

    assert len(problems) == 1
    assert "rendered as 48" in problems[0] and "source says 49" in problems[0]


def test_readme_carries_no_volatile_number(checker):
    """The test count drifts on every commit; keeping it would make CI cry wolf."""
    quantities = {quantity for _, quantity, _ in checker.README_PATTERNS}

    assert "tests" not in quantities


def test_version_drift_is_reported(checker, tmp_path):
    page = tmp_path / "index.html"
    page.write_text(
        '"softwareVersion": "1.1.0",\n>v1.2.1 · available\n49 MCP tools 13 skills 4 hooks ~940 tests\n',
        encoding="utf-8",
    )

    _, problems = checker.check_file(page, checker.SITE_PATTERNS["public/index.html"], DERIVED)

    assert len(problems) == 1
    assert "rendered as 1.1.0" in problems[0] and "source says 1.2.1" in problems[0]


# --- the check must never pass by matching nothing ---

def test_missing_tilde_on_the_site_fails(checker, full_page):
    """An exact count published on a rarely-deployed page is a promise we break."""
    page, patterns = full_page("(v1.2.1, available)\n" + STATS_LINE.replace("~940", "946"))

    _, problems = checker.check_file(page, patterns, DERIVED)

    assert len(problems) == 1
    assert "tests number vanished" in problems[0]


def test_restructured_page_fails_per_missing_number(checker, full_page):
    page, patterns = full_page("<h1>dct</h1>\n")

    checked, problems = checker.check_file(page, patterns, DERIVED)

    assert checked == 0
    assert len(problems) == len(patterns)


def test_missing_file_is_reported(checker, tmp_path):
    checked, problems = checker.check_file(tmp_path / "gone.txt", checker.SITE_COUNTS, DERIVED)

    assert checked == 0
    assert "missing" in problems[0]


# --- --fix rewrites the numbers in place ---

def test_fix_corrects_a_drifted_number(checker, tmp_path):
    page = tmp_path / "llms.txt"
    page.write_text("48 MCP tools, 13 skills, 4 hooks\n", encoding="utf-8")

    rewrites = checker.fix_file(page, checker.SITE_COUNTS, DERIVED)

    assert page.read_text(encoding="utf-8") == "49 MCP tools, 13 skills, 4 hooks\n"
    assert len(rewrites) == 1
    assert "48" in rewrites[0] and "49" in rewrites[0]


def test_fix_touches_nothing_but_the_captured_span(checker, tmp_path):
    page = tmp_path / "llms.txt"
    before = "48 MCP tools, 13 skills, 4 hooks\nunrelated 48 elsewhere · 1948 · dcg: 17 tools\n"
    page.write_text(before, encoding="utf-8")

    checker.fix_file(page, checker.SITE_COUNTS, DERIVED)

    after = page.read_text(encoding="utf-8")
    assert after == before.replace("48 MCP tools", "49 MCP tools", 1)


def test_fix_is_idempotent(checker, tmp_path):
    page = tmp_path / "llms.txt"
    page.write_text("48 MCP tools, 13 skills, 4 hooks\n", encoding="utf-8")

    checker.fix_file(page, checker.SITE_COUNTS, DERIVED)
    settled = page.read_text(encoding="utf-8")
    second = checker.fix_file(page, checker.SITE_COUNTS, DERIVED)

    assert second == []
    assert page.read_text(encoding="utf-8") == settled


def test_fix_refuses_when_a_pattern_matches_nothing(checker, tmp_path):
    page = tmp_path / "llms.txt"
    page.write_text("48 MCP tools, 13 skills\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hooks"):
        checker.fix_file(page, checker.SITE_COUNTS, DERIVED)

    assert page.read_text(encoding="utf-8") == "48 MCP tools, 13 skills\n", "must not write a partial fix"


def test_fix_writes_approximate_targets_with_a_tilde_rounded_down(checker, tmp_path):
    page = tmp_path / "llms-full.txt"
    page.write_text("(v1.2.1, available)\n49 MCP tools · 13 skills · 4 hooks · ~820 tests\n", encoding="utf-8")

    checker.fix_file(page, checker.SITE_PATTERNS["public/llms-full.txt"], DERIVED)

    assert "~940 tests" in page.read_text(encoding="utf-8"), "946 rounds down to 940, keeps the tilde"


def test_fix_canonicalizes_an_approximation_the_check_tolerates(checker, tmp_path):
    """check() is lenient so CI stays quiet; fix() normalizes to one rendering."""
    page = tmp_path / "llms-full.txt"
    page.write_text("(v1.2.1, available)\n49 MCP tools · 13 skills · 4 hooks · ~900 tests\n", encoding="utf-8")
    patterns = checker.SITE_PATTERNS["public/llms-full.txt"]

    _, problems = checker.check_file(page, patterns, DERIVED)
    checker.fix_file(page, patterns, DERIVED)

    assert problems == [], "~900 is within tolerance of 946"
    assert "~940 tests" in page.read_text(encoding="utf-8")


def test_derived_hook_count_excludes_shared_hook_libs(checker):
    """A sourced *.lib.sh beside the hooks (K-48) is not a hook: the advertised
    hook count must equal the hooks the installer wires, not every *.sh file."""
    repo = SCRIPT.parent.parent
    hooks_dir = repo / "dct" / "hooks"
    wired = [p for p in hooks_dir.glob("*.sh") if not p.name.endswith(".lib.sh")]
    assert any(p.name.endswith(".lib.sh") for p in hooks_dir.glob("*.sh")), (
        "precondition: a shared hook lib exists, otherwise this test proves nothing")
    assert checker.derive(repo)["hooks"] == str(len(wired))
