"""The exit fixes of the push path (K-123, K-124, K-126, K-134) — what a
release may reach is exactly what the owner approved:

- K-123: a NEW release that pushes names the COMPLETE approved target
  (`--push-remote`, `--push-branch`, `--push-url` — what prepare showed);
  apply never re-selects the upstream. A remote re-pointed, a branch whose
  upstream changed or another branch at the same commit between prepare and
  apply send nothing to a destination nobody approved, and stamp nothing.
- K-124: a push URL that git would read as a REMOTE NAME (any config scope,
  the legacy `remotes/` / `branches/` files; with or without a slash) is
  refused before the stamp, by the takeover and before every push.
- K-126 / K-134: the ONE push command sends the two explicit refs and
  nothing else — no tag `push.followTags` would add, no submodule commit
  `push.recurseSubmodules` / `submodule.recurse` would publish to the
  submodule's own remote — for a new release and for the retry of the
  missing refs.

Real bare repositories under tmp_path stand for every remote; every push is
a real `git push`. The global git configuration is the isolated file of the
`git_identity` fixture (GIT_CONFIG_GLOBAL), never the machine's.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import alias_proposal, approved_plan_digest, git, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    cli_digest,
    interrupt_at,
    make_release_repo,
    proven,
    resume,
    use_cli,
)
from test_release_executor import world
from test_release_round4_push import bare, fetch_address, refs
from test_release_round4_reconstruct import legacy

from dct.core.releases import get_release
from dct.workflows.release import effects
from dct.workflows.release.effects import ReleaseRefused

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Every test here creates commits through the effects layer."""


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def global_config(*args: str) -> None:
    """`git config` on the ISOLATED global file of the git_identity fixture
    (the conftest `git` helper drops GIT_* — `--global` there would be the
    machine's file)."""
    git(Path(os.environ["GIT_CONFIG_GLOBAL"]).parent, "config", "--file",
        os.environ["GIT_CONFIG_GLOBAL"], *args)


def prepared(engine, repo, a: int) -> tuple[dict, dict]:
    """Plan + prepare for 1.1.0 with entry `a` → (the digests apply takes,
    the push target prepare SHOWED — what the owner approves)."""
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    out = effects.prepare_release(engine, SLUG, version="1.1.0", entry_ids=[a],
                                  plan_digest=digest, cwd=str(repo))
    return ({"plan_digest": digest, "proposal_digest": out["details"]["proposal_sha256"]},
            out["details"]["push_target"])


def approved_flags(shown: dict) -> dict:
    """The complete push choice of the approved prepare, as apply takes it."""
    return {"push_remote": shown["remote"], "push_branch": shown["branch"],
            "push_url": shown["url"]}


def run_apply(engine, repo, a: int, digests: dict, **push) -> dict:
    return effects.apply_release(engine, SLUG, version="1.1.0", entry_ids=[a], confirm=True,
                                 cwd=str(repo), **digests, **push)


# ── K-123: the push target of a new release is the approved one ──────────────


def _repoint_the_remote(repo: Path, tmp_path: Path) -> Path:
    other = bare(tmp_path, "other.git", seed_from=repo)
    git(repo, "remote", "set-url", "origin", str(other))
    return other


def _move_the_upstream(repo: Path, tmp_path: Path) -> Path:
    other = bare(tmp_path, "other.git", seed_from=repo)
    git(repo, "remote", "add", "fork", str(other))
    git(repo, "config", "branch.main.remote", "fork")
    return other


def _switch_to_a_branch_at_the_same_commit(repo: Path, tmp_path: Path) -> Path:
    other = bare(tmp_path, "other.git", seed_from=repo)
    git(repo, "remote", "add", "fork", str(other))
    git(repo, "switch", "-q", "-c", "side")
    git(repo, "config", "branch.side.remote", "fork")
    git(repo, "config", "branch.side.merge", "refs/heads/main")
    return other


CHANGES = {
    "remote_url": _repoint_the_remote,
    "upstream": _move_the_upstream,
    "branch_at_the_same_commit": _switch_to_a_branch_at_the_same_commit,
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_target_changed_after_the_approval_gets_nothing_from_a_default_apply(
        engine, proj, tmp_path, change):
    """prepare shows origin → A and the owner approves it; then the
    configuration changes so that the default (upstream) target is B. An
    apply without the approved push choice must not pick B: refused before
    the stamp, the database, the checkout and both repositories unchanged."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    approved = fetch_address(repo)
    digests, shown = prepared(engine, repo, a)
    assert shown == {"remote": "origin", "url": str(approved), "branch": "main",
                     "tag": "v1.1.0"}
    other = CHANGES[change](repo, tmp_path)
    before = world(engine, repo)[:2], refs(approved), refs(other)

    with pytest.raises(ReleaseRefused) as exc:
        run_apply(engine, repo, a, digests)

    assert (world(engine, repo)[:2], refs(approved), refs(other)) == before
    assert get_release(engine, SLUG, "1.1.0") is None  # refused before the stamp
    assert exc.value.details["push_target"] is None
    for flag in ("--push-remote", "--push-branch", "--push-url"):
        assert flag in str(exc.value)


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_the_approved_target_is_the_only_destination_after_such_a_change(
        engine, proj, tmp_path, change):
    """The same changes with the COMPLETE approved choice: the release goes
    to the approved repository and branch — or is refused when the approved
    URL is not a push address of its remote any more. The repository the
    changed configuration points at never receives a ref."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    approved = fetch_address(repo)
    digests, shown = prepared(engine, repo, a)
    other = CHANGES[change](repo, tmp_path)
    before_other, before_approved, before = refs(other), refs(approved), world(engine, repo)[:2]

    if change == "remote_url":
        with pytest.raises(ReleaseRefused, match="not a push URL"):
            run_apply(engine, repo, a, digests, **approved_flags(shown))
        assert get_release(engine, SLUG, "1.1.0") is None
        assert (world(engine, repo)[:2], refs(approved)) == (before, before_approved)
    else:
        out = run_apply(engine, repo, a, digests, **approved_flags(shown))
        assert out["outcome"] == "completed"
        record = get_release(engine, SLUG, "1.1.0")
        assert record["consent"]["push_target"] == shown
        head = record["release_commit_sha"]
        assert refs(approved) == {**before_approved, "refs/heads/main": head,
                                  "refs/tags/v1.1.0": head}
    assert refs(other) == before_other


@pytest.mark.parametrize("omitted", ["push_remote", "push_branch", "push_url"])
def test_an_incomplete_push_choice_is_refused_before_the_stamp(engine, proj, omitted):
    """Each of the three is required — the branch is not completed from the
    upstream, the URL not from the remote's configuration."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    digests, shown = prepared(engine, repo, a)
    push = approved_flags(shown)
    del push[omitted]
    before = world(engine, repo)
    with pytest.raises(ReleaseRefused) as exc:
        run_apply(engine, repo, a, digests, **push)
    assert world(engine, repo) == before and get_release(engine, SLUG, "1.1.0") is None
    assert exc.value.details.get("push_target", "absent") is None
    assert "--" + omitted.replace("_", "-") in str(exc.value)


def test_a_choice_that_differs_from_the_configured_target_is_refused(engine, proj, tmp_path):
    """A complete set that does not name a push address of that remote (or
    names no remote at all) is a mismatch, not a new destination."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    digests, shown = prepared(engine, repo, a)
    stranger = bare(tmp_path, "stranger.git", seed_from=repo)
    before = world(engine, repo), refs(stranger)
    for push in ({**approved_flags(shown), "push_url": str(stranger)},
                 {**approved_flags(shown), "push_remote": "nowhere"}):
        with pytest.raises(ReleaseRefused):
            run_apply(engine, repo, a, digests, **push)
        assert (world(engine, repo), refs(stranger)) == before
    assert get_release(engine, SLUG, "1.1.0") is None


def test_the_alias_needs_the_same_complete_choice(engine, proj, tmp_path, monkeypatch, capsys):
    """`dct clog release` is apply: the default (no push flag) form is
    refused before the stamp there too; with the approved choice it
    releases to the approved target."""
    repo, _ = use_cli(proj, engine, monkeypatch)
    a = proven(engine, repo, "feature a")
    approved = fetch_address(repo)
    proposal = alias_proposal(engine, SLUG, repo, version="1.1.0")
    other = _repoint_the_remote(repo, tmp_path)
    before = world(engine, repo)[:2], refs(approved), refs(other)
    argv = ["clog", "release", "-P", SLUG, "1.1.0", "--confirm", *proposal]

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, argv)
    assert exc.value.code == 1 and "--push-url" in capsys.readouterr().err
    assert (world(engine, repo)[:2], refs(approved), refs(other)) == before
    assert get_release(engine, SLUG, "1.1.0") is None

    git(repo, "remote", "set-url", "origin", str(approved))
    run_cli(monkeypatch, [*argv, "--push-remote", "origin", "--push-branch", "main",
                          "--push-url", str(approved)])
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(approved)["refs/tags/v1.1.0"] == head and refs(other) == before[2]
    assert a in get_release(engine, SLUG, "1.1.0")["entry_ids"]


def test_cli_apply_without_the_push_choice_is_refused(engine, proj, monkeypatch, capsys):
    repo, _ = use_cli(proj, engine, monkeypatch)
    a = proven(engine, repo, "feature a")
    approval = cli_digest(engine, repo, "1.1.0", [a], push=False)
    before = world(engine, repo)
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["release", "apply", "-P", SLUG, "--version", "1.1.0",
                              "--entries", str(a), "--confirm", "--json", *approval])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["details"]["push_target"] is None
    assert world(engine, repo) == before


def test_no_push_stays_an_explicit_local_release(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    digests, _ = prepared(engine, repo, a)
    before = refs(fetch_address(repo))
    out = run_apply(engine, repo, a, digests, no_push=True)
    assert out["outcome"] == "completed"
    assert get_release(engine, SLUG, "1.1.0")["consent"]["push_target"] is None
    assert refs(fetch_address(repo)) == before


def test_resume_uses_the_target_stored_in_the_consent(engine, proj, tmp_path):
    """After the stamp the consent is the only source: an upstream moved
    meanwhile is not consulted, and resume takes no push choice."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    approved = fetch_address(repo)
    digests, shown = prepared(engine, repo, a)
    with pytest.raises(Interrupted):
        run_apply(engine, repo, a, digests, on_point=interrupt_at("after_tagged"),
                  **approved_flags(shown))
    other = _move_the_upstream(repo, tmp_path)
    before_other = refs(other)
    assert resume(engine, repo)["outcome"] == "completed"
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(approved)["refs/tags/v1.1.0"] == head and refs(other) == before_other


# ── K-124: a push URL git reads as a remote name ─────────────────────────────


def _ignore(repo: Path, name: str) -> None:
    """Keep a repository placed INSIDE the work tree out of `git status`."""
    with open(Path(repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
        fh.write(f"/{name.split('/')[0]}/\n")


def _define_locally(repo: Path, name: str, fetch: Path, push: Path) -> None:
    git(repo, "config", f"remote.{name}.url", str(fetch))
    git(repo, "config", f"remote.{name}.pushurl", str(push))


def _define_globally(repo: Path, name: str, fetch: Path, push: Path) -> None:
    global_config(f"remote.{name}.url", str(fetch))
    global_config(f"remote.{name}.pushurl", str(push))


def _legacy_remotes_file(repo: Path, name: str, fetch: Path, push: Path) -> None:
    Path(repo, ".git", "remotes").mkdir(exist_ok=True)
    Path(repo, ".git", "remotes", name).write_text(f"URL: {fetch}\nPush: HEAD\n")


def _legacy_branches_file(repo: Path, name: str, fetch: Path, push: Path) -> None:
    Path(repo, ".git", "branches").mkdir(exist_ok=True)
    Path(repo, ".git", "branches", name).write_text(f"{push}\n")


def colliding(repo: Path, tmp_path: Path, name: str, define) -> tuple[Path, Path, Path]:
    """origin's push URL is the relative path `name` (a real repository
    inside the checkout); a REMOTE of that name reads from A and pushes to
    B. → (the repository the path names, A, B)."""
    named = bare(repo, name, seed_from=repo)
    _ignore(repo, name)
    reads, pushes = (bare(tmp_path, "A.git", seed_from=repo),
                     bare(tmp_path, "B.git", seed_from=repo))
    define(repo, name, reads, pushes)
    git(repo, "config", "remote.origin.pushurl", name)
    return named, reads, pushes


@pytest.mark.parametrize("define", [_define_locally, _define_globally],
                         ids=["local-config", "global-config-only"])
@pytest.mark.parametrize("name", ["publish", "group/publish"])
def test_a_push_url_that_is_a_remote_name_is_refused_before_the_stamp(
        engine, proj, tmp_path, name, define):
    """git resolves the string as the remote: `ls-remote` would read A, the
    push would land in B — neither is the repository the approved path
    names. Refused before the stamp; A, B and the named repository get
    nothing; the message names the explicit forms."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    named, reads, pushes = colliding(repo, tmp_path, name, define)
    digests, shown = prepared(engine, repo, a)
    before = world(engine, repo), refs(named), refs(reads), refs(pushes)

    with pytest.raises(ReleaseRefused) as exc:
        run_apply(engine, repo, a, digests, push_remote="origin", push_branch="main",
                  push_url=name)

    assert (world(engine, repo), refs(named), refs(reads), refs(pushes)) == before
    assert get_release(engine, SLUG, "1.1.0") is None
    assert shown is None  # prepare shows no target it could not address literally
    assert exc.value.details["url"] == name and exc.value.details["remote_name"] == name
    assert f"./{name}" in str(exc.value) and "absolute path" in str(exc.value)


@pytest.mark.parametrize("define", [_legacy_remotes_file, _legacy_branches_file],
                         ids=["remotes-file", "branches-file"])
def test_a_push_url_named_by_a_legacy_remote_file_is_refused(engine, proj, tmp_path, define):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    named, reads, pushes = colliding(repo, tmp_path, "publish", define)
    digests, _ = prepared(engine, repo, a)
    before = world(engine, repo), refs(named), refs(reads), refs(pushes)
    with pytest.raises(ReleaseRefused) as exc:
        run_apply(engine, repo, a, digests, push_remote="origin", push_branch="main",
                  push_url="publish")
    assert (world(engine, repo), refs(named), refs(reads), refs(pushes)) == before
    assert exc.value.details["remote_name"] == "publish"


def test_the_explicit_path_form_reaches_the_repository_it_names(engine, proj, tmp_path):
    """`./publish` is never a remote name: the release goes to that
    repository although a remote called `publish` exists."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    named, reads, pushes = colliding(repo, tmp_path, "publish", _define_locally)
    git(repo, "config", "remote.origin.pushurl", "./publish")
    before = refs(named), refs(reads), refs(pushes)
    digests, shown = prepared(engine, repo, a)
    assert shown["url"] == "./publish"
    assert run_apply(engine, repo, a, digests, **approved_flags(shown))["outcome"] == "completed"
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(named) == {**before[0], "refs/heads/main": head, "refs/tags/v1.1.0": head}
    assert (refs(reads), refs(pushes)) == before[1:]


def test_a_remote_named_like_the_approved_url_stops_the_resumed_push(engine, proj, tmp_path):
    """The collision appears AFTER the consent (a remote added, here only
    in the global configuration): the check runs before every push — the
    record stays `tagged`, nothing reaches the substituted repository."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    named = bare(repo, "publish", seed_from=repo)
    _ignore(repo, "publish")
    git(repo, "config", "remote.origin.pushurl", "publish")
    digests, shown = prepared(engine, repo, a)
    assert shown["url"] == "publish"  # a plain relative path while no remote has that name
    with pytest.raises(Interrupted):
        run_apply(engine, repo, a, digests, on_point=interrupt_at("after_tagged"),
                  **approved_flags(shown))
    reads, pushes = (bare(tmp_path, "A.git", seed_from=repo),
                     bare(tmp_path, "B.git", seed_from=repo))
    _define_globally(repo, "publish", reads, pushes)
    before = refs(named), refs(reads), refs(pushes)
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, repo)
    assert (refs(named), refs(reads), refs(pushes)) == before
    assert exc.value.details["remote_name"] == "publish"
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"


def test_a_takeover_refuses_a_push_url_that_is_a_remote_name(engine, proj, tmp_path):
    repo, _ = proj
    legacy(engine, repo, tag=None)
    effects.reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    named, reads, pushes = colliding(repo, tmp_path, "group/publish", _define_locally)
    before = world(engine, repo), refs(named), refs(reads), refs(pushes)
    with pytest.raises(ReleaseRefused) as exc:
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), push_remote="origin",
                         push_branch="main")
    assert exc.value.details["remote_name"] == "group/publish"
    assert (world(engine, repo), refs(named), refs(reads), refs(pushes)) == before


# ── K-126 / K-134: the push sends the two explicit refs, nothing else ────────


def with_submodule(repo: Path, tmp_path: Path) -> Path:
    """`sub` — a submodule of `repo` cloned from its own remote, holding a
    commit that remote does NOT have; the superproject's gitlink points at
    it (committed). → the submodule's remote."""
    sub_remote = tmp_path / "sub-remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(sub_remote))
    source = tmp_path / "sub-source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "commit", "-q", "--allow-empty", "-m", "sub: base")
    git(source, "push", "-q", str(sub_remote), "main")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "--quiet", "add",
        str(sub_remote), "sub")
    git(repo, "commit", "-q", "-m", "chore: add sub")
    git(repo / "sub", "commit", "-q", "--allow-empty", "-m", "sub: UNPUBLISHED work")
    git(repo, "add", "sub")
    git(repo, "commit", "-q", "-m", "chore: bump sub")
    return sub_remote


def recursing(repo: Path) -> None:
    """Configuration that makes a plain `git push` publish the submodule's
    commits to the submodule's own remote first."""
    git(repo, "config", "push.recurseSubmodules", "on-demand")
    git(repo, "config", "submodule.recurse", "true")
    global_config("protocol.file.allow", "always")


def annotated_tag_in_the_ancestry(repo: Path, name: str = "private-note") -> None:
    git(repo, "tag", "-a", name, "-m", "annotated, never published", "HEAD")


def release_refs(before: dict, head: str) -> dict:
    return {**before, "refs/heads/main": head, "refs/tags/v1.1.0": head}


def test_a_new_release_never_publishes_submodule_commits(engine, proj, tmp_path):
    """K-134: the consent covers the superproject's destination. With
    `push.recurseSubmodules=on-demand` and `submodule.recurse=true` the
    submodule's remote still receives no ref (the gitlink's commit stays
    unpublished — a limit of what dct publishes, not a failure)."""
    repo, _ = proj
    sub_remote = with_submodule(repo, tmp_path)
    recursing(repo)
    a = proven(engine, repo, "feature a")
    before_sub, before = refs(sub_remote), refs(fetch_address(repo))
    digests, shown = prepared(engine, repo, a)
    assert run_apply(engine, repo, a, digests, **approved_flags(shown))["outcome"] == "completed"
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(sub_remote) == before_sub
    assert refs(fetch_address(repo)) == release_refs(before, head)


@pytest.mark.parametrize("scope", ["repository", "global"])
def test_a_new_release_pushes_no_tag_it_did_not_name(engine, proj, scope):
    """K-126: `push.followTags=true` would add every annotated tag reachable
    from the release commit that the destination lacks."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    annotated_tag_in_the_ancestry(repo)
    if scope == "global":
        global_config("push.followTags", "true")
    else:
        git(repo, "config", "push.followTags", "true")
    before = refs(fetch_address(repo))
    digests, shown = prepared(engine, repo, a)
    assert run_apply(engine, repo, a, digests, **approved_flags(shown))["outcome"] == "completed"
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(fetch_address(repo)) == release_refs(before, head)


def _everything_on(repo: Path, tmp_path: Path) -> Path:
    sub_remote = with_submodule(repo, tmp_path)
    recursing(repo)
    git(repo, "config", "push.followTags", "true")
    global_config("push.followTags", "true")
    return sub_remote


def test_a_new_release_with_follow_tags_and_submodule_recursion_sends_two_refs(
        engine, proj, tmp_path):
    repo, _ = proj
    sub_remote = _everything_on(repo, tmp_path)
    a = proven(engine, repo, "feature a")
    annotated_tag_in_the_ancestry(repo)
    before_sub, before = refs(sub_remote), refs(fetch_address(repo))
    digests, shown = prepared(engine, repo, a)
    assert run_apply(engine, repo, a, digests, **approved_flags(shown))["outcome"] == "completed"
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(fetch_address(repo)) == release_refs(before, head)
    assert refs(sub_remote) == before_sub


def _reject_pushes(remote: Path) -> Path:
    hook = remote / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    fd = os.open(hook, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/sh\necho 'remote: maintenance' >&2\nexit 1\n")
    return hook


@pytest.mark.parametrize("first_attempt", ["failed", "partial"])
def test_the_retry_of_missing_refs_sends_only_them(engine, proj, tmp_path, first_attempt):
    """Resume after a push that FAILED (the destination refused it) or
    reached the destination only PARTLY (the branch is there, the tag is
    not): the retry sends the missing explicit refs — no followed tag, no
    submodule commit."""
    repo, _ = proj
    sub_remote = _everything_on(repo, tmp_path)
    a = proven(engine, repo, "feature a")
    annotated_tag_in_the_ancestry(repo)
    destination = fetch_address(repo)
    before_sub, before = refs(sub_remote), refs(destination)
    digests, shown = prepared(engine, repo, a)
    if first_attempt == "failed":
        hook = _reject_pushes(destination)
        with pytest.raises(ReleaseRefused, match="failed") as exc:
            run_apply(engine, repo, a, digests, **approved_flags(shown))
        assert exc.value.details["push_target"] == shown
        hook.rename(hook.with_name("pre-receive.off"))
    else:
        with pytest.raises(Interrupted):
            run_apply(engine, repo, a, digests, on_point=interrupt_at("before_push"),
                      **approved_flags(shown))
        head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
        git(repo, "push", "-q", "--no-follow-tags", "--recurse-submodules=no",
            str(destination), f"{head}:refs/heads/main")
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"
    assert (refs(sub_remote), {k: v for k, v in refs(destination).items()
                               if k != "refs/heads/main"}) == (
        before_sub, {k: v for k, v in before.items() if k != "refs/heads/main"})

    assert resume(engine, repo)["outcome"] == "completed"
    head = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(destination) == release_refs(before, head)
    assert refs(sub_remote) == before_sub
