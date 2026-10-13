"""One changelog-exemption policy for both changelog hooks (K-59).

The blocking commit hook (`check-changelog.sh`) and the Stop reminder
(`check-changelog-on-stop.sh`) must agree on which changed files need a
changelog entry: a doc/meta file exempt in one and not the other makes the
Stop hook nag about a change the commit hook lets through (or the reverse).
The predicate lives once, in `dct-changelog-count.lib.sh`; these tests drive
BOTH hooks on the same changed paths and compare their decisions.

The `dct` on PATH is a fake that logs its argv: the project is managed and
has no unreleased entry, so a hook that sees app code denies (commit, exit 2)
or reminds (Stop). An exempt-only change must pass both — and the Stop hook
must still decide it without starting `dct` at all (the zero-Python fast path).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git, make_git_repo, require_tools

from dct.integrations.claude import HOOKS_DIR

pytestmark = pytest.mark.slow

COMMIT_HOOK = HOOKS_DIR / "check-changelog.sh"
STOP_HOOK = HOOKS_DIR / "check-changelog-on-stop.sh"
LIB = HOOKS_DIR / "dct-changelog-count.lib.sh"

_COMMIT_PAYLOAD = json.dumps({"tool_input": {"command": 'git commit -m "feat: x"'}})


@pytest.fixture(autouse=True)
def _hook_dependencies():
    """Skip locally, FAIL under CI when a tool the hooks need is missing."""
    require_tools("bash", "git", "jq")


def _fake_dct(tmp_path: Path) -> Path:
    """A logging `dct`: managed project proj-a with zero unreleased entries."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = bin_dir / "argv.log"
    body = (
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        'case "$1 $2" in\n'
        '  "project detect") echo proj-a ;;\n'
        '  "project manages-changelog") exit 0 ;;\n'
        '  "clog count") echo 0 ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    # Created executable in one step (no separate mode change).
    fd = os.open(bin_dir / "dct", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return bin_dir


def _env(tmp_path: Path, bin_dir: Path) -> dict:
    cache = tmp_path / "cache"
    cache.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    path = os.pathsep.join((str(bin_dir), str(Path(sys.executable).parent), env.get("PATH", "")))
    return {**env, "TMPDIR": str(cache), "PATH": path}


def _staged_repo(tmp_path: Path, paths: list[str]) -> Path:
    root = Path(make_git_repo(tmp_path / "proj", branch="feat/x"))
    for rel in paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    git(root, "add", "-A")
    return root


def _decide(tmp_path: Path, paths: list[str]) -> tuple[bool, bool, list[str]]:
    """(commit hook wants an entry, Stop hook wants an entry, Stop's dct calls)."""
    bin_dir = _fake_dct(tmp_path)
    env = _env(tmp_path, bin_dir)
    repo = _staged_repo(tmp_path, paths)
    commit = subprocess.run(["bash", str(COMMIT_HOOK)], cwd=repo, input=_COMMIT_PAYLOAD,
                            capture_output=True, text=True, env=env, timeout=60)
    assert commit.returncode in (0, 2), commit.stderr
    log = bin_dir / "argv.log"
    if log.exists():
        os.replace(log, bin_dir / "argv.commit.log")
    stop = subprocess.run(["bash", str(STOP_HOOK)], cwd=repo, input="{}",
                          capture_output=True, text=True, env=env, timeout=60)
    assert stop.returncode == 0, stop.stderr
    stop_calls = log.read_text().splitlines() if log.exists() else []
    return commit.returncode == 2, "Reminder" in stop.stdout, stop_calls


# Each case: changed paths → does the change need a changelog entry?
_CASES = [
    # Project instruction files — AGENTS.md is the canonical one (K-52);
    # CLAUDE.md / CLAUDE.local.md stay exempt for other dct users' projects.
    (["AGENTS.md"], False),
    (["CLAUDE.md"], False),
    (["CLAUDE.local.md"], False),
    (["sub/AGENTS.md"], False),
    (["sub/CLAUDE.local.md"], False),
    # The rest of the doc/meta list, one representative per pattern.
    ([".claude/settings.json"], False),
    ([".github/workflows/ci.yml"], False),
    ([".cursor/rules"], False),
    ([".vscode/settings.json"], False),
    ([".gstack/x"], False),
    (["docs/guide.md"], False),
    (["scripts/tool.py"], False),
    (["tests/test_a.py"], False),
    (["test_top.py"], False),
    (["pkg/conftest.py"], False),
    (["README.md"], False),
    (["LICENSE"], False),
    ([".gitignore"], False),
    (["VERSION"], False),
    (["pyproject.toml"], False),
    (["Cargo.toml"], False),
    (["package.json"], False),
    (["biome.json"], False),
    (["Dockerfile"], False),
    (["docker-compose.yml"], False),
    # Application code.
    (["app.py"], True),
    (["dct/core/x.py"], True),
    (["dct/hooks/x.sh"], True),
    (["AGENTS.md.py"], True),
    # Mixes: documentation alone passes, documentation plus code does not.
    (["docs/a.md", "AGENTS.md", "CLAUDE.local.md", "tests/test_b.py"], False),
    (["docs/a.md", "AGENTS.md", "app.py"], True),
    (["CLAUDE.local.md", "biome.json", "dct/x.py"], True),
]


@pytest.mark.parametrize(("paths", "needs_entry"), _CASES,
                         ids=["+".join(c[0]) for c in _CASES])
def test_both_hooks_apply_the_same_exemption(tmp_path, paths, needs_entry):
    commit_wants, stop_wants, stop_calls = _decide(tmp_path, paths)
    assert (commit_wants, stop_wants) == (needs_entry, needs_entry)
    if not needs_entry:
        # Exempt-only change: the Stop hook decides without starting dct.
        assert stop_calls == []


def test_the_exemption_list_lives_only_in_the_shared_lib():
    """Neither hook carries its own copy of the list (the drift K-59 fixed)."""
    for hook in (COMMIT_HOOK, STOP_HOOK):
        src = hook.read_text()
        assert "exempt_re=" not in src and "exempt_patterns=" not in src, hook.name
        assert "dct_changelog_app_files" in src, hook.name
    assert "dct_changelog_app_files()" in LIB.read_text()
