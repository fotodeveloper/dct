"""K-80 / K-81 / K-95 C1 — ONE immutable candidate and ONE approved plan from
reading the proof to the stamp. The plan's snapshot (repo / checkout, full
candidate SHA, range base, V, selected ids, proof map, the state of every
unreleased entry) is approved by its digest; `apply` re-derives it FOR THE
CANDIDATE IT PINNED and refuses before any database mutation when it
differs, when HEAD moved, or when an entry changed inside the stamp
transaction. Driven through the production effects layer on real git
repositories; every refusal is checked against the database (entries,
current_version, release records) and the repository (HEAD, refs)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import approved_proposal_digest, git, make_git_repo, run_cli

from dct.core.changelog import add_entry, release_entries, update_entry
from dct.core.projects import create_project, get_project
from dct.core.releases import get_release, set_version_files
from dct.db import changelog_entries, releases
from dct.workflows.release import build_plan
from dct.workflows.release.effects import ReleaseRefused, apply_release

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SLUG = "proj-b"


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The effects layer creates commits."""


@pytest.fixture
def repo(engine, tmp_path):
    path = Path(make_git_repo(tmp_path / SLUG))
    (path / "changelog.md").write_text("# Changelog\n\n## [1.0.0] - 2026-01-01\n\n- first\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "chore: setup")
    git(path, "tag", "v1.0.0")
    create_project(engine, slug=SLUG, name="B", path=str(path), current_version="1.0.0")
    set_version_files(engine, SLUG, [], confirm=True)
    return path


def commit(repo, message: str, path: str = "f.txt") -> str:
    with open(Path(repo) / path, "a", encoding="utf-8") as fh:
        fh.write(message.splitlines()[0] + "\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def proven(engine, repo, text: str) -> int:
    eid = add_entry(engine, SLUG, "added", text)["id"]
    commit(repo, f"feat: {text}\n\nChangelog: {eid}\n")
    return eid


def state(engine, repo) -> dict:
    """What a refusal must leave untouched."""
    with engine.begin() as conn:
        entries = sorted(map(tuple, conn.execute(sa.select(
            changelog_entries.c.id, changelog_entries.c.entry, changelog_entries.c.version,
            changelog_entries.c.released_at)).all()))
        records = conn.execute(sa.select(sa.func.count()).select_from(releases)).scalar()
    return {"entries": entries, "records": records,
            "current": get_project(engine, SLUG)["current_version"],
            "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)")}


def digest(engine, repo, **kw) -> str:
    return build_plan(engine, SLUG, cwd=str(repo), **kw)["snapshot_digest"]


def apply(engine, repo, **kw):
    """apply_release; with an approved plan digest also the proposal digest
    `prepare` stores for it (K-76) — None when prepare refuses (a stale
    plan: apply then refuses on the plan first)."""
    kw.setdefault("confirm", True)
    kw.setdefault("no_push", True)  # no remote: an explicit local release (K-97 F2)
    if kw.get("plan_digest") and "proposal_digest" not in kw:
        selection = {k: kw[k] for k in ("version", "bump_type", "entry_ids", "no_entries")
                     if k in kw}
        kw["proposal_digest"] = approved_proposal_digest(
            engine, SLUG, repo, plan_digest=kw["plan_digest"], **selection)
    return apply_release(engine, SLUG, cwd=str(repo), **kw)


# ── K-80: the approved snapshot ──────────────────────────────────────────────


def test_apply_requires_the_approved_plan_digest(engine, repo):
    a = proven(engine, repo, "a")
    before = state(engine, repo)
    with pytest.raises(ReleaseRefused, match="plan-digest"):
        apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert state(engine, repo) == before


def test_apply_with_the_approved_digest_pins_the_planned_candidate(engine, repo):
    a = proven(engine, repo, "a")
    plan = build_plan(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a])
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=plan["snapshot_digest"])
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    assert record["candidate_sha"] == plan["snapshot"]["candidate_sha"]
    assert record["proof"] == plan["snapshot"]["proof"]


def test_head_moved_after_the_plan_is_refused_before_any_mutation(engine, repo):
    a = proven(engine, repo, "a")
    approved = digest(engine, repo, version="1.1.0", entry_ids=[a])
    commit(repo, "feat: unplanned\n", "late.txt")
    before = state(engine, repo)
    with pytest.raises(ReleaseRefused, match="approved plan") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=approved)
    assert exc.value.details["approved_digest"] == approved
    assert exc.value.details["snapshot"]["candidate_sha"] == git(repo, "rev-parse", "HEAD")
    assert state(engine, repo) == before


@pytest.mark.parametrize("change", ["added", "edited", "selection", "version"])
def test_any_change_of_the_approved_context_needs_a_new_plan(engine, repo, change):
    a = proven(engine, repo, "a")
    b = proven(engine, repo, "b")
    approved = digest(engine, repo, version="1.1.0", entry_ids=[a])
    kw = {"version": "1.1.0", "entry_ids": [a]}
    if change == "added":
        add_entry(engine, SLUG, "added", "late")
    elif change == "edited":
        update_entry(engine, SLUG, a, entry="a, reworded")
    elif change == "selection":
        kw["entry_ids"] = [a, b]
    else:
        kw["version"] = "1.2.0"
    before = state(engine, repo)
    with pytest.raises(ReleaseRefused, match="approved plan"):
        apply(engine, repo, plan_digest=approved, **kw)
    assert state(engine, repo) == before


def test_head_moving_between_the_plan_check_and_the_stamp_is_refused(engine, repo):
    """The re-derived plan matched; a commit lands right before the stamp —
    nothing is stamped (never a record whose effects point at another
    change)."""
    a = proven(engine, repo, "a")
    approved = digest(engine, repo, version="1.1.0", entry_ids=[a])
    before = state(engine, repo)
    moved = {}

    def hook(point):
        if point == "before_stamp":
            moved["sha"] = commit(repo, "feat: racing\n", "race.txt")

    with pytest.raises(ReleaseRefused, match="HEAD moved"):
        apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=approved, on_point=hook)
    after = state(engine, repo)
    assert {k: after[k] for k in ("entries", "records", "current")} == \
        {k: before[k] for k in ("entries", "records", "current")}


# ── K-81: only approved AND proven ids get a stamp (race plan → insert → stamp)


@pytest.mark.parametrize("race", ["insert", "edit", "delete"])
def test_entry_change_racing_the_stamp_is_refused_in_the_transaction(engine, repo, race):
    a = proven(engine, repo, "a")
    other = add_entry(engine, SLUG, "added", "unproven bystander")["id"]
    approved = digest(engine, repo, version="1.1.0", entry_ids=[a])
    before = state(engine, repo)

    def hook(point):
        if point != "before_stamp":
            return
        if race == "insert":
            add_entry(engine, SLUG, "added", "inserted after the plan")
        elif race == "edit":
            update_entry(engine, SLUG, a, entry="edited after the plan")
        else:
            with engine.begin() as conn:
                conn.execute(changelog_entries.update().where(changelog_entries.c.id == other)
                             .values(deleted_at=sa.func.now()))

    with pytest.raises(ReleaseRefused, match="changed since the approved plan"):
        apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=approved, on_point=hook)
    after = state(engine, repo)
    assert after["current"] == before["current"] == "1.0.0"
    assert after["records"] == before["records"] == 0
    stamped = [row for row in after["entries"] if row[2] is not None]
    assert stamped == []  # no entry got a version
    assert after["refs"] == before["refs"]


def test_core_refuses_a_new_stamp_outside_the_approved_proven_set(engine, repo):
    a = add_entry(engine, SLUG, "added", "a")["id"]
    b = add_entry(engine, SLUG, "added", "b")["id"]
    before = state(engine, repo)
    with pytest.raises(ValueError, match=f"does not prove: #{b}"):
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a, b], stampable_ids=[a])
    assert state(engine, repo) == before


def test_core_allows_the_legacy_entry_already_carrying_v(engine, repo):
    """The only exception: a live entry of the project already stamped with
    the semantically same V (legacy spelling) — never stamped again."""
    a = add_entry(engine, SLUG, "added", "a")["id"]
    legacy = add_entry(engine, SLUG, "added", "legacy")["id"]
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == legacy)
                     .values(version="v1.1"))
    out = release_entries(engine, SLUG, "1.1.0", entry_ids=[a, legacy], stampable_ids=[a])
    assert out["released"] == 1 and sorted(out["release"]["entry_ids"]) == sorted([a, legacy])


# ── K-95 C1: a project without a registered path ─────────────────────────────


def test_apply_refuses_a_project_without_a_path(engine, tmp_path):
    create_project(engine, slug="proj-np", name="NP", current_version="1.0.0")
    add_entry(engine, "proj-np", "added", "x")
    with pytest.raises(ReleaseRefused, match="no registered path"):
        apply_release(engine, "proj-np", version="1.1.0", no_entries=True, confirm=True,
                      plan_digest="0" * 64, cwd=str(tmp_path))
    assert get_project(engine, "proj-np")["current_version"] == "1.0.0"
    assert get_release(engine, "proj-np") is None


# ── CLI: plan → digest → apply ───────────────────────────────────────────────


def test_cli_plan_digest_then_apply(cli_engine, repo, monkeypatch, capsys):
    import json

    a = proven(cli_engine, repo, "a")
    monkeypatch.chdir(repo)
    run_cli(monkeypatch, ["release", "plan", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["snapshot"]["selected_ids"] == [a]
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["release", "apply", "--no-push", "-P", SLUG, "--version", "1.1.0",
                              "--entries", str(a), "--confirm"])
    assert exc.value.code == 2  # --plan-digest is required
    capsys.readouterr()
    run_cli(monkeypatch, ["release", "prepare", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--plan-digest", plan["snapshot_digest"],
                          "--json"])
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["outcome"] == "prepared"
    run_cli(monkeypatch, ["release", "apply", "--no-push", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--confirm", "--plan-digest",
                          plan["snapshot_digest"], "--proposal-digest",
                          prepared["details"]["proposal_sha256"], "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "completed"


# ── K-90 through the effects layer ───────────────────────────────────────────


def test_pr_reference_survives_a_setting_change_between_apply_and_resume(engine, repo):
    """The changelog section renders `(#7)` from the stored proof of the
    merge that brought the entry in; the render setting is the RECORD's, so
    switching the project preference off before the resume changes neither
    the section nor its expected hash."""
    from dct.core.projects import set_changelog_pr_refs
    from dct.workflows.release.effects import resume_release

    set_changelog_pr_refs(engine, SLUG, True)
    a = add_entry(engine, SLUG, "added", "feature a", branch="feat/a",
                  branch_source="client")["id"]
    git(repo, "checkout", "-q", "-b", "feat/a")
    commit(repo, f"feat: a\n\nChangelog: {a}\n", "a.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #7 from org/feat/a", "feat/a")
    approved = digest(engine, repo, version="1.1.0", entry_ids=[a])

    class Stop(Exception):
        pass

    def stop(point):
        if point == "after_stamp":
            raise Stop

    with pytest.raises(Stop):
        apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=approved, on_point=stop)
    assert get_release(engine, SLUG, "1.1.0")["render"] == {"pr_refs": True}
    set_changelog_pr_refs(engine, SLUG, False)
    out = resume_release(engine, SLUG, cwd=str(repo))
    assert out["outcome"] == "completed"
    text = (repo / "changelog.md").read_text()
    assert "- feature a (#7)" in text and "feat/a" not in text
    assert git(repo, "show", "HEAD:changelog.md") == text.rstrip("\n")
