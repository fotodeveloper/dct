"""Per-branch changelog count in the commit hook and the Stop hook (#801, C7).

Two kinds of `dct` on PATH:
  * the REAL CLI of this tree (PYTHONPATH) against a per-test throwaway SQLite
    database — proves the correct zero / the unbranched rule end to end;
  * a FAKE `dct` shell script that LOGS its argv — an old CLI that rejects
    --branch (version skew → project-wide fallback), a broken one (fail-open
    for the blocking hook, keep-reminding for the Stop hook), and the target
    of the pasted deny-message command (K-33 quoting).

Both hooks source the shared count lib dct/hooks/dct-changelog-count.lib.sh
(K-48); the installed-form tests run them through the real installer.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core.changelog import add_entry
from dct.core.projects import create_project
from dct.db import create_tables, get_engine
from dct.integrations import claude
from dct.integrations.claude import HOOKS_DIR

COMMIT_HOOK = HOOKS_DIR / "check-changelog.sh"
STOP_HOOK = HOOKS_DIR / "check-changelog-on-stop.sh"
LIB_NAME = "dct-changelog-count.lib.sh"

_MISSING_TOOLS = [t for t in ("git", "jq") if shutil.which(t) is None]

# K-45: locally a missing jq/git skips the module; under CI (GitHub Actions
# sets CI=true) the skip is disabled and the sentinel below FAILS instead, so
# the hook suite can never go silently green on a runner without jq.
pytestmark = pytest.mark.skipif(
    bool(_MISSING_TOOLS) and not os.environ.get("CI"),
    reason=f"{' and '.join(_MISSING_TOOLS)} required by the hooks",
)


@pytest.mark.skipif(not os.environ.get("CI"),
                    reason="sentinel for CI only; set CI=1 to arm it")
def test_ci_runner_has_the_hook_dependencies():
    assert not _MISSING_TOOLS, (
        f"CI is set but {_MISSING_TOOLS} missing — the changelog hook tests "
        "would otherwise be skipped silently")

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
    """A `dct` executable on a private bin dir that appends its argv (one
    argument per line, `--end--` after each call) to ``argv.log`` next to it.
    project detect → proj-a, manages-changelog → yes, `clog update` → ok;
    `clog count` runs `count_script` (bash)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = bin_dir / "argv.log"
    body = (
        "#!/bin/bash\n"
        f"{{ for a in \"$@\"; do printf '%s\\n' \"$a\"; done; echo --end--; }} >> '{log}'\n"
        'case "$1 $2" in\n'
        '  "project detect") echo proj-a ;;\n'
        '  "project manages-changelog") exit 0 ;;\n'
        f'  "clog count") {count_script} ;;\n'
        '  "clog update") exit 0 ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    # Created executable in one step (no separate mode change).
    fd = os.open(bin_dir / "dct", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return bin_dir


def _calls(bin_dir: Path) -> list[list[str]]:
    """Every logged invocation of the fake `dct`, as argv lists."""
    log = bin_dir / "argv.log"
    if not log.exists():
        return []
    calls, cur = [], []
    for line in log.read_text().splitlines():
        if line == "--end--":
            calls.append(cur)
            cur = []
        else:
            cur.append(line)
    return calls


# An older CLI: argparse rejects --branch in either spelling.
_OLD_CLI = (
    'case " $* " in *" --branch"*) '
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
    assert "entries written on other branches do not" in reason
    assert "dct clog update <id> --branch=feat/x" in reason
    assert "THIS specific change" in reason and "/clog" in reason


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


# ── argv contract: never an empty --branch, always one `--branch=<b>` word ──


def _count_calls(bin_dir: Path) -> list[list[str]]:
    return [c for c in _calls(bin_dir) if c[:2] == ["clog", "count"]]


def _assert_branch_args_well_formed(calls: list[list[str]]):
    for argv in calls:
        assert "--branch" not in argv, f"separate --branch word: {argv}"
        for arg in argv:
            if arg.startswith("--branch"):
                assert arg.startswith("--branch=") and arg != "--branch=", argv


@pytest.mark.parametrize("run", [lambda r, e: _commit_hook(r, e), lambda r, e: _stop_hook(r, e)],
                         ids=["commit", "stop"])
def test_hooks_pass_the_branch_as_one_branch_equals_word(repo, tmp_path, run):
    bin_dir = _fake_dct(tmp_path, "echo 0")
    run(repo, _env(tmp_path, bin_dir))
    calls = _count_calls(bin_dir)
    assert calls, "the hook never asked for a count"
    _assert_branch_args_well_formed(calls)
    assert "--branch=feat/x" in calls[0] and "--include-unbranched" in calls[0]


@pytest.mark.parametrize("run", [lambda r, e: _commit_hook(r, e), lambda r, e: _stop_hook(r, e)],
                         ids=["commit", "stop"])
def test_hooks_on_detached_head_never_pass_any_branch(repo, tmp_path, run):
    git(repo, "commit", "-q", "-m", "wip")
    git(repo, "checkout", "-q", "--detach")
    (repo / "app.py").write_text("x = 2\n")
    git(repo, "add", "app.py")
    bin_dir = _fake_dct(tmp_path, "echo 0")
    run(repo, _env(tmp_path, bin_dir))
    calls = _count_calls(bin_dir)
    assert calls == [["clog", "count", "--project=proj-a"]]


# ── K-33: the ready-to-run command is safe to paste into bash ─────────────────

# Valid Git branch names (`git check-ref-format --branch` accepts each) that
# would run code if the deny message interpolated them unquoted.
_HOSTILE_BRANCHES = ["feat/x;true", "feat/$(touch${IFS}pwned)", "feat/`touch${IFS}pwned`"]


def _branch_repo(tmp_path, name: str) -> Path:
    ok = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True)
    assert ok.returncode == 0, f"test premise: {name!r} must be a valid branch name"
    root = Path(make_git_repo(tmp_path / "proj", branch=name))
    assert git(root, "branch", "--show-current") == name
    (root / "app.py").write_text("x = 1\n")
    git(root, "add", "app.py")
    return root


def _guidance_command(text: str) -> str:
    m = re.search(r"move it here with: (dct clog update <id> \S+) — otherwise", text)
    assert m, text
    return m.group(1)


def _deny_reason(r) -> str:
    assert r.returncode == 2, (r.stdout, r.stderr)
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("branch", _HOSTILE_BRANCHES)
@pytest.mark.parametrize("hook", ["commit", "stop"])
def test_pasted_guidance_command_executes_nothing(tmp_path, branch, hook):
    root = _branch_repo(tmp_path, branch)
    bin_dir = _fake_dct(tmp_path, "echo 0")
    env = _env(tmp_path, bin_dir)
    if hook == "commit":
        text = _deny_reason(_commit_hook(root, env))
    else:
        r = _stop_hook(root, env)
        assert r.returncode == 0 and "Reminder" in r.stdout
        text = r.stdout
    command = _guidance_command(text).replace("<id>", "7")

    paste_dir = tmp_path / "paste"
    paste_dir.mkdir()
    (bin_dir / "argv.log").unlink()
    pasted = subprocess.run(["bash", "-c", command], cwd=paste_dir, env=env,
                            capture_output=True, text=True, timeout=30)
    assert pasted.returncode == 0, pasted.stderr
    assert _calls(bin_dir) == [["clog", "update", "7", f"--branch={branch}"]]
    assert list(paste_dir.iterdir()) == [], "the pasted command had a side effect"


def _dash_branch_repo(tmp_path) -> Path:
    """A repo checked out on `-x` — `git checkout -b` refuses the name, but
    the ref itself is valid, so create it at the plumbing level."""
    root = Path(make_git_repo(tmp_path / "proj"))
    made = subprocess.run(["git", "update-ref", "refs/heads/-x", "HEAD"], cwd=root,
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"git refuses refs/heads/-x: {made.stderr.strip()}")
    git(root, "symbolic-ref", "HEAD", "refs/heads/-x")
    if git(root, "branch", "--show-current") != "-x":
        pytest.skip("git does not report -x as the current branch")
    (root / "app.py").write_text("x = 1\n")
    git(root, "add", "app.py")
    return root


def test_dash_branch_is_never_parsed_as_an_option(tmp_path):
    root = _dash_branch_repo(tmp_path)
    bin_dir = _fake_dct(tmp_path, "echo 0")
    reason = _deny_reason(_commit_hook(root, _env(tmp_path, bin_dir)))
    calls = _count_calls(bin_dir)
    _assert_branch_args_well_formed(calls)
    assert "--branch=-x" in calls[0]
    assert "dct clog update <id> --branch=-x " in reason


def test_dash_branch_counts_its_own_entry_with_the_real_cli(tmp_path):
    """End to end: the real argparse reads `--branch=-x` as a value."""
    root = _dash_branch_repo(tmp_path)
    env = _env(tmp_path)
    _seed(env, root, ["main", "-x"])
    assert _commit_hook(root, env).returncode == 0


def test_dash_branch_without_entry_denies_with_the_real_cli(tmp_path):
    root = _dash_branch_repo(tmp_path)
    env = _env(tmp_path)
    _seed(env, root, ["main"])
    assert "on branch '-x'" in _deny_reason(_commit_hook(root, env))


# ── K-33: fallback wording vs per-branch wording ──────────────────────────────


def test_commit_old_cli_deny_says_project_wide_and_offers_no_branch_move(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, _OLD_CLI.replace("echo 3", "echo 0")))
    reason = _deny_reason(_commit_hook(repo, env))
    assert "could not count per branch" in reason and "project-wide" in reason
    assert "other branches" not in reason
    assert "clog update" not in reason
    assert "/clog" in reason


def test_commit_detached_head_deny_says_no_branch_checked_out(repo, tmp_path):
    git(repo, "commit", "-q", "-m", "wip")
    git(repo, "checkout", "-q", "--detach")
    (repo / "app.py").write_text("x = 2\n")
    git(repo, "add", "app.py")
    env = _env(tmp_path)
    _seed(env, repo, [])
    reason = _deny_reason(_commit_hook(repo, env))
    assert "no branch is checked out" in reason
    assert "other branches" not in reason and "clog update" not in reason


def test_stop_old_cli_zero_reminder_says_project_wide(repo, tmp_path):
    env = _env(tmp_path, _fake_dct(tmp_path, _OLD_CLI.replace("echo 3", "echo 0")))
    out = _stop_hook(repo, env).stdout
    assert "Reminder" in out and "project-wide" in out
    assert "other branches" not in out and "clog update" not in out


def test_stop_per_branch_reminder_offers_the_move_for_this_change_only(repo, tmp_path):
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other"])
    out = _stop_hook(repo, env).stdout
    assert "on branch 'feat/x'" in out and "other branches do not" in out
    assert "THIS specific change" in out
    assert "dct clog update <id> --branch=feat/x" in out


def test_stop_broken_count_keeps_the_generic_reminder(repo, tmp_path):
    out = _stop_hook(repo, _env(tmp_path, _fake_dct(tmp_path, _BROKEN_CLI))).stdout
    assert "Use /clog to add an entry." in out
    assert "clog update" not in out


# ── K-48: installed forms find the shared lib ─────────────────────────────────


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Run the REAL installer into a tmp hooks dir; `copy=True` simulates a
    host without symlink privilege (the `_link_or_copy` fallback)."""
    def _no_symlink(self, target, target_is_directory=False):
        raise OSError("symlinks unavailable")

    def _install(copy: bool) -> Path:
        dst = tmp_path / ("hooks-copy" if copy else "hooks-link")
        with monkeypatch.context() as m:
            m.setattr(claude, "CLAUDE_HOOKS_DIR", dst)
            if copy:
                m.setattr(Path, "symlink_to", _no_symlink)
            assert claude.install_hook() is True
        return dst
    return _install


def _run(hook: Path, repo: Path, env: dict, payload: str):
    return subprocess.run(["bash", str(hook)], cwd=repo, input=payload,
                          capture_output=True, text=True, env=env, timeout=60)


def test_symlinked_and_copied_installs_behave_identically(repo, tmp_path, installed):
    link_dir, copy_dir = installed(copy=False), installed(copy=True)
    assert (link_dir / "dct-check-changelog.sh").is_symlink()
    assert (link_dir / LIB_NAME).is_symlink()
    assert not (copy_dir / "dct-check-changelog.sh").is_symlink()
    assert (copy_dir / LIB_NAME).is_file() and not (copy_dir / LIB_NAME).is_symlink()

    env = _env(tmp_path)
    _seed(env, repo, ["feat/other"])
    outputs = []
    for d in (link_dir, copy_dir):
        c = _run(d / "dct-check-changelog.sh", repo, env, _COMMIT_PAYLOAD)
        s = _run(d / "dct-check-changelog-on-stop.sh", repo, env, "{}")
        outputs.append((c.returncode, c.stdout, s.returncode, s.stdout))
    assert outputs[0] == outputs[1]
    assert outputs[0][0] == 2 and "--branch=feat/x" in outputs[0][1]
    assert "--branch=feat/x" in outputs[0][3]


def test_symlinked_hook_finds_the_lib_beside_its_real_path_only(repo, tmp_path):
    """A relative two-hop symlink chain to the packaged hook, in a dir WITHOUT
    a lib copy: the portable readlink walk must reach the package dir."""
    hop_dir = tmp_path / "hop"
    hop_dir.mkdir()
    (hop_dir / "inner.sh").symlink_to(os.path.relpath(COMMIT_HOOK, hop_dir))
    outer_dir = tmp_path / "outer"
    outer_dir.mkdir()
    (outer_dir / "dct-check-changelog.sh").symlink_to(os.path.relpath(hop_dir / "inner.sh", outer_dir))
    assert not (outer_dir / LIB_NAME).exists()
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other"])
    r = _run(outer_dir / "dct-check-changelog.sh", repo, env, _COMMIT_PAYLOAD)
    assert "--branch=feat/x" in _deny_reason(r)


def test_copied_hooks_without_the_lib_degrade_per_their_policy(repo, tmp_path):
    """No lib anywhere: the blocking hook fails OPEN, the Stop hook keeps
    reminding generically."""
    bare = tmp_path / "bare"
    bare.mkdir()
    for name in ("check-changelog.sh", "check-changelog-on-stop.sh"):
        shutil.copy2(HOOKS_DIR / name, bare / name)
    env = _env(tmp_path)
    _seed(env, repo, ["feat/other"])
    c = _run(bare / "check-changelog.sh", repo, env, _COMMIT_PAYLOAD)
    assert (c.returncode, c.stdout) == (0, "")
    s = _run(bare / "check-changelog-on-stop.sh", repo, env, "{}")
    assert s.returncode == 0 and "Use /clog to add an entry." in s.stdout


def test_doctor_flags_a_copied_install_missing_its_lib(tmp_path, installed, monkeypatch):
    copy_dir = installed(copy=True)
    monkeypatch.setattr(claude, "CLAUDE_HOOKS_DIR", copy_dir)
    for attr, name in (("CLAUDE_JSON", "claude.json"), ("CLAUDE_MD", "CLAUDE.md"),
                       ("CLAUDE_SETTINGS", "settings.json"), ("CLAUDE_SKILLS_DIR", "skills")):
        monkeypatch.setattr(claude, attr, tmp_path / name)

    def hook_assets():
        return next(c for c in claude.ClaudeIntegration().doctor() if c["check"] == "hook_assets")

    assert hook_assets()["status"] == "ok", hook_assets()
    (copy_dir / LIB_NAME).unlink()
    row = hook_assets()
    assert row["status"] == "error" and LIB_NAME in row["message"]
