"""Round 4 — the git effects of the release (K-103, K-107, K-118 L1–L3).

The release commit and the attestation commit are created by an EXPLICIT
hook lifecycle (`git hook run` pre-commit → prepare-commit-msg → commit-msg
→ [verify → commit-tree on the PINNED parent → compare-and-swap of the ref
HEAD named at the pre-check] → post-commit). What the tests prove, on real
repositories with real executable hooks:

- hooks get what `git commit` gives them (order, arguments, the temporary
  index with the approved tree, GIT_EDITOR, the author identity, the work
  tree as cwd, core.hooksPath, the signing policy);
- a hook that changes the approved content or message stops the release
  BEFORE anything is published — no foreign commit is left on the branch
  (K-118 L1–L3), nothing is reset for the owner;
- the publication is a real compare-and-swap of the pinned ref: a HEAD that
  moved, a branch switch, an attached / detached HEAD change between the
  last check and the publication lose NOTHING of the other process — not
  its commits, not its staging (barriers: on_point, no sleeps);
- post-commit runs once, after the publication; its failure is a warning;
- attest proves EXACTLY the approved ids (K-107), checked before
  publication.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import git
from test_release_effects import (
    SLUG,
    Interrupted,
    apply,
    assert_released,
    interrupt_at,
    make_release_repo,
    proven,
    remote_refs,
    resume,
)
from test_release_git_effects import (
    foreign_blob,
    hooks_dir,
    install_hook,
    local_tag,
    stage_blob,
    staged_blob,
)

from dct.core.changelog import add_entry
from dct.core.releases import get_release
from dct.workflows.release import effects
from dct.workflows.release.effects import ReleaseRefused, attest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Every test here creates commits through the effects layer."""


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def all_subjects(repo: Path) -> list[str]:
    """Subjects of every commit reachable from any ref."""
    return git(repo, "log", "--all", "--format=%s").splitlines()


def foreign_commit(repo: Path, name: str = "g.txt") -> str:
    """Another process commits a new file on the current HEAD."""
    (repo / name).write_text("someone else's work\n")
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", "someone else")
    return git(repo, "rev-parse", "HEAD")


def at(point: str, action):
    """on_point: run `action` once, at `point`; `.fired` tells the test the
    barrier was really reached."""
    def hook(name: str) -> None:
        if name == point and not hook.fired:
            hook.fired = True
            action()
    hook.fired = False
    return hook


def unreleased(engine, text: str = "squashed work") -> int:
    return add_entry(engine, SLUG, "added", text)["id"]


# ── K-103: hooks get what `git commit` gives them ────────────────────────────


def test_hooks_get_the_arguments_index_and_environment_of_git_commit(engine, proj, tmp_path):
    repo, _ = proj
    trace = tmp_path / "hooks.log"
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
        install_hook(hooks_dir(repo), name, (
            f'{{ printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "{name}" "$*" "$GIT_INDEX_FILE" '
            f'"$GIT_EDITOR" "$GIT_AUTHOR_NAME <$GIT_AUTHOR_EMAIL>" "$GIT_AUTHOR_DATE" '
            f'"$(git write-tree)" "$(pwd -P)"; }} >> "{trace}"\n'
            f'if [ "{name}" = commit-msg ]; then cp "$1" "{tmp_path}/message.seen"; fi\n'
            f'if [ "{name}" = post-commit ]; then git rev-parse HEAD > "{tmp_path}/head.seen"; '
            f'fi\n'))
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    record = assert_released(engine, repo, candidate, [a])
    rows = [line.split("\t") for line in trace.read_text().splitlines()]
    assert [row[0] for row in rows] == [
        "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"]
    by_name = {row[0]: row for row in rows}
    # arguments: the message file and its source, exactly as `git commit -m`
    assert by_name["pre-commit"][1] == ""
    assert by_name["prepare-commit-msg"][1] == ".git/COMMIT_EDITMSG message"
    assert by_name["commit-msg"][1] == ".git/COMMIT_EDITMSG"
    assert by_name["post-commit"][1] == ""
    real_index = os.path.realpath(repo / ".git" / "index")
    release_commit = record["release_commit_sha"]
    author_date = git(repo, "log", "-1", "--format=%at", release_commit)
    for row in rows:
        _, _, index, editor, author, date, tree, cwd = row
        # the temporary index holds exactly the approved tree; never the real one
        assert index and os.path.realpath(index) != real_index
        assert tree == record["manifest"]["tree"]
        assert editor == ":" and author == "t <t@t>"
        assert date.startswith(f"@{author_date} ")  # the commit carries the date hooks saw
        assert cwd == os.path.realpath(repo)  # the same work tree
    assert (tmp_path / "message.seen").read_text() == "release: v1.1.0\n"
    assert (tmp_path / "head.seen").read_text().strip() == release_commit
    assert not out["warnings"]


def test_core_hooks_path_is_honoured(engine, proj, tmp_path):
    repo, _ = proj
    trace = tmp_path / "hooks.log"
    custom = tmp_path / "team-hooks"
    install_hook(custom, "pre-commit", f'echo custom >> "{trace}"\n')
    install_hook(hooks_dir(repo), "pre-commit", f'echo default >> "{trace}"\n')
    git(repo, "config", "core.hooksPath", str(custom))
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert_released(engine, repo, candidate, [a])
    assert trace.read_text().split() == ["custom"]


def fake_signer(tmp_path: Path, *, ok: bool = True) -> tuple[Path, Path]:
    log = tmp_path / "signer.log"
    body = (f'echo "$*" >> "{log}"\ncat > /dev/null\n'
            'echo "[GNUPG:] SIG_CREATED D 1 8 00 1 0" >&2\n'
            'printf -- "-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n"\n'
            if ok else 'echo "no secret key" >&2\nexit 2\n')
    return install_hook(tmp_path / "bin", "fake-gpg", body), log


def test_the_signing_policy_in_force_signs_the_release_commit(engine, proj, tmp_path):
    repo, _ = proj
    signer, log = fake_signer(tmp_path)
    git(repo, "config", "gpg.program", str(signer))
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "user.signingKey", "KEY123")
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    record = assert_released(engine, repo, candidate, [a])
    raw = git(repo, "cat-file", "commit", record["release_commit_sha"])
    assert "\ngpgsig " in raw and "KEY123" in log.read_text()


def test_without_a_signing_policy_the_release_commit_is_not_signed(engine, proj, tmp_path):
    repo, _ = proj
    signer, log = fake_signer(tmp_path)
    git(repo, "config", "gpg.program", str(signer))
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    raw = git(repo, "cat-file", "commit", get_release(engine, SLUG, "1.1.0")["release_commit_sha"])
    assert "gpgsig" not in raw and not log.exists()


def test_a_failing_signer_publishes_nothing(engine, proj, tmp_path):
    repo, _ = proj
    signer, _ = fake_signer(tmp_path, ok=False)
    git(repo, "config", "gpg.program", str(signer))
    git(repo, "config", "commit.gpgSign", "true")
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ReleaseRefused, match="sign"):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert "release: v1.1.0" not in all_subjects(repo)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"


def test_a_git_too_old_for_the_hook_lifecycle_is_refused_before_any_effect(
        engine, proj, monkeypatch):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"),
              remote_refs(repo))
    monkeypatch.setattr(effects, "MIN_GIT_VERSION", (99, 0), raising=False)
    with pytest.raises(ReleaseRefused, match="git 99.0"):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert get_release(engine, SLUG, "1.1.0") is None  # refused before the stamp
    assert (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"),
            remote_refs(repo)) == before
    b = unreleased(engine)
    with pytest.raises(ReleaseRefused, match="git 99.0"):
        attest(engine, SLUG, [b], confirm=True, cwd=str(repo))
    assert git(repo, "rev-parse", "HEAD") == before[0]


# ── K-103 / K-118 L1–L3: a hook that changes what was approved ───────────────


def test_a_formatter_hook_is_refused_before_any_commit_is_published(engine, proj):
    repo, _ = proj
    hook = install_hook(hooks_dir(repo), "pre-commit",
                        'printf "# formatted\\n" >> pyproject.toml\ngit add pyproject.toml\n')
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    before_remote = remote_refs(repo)
    index = git(repo, "ls-files", "-s")
    with pytest.raises(ReleaseRefused, match="hook") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    record = get_release(engine, SLUG, "1.1.0")
    # NOTHING was published: the branch still ends at the candidate, no
    # commit titled like the release exists on any ref, the index is the user's.
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert git(repo, "rev-parse", "refs/heads/main") == candidate
    assert "release: v1.1.0" not in all_subjects(repo)
    assert git(repo, "ls-files", "-s") == index
    assert record["status"] == "files_written" and record["release_commit_sha"] is None
    assert local_tag(repo, "v1.1.0") == "" and remote_refs(repo) == before_remote
    assert exc.value.details["head"] == candidate
    assert exc.value.details["expected_tree"] == record["manifest"]["tree"]
    # nothing is restored for the owner either: the formatter's edit stays
    assert (repo / "pyproject.toml").read_text().endswith("# formatted\n")
    # The owner removes the formatter and restores the approved file; resume
    # then finishes on the SAME candidate — no reset of any branch needed.
    hook.unlink()
    text = (repo / "pyproject.toml").read_text()
    (repo / "pyproject.toml").write_text(text.removesuffix("# formatted\n"))
    assert resume(engine, repo)["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])


def test_a_hook_that_rewrites_the_subject_publishes_nothing(engine, proj):
    repo, _ = proj
    hook = install_hook(hooks_dir(repo), "commit-msg", 'echo "chore: renamed by a hook" > "$1"\n')
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ReleaseRefused, match="message") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert "chore: renamed by a hook" not in all_subjects(repo)
    assert exc.value.details["subject"] == "chore: renamed by a hook"
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    hook.unlink()
    assert resume(engine, repo)["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])


def test_a_hook_that_only_appends_a_trailer_is_within_the_message_contract(engine, proj):
    repo, _ = proj
    install_hook(hooks_dir(repo), "commit-msg",
                 'printf "\\nSigned-off-by: T <t@t>\\n" >> "$1"\n')
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    record = assert_released(engine, repo, candidate, [a])
    body = git(repo, "log", "-1", "--format=%B", record["release_commit_sha"])
    assert body.splitlines() == ["release: v1.1.0", "", "Signed-off-by: T <t@t>"]


def test_a_failing_post_commit_is_a_warning_and_never_a_second_commit(engine, proj, tmp_path):
    repo, _ = proj
    trace = tmp_path / "post.log"
    install_hook(hooks_dir(repo), "post-commit",
                 f'git rev-parse HEAD >> "{trace}"\necho "notifier down" >&2\nexit 7\n')
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at("after_commit"))
    head = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == candidate  # published despite the hook's failure
    assert trace.read_text().split() == [head]  # … and the hook saw the published commit
    out = resume(engine, repo)
    assert out["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])  # exactly ONE release commit
    assert trace.read_text().split() == [head]  # resume neither commits nor re-runs it


def test_a_failing_post_commit_is_reported_as_a_warning(engine, proj):
    repo, _ = proj
    install_hook(hooks_dir(repo), "post-commit", 'echo "notifier down" >&2\nexit 7\n')
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert_released(engine, repo, candidate, [a])
    assert any("post-commit" in w and "notifier down" in w for w in out["warnings"])


# ── K-103: the publication is a compare-and-swap of the pinned ref ───────────


@pytest.mark.parametrize("point", ["before_commit", "before_publish"])
def test_a_commit_made_meanwhile_is_never_dropped_from_the_branch(engine, proj, tmp_path,
                                                                  point):
    """HEAD moves between the last check and the publication (window: before
    the hooks / after them, right before the ref update): the other
    process's commit stays the branch tip with its file, its staging stays,
    and no release commit is published anywhere."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    mine = foreign_blob(repo, tmp_path, "staged by someone else\n")
    made = {}

    def other_process() -> None:
        made["commit"] = foreign_commit(repo)
        stage_blob(repo, "f.txt", mine)

    barrier = at(point, other_process)
    with pytest.raises(ReleaseRefused) as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=barrier)
    assert barrier.fired
    assert git(repo, "rev-parse", "HEAD") == made["commit"]
    assert git(repo, "rev-parse", "refs/heads/main") == made["commit"]
    assert git(repo, "rev-parse", "HEAD^") == candidate
    assert "g.txt" in git(repo, "ls-tree", "--name-only", "HEAD").split()
    assert staged_blob(repo, "f.txt") == mine  # the other process's staging survives
    assert "release: v1.1.0" not in all_subjects(repo)
    assert exc.value.details["head"] == made["commit"]
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    assert local_tag(repo, "v1.1.0") == ""


def test_a_branch_switch_before_the_publication_publishes_nowhere(engine, proj):
    """HEAD is re-pointed to ANOTHER branch with the same tip after the last
    check: the SHA did not change, the symbolic HEAD did. Nothing is
    published — neither on the pinned branch nor on the new one."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    barrier = at("before_publish", lambda: git(repo, "switch", "-q", "-c", "other"))
    with pytest.raises(ReleaseRefused, match="HEAD") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=barrier)
    assert barrier.fired
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/other"
    assert git(repo, "rev-parse", "refs/heads/main") == candidate
    assert git(repo, "rev-parse", "refs/heads/other") == candidate
    assert "release: v1.1.0" not in all_subjects(repo)
    assert exc.value.details["pinned_ref"] == "refs/heads/main"
    assert exc.value.details["head_ref"] == "refs/heads/other"
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    # Back on the pinned branch the release finishes on its candidate.
    git(repo, "switch", "-q", "main")
    assert resume(engine, repo)["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])


def test_head_cannot_be_repointed_while_the_ref_transaction_is_prepared(engine, proj):
    """Inside the prepared ref transaction (the pinned ref — and HEAD naming
    it — locked by git) a `git switch` of another process FAILS; the release
    commit is then published on the pinned branch HEAD still names."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    seen = {}

    def try_switch() -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        done = subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "other"],
                              env=env, capture_output=True, text=True, check=False)
        seen["code"], seen["head"] = done.returncode, git(repo, "rev-parse", "HEAD")

    barrier = at("publishing", try_switch)
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=barrier)
    assert barrier.fired and out["outcome"] == "completed"
    assert seen["code"] != 0 and seen["head"] == candidate  # locked, not yet moved
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert_released(engine, repo, candidate, [a])


def test_a_detached_head_release_moves_head_only(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True)
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    assert git(repo, "rev-parse", "HEAD") == record["release_commit_sha"]
    assert git(repo, "rev-parse", "HEAD^") == candidate
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    assert subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], env=env,
                          capture_output=True, check=False).returncode == 1  # still detached
    assert git(repo, "rev-parse", "refs/heads/main") == candidate  # no branch moved


def test_a_detached_head_attached_meanwhile_publishes_nothing(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    barrier = at("before_publish", lambda: git(repo, "switch", "-q", "main"))
    with pytest.raises(ReleaseRefused, match="HEAD") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True, on_point=barrier)
    assert barrier.fired
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(repo, "rev-parse", "refs/heads/main") == candidate
    assert "release: v1.1.0" not in all_subjects(repo)
    assert exc.value.details["pinned_ref"] is None
    assert exc.value.details["head_ref"] == "refs/heads/main"


# ── K-118 L1–L3: a partial effect found from an older run ────────────────────


def test_a_foreign_commit_found_on_the_candidate_is_reported_never_reset(engine, proj,
                                                                         tmp_path):
    """An older dct (its `git commit` let a hook change the content) left a
    commit titled like the release on the candidate. resume reports exactly
    what it found — commit, parents, tree against the approved one — touches
    no ref and no staging, and its repair hint is conditional."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_files_written"))
    record = get_release(engine, SLUG, "1.1.0")
    git(repo, "commit", "-q", "--allow-empty", "-m", "release: v1.1.0")  # not the approved tree
    leftover = git(repo, "rev-parse", "HEAD")
    mine = foreign_blob(repo, tmp_path, "staged by the owner\n")
    stage_blob(repo, "f.txt", mine)
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    with pytest.raises(ReleaseRefused, match="never resets") as exc:
        resume(engine, repo)
    details = exc.value.details
    assert details["commit"] == leftover and details["parents"] == [candidate]
    assert details["tree"] == git(repo, "rev-parse", f"{leftover}^{{tree}}")
    assert details["expected_tree"] == record["manifest"]["tree"]
    assert details["candidate"] == candidate and details["subject"] == "release: v1.1.0"
    assert "if " in str(exc.value).lower()  # the repair hint is conditional
    assert git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert staged_blob(repo, "f.txt") == mine
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"


# ── K-107: attest proves EXACTLY the approved ids ────────────────────────────


@pytest.mark.parametrize("hook, why", [
    ('printf "Changelog: 999\\n" >> "$1"\n', "adds an id to the trailer block"),
    ('printf "%s\\n\\nChangelog: 999\\n\\n%s\\n" "$(sed -n 1p "$1")" "$(sed -n 3p "$1")" > "$1"\n',
     "adds a second Changelog line in the body"),
])
def test_a_hook_that_widens_the_attested_ids_publishes_nothing(engine, proj, hook, why):
    repo, _ = proj
    a = unreleased(engine)
    head = git(repo, "rev-parse", "HEAD")
    install_hook(hooks_dir(repo), "commit-msg", hook)
    with pytest.raises(ReleaseRefused, match="exactly") as exc:
        attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    assert git(repo, "rev-parse", "HEAD") == head, why
    assert effects.ATTEST_SUBJECT not in all_subjects(repo), why
    assert exc.value.details["approved_ids"] == [a]
    assert exc.value.details["proven_ids"] == sorted([a, 999])
    # … so the next plan cannot read an unapproved attestation from history
    from dct.workflows.release import build_plan

    plan = build_plan(engine, SLUG, cwd=str(repo))
    assert a in {e["id"] for e in plan["entries"]["unproven"]}


def test_a_hook_that_drops_an_attested_id_publishes_nothing(engine, proj):
    repo, _ = proj
    a, b = unreleased(engine, "one"), unreleased(engine, "two")
    head = git(repo, "rev-parse", "HEAD")
    install_hook(hooks_dir(repo), "commit-msg",
                 f'printf "{effects.ATTEST_SUBJECT}\\n\\nChangelog: {a}\\n" > "$1"\n')
    with pytest.raises(ReleaseRefused, match="exactly") as exc:
        attest(engine, SLUG, [a, b], confirm=True, cwd=str(repo))
    assert git(repo, "rev-parse", "HEAD") == head
    assert effects.ATTEST_SUBJECT not in all_subjects(repo)
    assert exc.value.details["approved_ids"] == [a, b]
    assert exc.value.details["proven_ids"] == [a]


def test_an_addition_that_keeps_the_attested_set_is_allowed(engine, proj):
    repo, _ = proj
    a = unreleased(engine)
    head = git(repo, "rev-parse", "HEAD")
    install_hook(hooks_dir(repo), "commit-msg", 'printf "Signed-off-by: T <t@t>\\n" >> "$1"\n')
    out = attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    assert out["outcome"] == "attested" and git(repo, "rev-parse", "HEAD^") == head
    assert "Signed-off-by: T <t@t>" in git(repo, "log", "-1", "--format=%B")


def test_an_attestation_hook_that_stages_content_publishes_nothing(engine, proj):
    repo, _ = proj
    a = unreleased(engine)
    head = git(repo, "rev-parse", "HEAD")
    install_hook(hooks_dir(repo), "pre-commit", 'echo x > hooked.txt\ngit add hooked.txt\n')
    with pytest.raises(ReleaseRefused, match="hook"):
        attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    assert git(repo, "rev-parse", "HEAD") == head
    assert effects.ATTEST_SUBJECT not in all_subjects(repo)


def test_an_attestation_never_drops_a_commit_made_meanwhile(engine, proj):
    repo, _ = proj
    a = unreleased(engine)
    head = git(repo, "rev-parse", "HEAD")
    made = {}
    barrier = at("before_publish", lambda: made.update(commit=foreign_commit(repo)))
    with pytest.raises(ReleaseRefused) as exc:
        attest(engine, SLUG, [a], confirm=True, cwd=str(repo), on_point=barrier)
    assert barrier.fired
    assert git(repo, "rev-parse", "HEAD") == made["commit"]
    assert git(repo, "rev-parse", "HEAD^") == head
    assert "g.txt" in git(repo, "ls-tree", "--name-only", "HEAD").split()
    assert effects.ATTEST_SUBJECT not in all_subjects(repo)
    assert exc.value.details["head"] == made["commit"]


@pytest.mark.parametrize("cleanup, expected", [
    (None, ["release: v1.1.0", "", "# kept: -m messages are not comment-stripped", "",
            "Reviewed-by: T <t@t>"]),
    ("strip", ["release: v1.1.0", "", "Reviewed-by: T <t@t>"]),
])
def test_the_message_is_cleaned_up_as_git_commit_cleans_it(engine, proj, cleanup, expected):
    """A hook's edit of the message file goes through git's own cleanup
    (commit.cleanup; default for a message given on the command line:
    whitespace only) — trailing blank lines and runs of blank lines go,
    comment lines only with `strip`."""
    repo, _ = proj
    if cleanup:
        git(repo, "config", "commit.cleanup", cleanup)
    install_hook(hooks_dir(repo), "commit-msg", (
        'printf "\\n\\n\\n# kept: -m messages are not comment-stripped\\n\\n\\n'
        'Reviewed-by: T <t@t>   \\n\\n\\n" >> "$1"\n'))
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    record = assert_released(engine, repo, candidate, [a])
    raw = git(repo, "cat-file", "commit", record["release_commit_sha"])
    assert raw.split("\n\n", 1)[1].splitlines() == expected
