"""The release EFFECTS layer (K-72 layer 3; K-49..K-51, K-31/K-32, K-55,
K-57, K-60, K-61, K-63, K-73, K-76) — the PRODUCTION workflow
(dct.workflows.release.effects) driven on real throwaway git repositories and
files: prepare (isolated version sync) → stamp → changelog file → approved
version files → release commit → tag, with interruptions injected before
every effect and between every effect and its progress write, then `resume`
— which must finish exactly once and never re-runs the sync. The artifacts
contract itself (prepare, approval, content-verified retry) is pinned in
tests/test_release_prepare.py.

Every scenario checks reality, not the record: F's bytes (section + history),
the version file, the whole index, HEAD, the release commit's tree and
parent, the tag target, the DB stamps and the record status / manifest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (
    alias_argv,
    approved_plan_digest,
    approved_proposal_digest,
    approved_push,
    git,
    make_git_repo,
    push_argv,
    run_cli,
)

from dct.core.changelog import add_entry, list_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import get_release, get_version_files, set_version_files
from dct.db import changelog_entries, projects, releases
from dct.workflows.release import effects
from dct.workflows.release.effects import (
    CONSENT_INTERRUPTION_POINTS,
    INTERRUPTION_POINTS,
    RESULT_KEYS,
    ReleaseRefused,
    SyncFailed,
    abandon,
    apply_release,
    attest,
    prepare_release,
    reconstruct,
    resume_release,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

HISTORY = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n\n- first\n"
SLUG = "proj-r"


class Interrupted(Exception):
    """Stands for a crash / kill at an interruption point."""


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Every test here creates commits through the effects layer."""


def write_sync_script(root: Path, log: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "dct-version-sync.sh").write_text(
        "#!/bin/bash\nset -e\n"
        f'echo "$1" >> "{log}"\n'
        'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n')


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def make_release_repo(engine, tmp_path):
    """proj-r: a repo with changelog.md history, pyproject.toml 1.0.0, a sync
    script logging every run to sync.log (outside the repo), tagged v1.0.0;
    the version-file contract is already ["pyproject.toml"]. Its branch
    `main` tracks `origin/main` of a BARE remote (remote.git under tmp_path)
    holding main and v1.0.0 — the default push target (K-97 F2)."""
    repo = Path(make_git_repo(tmp_path / "proj-r"))
    log = tmp_path / "sync.log"
    (repo / "changelog.md").write_text(HISTORY)
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n')
    write_sync_script(repo, log)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "chore: setup")
    git(repo, "tag", "v1.0.0")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main", "v1.0.0")
    create_project(engine, slug=SLUG, name="R", path=str(repo), current_version="1.0.0")
    set_version_files(engine, SLUG, ["pyproject.toml"], confirm=True)
    return repo, log


def commit(repo, message: str, path: str = "f.txt") -> str:
    target = Path(repo) / path
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(message.splitlines()[0] + "\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def proven(engine, repo, text: str, category: str = "added") -> int:
    """An unreleased entry plus a commit that proves it (trailer)."""
    eid = add_entry(engine, SLUG, category, text)["id"]
    commit(repo, f"feat: {text}\n\nChangelog: {eid}\n")
    return eid


def snapshot(repo: Path) -> dict:
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "index": git(repo, "ls-files", "-s"),
        "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
        "tags": git(repo, "for-each-ref", "--format=%(refname:strip=2) %(objectname)",
                    "refs/tags"),
        "changelog": (repo / "changelog.md").read_bytes(),
        "pyproject": (repo / "pyproject.toml").read_bytes(),
    }


def remote_refs(repo) -> dict[str, str]:
    """{ref: object} of the push remote `origin` (git ls-remote)."""
    out = git(repo, "ls-remote", "origin")
    return {ref: sha for sha, ref in (line.split("\t") for line in out.splitlines())}


def sync_runs(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def released_section(date: str, version: str = "1.1.0", lines: str = "- feature a\n") -> str:
    return f"## [{version}] - {date}\n\n### Added\n\n{lines}"


def record_date(engine, version="1.1.0") -> str:
    created = get_release(engine, SLUG, version)["created_at"]
    if created.tzinfo is not None:
        created = created.astimezone(UTC)
    return created.date().isoformat()


def apply(engine, repo, **kw):
    """apply_release with the digest of the plan for the same selection (the
    owner's approval, K-80) and of the proposal `prepare` stores for it
    (K-76) unless the test passes its own — and the push choice the owner
    approves with it (K-123, conftest.approved_push: the upstream target
    prepare shows; what the test passes wins)."""
    kw.setdefault("confirm", True)
    if "plan_digest" not in kw and not kw.get("strict"):
        kw["plan_digest"] = approved_plan_digest(engine, SLUG, repo, **kw)
    if "proposal_digest" not in kw and not kw.get("strict"):
        selection = {k: kw[k] for k in ("version", "bump_type", "entry_ids", "no_entries")
                     if k in kw}
        kw["proposal_digest"] = approved_proposal_digest(
            engine, SLUG, repo, plan_digest=kw["plan_digest"], **selection)
    push = {k: kw.pop(k) for k in ("push_remote", "push_branch", "push_url", "no_push")
            if k in kw}
    return apply_release(engine, SLUG, cwd=str(repo), **kw, **approved_push(repo, **push))


def cli_digest(engine, repo, version: str, entries: list[int] | None, *,
               push: bool = True) -> list[str]:
    """The approval flags of a `dct release apply` argv: both digests and
    (`push`) the approved push choice (K-123) — a push flag the test adds
    AFTER these wins (argparse keeps the last)."""
    kw = {"entry_ids": entries} if entries is not None else {"no_entries": True}
    plan = approved_plan_digest(engine, SLUG, repo, version=version, **kw)
    proposal = approved_proposal_digest(engine, SLUG, repo, plan_digest=plan, version=version,
                                        **kw)
    return ["--plan-digest", plan, *(["--proposal-digest", proposal] if proposal else []),
            *(push_argv(repo) if push else [])]


def resume(engine, repo, **kw):
    return resume_release(engine, SLUG, cwd=str(repo), **kw)


def interrupt_at(point: str):
    def hook(name: str) -> None:
        if name == point:
            raise Interrupted(point)
    return hook


def assert_released(engine, repo, candidate: str, ids: list[int], version="1.1.0",
                    section: str | None = None) -> dict:
    """The complete, verified end state of a release of `version`."""
    record = get_release(engine, SLUG, version)
    assert record["status"] == "completed" and record["entry_ids"] == sorted(ids)
    assert record["candidate_sha"] == candidate
    head = git(repo, "rev-parse", "HEAD")
    # exactly ONE release commit on the candidate, with exactly the artifacts
    assert git(repo, "rev-list", "--count", f"{candidate}..HEAD") == "1"
    assert git(repo, "rev-parse", "HEAD^") == candidate
    assert git(repo, "log", "-1", "--format=%s") == f"release: v{version}"
    assert record["release_commit_sha"] == head
    assert git(repo, "rev-parse", "HEAD^{tree}") == record["manifest"]["tree"]
    assert set(git(repo, "diff", "--name-only", candidate, "HEAD").split()) == {
        "changelog.md", "pyproject.toml"}
    # the tag is lightweight and points at the release commit; one tag only
    assert git(repo, "cat-file", "-t", f"v{version}") == "commit"
    assert git(repo, "rev-parse", f"v{version}") == head
    assert git(repo, "tag", "--list").split() == sorted(["v1.0.0", f"v{version}"])
    # K-97 F2: the release commit and its tag are on the push target (the
    # branch's upstream), verified — and recorded as the consent's target
    remote = remote_refs(repo)
    assert remote["refs/heads/main"] == head and remote[f"refs/tags/v{version}"] == head
    assert record["consent"]["push_target"] == {
        "remote": "origin", "url": git(repo, "remote", "get-url", "--push", "origin"),
        "branch": "main", "tag": f"v{version}"}
    # files and index consistent with HEAD
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    text = (repo / "changelog.md").read_text()
    expected_section = section or released_section(record_date(engine, version))
    assert text == "# Changelog\n\n" + expected_section + "\n" + HISTORY[len("# Changelog\n\n"):]
    assert text.count(f"## [{version}]") == 1
    assert f'version = "{version}"' in (repo / "pyproject.toml").read_text()
    # DB stamps
    stamped = {e["id"]: e["version"] for e in list_entries(engine, SLUG) if e["id"] in ids}
    assert stamped == {i: version for i in ids}
    assert get_project(engine, SLUG)["current_version"] == version
    manifest = record["manifest"]
    assert manifest["changelog"]["path"] == "changelog.md"
    assert manifest["changelog"]["section_date"] == record_date(engine, version)
    assert [f["path"] for f in manifest["version_files"]] == ["pyproject.toml"]
    return record


# ── the happy path ───────────────────────────────────────────────────────────


def test_apply_performs_and_verifies_every_effect(engine, proj):
    repo, log = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed" and tuple(out) == RESULT_KEYS
    assert [s["step"] for s in out["steps"]] == ["stamp", "changelog", "version_files", "commit",
                                                  "index", "tag", "push", "complete"]
    record = assert_released(engine, repo, candidate, [a])
    assert record["proof"] == {str(a): {"commits": [candidate], "merge_commit": None,
                                        "pr": None, "pr_source": None, "source": None,
                                        "ambiguous": False, "associations": []}}
    assert sync_runs(log) == ["1.1.0"]
    json.dumps(out)  # JSON-native


def test_completed_release_resume_and_rerun_have_no_side_effects(engine, proj):
    repo, log = proj
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    before = snapshot(repo)
    assert resume(engine, repo)["outcome"] == "noop"
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "noop"
    assert snapshot(repo) == before and sync_runs(log) == ["1.1.0"]


def test_historical_rerun_after_a_newer_release_has_no_side_effects(engine, proj):
    repo, log = proj
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    b = proven(engine, repo, "feature b")
    apply(engine, repo, version="1.2.0", entry_ids=[b])
    before = snapshot(repo)
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "noop"
    assert out["details"]["current_version"] == "1.2.0"
    assert snapshot(repo) == before and sync_runs(log) == ["1.1.0", "1.2.0"]


def test_version_only_release_end_to_end(engine, proj):
    repo, _ = proj
    candidate = git(repo, "rev-parse", "HEAD")
    out = apply(engine, repo, version="1.1.0", no_entries=True)
    assert out["outcome"] == "completed" and out["details"]["mode"] == "version_only"
    date = record_date(engine)
    assert_released(engine, repo, candidate, [], section=(
        f"## [1.1.0] - {date}\n\n- Version-only release — no changelog entries.\n"))


def test_v_prefixed_request_uses_the_records_spelling_everywhere(engine, proj):
    """K-55: the requested spelling only reaches the report; artifacts use
    release.version."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    out = apply(engine, repo, version="v1.1.0", entry_ids=[a])
    assert out["version"] == "1.1.0" and out["details"]["requested_version"] == "1.1.0"
    assert_released(engine, repo, candidate, [a])


def test_bootstrap_writes_the_full_export_with_the_section_once(engine, proj):
    repo, _ = proj
    git(repo, "rm", "-q", "changelog.md")
    git(repo, "commit", "-q", "-m", "chore: drop changelog")
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    text = (repo / "changelog.md").read_text()
    # K-81: exactly one blank line under the heading, no run of blank lines.
    assert text.startswith("# Changelog\n\n## [")
    assert "\n\n\n" not in text, repr(text[:120])
    assert text.count("## [1.1.0]") == 1 and "- feature a" in text
    assert f"## [1.1.0] - {record_date(engine)}" in text
    assert git(repo, "status", "--porcelain") == ""


# ── interruption + resume (K-57) ─────────────────────────────────────────────


# The points of `apply --consent` (its work / attestation commits before the
# stamp) are never reached on this path — tests/test_release_apply_consent.py.
@pytest.mark.parametrize("point", [p for p in INTERRUPTION_POINTS
                                   if p not in CONSENT_INTERRUPTION_POINTS])
def test_interrupted_release_resumes_exactly_once(engine, proj, point):
    repo, log = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at(point))
    if point == "before_stamp":
        assert get_release(engine, SLUG, "1.1.0") is None
        assert resume(engine, repo)["outcome"] == "noop"
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    else:
        out = resume(engine, repo)
        assert out["outcome"] == "completed", out
    assert_released(engine, repo, candidate, [a])
    # K-76: the sync runs ONLY in prepare (isolated) — once per prepare,
    # never after the stamp, never on a resume (before_stamp: prepared twice).
    assert sync_runs(log) == (["1.1.0", "1.1.0"] if point == "before_stamp" else ["1.1.0"])
    final = snapshot(repo)
    assert resume(engine, repo)["outcome"] == "noop"
    assert snapshot(repo) == final


@pytest.mark.parametrize("point", ["after_changelog", "after_files_written", "after_commit"])
def test_interrupted_version_only_release_resumes(engine, proj, point):
    repo, _ = proj
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", no_entries=True, on_point=interrupt_at(point))
    resume(engine, repo)
    assert_released(engine, repo, candidate, [], section=(
        f"## [1.1.0] - {record_date(engine)}\n\n- Version-only release — no changelog entries.\n"))


def test_resume_section_keeps_the_original_date(engine, proj):
    """K-32/K-61: a resume on a later day still writes the record's date."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("before_changelog"))
    with engine.begin() as conn:
        old = datetime(2025, 3, 4, 12, tzinfo=UTC)
        conn.execute(releases.update().values(created_at=old))
        conn.execute(changelog_entries.update().values(released_at=old))
    resume(engine, repo)
    assert "## [1.1.0] - 2025-03-04\n" in (repo / "changelog.md").read_text()


# ── pre-check refusals (nothing stamped, nothing touched) ────────────────────


def _nothing_released(engine, repo, before, eid):
    assert get_release(engine, SLUG, "1.1.0") is None
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == eid] == [None]
    assert snapshot(repo) == before


def test_unstaged_change_refuses_before_stamping(engine, proj):
    repo, log = proj
    a = proven(engine, repo, "feature a")
    (repo / "f.txt").write_text("dirty\n")
    before = snapshot(repo)
    with pytest.raises(ReleaseRefused, match="not clean"):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    _nothing_released(engine, repo, before, a)
    assert sync_runs(log) == []


def test_foreign_staged_path_refuses_before_stamping(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    (repo / "other.txt").write_text("staged\n")
    git(repo, "add", "other.txt")
    before = snapshot(repo)
    with pytest.raises(ReleaseRefused) as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["staged"] == ["other.txt"]
    _nothing_released(engine, repo, before, a)


def test_unproven_entry_is_never_stamped(engine, proj):
    repo, _ = proj
    a = add_entry(engine, SLUG, "added", "no trailer")["id"]
    commit(repo, "feat: something\n")
    before = snapshot(repo)
    with pytest.raises(ReleaseRefused, match="does not prove") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["unproven"] == [a]
    _nothing_released(engine, repo, before, a)


def test_attestation_then_apply(engine, proj):
    repo, _ = proj
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    commit(repo, "feat: a without trailer\n")
    previous = git(repo, "rev-parse", "HEAD")
    index = git(repo, "ls-files", "-s")
    out = attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    assert out["outcome"] == "attested"
    head = git(repo, "rev-parse", "HEAD")
    assert out["details"] == {"previous_head": previous, "head": head, "entry_ids": [a]}
    assert git(repo, "rev-parse", "HEAD^") == previous
    assert git(repo, "rev-parse", "HEAD^{tree}") == git(repo, "rev-parse", f"{previous}^{{tree}}")
    assert git(repo, "log", "-1", "--format=%B") == f"chore: attest changelog entries\n\nChangelog: {a}"
    assert git(repo, "ls-files", "-s") == index and git(repo, "status", "--porcelain") == ""
    assert get_release(engine, SLUG) is None  # no record yet
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert_released(engine, repo, head, [a])


def test_attest_refuses_staged_changes_and_proven_ids(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="does not list"):
        attest(engine, SLUG, [a], confirm=True, cwd=str(repo))
    b = add_entry(engine, SLUG, "added", "b")["id"]
    (repo / "f.txt").write_text("staged\n")
    git(repo, "add", "f.txt")
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ReleaseRefused, match="staged"):
        attest(engine, SLUG, [b], confirm=True, cwd=str(repo))
    assert git(repo, "rev-parse", "HEAD") == head
    with pytest.raises(ReleaseRefused, match="--confirm"):
        attest(engine, SLUG, [b], cwd=str(repo))


# ── first release: learning the version-file contract (K-73, K-76) ──────────


def test_first_release_learns_the_approved_proposal_as_the_contract(engine, proj):
    repo, log = proj
    with engine.begin() as conn:
        conn.execute(projects.update().values(version_files=None,
                                              version_files_confirmed_at=None))
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepared = prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                               plan_digest=digest)["details"]
    assert prepared["contract"] == {"confirmed": None, "proposed": ["pyproject.toml"],
                                    "change": True}
    assert get_version_files(engine, SLUG)["version_files"] is None  # nothing learned yet
    apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=digest,
          proposal_digest=prepared["proposal_sha256"])
    assert get_version_files(engine, SLUG)["version_files"] == ["pyproject.toml"]
    assert_released(engine, repo, candidate, [a])
    assert sync_runs(log) == ["1.1.0"]  # once, isolated — never after the stamp


def test_untracked_files_present_before_the_release_never_become_version_files(
        engine, proj):
    """Local junk holding V is invisible to the isolated sync and recorded
    as the untracked baseline with the stamp."""
    repo, _ = proj
    with engine.begin() as conn:
        conn.execute(projects.update().values(version_files=None))
    (repo / "notes.local").write_text("scratch 1.1.0\n")
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert get_release(engine, SLUG, "1.1.0")["manifest"]["untracked_baseline"] == [
        "notes.local"]
    assert get_version_files(engine, SLUG)["version_files"] == ["pyproject.toml"]
    assert "notes.local" not in git(repo, "show", "--name-only", "--format=", "HEAD")
    assert git(repo, "rev-parse", "HEAD^") == candidate
    assert (repo / "notes.local").exists()


def test_a_contract_change_is_shown_and_approved_by_the_proposal_digest(engine, proj):
    repo, _ = proj
    set_version_files(engine, SLUG, ["VERSION"], confirm=True)
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepared = prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                               plan_digest=digest)["details"]
    assert prepared["contract"] == {"confirmed": ["VERSION"], "proposed": ["pyproject.toml"],
                                    "change": True}
    assert get_version_files(engine, SLUG)["version_files"] == ["VERSION"]
    apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=digest,
          proposal_digest=prepared["proposal_sha256"])
    contract = get_version_files(engine, SLUG)
    assert contract["version_files"] == ["pyproject.toml"]
    assert contract["history"][-1]["version_files"] == ["VERSION"]  # the replaced contract


def test_failing_sync_script_stops_before_the_stamp(engine, proj):
    repo, _ = proj
    (repo / "scripts" / "dct-version-sync.sh").write_text("#!/bin/bash\nexit 3\n")
    git(repo, "commit", "-qam", "chore: broken sync")
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(SyncFailed) as exc:
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                        plan_digest=digest)
    assert exc.value.exit_code == 3
    assert get_release(engine, SLUG, "1.1.0") is None
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"


# ── resume negatives: contradictions stop without overwriting ────────────────


def _interrupted(engine, repo, point, **kw):
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at(point), **kw)
    return a, candidate


def test_partial_section_in_the_changelog_is_not_overwritten(engine, proj):
    repo, log = proj
    _interrupted(engine, repo, "before_changelog")
    partial = "# Changelog\n\n## [1.1.0] - 2026-10-01\n\n### Added\n\n" + HISTORY[13:]
    (repo / "changelog.md").write_text(partial)
    with pytest.raises(ReleaseRefused, match="differ"):
        resume(engine, repo)
    assert (repo / "changelog.md").read_text() == partial
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    assert sync_runs(log) == ["1.1.0"]  # prepare only


def test_reverted_version_file_after_files_written_is_refused(engine, proj):
    repo, _ = proj
    _, candidate = _interrupted(engine, repo, "after_files_written")
    git(repo, "checkout", "--", "pyproject.toml")
    before = snapshot(repo)
    with pytest.raises(ReleaseRefused, match="manifest"):
        resume(engine, repo)
    assert snapshot(repo) == before
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    assert git(repo, "rev-parse", "HEAD") == candidate


def test_foreign_staged_content_inside_a_release_file_is_refused(engine, proj):
    """K-50: an allowlist of paths would not see this — the CONTENT of a
    release artifact is checked in the index."""
    repo, _ = proj
    _, candidate = _interrupted(engine, repo, "after_files_written")
    good = (repo / "pyproject.toml").read_bytes()
    (repo / "pyproject.toml").write_bytes(good + b"foreign = true\n")
    git(repo, "add", "pyproject.toml")
    (repo / "pyproject.toml").write_bytes(good)  # the work tree matches the manifest again
    index = git(repo, "ls-files", "-s")
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, repo)
    assert exc.value.details["foreign_content"] == ["pyproject.toml"]
    assert git(repo, "ls-files", "-s") == index  # the user's staging is untouched
    assert git(repo, "rev-parse", "HEAD") == candidate
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"


def test_foreign_staged_path_at_resume_is_refused(engine, proj):
    repo, _ = proj
    _, candidate = _interrupted(engine, repo, "after_files_written")
    (repo / "other.txt").write_text("x\n")
    git(repo, "add", "other.txt")
    index = git(repo, "ls-files", "-s")
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, repo)
    assert exc.value.details["foreign_paths"] == ["other.txt"]
    assert git(repo, "ls-files", "-s") == index
    assert git(repo, "rev-parse", "HEAD") == candidate


def test_interrupted_before_the_commit_resumes_with_the_index_untouched(engine, proj):
    """K-79: the release commit is built from a TEMPORARY index — the real
    index is never staged before the commit (no `git add` on it)."""
    repo, _ = proj
    a, candidate = _interrupted(engine, repo, "before_commit")
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert git(repo, "rev-parse", "HEAD") == candidate
    resume(engine, repo)
    assert_released(engine, repo, candidate, [a])


def test_resume_from_another_checkout_is_refused(engine, proj, tmp_path):
    repo, log = proj
    _, candidate = _interrupted(engine, repo, "after_stamp")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), candidate)
    write_sync_script(wt, log)
    before_wt = git(wt, "status", "--porcelain")
    with pytest.raises(ReleaseRefused, match="never re-pinned"):
        resume(engine, wt)
    record = get_release(engine, SLUG, "1.1.0")
    assert record["checkout_root"] == str(repo) and record["status"] == "stamped"
    assert git(wt, "status", "--porcelain") == before_wt
    assert sync_runs(log) == ["1.1.0"]  # prepare only


def test_resume_on_a_different_head_is_refused_and_never_syncs(engine, proj):
    repo, log = proj
    _interrupted(engine, repo, "after_stamp")
    newer = commit(repo, "feat: later work\n")
    with pytest.raises(ReleaseRefused, match="not the release candidate"):
        resume(engine, repo)
    assert git(repo, "rev-parse", "HEAD") == newer
    assert sync_runs(log) == ["1.1.0"]  # prepare only
    assert not (repo / "changelog.md").read_text().count("1.1.0")


def test_head_moved_after_the_commit_still_gets_the_tag(engine, proj):
    """From 'committed' on only the tag is missing: a newer HEAD that
    contains the release commit is fine, the sync is not re-run."""
    repo, log = proj
    _interrupted(engine, repo, "after_committed")
    release_commit = git(repo, "rev-parse", "HEAD")
    commit(repo, "feat: next\n")
    resume(engine, repo)
    assert git(repo, "rev-parse", "v1.1.0") == release_commit
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"
    assert sync_runs(log) == ["1.1.0"]


def test_existing_conflicting_tag_is_refused(engine, proj):
    """K-8 (#876 round 2): a local tag of V elsewhere is known before the
    stamp — prepare and apply refuse it then, never the tag step after the
    release commit (which left a 'committed' record)."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    git(repo, "tag", "v1.1.0", "v1.0.0")
    head = git(repo, "rev-parse", "HEAD")
    plan = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(ReleaseRefused, match="v1.1.0"):
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                        plan_digest=plan)
    with pytest.raises(ReleaseRefused):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert git(repo, "rev-parse", "v1.1.0") == git(repo, "rev-parse", "v1.0.0")
    assert get_release(engine, SLUG, "1.1.0") is None
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert git(repo, "rev-parse", "HEAD") == head


def test_unfinished_record_blocks_a_new_apply(engine, proj):
    repo, _ = proj
    _interrupted(engine, repo, "after_stamp")
    with pytest.raises(ReleaseRefused, match="resume"):
        apply(engine, repo, version="1.2.0", no_entries=True)


# ── abandon (K-60) ───────────────────────────────────────────────────────────


def test_abandoned_record_has_no_effects_and_keeps_the_version(engine, proj):
    repo, log = proj
    a, _ = _interrupted(engine, repo, "after_changelog")
    out = abandon(engine, SLUG, "1.1.0", "broken build", confirm=True)
    assert out["outcome"] == "abandoned" and out["release"]["status"] == "abandoned"
    before = snapshot(repo)
    assert resume(engine, repo)["outcome"] == "noop"
    assert snapshot(repo) == before
    b = proven(engine, repo, "feature b")
    git(repo, "checkout", "--", "changelog.md")
    with pytest.raises(ReleaseRefused, match="different set"):
        apply(engine, repo, version="1.1.0", entry_ids=[a, b])
    assert get_release(engine, SLUG, "1.1.0")["status"] == "abandoned"
    assert get_project(engine, SLUG)["current_version"] == "1.1.0"
    assert sync_runs(log) == ["1.1.0"]  # prepare only


# ── reconstruct (K-60) ───────────────────────────────────────────────────────


@pytest.fixture
def legacy(engine, proj):
    """1.1.0 released by hand BEFORE records existed: entry stamped,
    current_version 1.1.0, release commit + tag + section + pyproject."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    when = datetime(2026, 2, 1, 10, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version="1.1.0", released_at=when))
        conn.execute(projects.update().values(current_version="1.1.0"))
    (repo / "changelog.md").write_text(
        "# Changelog\n\n" + released_section("2026-02-01") + "\n" + HISTORY[13:])
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.1.0"\n')
    git(repo, "commit", "-qam", "release: v1.1.0")
    git(repo, "tag", "v1.1.0")
    return repo, a, candidate, git(repo, "rev-parse", "HEAD")


def test_reconstruct_with_clean_evidence(engine, legacy):
    repo, a, candidate, release_commit = legacy
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["outcome"] == "preview" and get_release(engine, SLUG, "1.1.0") is None
    evidence = preview["details"]["evidence"]
    assert evidence["candidate_sha"] == candidate
    assert evidence["release_commit_sha"] == release_commit and evidence["tag"] == "v1.1.0"
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    record = out["release"]
    assert record["status"] == "completed" and record["mode"] == "reconstructed"
    assert record["entry_ids"] == [a] and record["release_commit_sha"] == release_commit
    assert record["manifest"]["tree"] == git(repo, "rev-parse", "HEAD^{tree}")
    assert record["manifest"]["changelog"]["section_date"] == "2026-02-01"
    assert [f["path"] for f in record["manifest"]["version_files"]] == ["pyproject.toml"]
    # nothing re-stamped, nothing touched
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == a] == ["1.1.0"]
    assert git(repo, "status", "--porcelain") == ""


# K-96 D8: a second commit titled like the release (it fails verification)
# and a missing tag (a 'committed' reconstruction) are no longer refusals —
# tests/test_release_reconstruct.py pins both.
@pytest.mark.parametrize("ambiguity", ["section_mismatch", "version_file_without_v"])
def test_reconstruct_refuses_ambiguous_evidence(engine, legacy, ambiguity):
    repo, a, candidate, _ = legacy
    if ambiguity == "section_mismatch":
        with engine.begin() as conn:
            conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                         .values(entry="something else"))
    else:
        set_version_files(engine, SLUG, ["pyproject.toml"], confirm=True)
        git(repo, "tag", "-d", "v1.1.0")
        git(repo, "reset", "-q", "--hard", candidate)
        (repo / "changelog.md").write_text(
            "# Changelog\n\n" + released_section("2026-02-01") + "\n" + HISTORY[13:])
        (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
        git(repo, "commit", "-qam", "release: v1.1.0")
        git(repo, "tag", "v1.1.0")
    with pytest.raises(ReleaseRefused):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert get_release(engine, SLUG, "1.1.0") is None


# ── projects without git (K-63) ──────────────────────────────────────────────


def test_release_of_a_project_without_git(engine, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "changelog.md").write_text(HISTORY)
    create_project(engine, slug=SLUG, name="P", path=str(plain), current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    out = apply(engine, tmp_path, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    assert record["candidate_sha"] is None and record["release_commit_sha"] is None
    assert "tree" not in record["manifest"]
    text = (plain / "changelog.md").read_text()
    assert text == "# Changelog\n\n" + released_section(record_date(engine)) + "\n" + HISTORY[13:]


# ── CLI surface ──────────────────────────────────────────────────────────────


@pytest.fixture
def cli_proj(proj, engine, monkeypatch):
    return use_cli(proj, engine, monkeypatch)


def use_cli(proj, engine, monkeypatch):
    """Point the CLI at the test engine and the project's checkout."""
    from dct import cli

    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli, "_get_readonly_engine", lambda: engine)
    monkeypatch.chdir(proj[0])
    return proj


def _exit(monkeypatch, argv) -> int:
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, argv)
    return exc.value.code


def test_cli_apply_json_document(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    a = proven(engine, repo, "feature a")
    run_cli(monkeypatch, ["release", "apply", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--confirm", "--json",
                          *cli_digest(engine, repo, "1.1.0", [a])])
    doc = json.loads(capsys.readouterr().out)
    assert tuple(doc) == RESULT_KEYS and doc["outcome"] == "completed"
    assert doc["release"]["status"] == "completed"


def test_cli_apply_needs_confirm_and_an_explicit_selection(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    a = proven(engine, repo, "feature a")
    assert _exit(monkeypatch, ["release", "apply", "-P", SLUG, "--version", "1.1.0"]) == 2
    assert _exit(monkeypatch, ["release", "apply", "-P", SLUG, "--version", "1.1.0",
                               "--entries", str(a), "--json",
                               *cli_digest(engine, repo, "1.1.0", [a])]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "refused" and "--confirm" in doc["details"]["error"]
    assert get_release(engine, SLUG, "1.1.0") is None


def test_cli_clog_release_alias_refuses_unproven_entries(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    proven(engine, repo, "feature a")
    b = add_entry(engine, SLUG, "added", "no proof")["id"]
    assert _exit(monkeypatch, ["clog", "release", "--confirm", "-P", SLUG, "1.1.0"]) == 1
    assert f"#{b} (no_trailer)" in capsys.readouterr().err
    assert get_release(engine, SLUG, "1.1.0") is None


def test_cli_clog_release_alias_releases_every_proven_entry(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    run_cli(monkeypatch, alias_argv(engine, SLUG, [
        "clog", "release", "--confirm", "-P", SLUG, "--minor"]))
    assert "Released 1 entries as 1.1.0" in capsys.readouterr().out
    assert_released(engine, repo, candidate, [a])


def test_cli_resume_and_abandon(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    _interrupted(engine, repo, "after_stamp")
    assert _exit(monkeypatch, ["release", "abandon", "-P", SLUG, "--version", "1.1.0",
                               "--reason", "x"]) == 1
    run_cli(monkeypatch, ["release", "abandon", "-P", SLUG, "--version", "1.1.0",
                          "--reason", "x", "--confirm", "--json"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "abandoned"
    run_cli(monkeypatch, ["release", "resume", "-P", SLUG, "--json"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "noop"


def test_cli_sync_failure_exits_with_the_scripts_code(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    (repo / "scripts" / "dct-version-sync.sh").write_text("#!/bin/bash\nexit 3\n")
    git(repo, "commit", "-qam", "chore: broken sync")
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    assert _exit(monkeypatch, ["release", "prepare", "-P", SLUG, "--version", "1.1.0",
                               "--entries", str(a), "--json", "--plan-digest", digest]) == 3
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "failed" and doc["release"] is None
    assert doc["details"]["sync"]["exit_code"] == 3
    assert get_release(engine, SLUG, "1.1.0") is None


def test_result_keys_are_the_documented_schema():
    assert RESULT_KEYS == ("schema_version", "action", "project", "outcome", "version",
                           "release", "steps", "warnings", "details")
    assert effects.SCHEMA_VERSION == 1


def test_existing_tag_in_another_spelling_at_the_release_commit_is_not_doubled(engine, proj):
    """K-55: no second tag only because of an alternative spelling."""
    repo, _ = proj
    _interrupted(engine, repo, "after_committed")
    git(repo, "tag", "1.1", "HEAD")
    resume(engine, repo)
    assert git(repo, "tag", "--list").split() == ["1.1", "v1.0.0"]
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"


def test_operation_in_progress_refuses_before_stamping(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    (git_dir / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD") + "\n")
    with pytest.raises(ReleaseRefused, match="in progress") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["in_progress"] == ["MERGE_HEAD"]
    assert get_release(engine, SLUG, "1.1.0") is None


def test_git_write_ignores_repository_location_variables(proj, monkeypatch, tmp_path):
    """The writer keeps identity variables but never a GIT_DIR / GIT_INDEX_FILE
    leaked from a hook environment: it writes to the repository it is told."""
    from dct.core import gitinfo

    repo, _ = proj
    other = Path(make_git_repo(tmp_path / "other"))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Release Bot")
    head = git(repo, "rev-parse", "HEAD")
    _, out = gitinfo.git_write(repo, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "x")
    sha = out.strip()
    assert git(repo, "cat-file", "-t", sha) == "commit"
    assert git(repo, "log", "-1", "--format=%an", sha) == "Release Bot"
    with pytest.raises(subprocess.CalledProcessError):
        git(other, "cat-file", "-t", sha)


def test_cli_release_status_json(engine, cli_proj, monkeypatch, capsys):
    repo, _ = cli_proj
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    capsys.readouterr()
    run_cli(monkeypatch, ["release", "status", "-P", SLUG, "--version", "1.1.0", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert tuple(doc) == RESULT_KEYS and doc["outcome"] == "found"
    assert doc["release"]["status"] == "completed"
    run_cli(monkeypatch, ["release", "status", "-P", SLUG, "--json"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "none"
