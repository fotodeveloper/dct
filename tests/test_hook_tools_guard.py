"""The shared missing-tool guard of the hook suites (K-45, K-65).

conftest.require_tools must SKIP locally and FAIL under CI — otherwise a CI
runner without jq turns every hook module silently green.
"""

import shutil

import pytest
from conftest import require_tools


def _without(monkeypatch, *absent):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda tool, *a, **k: None if tool in absent else real(tool))


def test_missing_tool_skips_locally(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    _without(monkeypatch, "jq")
    with pytest.raises(pytest.skip.Exception, match="jq required"):
        require_tools("bash", "jq")


def test_missing_tool_fails_under_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    _without(monkeypatch, "jq")
    with pytest.raises(pytest.fail.Exception, match=r"CI is set but \['jq'\] missing"):
        require_tools("bash", "jq")


def test_present_tools_pass_under_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(shutil, "which", lambda tool, *a, **k: f"/usr/bin/{tool}")
    require_tools("bash", "jq", "git")


@pytest.mark.parametrize("module", [
    "test_hook_changelog_branch", "test_hook_bypass", "test_hook_plan_autoscan",
    "test_codex_hooks", "test_hook_task_mirror",
])
def test_every_jq_hook_module_uses_the_shared_guard(module):
    from pathlib import Path

    source = (Path(__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    assert "require_tools" in source or "hook_tools" in source
    assert 'which("jq")' not in source and "which('jq')" not in source
    assert "'jq')):" not in source  # the old ad-hoc all(shutil.which(...)) skip
