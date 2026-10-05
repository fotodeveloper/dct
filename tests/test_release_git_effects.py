"""The release's GIT effects (K-79, K-96 hooks / D1 / D7, K-97 F2 / F4) —
the production effects layer on real throwaway repositories with real hooks
(executable bash scripts) and a BARE remote under tmp_path:

- K-96: the release commit and the attestation commit go through the
  repository's hook lifecycle (never skipped); a refusing hook leaves the
  record unchanged and nothing tagged. Since K-103 the lifecycle is run
  explicitly (`git hook run` + commit-tree on the pinned parent + a
  compare-and-swap of the ref) — a hook that CHANGES the content or the
  message, and the races at the publication boundary, are pinned in
  tests/test_release_round4_git.py;
- K-79: the real index is updated only under git's own index.lock protocol
  (a foreign index.lock is refused and left alone) and the USER'S staged
  blobs survive a concurrent stage at both windows (before the commit and
  during it), a concurrent HEAD move fails the publication's CAS;
- K-96 D7: one operation lock per repository — a second mutating command
  (also from another worktree, also another process) refuses at once;
- K-96 D1: the `clog release` alias needs --confirm;
- K-97 F2 / F4: the workflow pushes the release commit + tag atomically to
  the consented target and verifies it; a failed push is retried alone by
  resume (no new bump / commit / tag); the stored consent is what resume
  executes.

Barriers are threading.Event or the hook / on_point itself — no sleeps.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import alias_argv, git, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    _exit,
    apply,
    assert_released,
    interrupt_at,
    make_release_repo,
    proven,
    remote_refs,
    resume,
    sync_runs,
    use_cli,
)

from dct.core.releases import get_release
from dct.workflows.release import effects, oplock
from dct.workflows.release.effects import ReleaseRefused, attest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Every test here creates commits through the effects layer."""


@pytest.fixture
def proj(engine, tmp_path):
    """The release repository of test_release_effects (bare remote, upstream)."""
    return make_release_repo(engine, tmp_path)


@pytest.fixture
def cli_proj(proj, engine, monkeypatch):
    return use_cli(proj, engine, monkeypatch)


def hooks_dir(repo: Path) -> Path:
    """The main checkout's hooks directory (the test helper `git` itself
    disables hooks, so it cannot be asked)."""
    return repo / ".git" / "hooks"


def install_hook(directory: Path, name: str, body: str) -> Path:
    """An executable bash hook (os.open with mode 0o755 — no shell chmod)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/bash\n" + body)
    return path


def foreign_blob(repo: Path, tmp_path: Path, content: str) -> str:
    """A blob of `content` in the repository's object database."""
    scratch = tmp_path / "foreign-content"
    scratch.write_text(content)
    return git(repo, "hash-object", "-w", str(scratch))


def stage_blob(repo: Path, rel: str, blob: str) -> None:
    """Another process stages `blob` for `rel` in the REAL index."""
    git(repo, "update-index", "--cacheinfo", f"100644,{blob},{rel}")


def staged_blob(repo: Path, rel: str) -> str:
    return git(repo, "ls-files", "-s", "--", rel).split()[1]


def common_dir(repo: Path) -> str:
    return git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")


def local_tag(repo: Path, name: str) -> str:
    return git(repo, "tag", "--list", name)


# ── K-96: the hook lifecycle ─────────────────────────────────────────────────


def test_release_commit_and_push_run_the_repositorys_hooks(engine, proj, tmp_path):
    repo, _ = proj
    trace = tmp_path / "hooks.log"
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-push"):
        install_hook(hooks_dir(repo), name, f'echo "{name}" >> "{trace}"\n')
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])
    assert trace.read_text().split() == [
        "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-push"]
    assert [s["step"] for s in out["steps"]][-3:] == ["tag", "push", "complete"]


@pytest.mark.parametrize("hook", ["pre-commit", "commit-msg"])
def test_a_refusing_commit_hook_leaves_the_record_and_nothing_tagged(engine, proj, hook):
    repo, log = proj
    path = install_hook(hooks_dir(repo), hook, "echo 'refused by policy' >&2\nexit 1\n")
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    index = git(repo, "ls-files", "-s")
    before_remote = remote_refs(repo)
    with pytest.raises(ReleaseRefused, match="refused") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert "refused by policy" in exc.value.details["git_error"]
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert local_tag(repo, "v1.1.0") == ""
    assert git(repo, "ls-files", "-s") == index  # the real index untouched
    assert remote_refs(repo) == before_remote
    # The owner fixes the policy problem; resume finishes exactly once.
    path.unlink()
    assert resume(engine, repo)["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])
    assert sync_runs(log) == ["1.1.0"]


def test_attestation_commit_runs_the_hooks_and_a_refusal_attests_nothing(engine, proj,
                                                                          tmp_path):
    repo, _ = proj
    trace = tmp_path / "hooks.log"
    from dct.core.changelog import add_entry

    a = add_entry(engine, SLUG, "added", "squashed work")["id"]
    head = git(repo, "rev-parse", "HEAD")
    refuse = install_hook(hooks_dir(repo), "commit-msg", "echo no >&2\nexit 1\n")
    with pytest.raises(ReleaseRefused, match="refused"):
        attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    assert git(repo, "rev-parse", "HEAD") == head
    refuse.unlink()
    for name in ("pre-commit", "commit-msg", "post-commit"):
        install_hook(hooks_dir(repo), name, f'echo "{name}" >> "{trace}"\n')
    out = attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    assert out["outcome"] == "attested" and git(repo, "rev-parse", "HEAD^") == head
    assert trace.read_text().split() == ["pre-commit", "commit-msg", "post-commit"]
    assert git(repo, "rev-parse", "HEAD^{tree}") == git(repo, "rev-parse", f"{head}^{{tree}}")


# ── K-79: git's index.lock protocol and the user's staged blobs ──────────────


def test_window_a_a_stage_after_the_index_check_is_preserved(engine, proj, tmp_path):
    """Another process stages a DIFFERENT blob for a release path after the
    commit step checked the index (on_point barrier `before_commit`): the
    release commit is still exactly the approved tree, and the user's blob
    is NOT overwritten — the index update refuses and says what happened."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    blob = foreign_blob(repo, tmp_path, "[project]\nname = 'mine'\n")

    def concurrent_stage(point: str) -> None:
        if point == "before_commit":
            stage_blob(repo, "pyproject.toml", blob)

    with pytest.raises(ReleaseRefused, match="staged") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=concurrent_stage)
    assert exc.value.details["foreign_staged"] == ["pyproject.toml"]
    assert staged_blob(repo, "pyproject.toml") == blob  # the user's blob survives
    record = get_release(engine, SLUG, "1.1.0")
    head = git(repo, "rev-parse", "HEAD")
    assert exc.value.details["commit"] == head  # effect performed: the commit exists
    assert git(repo, "rev-parse", "HEAD^") == candidate
    assert git(repo, "rev-parse", "HEAD^{tree}") == record["manifest"]["tree"]
    assert record["status"] == "files_written" and local_tag(repo, "v1.1.0") == ""
    # Still staged → resume refuses again, the blob stays.
    with pytest.raises(ReleaseRefused, match="staged"):
        resume(engine, repo)
    assert staged_blob(repo, "pyproject.toml") == blob
    # The owner unstages it; resume adopts the release commit and finishes.
    git(repo, "reset", "-q", "--", "pyproject.toml")
    assert resume(engine, repo)["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])


def test_window_b_a_stage_during_the_commit_is_preserved(engine, proj, tmp_path):
    """The index changes WHILE the commit runs (a pre-commit hook — another
    process — stages into the REAL index): the commit is the approved tree,
    the user's blob survives, and other foreign staging is kept untouched."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    mine = foreign_blob(repo, tmp_path, "my staged changelog\n")
    install_hook(hooks_dir(repo), "pre-commit", (
        f'env -u GIT_INDEX_FILE git update-index --cacheinfo "100644,{mine},changelog.md"\n'))
    with pytest.raises(ReleaseRefused, match="staged") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["foreign_staged"] == ["changelog.md"]
    assert staged_blob(repo, "changelog.md") == mine
    record = get_release(engine, SLUG, "1.1.0")
    assert git(repo, "rev-parse", "HEAD^{tree}") == record["manifest"]["tree"]
    assert git(repo, "rev-parse", "HEAD^") == candidate
    assert record["status"] == "files_written"


def test_window_b_head_moved_during_the_commit_fails_its_compare_and_swap(engine, proj):
    """HEAD moves WHILE the hooks run: the publication's compare-and-swap
    (old value = the pinned candidate) refuses, nothing is published by
    dct, the other process's HEAD stays."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    install_hook(hooks_dir(repo), "pre-commit", (
        'other=$(git commit-tree "HEAD^{tree}" -p HEAD -m "someone else")\n'
        'git update-ref HEAD "$other"\n'))
    with pytest.raises(ReleaseRefused) as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    head = git(repo, "rev-parse", "HEAD")
    assert head != candidate and git(repo, "log", "-1", "--format=%s") == "someone else"
    assert exc.value.details["head"] == head
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    assert local_tag(repo, "v1.1.0") == ""
    assert "release: v1.1.0" not in git(repo, "log", "--all", "--format=%s")


@pytest.mark.parametrize("point", ["after_files_written", "after_commit"])
def test_a_foreign_index_lock_is_refused_and_left_alone(engine, proj, point):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at(point))
    head = git(repo, "rev-parse", "HEAD")
    index_path = Path(git(repo, "rev-parse", "--git-path", "index"))
    index_path = index_path if index_path.is_absolute() else repo / index_path
    lock = index_path.with_name("index.lock")
    lock.write_bytes(b"another git process")
    index = index_path.read_bytes()
    with pytest.raises(ReleaseRefused, match="index.lock"):
        resume(engine, repo)
    assert lock.read_bytes() == b"another git process"  # never deleted, never written
    assert index_path.read_bytes() == index
    assert git(repo, "rev-parse", "HEAD") == head
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    lock.unlink()  # the other git process finished
    resume(engine, repo)
    assert_released(engine, repo, candidate, [a])


# ── K-96 D7: one operation lock per repository ───────────────────────────────


def _in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as exc:  # noqa: BLE001 — reported to the test
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_a_second_mutating_command_refuses_while_a_release_runs(engine, proj, tmp_path):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), candidate)
    entered, go = threading.Event(), threading.Event()

    def barrier(point: str) -> None:
        if point == "before_commit":
            entered.set()
            assert go.wait(60), "the test never released the barrier"

    # Digests are computed BEFORE the race (prepare takes the lock itself).
    from conftest import approved_plan_digest, approved_proposal_digest

    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    proposal = approved_proposal_digest(engine, SLUG, repo, plan_digest=digest,
                                        version="1.1.0", entry_ids=[a])
    thread, box = _in_thread(lambda: effects.apply_release(
        engine, SLUG, version="1.1.0", entry_ids=[a], confirm=True, plan_digest=digest,
        proposal_digest=proposal, cwd=str(repo), on_point=barrier))
    try:
        assert entered.wait(60), box.get("error")
        lock = oplock.lock_path(git_common_dir=common_dir(repo))
        assert oplock.read_holder(lock)["action"] == "apply"
        attempts = {
            "resume (main checkout)": lambda: effects.resume_release(engine, SLUG, cwd=str(repo)),
            "resume (linked worktree)": lambda: effects.resume_release(engine, SLUG,
                                                                       cwd=str(wt)),
            "abandon": lambda: effects.abandon(engine, SLUG, "1.1.0", "x", confirm=True),
            "prepare (worktree)": lambda: effects.prepare_release(
                engine, SLUG, version="1.2.0", no_entries=True, plan_digest="0" * 64,
                cwd=str(wt)),
            "attest": lambda: attest(engine, SLUG, [a], confirm=True, cwd=str(wt)),
        }
        for what, call in attempts.items():
            with pytest.raises(ReleaseRefused, match="another dct release operation"):
                call()
            assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written", what
    finally:
        go.set()
        thread.join(60)
    assert box.get("out", {}).get("outcome") == "completed", box.get("error")
    assert_released(engine, repo, candidate, [a])


def test_another_process_holding_the_lock_refuses_apply_before_the_stamp(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    common = common_dir(repo)
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys\nfrom dct.workflows.release import oplock\n"
            f"with oplock.operation_lock(oplock.lock_path(git_common_dir={common!r}), 'other'):\n"
            "    print('locked', flush=True)\n    sys.stdin.readline()\n")],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(ReleaseRefused, match="another dct release operation"):
            effects.apply_release(engine, SLUG, version="1.1.0", entry_ids=[a], confirm=True,
                                  plan_digest="0" * 64, proposal_digest="0" * 64,
                                  cwd=str(repo))
        assert get_release(engine, SLUG, "1.1.0") is None
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(60)
    assert apply(engine, repo, version="1.1.0", entry_ids=[a])["outcome"] == "completed"


def test_the_lock_is_one_per_repository_and_per_plain_directory(proj, tmp_path, monkeypatch):
    repo, _ = proj
    wt = tmp_path / "wt2"
    git(repo, "worktree", "add", "-q", "--detach", str(wt))
    assert oplock.lock_path(git_common_dir=common_dir(repo)) == \
        oplock.lock_path(git_common_dir=common_dir(wt))
    monkeypatch.setenv("DCT_DATA_DIR", str(tmp_path / "data"))
    plain = tmp_path / "plain"
    plain.mkdir()
    path = oplock.lock_path(project_path=str(plain))
    assert path.startswith(str(tmp_path / "data" / "release-locks"))
    assert oplock.lock_path(project_path=str(plain / ".")) == path
    with oplock.operation_lock(path, "one"), pytest.raises(oplock.OperationLocked), \
            oplock.operation_lock(path, "two"):
        pass
    with oplock.operation_lock(path, "three"):  # released after use
        pass


# ── K-96 D1: the alias needs an explicit confirmation ────────────────────────


def test_clog_release_alias_needs_confirm(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    assert _exit(monkeypatch, ["clog", "release", "-P", SLUG, "1.1.0"]) == 1
    assert "--confirm" in capsys.readouterr().err
    assert get_release(engine, SLUG, "1.1.0") is None
    run_cli(monkeypatch, alias_argv(engine, SLUG, [
        "clog", "release", "-P", SLUG, "1.1.0", "--confirm"]))
    assert_released(engine, repo, candidate, [a])


# ── K-97 F2: the push ────────────────────────────────────────────────────────


def test_a_rejected_push_is_retried_alone_by_resume(engine, proj, tmp_path):
    repo, log = proj
    hook = install_hook(tmp_path / "remote.git" / "hooks", "pre-receive",
                        "echo 'remote says no' >&2\nexit 1\n")
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    before_remote = remote_refs(repo)
    with pytest.raises(ReleaseRefused, match="push") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert "remote says no" in exc.value.details["git_error"]
    record = get_release(engine, SLUG, "1.1.0")
    release_commit = git(repo, "rev-parse", "HEAD")
    assert record["status"] == "tagged" and record["release_commit_sha"] == release_commit
    assert git(repo, "rev-parse", "v1.1.0") == release_commit
    assert remote_refs(repo) == before_remote  # atomic: neither branch nor tag
    hook.unlink()
    out = resume(engine, repo)
    assert out["outcome"] == "completed"
    assert [s["result"] for s in out["steps"] if s["step"] in ("commit", "tag", "push")] == [
        "verified", "verified", "done"]
    assert git(repo, "rev-parse", "HEAD") == release_commit  # no new commit
    assert_released(engine, repo, candidate, [a])
    assert sync_runs(log) == ["1.1.0"]


def test_a_remote_tag_of_v_at_another_commit_refuses_before_the_stamp(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    git(repo, "push", "-q", "origin", "v1.0.0^{commit}:refs/tags/v1.1.0")
    with pytest.raises(ReleaseRefused, match="remote") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["remote_tags"]
    assert get_release(engine, SLUG, "1.1.0") is None


def test_a_remote_tag_conflict_after_the_tag_refuses_the_push(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    git(repo, "push", "-q", "origin", "v1.0.0^{commit}:refs/tags/v1.1.0")
    with pytest.raises(ReleaseRefused, match="another commit"):
        resume(engine, repo)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"
    assert "refs/heads/main" in remote_refs(repo)
    assert remote_refs(repo)["refs/heads/main"] != git(repo, "rev-parse", "HEAD")


def test_no_upstream_refuses_at_the_pre_check_with_the_choice(engine, proj):
    repo, _ = proj
    git(repo, "branch", "--unset-upstream")
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="upstream") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert "--push-remote" in str(exc.value) and "--no-push" in str(exc.value)
    assert get_release(engine, SLUG, "1.1.0") is None


def test_an_explicit_no_push_release_completes_locally(engine, proj):
    repo, _ = proj
    git(repo, "branch", "--unset-upstream")
    a = proven(engine, repo, "feature a")
    before_remote = remote_refs(repo)
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True)
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    assert record["consent"]["push_target"] is None and record["status"] == "completed"
    assert remote_refs(repo) == before_remote
    assert {"step": "push", "result": "skipped", "reason": "no push target approved"} in \
        out["steps"]


def test_an_explicit_push_target_is_used(engine, proj, tmp_path):
    repo, _ = proj
    other = tmp_path / "other.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(other))
    git(repo, "remote", "add", "mirror", str(other))
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a], push_remote="mirror",
          push_branch="release")
    record = get_release(engine, SLUG, "1.1.0")
    assert record["consent"]["push_target"] == {
        "remote": "mirror", "url": str(other), "branch": "release", "tag": "v1.1.0"}
    out = git(repo, "ls-remote", "mirror")
    assert f"{record['release_commit_sha']}\trefs/heads/release" in out
    assert f"{record['release_commit_sha']}\trefs/tags/v1.1.0" in out


# ── K-97 F4: resume executes the stored consent ──────────────────────────────


def test_resume_pushes_to_the_consented_target_not_a_changed_upstream(engine, proj, tmp_path):
    repo, _ = proj
    other = tmp_path / "other.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(other))
    git(repo, "remote", "add", "other", str(other))
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    git(repo, "config", "branch.main.remote", "other")  # the upstream changes
    assert resume(engine, repo)["outcome"] == "completed"  # no new approval needed
    assert git(repo, "ls-remote", "other") == ""
    git(repo, "config", "branch.main.remote", "origin")
    assert_released(engine, repo, candidate, [a])


def test_resume_refuses_a_push_target_outside_the_consent(engine, proj, tmp_path):
    repo, _ = proj
    other = tmp_path / "other.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(other))
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    git(repo, "remote", "set-url", "origin", str(other))
    with pytest.raises(ReleaseRefused, match="consent"):
        resume(engine, repo)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"
    assert git(repo, "ls-remote", "origin") == ""
