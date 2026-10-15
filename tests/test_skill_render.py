"""Workflow rendering boundaries: fail closed, shared semantics and no writes."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from dct.skill_render import (
    check_freshness,
    freshness_errors,
    render_all,
    skill_names,
    validate_frontmatter,
)


@pytest.fixture
def source_tree(tmp_path):
    source = tmp_path / "dct/skill_sources"
    source.mkdir(parents=True)
    manifest = 'version = 1\n'
    for host in ("claude", "codex", "fallback"):
        manifest += f'[hosts.{host}]\ndisplay_name = "{host}"\ncapabilities = []\n'
        folder = source / f"hosts/{host}"
        folder.mkdir(parents=True)
        (folder / "presentation.md").write_text(f"Presentation: {host}")
    manifest += '\n[[skills]]\nname = "action"\ndescription = "Use for a requested action: \\"quoted\\"."\nexplicit_only = true\nbody = "workflows/action.md"\nentry_tool = "perform_action"\n'
    manifest += 'short_description = "Initial action description placeholder"\n'
    (source / "manifest.toml").write_text(manifest)
    (source / "workflows").mkdir()
    (source / "workflows/action.md").write_text("{{shared.rule}}\n\n{{host.presentation}}")
    (source / "shared").mkdir()
    (source / "shared/rule.md").write_text("Never write without explicit approval.")
    (source / "shared/reference.md").write_text("# Reference\n{{inventory}}\n{{workflows}}")
    (source / "hosts/fallback/header.md").write_text("# Fallback")
    (source / "hosts/fallback/footer.md").write_text("Keep returned identifiers.")
    return tmp_path


def _save_outputs(root):
    for relative, text in render_all(root).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def test_shared_domain_edit_reaches_both_hosts_reference_and_fallback(source_tree):
    """A policy edit cannot leave any consumer using the old contract."""
    before = render_all(source_tree)
    (source_tree / "dct/skill_sources/shared/rule.md").write_text("Approval must name the exact changed files.")
    after = render_all(source_tree)
    targets = ("dct/skills/action/SKILL.md", "dct/assets/codex-action.md", "dct/assets/dct-reference.md", "docs/assistant-fallbacks.md")
    assert all(before[path] != after[path] and "Approval must name" in after[path] for path in targets)
    assert "Presentation: claude" in after[targets[0]]
    assert "Presentation: codex" in after[targets[1]]
    assert "Presentation: fallback" in after[targets[2]]


@pytest.mark.parametrize("broken", ["{{unknown.rule}}", "{{host.missing}}", "{{shared.../rule}}", "{{shared.rule", "{{host.presentation} }"])
def test_unknown_missing_or_malformed_fragments_refuse_generation(source_tree, broken):
    (source_tree / "dct/skill_sources/workflows/action.md").write_text(broken)
    with pytest.raises(ValueError):
        render_all(source_tree)


def test_fragment_cycles_refuse_instead_of_recursing_forever(source_tree):
    (source_tree / "dct/skill_sources/shared/rule.md").write_text("{{shared.rule}}")
    with pytest.raises(ValueError, match="Cyclic"):
        render_all(source_tree)


def test_symlink_fragment_cannot_escape_the_authored_source_tree(source_tree, tmp_path):
    outside = source_tree / "outside.md"
    outside.write_text("Foreign policy")
    fragment = source_tree / "dct/skill_sources/shared/external.md"
    fragment.symlink_to(outside)
    (source_tree / "dct/skill_sources/workflows/action.md").write_text("{{shared.external}}")
    with pytest.raises(ValueError, match="leaves"):
        render_all(source_tree)


@pytest.mark.parametrize("change", ["duplicate", "traversal", "output", "wrong_boolean"])
def test_manifest_rejects_duplicate_names_and_unfixed_paths(source_tree, change):
    path = source_tree / "dct/skill_sources/manifest.toml"
    text = path.read_text()
    if change == "duplicate":
        text += text[text.index("[[skills]]"):]
    elif change == "traversal":
        text = text.replace('name = "action"', 'name = "../action"')
    elif change == "output":
        text = text.replace('body = "workflows/action.md"', 'body = "../../foreign.md"')
    else:
        text = text.replace('explicit_only = true', 'explicit_only = "true"')
    path.write_text(text)
    with pytest.raises(ValueError):
        render_all(source_tree)


def test_native_frontmatter_and_explicit_invocation_policy_are_host_correct(source_tree):
    rendered = render_all(source_tree)
    description = 'Use for a requested action: "quoted".'
    for host, path in (("claude", "dct/skills/action/SKILL.md"), ("codex", "dct/assets/codex-action.md")):
        text = rendered[path]
        validate_frontmatter(text, name="action", description=description, explicit_only=True, host=host)
        assert text.startswith("---\n")
        assert text.index("DO NOT EDIT") > text.index("\n---", 4)
    assert "allow_implicit_invocation: false" in rendered["dct/assets/codex-action.yaml"]
    with pytest.raises(ValueError, match="Wrong-host"):
        validate_frontmatter(rendered["dct/skills/action/SKILL.md"], name="action", description=description, explicit_only=True, host="codex")


def test_freshness_reports_all_failures_without_changing_any_bytes(source_tree):
    _save_outputs(source_tree)
    assert freshness_errors(source_tree) == []
    stale = source_tree / "dct/assets/codex-action.md"
    stale.write_text("Owner edit")
    missing = source_tree / "dct/assets/codex-action.yaml"
    missing.rename(source_tree / "metadata-kept.yaml")
    extra = source_tree / "dct/assets/codex-retired.md"
    extra.write_text("Old generated workflow")
    before = {p: p.read_bytes() for p in source_tree.rglob("*") if p.is_file()}
    errors = freshness_errors(source_tree)
    assert "stale: dct/assets/codex-action.md" in errors
    assert "missing: dct/assets/codex-action.yaml" in errors
    assert "unexpected: dct/assets/codex-retired.md" in errors
    with pytest.raises(ValueError, match="not fresh"):
        check_freshness(source_tree)
    assert {p: p.read_bytes() for p in source_tree.rglob("*") if p.is_file()} == before


def test_inventory_comes_from_manifest_and_renderer_does_not_write(source_tree):
    before = {p: p.read_bytes() for p in source_tree.rglob("*") if p.is_file()}
    assert skill_names(source_tree) == ("action",)
    assert len(render_all(source_tree)) == 5
    assert {p: p.read_bytes() for p in source_tree.rglob("*") if p.is_file()} == before


def test_reference_cannot_silently_omit_canonical_workflow_inventory(source_tree):
    (source_tree / "dct/skill_sources/shared/reference.md").write_text("Only a static guide")
    with pytest.raises(ValueError, match="canonical workflow inventory"):
        render_all(source_tree)


def _build_fixture(root):
    repository = Path(__file__).resolve().parent.parent
    shutil.copyfile(repository / "setup.py", root / "setup.py")
    shutil.copyfile(repository / "dct/skill_render.py", root / "dct/skill_render.py")
    (root / "dct/__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "workflow-fixture"\nversion = "0.1.0"\n'
        '[tool.setuptools]\ninclude-package-data = true\n'
        '[tool.setuptools.packages.find]\ninclude = ["dct*"]\n'
        '[tool.setuptools.package-data]\ndct = ["skills/**/*.md", "assets/*.md", "assets/*.yaml", "skill_sources/*.toml", "skill_sources/**/*.md"]\n'
    )
    (root / "MANIFEST.in").write_text('graft dct\ninclude docs/assistant-fallbacks.md\n')
    _save_outputs(root)


def _run_build(root, command):
    environment = dict(os.environ)
    # Use only already-provided build dependencies; never invoke pip/network.
    result = subprocess.run([sys.executable, "setup.py", command], cwd=root, env=environment, capture_output=True, text=True, check=False)
    return result


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("setuptools") is None, reason="An existing setuptools build runtime is required; tests never install it")
@pytest.mark.parametrize("command", ["sdist", "bdist_wheel", "editable_wheel"])
def test_every_package_build_refuses_stale_outputs_before_artifact_creation(source_tree, command):
    """Builds outside CI must not ship stale workflow policy or rewrite it."""
    _build_fixture(source_tree)
    stale = source_tree / "dct/assets/codex-action.md"
    stale.write_text("Owner changed the generated file")
    result = _run_build(source_tree, command)
    assert result.returncode != 0
    assert "not fresh" in result.stderr
    assert stale.read_text() == "Owner changed the generated file"
    assert not list((source_tree / "dist").glob("*.whl"))
    assert not list((source_tree / "dist").glob("*.tar.gz"))


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("setuptools") is None, reason="An existing setuptools build runtime is required; tests never install it")
def test_sdist_to_wheel_build_is_offline_and_contains_inputs_metadata_and_outputs(source_tree, tmp_path):
    """A checkout-free wheel built from sdist retains regeneration/installation inputs."""
    _build_fixture(source_tree)
    result = _run_build(source_tree, "sdist")
    assert result.returncode == 0, result.stdout + result.stderr
    archive = next((source_tree / "dist").glob("*.tar.gz"))
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(archive) as handle:
        handle.extractall(unpacked, filter="data")
    built = next(unpacked.iterdir())
    assert not (built / ".git").exists()
    result = _run_build(built, "bdist_wheel")
    assert result.returncode == 0, result.stdout + result.stderr
    wheel = next((built / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as handle:
        names = set(handle.namelist())
        required = {
            "dct/skills/action/SKILL.md", "dct/assets/codex-action.md", "dct/assets/codex-action.yaml",
            "dct/assets/dct-reference.md", "dct/skill_sources/manifest.toml",
            "dct/skill_sources/workflows/action.md", "dct/skill_sources/shared/rule.md",
            "dct/skill_sources/hosts/codex/presentation.md",
        }
        assert required <= names
        assert handle.read("dct/assets/codex-action.yaml").decode().endswith("allow_implicit_invocation: false\n")
    assert freshness_errors(built) == []


def test_fallback_heading_composition_preserves_code_and_section_boundaries(source_tree):
    body = source_tree / 'dct/skill_sources/workflows/action.md'
    body.write_text('# Action\n## Rules\n```md\n## Literal heading\n```\n~~~\n# More code\n~~~\n## Recovery\n')
    fallback = render_all(source_tree)['docs/assistant-fallbacks.md']
    assert '## `/action` fallback\n\n### Action\n#### Rules\n' in fallback
    assert '```md\n## Literal heading\n```' in fallback
    assert '~~~\n# More code\n~~~' in fallback
    assert '#### Recovery' in fallback


def test_nested_json_examples_survive_fragment_rendering(source_tree):
    """Adjacent JSON closing braces are data, never a source-token error."""
    example = '{by, date, answers: {"K-n": {dec,opt,note}}}'
    (source_tree / 'dct/skill_sources/workflows/action.md').write_text('{{shared.rule}}\n' + example)
    rendered = render_all(source_tree)
    assert all(example in text for path, text in rendered.items() if path.endswith('.md') and 'reference' not in path)


@pytest.mark.parametrize('opening,closing', [('1. ```', '   ```'), ('  10. ~~~', '      ~~~'), ('    ```', '    ```')])
def test_fallback_nested_list_fences_keep_code_and_following_workflow_headings(opening, closing):
    """A list fence must not turn its closing marker into a new open block."""
    from dct.skill_render import _fallback_headings
    body = '# Workflow\n' + opening + '\n# Code heading must stay literal\n' + closing + '\n### Final step\n## Rules\n'
    result = _fallback_headings(body)
    assert '# Code heading must stay literal' in result
    assert '\n##### Final step\n#### Rules' in result


def test_picker_description_comes_from_authorized_manifest_use_case(source_tree):
    """The picker must display the authored purpose rather than reconstruct a name label."""
    path = source_tree / 'dct/skill_sources/manifest.toml'
    path.write_text(path.read_text().replace('Initial action description placeholder', 'Run the explicitly requested action'))
    metadata = render_all(source_tree)['dct/assets/codex-action.yaml']
    assert 'short_description: "Run the explicitly requested action"' in metadata


@pytest.mark.parametrize('length,valid', [(24, False), (25, True), (64, True), (65, False)])
def test_picker_description_respects_documented_character_bounds(source_tree, length, valid):
    """Native short descriptions are 25–64 characters, not a display-width heuristic."""
    path = source_tree / 'dct/skill_sources/manifest.toml'
    path.write_text(path.read_text().replace('Initial action description placeholder', 'x' * length))
    if valid:
        render_all(source_tree)
    else:
        with pytest.raises(ValueError, match='short_description'):
            render_all(source_tree)
