"""Round 4, R8d — reconstruction and the controlled takeover of an old
release (K-109, K-110, K-115 reconstruction side, K-117 J4, K-119 M3 / M9).

- K-109: a reconstruction verifies the WHOLE changelog history against the
  release commit's parent — F at the commit with the V section taken out
  must be F at the parent, byte for byte. A removed / edited older section,
  also a typo fix, is a deviation: the diff is shown, the commit is never
  called verified, nothing is recorded, nothing in the repository changes.
  The bootstrap exception (F absent at the parent) is explicit.
- K-110: an UNKNOWN version-file contract (None) is not a confirmed "no
  version files" ([]): without git None refuses and names the procedure
  that shows and confirms the contract; the scope of verification is
  recorded; a confirmed [] with no managed changelog is a LIMITED scope —
  no file, commit or tag is invented.
- K-119 M3 / K-117 J4: a record without consent (a reconstruction that ends
  at 'committed', a stamp made through MCP) is never left at "do it by hand
  or abandon": the takeover supplies consent and proposal FROM THE VERIFIED
  ARTIFACTS, without a re-stamp and without changing recorded proofs; resume
  then performs only the missing effects.
- Own executor: an `own` project is reconstructed through the adapter that
  verifies the evidence its tool supplies, never through dct's default
  parser.

Every refusal leaves the full database state identical; real repositories.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import git, make_git_repo, run_cli, tag_proof_for
from test_release_effects import (
    HISTORY,
    SLUG,
    commit,
    make_release_repo,
    proven,
    released_section,
    remote_refs,
    sync_runs,
    use_cli,
)

from dct.core import releases as ledger
from dct.core.changelog import (
    add_entry,
    list_entries,
    normalize_worktree,
    release_entries,
)
from dct.core.projects import (
    create_project,
    get_project,
    set_manages_changelog,
    set_release_executor,
)
from dct.core.releases import get_release, get_version_files
from dct.db import changelog_entries, projects, releases
from dct.workflows.release import effects
from dct.workflows.release.effects import (
    ReleaseRefused,
    reconstruct,
    record_progress,
    resume_release,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

GOOD = "# Changelog\n\n" + released_section("2026-02-01") + "\n" + HISTORY[13:]
PIN_REFS = "refs/dct/release-proposals"


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The fixtures build commits."""


def db_state(engine) -> tuple:
    """Every row of the three tables a release touches."""
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases))


def repo_state(repo) -> tuple:
    return (git(repo, "rev-parse", "HEAD"),
            git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            git(repo, "status", "--porcelain", "--untracked-files=all"),
            (Path(repo) / "changelog.md").read_bytes())


def stamp(engine, entry_id: int, version: str = "1.1.0") -> None:
    """Stamp BY HAND, the legacy way (no record)."""
    when = datetime(2026, 2, 1, 10, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == entry_id)
                     .values(version=version, released_at=when))
        conn.execute(projects.update().values(current_version=version))


def release_commit(repo, *, changelog: str = GOOD, tag: str | None = "v1.1.0",
                   pyproject: bool = True) -> tuple[str, str]:
    """The legacy release commit `release: v1.1.0` on HEAD → (candidate, commit)."""
    candidate = git(repo, "rev-parse", "HEAD")
    (repo / "changelog.md").write_text(changelog)
    if pyproject:
        (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.1.0"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "release: v1.1.0")
    if tag:
        git(repo, "tag", tag)
    return candidate, git(repo, "rev-parse", "HEAD")


def legacy(engine, repo, **kw) -> tuple[int, str, str]:
    """1.1.0 released by hand before records existed → (entry, candidate, commit)."""
    a = proven(engine, repo, "feature a")
    stamp(engine, a)
    candidate, head = release_commit(repo, **kw)
    return a, candidate, head


def forget_contract(engine) -> None:
    """The version-file contract as a project has it before anything was
    learned: None."""
    with engine.begin() as conn:
        conn.execute(projects.update().values(
            version_files=None, version_files_confirmed_at=None, version_files_history=None))


@pytest.fixture
def proj(engine, tmp_path):
    repo, log = make_release_repo(engine, tmp_path)
    return repo, log


# ── K-109: the whole changelog history against the parent ───────────────────


DEVIATIONS = {
    "removed_section": ("# Changelog\n\n" + released_section("2026-02-01"),
                        "-## [1.0.0] - 2026-01-01"),
    "typo_fix": (GOOD.replace("- first", "- First"), "+- First"),
    "appended_line": (GOOD + "- sneaked into 1.0.0\n", "+- sneaked into 1.0.0"),
    "preamble": (GOOD.replace("# Changelog", "# Change log"), "+# Change log"),
}


@pytest.mark.parametrize("deviation", sorted(DEVIATIONS))
@pytest.mark.parametrize("tag", ["v1.1.0", None])
def test_a_release_commit_that_changed_older_history_is_never_verified(
        engine, proj, deviation, tag):
    """The V section itself is the database's — only the OLDER content
    differs from the parent. Before K-109 this reconstructed."""
    repo, _ = proj
    changelog, diff_line = DEVIATIONS[deviation]
    _, _, head = legacy(engine, repo, changelog=changelog, tag=tag)
    before, files_before = db_state(engine), repo_state(repo)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused, match="history") as exc:
            reconstruct(engine, SLUG, "1.1.0", confirm=confirm, cwd=str(repo))
        [candidate] = [c for c in exc.value.details["candidates"] if c["commit"] == head]
        assert candidate["verified"] is False
        assert candidate["history"]["state"] == "changed"
        assert diff_line in candidate["history"]["diff"]
        assert len(candidate["history"]["diff_sha256"]) == 64
    assert db_state(engine) == before          # nothing recorded
    assert repo_state(repo) == files_before    # the old commit and the file are untouched


def test_a_typo_fix_shows_both_sides_of_the_change(engine, proj):
    repo, _ = proj
    legacy(engine, repo, changelog=DEVIATIONS["typo_fix"][0])
    with pytest.raises(ReleaseRefused) as exc:
        reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    diff = exc.value.details["candidates"][0]["history"]["diff"]
    assert "-- first" in diff and "+- First" in diff
    # the V section itself is not part of the history diff
    assert not any("feature a" in line for line in diff)


def test_a_whitespace_only_change_of_an_older_section_is_a_deviation_too(engine, proj):
    """Normalising the NEW block must not hide a change of an older one."""
    repo, _ = proj
    legacy(engine, repo, changelog=GOOD + "\n")
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="history") as exc:
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert exc.value.details["candidates"][0]["history"]["state"] == "changed"
    assert db_state(engine) == before


def test_preserved_history_is_recorded_as_the_verified_scope(engine, proj):
    repo, _ = proj
    _, _, head = legacy(engine, repo)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    evidence = preview["details"]["evidence"]
    assert evidence["verification"]["history"] == "preserved"
    [found] = evidence["candidates"]
    assert found["verified"] is True and found["history"] == {"state": "preserved"}
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert record["manifest"]["verification"]["history"] == "preserved"
    assert (record["status"], record["release_commit_sha"]) == ("completed", head)


def test_the_bootstrap_exception_is_explicit(engine, proj):
    """F does not exist at the parent: there is no earlier history to
    preserve — said so, never reported as 'preserved'."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    git(repo, "rm", "-q", "changelog.md")
    git(repo, "commit", "-q", "-m", "chore: drop the changelog")
    stamp(engine, a)
    _, head = release_commit(repo)
    evidence = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))["details"]["evidence"]
    assert evidence["verification"]["history"] == "bootstrap"
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert record["manifest"]["verification"]["history"] == "bootstrap"
    assert record["release_commit_sha"] == head


def test_an_equivalent_label_and_the_section_date_still_reconstruct(engine, proj):
    """The existing rules stay: `## [1.1]` for a release stored as 1.1.0, its
    own date kept — with the older history preserved."""
    repo, _ = proj
    changelog = "# Changelog\n\n" + released_section("2025-12-24", "1.1") + "\n" + HISTORY[13:]
    legacy(engine, repo, changelog=changelog)
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert record["version"] == "1.1.0"
    assert record["manifest"]["changelog"]["section_date"] == "2025-12-24"
    assert record["manifest"]["verification"]["history"] == "preserved"


def test_cli_shows_the_history_diff_of_a_refused_reconstruction(
        engine, proj, monkeypatch, capsys):
    repo, _ = use_cli(proj, engine, monkeypatch)
    legacy(engine, repo, changelog=DEVIATIONS["typo_fix"][0])
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["release", "reconstruct", "-P", SLUG, "--version", "1.1.0"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "-- first" in err and "+- First" in err and "history" in err


# ── K-110: None (unknown) is not [] (confirmed none) ─────────────────────────


@pytest.fixture
def plain(engine, tmp_path):
    """A project CONFIRMED outside git, 1.1.0 released by hand; no contract."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "changelog.md").write_text(GOOD)
    (root / "VERSION").write_text("1.1.0\n")
    create_project(engine, slug=SLUG, name="P", path=str(root), current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    stamp(engine, a)
    assert get_version_files(engine, SLUG)["version_files"] is None
    return root, a


def test_without_git_an_unknown_contract_refuses_and_names_the_procedure(engine, plain):
    root, _ = plain
    before = db_state(engine)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused, match="dct release version-files") as exc:
            reconstruct(engine, SLUG, "1.1.0", confirm=confirm, cwd=str(root))
        assert exc.value.details["contract"] == {"version_files": None, "state": "unknown"}
    assert db_state(engine) == before


def test_the_contract_procedure_shows_and_confirms(engine, plain):
    _ = plain
    shown = effects.version_files_contract(engine, SLUG)
    assert shown["outcome"] == "none"
    assert shown["details"]["contract"] == {
        "version_files": None, "state": "unknown", "confirmed_at": None}
    before = db_state(engine)
    preview = effects.version_files_contract(engine, SLUG, paths=["VERSION"])
    assert preview["outcome"] == "preview" and preview["details"]["proposed"] == ["VERSION"]
    assert db_state(engine) == before          # showing a proposal writes nothing
    done = effects.version_files_contract(engine, SLUG, paths=["VERSION"], confirm=True)
    assert done["outcome"] == "confirmed"
    assert done["details"]["contract"]["version_files"] == ["VERSION"]
    assert done["details"]["contract"]["state"] == "confirmed"
    assert get_version_files(engine, SLUG)["version_files"] == ["VERSION"]
    again = effects.version_files_contract(engine, SLUG)
    assert again["outcome"] == "found"


def test_a_confirmed_empty_contract_is_shown_as_confirmed_not_as_unknown(engine, plain):
    _ = plain
    effects.version_files_contract(engine, SLUG, paths=[], confirm=True)
    shown = effects.version_files_contract(engine, SLUG)
    assert shown["outcome"] == "found"
    assert shown["details"]["contract"]["version_files"] == []
    assert shown["details"]["contract"]["state"] == "confirmed"


def test_without_git_the_declared_artifacts_are_checked_and_the_scope_recorded(engine, plain):
    root, _ = plain
    effects.version_files_contract(engine, SLUG, paths=["VERSION"], confirm=True)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    evidence = preview["details"]["evidence"]
    assert evidence["contract"] == {"version_files": ["VERSION"], "state": "confirmed"}
    scope = evidence["verification"]
    assert (scope["contract"], scope["changelog"], scope["version_files"]) == (
        ["VERSION"], "section_verified", "verified")
    assert scope["history"] == "not_verifiable" and scope["limited"] is False
    assert (scope["commit"], scope["tag"]) == (False, False)
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))["release"]
    assert record["status"] == "completed" and record["release_mode"] == "without_git"
    assert record["manifest"]["verification"] == scope
    assert [f["path"] for f in record["manifest"]["version_files"]] == ["VERSION"]


def test_a_declared_file_without_the_version_refuses(engine, plain):
    root, _ = plain
    effects.version_files_contract(engine, SLUG, paths=["VERSION"], confirm=True)
    (root / "VERSION").write_text("9.9.9\n")
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="VERSION"):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))
    assert db_state(engine) == before


def test_no_changelog_and_a_confirmed_empty_contract_report_a_limited_scope(engine, plain):
    """Nothing but the database proves this release: said so — no file,
    commit or tag is invented."""
    root, _ = plain
    set_manages_changelog(engine, SLUG, False)
    with pytest.raises(ReleaseRefused, match="dct release version-files"):
        reconstruct(engine, SLUG, "1.1.0", cwd=str(root))      # None is still unknown
    effects.version_files_contract(engine, SLUG, paths=[], confirm=True)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    evidence = preview["details"]["evidence"]
    assert evidence["contract"] == {"version_files": [], "state": "confirmed"}
    scope = evidence["verification"]
    assert scope["limited"] is True
    assert (scope["contract"], scope["changelog"], scope["version_files"]) == (
        [], "not_managed", "none")
    assert any("limited" in w for w in preview["warnings"])
    assert evidence["manifest"]["version_files"] == []
    assert "changelog" not in evidence["manifest"] and "tree" not in evidence["manifest"]
    assert (evidence["candidate_sha"], evidence["release_commit_sha"], evidence["tag"],
            evidence["tag_proof"]) == (None, None, None, None)
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))
    record = out["release"]
    assert record["manifest"]["verification"]["limited"] is True
    assert record["manifest"]["verification"]["contract"] == []
    assert (record["candidate_sha"], record["release_commit_sha"], record["tag_proof"],
            record["push_proof"]) == (None, None, None, None)
    assert any("limited" in w for w in out["warnings"])


def test_in_git_an_unknown_contract_is_shown_and_stored_as_unknown(engine, proj):
    repo, _ = proj
    forget_contract(engine)
    legacy(engine, repo)
    evidence = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))["details"]["evidence"]
    assert evidence["contract"] == {"version_files": None, "state": "unknown"}
    assert evidence["verification"]["contract"] is None
    assert evidence["verification"]["version_files"] == "from_commit"
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert record["manifest"]["verification"]["contract"] is None
    assert [f["path"] for f in record["manifest"]["version_files"]] == ["pyproject.toml"]
    assert get_version_files(engine, SLUG)["version_files"] is None   # nothing learned silently


def test_in_git_a_confirmed_empty_contract_is_stored_as_confirmed(engine, proj):
    repo, _ = proj
    effects.version_files_contract(engine, SLUG, paths=[], confirm=True)
    legacy(engine, repo, pyproject=False)
    evidence = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))["details"]["evidence"]
    assert evidence["contract"] == {"version_files": [], "state": "confirmed"}
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    scope = record["manifest"]["verification"]
    assert (scope["contract"], scope["version_files"]) == ([], "none")
    assert record["manifest"]["version_files"] == []


def test_core_refuses_a_reconstruction_without_git_that_records_no_scope(engine, plain):
    """The ledger part of K-110: `version_files: []` alone is not evidence."""
    _, a = plain
    before = db_state(engine)
    kwargs = {"entry_ids": [a], "candidate_sha": None, "release_commit_sha": None,
              "status": "completed", "confirm": True, "without_git": True}
    with pytest.raises(ValueError, match="verification"):
        ledger.reconstruct_release(engine, SLUG, "1.1.0", manifest={"version_files": []},
                                   **kwargs)
    with pytest.raises(ValueError, match="unknown"):
        ledger.reconstruct_release(
            engine, SLUG, "1.1.0", manifest={
                "version_files": [], "verification": {"contract": None}}, **kwargs)
    assert db_state(engine) == before
    record = ledger.reconstruct_release(
        engine, SLUG, "1.1.0", manifest={
            "version_files": [], "verification": {"contract": []}}, **kwargs)
    assert record["manifest"]["verification"] == {"contract": []}


def test_cli_version_files_shows_and_confirms(engine, plain, monkeypatch, capsys):
    root, _ = plain
    use_cli((root, None), engine, monkeypatch)
    run_cli(monkeypatch, ["release", "version-files", "-P", SLUG, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "none" and doc["details"]["contract"]["state"] == "unknown"
    run_cli(monkeypatch, ["release", "version-files", "-P", SLUG, "--none", "--json"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "preview"
    assert get_version_files(engine, SLUG)["version_files"] is None
    run_cli(monkeypatch, ["release", "version-files", "-P", SLUG, "--none", "--confirm"])
    assert "confirmed" in capsys.readouterr().out
    assert get_version_files(engine, SLUG)["version_files"] == []
    run_cli(monkeypatch, ["release", "version-files", "-P", SLUG, "--set", "VERSION",
                          "--confirm", "--json"])
    assert json.loads(capsys.readouterr().out)["details"]["contract"]["version_files"] == [
        "VERSION"]


# ── K-119 M3: the proven stage, every alternative, a way to finish ───────────


def test_a_committed_reconstruction_names_the_ways_to_finish(engine, proj):
    repo, _ = proj
    legacy(engine, repo, tag=None)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["details"]["evidence"]["status"] == "committed"
    ways = {w["way"]: w for w in preview["details"]["finish"]}
    assert set(ways) == {"takeover", "ledger"}
    assert "dct release takeover --version 1.1.0" in ways["takeover"]["command"]
    assert "release-advance" in ways["ledger"]["command"]
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert {w["way"] for w in out["details"]["finish"]} == {"takeover", "ledger"}


def test_a_completed_reconstruction_has_nothing_left_to_finish(engine, proj):
    repo, _ = proj
    legacy(engine, repo)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["details"]["finish"] == []


def two_verified_candidates(engine, repo) -> tuple[str, str]:
    """The same release on two refs, no tag → (the one on main, the other)."""
    _, candidate, head = legacy(engine, repo, tag=None)
    git(repo, "checkout", "-q", "-b", "other", candidate)
    git(repo, "cherry-pick", "--keep-redundant-commits", head)
    git(repo, "commit", "-q", "--amend", "-m", "release: v1.1.0\n\nSame release, other ref.")
    other = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return head, other


def test_several_verified_candidates_are_all_shown_with_the_explicit_choice(engine, proj):
    repo, _ = proj
    head, other = two_verified_candidates(engine, repo)
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="--release-commit") as exc:
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    shown = {c["commit"] for c in exc.value.details["candidates"] if c["verified"]}
    assert shown == {head, other}
    assert db_state(engine) == before
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo),
                         release_commit=other)["release"]
    assert (record["status"], record["release_commit_sha"]) == ("committed", other)


def test_the_explicit_choice_must_be_a_verified_candidate(engine, proj):
    repo, _ = proj
    head, _ = two_verified_candidates(engine, repo)
    stray = git(repo, "rev-parse", f"{head}^")
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="not a verified"):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo), release_commit=stray)
    assert db_state(engine) == before


def test_the_assigned_executor_is_shown_as_a_responsibility_not_as_provenance(
        engine, proj, monkeypatch, capsys):
    repo, _ = use_cli(proj, engine, monkeypatch)
    legacy(engine, repo)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["details"]["executor"] == {"assigned": "dct", "provenance": False}
    run_cli(monkeypatch, ["release", "reconstruct", "-P", SLUG, "--version", "1.1.0"])
    out = capsys.readouterr().out
    assert "responsibility" in out and "not who made the release" in out


# ── the takeover (K-119 M3 / K-117 J4) ───────────────────────────────────────


def entry_rows(engine) -> list:
    with engine.begin() as conn:
        return [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))]


def frozen(record: dict) -> dict:
    """What a takeover must never change on a record."""
    return {k: record[k] for k in ("id", "version", "mode", "entry_ids", "created_at", "proof",
                                   "executor", "tag_proof", "push_proof")}


def take_over(engine, repo, **kw) -> dict:
    """Preview, then confirm with the digest of what the preview showed."""
    preview = effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), **kw)
    assert preview["outcome"] == "preview"
    return effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=True,
                            takeover_digest=preview["details"]["takeover_digest"], **kw)


def test_resume_of_a_reconstructed_record_names_the_takeover(engine, proj):
    repo, _ = proj
    legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    with pytest.raises(ReleaseRefused, match="dct release takeover"):
        resume_release(engine, SLUG, cwd=str(repo))


def test_takeover_then_resume_performs_only_the_missing_effects(engine, proj):
    repo, log = proj
    a, candidate, head = legacy(engine, repo, tag=None)
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert record["status"] == "committed"
    entries, project = entry_rows(engine), get_project(engine, SLUG)["current_version"]

    before = db_state(engine)
    preview = effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["outcome"] == "preview" and db_state(engine) == before
    shown = preview["details"]["consent"]
    assert (shown["version"], shown["entry_ids"], shown["candidate_sha"]) == (
        "1.1.0", [a], candidate)
    assert shown["executor"] == "dct"
    assert shown["takeover"]["release_commit_sha"] == head
    assert shown["takeover"]["tree"] == git(repo, "rev-parse", f"{head}^{{tree}}")
    assert shown["push_target"] == {
        "remote": "origin", "url": git(repo, "remote", "get-url", "--push", "origin"),
        "branch": "main", "tag": "v1.1.0"}
    assert preview["details"]["missing_effects"] == ["tag", "push"]

    with pytest.raises(ReleaseRefused, match="--takeover-digest"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=True)
    with pytest.raises(ReleaseRefused, match="takeover-digest"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=True,
                         takeover_digest="0" * 64)
    assert db_state(engine) == before

    out = effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=True,
                           takeover_digest=preview["details"]["takeover_digest"])
    taken = out["release"]
    assert out["outcome"] == "taken_over"
    assert taken["status"] == "committed"                       # no status invented
    assert frozen(taken) == frozen(record)                      # recorded facts unchanged
    assert taken["manifest"] == record["manifest"]
    assert (taken["candidate_sha"], taken["release_commit_sha"]) == (candidate, head)
    assert taken["consent"]["takeover"]["release_commit_sha"] == head
    assert taken["consent"]["proposal_sha256"] == ledger.proposal_digest(taken["proposal"])
    assert taken["proposal"]["paths"] == ["pyproject.toml"]
    assert taken["proposal"]["expected_blobs"]["pyproject.toml"] == git(
        repo, "rev-parse", f"{head}:pyproject.toml")
    assert ledger.progress_owner(taken) == "dct"
    pins = git(repo, "for-each-ref", "--format=%(refname)", PIN_REFS).split()
    assert pins == [effects.pin_ref(SLUG, taken["proposal"])]
    assert entry_rows(engine) == entries                        # no re-stamp
    assert get_project(engine, SLUG)["current_version"] == project   # no new bump

    files = (repo / "changelog.md").read_bytes(), (repo / "pyproject.toml").read_bytes()
    done = resume_release(engine, SLUG, cwd=str(repo))
    assert done["outcome"] == "completed"
    assert [(s["step"], s["result"]) for s in done["steps"]] == [
        ("commit", "verified"), ("tag", "done"), ("push", "done"), ("complete", "done")]
    final = done["release"]
    assert final["status"] == "completed" and frozen(final) | {
        "tag_proof": None, "push_proof": None} == frozen(record)
    assert git(repo, "rev-parse", "HEAD") == head               # no new commit
    assert git(repo, "rev-parse", "v1.1.0") == head
    remote = remote_refs(repo)
    assert remote["refs/heads/main"] == head and remote["refs/tags/v1.1.0"] == head
    assert final["tag_proof"]["commit"] == head and final["push_proof"]["tag_commit"] == head
    assert ((repo / "changelog.md").read_bytes(), (repo / "pyproject.toml").read_bytes()) == files
    assert sync_runs(log) == []                                 # the sync never ran
    assert entry_rows(engine) == entries
    assert git(repo, "for-each-ref", "--format=%(refname)", PIN_REFS) == ""


def test_takeover_with_an_explicit_no_push_completes_locally(engine, proj):
    repo, _ = proj
    _, _, head = legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    remote_before = remote_refs(repo)
    out = take_over(engine, repo, no_push=True)
    assert out["release"]["consent"]["push_target"] is None
    assert out["details"]["missing_effects"] == ["tag"]
    done = resume_release(engine, SLUG, cwd=str(repo))
    assert [(s["step"], s["result"]) for s in done["steps"]] == [
        ("commit", "verified"), ("tag", "done"), ("push", "skipped"), ("complete", "done")]
    assert done["release"]["push_proof"] is None
    assert remote_refs(repo) == remote_before
    assert git(repo, "rev-parse", "v1.1.0") == head


def mcp_stamped(engine, repo, *, with_candidate: bool = True, tag: str | None = None):
    """J4 — stamped through the ledger (MCP release_changelog): a record
    without consent or proposal; the files, the commit (and the tag) were
    then made by hand → (entry, candidate, release commit, record)."""
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    extra = {"candidate_sha": candidate, "checkout_root": str(repo)} if with_candidate else {}
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], **extra)
    _, head = release_commit(repo, tag=tag)
    return a, candidate, head, get_release(engine, SLUG, "1.1.0")


@pytest.mark.parametrize("with_candidate", [True, False])
def test_a_record_stamped_through_the_ledger_is_taken_over_at_its_proven_stage(
        engine, proj, with_candidate):
    repo, log = proj
    _, candidate, head, record = mcp_stamped(engine, repo, with_candidate=with_candidate,
                                             tag="v1.1.0")
    assert record["status"] == "stamped" and record["consent"] is None
    with pytest.raises(ReleaseRefused, match="dct release takeover"):
        resume_release(engine, SLUG, cwd=str(repo))
    entries = entry_rows(engine)
    out = take_over(engine, repo)
    taken = get_release(engine, SLUG, "1.1.0")
    assert taken["status"] == "committed"        # where the verified evidence ends
    assert (taken["candidate_sha"], taken["release_commit_sha"]) == (candidate, head)
    assert taken["checkout_root"] == normalize_worktree(str(repo))
    assert taken["manifest"]["tree"] == git(repo, "rev-parse", f"{head}^{{tree}}")
    assert frozen(taken) == frozen(record)
    assert out["details"]["missing_effects"] == ["push"]      # the tag exists already
    done = resume_release(engine, SLUG, cwd=str(repo))
    assert [(s["step"], s["result"]) for s in done["steps"]] == [
        ("commit", "verified"), ("tag", "verified"), ("push", "done"), ("complete", "done")]
    final = get_release(engine, SLUG, "1.1.0")
    assert final["status"] == "completed" and frozen(final) | {
        "tag_proof": None, "push_proof": None} == frozen(record)
    assert entry_rows(engine) == entries and sync_runs(log) == []
    assert git(repo, "rev-parse", "HEAD") == head


def test_takeover_keeps_a_tag_proof_recorded_through_the_ledger(engine, proj):
    repo, _ = proj
    _, _, head = legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    git(repo, "tag", "v1.1.0", head)
    tagged = record_progress(engine, SLUG, "1.1.0", "tagged",
                             tag_proof=tag_proof_for(head))["release"]
    out = take_over(engine, repo)
    assert out["release"]["status"] == "tagged"
    assert out["release"]["tag_proof"] == tagged["tag_proof"]    # recorded_at included
    done = resume_release(engine, SLUG, cwd=str(repo))
    assert done["release"]["tag_proof"] == tagged["tag_proof"]
    assert done["release"]["status"] == "completed"


@pytest.mark.parametrize("contradiction", [
    "no_release_commit",     # stamped, nothing made: no verified artifact to take over
    "another_candidate",     # the record's candidate is not the commit's parent
    "manifest",              # the recorded manifest differs from the commit's artifacts
    "section",               # the changelog section is not the database's entries
    "history",               # the release commit changed older history (K-109)
])
def test_takeover_is_refused_when_the_artifacts_contradict(engine, proj, contradiction):
    repo, _ = proj
    if contradiction == "no_release_commit":
        a = proven(engine, repo, "feature a")
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                        candidate_sha=git(repo, "rev-parse", "HEAD"), checkout_root=str(repo))
    elif contradiction == "another_candidate":
        a = proven(engine, repo, "feature a")
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                        candidate_sha=git(repo, "rev-parse", "HEAD"), checkout_root=str(repo))
        commit(repo, "chore: one more\n")
        release_commit(repo, tag=None)
    elif contradiction == "history":
        a = proven(engine, repo, "feature a")
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                        candidate_sha=git(repo, "rev-parse", "HEAD"), checkout_root=str(repo))
        release_commit(repo, tag=None, changelog=DEVIATIONS["typo_fix"][0])
    else:
        a, _, _ = legacy(engine, repo, tag=None)
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
        with engine.begin() as conn:
            if contradiction == "manifest":
                manifest = json.loads(conn.execute(sa.select(releases.c.manifest)).scalar())
                manifest["changelog"]["sha256"] = "0" * 64
                conn.execute(releases.update().values(manifest=json.dumps(manifest)))
            else:
                conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                             .values(entry="something else"))
    before, files = db_state(engine), repo_state(repo)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused) as exc:
            effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=confirm,
                             takeover_digest="0" * 64 if confirm else None)
        assert "release-advance" in str(exc.value)    # the ledger path stays, and is named
    assert db_state(engine) == before and repo_state(repo) == files
    assert git(repo, "for-each-ref", "--format=%(refname)", PIN_REFS) == ""


def test_takeover_needs_an_explicit_push_decision(engine, proj):
    repo, _ = proj
    legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    git(repo, "branch", "--unset-upstream")
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="--no-push"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    assert db_state(engine) == before


def test_a_changed_scope_between_preview_and_confirm_voids_the_digest(engine, proj):
    repo, _ = proj
    legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    preview = effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="takeover-digest"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=True, no_push=True,
                         takeover_digest=preview["details"]["takeover_digest"])
    assert db_state(engine) == before


def test_takeover_refuses_what_it_must_not_take(engine, proj):
    repo, _ = proj
    legacy(engine, repo, tag=None)
    with pytest.raises(ReleaseRefused, match="no release record"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    take_over(engine, repo)
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="dct release resume"):     # already dct's
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    assert db_state(engine) == before
    resume_release(engine, SLUG, cwd=str(repo))
    after = db_state(engine)
    with pytest.raises(ReleaseRefused, match="completed"):              # terminal
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    assert db_state(engine) == after


def test_core_advance_never_turns_a_record_into_one_dct_executes(engine, proj):
    """The open point of R8a-1: a consent written through the ledger path
    made the record dct-owned — only the takeover may do that."""
    repo, _ = proj
    a, candidate, _, _ = mcp_stamped(engine, repo)
    consent = {"version": "1.1.0", "entry_ids": [a], "candidate_sha": candidate}
    before = db_state(engine)
    for actor, message in (("ledger", "take_over_release"), ("dct", "not executed by dct")):
        with pytest.raises(ValueError, match=message):
            ledger.advance_release(engine, SLUG, "1.1.0", "stamped", consent=consent,
                                   actor=actor)
    assert db_state(engine) == before


def test_core_takeover_checks_the_record_and_is_write_once(engine, proj):
    repo, _ = proj
    a, candidate, head, _ = mcp_stamped(engine, repo)
    tree = git(repo, "rev-parse", f"{head}^{{tree}}")
    manifest = {"tree": tree, "version_files": []}
    consent = {"version": "1.1.0", "entry_ids": [a], "candidate_sha": candidate,
               "executor": "dct", "push_target": None,
               "takeover": {"release_commit_sha": head, "tree": tree,
                            "artifacts_sha256": ledger.artifacts_digest(manifest)}}
    call = {"consent": consent, "release_commit_sha": head, "manifest": manifest,
            "candidate_sha": candidate, "checkout_root": str(repo)}
    before = db_state(engine)
    with pytest.raises(ValueError, match="confirm"):
        ledger.take_over_release(engine, SLUG, "1.1.0", **call)
    with pytest.raises(ValueError, match="takeover"):                # names nothing verified
        ledger.take_over_release(engine, SLUG, "1.1.0", confirm=True, **{
            **call, "consent": {k: v for k, v in consent.items() if k != "takeover"}})
    with pytest.raises(ValueError, match="release commit"):          # another commit
        ledger.take_over_release(engine, SLUG, "1.1.0", confirm=True, **{
            **call, "release_commit_sha": candidate})
    assert db_state(engine) == before
    out = ledger.take_over_release(engine, SLUG, "1.1.0", confirm=True, **call)
    assert out["updated"] is True and out["release"]["status"] == "committed"
    assert out["release"]["release_mode"] == "git"
    again = ledger.take_over_release(engine, SLUG, "1.1.0", confirm=True, **call)
    assert again["updated"] is False
    state = db_state(engine)
    with pytest.raises(ValueError, match="already"):
        ledger.take_over_release(engine, SLUG, "1.1.0", confirm=True, **{
            **call, "consent": {**consent, "push_target": {"remote": "origin", "tag": "v1.1.0"}}})
    assert db_state(engine) == state


def test_cli_takeover_previews_and_confirms(engine, proj, monkeypatch, capsys):
    repo, _ = use_cli(proj, engine, monkeypatch)
    legacy(engine, repo, tag=None)
    run_cli(monkeypatch, ["release", "reconstruct", "-P", SLUG, "--version", "1.1.0",
                          "--confirm"])
    assert "dct release takeover --version 1.1.0" in capsys.readouterr().out
    run_cli(monkeypatch, ["release", "takeover", "-P", SLUG, "--version", "1.1.0", "--no-push",
                          "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "preview" and doc["action"] == "takeover"
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["release", "takeover", "-P", SLUG, "--version", "1.1.0",
                              "--no-push", "--confirm"])
    assert exc.value.code == 1
    capsys.readouterr()
    run_cli(monkeypatch, ["release", "takeover", "-P", SLUG, "--version", "1.1.0", "--no-push",
                          "--takeover-digest", doc["details"]["takeover_digest"], "--confirm"])
    assert "dct release resume" in capsys.readouterr().out
    assert get_release(engine, SLUG, "1.1.0")["consent"]["push_target"] is None


# ── the own executor: the adapter, never dct's default parser ───────────────


@pytest.fixture
def own(engine, tmp_path):
    """An `own` project: its own changelog layout, commit subject and tag
    name — none of them what dct's default parser assumes."""
    repo = Path(make_git_repo(tmp_path / "own"))
    (repo / "CHANGES.txt").write_text("1.0.0\n=====\n* first\n")
    (repo / "setup.cfg").write_text("[metadata]\nversion = 1.0.0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "chore: setup")
    create_project(engine, slug=SLUG, name="O", path=str(repo), current_version="1.0.0")
    set_release_executor(engine, SLUG, "own")
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    stamp(engine, a)
    (repo / "CHANGES.txt").write_text("1.1.0\n=====\n* feature a\n\n1.0.0\n=====\n* first\n")
    (repo / "setup.cfg").write_text("[metadata]\nversion = 1.1.0\n")
    git(repo, "commit", "-qam", "Bump to 1.1.0 [release]")
    git(repo, "tag", "-a", "-m", "release 1.1.0", "release-1.1.0")
    head = git(repo, "rev-parse", "HEAD")
    supplied = {
        "release_commit_sha": head, "candidate_sha": candidate, "status": "completed",
        "tag": "release-1.1.0",
        "artifacts": [{"path": p, "sha256": hashlib.sha256((repo / p).read_bytes()).hexdigest()}
                      for p in ("CHANGES.txt", "setup.cfg")]}
    return repo, a, candidate, head, supplied


def test_the_default_parser_refuses_an_own_project_and_names_the_adapter(engine, own):
    repo, *_ = own
    before = db_state(engine)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused, match="--evidence") as exc:
            reconstruct(engine, SLUG, "1.1.0", confirm=confirm, cwd=str(repo))
        assert exc.value.details["executor"] == "own"
    assert db_state(engine) == before


def test_the_own_adapter_records_what_it_verified_in_the_tools_own_format(engine, own):
    repo, a, candidate, head, supplied = own
    before = db_state(engine)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo), evidence=supplied)
    assert preview["outcome"] == "preview" and db_state(engine) == before
    evidence = preview["details"]["evidence"]
    assert evidence["verification"]["adapter"] == "own"
    assert preview["details"]["executor"] == {"assigned": "own", "provenance": False}
    # the tag identity comes from the REPOSITORY, not from the supplied data
    tag_object = git(repo, "rev-parse", "refs/tags/release-1.1.0")
    assert evidence["tag_proof"] == {"name": "release-1.1.0", "object": tag_object,
                                     "commit": head, "annotated": True}
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo),
                         evidence=supplied)["release"]
    assert (record["status"], record["executor"], record["mode"]) == (
        "completed", "own", "reconstructed")
    assert (record["candidate_sha"], record["release_commit_sha"]) == (candidate, head)
    assert record["entry_ids"] == [a]
    assert record["tag_proof"]["name"] == "release-1.1.0"
    assert record["push_proof"] is None and record["consent"] is None   # nothing invented
    assert record["manifest"]["artifacts"] == supplied["artifacts"]
    assert record["manifest"]["verification"]["adapter"] == "own"
    assert record["manifest"]["tree"] == git(repo, "rev-parse", f"{head}^{{tree}}")


@pytest.mark.parametrize("change, message", [
    ({"artifacts": [{"path": "setup.cfg", "sha256": "0" * 64}]}, "setup.cfg"),
    ({"artifacts": [{"path": "missing.txt", "sha256": "0" * 64}]}, "missing.txt"),
    ({"tag": "no-such-tag"}, "no-such-tag"),
    ({"tag": None}, "tag"),                               # 'completed' without a tag
    ({"candidate_sha": "f" * 40}, "candidate"),
    ({"release_commit_sha": "e" * 40}, "release commit"),
    ({"status": "pushed"}, "status"),
    ({"surprise": 1}, "surprise"),
])
def test_the_own_adapter_refuses_evidence_the_repository_contradicts(
        engine, own, change, message):
    repo, _, _, _, supplied = own
    before = db_state(engine)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused, match=message):
            reconstruct(engine, SLUG, "1.1.0", confirm=confirm, cwd=str(repo),
                        evidence={**supplied, **change})
    assert db_state(engine) == before


def test_a_tag_on_another_commit_is_refused_by_the_own_adapter(engine, own):
    repo, _, candidate, _, supplied = own
    git(repo, "tag", "elsewhere", candidate)
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="elsewhere"):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo),
                    evidence={**supplied, "tag": "elsewhere"})
    assert db_state(engine) == before


def test_an_own_record_keeps_the_ledger_path_and_gets_no_dct_takeover(engine, own):
    repo, _, _, head, supplied = own
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo),
                         evidence={**supplied, "status": "committed", "tag": None})["release"]
    assert record["status"] == "committed" and record["tag_proof"] is None
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="own"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), no_push=True)
    assert db_state(engine) == before
    tag_object = git(repo, "rev-parse", "refs/tags/release-1.1.0")
    out = record_progress(engine, SLUG, "1.1.0", "completed", tag_proof=tag_proof_for(
        head, "release-1.1.0", tag_object=tag_object))
    assert out["release"]["status"] == "completed"


def test_explicit_evidence_is_not_a_way_around_the_default_parser(engine, proj):
    """A dct-executed project is verified by dct's own rules (K-109) —
    supplied evidence would skip them."""
    repo, _ = proj
    _, candidate, head = legacy(engine, repo, changelog=DEVIATIONS["typo_fix"][0])
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="own"):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo), evidence={
            "release_commit_sha": head, "candidate_sha": candidate, "status": "completed",
            "tag": "v1.1.0", "artifacts": []})
    assert db_state(engine) == before


def test_cli_reconstruct_takes_the_own_evidence_file(engine, own, monkeypatch, capsys, tmp_path):
    repo, _, _, head, supplied = own
    use_cli((repo, None), engine, monkeypatch)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(supplied))
    run_cli(monkeypatch, ["release", "reconstruct", "-P", SLUG, "--version", "1.1.0",
                          "--evidence", str(path), "--confirm", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "reconstructed" and doc["release"]["executor"] == "own"
    assert doc["release"]["release_commit_sha"] == head


# ── K-115: a reconstruction never gets a fictitious confirmation ────────────


def test_a_reconstructed_record_carries_only_what_was_verified(engine, proj):
    repo, _ = proj
    _, _, head = legacy(engine, repo)
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert record["tag_proof"]["commit"] == head and record["tag_proof"]["name"] == "v1.1.0"
    assert record["push_proof"] is None and record["consent"] is None
    assert record["proposal"] is None
    assert record["manifest"]["verification"]["tag"] is True


def test_list_entries_are_untouched_by_every_reconstruction(engine, proj):
    repo, _ = proj
    legacy(engine, repo)
    entries = list_entries(engine, SLUG)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert list_entries(engine, SLUG) == entries


def test_takeover_of_an_old_release_pushes_only_the_missing_tag(engine, proj):
    """The usual shape of an OLD release: work went on after it and the
    remote branch is already ahead of the release commit. Resume must push
    what is MISSING (the tag) — never try to move the branch back."""
    repo, _ = proj
    _, _, head = legacy(engine, repo, tag=None)
    later = commit(repo, "feat: later work\n")
    git(repo, "push", "-q", "origin", "main")
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    take_over(engine, repo)
    done = resume_release(engine, SLUG, cwd=str(repo))
    assert done["release"]["status"] == "completed"
    remote = remote_refs(repo)
    assert remote["refs/tags/v1.1.0"] == head
    assert remote["refs/heads/main"] == later            # the branch was not touched
    assert git(repo, "rev-parse", "HEAD") == later
    assert done["release"]["push_proof"]["branch_tip"] == later


def test_the_first_section_of_a_title_only_changelog_preserves_it(engine, proj):
    """F had no version section before: the section was appended after the
    existing text — the parent's bytes are all still there."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    (repo / "changelog.md").write_text("# Changelog\n")
    git(repo, "commit", "-qam", "chore: start the changelog over")
    stamp(engine, a)
    release_commit(repo, changelog="# Changelog\n\n" + released_section("2026-02-01"))
    evidence = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))["details"]["evidence"]
    assert evidence["verification"]["history"] == "preserved"


def test_a_tag_created_after_the_preview_voids_the_takeover_digest(engine, proj):
    repo, _ = proj
    _, _, head = legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    preview = effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["details"]["stage"] == "committed"
    git(repo, "tag", "v1.1.0", head)
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="takeover-digest"):
        effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo), confirm=True,
                         takeover_digest=preview["details"]["takeover_digest"])
    assert db_state(engine) == before
    assert git(repo, "for-each-ref", "--format=%(refname)", PIN_REFS) == ""
    again = effects.takeover(engine, SLUG, "1.1.0", cwd=str(repo))
    assert again["details"]["stage"] == "tagged"
    assert again["details"]["missing_effects"] == ["push"]


def test_a_taken_over_release_is_abandoned_only_by_the_local_command(engine, proj):
    """After the takeover the record is one dct executes: the ledger path
    (no operation lock) may no longer end it; the local abandon does, and
    lets the pin of the derived proposal go."""
    repo, _ = proj
    legacy(engine, repo, tag=None)
    reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    take_over(engine, repo, no_push=True)
    assert git(repo, "for-each-ref", "--format=%(refname)", PIN_REFS) != ""
    with pytest.raises(ValueError, match="dct release abandon"):
        ledger.abandon_release(engine, SLUG, "1.1.0", "changed my mind", confirm=True)
    with pytest.raises(ReleaseRefused, match="executed by dct"):
        record_progress(engine, SLUG, "1.1.0", "completed")
    out = effects.abandon(engine, SLUG, "1.1.0", "changed my mind", confirm=True)
    assert out["release"]["status"] == "abandoned"
    assert git(repo, "for-each-ref", "--format=%(refname)", PIN_REFS) == ""
    assert git(repo, "tag", "--list", "v1.1.0") == ""            # no effect was performed


def test_the_explicit_choice_is_a_commit_id_never_another_git_argument(engine, proj):
    repo, _ = proj
    two_verified_candidates(engine, repo)
    before = db_state(engine)
    for bad in ("--all", "main", "HEAD~1"):
        with pytest.raises(ReleaseRefused, match="hex commit id"):
            reconstruct(engine, SLUG, "1.1.0", cwd=str(repo), release_commit=bad)
    assert db_state(engine) == before
