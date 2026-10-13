"""Integration commands must not open the database or hide broken installs."""

import json
import sys

import pytest

from dct import cli
from dct.integrations import doctor as diagnostics


def test_doctor_detects_editable_requirement_drift():
    data = {"requires_python": ">=3.11", "source": "pyproject.toml",
            "environment": {"python_version": "3.11", "python_full_version": "3.11.9"},
            "versions": {"sqlalchemy": "2.0.1", "mcp": "1.28.1"},
            "requirements": ["sqlalchemy>=2", 'mcp>=2,<3; extra == "mcp"',
                             'pytest; extra == "dev"']}
    checks = diagnostics.evaluate_runtime(data, "test-python")
    failed = [c for c in checks if c["status"] == "error"]
    assert len(failed) == 1
    assert failed[0]["check"] == "dependency:mcp"
    assert "1.28.1" in failed[0]["message"]
    assert "pyproject.toml" in failed[0]["message"]


def test_doctor_uses_target_python_markers():
    data = {"requires_python": ">=3.11", "source": "metadata",
            "environment": {"python_version": "3.11", "python_full_version": "3.11.9", "sys_platform": "win32"},
            "versions": {}, "requirements": ['platformdirs>=4.9.6; sys_platform == "win32"']}
    assert any(c["check"] == "dependency:platformdirs" and c["status"] == "error"
               for c in diagnostics.evaluate_runtime(data, "windows-python"))


def test_doctor_does_not_collapse_venv_symlinks(tmp_path, monkeypatch):
    first, second = tmp_path / "first-python", tmp_path / "second-python"
    first.symlink_to(sys.executable)
    second.symlink_to(sys.executable)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)
    found = diagnostics.discover_pythons([str(first), str(second)])
    assert str(first) in found and str(second) in found


@pytest.mark.slow
def test_doctor_missing_runtime_is_actionable():
    checks = diagnostics.runtime_checks("/nonexistent/dct-test-python")
    assert checks[0]["status"] == "error"


def test_doctor_probes_configured_mcp_environment(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / "codex"
    home.mkdir()
    executable = tmp_path / "custom-dct"
    executable.write_text(f"#!{sys.executable}\n")
    executable.chmod(0o755)
    # JSON quoting is valid TOML basic-string syntax for this temp path.
    (home / "config.toml").write_text(
        f"[mcp_servers.dct]\ncommand = {json.dumps(str(executable))}\n"
        '[mcp_servers.dct.env]\nDCT_ACTOR_CLIENT = "codex"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtimes, checks = diagnostics.configured_runtimes("codex")
    assert runtimes == [(sys.executable, {"DCT_ACTOR_CLIENT": "codex"})]
    assert not checks


@pytest.mark.slow
def test_runtime_probe_cannot_import_cwd_checkout(tmp_path, monkeypatch):
    # A caller-controlled checkout must not shadow the package in another venv.
    (tmp_path / "dct").mkdir()
    (tmp_path / "dct" / "__init__.py").write_text('raise RuntimeError("wrong package")')
    (tmp_path / "pyproject.toml").write_text('[project]\nname="dctracker"\ndependencies=["fictional-dependency"]')
    monkeypatch.chdir(tmp_path)
    checks = diagnostics.runtime_checks(sys.executable)
    assert not any(c["check"] == "dependency:fictional-dependency" for c in checks)
    assert checks[0]["check"] == "python"


def test_doctor_json_failure_never_bootstraps_database(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_engine", lambda: pytest.fail("doctor opened database"))
    monkeypatch.setattr(diagnostics, "doctor", lambda *args: {
        "ok": False, "checks": [{"check": "runtime", "status": "error", "message": "missing MCP"}],
        "integrations": []})
    args = cli.build_parser().parse_args(["doctor", "--json", "--python", "other-python"])
    with pytest.raises(SystemExit) as error:
        cli.cmd_doctor(args)
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_install_json_keeps_legacy_prints_off_stdout(monkeypatch, capsys):
    from dct.integrations import manager

    class Adapter:
        name = "codex"
        def detect(self): return True
        def doctor(self): return [{"check": "mcp", "status": "ok", "message": "ready"}]
        def install_mcp(self):
            print("installer note")
            return {"status": "ok"}
        def install_skills(self): return {"status": "ok"}
        def install_instructions(self): return {"status": "ok"}

    monkeypatch.setattr(manager, "get_integration", lambda name: Adapter())
    monkeypatch.setattr(cli, "_get_engine", lambda: pytest.fail("installer opened database"))
    cli.cmd_integrate(cli.build_parser().parse_args(["integrate", "install", "codex", "--json"]))
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is True
    assert "installer note" in output.err


def test_install_stops_after_failed_step():
    from dct.integrations.manager import install_integration

    class Adapter:
        name = "codex"
        def install_mcp(self): raise ValueError("malformed config")
        def install_skills(self): pytest.fail("continued after failed MCP installation")

    with pytest.raises(ValueError, match="malformed config"):
        install_integration(Adapter())
