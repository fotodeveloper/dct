"""Read-only runtime diagnostics, including editable dependency drift.

No engine is bootstrapped: diagnosing an installation must not migrate its
database. Each Python is queried separately rather than assuming the CLI and
the MCP server use the same environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

# Only standard-library modules run in the target interpreter; this probe must
# still diagnose an environment whose dependencies are missing or stale.
_PROBE = r'''
import importlib.metadata as m, json, platform, sys, tomllib
from pathlib import Path
from urllib.parse import urlparse, unquote
d = m.distribution("dctracker")
requirements = d.requires or []
python = d.metadata.get("Requires-Python", ">=3.11")
source = "installed metadata"
direct = json.loads(d.read_text("direct_url.json") or "{}")
project = None
if direct.get("dir_info", {}).get("editable"):
    url = urlparse(direct["url"])
    if url.scheme == "file":
        path = unquote(url.path)
        if sys.platform == "win32" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        project = Path(path) / "pyproject.toml"
        if not project.is_file():
            raise RuntimeError("Editable source pyproject.toml is missing")
if project and project.is_file():
    data = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
    if data.get("name") == "dctracker":
        requirements = list(data.get("dependencies", []))
        for extra, deps in data.get("optional-dependencies", {}).items():
            for dep in deps:
                base, sep, marker = dep.partition(";")
                requirements.append(base + "; " + ("(" + marker.strip() + ") and " if sep else "") + "extra == " + repr(extra))
        python = data.get("requires-python", ">=3.11")
        source = str(project)
versions = {x.metadata["Name"].lower().replace("_", "-"): x.version for x in m.distributions() if x.metadata["Name"]}
print(json.dumps({"version": d.version, "requirements": requirements,
    "requires_python": python, "source": source, "versions": versions,
    "environment": {"python_version": platform.python_version_tuple()[0] + "." + platform.python_version_tuple()[1],
        "python_full_version": platform.python_version(), "sys_platform": sys.platform,
        "os_name": __import__("os").name, "platform_machine": platform.machine(),
        "platform_system": platform.system(), "platform_release": platform.release(),
        "platform_version": platform.version(), "platform_python_implementation": platform.python_implementation(),
        "implementation_name": sys.implementation.name,
        "implementation_version": platform.python_version()}}))
'''


def evaluate_runtime(data: dict, executable: str) -> list[dict]:
    """Compare installed versions to the target's current dependency contract."""
    checks = []
    env = {**default_environment(), **data["environment"]}
    version = env["python_full_version"]
    supported = SpecifierSet(data["requires_python"]).contains(version, prereleases=True)
    checks.append({"check": "python", "status": "ok" if supported else "error",
                   "message": f"{executable}: Python {version}; requires {data['requires_python']}"})
    versions = data["versions"]
    normalized_versions = {canonicalize_name(k): v for k, v in versions.items()}
    for raw in data["requirements"]:
        requirement = Requirement(raw)
        # Core + MCP are required for integration. Check optional installed
        # database/web providers as well, but never require dev/test extras.
        extras = ["", "mcp"]
        if "psycopg" in versions: extras.append("pg")
        if "flask" in versions: extras.append("web")
        if requirement.marker and not any(
            requirement.marker.evaluate({**env, "extra": extra}) for extra in extras
        ):
            continue
        installed = normalized_versions.get(canonicalize_name(requirement.name))
        ok = installed is not None and requirement.specifier.contains(installed, prereleases=True)
        checks.append({"check": f"dependency:{requirement.name}", "status": "ok" if ok else "error",
                       "message": f"{executable}: {requirement.name} {installed or 'missing'}; requires {requirement.specifier or 'any version'} ({data['source']})"})
    return checks


def runtime_checks(executable: str, environment: dict | None = None) -> list[dict]:
    try:
        proc = subprocess.run([executable, "-I", "-c", _PROBE], capture_output=True,
                              env={**os.environ, **(environment or {})},
                              text=True, timeout=15, check=False)
        if proc.returncode:
            # Do not echo arbitrary startup stderr (may contain local secrets).
            raise ValueError("runtime probe failed; verify dctracker is installed and its editable source exists")
        return evaluate_runtime(json.loads(proc.stdout), executable)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        return [{"check": "runtime", "status": "error", "message": f"{executable}: {exc}"}]


def configured_runtimes(client: str | None) -> tuple[list[tuple[str, dict]], list[dict]]:
    """Discover direct Python entrypoints from each client's actual MCP config.

    Shell/package-manager launch wrappers require an explicit --python probe.
    Never execute an arbitrary wrapper just to discover what it would launch.
    """
    from dct.integrations import claude
    from dct.integrations.codex import CodexIntegration

    runtimes, checks = [], []
    for name in ([client] if client else ["claude", "codex"]):
        try:
            if name == "codex":
                path = CodexIntegration().config_path
                data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                entry = data.get("mcp_servers", {}).get("dct")
            else:
                path = claude.CLAUDE_JSON
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                entry = data.get("mcpServers", {}).get("dct")
            if not isinstance(entry, dict):
                continue  # adapter diagnostics report absent/malformed entries
            env = entry.get("env", {})
            if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
                raise ValueError("MCP environment must contain strings")
            command = entry.get("command")
            resolved = shutil.which(command, path=env.get("PATH", os.environ.get("PATH"))) if isinstance(command, str) else None
            python = None
            if resolved:
                if Path(resolved).name.lower().startswith("python"):
                    python = resolved
                else:
                    with open(resolved, "rb") as stream:
                        line = stream.readline(4096).decode("utf-8").strip()
                    if line.startswith("#!") and Path(line[2:]).is_file() and "python" in Path(line[2:]).name.lower():
                        python = line[2:]
            if python:
                runtimes.append((python, env))
            else:
                checks.append({"check": f"runtime:{name}", "status": "warning",
                               "message": "Configured MCP runtime cannot be inferred safely; use --python for its environment"})
            if any(key in env for key in ("PYTHONPATH", "PYTHONHOME")):
                checks.append({"check": f"runtime-environment:{name}", "status": "warning",
                               "message": "Custom Python import paths are configured; dependency probes use isolated Python and do not certify this launch environment"})
        except (OSError, ValueError, AttributeError, TypeError):
            checks.append({"check": f"runtime:{name}", "status": "error",
                           "message": "Cannot inspect configured MCP executable/environment"})
    return runtimes, checks


def discover_pythons(extra: list[str] = ()) -> list[str]:
    """Probe this runtime and a directly readable PATH entrypoint interpreter.

    Custom wrappers cannot safely be guessed. --python provides an explicit
    probe for those environments; no arbitrary wrapper command is executed.
    """
    candidates = [sys.executable, *extra]
    command = shutil.which("dct")
    if command:
        try:
            with open(command, "rb") as stream:
                line = stream.readline(4096).decode("utf-8").strip()
            if line.startswith("#!"):
                interpreter = line[2:]
                if Path(interpreter).is_file() and "python" in Path(interpreter).name.lower():
                    candidates.append(interpreter)
        except (OSError, UnicodeError):
            pass
    # Do NOT realpath Python symlinks: two venvs share a binary but have distinct
    # site-packages, which is precisely the drift this diagnostic must detect.
    return list(dict.fromkeys(os.path.abspath(p) if Path(p).is_file() else p for p in candidates))


def doctor(client: str | None = None, pythons: list[str] = ()) -> dict:
    from dct.config import load_config
    from dct.integrations.manager import get_integration, integration_status

    checks = []
    try:
        load_config()
        checks.append({"check": "config", "status": "ok", "message": "DCT configuration parsed; database was not opened or migrated"})
    except (OSError, ValueError) as exc:
        checks.append({"check": "config", "status": "error", "message": f"Cannot read DCT configuration ({type(exc).__name__})"})
    configured, extra_checks = configured_runtimes(client)
    checks.extend(extra_checks)
    candidates = [(p, {}) for p in discover_pythons(pythons)] + configured
    seen = set()
    for executable, environment in candidates:
        key = (executable, tuple(sorted(environment.items())))
        if key not in seen:
            checks.extend(runtime_checks(executable, environment))
            seen.add(key)
    integrations = []
    for name in ([client] if client else ["claude", "codex"]):
        adapter = get_integration(name)
        if client or adapter.detect():
            integrations.append(integration_status(adapter))
    return {"ok": not any(c["status"] == "error" for c in checks) and all(i["ok"] for i in integrations),
            "checks": checks, "integrations": integrations}
