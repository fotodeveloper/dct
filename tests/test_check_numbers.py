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


def test_exact_readme_count_tolerates_nothing(checker, tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("49 MCP tools\n49 tools\nAll tests (945)\n", encoding="utf-8")

    _, problems = checker.check_file(readme, checker.README_PATTERNS, DERIVED)

    assert len(problems) == 1
    assert "rendered as 945" in problems[0] and "source says 946" in problems[0]


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
