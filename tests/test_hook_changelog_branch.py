"""Per-branch changelog count in the commit hook and the Stop hook (#801, C7).

Two kinds of `dct` on PATH:
  * the REAL CLI of this tree (PYTHONPATH) against a per-test throwaway SQLite
    database — proves the correct zero / the unbranched rule end to end;
  * a FAKE `dct` shell script — an old CLI that rejects --branch (version
    skew → project-wide fallback) and a broken one (fail-open for the blocking
    hook, keep-reminding for the Stop hook).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core.changelog import add_entry
from dct.core.projects import create_project
from dct.db import create_tables, get_engine
from dct.integrations.claude import HOOKS_DIR

COMMIT_HOOK = HOOKS_DIR / "check-changelog.sh"
STOP_HOOK = HOOKS_DIR / "check-changelog-on-stop.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("jq") is None,
    reason="git and jq required by the hooks",
)

_COMMIT_PAYLOAD = json.dumps({"tool_input": {"command": 'git commit -m "feat: x"'}})


@pytest.fixture
def repo(tmp_path):
    """A repo on branch feat/x with an app file staged (and changed)."""
    root = Path(make_git_repo(tmp_path / "proj", branch="feat/x"))
    (root / "app.py").write_text("x = 1\n")
    git(root, "add", "app.py")
    return root


def _env(tmp_path, bin_dir: Path | None = None) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    db = f"sqlite:///{home / 'dct.db'}"
    path = os.pathsep.join(
        p for p in (str(bin_dir) if bin_dir else "", str(Path(sys.executable).parent),
                    os.environ.get("PATH", "")) if p)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**env, "HOME": str(home), "TMPDIR": str(cache), "PATH": path,
            "DCT_DATABASE_URL": db, "DCT_CONFIG_PATH": str(home / "dct.toml"),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}


def _seed(env: dict, repo: Path, entries: list[str | None]):
    """Register the repo as proj-a in the hook's database, add one unreleased
    entry per branch value (None = unbranched)."""
    engine = get_engine(env["DCT_DATABASE_URL"])
    create_tables(engine)
    create_project(engine, slug="proj-a", name="A", path=str(repo))
    for branch in entries:
        add_entry(engine, "proj-a", "added", "e", branch=branch)
    engine.dispose()


def _fake_dct(tmp_path, count_script: str) -> Path:
    """A `dct` executable on a private bin dir. project detect → proj-a,
    manages-changelog → yes; `clog count` runs `count_script` (bash)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    body = (
        "#!/bin/bash\n"
        'case "$1 $2" in\n'
        '  "project detect") echo proj-a ;;\n'
        '  "project manages-changelog") exit 0 ;;\n'
        f'  "clog count") {count_script} ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    # Created executable in one step (no separate mode change).
    fd = os.open(bin_dir / "dct", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return bin_dir


_OLD_CLI = (
    'case " $* " in *" --branch "*) '
    'echo "dct: error: unrecognized arguments: --branch" >&2; exit 2 ;; esac; echo 3'
)
_BROKEN_CLI = 'echo "database is down" >&2; exit 1'


def _commit_hook(repo: Path, env: dict):
    return subprocess.run(["bash", str(COMMIT_HOOK)], cwd=repo, input=_COMMIT_PAYLOAD,
                          capture_output=True, text=True, env=env, timeout=60)


def _stop_hook(repo: Path, env: dict):
    return subprocess.run(["bash", str(STOP_HOOK)], cwd=repo, input="{}",
                          capture_output=True, text=True, env=env, timeout=60)


# ── blocking commit hook ──────────────────────────────────────────────────────


def test_commit_denied_when_only_other_branches_have_entries(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other", "main"])
    r = _commit_hook(repo, env)
    assert r.returncode == 2, r.stderr
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "on branch 'feat/x'" in reason
    assert "other branches do not count" in reason
    assert "dct clog update <id> --branch feat/x" in reason


def test_commit_allowed_by_an_entry_on_this_branch(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other", "feat/x"])
    assert _commit_hook(repo, env).returncode == 0


def test_commit_allowed_by_an_unbranched_entry(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other", None])
    assert _commit_hook(repo, env).returncode == 0


def test_commit_denied_with_no_entries_at_all(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, [])
    assert _commit_hook(repo, env).returncode == 2


def test_commit_deny_json_survives_a_quote_in_the_branch_name(tmp_path):
    root = Path(make_git_repo(tmp_path / "proj", branch='feat/a"b'))
    (root / "app.py").write_text("x = 1\n")
    git(root, "add", "app.py")
    env = _env(tmp_path)
    _seed(env, root, ["main"])
    r = _commit_hook(root, env)
    assert r.returncode == 2
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "feat/a\"b" in reason


def test_commit_on_detached_head_uses_the_project_count(repo, tmp_path):
    """No branch → the hook never passes an empty --branch; it counts the
    whole project (here: an entry of another branch satisfies it)."""
    git(repo, "commit", "-q", "-m", "wip")  # commit the staged file first
    git(repo, "checkout", "-q", "--detach")
    (repo / "app.py").write_text("x = 2\n")
    git(repo, "add", "app.py")
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other"])
    assert _commit_hook(repo, env).returncode == 0


def test_commit_old_cli_rejecting_branch_falls_back_to_project_count(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, _OLD_CLI))
    r = _commit_hook(repo, env)
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_commit_old_cli_with_zero_project_count_still_denies(repo, tmp_path):
    old_zero = _OLD_CLI.replace("echo 3", "echo 0")
    env = _env(tmp_path, _fake_dct(tmp_path, old_zero))
    assert _commit_hook(repo, env).returncode == 2


def test_commit_fails_open_when_dct_count_is_broken(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, _BROKEN_CLI))
    r = _commit_hook(repo, env)
    assert r.returncode == 0
    assert r.stdout == ""


def test_commit_non_numeric_count_output_fails_open(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, 'echo "Traceback (most recent call last)"'))
    assert _commit_hook(repo, env).returncode == 0


# ── non-blocking Stop hook ────────────────────────────────────────────────────


def test_stop_reminds_when_only_other_branches_have_entries(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other"])
    r = _stop_hook(repo, env)
    assert r.returncode == 0
    assert "Reminder" in r.stdout


def test_stop_silent_when_this_branch_or_unbranched_has_entries(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, [None])
    r = _stop_hook(repo, env)
    assert r.returncode == 0
    assert "Reminder" not in r.stdout


def test_stop_old_cli_falls_back_to_project_count(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, _OLD_CLI))
    r = _stop_hook(repo, env)
    assert r.returncode == 0
    assert "Reminder" not in r.stdout


def test_stop_keeps_reminding_when_dct_count_is_broken(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, _BROKEN_CLI))
    r = _stop_hook(repo, env)
    assert r.returncode == 0
    assert "Reminder" in r.stdout and "proj-a" in r.stdout


# ── source invariants ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("hook", [COMMIT_HOOK, STOP_HOOK])
def test_hooks_never_pass_an_empty_branch(hook):
    src = hook.read_text()
    assert '[ -n "$branch" ] && count=$(dct clog count --project "$project" --branch "$branch"' in src
