"""K-76 (dogfood of #877): the packaged, generic version-sync template.

/dct-init used to copy dct's OWN scripts/dct-version-sync.sh — a script with a
live line for dct/__init__.py, macOS-only `sed -i ''`, and not shipped in the
wheel at all. The template now lives in dct/assets with every block commented;
`dct project version-sync-script` writes it into a project with the block of
its version file switched on, and never overwrites an existing script.

The generated script is RUN here (bash) on real files: it must change exactly
the version field and nothing else, keep the file's mode, and work with the
portable filters the template uses (no `sed -i`).
"""

import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
from conftest import run_cli

from dct.assets import version_sync_template_path
from dct.core.version import VERSION_FILES, detect_version
from dct.core.version_sync import SCRIPT_PATH, SENTINEL, render, write_script

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or ""

SAMPLES = {
    "VERSION": ("0.2.1\n", "0.3.0\n"),
    "pyproject.toml": (
        ('[build-system]\nrequires = ["setuptools"]\n\n'
         '[project]\nname = "greeter"\nversion = "0.2.1"\n'
         'dependencies = ["x"]\n\n[tool.other]\nversion = "9.9.9"\n'),
        ('[build-system]\nrequires = ["setuptools"]\n\n'
         '[project]\nname = "greeter"\nversion = "0.3.0"\n'
         'dependencies = ["x"]\n\n[tool.other]\nversion = "9.9.9"\n'),
    ),
    "package.json": (
        ('{\n  "name": "greeter",\n  "version": "0.2.1",\n'
         '  "devDependencies": {\n    "x": { "version": "1.0.0" }\n  }\n}\n'),
        ('{\n  "name": "greeter",\n  "version": "0.3.0",\n'
         '  "devDependencies": {\n    "x": { "version": "1.0.0" }\n  }\n}\n'),
    ),
    "Cargo.toml": (
        ('[package]\nname = "greeter"\nversion = "0.2.1"\n\n'
         '[dependencies]\nserde = { version = "1" }\n\n[dev-dependencies]\nversion = "0.0.1"\n'),
        ('[package]\nname = "greeter"\nversion = "0.3.0"\n\n'
         '[dependencies]\nserde = { version = "1" }\n\n[dev-dependencies]\nversion = "0.0.1"\n'),
    ),
    "src-tauri/tauri.conf.json": (
        '{\n  "productName": "greeter",\n  "version": "0.2.1",\n  "identifier": "x"\n}\n',
        '{\n  "productName": "greeter",\n  "version": "0.3.0",\n  "identifier": "x"\n}\n',
    ),
}


def _template() -> str:
    return version_sync_template_path().read_text(encoding="utf-8")


def _active_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


# ── the template itself ─────────────────────────────────────────────────────


def test_the_template_is_a_packaged_asset_with_the_sentinel():
    path = version_sync_template_path()
    assert path.parent == REPO_ROOT / "dct" / "assets"
    lines = _template().splitlines()
    assert lines[0] == "#!/bin/bash"
    assert lines[1] == SENTINEL, "the sentinel /dct-init recognises must follow the shebang"


def test_every_version_file_has_exactly_one_commented_block():
    text = _template()
    for kind in VERSION_FILES:
        assert text.count(f"# >>> {kind}\n") == 1, kind
        assert text.count(f"# <<< {kind}\n") == 1, kind


def test_the_template_changes_nothing_until_a_block_is_switched_on():
    """Every edit sits in a commented block: the bare template only defines
    its helper — no line writes a file."""
    active = _active_lines(_template())
    assert not [ln for ln in active if "edit_file " in ln and "()" not in ln
                and not ln.lstrip().startswith(("local", "shift"))], active
    assert not [ln for ln in active if "> \"$PROJECT_ROOT" in ln], active


def test_the_template_never_names_dct_itself_or_uses_sed_in_place():
    text = _template()
    assert "dct/__init__.py" not in text
    assert "sed -i" not in text.replace("no `sed -i`", "")


def test_the_template_never_releases_anything():
    active = " ".join(_active_lines(_template()))
    for word in ("git ", "npm publish", "cargo publish", "twine", "curl", "push"):
        assert word not in active, word


# ── render ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", VERSION_FILES)
def test_render_switches_on_only_the_chosen_block(kind):
    rendered = render(kind)
    before = _active_lines(_template())
    added = [ln for ln in _active_lines(rendered) if ln not in before]
    assert added, f"{kind}: nothing switched on"
    for other in VERSION_FILES:
        if other == kind:
            continue
        block = rendered.split(f"# >>> {other}\n")[1].split(f"# <<< {other}\n")[0]
        assert all(ln.startswith("# ") for ln in block.splitlines()), other


@pytest.mark.parametrize("bad", ["", "setup.py", "pyproject", "../pyproject.toml", "VERSION\n"])
def test_render_refuses_an_unknown_version_file(bad):
    with pytest.raises(ValueError):
        render(bad)


# ── the generated script, run for real ──────────────────────────────────────


@pytest.mark.slow
@pytest.mark.skipif(not BASH, reason="bash not available")
@pytest.mark.parametrize("kind", VERSION_FILES)
def test_the_generated_script_sets_only_the_version_field(tmp_path, kind):
    before, after = SAMPLES[kind]
    target = tmp_path / kind
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before, encoding="utf-8")
    os.chmod(target, 0o640)
    write_script(tmp_path, kind)

    run = subprocess.run([BASH, str(tmp_path / SCRIPT_PATH), "0.3.0"],
                         capture_output=True, text=True, timeout=30, check=False)
    assert run.returncode == 0, run.stderr
    assert target.read_text(encoding="utf-8") == after
    assert stat.S_IMODE(target.stat().st_mode) == 0o640, "the file's mode changed"
    assert detect_version(tmp_path) == ("0.3.0", kind)


@pytest.mark.slow
@pytest.mark.skipif(not BASH, reason="bash not available")
def test_the_generated_script_is_idempotent_and_needs_its_argument(tmp_path):
    (tmp_path / "pyproject.toml").write_text(SAMPLES["pyproject.toml"][0], encoding="utf-8")
    write_script(tmp_path, "pyproject.toml")
    script = str(tmp_path / SCRIPT_PATH)
    for _ in range(2):
        assert subprocess.run([BASH, script, "0.3.0"], timeout=30, check=False).returncode == 0
    assert (tmp_path / "pyproject.toml").read_text() == SAMPLES["pyproject.toml"][1]
    missing = subprocess.run([BASH, script], capture_output=True, text=True, timeout=30, check=False)
    assert missing.returncode != 0
    assert (tmp_path / "pyproject.toml").read_text() == SAMPLES["pyproject.toml"][1]


@pytest.mark.slow
@pytest.mark.skipif(not BASH, reason="bash not available")
def test_a_missing_version_file_fails_the_script_and_leaves_no_temp_file(tmp_path):
    write_script(tmp_path, "pyproject.toml")
    run = subprocess.run([BASH, str(tmp_path / SCRIPT_PATH), "0.3.0"],
                         capture_output=True, text=True, timeout=30, check=False)
    assert run.returncode != 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scripts"]


# ── write_script ────────────────────────────────────────────────────────────


def test_write_script_never_overwrites(tmp_path):
    existing = tmp_path / SCRIPT_PATH
    existing.parent.mkdir()
    existing.write_text("#!/bin/bash\necho mine\n")
    with pytest.raises(FileExistsError):
        write_script(tmp_path, "pyproject.toml")
    assert existing.read_text() == "#!/bin/bash\necho mine\n"


def test_write_script_refuses_a_symlink_in_the_scripts_place(tmp_path):
    elsewhere = tmp_path / "elsewhere.sh"
    (tmp_path / "scripts").mkdir()
    (tmp_path / SCRIPT_PATH).symlink_to(elsewhere)
    with pytest.raises(FileExistsError):
        write_script(tmp_path, "pyproject.toml")
    assert not elsewhere.exists(), "the write followed a dangling symlink"


def test_write_script_makes_the_script_executable(tmp_path):
    write_script(tmp_path, "VERSION")
    mode = (tmp_path / SCRIPT_PATH).stat().st_mode
    assert mode & stat.S_IXUSR


def test_the_release_runs_the_path_the_template_is_written_to():
    from dct.workflows.release.effects import VERSION_SYNC_SCRIPTS
    assert VERSION_SYNC_SCRIPTS[0] == SCRIPT_PATH


# ── CLI ─────────────────────────────────────────────────────────────────────


def _project(engine, tmp_path, slug, files=()):
    from dct.core.projects import create_project

    root = tmp_path / slug
    root.mkdir()
    for rel in files:
        (root / rel).write_text(SAMPLES[rel][0], encoding="utf-8")
    create_project(engine, slug=slug, name=slug, path=str(root))
    return root


def _exit_code(monkeypatch, argv):
    try:
        run_cli(monkeypatch, argv)
    except SystemExit as exc:
        return exc.code
    return 0


def test_cli_writes_the_detected_block_and_says_what_it_changes(
        cli_engine, tmp_path, monkeypatch, capsys):
    root = _project(cli_engine, tmp_path, "proj-k76", ["pyproject.toml"])
    assert _exit_code(monkeypatch, ["project", "version-sync-script", "proj-k76"]) == 0
    out = capsys.readouterr().out
    assert "pyproject.toml" in out and SCRIPT_PATH in out
    assert (root / SCRIPT_PATH).read_text(encoding="utf-8") == render("pyproject.toml")

    assert _exit_code(monkeypatch, ["project", "version-sync-script", "proj-k76"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert (root / SCRIPT_PATH).read_text(encoding="utf-8") == render("pyproject.toml")


def test_cli_without_a_version_file_needs_an_explicit_kind(
        cli_engine, tmp_path, monkeypatch, capsys):
    root = _project(cli_engine, tmp_path, "proj-k76b")
    assert _exit_code(monkeypatch, ["project", "version-sync-script", "proj-k76b"]) == 1
    assert "--kind" in capsys.readouterr().err
    assert not (root / SCRIPT_PATH).exists()
    argv = ["project", "version-sync-script", "proj-k76b", "--kind", "VERSION"]
    assert _exit_code(monkeypatch, argv) == 0
    assert (root / SCRIPT_PATH).read_text(encoding="utf-8") == render("VERSION")


def test_cli_rejects_a_kind_dct_cannot_edit(cli_engine, tmp_path, monkeypatch, capsys):
    _project(cli_engine, tmp_path, "proj-k76c", ["pyproject.toml"])
    argv = ["project", "version-sync-script", "proj-k76c", "--kind", "setup.py"]
    assert _exit_code(monkeypatch, argv) == 2  # argparse: not one of the choices
    assert not (tmp_path / "proj-k76c" / SCRIPT_PATH).exists()


def test_cli_unknown_project_is_refused(cli_engine, monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["project", "version-sync-script", "no-such-k76"]) == 1
    assert "not found" in capsys.readouterr().err


# ── packaging ───────────────────────────────────────────────────────────────


def test_the_template_ships_in_the_wheel_and_the_sdist():
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        patterns = tomllib.load(f)["tool"]["setuptools"]["package-data"]["dct"]
    pkg = REPO_ROOT / "dct"
    assert any(version_sync_template_path() in set(pkg.glob(p)) for p in patterns)
    assert "graft dct/assets" in (REPO_ROOT / "MANIFEST.in").read_text()
