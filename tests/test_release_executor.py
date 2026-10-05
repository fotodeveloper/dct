"""K-85 — the explicit release EXECUTOR (`dct` | `own`) and K-96 D10 (the old
release commands as adapters of the same ledger rules).

The project setting `release_executor` is copied onto each NEW record at
stamp time (core); the RECORD's executor decides who performs the effects
of that release, never today's project setting:

- a project whose executor is `own`: `dct release prepare / apply` (and the
  `clog release` alias) refuse BEFORE any write and point to the project's
  own integration — the proof layer (`dct release plan --json`) and the
  ledger (MCP release_changelog / get_release / advance_release /
  abandon_release) stay shared;
- a record whose executor is `own` (or that carries no consent — stamped
  outside `dct release apply`): `dct release resume` refuses and performs /
  adopts none of its effects; a record dct stamped with the owner's consent
  is advanced ONLY by the effects layer (a manual advance is refused);
- a later change of the project setting never takes over an unfinished
  release — tested both ways.

The fake own release tool (a Python script in tmp_path, run as a separate
process) consumes the shared plan, stamps through the ledger, performs its
OWN effects and advances the record; dct's apply / resume refuse to touch
it and its effects survive byte for byte.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import git, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    apply,
    assert_released,
    interrupt_at,
    make_release_repo,
    proven,
    resume,
    snapshot,
)

from dct.core.changelog import release_entries
from dct.core.projects import set_release_executor
from dct.core.releases import advance_release, get_pending_proposal, get_release
from dct.db import changelog_entries, projects, releases
from dct.workflows.release import effects
from dct.workflows.release.effects import ReleaseRefused

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Commits are created by dct and by the fake own tool."""


def db_state(engine) -> tuple:
    """Every row of the release-relevant tables — compared for 'nothing
    written' (timestamps included)."""
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases))


def world(engine, repo) -> tuple:
    return db_state(engine), snapshot(repo), git(repo, "ls-remote", "origin")


def dct_cli(engine, repo, *argv: str) -> subprocess.CompletedProcess:
    """`python -m dct.cli …` — the REAL command line in its own process, on
    the test database (conftest puts this tree first on PYTHONPATH)."""
    env = {**os.environ, "DCT_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    return subprocess.run([sys.executable, "-m", "dct.cli", *argv], cwd=str(repo), env=env,
                          capture_output=True, text=True, timeout=300, check=False)


# The project's OWN release tool: it uses only the shared surfaces — the plan
# (`dct release plan --json`) and the ledger (the MCP tools) — and performs
# its own effects in its own format: a VERSION file, CHANGES.txt, a commit
# `chore(release): V`, a tag `release-V`. `stop_after` simulates a crash
# right after recording that status; a second run resumes from get_release.
OWN_TOOL = r'''
import json, os, subprocess, sys
from dct.mcp import server as S

repo, version, entries = sys.argv[1], sys.argv[2], sys.argv[3]
stop_after = sys.argv[4] if len(sys.argv) > 4 else ""
SLUG = "proj-r"

def run(*cmd):
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=True).stdout

def advance(status, **kw):
    out = S.advance_release(version, status, project=SLUG, **kw)
    assert "error" not in out, out
    print(f"advanced {status}", flush=True)
    if status == stop_after:
        sys.exit(0)

record = S.get_release(project=SLUG, version=version)["release"]
if record is None:
    plan = json.loads(run(sys.executable, "-m", "dct.cli", "release", "plan", "-P", SLUG,
                          "--version", version, "--entries", entries, "--json"))
    snap = plan["snapshot"]
    out = S.release_changelog(project=SLUG, version=version,
                              entry_ids=snap["selected_ids"],
                              candidate_sha=snap["candidate_sha"],
                              checkout_root=snap["repo_root"], proof=snap["proof"])
    assert "error" not in out, out
    record = out["release"]
    with open(os.path.join(repo, "plan-proof.json"), "w") as fh:
        json.dump(snap["proof"], fh)
status = record["status"]
if status == "stamped":
    with open(os.path.join(repo, "VERSION"), "w") as fh:
        fh.write(version + "\n")
    with open(os.path.join(repo, "CHANGES.txt"), "a") as fh:
        fh.write(f"{version}: own tool release\n")
    advance("files_written", manifest={"own_tool": {"files": ["VERSION", "CHANGES.txt"]}})
    status = "files_written"
if status == "files_written":
    run("git", "add", "VERSION", "CHANGES.txt")
    run("git", "commit", "-q", "-m", f"chore(release): {version}")
    advance("committed", release_commit_sha=run("git", "rev-parse", "HEAD").strip())
    status = "committed"
if status == "committed":
    run("git", "tag", f"release-{version}")
    advance("tagged")
    status = "tagged"
if status == "tagged":
    advance("completed")
'''


def own_tool(engine, repo, tmp_path, version: str, entries: str, stop_after: str = ""):
    script = tmp_path / "own_release_tool.py"
    script.write_text(OWN_TOOL)
    env = {**os.environ, "DCT_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    # The plan / the ledger see this checkout; the tool's own files live in it.
    out = subprocess.run([sys.executable, str(script), str(repo), version, entries, stop_after],
                         cwd=str(repo), env=env, capture_output=True, text=True, timeout=300,
                         check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


@pytest.fixture
def own(concurrent_engine, tmp_path):
    """proj-r on a FILE database (the own tool and the CLI run as separate
    processes), its executor set to `own`, the plan/ledger information
    files ignored by git (the tool writes plan-proof.json into the checkout)."""
    engine = concurrent_engine
    repo, _ = make_release_repo(engine, tmp_path)
    (repo / ".git" / "info" / "exclude").write_text("plan-proof.json\n")
    set_release_executor(engine, SLUG, "own")
    return engine, repo


def test_own_tool_uses_the_shared_plan_and_ledger_and_dct_never_touches_its_effects(
        own, tmp_path):
    engine, repo = own
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")

    # 1. dct's own effects are refused BEFORE any write for an `own` project.
    before = world(engine, repo)
    for argv in (["release", "prepare", "-P", SLUG, "--version", "1.1.0", "--entries", str(a),
                  "--plan-digest", "0" * 64, "--json"],
                 ["release", "apply", "-P", SLUG, "--version", "1.1.0", "--entries", str(a),
                  "--plan-digest", "0" * 64, "--proposal-digest", "0" * 64, "--no-push",
                  "--confirm", "--json"],
                 ["clog", "release", "-P", SLUG, "1.1.0", "--no-push", "--confirm", "--json"]):
        out = dct_cli(engine, repo, *argv)
        assert out.returncode == 1, out.stdout + out.stderr
        doc = json.loads(out.stdout)
        assert doc["outcome"] == "refused" and doc["details"]["executor"] == "own"
        assert "own release tool" in doc["details"]["error"]
        assert world(engine, repo) == before
    assert get_pending_proposal(engine, SLUG) is None

    # 2. The own tool: shared plan → ledger stamp → its own effects; crash
    #    after 'committed'.
    own_tool(engine, repo, tmp_path, "1.1.0", str(a), stop_after="committed")
    record = get_release(engine, SLUG, "1.1.0")
    assert (record["status"], record["executor"], record["candidate_sha"]) == (
        "committed", "own", candidate)
    assert record["consent"] is None and record["proposal"] is None
    # the SAME proof set the plan computed
    assert record["proof"] == json.loads((repo / "plan-proof.json").read_text())
    assert record["proof"][str(a)]["commits"] == [candidate]
    tool_commit = git(repo, "rev-parse", "HEAD")
    assert record["release_commit_sha"] == tool_commit
    assert git(repo, "log", "-1", "--format=%s") == "chore(release): 1.1.0"

    # 3. dct's resume / apply refuse to touch it — nothing written, nothing
    #    moved, the own tool's files and commit preserved byte for byte.
    before = world(engine, repo)
    for argv in (["release", "resume", "-P", SLUG, "--json"],
                 ["release", "apply", "-P", SLUG, "--version", "1.1.0", "--entries", str(a),
                  "--plan-digest", "0" * 64, "--no-push", "--confirm", "--json"]):
        out = dct_cli(engine, repo, *argv)
        assert out.returncode == 1, out.stdout + out.stderr
        doc = json.loads(out.stdout)
        assert doc["outcome"] == "refused" and doc["details"]["executor"] == "own"
        assert world(engine, repo) == before
    # a manual advance of an own record is the ledger path — allowed (the
    # tool's own verification); shown here only as refused for dct's actor
    with pytest.raises(ValueError, match="not executed by dct"):
        advance_release(engine, SLUG, "1.1.0", "tagged", actor="dct")
    assert world(engine, repo) == before

    # 4. A LATER project-setting change never takes the release over.
    set_release_executor(engine, SLUG, "dct")
    before = world(engine, repo)
    out = dct_cli(engine, repo, "release", "resume", "-P", SLUG, "--json")
    assert out.returncode == 1 and json.loads(out.stdout)["details"]["executor"] == "own"
    assert world(engine, repo) == before

    # 5. The own tool finishes its release; its effects are its own.
    own_tool(engine, repo, tmp_path, "1.1.0", str(a))
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed" and record["executor"] == "own"
    assert git(repo, "rev-parse", "release-1.1.0") == tool_commit
    assert git(repo, "tag", "--list", "v1.1.0") == ""      # dct tagged nothing
    assert (repo / "VERSION").read_text() == "1.1.0\n"
    assert "## [1.1.0]" not in (repo / "changelog.md").read_text()  # dct wrote no section


def test_a_dct_record_stays_dct_after_the_project_switches_to_own(engine, tmp_path):
    """The other direction: dct stamped it (with consent) — the project now
    says `own`, the unfinished release is still finished by dct's resume."""
    repo, _ = make_release_repo(engine, tmp_path)
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at("after_stamp"))
    assert get_release(engine, SLUG, "1.1.0")["executor"] == "dct"
    set_release_executor(engine, SLUG, "own")
    assert resume(engine, repo)["outcome"] == "completed"
    record = assert_released(engine, repo, candidate, [a])
    assert record["executor"] == "dct"
    # ...but a NEW release of an `own` project is refused before any write
    b = proven(engine, repo, "feature b")
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="own release tool") as exc:
        effects.prepare_release(engine, SLUG, version="1.2.0", entry_ids=[b],
                                plan_digest="0" * 64, cwd=str(repo))
    assert exc.value.details["executor"] == "own"
    assert db_state(engine) == before


def test_a_consented_dct_record_is_advanced_only_by_the_effects_layer(engine, tmp_path,
                                                                     monkeypatch, capsys):
    """K-96 D10: the ledger's manual paths (MCP advance_release, `dct clog
    release-advance`) never bypass dct's verification of ITS OWN release."""
    repo, _ = make_release_repo(engine, tmp_path)
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_files_written"))
    before = db_state(engine)
    with pytest.raises(ValueError, match="dct release resume"):
        advance_release(engine, SLUG, "1.1.0", "committed", release_commit_sha="a" * 40,
                        actor="ledger")
    from dct import cli
    from dct.mcp import server as S

    monkeypatch.setattr(S, "_get_engine", lambda: engine)
    out = S.advance_release("1.1.0", "committed", project=SLUG, release_commit_sha="a" * 40)
    assert "dct release resume" in out["error"]
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", "committed", "-P", SLUG,
                              "--release-commit-sha", "a" * 40])
    assert exc.value.code == 1 and "dct release resume" in capsys.readouterr().err
    assert db_state(engine) == before
    assert resume(engine, repo)["outcome"] == "completed"


def test_a_record_without_consent_is_never_resumed_by_dct(engine, tmp_path, monkeypatch,
                                                          capsys):
    """J4 / L9: a record stamped outside `dct release apply` (MCP
    release_changelog, older records) has no consent — no approved proposal,
    no push target: resume refuses at every status instead of finishing it
    without a push; the manual ledger path records its verified progress."""
    repo, _ = make_release_repo(engine, tmp_path)
    a = proven(engine, repo, "feature a")
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], candidate_sha=git(repo, "rev-parse",
                    "HEAD"), checkout_root=str(repo))
    for status, kw in (("stamped", {}), ("tagged", {"release_commit_sha": "b" * 40})):
        if status != "stamped":
            advance_release(engine, SLUG, "1.1.0", status, actor="ledger", **kw)
        before = world(engine, repo)
        with pytest.raises(ReleaseRefused, match="stamped outside") as exc:
            resume(engine, repo)
        assert exc.value.details["consent"] is None
        assert world(engine, repo) == before
    from dct import cli

    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.chdir(repo)
    run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", "completed", "-P", SLUG])
    assert "tagged → completed" in capsys.readouterr().out
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"


def test_attest_is_a_proof_layer_commit_usable_by_an_own_project(own, tmp_path):
    engine, repo = own
    from dct.core.changelog import add_entry

    eid = add_entry(engine, SLUG, "added", "squashed work")["id"]
    out = effects.attest(engine, SLUG, [eid], confirm=True, cwd=str(repo))
    assert out["outcome"] == "attested"
    assert git(repo, "log", "-1", "--format=%B").strip().endswith(f"Changelog: {eid}")


@pytest.mark.parametrize("command", [
    ["clog", "release-advance", "1.1.0", "files_written"],
    ["clog", "release-abandon", "1.1.0", "--reason", "x", "--confirm"],
])
def test_the_old_commands_take_the_operation_lock(engine, tmp_path, monkeypatch, capsys,
                                                  command):
    """K-96 D10: `clog release-advance` / `clog release-abandon` are adapters
    of the same ledger functions under the same operation lock as every
    other mutating release path."""
    from dct import cli
    from dct.workflows.release import oplock

    repo, _ = make_release_repo(engine, tmp_path)
    a = proven(engine, repo, "feature a")
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], candidate_sha=git(repo, "rev-parse",
                    "HEAD"), checkout_root=str(repo))
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.chdir(repo)
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    before = db_state(engine)
    with oplock.operation_lock(oplock.lock_path(git_common_dir=common), "other"):
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, [*command, "-P", SLUG])
        assert exc.value.code == 1
        assert "another dct release operation" in capsys.readouterr().err
        assert db_state(engine) == before
    run_cli(monkeypatch, [*command, "-P", SLUG])
    assert db_state(engine) != before


def test_own_tool_record_can_be_abandoned_through_the_shared_ledger(own, tmp_path):
    engine, repo = own
    a = proven(engine, repo, "feature a")
    own_tool(engine, repo, tmp_path, "1.1.0", str(a), stop_after="files_written")
    out = effects.abandon(engine, SLUG, "1.1.0", "tool broke", confirm=True)
    assert out["outcome"] == "abandoned"
    assert Path(repo / "VERSION").read_text() == "1.1.0\n"   # effects stay
