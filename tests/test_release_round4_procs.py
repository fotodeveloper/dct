"""Round 4, R8b — every external process of the release workflow runs through
the ONE supervised runner (dct.core.procs): K-108, K-113, K-118 L10.

The production effects layer and git layer on real throwaway repositories,
real hooks and real scripts:

- K-108: an interrupted `prepare` (Ctrl-C, SIGTERM, SIGHUP) stops the
  version-sync script with its child and grandchild BEFORE the isolated
  worktree is removed and the operation lock released;
- K-118 L10: a commit hook / a push hook that exceeds the timeout is stopped
  with everything it started; the release stays resumable;
- K-113: the sync output and a hook's output are drained bounded (never a
  temporary file, never the whole text in memory or in the refusal); git
  output that is PROOF (history reads, blobs, `ls-remote`) is complete or
  refused past a documented limit, never cut.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import (
    approved_plan_digest,
    approved_proposal_digest,
    approved_push,
    git,
    make_git_repo,
)
from test_procs import MIB, ROOT, Family
from test_release_effects import SLUG as SLUG_R
from test_release_effects import apply, make_release_repo, proven, remote_refs, resume
from test_release_git_effects import hooks_dir, install_hook, local_tag
from test_release_prepare import HISTORY, PYPROJECT, checkout_state, worktrees

from dct.core import gitinfo, procs
from dct.core.changelog import add_entry
from dct.core.projects import create_project
from dct.core.releases import get_pending_proposal, get_release
from dct.workflows.release import effects, oplock
from dct.workflows.release.effects import ReleaseRefused

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
    pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX"),
]

SLUG = "proj-s"


@pytest.fixture(autouse=True)
def _hermetic(git_identity, monkeypatch):
    """The effects layer creates commits; a process that ignores SIGTERM
    costs the whole grace."""
    monkeypatch.setattr(procs, "TERM_GRACE_SECONDS", 0.3)


@pytest.fixture
def family(tmp_path):
    fam = Family(tmp_path)
    try:
        yield fam
    finally:
        fam.close()


def bash_family(family: Family, mode: str = "ignore", *, barrier: bool = True) -> str:
    """A bash line that BECOMES the family's leader (`exec`)."""
    return "exec " + " ".join(f'"{arg}"' for arg in family.argv(mode, barrier=barrier)) + "\n"


# ── K-108: prepare interrupted ───────────────────────────────────────────────

# Stands for `dct release prepare` in its own process (the production
# function, a database the test shares with it).
PREPARE = r'''
import json, sys
from dct.core import procs
from dct.db import get_engine
from dct.workflows.release import effects

cfg = json.loads(sys.argv[1])
procs.TERM_GRACE_SECONDS = 0.3
effects.prepare_release(get_engine(cfg["url"]), cfg["slug"], version="1.1.0",
                        entry_ids=cfg["ids"], plan_digest=cfg["digest"], cwd=cfg["repo"])
'''


@pytest.fixture
def sync_proj(concurrent_engine, tmp_path, family):
    """proj-s: a repository whose committed version-sync script becomes the
    family (it never finishes on its own), one proven unreleased entry."""
    engine = concurrent_engine
    repo = Path(make_git_repo(tmp_path / "proj-s"))
    (repo / "changelog.md").write_text(HISTORY)
    (repo / "pyproject.toml").write_text(PYPROJECT.format(v="1.0.0"))
    (repo / "scripts").mkdir()
    (repo / "scripts" / "dct-version-sync.sh").write_text(
        "#!/bin/bash\n" + bash_family(family, "ignore,cwd"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "chore: setup")
    git(repo, "tag", "v1.0.0")
    create_project(engine, slug=SLUG, name="S", path=str(repo), current_version="1.0.0")
    eid = add_entry(engine, SLUG, "added", "feature a")["id"]
    (repo / "f.txt").write_text("a\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", f"feat: a\n\nChangelog: {eid}\n")
    return engine, repo, eid


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_an_interrupted_prepare_leaves_no_script_process_behind(
        sync_proj, tmp_path, family, signum):
    """verify3 #7: Ctrl-C never reached the script (its own session), SIGTERM
    / SIGHUP did not even unwind — the script and its descendants wrote on
    into a worktree dct had already removed, under a released lock."""
    engine, repo, eid = sync_proj
    before = checkout_state(repo)
    config = {"url": engine.url.render_as_string(hide_password=False), "slug": SLUG,
              "ids": [eid], "repo": str(repo),
              "digest": approved_plan_digest(engine, SLUG, repo, version="1.1.0",
                                             entry_ids=[eid])}
    script = tmp_path / "prepare.py"
    script.write_text(PREPARE)
    helper = subprocess.Popen([sys.executable, str(script), json.dumps(config)], cwd=ROOT,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        family.wait_ready(helper)
        isolated = (family.dir / "cwd").read_text()
        assert isolated != str(repo) and os.path.isdir(isolated)
        while not os.path.exists(os.path.join(isolated, "ticks")):
            time.sleep(0.01)  # the family IS writing into the isolated worktree
        helper.send_signal(signum)
        stderr = helper.communicate(timeout=120)[1]
    finally:
        helper.kill()
    assert family.alive() == [], stderr
    assert helper.returncode == -signum, stderr
    assert not os.path.exists(isolated)
    assert worktrees(repo) == [f"worktree {repo}"]
    assert checkout_state(repo) == before
    lock = oplock.lock_path(git_common_dir=str(repo / ".git"))
    assert oplock.read_holder(lock) is None
    assert get_pending_proposal(engine, SLUG) is None and get_release(engine, SLUG) is None
    family.assert_silent()


def test_a_sync_group_that_cannot_be_stopped_keeps_its_worktree_and_the_protection(
        sync_proj, family, monkeypatch):
    """The escalation ladder is cut to SIGTERM (standing for a group no
    signal ends): nothing is removed under a process that may still write
    there, and the next command refuses while the group lives."""
    engine, repo, eid = sync_proj
    monkeypatch.setattr(procs, "STOP_SIGNALS", ((signal.SIGTERM, 0.3),))
    monkeypatch.setattr(effects, "SYNC_TIMEOUT_SECONDS", 3)
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[eid])
    kw = {"version": "1.1.0", "entry_ids": [eid], "plan_digest": digest, "cwd": str(repo)}
    with pytest.raises(ReleaseRefused, match="could not be stopped") as exc:
        effects.prepare_release(engine, SLUG, **kw)
    pgid = family.pids()["leader"]
    assert exc.value.details["process_group"] == pgid
    isolated = (family.dir / "cwd").read_text()
    assert sorted(family.alive()) == ["child", "grandchild"]
    assert os.path.isdir(isolated) and len(worktrees(repo)) == 2  # left in place
    with pytest.raises(ReleaseRefused, match="still running") as second:
        effects.prepare_release(engine, SLUG, **kw)
    assert second.value.details["process_groups"] == [pgid]
    assert second.value.details["holder"]["action"] == "prepare"
    os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while procs.group_alive(pgid):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    lock = oplock.lock_path(git_common_dir=str(repo / ".git"))
    with oplock.operation_lock(lock, "after"):  # the dead group no longer blocks
        pass
    git(repo, "worktree", "remove", "--force", isolated)  # the documented cleanup


# ── K-118 L10: hooks and the push under the same supervision ─────────────────

# Stands for `dct release apply` in its own process.
APPLY = r'''
import json, sys
from dct.core import procs
from dct.db import get_engine
from dct.workflows.release import effects

cfg = json.loads(sys.argv[1])
procs.TERM_GRACE_SECONDS = 0.3
effects.apply_release(get_engine(cfg["url"]), cfg["slug"], version="1.1.0",
                      entry_ids=cfg["ids"], confirm=True, plan_digest=cfg["digest"],
                      proposal_digest=cfg["proposal"], cwd=cfg["repo"], **cfg["push"])
'''


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_an_apply_interrupted_inside_a_commit_hook_stops_the_hook_and_stays_resumable(
        concurrent_engine, tmp_path, family, signum):
    """The hook (it became the family) is reaped before dct lets go of the
    lock; nothing is committed, and resume finishes the release once."""
    engine = concurrent_engine
    repo, _ = make_release_repo(engine, tmp_path)
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    digest = approved_plan_digest(engine, SLUG_R, repo, version="1.1.0", entry_ids=[a])
    proposal = approved_proposal_digest(engine, SLUG_R, repo, plan_digest=digest,
                                        version="1.1.0", entry_ids=[a])
    hook = install_hook(hooks_dir(repo), "pre-commit", bash_family(family))
    config = {"url": engine.url.render_as_string(hide_password=False), "slug": SLUG_R,
              "ids": [a], "repo": str(repo), "digest": digest, "proposal": proposal,
              "push": approved_push(repo)}
    script = tmp_path / "apply.py"
    script.write_text(APPLY)
    helper = subprocess.Popen([sys.executable, str(script), json.dumps(config)], cwd=ROOT,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        family.wait_ready(helper)
        helper.send_signal(signum)
        stderr = helper.communicate(timeout=120)[1]
    finally:
        helper.kill()
    assert family.alive() == [], stderr
    assert helper.returncode == -signum, stderr
    assert oplock.read_holder(oplock.lock_path(git_common_dir=str(repo / ".git"))) is None
    assert get_release(engine, SLUG_R, "1.1.0")["status"] == "files_written"
    assert git(repo, "rev-parse", "HEAD") == candidate and local_tag(repo, "v1.1.0") == ""
    family.assert_silent()
    hook.unlink()
    assert resume(engine, repo)["outcome"] == "completed"


def test_a_group_left_by_a_killed_dct_refuses_the_next_release_command(engine, tmp_path):
    """The record a killed dct leaves in the lock file, with a group that
    really runs: every mutating command refuses and names the group."""
    repo, _ = make_release_repo(engine, tmp_path)
    leftover = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        lock = oplock.lock_path(git_common_dir=str(repo / ".git"))
        record = {"pid": 1, "action": "apply", "host": socket.gethostname(),
                  "started_at": "2026-01-01T00:00:00+00:00", "spawning": False,
                  "groups": [leftover.pid]}
        Path(lock).write_text(json.dumps(record))
        with pytest.raises(ReleaseRefused, match="still running") as exc:
            resume(engine, repo)
        assert exc.value.details["process_groups"] == [leftover.pid]
        assert exc.value.details["lock"] == lock and exc.value.details["holder"] == record
        assert leftover.poll() is None  # refused — never stopped by the contender
    finally:
        leftover.kill()
        leftover.wait()
    # Its end is all the next command needs (here: nothing to resume).
    assert resume(engine, repo)["outcome"] == "noop"


def test_a_commit_hook_over_the_timeout_is_stopped_with_its_children(
        engine, tmp_path, family, monkeypatch):
    repo, _ = make_release_repo(engine, tmp_path)
    hook = install_hook(hooks_dir(repo), "pre-commit", bash_family(family, barrier=False))
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(effects, "HOOK_GIT_TIMEOUT_SECONDS", 3)
    with pytest.raises(ReleaseRefused, match="timed out"):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    family.assert_started()
    assert family.alive() == []
    assert get_release(engine, SLUG_R, "1.1.0")["status"] == "files_written"
    assert git(repo, "rev-parse", "HEAD") == candidate and local_tag(repo, "v1.1.0") == ""
    family.assert_silent()
    hook.unlink()
    assert resume(engine, repo)["outcome"] == "completed"


def test_a_push_hook_over_the_timeout_is_stopped_with_its_children(
        engine, tmp_path, family, monkeypatch):
    repo, _ = make_release_repo(engine, tmp_path)
    hook = install_hook(hooks_dir(repo), "pre-push", bash_family(family, barrier=False))
    a = proven(engine, repo, "feature a")
    before_remote = remote_refs(repo)
    monkeypatch.setattr(effects, "HOOK_GIT_TIMEOUT_SECONDS", 3)
    with pytest.raises(ReleaseRefused, match="timed out"):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    family.assert_started()
    assert family.alive() == []
    assert get_release(engine, SLUG_R, "1.1.0")["status"] == "tagged"
    assert remote_refs(repo) == before_remote
    family.assert_silent()
    hook.unlink()
    assert resume(engine, repo)["outcome"] == "completed"


def test_a_noisy_refusing_hook_is_reported_bounded_with_its_last_words(engine, tmp_path):
    """8 MiB of hook output: the refusal quotes the start and the END (where
    a hook says why it refused) and counts what it dropped."""
    repo, _ = make_release_repo(engine, tmp_path)
    install_hook(hooks_dir(repo), "pre-commit",
                 "echo 'first words' >&2\n"
                 f'"{sys.executable}" -c "import os\n'
                 f'for _ in range(128): os.write(2, b\'x\' * 65536)"\n'
                 "echo 'refused by policy' >&2\nexit 1\n")
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused) as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    error = exc.value.details["git_error"]
    assert len(error) < gitinfo.GIT_DIAGNOSTIC_MAX_BYTES + 1024
    assert "first words" in error and "refused by policy" in error
    assert "bytes of output dropped" in error


# ── K-113: the sync output ───────────────────────────────────────────────────

SYNC_UNDER_A_FILE_LIMIT = r'''
import json, resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
from dct.workflows.release import effects
print(json.dumps(effects.run_sync_script(sys.argv[1], "1.1.0", sys.argv[2])))
'''


def test_the_sync_output_never_reaches_the_disk(tmp_path):
    """verify3 #12: the script wrote straight into a temporary file and the
    cap applied only when reading it back. Under a 1 MiB file-size limit a
    script printing 8 MiB must still finish — its output goes through a
    pipe, of which the first 64 KiB are kept and the rest counted."""
    script = tmp_path / "sync.sh"
    script.write_text(f'#!/bin/bash\n"{sys.executable}" -c "import os\n'
                      f'for _ in range(128): os.write(1, b\'x\' * 65536)"\n')
    helper = tmp_path / "helper.py"
    helper.write_text(SYNC_UNDER_A_FILE_LIMIT)
    out = subprocess.run([sys.executable, str(helper), str(script), str(tmp_path)], cwd=ROOT,
                         capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0, out.stderr
    sync = json.loads(out.stdout)
    assert (sync["exit_code"], sync["timed_out"]) == (0, False)
    assert sync["output"] == "x" * effects.SYNC_OUTPUT_MAX_BYTES
    assert sync["output_bytes"] == 8 * MIB
    assert sync["output_dropped_bytes"] == 8 * MIB - effects.SYNC_OUTPUT_MAX_BYTES


def test_the_sync_cap_never_cuts_a_character_in_half(tmp_path, monkeypatch):
    script = tmp_path / "sync.sh"
    script.write_text("#!/bin/bash\nprintf 'é%.0s' $(seq 1 100)\necho ' err' >&2\n")
    monkeypatch.setattr(effects, "SYNC_OUTPUT_MAX_BYTES", 51)
    sync = effects.run_sync_script(str(script), "1.1.0", str(tmp_path))
    assert sync["output"] == "é" * 25
    assert (sync["output_bytes"], sync["output_dropped_bytes"]) == (205, 155)


# ── K-113: git output that is proof is complete or refused ───────────────────


@pytest.fixture
def repo(tmp_path):
    path = Path(make_git_repo(tmp_path / "plain-repo"))
    for n in range(12):
        (path / f"{n}.txt").write_text(f"{n}\n")
        git(path, "add", ".")
        git(path, "commit", "-q", "-m", f"feat: change number {n}")
        git(path, "tag", f"t{n}")
    return path


def test_a_history_read_over_the_limit_is_refused_not_cut(repo, monkeypatch):
    whole = gitinfo.git_read(repo, "log", "--format=%H %s")[1]
    assert whole.count("feat: change number") == 12
    monkeypatch.setattr(gitinfo, "GIT_OUTPUT_MAX_BYTES", len(whole.encode()))
    assert gitinfo.git_read(repo, "log", "--format=%H %s")[1] == whole  # exactly the limit
    monkeypatch.setattr(gitinfo, "GIT_OUTPUT_MAX_BYTES", len(whole.encode()) - 1)
    with pytest.raises(gitinfo.GitReadError, match="limit"):
        gitinfo.git_read(repo, "log", "--format=%H %s")


def test_a_blob_over_the_limit_is_refused_not_cut(repo, monkeypatch):
    (repo / "big.bin").write_bytes(b"\x00\xff" * 4096)
    git(repo, "add", "big.bin")
    git(repo, "commit", "-q", "-m", "chore: big")
    head = git(repo, "rev-parse", "HEAD")
    assert gitinfo.git_read_blob(repo, head, "big.bin") == b"\x00\xff" * 4096
    monkeypatch.setattr(gitinfo, "GIT_OUTPUT_MAX_BYTES", 8191)
    with pytest.raises(gitinfo.GitReadError, match="limit"):
        gitinfo.git_read_blob(repo, head, "big.bin")


def test_remote_refs_over_the_limit_are_refused_not_cut(repo, tmp_path, monkeypatch):
    """The `ls-remote` answer is what the push proof is built from."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main", "--tags")
    refs = effects._ls_remote(str(repo), "origin")
    head = git(repo, "rev-parse", "HEAD")
    assert refs["refs/tags/t11"] == head == refs["refs/heads/main"]
    assert sum(ref.startswith("refs/tags/") for ref in refs) == 12
    monkeypatch.setattr(gitinfo, "GIT_OUTPUT_MAX_BYTES", 300)
    with pytest.raises(ReleaseRefused, match="cannot be read") as exc:
        effects._ls_remote(str(repo), "origin")
    assert "limit" in exc.value.details["git_error"]


def test_a_streamed_line_over_the_limit_is_refused(repo, monkeypatch):
    assert list(gitinfo.git_read_lines(repo, "rev-list", "HEAD")) == \
        git(repo, "rev-list", "HEAD").splitlines()
    monkeypatch.setattr(gitinfo, "GIT_LINE_MAX_BYTES", 16)
    with pytest.raises(gitinfo.GitReadError, match="limit"):
        list(gitinfo.git_read_lines(repo, "rev-list", "HEAD"))


def _fake_git(tmp_path: Path, monkeypatch, body: str) -> None:
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fd = os.open(fake, os.O_WRONLY | os.O_CREAT, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/bash\n" + body)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")


def test_a_timed_out_streamed_read_takes_its_children_down(repo, tmp_path, family, monkeypatch):
    """R8a-2 Q5: the streamed reader killed only the git process."""
    _fake_git(tmp_path, monkeypatch, "echo " + "a" * 40 + "\n" + bash_family(family,
                                                                             barrier=False))
    with pytest.raises(gitinfo.GitReadError, match="timed out"):
        list(gitinfo.git_read_lines(str(repo), "rev-list", "HEAD", timeout=1.5))
    family.assert_started()
    assert family.alive() == []


def test_a_timed_out_git_command_takes_its_children_down(repo, tmp_path, family, monkeypatch):
    _fake_git(tmp_path, monkeypatch, bash_family(family, barrier=False))
    started = time.monotonic()
    with pytest.raises(gitinfo.GitWriteError, match="timed out"):
        gitinfo.git_write(str(repo), "commit", "-m", "x", timeout=1.5)
    assert time.monotonic() - started < 15
    family.assert_started()
    assert family.alive() == []
