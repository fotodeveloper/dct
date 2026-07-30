"""Tests for changelog export to Keep a Changelog markdown."""

import pytest

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project
from dct.export import export_changelog_md


@pytest.fixture
def project(engine):
    return create_project(engine, slug="proj-a", name="Project A")


def test_export_empty(engine, project):
    md = export_changelog_md(engine, "proj-a")
    assert "# Changelog" in md
    assert "[Unreleased]" not in md


def test_export_unreleased_only(engine, project):
    add_entry(engine, "proj-a", "added", "New feature")
    add_entry(engine, "proj-a", "fixed", "Bug fix")
    md = export_changelog_md(engine, "proj-a")
    assert "## [Unreleased]" in md
    assert "### Added" in md
    assert "- New feature" in md
    assert "### Fixed" in md
    assert "- Bug fix" in md


def test_export_with_component(engine, project):
    add_entry(engine, "proj-a", "added", "Parser support", component="parser")
    md = export_changelog_md(engine, "proj-a")
    assert "- Parser support (parser)" in md


def test_export_released_with_date(engine, project):
    add_entry(engine, "proj-a", "added", "Feature A")
    release_entries(engine, "proj-a", "1.0")
    md = export_changelog_md(engine, "proj-a")
    assert "## [1.0]" in md
    assert "### Added" in md
    assert "- Feature A" in md


def test_export_mixed_unreleased_and_released(engine, project):
    add_entry(engine, "proj-a", "added", "Old feature")
    release_entries(engine, "proj-a", "1.0")
    add_entry(engine, "proj-a", "added", "New feature")
    md = export_changelog_md(engine, "proj-a")
    lines = md.split("\n")
    unreleased_idx = next(i for i, l in enumerate(lines) if "[Unreleased]" in l)
    released_idx = next(i for i, l in enumerate(lines) if "[1.0]" in l)
    # Unreleased must come before released
    assert unreleased_idx < released_idx


def test_export_category_ordering(engine, project):
    add_entry(engine, "proj-a", "removed", "Removed thing")
    add_entry(engine, "proj-a", "fixed", "Fixed thing")
    add_entry(engine, "proj-a", "changed", "Changed thing")
    add_entry(engine, "proj-a", "added", "Added thing")
    md = export_changelog_md(engine, "proj-a")
    lines = md.split("\n")
    # Categories should appear in order: Added, Changed, Fixed, Removed
    added_idx = next(i for i, l in enumerate(lines) if "### Added" in l)
    changed_idx = next(i for i, l in enumerate(lines) if "### Changed" in l)
    fixed_idx = next(i for i, l in enumerate(lines) if "### Fixed" in l)
    removed_idx = next(i for i, l in enumerate(lines) if "### Removed" in l)
    assert added_idx < changed_idx < fixed_idx < removed_idx


def test_export_without_unreleased(engine, project):
    add_entry(engine, "proj-a", "added", "Feature")
    md = export_changelog_md(engine, "proj-a", include_unreleased=False)
    assert "[Unreleased]" not in md


def test_export_multiple_versions_ordering(engine, project):
    add_entry(engine, "proj-a", "added", "V1 feature")
    release_entries(engine, "proj-a", "1.0")
    add_entry(engine, "proj-a", "added", "V2 feature")
    release_entries(engine, "proj-a", "2.0")
    md = export_changelog_md(engine, "proj-a")
    lines = md.split("\n")
    v2_idx = next(i for i, l in enumerate(lines) if "[2.0]" in l)
    v1_idx = next(i for i, l in enumerate(lines) if "[1.0]" in l)
    # Newer version should come first
    assert v2_idx < v1_idx


def test_export_nonexistent_project(engine):
    with pytest.raises(ValueError, match="not found"):
        export_changelog_md(engine, "nonexistent")


# ── v0.5+ single-version render (append-only release flow, item #214) ──────────

def test_export_single_version_isolates_section(engine, project):
    """version=X renders ONLY that version's block — no header, no other
    versions, no Unreleased. This is the unit the append-only /release flow
    prepends into the existing changelog.md."""
    add_entry(engine, "proj-a", "added", "V1 feature")
    release_entries(engine, "proj-a", "1.0")
    add_entry(engine, "proj-a", "added", "V2 feature")
    release_entries(engine, "proj-a", "2.0")
    add_entry(engine, "proj-a", "fixed", "Still unreleased")

    md = export_changelog_md(engine, "proj-a", version="1.0")

    assert "## [1.0]" in md
    assert "- V1 feature" in md
    # The single-version block must NOT carry the file-level header...
    assert "# Changelog" not in md
    # ...nor leak any other version or the unreleased staging area.
    assert "[2.0]" not in md
    assert "V2 feature" not in md
    assert "[Unreleased]" not in md
    assert "Still unreleased" not in md


def test_export_single_version_unknown_returns_empty(engine, project):
    """An unknown version has no block to render -> empty string (caller skips
    the prepend rather than writing a bogus section)."""
    add_entry(engine, "proj-a", "added", "V1 feature")
    release_entries(engine, "proj-a", "1.0")

    assert export_changelog_md(engine, "proj-a", version="9.9") == ""


def test_export_single_version_is_deterministic(engine, project):
    """Rendering the same version twice yields identical output — re-running
    /release for an already-archived version stays a safe no-op (the prepend
    can detect 'already present' by exact-match)."""
    add_entry(engine, "proj-a", "added", "V1 feature")
    add_entry(engine, "proj-a", "fixed", "V1 bugfix")
    release_entries(engine, "proj-a", "1.0")

    first = export_changelog_md(engine, "proj-a", version="1.0")
    second = export_changelog_md(engine, "proj-a", version="1.0")
    assert first == second
    assert first  # non-empty


def test_export_single_version_does_not_break_full_render(engine, project):
    """Regression: the default (version=None) path is unchanged — full file
    with header + all versions."""
    add_entry(engine, "proj-a", "added", "V1 feature")
    release_entries(engine, "proj-a", "1.0")
    add_entry(engine, "proj-a", "added", "V2 feature")
    release_entries(engine, "proj-a", "2.0")

    md = export_changelog_md(engine, "proj-a")
    assert "# Changelog" in md
    assert "## [1.0]" in md
    assert "## [2.0]" in md
