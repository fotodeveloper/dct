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


# ── K-44: one version contract (parse / normalize / compare / bump) ──────────


class TestVersionContract:
    @pytest.mark.parametrize("raw,expected", [
        ("1.6.0", "1.6.0"), ("v1.6.0", "1.6.0"), ("V1.6", "1.6"),
        (" 0.18 ", "0.18"), ("v0.18.1\n", "0.18.1"), ("01.2.03", "1.2.3"),
    ])
    def test_normalize_strips_v_prefix_and_whitespace(self, raw, expected):
        from dct.core.version import normalize_version
        assert normalize_version(raw) == expected

    @pytest.mark.parametrize("bad", [
        "", "  ", "v", "1", "1.", ".1", "1.2.3.4", "1.2.3rc1", "1.2-beta",
        "2026-rc", "vv1.2", "1..2", "x.y.z", "1.2.3 4", "-1.2", "+1.2",
        "١.٢",  # Unicode digits are not ASCII release numbers
    ])
    def test_unsupported_formats_are_refused_explicitly(self, bad):
        from dct.core.version import normalize_version
        with pytest.raises(ValueError, match="Unsupported version"):
            normalize_version(bad)
        with pytest.raises(ValueError, match="Unsupported version"):
            parse_version(bad)

    def test_parse_accepts_v_prefix(self):
        assert parse_version("v1.6.0") == (1, 6, 0)

    @pytest.mark.parametrize("a,b,expected", [
        ("1.5", "1.5.0", 0), ("v1.5.0", "1.5", 0), ("1.4.0", "1.5.0", -1),
        ("1.10", "1.9.9", 1), ("2.0", "1.99.99", 1), ("0.18.1", "0.18", 1),
    ])
    def test_compare_drops_trailing_zeros(self, a, b, expected):
        from dct.core.version import compare_versions
        assert compare_versions(a, b) == expected
        assert compare_versions(b, a) == -expected

    def test_compare_refuses_unsupported(self):
        from dct.core.version import compare_versions
        with pytest.raises(ValueError, match="Unsupported version"):
            compare_versions("1.2.3rc1", "1.2.3")

    @pytest.mark.parametrize("current,bump_type,expected", [
        ("v1.6.0", "patch", "1.6.1"), ("v0.18", "patch", "0.18.1"),
        ("V2.0", "major", "3.0"),
    ])
    def test_bump_of_v_prefixed_current_gives_a_normalized_version(
        self, current, bump_type, expected
    ):
        assert bump(current, bump_type) == expected

    @pytest.mark.parametrize("bad", ["1", "1.2.3.4", "2026-rc"])
    def test_bump_refuses_unsupported_current(self, bad):
        with pytest.raises(ValueError, match="Unsupported version"):
            bump(bad, "patch")
