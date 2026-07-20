"""Packaging regression tests — a wheel install must carry skills, hooks, web assets.

Locks in the fix for the pre-1.0.0 release blocker: skills/ and hooks/ used to
be repo *siblings* of the package, so a PyPI wheel shipped none of them —
SKILLS_DIR/HOOKS_DIR resolved to nonexistent site-packages paths, `dct init`
silently installed zero skills/hooks, and `dct web` died on missing templates.

The contract locked here: every asset the installer or the web viewer needs
lives INSIDE the `dct` package and is declared for both wheel (package-data)
and sdist (MANIFEST.in) builds, and the installable skill list is derived from
the directory — never a hardcoded list that can drift.
"""

import tomllib
from pathlib import Path

from dct.integrations.claude import HOOKS_DIR, SKILLS_DIR, discover_skills

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "dct"

EXPECTED_HOOKS = [
    "check-changelog-on-stop.sh",
    "check-changelog.sh",
    "dct-plan-autoscan.sh",
    "dct-task-mirror.sh",
]


def test_skills_dir_lives_inside_the_package():
    assert SKILLS_DIR == PKG_ROOT / "skills"
    assert SKILLS_DIR.is_dir()


def test_hooks_dir_lives_inside_the_package():
    assert HOOKS_DIR == PKG_ROOT / "hooks"
    assert sorted(p.name for p in HOOKS_DIR.glob("*.sh")) == EXPECTED_HOOKS


def test_skill_discovery_matches_directory_contents():
    """The skill list comes from the filesystem — no hardcoded-list drift.

    The pre-fix hardcoded SKILL_NAMES silently omitted plan-status and
    plan-resolve-uncertain, so `dct init` never installed them.
    """
    discovered = discover_skills()
    on_disk = sorted(
        p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()
    )
    assert discovered == on_disk
    assert "plan-status" in discovered
    assert "plan-resolve-uncertain" in discovered
    assert len(discovered) >= 13


def test_every_skill_directory_has_a_skill_md():
    stray = [
        d.name
        for d in SKILLS_DIR.iterdir()
        if d.is_dir() and not (d / "SKILL.md").is_file()
    ]
    assert not stray, f"skill dirs without SKILL.md (won't be installed): {stray}"


def _asset_files() -> list[Path]:
    """Every non-Python file the package must ship, relative to dct/."""
    wanted = []
    for sub in ("skills", "hooks", "web/templates", "web/static"):
        for p in (PKG_ROOT / sub).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store":
                wanted.append(p)
    assert wanted, "asset scan came up empty — did the directories move?"
    return wanted


def test_package_data_covers_every_asset_file():
    """Wheel builds: each asset file must match a [tool.setuptools.package-data] glob."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        patterns = tomllib.load(f)["tool"]["setuptools"]["package-data"]["dct"]
    covered = {p for pat in patterns for p in PKG_ROOT.glob(pat) if p.is_file()}
    missing = [str(p.relative_to(PKG_ROOT)) for p in _asset_files() if p not in covered]
    assert not missing, f"files no package-data glob covers: {missing}"


def test_manifest_grafts_every_asset_dir():
    """Sdist builds (python -m build in CI publishes sdist→wheel): MANIFEST.in
    must graft each asset directory, or the sdist — and any wheel built from
    it — ships without them even when package-data is correct."""
    manifest = (REPO_ROOT / "MANIFEST.in").read_text()
    for sub in ("dct/skills", "dct/hooks", "dct/web/templates", "dct/web/static"):
        assert f"graft {sub}" in manifest, f"MANIFEST.in missing: graft {sub}"
