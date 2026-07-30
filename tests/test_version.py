"""Tests for version bump + autodetect logic."""

import json

import pytest

from dct.core.version import bump, detect_version, format_version, parse_version


class TestParseFormat:
    def test_two_part(self):
        assert parse_version("0.18") == (0, 18)

    def test_three_part(self):
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_format_two(self):
        assert format_version((0, 18)) == "0.18"

    def test_format_three(self):
        assert format_version((1, 2, 3)) == "1.2.3"


class TestBumpThreePart:
    def test_patch(self):
        assert bump("0.1.0", "patch") == "0.1.1"

    def test_minor(self):
        assert bump("0.1.0", "minor") == "0.2.0"

    def test_major(self):
        assert bump("0.1.0", "major") == "1.0.0"

    def test_patch_nonzero(self):
        assert bump("1.5.3", "patch") == "1.5.4"

    def test_minor_resets_patch(self):
        assert bump("1.5.3", "minor") == "1.6.0"

    def test_major_resets_all(self):
        assert bump("1.5.3", "major") == "2.0.0"


class TestBumpTwoPart:
    def test_patch_adds_third(self):
        assert bump("0.18", "patch") == "0.18.1"

    def test_minor(self):
        assert bump("0.18", "minor") == "0.19"

    def test_major(self):
        assert bump("0.18", "major") == "1.0"

    def test_minor_from_zero(self):
        assert bump("0.1", "minor") == "0.2"


class TestBumpInvalid:
    def test_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid bump type"):
            bump("0.1.0", "hotfix")


class TestBumpIntegration:
    def test_release_with_bump(self, engine):
        from dct.core.changelog import add_entry, release_entries
        from dct.core.projects import create_project, get_project

        create_project(engine, slug="proj-a", name="Project A", current_version="1.0")
        add_entry(engine, "proj-a", "added", "New feature")

        new_version = bump("1.0", "minor")
        assert new_version == "1.1"

        release_entries(engine, "proj-a", new_version)
        p = get_project(engine, "proj-a")
        assert p["current_version"] == "1.1"


class TestDetectVersion:
    def test_version_file_wins(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.18\n")
        assert detect_version(tmp_path) == ("0.18", "VERSION")

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "2.3.4"\n'
        )
        assert detect_version(tmp_path) == ("2.3.4", "pyproject.toml")

    def test_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "foo", "version": "1.2.3"}))
        assert detect_version(tmp_path) == ("1.2.3", "package.json")

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "foo"\nversion = "0.7.1"\n'
        )
        assert detect_version(tmp_path) == ("0.7.1", "Cargo.toml")

    def test_tauri_v2_top_level(self, tmp_path):
        src = tmp_path / "src-tauri"
        src.mkdir()
        (src / "tauri.conf.json").write_text(json.dumps({"version": "0.4.0"}))
        assert detect_version(tmp_path) == ("0.4.0", "src-tauri/tauri.conf.json")

    def test_tauri_v1_nested_package(self, tmp_path):
        src = tmp_path / "src-tauri"
        src.mkdir()
        (src / "tauri.conf.json").write_text(json.dumps({"package": {"version": "0.3.0"}}))
        assert detect_version(tmp_path) == ("0.3.0", "src-tauri/tauri.conf.json")

    def test_priority_version_over_pyproject(self, tmp_path):
        (tmp_path / "VERSION").write_text("9.9.9")
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.0.0"\n')
        assert detect_version(tmp_path) == ("9.9.9", "VERSION")

    def test_empty_version_file_falls_through(self, tmp_path):
        (tmp_path / "VERSION").write_text("   \n")
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "7.7.7"\n')
        assert detect_version(tmp_path) == ("7.7.7", "pyproject.toml")

    def test_malformed_pyproject_falls_through(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("not { valid :: toml")
        (tmp_path / "package.json").write_text(json.dumps({"version": "5.5.5"}))
        assert detect_version(tmp_path) == ("5.5.5", "package.json")

    def test_malformed_package_json_falls_through(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json}")
        (tmp_path / "Cargo.toml").write_text('[package]\nversion = "4.4.4"\n')
        assert detect_version(tmp_path) == ("4.4.4", "Cargo.toml")

    def test_nothing_found(self, tmp_path):
        assert detect_version(tmp_path) == (None, None)

    def test_pyproject_without_version_field(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "foo"\n')
        assert detect_version(tmp_path) == (None, None)

    def test_nonexistent_path(self, tmp_path):
        assert detect_version(tmp_path / "does-not-exist") == (None, None)

    def test_version_non_string_ignored(self, tmp_path):
        # package.json with numeric version field → not a string → skip
        (tmp_path / "package.json").write_text('{"version": 1}')
        (tmp_path / "Cargo.toml").write_text('[package]\nversion = "3.3.3"\n')
        assert detect_version(tmp_path) == ("3.3.3", "Cargo.toml")
