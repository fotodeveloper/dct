"""The read-only release proof layer (K-72 layer 1; K-68..K-71, K-50, K-53,
K-57): `dct release plan` / `dct clog candidates` on REAL throwaway git
repositories — the production workflow (dct.workflows.release), never a test
re-implementation of the rules."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import git, make_git_repo, run_cli

from dct.core.changelog import add_entry, delete_entry, release_entries
from dct.core.gitinfo import NotACheckoutError
from dct.core.projects import create_project, set_version
from dct.core.releases import abandon_release, set_version_files
from dct.db import metadata
from dct.workflows.release import (
    CANDIDATE_KEYS,
    PLAN_KEYS,
    build_plan,
    commit_candidates,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


# ── helpers ──────────────────────────────────────────────────────────────────


def commit(repo, message: str, path: str = "f.txt", content: str | None = None) -> str:
    """Commit a change to `path` with the VERBATIM `message`; return its SHA."""
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(content if content is not None else message.splitlines()[0] + "\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD")


def entry(engine, slug, text, *, branch=None, worktree=None, source=None) -> int:
    kwargs = {}
    if branch or worktree:
        kwargs = {"branch": branch, "worktree": worktree, "branch_source": source or "client"}
    return add_entry(engine, slug, "added", text, **kwargs)["id"]


def by_id(plan: dict) -> dict[int, dict]:
    rows = plan["entries"]["proven"] + plan["entries"]["unproven"]
    return {row["id"]: row for row in rows}


@pytest.fixture
def repo(engine, tmp_path):
    """proj-r at a fresh repo on main, tagged v1.0.0 (current_version 1.0.0)."""
    path = Path(make_git_repo(tmp_path / "proj-r"))
    git(path, "tag", "v1.0.0")
    create_project(engine, slug="proj-r", name="R", path=str(path), current_version="1.0.0")
    return path


def plan_of(engine, repo, **kw) -> dict:
    return build_plan(engine, "proj-r", cwd=str(repo), **kw)


# ── proof through merge modes ────────────────────────────────────────────────


def test_merge_commit_proves_with_merge_and_pr_provenance(engine, repo):
    e1 = entry(engine, "proj-r", "a", branch="feat/a", worktree=str(repo))
    git(repo, "checkout", "-q", "-b", "feat/a")
    proving = commit(repo, f"feat: a\n\nChangelog: {e1}\n", "a.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #7 from org/feat/a", "feat/a")
    merge = head(repo)

    plan = plan_of(engine, repo)
    row = by_id(plan)[e1]
    assert row["commits"] == [proving]
    assert row["merge_commit"] == merge and row["pr"] == 7
    assert plan["proof"] == {str(e1): {
        "commits": [proving], "merge_commit": merge, "pr": 7, "pr_source": "git",
        "source": "introducing_merge", "ambiguous": False, "associations": [
            {"merge_commit": merge, "pr": 7, "source": "introducing_merge", "certain": True}]}}
    assert plan["range"]["base_tag"] == "v1.0.0" and not plan["range"]["confirm_required"]


def test_rebase_merge_proves_on_the_first_parent_line(engine, repo):
    e1 = entry(engine, "proj-r", "a")
    e2 = entry(engine, "proj-r", "b")
    git(repo, "checkout", "-q", "-b", "feat/r")
    commit(repo, f"feat: a\n\nChangelog: {e1}\n", "a.txt")
    commit(repo, f"feat: b\n\nChangelog: {e2}\n", "b.txt")
    git(repo, "checkout", "-q", "main")
    commit(repo, "chore: main moved\n", "m.txt")
    git(repo, "checkout", "-q", "feat/r")
    git(repo, "rebase", "-q", "main")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "feat/r")

    rows = by_id(plan_of(engine, repo))
    assert rows[e1]["merge_commit"] is None and rows[e2]["merge_commit"] is None
    assert rows[e1]["commits"] and rows[e2]["commits"]
    assert all(git(repo, "merge-base", "--is-ancestor", rows[e]["commits"][0], "HEAD") == ""
               for e in (e1, e2))


def test_hosted_squash_keeping_messages_is_proven_by_column_zero_lines(engine, repo):
    e1, e2, e3 = (entry(engine, "proj-r", t) for t in "abc")
    squash = commit(repo, (
        f"feat: squashed (#12)\n\n* feat: part one\n\nChangelog: {e1}\n\n"
        f"* fix: part two\n\nChangelog: {e2}, {e3}\n\nCo-Authored-By: X <x@example.com>\n"))
    rows = by_id(plan_of(engine, repo))
    for e in (e1, e2, e3):
        # K-95 C4: the `(#12)` suffix may be an issue — listed as an
        # uncertain association, never the entry's PR number.
        assert rows[e]["commits"] == [squash] and rows[e]["pr"] is None
        assert rows[e]["merge_commit"] is None
        assert rows[e]["associations"] == [
            {"merge_commit": None, "pr": 12, "source": "subject_suffix", "certain": False}]


def test_quoted_bulleted_indented_and_inline_lines_never_prove(engine, repo):
    ids = [entry(engine, "proj-r", t) for t in "abcd"]
    commit(repo, f"docs: x\n\n> Changelog: {ids[0]}\n\n- Changelog: {ids[1]}\n")
    commit(repo, f"docs: y\n\n    Changelog: {ids[2]}\n\nThe format is Changelog: {ids[3]}.\n")
    plan = plan_of(engine, repo)
    assert plan["entries"]["proven"] == []
    assert {r["reason"] for r in plan["entries"]["unproven"]} <= {"no_trailer", "squash_suspected"}


def test_hosted_squash_title_only_proposes_one_group_for_the_branch(engine, repo):
    e1 = entry(engine, "proj-r", "t1", branch="feat/t", worktree=str(repo))
    e2 = entry(engine, "proj-r", "t2", branch="feat/t", worktree=str(repo))
    other = entry(engine, "proj-r", "o", branch="feat/other", worktree=str(repo))
    git(repo, "checkout", "-q", "-b", "feat/t")
    commit(repo, f"feat: t1\n\nChangelog: {e1}\n", "t.txt", "one\n")
    commit(repo, f"feat: t2\n\nChangelog: {e2}\n", "t.txt", "two\n")
    git(repo, "checkout", "-q", "main")
    # What a hosted "squash and merge" with the PR title only leaves behind.
    squash = commit(repo, "feat: t (#13)\n", "t.txt", "one\ntwo\n")

    plan = plan_of(engine, repo)
    rows = by_id(plan)
    assert rows[e1]["reason"] == rows[e2]["reason"] == "squash_suspected"
    assert rows[other]["reason"] == "no_trailer"  # no branch ref, nothing links it
    assert plan["proof"] == {}  # a proposal is never proof
    [group] = plan["attestation_groups"]
    assert group["branch"] == "feat/t" and group["entry_ids"] == sorted([e1, e2])
    assert group["squash_commits"] == [squash]
    assert group["signals"] == ["branch_commits_absent"]
    assert rows[e1]["group"] == rows[e2]["group"] == 0
    [report] = plan["squash_commits"]
    assert report["sha"] == squash and report["signals"] == ["pr_suffix"] and report["pr"] == 13


def test_local_merge_squash_is_detected_as_lost_trailers(engine, repo):
    e1 = entry(engine, "proj-r", "l", branch="feat/l", worktree=str(repo))
    git(repo, "checkout", "-q", "-b", "feat/l")
    commit(repo, f"feat: l\n\nChangelog: {e1}\nCo-Authored-By: X <x@example.com>\n", "l.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--squash", "feat/l")
    git(repo, "commit", "-q", "--no-edit")
    assert f"    Changelog: {e1}" in git(repo, "log", "-1", "--format=%B")  # git's indentation

    plan = plan_of(engine, repo)
    assert by_id(plan)[e1]["reason"] == "squash_suspected"
    [report] = plan["squash_commits"]
    assert report["lost_ids"] == [e1] and report["proves"] == []
    assert {"local_squash", "indented_trailers"} <= set(report["signals"])
    [group] = plan["attestation_groups"]
    assert group["branch"] == "feat/l" and "indented_trailer" in group["signals"]


# ── reverts (K-68) ───────────────────────────────────────────────────────────


def test_revert_removes_proof_and_revert_of_revert_restores_it(engine, repo):
    e1 = entry(engine, "proj-r", "r")
    proving = commit(repo, f"feat: r\n\nChangelog: {e1}\n", "r.txt")
    git(repo, "revert", "--no-edit", "HEAD")
    revert = head(repo)

    row = by_id(plan_of(engine, repo))[e1]
    assert row["reason"] == "reverted"
    assert row["reverted_by"] == [revert] and row["commits"] == [proving]

    git(repo, "revert", "--no-edit", "HEAD")  # git >= 2.43 says 'Reapply "…"'
    plan = plan_of(engine, repo)
    assert by_id(plan)[e1]["commits"] == [proving]
    statuses = {r["sha"]: r["effective"] for r in plan["reverts"]}
    assert statuses[revert] is False and statuses[head(repo)] is True

    git(repo, "revert", "--no-edit", "HEAD")  # third revert: removed again
    assert by_id(plan_of(engine, repo))[e1]["reason"] == "reverted"


def test_revert_of_a_commit_outside_the_range_is_reported_not_applied(engine, repo):
    git(repo, "tag", "-d", "v1.0.0")
    old = commit(repo, "feat: old\n", "o.txt")
    git(repo, "tag", "v1.0.0")
    e1 = entry(engine, "proj-r", "x")
    commit(repo, f"feat: x\n\nChangelog: {e1}\n", "x.txt")
    git(repo, "revert", "--no-edit", old)

    plan = plan_of(engine, repo)
    [report] = plan["reverts"]
    assert report["status"] == "outside_range" and report["target"] == old
    assert by_id(plan)[e1]["commits"]
    assert any("outside range" in w for w in plan["warnings"])


def test_prose_mentioning_a_revert_is_not_a_revert(engine, repo):
    e1 = entry(engine, "proj-r", "p")
    proving = commit(repo, f"feat: p\n\nChangelog: {e1}\n", "p.txt")
    commit(repo, f"docs: note\n\nThis reverts commit {proving}.\n", "n.txt")
    plan = plan_of(engine, repo)
    assert plan["reverts"] == [] and by_id(plan)[e1]["commits"] == [proving]


# ── range (K-71) ─────────────────────────────────────────────────────────────


def test_nearest_ancestor_tag_on_a_maintenance_branch_requires_confirmation(engine, repo):
    commit(repo, "feat: 1.1 work\n", "main.txt")
    git(repo, "tag", "v1.1.0")
    set_version(engine, "proj-r", "1.1.0")  # (a record would make a resume plan, K-95 C5)
    git(repo, "checkout", "-q", "-b", "maint/1.0", "v1.0.0")
    commit(repo, "fix: backport\n", "maint.txt")
    git(repo, "tag", "v1.0.1")
    e1 = entry(engine, "proj-r", "m")
    commit(repo, f"fix: more\n\nChangelog: {e1}\n", "maint.txt")

    plan = plan_of(engine, repo)
    rng = plan["range"]
    assert rng["base_tag"] == "v1.0.1" and rng["confirm_required"] is True
    assert rng["expected_tag"] == {"name": "v1.1.0", "exists": True, "ancestor": False}
    assert rng["commit_count"] == 1 and by_id(plan)[e1]["commits"]
    assert any("v1.0.1" in w and "confirm" in w for w in plan["warnings"])


def test_tag_of_the_current_version_is_the_base_among_older_tags(engine, repo):
    git(repo, "tag", "0.9")  # older, other spelling
    git(repo, "tag", "not-a-version")
    rng = plan_of(engine, repo)["range"]
    assert rng["base_tag"] == "v1.0.0" and rng["confirm_required"] is False
    assert rng["approximate"] is False


def test_no_ancestor_tag_falls_back_to_the_approximate_window(engine, repo):
    git(repo, "tag", "-d", "v1.0.0")
    e1 = entry(engine, "proj-r", "w")
    commit(repo, f"feat: w\n\nChangelog: {e1}\n", "w.txt")
    plan = plan_of(engine, repo)
    rng = plan["range"]
    assert rng["approximate"] is True and rng["since"] and rng["base_tag"] is None
    assert rng["expected_tag"] == {"name": "v1.0.0", "exists": False, "ancestor": False}
    assert by_id(plan)[e1]["commits"]
    assert any("approximate" in w for w in plan["warnings"])


def test_no_tag_and_nothing_unreleased_is_an_empty_range(engine, repo):
    git(repo, "tag", "-d", "v1.0.0")
    plan = plan_of(engine, repo)
    assert plan["range"]["revs"] is None and plan["range"]["commit_count"] == 0
    assert any("empty range" in w for w in plan["warnings"])


def test_entry_cited_only_below_the_base_tag_is_below_range(engine, repo):
    git(repo, "tag", "-d", "v1.0.0")
    e1 = entry(engine, "proj-r", "b")
    below = commit(repo, f"feat: b\n\nChangelog: {e1}\n", "b.txt")
    git(repo, "tag", "v1.0.0")
    commit(repo, "chore: later\n", "c.txt")
    row = by_id(plan_of(engine, repo))[e1]
    assert row["reason"] == "below_range" and row["commits"] == [below]


# ── citations and provenance ─────────────────────────────────────────────────


def test_foreign_citations_are_reported_never_proven(engine, repo):
    create_project(engine, slug="proj-o", name="O", path="/tmp/proj-o")
    foreign = add_entry(engine, "proj-o", "added", "theirs")["id"]
    released = entry(engine, "proj-r", "old")
    release_entries(engine, "proj-r", "1.0.1", entry_ids=[released])
    abandon_release(engine, "proj-r", "1.0.1", "test: no unfinished record", confirm=True)
    deleted = entry(engine, "proj-r", "gone")
    delete_entry(engine, "proj-r", deleted)
    mine = entry(engine, "proj-r", "mine")
    commit(repo, f"feat: x\n\nChangelog: {mine}, {foreign}, {released}, {deleted}, 99999\n")

    plan = plan_of(engine, repo)
    reasons = {c["id"]: (c["reason"], c["version"]) for c in plan["foreign_citations"]}
    assert reasons == {
        foreign: ("other_project", None), released: ("released", "1.0.1"),
        deleted: ("deleted", None), 99999: ("unknown", None),
    }
    assert set(plan["proof"]) == {str(mine)}


def test_uncertain_provenance_is_flagged_but_classified_by_proof(engine, repo):
    server = entry(engine, "proj-r", "s", branch="main", worktree="/tmp/elsewhere", source="server")
    bare = entry(engine, "proj-r", "n")
    client = entry(engine, "proj-r", "c", branch="main", worktree=str(repo))
    commit(repo, f"feat: s\n\nChangelog: {server}, {client}\n")
    rows = by_id(plan_of(engine, repo))
    assert rows[server]["uncertain"] is True and rows[server]["commits"]
    assert rows[bare]["uncertain"] is True and rows[bare]["reason"] == "no_trailer"
    assert rows[client]["uncertain"] is False and rows[client]["commits"]


# ── index (K-50, read part) and the version-file contract (K-73) ─────────────


def test_index_state_reports_staged_unstaged_and_release_artifacts(engine, repo):
    commit(repo, "chore: files\n", "pyproject.toml", "version = '1.0.0'\n")
    commit(repo, "chore: log\n", "changelog.md", "# Changelog\n")
    plan = plan_of(engine, repo)
    assert plan["index"]["clean"] is True
    assert plan["version_files"]["version_files"] is None  # learned at the first release

    set_version_files(engine, "proj-r", ["pyproject.toml"], confirm=True)
    (repo / "pyproject.toml").write_text("version = 'foreign'\n")
    git(repo, "add", "pyproject.toml")
    (repo / "README").write_text("changed\n")
    (repo / "new.txt").write_text("u\n")
    index = plan_of(engine, repo)["index"]
    assert index["clean"] is False
    assert index["staged"] == ["pyproject.toml"] and index["unstaged"] == ["README"]
    assert index["untracked"] == ["new.txt"]
    artifacts = {a["path"]: a for a in index["release_artifacts"]}
    assert artifacts["pyproject.toml"]["role"] == "version_file"
    assert artifacts["pyproject.toml"]["staged"] is True
    assert artifacts["changelog.md"] == {"path": "changelog.md", "role": "changelog",
                                         "staged": False, "unstaged": False,
                                         "untracked": False, "conflicted": False}


def test_project_without_git_is_unproven_no_git(engine, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    create_project(engine, slug="proj-n", name="N", path=str(plain), current_version="1.0")
    e1 = add_entry(engine, "proj-n", "added", "x")["id"]
    plan = build_plan(engine, "proj-n", cwd=str(plain), bump_type="minor")
    assert plan["checkout"]["git"] is False and plan["range"] is None
    assert plan["version"]["target"] == "1.1"
    assert [(r["id"], r["reason"]) for r in plan["entries"]["unproven"]] == [(e1, "no_git")]


def test_plan_from_another_repository_is_refused(engine, repo, tmp_path):
    other = make_git_repo(tmp_path / "other")
    with pytest.raises(NotACheckoutError):
        build_plan(engine, "proj-r", cwd=other)


def test_unfinished_record_makes_a_resume_plan(engine, repo):
    e1 = entry(engine, "proj-r", "a")
    release_entries(engine, "proj-r", "1.1.0", entry_ids=[e1], candidate_sha=head(repo),
                    checkout_root=str(repo))
    plan = plan_of(engine, repo, version="1.2.0")
    assert plan["kind"] == "resume"
    assert plan["unfinished_release"]["version"] == "1.1.0"
    assert plan["checkout"]["matches_record"] == {"candidate_sha": True, "checkout_root": True}
    assert any("resume or abandon" in w for w in plan["warnings"])


# ── no side effects ──────────────────────────────────────────────────────────


def _db_snapshot(engine) -> dict:
    with engine.begin() as conn:
        return {
            table.name: sorted(map(repr, conn.execute(sa.select(table)).all()))
            for table in metadata.sorted_tables
        }


def _repo_snapshot(repo: Path) -> dict:
    gitdir = repo / ".git"
    index = gitdir / "index"
    files = {}
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            path = Path(root) / name
            files[str(path.relative_to(repo))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": (gitdir / "HEAD").read_text(),
        "index": (hashlib.sha256(index.read_bytes()).hexdigest(), index.stat().st_mtime_ns),
        # The snapshot itself must not refresh the index (a plain status does).
        "status": git(repo, "--no-optional-locks", "status", "--porcelain=v1"),
        "files": files,
        "gitdir": sorted(p.name for p in gitdir.iterdir()),
    }


def test_plan_and_candidates_have_no_side_effects(cli_engine, repo, monkeypatch, capsys):
    engine = cli_engine
    e1 = entry(engine, "proj-r", "a", branch="main", worktree=str(repo))
    commit(repo, f"feat: a\n\nChangelog: {e1}\n", "a.txt")
    (repo / "a.txt").write_text("dirty\n")  # an unstaged change
    (repo / "staged.txt").write_text("s\n")
    git(repo, "add", "staged.txt")
    os.utime(repo / "a.txt", (1, 1))  # a stat change a plain `git status` would refresh
    before_db, before_repo = _db_snapshot(engine), _repo_snapshot(repo)
    monkeypatch.chdir(repo)
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-r", "--minor", "--json"])
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-r"])
    run_cli(monkeypatch, ["clog", "candidates", "-P", "proj-r", "--json"])
    run_cli(monkeypatch, ["clog", "candidates", "-P", "proj-r"])
    capsys.readouterr()
    assert _db_snapshot(engine) == before_db
    assert _repo_snapshot(repo) == before_repo


# ── /commit candidates (K-53) ────────────────────────────────────────────────


@pytest.fixture
def worktree_setup(engine, repo, tmp_path):
    """Project registered at the main checkout; a linked worktree on feat/x."""
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    return repo, wt.resolve()


def test_candidates_main_server_worktree_client(engine, worktree_setup):
    main, wt = worktree_setup
    # The MCP server ran in the MAIN checkout: it recorded 'main' + the main path.
    server_match = entry(engine, "proj-r", "add widget parser", branch="main",
                         worktree=str(main), source="server")
    server_other = entry(engine, "proj-r", "rewrite billing", branch="main",
                         worktree=str(main), source="server")
    confirmed = entry(engine, "proj-r", "x", branch="feat/x", worktree=str(wt))
    elsewhere = entry(engine, "proj-r", "main work", branch="main", worktree=str(main))
    wrong_branch = entry(engine, "proj-r", "w", branch="feat/old", worktree=str(wt))
    proven = entry(engine, "proj-r", "p", branch="feat/x", worktree=str(wt))
    commit(wt, f"feat: p\n\nChangelog: {proven}\n", "p.txt")
    (wt / "widget.py").write_text("parser\n")
    git(wt, "add", "widget.py")

    result = commit_candidates(engine, "proj-r", cwd=str(wt))
    ids = {key: [e["id"] for e in result[key]] for key in ("confirmed", "uncertain", "other_branch")}
    assert ids["confirmed"] == [confirmed]
    # Both server-detected rows are OFFERED as uncertain (the worktree filter
    # never removes them) — the matching one is never auto-confirmed, the
    # one describing another change is left to the agent's judgement.
    assert ids["uncertain"] == [server_match, server_other]
    assert all(e["uncertain_reason"] == "server_detected" for e in result["uncertain"])
    assert ids["other_branch"] == [wrong_branch]
    assert all(elsewhere not in listed for listed in ids.values())
    assert result["already_proven"] == [proven]
    assert result["staged_files"] == ["widget.py"]
    assert result["checkout"]["branch"] == "feat/x" and result["checkout"]["detached"] is False


def test_candidates_on_a_detached_head_confirm_nothing(engine, worktree_setup):
    main, wt = worktree_setup
    client = entry(engine, "proj-r", "c", branch="feat/x", worktree=str(wt))
    server = entry(engine, "proj-r", "s", branch="main", worktree=str(main), source="server")
    other = entry(engine, "proj-r", "o", branch="main", worktree=str(main))
    git(wt, "checkout", "-q", "--detach")
    result = commit_candidates(engine, "proj-r", cwd=str(wt))
    assert result["checkout"]["detached"] is True and result["confirmed"] == []
    reasons = {e["id"]: e["uncertain_reason"] for e in result["uncertain"]}
    assert reasons == {client: "detached_head", server: "server_detected"}
    assert other not in reasons
    assert any("detached" in w for w in result["warnings"])


# ── JSON schema stability ────────────────────────────────────────────────────

PINNED_PLAN_KEYS = [
    "schema_version", "project", "kind", "version", "checkout", "executor",
    "unfinished_release", "range", "entries", "foreign_citations", "reverts",
    "squash_commits", "attestation_groups", "proof", "index", "version_files",
    "changelog_file", "warnings", "resume", "snapshot", "snapshot_digest",
]
PINNED_CANDIDATE_KEYS = [
    "schema_version", "project", "checkout", "range", "confirmed", "uncertain",
    "other_branch", "already_proven", "staged_files", "warnings",
]


def test_json_documents_keep_their_top_level_keys(cli_engine, repo, monkeypatch, capsys):
    assert list(PLAN_KEYS) == PINNED_PLAN_KEYS
    assert list(CANDIDATE_KEYS) == PINNED_CANDIDATE_KEYS
    monkeypatch.chdir(repo)
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-r", "--patch", "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert list(plan) == PINNED_PLAN_KEYS and plan["schema_version"] == 4
    assert set(plan["executor"]) == {"project", "record"}
    assert plan["version"] == {"current": "1.0.0", "target": "1.0.1", "bump": "patch"}
    assert set(plan["entries"]) == {"proven", "unproven"}
    assert set(plan["range"]) == {
        "candidate_sha", "base_tag", "base_sha", "base_version", "base_distance",
        "alternatives", "expected_tag", "confirm_required", "approximate", "since", "revs",
        "display", "below_scan", "commit_count"}
    assert set(plan["snapshot"]) == {
        "project", "version", "current_version", "git", "repo_root", "checkout_root",
        "project_root", "candidate_sha", "range", "selected_ids", "proof", "entries"}
    assert len(plan["snapshot_digest"]) == 64 and plan["resume"] is None
    run_cli(monkeypatch, ["clog", "candidates", "-P", "proj-r", "--json"])
    candidates = json.loads(capsys.readouterr().out)
    assert list(candidates) == PINNED_CANDIDATE_KEYS and candidates["schema_version"] == 4


def test_release_plan_cli_refuses_outside_a_checkout(cli_engine, repo, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(make_git_repo(tmp_path / "foreign"))
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["release", "plan", "-P", "proj-r"])
    assert exc.value.code == 1
    assert "not a checkout" in capsys.readouterr().err


def test_release_status_is_an_alias_of_clog_release_status(cli_engine, repo, monkeypatch, capsys):
    release_entries(cli_engine, "proj-r", "1.1.0", entry_ids=[])
    run_cli(monkeypatch, ["release", "status", "-P", "proj-r", "--version", "1.1.0"])
    alias = capsys.readouterr().out
    run_cli(monkeypatch, ["clog", "release-status", "-P", "proj-r", "--version", "1.1.0"])
    assert alias == capsys.readouterr().out and "Release 1.1.0: stamped" in alias
