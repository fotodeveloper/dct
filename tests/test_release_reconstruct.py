"""K-86 / K-96 D8 / J8 — reconstruction of a legacy release (one made before
release records existed), effects side.

- The AUTHORITATIVE version spelling comes from the shared legacy resolver
  core uses (changelog.resolve_version_spelling) BEFORE any artifact is
  verified; the requested V is only reported; every comparison is
  same_version; files, section, tag and record refer to that ONE resolved
  version. A real artifact contradiction is refused — never masked by
  picking a string. Nothing is re-stamped, no history rewritten.
- Version files are checked by their version FIELD / changed lines (the D9
  contract of prepare), not by a substring search (J8).
- D8: release commits found on refs are CANDIDATES — each is verified
  (single parent, the paths it changed, the section, the version files); a
  tag of V is required for a 'completed' reconstruction, a verified release
  commit without a tag is reconstructed as 'committed'.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import git
from test_release_effects import (
    HISTORY,
    SLUG,
    commit,
    make_release_repo,
    proven,
    released_section,
)

from dct.core.changelog import list_entries
from dct.core.releases import get_release, set_version_files
from dct.db import changelog_entries, projects, releases
from dct.workflows.release.effects import ReleaseRefused, reconstruct, resume_release

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The fixture builds commits."""


def db_state(engine) -> tuple:
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases))


def hand_release(engine, repo, a, *, stored: str, label: str, field: str, tag: str | None,
                 subject: str | None = None) -> tuple[str, str]:
    """Release `a` BY HAND, the legacy way: the entry stamped `stored`,
    current_version `stored`, a section `## [label]`, pyproject's version
    field `field`, a commit `release: v<label>` and (optionally) `tag`.
    Returns (candidate, release commit)."""
    candidate = git(repo, "rev-parse", "HEAD")
    when = datetime(2026, 2, 1, 10, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version=stored, released_at=when))
        conn.execute(projects.update().values(current_version=stored))
    (repo / "changelog.md").write_text(
        "# Changelog\n\n" + released_section("2026-02-01", label) + "\n" + HISTORY[13:])
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{field}"\n')
    git(repo, "commit", "-qam", subject or f"release: v{label}")
    if tag:
        git(repo, "tag", tag)
    return candidate, git(repo, "rev-parse", "HEAD")


@pytest.fixture
def proj(engine, tmp_path):
    repo, _ = make_release_repo(engine, tmp_path)
    return repo


def test_requesting_1_1_0_reconstructs_the_stored_1_1(engine, proj):
    """K-86: entries, current_version, section, pyproject and tag all say
    1.1; the request says 1.1.0 — the record is 1.1, nothing re-stamped."""
    repo = proj
    a = proven(engine, repo, "feature a")
    candidate, release_commit = hand_release(engine, repo, a, stored="1.1", label="1.1",
                                             field="1.1", tag="v1.1")
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    evidence = preview["details"]["evidence"]
    assert preview["version"] == "1.1" and evidence["version"] == "1.1"
    assert evidence["requested_version"] == "1.1.0"
    assert evidence["spellings"] == {"1.1": [a]}
    assert (evidence["candidate_sha"], evidence["release_commit_sha"], evidence["tag"]) == (
        candidate, release_commit, "v1.1")
    assert evidence["manifest"]["version_files"][0]["path"] == "pyproject.toml"
    entries_before = list_entries(engine, SLUG)
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    record = out["release"]
    assert (record["version"], record["status"], record["mode"]) == (
        "1.1", "completed", "reconstructed")
    assert record["entry_ids"] == [a] and record["release_commit_sha"] == release_commit
    assert list_entries(engine, SLUG) == entries_before   # nothing re-stamped or rewritten
    assert get_release(engine, SLUG, "1.1.0")["version"] == "1.1"


def test_equivalent_spellings_across_the_artifacts_resolve_to_the_record_spelling(
        engine, proj):
    """Section `## [1.1.0]`, commit `release: v1.1.0`, pyproject 1.1.0, tag
    v1.1.0 — while the database stored 1.1: the SAME release under the
    version contract; the record keeps the database's spelling."""
    repo = proj
    a = proven(engine, repo, "feature a")
    hand_release(engine, repo, a, stored="1.1", label="1.1.0", field="1.1.0", tag="v1.1.0")
    out = reconstruct(engine, SLUG, "1.1", confirm=True, cwd=str(repo))
    assert out["release"]["version"] == "1.1"
    assert out["details"]["evidence"]["tag"] == "v1.1.0"


@pytest.mark.parametrize("contradiction", [
    "version_field",          # pyproject's version FIELD is another version
    "two_sections",           # `## [1.1]` AND `## [1.1.0]` in the changelog
    "tags_disagree",          # v1.1 and v1.1.0 on DIFFERENT commits
    "substring_only",         # J8: "1.1" appears in the file, the field is 9.9
])
def test_a_real_artifact_contradiction_is_refused_with_the_database_unchanged(
        engine, proj, contradiction):
    repo = proj
    a = proven(engine, repo, "feature a")
    field = {"version_field": "1.2", "substring_only": "9.9"}.get(contradiction, "1.1")
    candidate, _ = hand_release(engine, repo, a, stored="1.1", label="1.1", field=field,
                                tag="v1.1")
    if contradiction == "substring_only":
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndescription = "since 1.1.0"\nversion = "9.9"\n')
        git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
        git(repo, "tag", "-f", "v1.1")
    elif contradiction == "two_sections":
        text = (repo / "changelog.md").read_text()
        (repo / "changelog.md").write_text(
            text.replace("## [1.1]", released_section("2026-02-01", "1.1.0") + "\n## [1.1]"))
        git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
        git(repo, "tag", "-f", "v1.1")
    elif contradiction == "tags_disagree":
        git(repo, "tag", "v1.1.0", candidate)
    before = db_state(engine)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused):
            reconstruct(engine, SLUG, "1.1.0", confirm=confirm, cwd=str(repo))
    assert db_state(engine) == before


def test_a_commit_titled_like_a_release_on_another_ref_is_only_a_candidate(engine, proj):
    """D8: a second `release: v1.1.0` commit (another branch, changing a
    file outside the contract) fails verification — the verified, tagged
    release commit is the evidence; the other one is reported, not used."""
    repo = proj
    a = proven(engine, repo, "feature a")
    candidate, release_commit = hand_release(engine, repo, a, stored="1.1.0", label="1.1.0",
                                             field="1.1.0", tag="v1.1.0")
    git(repo, "checkout", "-q", "-b", "other", candidate)
    stray = commit(repo, "release: v1.1.0\n", "g.txt")
    git(repo, "checkout", "-q", "main")
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert out["release"]["release_commit_sha"] == release_commit
    candidates = {c["commit"]: c for c in out["details"]["evidence"]["candidates"]}
    assert candidates[release_commit]["verified"] is True
    assert candidates[stray]["verified"] is False and candidates[stray]["reason"]


def test_two_verified_release_commits_without_a_tag_are_ambiguous(engine, proj):
    repo = proj
    a = proven(engine, repo, "feature a")
    candidate, release_commit = hand_release(engine, repo, a, stored="1.1.0", label="1.1.0",
                                             field="1.1.0", tag=None)
    git(repo, "checkout", "-q", "-b", "other", candidate)
    git(repo, "cherry-pick", "--keep-redundant-commits", release_commit)
    git(repo, "commit", "-q", "--amend", "-m", "release: v1.1.0\n\nSame release, other ref.")
    git(repo, "checkout", "-q", "main")
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="ambiguous"):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert db_state(engine) == before


def test_a_verified_release_commit_without_a_tag_reconstructs_as_committed(engine, proj):
    """D8: the tag is required for the 'completed' stage only — the evidence
    ends at the release commit, so the record does too; dct's resume does
    not continue it (no consent: nobody approved a push target)."""
    repo = proj
    a = proven(engine, repo, "feature a")
    candidate, release_commit = hand_release(engine, repo, a, stored="1.1.0", label="1.1.0",
                                             field="1.1.0", tag=None)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    assert preview["details"]["evidence"]["status"] == "committed"
    assert preview["details"]["evidence"]["tag"] is None
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))["release"]
    assert (record["status"], record["release_commit_sha"], record["candidate_sha"]) == (
        "committed", release_commit, candidate)
    with pytest.raises(ReleaseRefused, match="stamped outside"):
        resume_release(engine, SLUG, cwd=str(repo))
    assert get_release(engine, SLUG, "1.1.0")["status"] == "committed"


def test_a_tag_on_a_commit_that_is_not_a_verifiable_release_is_refused(engine, proj):
    repo = proj
    a = proven(engine, repo, "feature a")
    candidate, _ = hand_release(engine, repo, a, stored="1.1.0", label="1.1.0", field="1.1.0",
                                tag=None)
    git(repo, "tag", "v1.1.0", candidate)        # the tag points at the candidate
    before = db_state(engine)
    with pytest.raises(ReleaseRefused, match="tag"):
        reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(repo))
    assert db_state(engine) == before


def test_a_contract_mismatch_names_the_candidate_reason(engine, proj):
    repo = proj
    a = proven(engine, repo, "feature a")
    hand_release(engine, repo, a, stored="1.1.0", label="1.1.0", field="1.1.0", tag="v1.1.0")
    set_version_files(engine, SLUG, ["pyproject.toml", "VERSION"], confirm=True)
    with pytest.raises(ReleaseRefused, match="contract") as exc:
        reconstruct(engine, SLUG, "1.1.0", cwd=str(repo))
    assert exc.value.details["candidates"]


def test_an_overlong_final_spelling_is_refused_by_the_effects_layer_too(engine, proj):
    """K-87 through the effects layer: the resolved FINAL spelling must fit
    the schema (ensure_version_fits) — the preview and --confirm refuse
    before reading any artifact; the full database is unchanged."""
    if engine.dialect.name == "postgresql":
        pytest.skip("PostgreSQL enforces VARCHAR(50): no overlong legacy row can exist")
    repo = proj
    a = proven(engine, repo, "feature a")
    long_version = "1.2." + "9" * 60
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version=long_version))
        conn.execute(projects.update().values(current_version=long_version))
    before = db_state(engine)
    for confirm in (False, True):
        with pytest.raises(ReleaseRefused, match="too long"):
            reconstruct(engine, SLUG, long_version, confirm=confirm, cwd=str(repo))
    assert db_state(engine) == before
