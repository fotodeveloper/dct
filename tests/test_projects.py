import pytest

from dct.core.projects import (
    create_project,
    delete_project,
    get_project,
    get_project_by_path,
    list_projects,
)


def test_create_and_get(engine):
    p = create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    assert p["slug"] == "proj-a"
    assert p["name"] == "Project A"
    assert p["deleted_at"] is None

    fetched = get_project(engine, "proj-a")
    assert fetched["id"] == p["id"]


def test_create_duplicate_raises(engine):
    create_project(engine, slug="proj-a", name="Project A")
    with pytest.raises(ValueError, match="already exists"):
        create_project(engine, slug="proj-a", name="Project A again")


def test_list_projects(engine):
    create_project(engine, slug="a", name="A")
    create_project(engine, slug="b", name="B")
    result = list_projects(engine)
    assert len(result) == 2
    assert result[0]["slug"] == "a"  # ordered by slug


def test_soft_delete(engine):
    create_project(engine, slug="proj-a", name="Project A")
    assert delete_project(engine, "proj-a") is True
    assert get_project(engine, "proj-a") is None
    assert len(list_projects(engine)) == 0
    assert len(list_projects(engine, include_deleted=True)) == 1


def test_get_project_by_path(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    create_project(engine, slug="proj-b", name="Project B", path="/tmp/proj-b")

    p = get_project_by_path(engine, "/tmp/proj-a/subdir/nested")
    assert p["slug"] == "proj-a"

    p = get_project_by_path(engine, "/tmp/proj-b")
    assert p["slug"] == "proj-b"

    assert get_project_by_path(engine, "/tmp/other") is None


def test_get_project_by_path_resolves_symlinks(engine, tmp_path):
    """#227: a project registered via a symlinked path must resolve when queried
    from the physical (realpath) location and vice-versa. os.getcwd() yields the
    physical path on macOS, so an abspath-stored / realpath-queried mismatch made
    `dct project detect` silently miss → changelog enforcement off. Both sides
    must be canonicalized with os.path.realpath."""
    import os

    real = tmp_path / "realproj"
    real.mkdir()
    link = tmp_path / "linkproj"
    os.symlink(real, link)

    create_project(engine, slug="sym", name="Sym", path=str(link))

    # queried from the physical path (what CWD resolves to after cd into the link)
    assert get_project_by_path(engine, str(real))["slug"] == "sym"
    # a nested subdir under the physical path
    assert get_project_by_path(engine, str(real / "a" / "b"))["slug"] == "sym"
    # and from the symlink path itself
    assert get_project_by_path(engine, str(link))["slug"] == "sym"


def test_create_project_canonicalizes_path(engine, tmp_path):
    """#227: stored path is canonical (realpath) so the DB never carries a
    symlinked/logical path that later fails CWD detection."""
    import os

    real = tmp_path / "r"
    real.mkdir()
    link = tmp_path / "l"
    os.symlink(real, link)

    p = create_project(engine, slug="rp", name="RP", path=str(link))
    assert p["path"] == os.path.realpath(str(link))


def test_get_nonexistent(engine):
    assert get_project(engine, "nope") is None


def test_create_with_version(engine):
    p = create_project(engine, slug="proj-a", name="Project A", current_version="0.18")
    assert p["current_version"] == "0.18"


def test_set_version(engine):
    from dct.core.projects import set_version

    create_project(engine, slug="proj-a", name="Project A")
    p = set_version(engine, "proj-a", "1.0")
    assert p["current_version"] == "1.0"


def test_set_version_nonexistent(engine):
    from dct.core.projects import set_version

    assert set_version(engine, "nope", "1.0") is None


def test_create_project_auto_detects_version(engine, tmp_path):
    """With no --version but a VERSION file in path, detection populates it."""
    (tmp_path / "VERSION").write_text("0.42")
    p = create_project(engine, slug="auto", name="Auto", path=str(tmp_path))
    assert p["current_version"] == "0.42"
    assert p["_detected_source"] == "VERSION"


def test_create_project_explicit_version_wins_over_detect(engine, tmp_path):
    """Explicit current_version bypasses autodetect entirely."""
    (tmp_path / "VERSION").write_text("9.9.9")
    p = create_project(engine, slug="explicit", name="X", path=str(tmp_path), current_version="1.0")
    assert p["current_version"] == "1.0"
    assert "_detected_source" not in p


def test_create_project_without_path_skips_autodetect(engine):
    p = create_project(engine, slug="nopath", name="NoPath")
    assert p["current_version"] is None
    assert "_detected_source" not in p


def test_create_project_path_without_version_files(engine, tmp_path):
    """Valid path but no VERSION-bearing files — still succeeds, NULL version."""
    p = create_project(engine, slug="empty", name="Empty", path=str(tmp_path))
    assert p["current_version"] is None
    assert "_detected_source" not in p


def test_create_project_detects_pyproject(engine, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "2.1.0"\n')
    p = create_project(engine, slug="py", name="Py", path=str(tmp_path))
    assert p["current_version"] == "2.1.0"
    assert p["_detected_source"] == "pyproject.toml"


# ── update_project (rename) ──────────────────────────────────────────────────


class TestUpdateProject:
    """update_project is the rename/retitle path — slug, name, description,
    path may all change while project_id stays stable so items/changelog FKs
    are preserved.
    """

    def test_rename_name_only(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="old-slug", name="Old", path="/x")
        before = get_project(engine, "old-slug")

        after = update_project(engine, "old-slug", new_name="example.io")

        assert after["name"] == "example.io"
        assert after["slug"] == "old-slug"
        assert after["id"] == before["id"], "project_id must stay stable"

    def test_rename_slug_only(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="old-slug", name="Old")
        before_id = get_project(engine, "old-slug")["id"]

        after = update_project(engine, "old-slug", new_slug="new-slug")

        assert after["slug"] == "new-slug"
        assert after["name"] == "Old"
        assert after["id"] == before_id
        assert get_project(engine, "old-slug") is None

    def test_rename_both_slug_and_name(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="old-slug", name="Old")

        after = update_project(
            engine, "old-slug", new_slug="new-slug", new_name="example.io"
        )

        assert after["slug"] == "new-slug"
        assert after["name"] == "example.io"

    def test_rename_description_and_path(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="x", name="X", path="/old")

        after = update_project(
            engine, "x", new_description="fresh desc", new_path="/new"
        )

        assert after["description"] == "fresh desc"
        assert after["path"] == "/new"

    def test_rename_to_taken_slug_raises(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="a", name="A")
        create_project(engine, slug="b", name="B")

        with pytest.raises(ValueError, match="already exists"):
            update_project(engine, "a", new_slug="b")

    def test_rename_nonexistent_returns_none(self, engine):
        from dct.core.projects import update_project
        assert update_project(engine, "nope", new_name="X") is None

    def test_rename_empty_name_raises(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="a", name="A")
        with pytest.raises(ValueError, match="new_name cannot be empty"):
            update_project(engine, "a", new_name="   ")

    def test_rename_empty_slug_raises(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="a", name="A")
        with pytest.raises(ValueError, match="new_slug cannot be empty"):
            update_project(engine, "a", new_slug="")

    def test_rename_no_fields_raises(self, engine):
        from dct.core.projects import update_project
        create_project(engine, slug="a", name="A")
        with pytest.raises(ValueError, match="no fields supplied"):
            update_project(engine, "a")

    def test_rename_same_slug_no_collision(self, engine):
        """Passing new_slug equal to current slug is a no-op on slug, not collision."""
        from dct.core.projects import update_project
        create_project(engine, slug="a", name="A")
        after = update_project(engine, "a", new_slug="a", new_name="B")
        assert after["slug"] == "a"
        assert after["name"] == "B"

    def test_rename_ignores_soft_deleted_collision(self, engine):
        """Soft-deleted project with target slug should NOT block rename."""
        from dct.core.projects import update_project
        create_project(engine, slug="old", name="Old")
        delete_project(engine, "old")
        create_project(engine, slug="current", name="Current")

        after = update_project(engine, "current", new_slug="old")
        assert after["slug"] == "old"

    def test_rename_preserves_items_fk(self, engine):
        """Items created for the project must still resolve after rename."""
        from dct.core.items import create_item, list_items
        from dct.core.projects import update_project

        create_project(engine, slug="old-slug", name="Old")
        create_item(engine, project_slug="old-slug", item_type="todo", title="A")

        update_project(engine, "old-slug", new_slug="new-slug", new_name="example.io")

        items = list_items(engine, project_slug="new-slug")
        rows = items["rows"] if isinstance(items, dict) else items
        assert len(rows) == 1


# ── manages_changelog: opt-in changelog enforcement (#222) ───────────────────


class TestManagesChangelog:
    """projects.manages_changelog gates the dct-check-changelog hook per project.
    New projects default ON (added to dct ⇒ dct manages its changelog); opt out
    with set_manages_changelog(slug, False)."""

    def test_new_project_defaults_to_managed(self, engine):
        p = create_project(engine, slug="a", name="A")
        assert p["manages_changelog"] is True

    def test_helper_true_for_new_project(self, engine):
        from dct.core.projects import project_manages_changelog
        create_project(engine, slug="a", name="A")
        assert project_manages_changelog(engine, "a") is True

    def test_set_off_then_on(self, engine):
        from dct.core.projects import project_manages_changelog, set_manages_changelog
        create_project(engine, slug="a", name="A")

        off = set_manages_changelog(engine, "a", False)
        assert off["manages_changelog"] is False
        assert project_manages_changelog(engine, "a") is False

        on = set_manages_changelog(engine, "a", True)
        assert on["manages_changelog"] is True
        assert project_manages_changelog(engine, "a") is True

    def test_set_nonexistent_returns_none(self, engine):
        from dct.core.projects import set_manages_changelog
        assert set_manages_changelog(engine, "nope", False) is None

    def test_helper_false_for_unknown_project(self, engine):
        from dct.core.projects import project_manages_changelog
        assert project_manages_changelog(engine, "nope") is False


# ── changelog_path: per-project changelog location (#225) ────────────────────


class TestChangelogPath:
    """projects.changelog_path (#225) lets a project keep its changelog outside
    the repo root (e.g. a monorepo package → 'pkg/changelog.md'). Default is
    'changelog.md'. /release resolves {project.path}/{changelog_path}. The value
    is constrained to the project root (leading '/' stripped, '..' escape rejected)."""

    def test_new_project_defaults_to_root_changelog(self, engine):
        p = create_project(engine, slug="a", name="A")
        assert p["changelog_path"] == "changelog.md"

    def test_set_and_get(self, engine):
        from dct.core.projects import set_changelog_path

        create_project(engine, slug="a", name="A")
        out = set_changelog_path(engine, "a", "pkg/changelog.md")
        assert out["changelog_path"] == "pkg/changelog.md"
        assert get_project(engine, "a")["changelog_path"] == "pkg/changelog.md"

    def test_set_strips_leading_slash(self, engine):
        from dct.core.projects import set_changelog_path

        create_project(engine, slug="a", name="A")
        out = set_changelog_path(engine, "a", "/pkg/changelog.md")
        assert out["changelog_path"] == "pkg/changelog.md"

    def test_set_normalizes_redundant_segments(self, engine):
        from dct.core.projects import set_changelog_path

        create_project(engine, slug="a", name="A")
        out = set_changelog_path(engine, "a", "pkg/./sub/../changelog.md")
        assert out["changelog_path"] == "pkg/changelog.md"

    def test_set_rejects_parent_escape(self, engine):
        from dct.core.projects import set_changelog_path

        create_project(engine, slug="a", name="A")
        with pytest.raises(ValueError, match="within the project root"):
            set_changelog_path(engine, "a", "../../etc/changelog.md")

    def test_set_rejects_empty(self, engine):
        from dct.core.projects import set_changelog_path

        create_project(engine, slug="a", name="A")
        with pytest.raises(ValueError, match="cannot be empty"):
            set_changelog_path(engine, "a", "   ")

    def test_set_nonexistent_returns_none(self, engine):
        from dct.core.projects import set_changelog_path

        assert set_changelog_path(engine, "nope", "pkg/changelog.md") is None

    def test_resolve_joins_path_and_changelog_path(self, engine):
        import os

        from dct.core.projects import resolve_changelog_path, set_changelog_path

        create_project(engine, slug="a", name="A", path="/Users/x/mono")
        set_changelog_path(engine, "a", "pkg/changelog.md")
        proj = get_project(engine, "a")
        assert resolve_changelog_path(proj) == os.path.join(proj["path"], "pkg/changelog.md")

    def test_resolve_default_is_root_changelog(self, engine):
        import os

        from dct.core.projects import resolve_changelog_path

        create_project(engine, slug="a", name="A", path="/Users/x/proj")
        proj = get_project(engine, "a")
        assert resolve_changelog_path(proj) == os.path.join(proj["path"], "changelog.md")

    def test_resolve_none_when_no_path(self, engine):
        from dct.core.projects import resolve_changelog_path

        create_project(engine, slug="a", name="A")  # no path registered
        proj = get_project(engine, "a")
        assert resolve_changelog_path(proj) is None
