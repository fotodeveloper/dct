"""Round 4 of the release proof layer (R8a-2) — on REAL throwaway git
repositories through the production build_plan / commit_candidates:

- K-105: a revert is compared on a representation that keeps the blob
  identity, the file type and the mode where git prints no textual patch;
- K-116: the accepted proof-layer contracts (H1, H3, H5, H6) and the two
  changed ones — the origin of a PR number (H7) and the bounded, never
  truncated ancestry read (H8);
- K-119: the plan JSON carries the project's executor and, for an unfinished
  record, the record's stored executor, its progress owner and its consent.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import approved_proposal_digest, git, make_git_repo, run_cli

from dct.core import gitinfo
from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project, set_release_executor
from dct.core.releases import get_release, proof_pr, validate_proof
from dct.export import export_changelog_md
from dct.workflows.release import build_plan, commit_candidates
from dct.workflows.release import plan as plan_module

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

B0, B1, B2 = b"B0\x00zero", b"B1\x00one", b"B2\x00two"


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD")


def write(repo, path: str, content: bytes) -> None:
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def commit_all(repo, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    return head(repo)


def commit_file(repo, message: str, path: str, content: bytes) -> str:
    write(repo, path, content)
    return commit_all(repo, message)


def entry(engine, text: str = "change") -> int:
    return add_entry(engine, "proj-r", "added", text)["id"]


def rows(plan: dict) -> dict[int, dict]:
    return {r["id"]: r for r in plan["entries"]["proven"] + plan["entries"]["unproven"]}


def reports(plan: dict) -> dict[str, dict]:
    return {r["sha"]: r for r in plan["reverts"]}


@pytest.fixture
def repo(engine, tmp_path):
    """proj-r: a repository on main holding b.bin = B0, tagged v1.0.0."""
    path = Path(make_git_repo(tmp_path / "proj-r"))
    commit_file(path, "chore: binary base", "b.bin", B0)
    git(path, "tag", "v1.0.0")
    create_project(engine, slug="proj-r", name="R", path=str(path), current_version="1.0.0")
    return path


def plan_of(engine, repo, **kw) -> dict:
    return build_plan(engine, "proj-r", cwd=str(repo), **kw)


def revert(repo, sha: str) -> str:
    """A clean, unedited `git revert` of `sha`."""
    git(repo, "revert", "--no-edit", sha)
    return head(repo)


def revert_resolved(repo, sha: str, resolve) -> str:
    """`git revert` of `sha` whose result is then changed by `resolve(repo)`
    before the commit — git's standard message is kept."""
    git(repo, "revert", "--no-commit", sha)
    resolve(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return head(repo)


# ── K-105: blob identity in the revert comparison ────────────────────────────


def test_full_binary_revert_is_still_confirmed(engine, repo):
    """The fix does not turn every binary revert into an unconfirmed one:
    B0 → B1 reverted to exactly B0 is a FULL revert."""
    e = entry(engine)
    c = commit_file(repo, f"feat: binary\n\nChangelog: {e}\n", "b.bin", B1)
    r = revert(repo, c)
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "in_range"
    assert rows(plan)[e]["reason"] == "reverted"
    assert rows(plan)[e]["reverted_by"] == [r]


def test_revert_of_revert_to_another_blob_restores_no_proof(engine, repo):
    """B0 → B1 (proves the entry), its full revert, then a revert of that
    revert resolved to B2: the entry's change did NOT come back — two
    "Binary files differ" sections are not the same change."""
    e = entry(engine)
    c = commit_file(repo, f"feat: binary\n\nChangelog: {e}\n", "b.bin", B1)
    r = revert(repo, c)
    r2 = revert_resolved(repo, r, lambda repo: write(repo, "b.bin", B2))
    plan = plan_of(engine, repo)
    assert reports(plan)[r2]["status"] == "unconfirmed"
    assert reports(plan)[r2]["reason"] == "partial"
    assert reports(plan)[r]["status"] == "in_range" and reports(plan)[r]["effective"] is True
    assert e not in {x["id"] for x in plan["entries"]["proven"]}
    assert str(e) not in plan["proof"]
    assert rows(plan)[e]["reason"] == "reverted"


def test_revert_of_revert_back_to_the_same_blob_restores_the_proof(engine, repo):
    """…until the real result returns: the revert of the revert that brings
    B1 back restores the original proof."""
    e = entry(engine)
    c = commit_file(repo, f"feat: binary\n\nChangelog: {e}\n", "b.bin", B1)
    r = revert(repo, c)
    r2 = revert(repo, r)
    plan = plan_of(engine, repo)
    assert reports(plan)[r2]["status"] == "in_range"
    assert reports(plan)[r]["effective"] is False
    assert rows(plan)[e]["commits"] == [c]


def test_candidates_do_not_treat_a_different_blob_as_proven(engine, repo):
    """`dct clog candidates` evaluates the same comparison."""
    e = entry(engine)
    c = commit_file(repo, f"feat: binary\n\nChangelog: {e}\n", "b.bin", B1)
    r = revert(repo, c)
    revert_resolved(repo, r, lambda repo: write(repo, "b.bin", B2))
    assert e not in commit_candidates(engine, "proj-r", cwd=str(repo))["already_proven"]


def test_edited_revert_of_a_binary_change_is_partial(engine, repo):
    """A revert that leaves another blob than the one the reverted commit
    replaced neutralizes nothing automatically."""
    e = entry(engine)
    c = commit_file(repo, f"feat: binary\n\nChangelog: {e}\n", "b.bin", B1)
    r = revert_resolved(repo, c, lambda repo: write(repo, "b.bin", B2))
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "revert_unconfirmed"


def test_added_binary_file_reverted_and_re_added_with_other_content(engine, repo):
    e = entry(engine)
    c = commit_file(repo, f"feat: add\n\nChangelog: {e}\n", "new.bin", B1)
    r = revert(repo, c)
    assert reports(plan_of(engine, repo))[r]["status"] == "in_range"
    r2 = revert_resolved(repo, r, lambda repo: write(repo, "new.bin", B2))
    plan = plan_of(engine, repo)
    assert reports(plan)[r2]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "reverted"


def test_deleted_binary_file_restored_with_other_content_is_partial(engine, repo):
    e = entry(engine)
    os.unlink(repo / "b.bin")
    c = commit_all(repo, f"feat: drop\n\nChangelog: {e}\n")
    r = revert_resolved(repo, c, lambda repo: write(repo, "b.bin", B2))
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "revert_unconfirmed"


def test_deleted_binary_file_restored_exactly_is_a_full_revert(engine, repo):
    e = entry(engine)
    os.unlink(repo / "b.bin")
    c = commit_all(repo, f"feat: drop\n\nChangelog: {e}\n")
    r = revert(repo, c)
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "in_range"
    assert rows(plan)[e]["reason"] == "reverted"


def test_binary_rename_reverted_and_reapplied_with_other_content(engine, repo):
    e = entry(engine)
    git(repo, "mv", "b.bin", "renamed.bin")
    c = commit_all(repo, f"feat: rename\n\nChangelog: {e}\n")
    r = revert(repo, c)
    assert reports(plan_of(engine, repo))[r]["status"] == "in_range"
    r2 = revert_resolved(repo, r, lambda repo: write(repo, "renamed.bin", B2))
    plan = plan_of(engine, repo)
    assert reports(plan)[r2]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "reverted"


def test_binary_rename_reverted_and_reapplied_exactly(engine, repo):
    e = entry(engine)
    git(repo, "mv", "b.bin", "renamed.bin")
    c = commit_all(repo, f"feat: rename\n\nChangelog: {e}\n")
    r = revert(repo, c)
    r2 = revert(repo, r)
    plan = plan_of(engine, repo)
    assert reports(plan)[r2]["status"] == "in_range"
    assert rows(plan)[e]["commits"] == [c]


def test_mode_only_change_and_its_revert(engine, repo):
    """A mode-only change has no textual patch either: its full revert
    confirms, a "revert" that also replaces the blob does not."""
    e = entry(engine)
    git(repo, "update-index", "--chmod=+x", "b.bin")
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", f"feat: exec\n\nChangelog: {e}\n")
    c = head(repo)
    git(repo, "checkout", "-q", "--", "b.bin")
    r = revert(repo, c)
    assert reports(plan_of(engine, repo))[r]["status"] == "in_range"

    def reapply_with_other_blob(repo):
        write(repo, "b.bin", B2)
        git(repo, "add", "b.bin")
        git(repo, "update-index", "--chmod=+x", "b.bin")

    git(repo, "revert", "--no-commit", r)
    reapply_with_other_blob(repo)
    git(repo, "commit", "-q", "--no-edit")
    plan = plan_of(engine, repo)
    assert reports(plan)[head(repo)]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "reverted"


def test_binary_change_with_a_mode_change_added_by_the_revert_is_partial(engine, repo):
    e = entry(engine)
    c = commit_file(repo, f"feat: binary\n\nChangelog: {e}\n", "b.bin", B1)
    git(repo, "revert", "--no-commit", c)
    git(repo, "update-index", "--chmod=+x", "b.bin")
    git(repo, "commit", "-q", "--no-edit")
    plan = plan_of(engine, repo)
    assert reports(plan)[head(repo)]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "revert_unconfirmed"


def test_file_replaced_by_a_symlink_and_reverted_to_other_content(engine, repo):
    """The file TYPE is part of the change: a regular file replaced by a
    symlink, "reverted" to a regular file with another blob."""
    e = entry(engine)
    os.unlink(repo / "b.bin")
    os.symlink("README", repo / "b.bin")
    c = commit_all(repo, f"feat: link\n\nChangelog: {e}\n")
    full = revert(repo, c)
    assert reports(plan_of(engine, repo))[full]["status"] == "in_range"

    def as_file_with_other_blob(repo):
        os.unlink(repo / "b.bin")
        write(repo, "b.bin", B2)

    git(repo, "reset", "-q", "--hard", c)
    r = revert_resolved(repo, c, as_file_with_other_blob)
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "revert_unconfirmed"


def test_text_revert_after_an_unrelated_edit_of_the_same_file_stays_full(engine, repo):
    """Blob ids of a TEXT file legitimately differ when another commit
    touched other lines in between — the textual change is the identity
    there, so a clean revert still confirms."""
    lines = [f"line {i}\n" for i in range(40)]
    commit_file(repo, "chore: text base", "t.txt", "".join(lines).encode())
    e = entry(engine)
    changed = lines.copy()
    changed[5] = "line five changed\n"
    c = commit_file(repo, f"feat: text\n\nChangelog: {e}\n", "t.txt", "".join(changed).encode())
    later = changed.copy()
    later[30] = "line thirty changed\n"
    commit_file(repo, "chore: elsewhere", "t.txt", "".join(later).encode())
    r = revert(repo, c)
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "in_range"
    assert rows(plan)[e]["reason"] == "reverted"


def test_change_representation_keeps_the_ids_only_without_a_textual_patch(engine, repo):
    """The ONE representation: sections of a binary / mode-only / empty
    change carry the full blob ids; textual sections carry their lines."""
    base = head(repo)
    commit_file(repo, "feat: binary", "b.bin", B1)
    b1 = head(repo)
    commit_file(repo, "feat: binary again", "b.bin", B2)
    first = plan_module.change_between(str(repo), base, b1)
    second = plan_module.change_between(str(repo), base, head(repo))
    assert first != second
    blob0, blob1 = git(repo, "rev-parse", f"{base}:b.bin"), git(repo, "rev-parse", f"{b1}:b.bin")
    assert len(blob0) >= 40
    assert any(blob0 in line and blob1 in line for section in first for line in section)
    assert plan_module.change_between(str(repo), b1, base) != first


# ── K-116 H7: the origin of a PR number ──────────────────────────────────────

SHA, MERGE = "a" * 40, "b" * 40


def text_commit(repo, message: str, path: str = "f.txt") -> str:
    with open(Path(repo) / path, "a", encoding="utf-8") as fh:
        fh.write(message.splitlines()[0] + "\n")
    return commit_all(repo, message)


def merged_pr(engine, repo, number: int = 7) -> tuple[int, str]:
    """An entry proven on feat/a, brought in by `Merge pull request #N`."""
    e = entry(engine, "feature a")
    git(repo, "checkout", "-q", "-b", "feat/a")
    text_commit(repo, f"feat: a\n\nChangelog: {e}\n", "a.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m",
        f"Merge pull request #{number} from org/feat/a", "feat/a")
    return e, head(repo)


def test_plan_marks_a_merge_recognised_pr_as_git(engine, repo):
    e, merge = merged_pr(engine, repo)
    plan = plan_of(engine, repo)
    proof = plan["proof"][str(e)]
    assert (proof["pr"], proof.get("pr_source"), proof["merge_commit"]) == (7, "git", merge)
    assert rows(plan)[e].get("pr_source") == "git"
    assert plan["snapshot"]["proof"][str(e)].get("pr_source") == "git"


def test_plan_heuristic_number_is_never_a_declared_pr(engine, repo):
    """A `(#12)` subject suffix is a heuristic: no `pr`, no `pr_source`, and
    its association is never certain."""
    e = entry(engine, "squashed")
    text_commit(repo, f"feat: squashed (#12)\n\nChangelog: {e}\n")
    plan = plan_of(engine, repo)
    proof = plan["proof"][str(e)]
    assert proof["pr"] is None and proof["pr_source"] is None
    assert proof["associations"] == [
        {"merge_commit": None, "pr": 12, "source": "subject_suffix", "certain": False}]
    assert plan["squash_commits"][0]["pr"] == 12  # the proposal's signal only
    assert proof_pr(plan["proof"], e) is None


def test_core_stamp_records_a_supplied_pr_as_a_client_attestation(engine, repo):
    e = entry(engine)
    release_entries(engine, "proj-r", "1.1.0", entry_ids=[e],
                    proof={str(e): {"commits": [SHA], "merge_commit": None, "pr": 7}})
    stored = get_release(engine, "proj-r", "1.1.0")["proof"][str(e)]
    assert stored["pr"] == 7 and stored.get("pr_source") == "client"


def test_mcp_supplied_pr_is_a_client_attestation_whatever_the_map_claims(engine, srv):
    """The MCP server cannot verify git: a number that arrives through it is
    the client's declaration — also when the map copies the plan's `git`."""
    a = add_entry(engine, "test", "added", "a")["id"]
    b = add_entry(engine, "test", "added", "b")["id"]
    out = srv.release_changelog(project="test", version="1.1.0", entry_ids=[a, b], proof={
        str(a): {"commits": [SHA], "merge_commit": MERGE, "pr": 7, "source": "own_merge",
                 "pr_source": "git"},
        str(b): {"commits": [SHA], "merge_commit": None, "pr": 9},
    })
    assert "error" not in out, out
    stored = out["release"]["proof"]
    assert [stored[str(i)].get("pr_source") for i in (a, b)] == ["client", "client"]
    assert srv.get_release(project="test", version="1.1.0")["release"]["proof"] == stored
    # The identical call is the documented no-op re-run.
    again = srv.release_changelog(project="test", version="1.1.0", entry_ids=[a, b], proof={
        str(a): {"commits": [SHA], "merge_commit": MERGE, "pr": 7, "source": "own_merge",
                 "pr_source": "git"},
        str(b): {"commits": [SHA], "merge_commit": None, "pr": 9},
    })
    assert "error" not in again and again["released"] == 0


def test_git_origin_needs_the_merge_recognition(engine):
    """Only a real merge recognition is a git-recognised PR."""
    with pytest.raises(ValueError, match="pr_source"):
        validate_proof({"1": {"commits": [SHA], "merge_commit": None, "pr": 7}}, pr_source="git")
    with pytest.raises(ValueError, match="pr_source"):
        validate_proof({"1": {"commits": [SHA], "merge_commit": MERGE, "pr": 7,
                              "source": "own_merge", "pr_source": "client"}}, pr_source="git")
    ok = validate_proof({"1": {"commits": [SHA], "merge_commit": MERGE, "pr": 7,
                               "source": "introducing_merge"}}, pr_source="git")
    assert ok["1"].get("pr_source") == "git"


def test_proof_without_a_pr_states_no_origin(engine):
    assert "pr_source" not in validate_proof({"1": [SHA]})["1"]
    with pytest.raises(ValueError, match="pr_source"):
        validate_proof({"1": {"commits": [SHA], "merge_commit": None, "pr": None,
                              "pr_source": "client"}})
    with pytest.raises(ValueError, match="pr_source"):
        validate_proof({"1": {"commits": [SHA], "merge_commit": None, "pr": 7,
                              "pr_source": "hosting"}})
    with pytest.raises(ValueError, match="pr_source"):
        validate_proof({"1": [SHA]}, pr_source="plan")


def test_a_heuristic_association_can_never_be_certain(engine):
    with pytest.raises(ValueError, match="subject_suffix"):
        validate_proof({"1": {"commits": [SHA], "merge_commit": None, "pr": None,
                              "associations": [{"merge_commit": None, "pr": 12,
                                                "source": "subject_suffix", "certain": True}]}})


def test_release_status_shows_the_origin_of_each_pr(cli_engine, monkeypatch, capsys):
    a = add_entry(cli_engine, "proj-a", "added", "a")["id"]
    b = add_entry(cli_engine, "proj-a", "added", "b")["id"]
    release_entries(cli_engine, "proj-a", "1.1.0", entry_ids=[a, b], proof={
        str(a): {"commits": [SHA], "merge_commit": None, "pr": 7},
        str(b): [SHA]})
    run_cli(monkeypatch, ["release", "status", "-P", "proj-a", "--version", "1.1.0"])
    out = capsys.readouterr().out
    assert f"#{a}: PR #7 — client attestation" in out
    assert f"#{b}: PR" not in out
    run_cli(monkeypatch, ["release", "status", "-P", "proj-a", "--version", "1.1.0", "--json"])
    record = json.loads(capsys.readouterr().out)["release"]
    assert record["proof"][str(a)].get("pr_source") == "client"


def test_a_client_attested_pr_still_renders_only_by_opt_in(engine):
    """K-90 is unchanged: the unambiguous stored `pr`, with the opt-in."""
    from dct.core.projects import set_changelog_pr_refs

    create_project(engine, slug="proj-k", name="K", current_version="1.0.0")
    off = add_entry(engine, "proj-k", "added", "off")["id"]
    release_entries(engine, "proj-k", "1.1.0", entry_ids=[off],
                    proof={str(off): {"commits": [SHA], "merge_commit": None, "pr": 7}})
    assert "(#7)" not in export_changelog_md(engine, "proj-k", version="1.1.0")
    set_changelog_pr_refs(engine, "proj-k", True)
    on = add_entry(engine, "proj-k", "added", "on")["id"]
    release_entries(engine, "proj-k", "1.2.0", entry_ids=[on],
                    proof={str(on): {"commits": [SHA], "merge_commit": None, "pr": 8}})
    assert "- on (#8)" in export_changelog_md(engine, "proj-k", version="1.2.0")


# ── K-116 H8: limits refuse, never truncate ──────────────────────────────────


def test_too_many_associations_refuse_the_plan(engine, repo, monkeypatch):
    """One entry proven by more recognitions than the cap: the plan is
    refused — never a map cut to the first N."""
    from dct.core import releases as ledger

    monkeypatch.setattr(ledger, "ASSOCIATIONS_MAX", 2)
    e = entry(engine)
    for n in (11, 12, 13):
        text_commit(repo, f"feat: part {n} (#{n})\n\nChangelog: {e}\n", f"{n}.txt")
    with pytest.raises(ValueError, match="more than 2"):
        plan_of(engine, repo)


def test_ancestry_reader_reports_every_parent_line(repo):
    git(repo, "checkout", "-q", "-b", "side")
    text_commit(repo, "feat: side", "s.txt")
    git(repo, "checkout", "-q", "main")
    text_commit(repo, "feat: main", "m.txt")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
    graph = dict(plan_module.read_ancestry(str(repo), head(repo)))
    expected = {}
    for line in git(repo, "rev-list", "--parents", "HEAD").splitlines():
        sha, *parents = line.split()
        expected[sha] = parents
    assert graph == expected and len(graph[head(repo)]) == 2


def test_ancestry_past_the_cap_is_refused_not_cut(engine, repo, monkeypatch):
    """A graph larger than the documented cap refuses the plan; a base
    computed on the first N commits would be a silently wrong range."""
    for n in range(4):
        text_commit(repo, f"feat: {n}", f"{n}.txt")
    monkeypatch.setattr(plan_module, "ANCESTRY_MAX_COMMITS", 3)
    with pytest.raises(gitinfo.GitReadError, match="more than 3 commits"):
        plan_of(engine, repo)
    monkeypatch.setattr(plan_module, "ANCESTRY_MAX_COMMITS", 6)
    assert plan_of(engine, repo)["range"]["base_distance"] == 4


def test_ancestry_exactly_at_the_cap_is_read(engine, repo, monkeypatch):
    total = len(git(repo, "rev-list", "HEAD").splitlines())
    monkeypatch.setattr(plan_module, "ANCESTRY_MAX_COMMITS", total)
    assert plan_of(engine, repo)["range"]["base_tag"] == "v1.0.0"
    monkeypatch.setattr(plan_module, "ANCESTRY_MAX_COMMITS", total - 1)
    with pytest.raises(gitinfo.GitReadError, match="more than"):
        plan_of(engine, repo)


def test_stream_reader_fails_on_a_git_error_instead_of_a_partial_result(repo):
    with pytest.raises(gitinfo.GitReadError):
        list(gitinfo.git_read_lines(str(repo), "rev-list", "--parents", "no-such-ref", "--"))


def test_stream_reader_stops_git_when_the_consumer_stops_early(repo):
    """Closing the reader early (a refusal past the cap) leaves no process
    behind and raises nothing by itself."""
    for n in range(3):
        text_commit(repo, f"feat: {n}", f"{n}.txt")
    lines = gitinfo.git_read_lines(str(repo), "rev-list", "--parents", "HEAD", "--")
    first = next(lines)
    lines.close()
    assert first.split()[0] == head(repo)


def test_stream_reader_times_out_as_a_read_error(repo, monkeypatch, tmp_path):
    """A git that never finishes is stopped at the deadline and reported —
    never a result built from what happened to arrive."""
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fd = os.open(fake, os.O_WRONLY | os.O_CREAT, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/sh\necho " + "a" * 40 + "\nexec sleep 30\n")
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(gitinfo.GitReadError, match="timed out"):
        list(gitinfo.git_read_lines(str(repo), "rev-list", "HEAD", timeout=0.5))


# ── K-116 H1 / H3 / H5 / H6: the accepted contracts ──────────────────────────


def test_h1_alias_refuses_a_project_without_a_path(cli_engine, monkeypatch, tmp_path, capsys):
    """A missing path is not a confirmed "no git": the `clog release` alias
    refuses like apply (reason `no_path` in the plan)."""
    from dct.core.projects import get_project

    create_project(cli_engine, slug="proj-np", name="NP", current_version="1.0.0")
    add_entry(cli_engine, "proj-np", "added", "x")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["clog", "release", "1.1.0", "-P", "proj-np", "--no-push",
                              "--confirm"])
    assert exc.value.code == 1
    assert "no registered path" in capsys.readouterr().err
    assert get_project(cli_engine, "proj-np")["current_version"] == "1.0.0"
    assert get_release(cli_engine, "proj-np") is None
    plan = build_plan(cli_engine, "proj-np", cwd=str(tmp_path), version="1.1.0")
    assert plan["checkout"]["reason"] == "no_path" and plan["snapshot"] is None


def test_h3_an_unrelated_new_entry_changes_the_approved_snapshot(engine, repo):
    """The snapshot covers EVERY unreleased entry: also an entry outside the
    selection, added after the plan, makes it another plan."""
    e = entry(engine)
    text_commit(repo, f"feat: x\n\nChangelog: {e}\n")
    approved = plan_of(engine, repo, version="1.1.0", entry_ids=[e])
    late = entry(engine, "unrelated, not selected")
    after = plan_of(engine, repo, version="1.1.0", entry_ids=[e])
    assert after["snapshot"]["selected_ids"] == approved["snapshot"]["selected_ids"] == [e]
    assert str(late) in after["snapshot"]["entries"]
    assert after["snapshot_digest"] != approved["snapshot_digest"]


def test_h5_a_tie_is_decided_deterministically_and_still_shown(cli_engine, monkeypatch, capsys,
                                                             tmp_path):
    path = Path(make_git_repo(tmp_path / "proj-t"))
    git(path, "tag", "v1.0.0")
    create_project(cli_engine, slug="proj-t", name="T", path=str(path), current_version="1.0.0")
    git(path, "checkout", "-q", "-b", "left")
    text_commit(path, "feat: l", "l.txt")
    git(path, "tag", "v2.0.0")
    git(path, "checkout", "-q", "-b", "right", "v1.0.0")
    text_commit(path, "feat: r", "r.txt")
    git(path, "tag", "v1.9.0")
    git(path, "checkout", "-q", "left")
    git(path, "merge", "-q", "--no-ff", "-m", "Merge branch 'right'", "right")
    first = build_plan(cli_engine, "proj-t", cwd=str(path))
    assert build_plan(cli_engine, "proj-t", cwd=str(path))["range"] == first["range"]
    rng = first["range"]
    assert rng["base_tag"] == "v1.9.0" and rng["confirm_required"] is True
    assert [a["name"] for a in rng["alternatives"]] == ["v2.0.0"]
    assert any("equally near: v2.0.0" in w for w in first["warnings"])
    monkeypatch.chdir(path)
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-t"])
    assert "equally near: v2.0.0" in capsys.readouterr().out


def test_h6_an_unverifiable_revert_gives_no_automatic_proof(engine, tmp_path):
    """A revert of the ROOT commit has nothing to be compared with: it is
    unconfirmed, and the entry the root proves gets no automatic proof."""
    path = tmp_path / "proj-root"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    create_project(engine, slug="proj-root", name="Root", path=str(path),
                   current_version="1.0.0")
    e = add_entry(engine, "proj-root", "added", "root change")["id"]
    (path / "README").write_text("x\n")
    root = commit_all(path, f"feat: root\n\nChangelog: {e}\n")
    (path / "other.txt").write_text("y\n")
    commit_all(path, "chore: other")
    git(path, "rm", "-q", "README")
    r = commit_all(path, f'Revert "feat: root"\n\nThis reverts commit {root}.\n')
    plan = build_plan(engine, "proj-root", cwd=str(path))
    assert reports(plan)[r]["status"] == "unconfirmed"
    assert reports(plan)[r]["reason"] == "partial"
    assert rows(plan)[e]["reason"] == "revert_unconfirmed"
    assert plan["proof"] == {}


def test_h6_an_edited_plain_revert_gives_no_automatic_proof(engine, repo):
    e = entry(engine)
    c = text_commit(repo, f"feat: two lines\n\nChangelog: {e}\n", "two.txt")
    r = revert_resolved(repo, c, lambda repo: write(repo, "two.txt", b"kept by hand\n"))
    plan = plan_of(engine, repo)
    assert reports(plan)[r]["status"] == "unconfirmed"
    assert rows(plan)[e]["reason"] == "revert_unconfirmed"
    assert str(e) not in plan["proof"]


# ── K-119: executor and record ownership in the plan JSON ────────────────────


def test_plan_names_the_project_executor(engine, repo):
    plan = plan_of(engine, repo)
    assert plan["schema_version"] == 3
    assert plan["executor"] == {"project": "dct", "record": None}
    set_release_executor(engine, "proj-r", "own")
    assert plan_of(engine, repo)["executor"] == {"project": "own", "record": None}


def test_resume_plan_routes_by_the_record_not_by_todays_setting(engine, repo):
    """A record stamped through the ledger (no consent) while the project
    was `dct`; the setting changes to `own` afterwards — the plan shows both,
    and the record's stored values decide."""
    release_entries(engine, "proj-r", "1.1.0", entry_ids=[])
    set_release_executor(engine, "proj-r", "own")
    plan = plan_of(engine, repo)
    assert plan["kind"] == "resume"
    assert plan["executor"] == {
        "project": "own",
        "record": {"version": "1.1.0", "status": "stamped", "executor": "dct",
                   "progress_owner": "ledger", "consent": False},
    }


def test_resume_plan_of_an_own_record_under_a_dct_project(engine, repo):
    set_release_executor(engine, "proj-r", "own")
    release_entries(engine, "proj-r", "1.1.0", entry_ids=[], expected_executor="own")
    set_release_executor(engine, "proj-r", "dct")
    assert plan_of(engine, repo)["executor"] == {
        "project": "dct",
        "record": {"version": "1.1.0", "status": "stamped", "executor": "own",
                   "progress_owner": "ledger", "consent": False},
    }


def test_plan_without_a_checkout_still_names_the_executor(engine, tmp_path):
    create_project(engine, slug="proj-np", name="NP", current_version="1.0.0")
    set_release_executor(engine, "proj-np", "own")
    plan = build_plan(engine, "proj-np", cwd=str(tmp_path))
    assert plan["executor"] == {"project": "own", "record": None}
    release_entries(engine, "proj-np", "1.1.0", entry_ids=[], expected_executor="own")
    plan = build_plan(engine, "proj-np", cwd=str(tmp_path))
    assert plan["kind"] == "resume"
    assert (plan["executor"] or {}).get("record", {}).get("executor") == "own"


def test_plan_json_carries_the_executor_through_the_cli(cli_engine, monkeypatch, capsys,
                                                       tmp_path):
    path = Path(make_git_repo(tmp_path / "proj-c"))
    git(path, "tag", "v1.0.0")
    create_project(cli_engine, slug="proj-c", name="C", path=str(path),
                   current_version="1.0.0")
    monkeypatch.chdir(path)
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-c", "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema_version"] == 3 and plan["executor"] == {"project": "dct", "record": None}
    run_cli(monkeypatch, ["release", "plan", "-P", "proj-c"])
    assert "executor: dct" in capsys.readouterr().out


# ── K-114: each documented schema table against ITS module ───────────────────

DOC = Path(__file__).resolve().parent.parent / "docs" / "release-workflow.md"


def doc_table(heading_prefix: str) -> dict[str, str]:
    """key → content of the FIRST `| key | content |` table under the
    heading that starts with `heading_prefix` (and before the next heading)
    in docs/release-workflow.md — one specific table, found by its section."""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading_prefix)]
    assert len(starts) == 1, f"{heading_prefix!r} must name exactly one heading: {starts}"
    table: dict[str, str] = {}
    for line in lines[starts[0] + 1:]:
        if line.startswith("#"):
            break
        if line.startswith("| `"):
            key, content = line.strip().strip("|").split("|", 1)
            table[key.strip().strip("`")] = content.strip()
        elif table:
            break
    return table


@pytest.mark.parametrize("heading, keys, module", [
    ("## `dct release plan ", "PLAN_KEYS", "plan"),
    ("## `dct clog candidates ", "CANDIDATE_KEYS", "plan"),
    ("### JSON (`--json`, every subcommand)", "RESULT_KEYS", "effects"),
])
def test_documented_schema_table_matches_its_module(heading, keys, module):
    """The table's keys (in order) and its `schema_version` cell are the
    constants of the module that emits THAT document — the plan and the
    candidates share plan.SCHEMA_VERSION, the effects result has its own."""
    import importlib

    source = importlib.import_module(f"dct.workflows.release.{module}")
    table = doc_table(heading)
    assert list(table) == list(getattr(source, keys))
    assert table["schema_version"] == f"`{source.SCHEMA_VERSION}`"


def test_schema_versions_of_the_two_layers_are_independent():
    from dct.workflows.release import effects

    assert plan_module.SCHEMA_VERSION == 3 and effects.SCHEMA_VERSION == 1


def test_prose_names_the_current_plan_schema_version():
    text = DOC.read_text(encoding="utf-8")
    assert f"`schema_version` (currently `{plan_module.SCHEMA_VERSION}`" in text
    assert "currently `2`" not in text


@pytest.mark.parametrize("path", [
    "dct/skills/release/SKILL.md", "docs/assistant-fallbacks.md",
])
def test_clients_name_the_plan_schema_version_they_need(path):
    """The capability line of each client: the plan document it reads must
    be the schema of THIS code (the `executor` key arrived with it)."""
    text = (DOC.parent.parent / path).read_text(encoding="utf-8")
    assert f"`schema_version` lower than {plan_module.SCHEMA_VERSION}" in text


# ── K-116 H7 through the effects layer ───────────────────────────────────────


def test_apply_stores_the_plans_recognised_pr_as_git(engine, tmp_path, git_identity):
    """`dct release apply` stamps the approved snapshot: its PR number was
    recognised in the repository, and the record says so."""
    from dct.core.releases import set_version_files
    from dct.workflows.release.effects import apply_release

    path = Path(make_git_repo(tmp_path / "proj-e"))
    (path / "changelog.md").write_text("# Changelog\n\n## [1.0.0] - 2026-01-01\n\n- first\n")
    commit_all(path, "chore: setup")
    git(path, "tag", "v1.0.0")
    create_project(engine, slug="proj-e", name="E", path=str(path), current_version="1.0.0")
    set_version_files(engine, "proj-e", [], confirm=True)
    e = add_entry(engine, "proj-e", "added", "feature a")["id"]
    git(path, "checkout", "-q", "-b", "feat/a")
    text_commit(path, f"feat: a\n\nChangelog: {e}\n", "a.txt")
    git(path, "checkout", "-q", "main")
    git(path, "merge", "-q", "--no-ff", "-m", "Merge pull request #7 from org/feat/a", "feat/a")
    selection = {"version": "1.1.0", "entry_ids": [e]}
    digest = build_plan(engine, "proj-e", cwd=str(path), **selection)["snapshot_digest"]
    proposal = approved_proposal_digest(engine, "proj-e", path, plan_digest=digest, **selection)
    out = apply_release(engine, "proj-e", cwd=str(path), plan_digest=digest,
                        proposal_digest=proposal, no_push=True, confirm=True, **selection)
    assert out["outcome"] == "completed"
    stored = get_release(engine, "proj-e", "1.1.0")["proof"][str(e)]
    assert (stored["pr"], stored["pr_source"]) == (7, "git")
