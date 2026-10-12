"""ONE secret-path predicate for /commit and /release (K-54).

`dct clog candidates --json` (what /commit's preflight reads) carries
`secret_like`: the changed files — staged, unstaged or untracked — whose
path looks like a secret by the release preview's own predicate
(plan.secret_like; effects keeps no copy). A staged one names the quoted
command that takes it out of the index (`--` before the path); nothing
here ever stages or unstages. The heuristic is a NAME policy, not a content
scanner, and it is no blanket ban on configuration: `.env.example` /
`.env.sample` templates and ordinary config files are not flagged.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import git, make_git_repo, run_cli
from test_release_effects import SLUG, make_release_repo

from dct.core.projects import create_project
from dct.workflows.release import effects
from dct.workflows.release import plan as planning
from dct.workflows.release.plan import commit_candidates

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SECRETS = [".env", ".env.local", ".env.production", "config/.env", "server.pem", "tls.key",
           "id_rsa", "keys/cert.p12", "credentials.json",
           "credentials-prod.json"]
NOT_SECRETS = [".env.example", ".env.sample", "config/.env.example", "settings.toml",
               "config.json", "app.yaml", "README.md", "environment.md", "credentials.md",
               "keyboard.txt", "monkey.js", "envoy.conf"]


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The repositories here get commits."""


@pytest.fixture
def repo(engine, tmp_path):
    root = Path(make_git_repo(tmp_path / "proj-r"))
    create_project(engine, slug=SLUG, name="R", path=str(root), current_version="1.0.0")
    return root


def write(root: Path, rel: str, text: str = "x\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def candidates_json(engine, root: Path, monkeypatch, capsys) -> dict:
    """`dct clog candidates -P proj-r --json` in `root` — the document
    /commit's preflight reads."""
    from dct import cli as cli_module

    monkeypatch.setattr(cli_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli_module, "_get_readonly_engine", lambda: engine)
    monkeypatch.chdir(root)
    capsys.readouterr()
    run_cli(monkeypatch, ["clog", "candidates", "-P", SLUG, "--json"])
    return json.loads(capsys.readouterr().out)


def run_command(root: Path, command: str) -> None:
    argv = shlex.split(command)
    assert argv[:1] == ["git"], command
    subprocess.run(["git", "-C", str(root), *argv[1:]], check=True, capture_output=True)


# ── the predicate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", SECRETS)
def test_secret_names_are_flagged(path):
    assert planning.secret_like(path)


@pytest.mark.parametrize("path", NOT_SECRETS)
def test_templates_and_ordinary_configuration_are_not_flagged(path):
    """The policy the release preview already applied: `.env.*` templates
    are not secrets, and a configuration file is not one by its kind."""
    assert not planning.secret_like(path)


def test_the_release_preview_has_no_predicate_of_its_own():
    """One predicate: the effects layer keeps no copy of the patterns or
    of the function (a second list would drift)."""
    assert not hasattr(effects, "_secret_like")
    assert not hasattr(effects, "SECRET_LIKE_PATTERNS")
    assert not hasattr(effects, "SECRET_LIKE_TEMPLATES")


# ── the candidates /commit reads ─────────────────────────────────────────────


def test_a_staged_secret_is_flagged_with_a_command_that_unstages_only_it(
        engine, repo, monkeypatch, capsys):
    write(repo, ".env", "TOKEN=abc\n")
    write(repo, "app.py", "print(1)\n")
    git(repo, "add", ".env", "app.py")
    doc = candidates_json(engine, repo, monkeypatch, capsys)
    assert doc["secret_like"] == [
        {"path": ".env", "staged": True, "unstage": "git restore --staged -- .env"}]
    run_command(repo, doc["secret_like"][0]["unstage"])
    assert git(repo, "diff", "--cached", "--name-only") == "app.py", \
        "the command touched more than the secret-like path"
    assert (repo / ".env").read_text() == "TOKEN=abc\n", "the command changed the file"


def test_an_unstaged_or_untracked_secret_is_flagged_without_a_command(
        engine, repo, monkeypatch, capsys):
    """Both unstaged states: a tracked secret-like file modified in the
    work tree, and a new untracked one — flagged (never offered for
    staging), no command (there is nothing to unstage); the index is not
    touched."""
    write(repo, "deploy.key", "old\n")
    git(repo, "add", "deploy.key")
    git(repo, "commit", "-q", "-m", "chore: key")
    write(repo, "deploy.key", "new\n")
    write(repo, "config/.env", "A=1\n")
    write(repo, "config/.env.example", "A=\n")
    index = git(repo, "ls-files", "-s")
    doc = candidates_json(engine, repo, monkeypatch, capsys)
    assert doc["secret_like"] == [
        {"path": "config/.env", "staged": False, "unstage": None},
        {"path": "deploy.key", "staged": False, "unstage": None}]
    assert git(repo, "ls-files", "-s") == index


def test_a_partly_staged_secret_counts_as_staged(engine, repo, monkeypatch, capsys):
    write(repo, ".env", "A=1\n")
    git(repo, "add", ".env")
    write(repo, ".env", "A=2\n")
    [item] = candidates_json(engine, repo, monkeypatch, capsys)["secret_like"]
    assert item["staged"] is True and item["unstage"] == "git restore --staged -- .env"


def test_no_secret_like_change_gives_an_empty_list(engine, repo, monkeypatch, capsys):
    write(repo, ".env.example", "A=\n")
    write(repo, "settings.toml", "a = 1\n")
    git(repo, "add", ".env.example")
    assert candidates_json(engine, repo, monkeypatch, capsys)["secret_like"] == []


@pytest.mark.parametrize("path", ["dir with space/.env", "-odd/.env", "it's/.env.local"])
def test_the_unstage_command_is_quoted_and_separated(engine, repo, monkeypatch, capsys, path):
    """Any path survives the shell: quoted, and after `--` (a leading dash
    is never an option)."""
    write(repo, path, "S=1\n")
    git(repo, "add", "--", path)
    [item] = candidates_json(engine, repo, monkeypatch, capsys)["secret_like"]
    assert item["path"] == path
    argv = shlex.split(item["unstage"])
    assert argv[:4] == ["git", "restore", "--staged", "--"] and argv[4:] == [path]
    run_command(repo, item["unstage"])
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_before_the_first_commit_the_command_removes_it_from_the_index(
        engine, tmp_path, monkeypatch, capsys):
    """An unborn HEAD has nothing to restore from: `git rm --cached` — the
    file stays on disk."""
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    create_project(engine, slug=SLUG, name="R", path=str(root), current_version="1.0.0")
    write(root, ".env", "A=1\n")
    git(root, "add", ".env")
    [item] = candidates_json(engine, root, monkeypatch, capsys)["secret_like"]
    assert item == {"path": ".env", "staged": True, "unstage": "git rm --cached -q -- .env"}
    run_command(root, item["unstage"])
    assert git(root, "ls-files") == "" and (root / ".env").exists()


def test_candidates_and_the_preview_label_the_same_paths(engine, tmp_path, monkeypatch, capsys):
    """Bidirectional: what the preview leaves out as secret-like and what
    /commit's candidates flag come from one predicate — the same verdict
    for every untracked path."""
    repo, _ = make_release_repo(engine, tmp_path)
    for path in SECRETS[:6] + NOT_SECRETS[:6]:
        write(repo, path, "v\n")
    doc = candidates_json(engine, repo, monkeypatch, capsys)
    flagged = {item["path"] for item in doc["secret_like"]}
    preview = effects.preview_release(engine, SLUG, cwd=str(repo), version="1.1.0",
                                      variants=False)
    left_out = {item["path"]: item["secret_like"] for item in preview["details"]["left_out"]}
    assert set(left_out) >= set(SECRETS[:6] + NOT_SECRETS[:6])
    assert {p for p, secret in left_out.items() if secret} == flagged


def test_outside_git_the_list_is_empty(engine, tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".env").write_text("A=1\n")
    create_project(engine, slug=SLUG, name="P", path=str(plain), current_version="1.0.0")
    result = commit_candidates(engine, SLUG, cwd=str(plain))
    assert result["secret_like"] == [] and result["checkout"] == {"git": False}
