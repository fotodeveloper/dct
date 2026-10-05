"""Round 4 — ONE effective push target (K-104).

Plan / prepare, the consent, the pre-stamp control read, the push, its
verification and every resume use the SAME destination: one URL out of the
full `git remote get-url --push --all` of the approved remote — never
silently the first of several, never the fetch URL in place of the push
URL. The commands address that exact URL with explicit refspecs, so a
config change after the consent cannot add or swap a recipient.

Real bare repositories under tmp_path stand for the remotes; the push is a
real `git push`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import approved_plan_digest, git, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    apply,
    assert_released,
    cli_digest,
    interrupt_at,
    make_release_repo,
    proven,
    resume,
    use_cli,
)

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


def bare(tmp_path: Path, name: str, *, seed_from: Path | None = None) -> Path:
    """A bare repository; with `seed_from` it starts with that repo's main
    and v1.0.0 (a second address of the same project)."""
    path = tmp_path / name
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(path))
    if seed_from is not None:
        git(seed_from, "push", "-q", str(path), "v1.0.0^{commit}:refs/heads/main", "v1.0.0")
    return path


def refs(repo_or_bare: Path) -> dict[str, str]:
    out = git(repo_or_bare, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ") for line in out.splitlines())


def fetch_address(repo: Path) -> Path:
    return Path(git(repo, "remote", "get-url", "origin"))


# ── several push URLs: one explicit choice, never the first silently ─────────


def test_two_push_urls_are_refused_before_the_stamp_without_a_choice(engine, proj, tmp_path):
    repo, _ = proj
    one, two = bare(tmp_path, "push1.git", seed_from=repo), bare(tmp_path, "push2.git",
                                                                 seed_from=repo)
    git(repo, "config", "--add", "remote.origin.pushurl", str(one))
    git(repo, "config", "--add", "remote.origin.pushurl", str(two))
    a = proven(engine, repo, "feature a")
    before = refs(one), refs(two), refs(fetch_address(repo))
    with pytest.raises(ReleaseRefused, match="--push-url") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["push_urls"] == [str(one), str(two)]
    assert get_release(engine, SLUG, "1.1.0") is None  # refused before the stamp
    assert (refs(one), refs(two), refs(fetch_address(repo))) == before


def test_the_chosen_push_url_is_the_only_recipient_and_the_one_verified(engine, proj, tmp_path):
    repo, _ = proj
    one, two = bare(tmp_path, "push1.git", seed_from=repo), bare(tmp_path, "push2.git",
                                                                 seed_from=repo)
    git(repo, "config", "--add", "remote.origin.pushurl", str(one))
    git(repo, "config", "--add", "remote.origin.pushurl", str(two))
    a = proven(engine, repo, "feature a")
    before_one, before_fetch = refs(one), refs(fetch_address(repo))
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], push_url=str(two))
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    commit = record["release_commit_sha"]
    target = {"remote": "origin", "url": str(two), "branch": "main", "tag": "v1.1.0"}
    assert record["consent"]["push_target"] == target
    assert record["push_proof"]["target"] == target
    assert record["push_proof"]["tag_commit"] == commit  # read back from the CHOSEN url
    assert refs(two)["refs/heads/main"] == commit and refs(two)["refs/tags/v1.1.0"] == commit
    assert refs(one) == before_one  # the other push URL received nothing
    assert refs(fetch_address(repo)) == before_fetch


def test_a_push_url_that_is_not_one_of_the_remotes_is_refused(engine, proj, tmp_path):
    repo, _ = proj
    stranger = bare(tmp_path, "stranger.git")
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="not a push URL") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], push_url=str(stranger))
    assert exc.value.details["push_urls"] == [str(fetch_address(repo))]
    assert get_release(engine, SLUG, "1.1.0") is None and refs(stranger) == {}


# ── fetch URL ≠ push URL (https to read, ssh to write) stays valid ───────────


def test_a_separate_push_url_is_pushed_to_and_verified_not_the_fetch_url(engine, proj,
                                                                         tmp_path):
    repo, _ = proj
    write_side = bare(tmp_path, "write-side.git", seed_from=repo)
    git(repo, "remote", "set-url", "--push", "origin", str(write_side))
    a = proven(engine, repo, "feature a")
    before_fetch = refs(fetch_address(repo))
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    commit = record["release_commit_sha"]
    assert record["consent"]["push_target"]["url"] == str(write_side)
    assert refs(write_side)["refs/heads/main"] == commit
    assert refs(write_side)["refs/tags/v1.1.0"] == commit
    assert refs(fetch_address(repo)) == before_fetch  # the read address is never written


def test_the_pre_stamp_control_read_asks_the_push_url(engine, proj, tmp_path):
    """A tag of V already on the PUSH address (and not on the fetch address)
    must refuse before the stamp — the control read is of where the push
    goes."""
    repo, _ = proj
    write_side = bare(tmp_path, "write-side.git", seed_from=repo)
    git(repo, "remote", "set-url", "--push", "origin", str(write_side))
    git(repo, "push", "-q", str(write_side), "v1.0.0^{commit}:refs/tags/v1.1.0")
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="already has a tag") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["push_target"]["url"] == str(write_side)
    assert get_release(engine, SLUG, "1.1.0") is None


def test_prepare_shows_the_same_target_apply_consents_to(engine, proj, tmp_path):
    repo, _ = proj
    write_side = bare(tmp_path, "write-side.git", seed_from=repo)
    git(repo, "remote", "set-url", "--push", "origin", str(write_side))
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepared = effects.prepare_release(engine, SLUG, version="1.1.0", entry_ids=[a],
                                       plan_digest=digest, cwd=str(repo))
    shown = prepared["details"]["push_target"]
    assert shown["url"] == str(write_side)
    effects.apply_release(engine, SLUG, version="1.1.0", entry_ids=[a], confirm=True,
                          plan_digest=digest,
                          proposal_digest=prepared["details"]["proposal_sha256"], cwd=str(repo))
    assert get_release(engine, SLUG, "1.1.0")["consent"]["push_target"] == shown


# ── a config change after the consent cannot add or swap a recipient ─────────


def test_resume_after_a_push_url_was_added_pushes_only_to_the_approved_one(engine, proj,
                                                                           tmp_path):
    repo, _ = proj
    approved = fetch_address(repo)
    added = bare(tmp_path, "added.git", seed_from=repo)
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    # `git push origin` would now send to BOTH addresses.
    git(repo, "config", "--add", "remote.origin.pushurl", str(approved))
    git(repo, "config", "--add", "remote.origin.pushurl", str(added))
    before_added = refs(added)
    assert resume(engine, repo)["outcome"] == "completed"
    assert refs(added) == before_added  # no new recipient
    git(repo, "config", "--unset-all", "remote.origin.pushurl")
    assert_released(engine, repo, candidate, [a])


def test_resume_refuses_when_the_remote_no_longer_pushes_to_the_approved_url(engine, proj,
                                                                             tmp_path):
    repo, _ = proj
    approved = fetch_address(repo)
    swapped = bare(tmp_path, "swapped.git", seed_from=repo)
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    git(repo, "remote", "set-url", "--push", "origin", str(swapped))
    before = refs(swapped), refs(approved)
    with pytest.raises(ReleaseRefused, match="consent") as exc:
        resume(engine, repo)
    assert exc.value.details["push_urls"] == [str(swapped)]
    assert "url_now" not in exc.value.details  # R8c S11: `push_urls` is the whole answer
    assert (refs(swapped), refs(approved)) == before
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"


def test_a_rewrite_rule_matching_the_approved_url_is_refused(engine, proj, tmp_path):
    """`url.<other>.insteadOf <approved>` added after the consent would send
    the literal approved URL elsewhere — refused, nothing pushed."""
    repo, _ = proj
    approved = fetch_address(repo)
    elsewhere = bare(tmp_path, "elsewhere.git", seed_from=repo)
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    git(repo, "config", f"url.{elsewhere}.insteadOf", str(approved))
    before = refs(elsewhere), refs(approved)
    with pytest.raises(ReleaseRefused):
        resume(engine, repo)
    assert (refs(elsewhere), refs(approved)) == before
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"


def test_a_push_url_that_git_would_rewrite_again_is_refused_before_the_stamp(engine, proj,
                                                                             tmp_path):
    """The effective push URL must be addressable LITERALLY: when a second
    rewrite rule matches the URL `get-url` reports, the command line URL
    would go to yet another repository."""
    repo, _ = proj
    approved = fetch_address(repo)
    second = bare(tmp_path, "second.git", seed_from=repo)
    third = bare(tmp_path, "third.git", seed_from=repo)
    git(repo, "config", f"url.{second}.insteadOf", str(approved))
    git(repo, "config", f"url.{third}.insteadOf", str(second))
    a = proven(engine, repo, "feature a")
    before = refs(second), refs(third), refs(approved)
    with pytest.raises(ReleaseRefused, match="insteadOf") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["url"] == str(second)
    assert get_release(engine, SLUG, "1.1.0") is None
    assert (refs(second), refs(third), refs(approved)) == before


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_apply_takes_the_push_url_choice(engine, proj, tmp_path, monkeypatch, capsys):
    repo, _ = use_cli(proj, engine, monkeypatch)
    one, two = bare(tmp_path, "push1.git", seed_from=repo), bare(tmp_path, "push2.git",
                                                                 seed_from=repo)
    git(repo, "config", "--add", "remote.origin.pushurl", str(one))
    git(repo, "config", "--add", "remote.origin.pushurl", str(two))
    a = proven(engine, repo, "feature a")
    before_two = refs(two)
    run_cli(monkeypatch, ["release", "apply", "-P", SLUG, "--version", "1.1.0", "--entries",
                          str(a), "--confirm", *cli_digest(engine, repo, "1.1.0", [a]),
                          "--push-url", str(one)])
    assert str(one) in capsys.readouterr().out
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert refs(one)["refs/heads/main"] == commit and refs(two) == before_two
