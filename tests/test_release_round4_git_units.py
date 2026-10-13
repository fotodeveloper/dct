"""Round 4 — the building blocks of the git effects (K-103, K-117, K-118 L8),
each on its own contract: the supervised runner's `stdin`, the
compare-and-swap ref transaction, the lock location of a registered path,
the proposal's stable identity and the list of live proposals.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core import gitinfo, procs
from dct.core import releases as ledger
from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]

ZERO = "0" * 64


@pytest.fixture
def repo(tmp_path, git_identity):
    return Path(make_git_repo(tmp_path / "repo"))


def child_commit(repo: Path, message: str = "next") -> str:
    """An unpublished commit on HEAD (same tree)."""
    return git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", message)


def locks(repo: Path) -> list[str]:
    return sorted(str(p.relative_to(repo)) for p in (repo / ".git").rglob("*.lock"))


# ── procs: stdin is a descriptor of DATA, closed by default ──────────────────


def test_run_feeds_a_data_file_as_stdin_and_defaults_to_a_closed_stdin(tmp_path):
    data = tmp_path / "in.txt"
    data.write_bytes(b"line one  \n\n\n\nline two\n")
    with open(data, "rb") as fh:
        done = procs.run(["cat"], timeout=30, stdin=fh.fileno(), stdout=procs.complete(1 << 16))
    assert done.returncode == 0 and done.stdout.data == data.read_bytes()
    closed = procs.run(["cat"], timeout=30, stdout=procs.complete(1 << 16))
    assert closed.returncode == 0 and closed.stdout.data == b""  # EOF at once: never a prompt


def test_stream_lines_answers_what_the_caller_writes_to_its_pipe():
    reader, writer = os.pipe()
    lines = procs.stream_lines(["cat"], timeout=30, stdin=reader)
    try:
        os.write(writer, b"first\n")
        assert next(lines) == b"first"
        os.write(writer, b"second\n")  # written only after the first answer was read
        assert next(lines) == b"second"
        os.close(writer)
        writer = None
        assert list(lines) == []  # EOF → exit 0 → the generator ends normally
    finally:
        lines.close()
        os.close(reader)
        if writer is not None:
            os.close(writer)


# ── gitinfo.git_update_ref_cas ───────────────────────────────────────────────


def test_cas_moves_the_ref_only_from_the_expected_value(repo):
    old = git(repo, "rev-parse", "HEAD")
    new = child_commit(repo)
    gitinfo.git_update_ref_cas(repo, "refs/heads/main", new, old, message="commit: next")
    assert git(repo, "rev-parse", "refs/heads/main") == new
    assert git(repo, "reflog", "-1", "--format=%gs", "refs/heads/main") == "commit: next"
    assert locks(repo) == []
    # The same request again: the ref is not at `old` any more — a lost race.
    with pytest.raises(gitinfo.GitRefRaceError, match="refs/heads/main"):
        gitinfo.git_update_ref_cas(repo, "refs/heads/main", child_commit(repo), old,
                                   message="commit: again")
    assert git(repo, "rev-parse", "refs/heads/main") == new and locks(repo) == []


def test_cas_verify_runs_with_the_ref_locked_and_can_abort(repo):
    old = git(repo, "rev-parse", "HEAD")
    new = child_commit(repo)
    seen = {}

    def refuse() -> str:
        seen["locks"] = locks(repo)
        seen["value"] = git(repo, "rev-parse", "refs/heads/main")
        return "the caller changed its mind"

    with pytest.raises(gitinfo.GitRefVerifyError) as exc:
        gitinfo.git_update_ref_cas(repo, "refs/heads/main", new, old, message="x", verify=refuse)
    assert exc.value.reason == "the caller changed its mind"
    assert ".git/HEAD.lock" in seen["locks"] and ".git/refs/heads/main.lock" in seen["locks"]
    assert seen["value"] == old  # prepared, not yet written
    assert git(repo, "rev-parse", "refs/heads/main") == old and locks(repo) == []


def test_cas_verify_raising_abandons_the_transaction_and_its_locks(repo):
    old = git(repo, "rev-parse", "HEAD")

    def boom() -> None:
        raise RuntimeError("interrupted while prepared")

    with pytest.raises(RuntimeError, match="interrupted while prepared"):
        gitinfo.git_update_ref_cas(repo, "refs/heads/main", child_commit(repo), old,
                                   message="x", verify=boom)
    assert git(repo, "rev-parse", "refs/heads/main") == old and locks(repo) == []


def test_cas_on_a_detached_head_moves_head_itself_not_a_branch(repo):
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    new = child_commit(repo)
    gitinfo.git_update_ref_cas(repo, "HEAD", new, old, message="commit: d", no_deref=True)
    assert git(repo, "rev-parse", "HEAD") == new
    assert git(repo, "rev-parse", "refs/heads/main") == old


@pytest.mark.parametrize("bad", ["refs/heads/a b", "refs/heads/a\nb", '"refs/heads/x', ""])
def test_cas_refuses_an_argument_that_would_break_the_command_line(repo, bad):
    old = git(repo, "rev-parse", "HEAD")
    with pytest.raises(gitinfo.GitWriteError, match="unsafe"):
        gitinfo.git_update_ref_cas(repo, bad, old, old, message="x")


# ── gitinfo.release_lock_location (K-118 L8) ─────────────────────────────────


def test_lock_location_is_the_common_dir_for_every_path_of_the_repository(repo, tmp_path):
    common = os.path.realpath(repo / ".git")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(wt))
    (repo / "packages" / "app").mkdir(parents=True)
    assert gitinfo.release_lock_location(repo) == common
    assert gitinfo.release_lock_location(wt) == common
    assert gitinfo.release_lock_location(repo / "packages" / "app") == common


def test_lock_location_of_a_path_outside_git_is_none_and_unverifiable_raises(tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitinfo.release_lock_location(plain) is None
    with pytest.raises(gitinfo.CheckoutVerificationError):
        gitinfo.release_lock_location(tmp_path / "missing")
    (repo / ".git" / "HEAD").write_text("garbage\n")
    with pytest.raises(gitinfo.CheckoutVerificationError):
        gitinfo.release_lock_location(repo)


# ── core.releases: the proposal's identity and the live proposals ────────────


def proposal(**extra) -> dict:
    return {"paths": ["VERSION"], "base_blobs": {"VERSION": "a" * 40},
            "expected_blobs": {"VERSION": "b" * 40}, **extra}


def test_a_proposal_id_is_optional_and_part_of_the_digest_only_when_present():
    without = ledger.validate_proposal(proposal())
    assert "id" not in without  # a proposal stored before the field keeps its digest
    one = ledger.validate_proposal(proposal(id="01HZZZZZZZZZZZZZZZZZZZZZZZ"))
    two = ledger.validate_proposal(proposal(id="01HYYYYYYYYYYYYYYYYYYYYYYY"))
    assert one["id"] == "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    digests = {ledger.proposal_digest(p) for p in (without, one, two)}
    assert len(digests) == 3  # the same content under two identities: two proposals


@pytest.mark.parametrize("bad", ["", "with space", "a/b", "x" * 65, 7, "..", "ref~1"])
def test_a_proposal_id_must_be_safe_in_a_ref_name(bad):
    with pytest.raises(ValueError, match=r"proposal\.id"):
        ledger.validate_proposal(proposal(id=bad))


def test_live_proposals_are_the_pending_ones_and_those_of_unfinished_records(engine):
    for slug in ("proj-a", "proj-b", "proj-c"):
        create_project(engine, slug=slug, name=slug, current_version="1.0.0")
    assert ledger.live_proposals(engine) == []
    ledger.set_pending_proposal(engine, "proj-a", proposal(id="01A"))
    consumed = ledger.set_pending_proposal(engine, "proj-b", proposal(id="01B"))
    eid = add_entry(engine, "proj-b", "added", "x")["id"]
    release_entries(engine, "proj-b", "1.1.0", consume_proposal=True,
                    consent={"version": "1.1.0", "entry_ids": [eid],
                             "proposal_sha256": consumed["proposal_sha256"]})
    live = {(item["project"], item["version"], item["proposal"]["id"])
            for item in ledger.live_proposals(engine)}
    assert live == {("proj-a", None, "01A"), ("proj-b", "1.1.0", "01B")}
    ledger.abandon_release(engine, "proj-b", "1.1.0", "stop", confirm=True, actor="dct")
    assert {item["proposal"]["id"] for item in ledger.live_proposals(engine)} == {"01A"}


# ── effects.pin_ref: the project and the proposal in a valid ref name ────────


@pytest.mark.parametrize("slug", ["proj-a", "My Project", "a/b", "x..y", "ünï", "name.lock", "-x"])
def test_the_pin_ref_is_a_valid_ref_for_any_project_slug(repo, slug):
    from dct.workflows.release import effects

    ref = effects.pin_ref(slug, {"id": "01HZZZZZZZZZZZZZZZZZZZZZZZ"})
    assert ref.startswith("refs/dct/release-proposals/")
    assert ref.endswith("/01HZZZZZZZZZZZZZZZZZZZZZZZ")
    git(repo, "check-ref-format", ref)  # raises when git rejects the name
    other = effects.pin_ref(slug + "x", {"id": "01HZZZZZZZZZZZZZZZZZZZZZZZ"})
    assert other != ref  # another project → another ref
    assert effects.pin_ref(slug, {}) is None  # a proposal without an identity has no pin
