"""Round 3 of the release proof layer (R7b): merge-revert mainlines (K-82),
the graph-nearest range base (K-83), the proving commit's own merge
provenance (K-89) and the proof-layer behaviours of K-95 (C1-C5) — on REAL
throwaway git repositories through the production build_plan /
commit_candidates."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core.changelog import add_entry, release_entries
from dct.core.gitinfo import CheckoutVerificationError
from dct.core.projects import create_project, set_version
from dct.workflows.release import build_plan, commit_candidates

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def commit(repo, message: str, path: str = "f.txt", content: str | None = None) -> str:
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(content if content is not None else message.splitlines()[0] + "\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD")


def entry(engine, text, **kwargs) -> int:
    return add_entry(engine, "proj-r", "added", text, **kwargs)["id"]


def rows(plan: dict) -> dict[int, dict]:
    return {r["id"]: r for r in plan["entries"]["proven"] + plan["entries"]["unproven"]}


@pytest.fixture
def repo(engine, tmp_path):
    path = Path(make_git_repo(tmp_path / "proj-r"))
    git(path, "tag", "v1.0.0")
    create_project(engine, slug="proj-r", name="R", path=str(path), current_version="1.0.0")
    return path


def plan_of(engine, repo, **kw) -> dict:
    return build_plan(engine, "proj-r", cwd=str(repo), **kw)


# ── K-82: the mainline of a merge revert ─────────────────────────────────────


@pytest.fixture
def merged(engine, repo):
    """main: b (proves e2); feat/s: a (proves e1), a3 (proves e3); M merges
    feat/s into main — parents [b, a3]."""
    e1, e2, e3 = entry(engine, "side"), entry(engine, "main"), entry(engine, "side two")
    git(repo, "checkout", "-q", "-b", "feat/s")
    a = commit(repo, f"feat: side\n\nChangelog: {e1}\n", "s.txt")
    a3 = commit(repo, f"feat: side two\n\nChangelog: {e3}\n", "t.txt")
    git(repo, "checkout", "-q", "main")
    b = commit(repo, f"feat: main\n\nChangelog: {e2}\n", "m.txt")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'feat/s'", "feat/s")
    return {"e1": e1, "e2": e2, "e3": e3, "a": a, "a3": a3, "b": b, "M": head(repo)}


def test_revert_m1_neutralizes_the_side_branch(engine, repo, merged):
    git(repo, "revert", "--no-edit", "-m", "1", merged["M"])
    plan = plan_of(engine, repo)
    r = rows(plan)
    assert r[merged["e1"]]["reason"] == r[merged["e3"]]["reason"] == "reverted"
    assert r[merged["e2"]]["commits"] == [merged["b"]]
    [report] = plan["reverts"]
    assert report["status"] == "in_range" and report["mainline"] == merged["b"]


def test_revert_m2_neutralizes_the_mainline_side_not_parent_one(engine, repo, merged):
    """`git revert -m 2 M` keeps the side branch and removes what the first
    parent brought — the proof must follow THAT parent, not always parent 1."""
    git(repo, "revert", "--no-edit", "-m", "2", merged["M"])
    plan = plan_of(engine, repo)
    r = rows(plan)
    assert r[merged["e2"]]["reason"] == "reverted"
    assert r[merged["e1"]]["commits"] == [merged["a"]]
    assert r[merged["e3"]]["commits"] == [merged["a3"]]
    [report] = plan["reverts"]
    assert report["mainline"] == merged["a3"]
    assert set(report["affected"]) == {merged["b"], merged["M"]}


def test_revert_of_a_merge_revert_restores_the_side_branch(engine, repo, merged):
    git(repo, "revert", "--no-edit", "-m", "1", merged["M"])
    first = head(repo)
    git(repo, "revert", "--no-edit", first)
    plan = plan_of(engine, repo)
    r = rows(plan)
    assert r[merged["e1"]]["commits"] == [merged["a"]]
    assert r[merged["e3"]]["commits"] == [merged["a3"]]
    effective = {x["sha"]: x["effective"] for x in plan["reverts"]}
    assert effective[first] is False and effective[head(repo)] is True


def test_partial_merge_revert_is_flagged_never_guessed(engine, repo, merged):
    """A revert whose conflict was resolved differently from a full revert
    (here: one file of the side branch kept) proves nothing automatically
    for the entries it may touch — flagged for the owner's confirmation."""
    git(repo, "revert", "--no-commit", "-m", "1", merged["M"])
    git(repo, "checkout", merged["M"], "--", "t.txt")
    git(repo, "commit", "-q", "--no-edit")
    plan = plan_of(engine, repo)
    r = rows(plan)
    assert r[merged["e1"]]["reason"] == r[merged["e3"]]["reason"] == "revert_unconfirmed"
    assert r[merged["e1"]]["reverted_by"] == [head(repo)]
    [report] = plan["reverts"]
    assert report["status"] == "unconfirmed" and report["reason"] == "partial"
    assert plan["proof"].keys() == {str(merged["e2"])}
    assert any("confirm" in w for w in plan["warnings"])


@pytest.mark.parametrize("tail, reason", [
    pytest.param(".", "no_mainline", id="edited-no-mainline"),
    pytest.param(", reversing\nchanges made to {other}.", "mainline_not_a_parent",
                 id="mainline-not-a-parent"),
])
def test_merge_revert_without_a_usable_mainline_is_flagged(engine, repo, merged, tail, reason):
    git(repo, "revert", "--no-commit", "-m", "1", merged["M"])
    other = git(repo, "rev-parse", "v1.0.0")
    message = (f"Revert \"Merge branch 'feat/s'\"\n\nThis reverts commit {merged['M']}"
               + tail.format(other=other) + "\n")
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    plan = plan_of(engine, repo)
    [report] = plan["reverts"]
    assert report["status"] == "unconfirmed" and report["reason"] == reason
    r = rows(plan)
    # Which side was reverted is unknown: every entry the merge may touch
    # loses its automatic proof (both sides), none is guessed proven.
    for key in ("e1", "e2", "e3"):
        assert r[merged[key]]["reason"] == "revert_unconfirmed"
    assert plan["proof"] == {}


def test_a_plain_revert_still_neutralizes_its_target(engine, repo):
    e1 = entry(engine, "r")
    proving = commit(repo, f"feat: r\n\nChangelog: {e1}\n", "r.txt")
    git(repo, "revert", "--no-edit", "HEAD")
    plan = plan_of(engine, repo)
    assert rows(plan)[e1]["reason"] == "reverted"
    [report] = plan["reverts"]
    assert report["status"] == "in_range" and report["mainline"] is None
    assert report["affected"] == [proving]


# ── K-83: the graph-nearest ancestor tag ─────────────────────────────────────


def test_nearest_base_is_the_graph_distance_not_count_nor_highest(engine, repo):
    """M = merge(m3, s1): v1.0.0 is 2 parent edges away (M → s1 → T0),
    v1.1.0 is 3 (M → m3 → m2 → m1). `rev-list --count TAG..M` would pick
    v1.1.0 (4 < 5) and so would the highest version — the graph distance
    picks v1.0.0 and, as it is not v<current>, asks for confirmation."""
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "feat: side\n", "s.txt")
    git(repo, "checkout", "-q", "main")
    commit(repo, "feat: m1\n", "m.txt")
    git(repo, "tag", "v1.1.0")
    set_version(engine, "proj-r", "1.1.0")
    commit(repo, "feat: m2\n", "m.txt")
    commit(repo, "feat: m3\n", "m.txt")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
    assert git(repo, "rev-list", "--count", "v1.1.0..HEAD") < \
        git(repo, "rev-list", "--count", "v1.0.0..HEAD")

    rng = plan_of(engine, repo)["range"]
    assert rng["base_tag"] == "v1.0.0" and rng["base_distance"] == 2
    assert rng["confirm_required"] is True
    assert rng["expected_tag"] == {"name": "v1.1.0", "exists": True, "ancestor": True}
    assert rng["alternatives"] == []


def test_annotated_tag_counts_at_its_commit(engine, repo):
    commit(repo, "feat: x\n", "x.txt")
    git(repo, "tag", "-a", "v1.2.0", "-m", "release 1.2.0")
    tagged = head(repo)
    set_version(engine, "proj-r", "1.2.0")
    commit(repo, "feat: y\n", "y.txt")
    rng = plan_of(engine, repo)["range"]
    assert rng["base_tag"] == "v1.2.0" and rng["base_sha"] == tagged
    assert rng["base_distance"] == 1 and rng["confirm_required"] is False


def test_several_names_on_one_commit_pick_the_expected_and_show_the_rest(engine, repo):
    git(repo, "tag", "-a", "1.0", "-m", "same commit, other spelling")
    git(repo, "tag", "release-candidate")  # not a version: ignored
    commit(repo, "feat: z\n", "z.txt")
    rng = plan_of(engine, repo)["range"]
    assert rng["base_tag"] == "v1.0.0" and rng["confirm_required"] is False
    assert [(a["name"], a["distance"]) for a in rng["alternatives"]] == [("1.0", 1)]


def test_equal_distance_tags_on_different_commits_need_confirmation(engine, repo):
    git(repo, "checkout", "-q", "-b", "left")
    commit(repo, "feat: l\n", "l.txt")
    git(repo, "tag", "v2.0.0")
    git(repo, "checkout", "-q", "-b", "right", "v1.0.0")
    commit(repo, "feat: r\n", "r.txt")
    git(repo, "tag", "v1.9.0")
    git(repo, "checkout", "-q", "left")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'right'", "right")
    rng = plan_of(engine, repo)["range"]
    # Tie rule: not the expected tag → v-prefixed → the lowest name.
    assert rng["base_tag"] == "v1.9.0" and rng["base_distance"] == 1
    assert [a["name"] for a in rng["alternatives"]] == ["v2.0.0"]
    assert rng["confirm_required"] is True


# ── K-89: the proving commit's own merge provenance ──────────────────────────


def test_proving_merge_commit_records_its_own_merge_and_pr(engine, repo):
    e1 = entry(engine, "own")
    git(repo, "checkout", "-q", "-b", "feat/o")
    commit(repo, "feat: work\n", "o.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m",
        f"Merge pull request #7 from org/feat/o\n\nChangelog: {e1}\n", "feat/o")
    merge = head(repo)
    plan = plan_of(engine, repo)
    proof = plan["proof"][str(e1)]
    assert proof["commits"] == [merge]
    assert proof["merge_commit"] == merge and proof["pr"] == 7
    assert proof["source"] == "own_merge" and proof["ambiguous"] is False


def test_merge_without_pr_and_a_later_numbered_commit_never_pair(engine, repo):
    """The first merge has no PR; a later proving commit's subject ends
    `(#9)` — maybe an issue. merge_commit and pr come from ONE recognition:
    the merge stays, no PR number is borrowed from another commit."""
    e1 = entry(engine, "pair")
    git(repo, "checkout", "-q", "-b", "feat/p")
    commit(repo, f"feat: p\n\nChangelog: {e1}\n", "p.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'feat/p'", "feat/p")
    merge = head(repo)
    commit(repo, f"fix: follow-up (#9)\n\nChangelog: {e1}\n", "q.txt")
    proof = plan_of(engine, repo)["proof"][str(e1)]
    assert proof["merge_commit"] == merge and proof["pr"] is None
    suffix = [a for a in proof["associations"] if a["source"] == "subject_suffix"]
    assert suffix == [{"merge_commit": None, "pr": 9, "source": "subject_suffix",
                       "certain": False}]


def test_two_different_pr_merges_are_ambiguous_not_picked(engine, repo):
    e1 = entry(engine, "twice")
    for n, branch in ((3, "feat/a"), (4, "feat/b")):
        git(repo, "checkout", "-q", "-b", branch, "main")
        commit(repo, f"feat: {branch}\n\nChangelog: {e1}\n", f"{n}.txt")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "-m", f"Merge pull request #{n} from org/{branch}",
            branch)
    proof = plan_of(engine, repo)["proof"][str(e1)]
    assert proof["ambiguous"] is True
    assert proof["merge_commit"] is None and proof["pr"] is None
    assert sorted(a["pr"] for a in proof["associations"]) == [3, 4]


def test_squash_suffix_is_never_a_pr_number(engine, repo):
    """K-95 C4: `(#12)` may be an issue reference — it feeds proposals only."""
    e1 = entry(engine, "s")
    commit(repo, f"feat: squashed (#12)\n\nChangelog: {e1}\n")
    plan = plan_of(engine, repo)
    assert plan["proof"][str(e1)]["pr"] is None
    assert rows(plan)[e1]["pr"] is None


# ── K-95 C1-C5 ───────────────────────────────────────────────────────────────


def test_c1_project_without_a_path_is_not_no_git(engine, tmp_path):
    create_project(engine, slug="proj-p", name="P", current_version="1.0")
    e1 = add_entry(engine, "proj-p", "added", "x")["id"]
    plan = build_plan(engine, "proj-p", cwd=str(tmp_path), bump_type="minor")
    assert plan["checkout"] == {"git": False, "reason": "no_path", "project_root": None}
    assert [(r["id"], r["reason"]) for r in plan["entries"]["unproven"]] == [(e1, "no_path")]


def test_c1_detection_error_refuses_instead_of_no_git(engine, tmp_path):
    create_project(engine, slug="proj-m", name="M", path=str(tmp_path / "missing"),
                   current_version="1.0")
    add_entry(engine, "proj-m", "added", "x")
    with pytest.raises(CheckoutVerificationError):
        build_plan(engine, "proj-m", cwd=str(tmp_path), bump_type="minor")


def test_c2_client_entry_without_worktree_is_a_candidate_not_confirmed(engine, repo):
    loose = entry(engine, "loose", branch="main", branch_source="client")
    here = entry(engine, "here", branch="main", worktree=str(repo), branch_source="client")
    result = commit_candidates(engine, "proj-r", cwd=str(repo))
    assert [e["id"] for e in result["confirmed"]] == [here]
    uncertain = {e["id"]: e["uncertain_reason"] for e in result["uncertain"]}
    assert uncertain == {loose: "no_worktree"}


def test_c3_below_range_scan_states_its_bound(engine, repo):
    e1 = entry(engine, "b")
    commit(repo, "chore: later\n", "c.txt")
    plan = plan_of(engine, repo)
    scan = plan["range"]["below_scan"]
    assert scan["until"] == plan["range"]["base_sha"] and scan["since"]
    assert scan["bounded"] is True
    assert rows(plan)[e1]["reason"] == "no_trailer"


def test_c5_resume_plan_shows_the_record_not_todays_head(engine, repo):
    e1 = entry(engine, "a")
    proving = commit(repo, f"feat: a\n\nChangelog: {e1}\n", "a.txt")
    candidate = head(repo)
    release_entries(engine, "proj-r", "1.1.0", entry_ids=[e1], candidate_sha=candidate,
                    checkout_root=str(repo), release_mode="git",
                    proof={str(e1): {"commits": [proving], "merge_commit": None, "pr": None}})
    e2 = entry(engine, "new")
    commit(repo, f"feat: new\n\nChangelog: {e2}\n", "n.txt")

    plan = plan_of(engine, repo)
    assert plan["kind"] == "resume"
    assert plan["checkout"]["candidate_sha"] == candidate
    assert plan["checkout"]["head"] == head(repo) != candidate
    assert plan["checkout"]["matches_record"]["candidate_sha"] is False
    resume = plan["resume"]
    assert resume["candidate_sha"] == candidate and resume["entry_ids"] == [e1]
    assert resume["proof"] == {str(e1): {"commits": [proving], "merge_commit": None,
                                         "pr": None}}
    assert resume["status"] == "stamped"
    assert resume["effects"]["stamped"] is True and resume["effects"]["committed"] is False
    assert [e["id"] for e in resume["new_unreleased"]] == [e2]
    # Nothing is recomputed on today's HEAD.
    assert plan["range"] is None and plan["proof"] == {}
    assert plan["entries"] == {"proven": [], "unproven": []}


def test_c5_resume_plan_cli_shows_record_and_head_separately(cli_engine, repo, monkeypatch, capsys):
    from conftest import run_cli

    e1 = entry(cli_engine, "a")
    commit(repo, f"feat: a\n\nChangelog: {e1}\n", "a.txt")
    candidate = head(repo)
    release_entries(cli_engine, "proj-r", "1.1.0", entry_ids=[e1], candidate_sha=candidate,
                    checkout_root=str(repo), release_mode="git")
    e2 = entry(cli_engine, "new")
    commit(repo, "chore: later\n", "n.txt")
    monkeypatch.chdir(repo)
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-r"])
    out = capsys.readouterr()
    assert f"recorded candidate: {candidate}" in out.out and f"#{e1}" in out.out
    assert f"current HEAD: {head(repo)}" in out.out and "NOT the recorded candidate" in out.out
    assert "New unreleased entries since the record (1)" in out.out and f"#{e2}" in out.out
    assert "not the recorded candidate" in out.err
