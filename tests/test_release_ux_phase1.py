"""Release / skill UX, phase 1 (#877) — the owner's decisions on cards K-34 …
K-37 and the small skill fixes, pinned as behaviour BEFORE the fix.

Contract assumed by these tests (the fix must provide exactly this shape):

- K-34: `build_plan` of an UNFINISHED record always carries
  `resume.blocked` — None when `dct release resume` can continue, else a
  dict computed by the SAME read-only predicate resume uses: `approved`
  (the changelog path the approved proposal binds) and `restore` (the very
  `dct project …` commands resume's refusal names in `details.restore`),
  plus the abandon way (`dct release abandon --version V …`). The check is
  stage-aware: while the record is `stamped` the proposal binds the path,
  later the verified manifest does (a changed setting does not block it
  then). The plan says so in `warnings` too. The dialogs (release skill +
  fallback) read `resume.blocked` and do not offer "finish" as doable.
- K-35: after a VERIFIED push, the remote-tracking ref of the pushed branch
  (found through the remote's FETCH refspec) is set to the branch tip
  OBSERVED on the destination — only when the approved URL is that
  remote's fetch URL (the same repository). The result reports it in
  `details.tracking_ref` = {ref, updated, …}; an unconfirmed mapping (the
  push URL is another repository than the fetch URL) leaves the ref alone
  and reports `updated: false` with a warning — never a failed release.
- K-36: the "— confirm it" wording of a range whose base is not the tag of
  the current version stays in the PLAN only (machine-readable:
  `range.confirm_required`); prepare and apply report the range base as
  information, never as another request to confirm.
- K-37: a version-sync script that writes into the user's checkout is
  refused with a message that NAMES the changed files (not the internal
  state keys); /dct-init never recommends wrapping a release/publish
  command, detects an existing registration first and drops the final
  "What next?" question.
- Small skill fixes: /commit has no ">= 10 unreleased entries" release
  suggestion; /track-resolve stops on a pending gate (no "proceed anyway")
  and resolves an unambiguous request without a confirmation question;
  /track-update records the reason of a transition in ONE operation —
  core `update_item(..., reason=...)` / MCP `update_item(reason=...)` write
  the note in the update's transaction.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import approved_plan_digest, git
from test_release_changelog_file_round2 import (
    backticked,
    interrupted_release,
    run_way,
)
from test_release_changelog_path import (
    SLUG as CL_SLUG,
)
from test_release_changelog_path import (
    _pretend_pre_fix_resolver,
    changelog_names,
    make_repo,
    plain_project,
    release,
)
from test_release_changelog_path import (
    proven as cl_proven,
)
from test_release_changelog_path import (
    resume as cl_resume,
)
from test_release_effects import (
    HISTORY,
    SLUG,
    Interrupted,
    apply,
    interrupt_at,
    make_release_repo,
    prepare_release,
    proven,
    remote_refs,
    resume,
)
from test_skill_prose_changelog import (
    _codex,
    _fallbacks,
    _skill,
    _workflow_fallback,
)

from dct.core.changelog import add_entry
from dct.core.items import add_note, create_item, get_item, list_notes, update_item
from dct.core.projects import set_changelog_path
from dct.core.releases import get_pending_proposal, get_release
from dct.db import projects
from dct.workflows.release import build_plan
from dct.workflows.release.effects import ReleaseRefused

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The effects layer creates commits."""


@pytest.fixture
def cli(engine, monkeypatch):
    from dct import cli as cli_module

    monkeypatch.setattr(cli_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli_module, "_get_readonly_engine", lambda: engine)
    return engine


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?;])\s+|\n\s*[-*]\s+|\n\n", text)


# ═════════════════════════════════════════════════════════════════════════════
# K-34 — the plan of an unfinished record says whether resume will refuse
# ═════════════════════════════════════════════════════════════════════════════


def _resume_plan(engine, root: Path) -> dict:
    """The plan the dialog reads for an unfinished record (no version given:
    the record's)."""
    plan = build_plan(engine, CL_SLUG, cwd=str(root))
    assert plan["kind"] == "resume", "precondition: an unfinished record"
    assert plan["resume"] is not None
    return plan


def _dogfood_record(engine, root: Path, a: int) -> None:
    """The dogfood shape: CHANGELOG.md tracked, the record stamped with an
    approved proposal that binds `changelog.md` (stamped by the pre-#876
    resolver) — resume refuses, and no setting can restore it."""
    git(root, "update-index", "--skip-worktree", "CHANGELOG.md")
    parked = root.parent / "parked-CHANGELOG.md"
    (root / "CHANGELOG.md").rename(parked)
    with pytest.MonkeyPatch.context() as mp:
        _pretend_pre_fix_resolver(mp)
        with pytest.raises(Interrupted):
            release(engine, root, [a], on_point=interrupt_at("after_stamp"))
    parked.rename(root / "CHANGELOG.md")
    git(root, "update-index", "--no-skip-worktree", "CHANGELOG.md")
    record = get_release(engine, CL_SLUG, "1.1.0")
    assert (record["status"], record["proposal"]["changelog"]["path"]) == (
        "stamped", "changelog.md"), "precondition: the dogfood record"


def _blocked(plan: dict) -> dict | None:
    assert "blocked" in plan["resume"], \
        "the resume plan does not say whether resume can continue (resume.blocked)"
    return plan["resume"]["blocked"]


def _assert_plan_matches_refusal(blocked: dict, exc: ReleaseRefused) -> None:
    """ONE predicate: the plan's block carries what resume's refusal says."""
    assert blocked is not None, "the plan does not report the block resume refuses with"
    assert blocked.get("approved") == exc.details.get("approved")
    assert blocked.get("restore") == exc.details.get("restore")
    dumped = json.dumps(blocked)
    for command in backticked(str(exc)):
        stem = command.split(" --reason")[0]
        assert stem in dumped, f"the plan does not offer the way `{command}` resume names"


def test_k34_dogfood_record_plan_reports_the_block_resume_refuses_with(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = cl_proven(engine, root, "feature a")
    _dogfood_record(engine, root, a)
    before = get_release(engine, CL_SLUG, "1.1.0")

    plan = _resume_plan(engine, root)
    blocked = _blocked(plan)
    assert blocked is not None, "a stamped record whose approved changelog file is not the " \
        "candidate's is reported as resumable"
    assert blocked["approved"] == "changelog.md"
    assert blocked["restore"] == [], "a restore that cannot finish a pre-fix record is offered"
    assert "dct release abandon --version 1.1.0" in json.dumps(blocked), \
        "the abandon way is not offered"
    assert any("changelog.md" in w and "resume" in w.lower() for w in plan["warnings"]), \
        f"the plan does not warn that resume will refuse: {plan['warnings']}"
    assert get_release(engine, CL_SLUG, "1.1.0") == before, "the plan wrote to the record"

    with pytest.raises(ReleaseRefused) as exc:
        cl_resume(engine, root)
    _assert_plan_matches_refusal(blocked, exc.value)
    assert (root / "CHANGELOG.md").read_text() == HISTORY
    assert changelog_names(root) == ["CHANGELOG.md"]


def test_k34_changed_setting_plan_offers_the_restore_and_it_unblocks(cli, tmp_path, monkeypatch):
    """A changelog setting changed after the stamp: the plan names the
    restore first (the same commands resume names); executed, the plan is no
    longer blocked and resume finishes — the two never disagree."""
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = cl_proven(engine, root, "feature a")
    interrupted_release(engine, root, a)
    set_changelog_path(engine, CL_SLUG, "CHANGES.md")

    blocked = _blocked(_resume_plan(engine, root))
    assert blocked is not None
    assert blocked["approved"] == "CHANGELOG.md"
    assert blocked["restore"] == [f"dct project changelog-path {CL_SLUG} CHANGELOG.md"]
    with pytest.raises(ReleaseRefused) as exc:
        cl_resume(engine, root)
    _assert_plan_matches_refusal(blocked, exc.value)

    for command in blocked["restore"]:
        run_way(monkeypatch, command)
    assert _blocked(_resume_plan(engine, root)) is None, "the restore did not unblock the plan"
    assert cl_resume(engine, root)["outcome"] == "completed"


def test_k34_consistent_record_is_not_blocked_and_resume_finishes(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = cl_proven(engine, root, "feature a")
    interrupted_release(engine, root, a)
    plan = _resume_plan(engine, root)
    assert _blocked(plan) is None
    assert not any("refuse" in w.lower() for w in plan["warnings"]), plan["warnings"]
    assert cl_resume(engine, root)["outcome"] == "completed"


def test_k34_after_files_written_the_manifest_binds_the_path_not_today_setting(
        engine, tmp_path):
    """Stage-aware: past 'stamped' the verified manifest binds the changelog
    file — a setting changed afterwards does not block resume, so the plan
    must not claim it does (an unconditional comparison with today's
    setting would block a correct resume)."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = cl_proven(engine, root, "feature a")
    with pytest.raises(Interrupted):
        release(engine, root, [a], on_point=interrupt_at("after_files_written"))
    assert get_release(engine, CL_SLUG, "1.1.0")["status"] == "files_written"
    set_changelog_path(engine, CL_SLUG, "CHANGES.md")
    assert _blocked(_resume_plan(engine, root)) is None
    assert cl_resume(engine, root)["outcome"] == "completed"
    assert get_release(engine, CL_SLUG, "1.1.0")["manifest"]["changelog"]["path"] == \
        "CHANGELOG.md"


def test_k34_project_outside_git_gets_the_same_block(engine, tmp_path):
    """The plan's no-checkout branch (a project CONFIRMED outside git) runs
    the same predicate: a setting changed after the stamp blocks resume, and
    the plan says so with resume's own restore."""
    plain = plain_project(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = add_entry(engine, CL_SLUG, "added", "feature a")["id"]
    with pytest.raises(Interrupted):
        release(engine, plain, [a], on_point=interrupt_at("after_stamp"))
    assert get_release(engine, CL_SLUG, "1.1.0")["status"] == "stamped"
    set_changelog_path(engine, CL_SLUG, "CHANGES.md")
    plan = _resume_plan(engine, plain)
    assert plan["checkout"]["git"] is False, "precondition: outside git"
    blocked = _blocked(plan)
    assert blocked is not None
    with pytest.raises(ReleaseRefused) as exc:
        cl_resume(engine, plain)
    _assert_plan_matches_refusal(blocked, exc.value)


# The dialog side of K-34 (release skill + fallback read `resume.blocked`)
# lives with the other Resume-path prose tests:
# tests/test_skill_prose_changelog.py::
# test_release_resume_path_reads_the_block_and_never_offers_finish_as_doable


def test_k34_workflow_doc_describes_resume_blocked():
    doc = _flat((ROOT / "docs" / "release-workflow.md").read_text(encoding="utf-8"))
    assert "resume.blocked" in doc


# ═════════════════════════════════════════════════════════════════════════════
# K-35 — the remote-tracking ref after a verified push
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def _ref(repo: Path, ref: str) -> str | None:
    out = git(repo, "for-each-ref", "--format=%(objectname)", ref)
    return out or None


def _status_line(repo: Path) -> str:
    return git(repo, "status", "-sb").splitlines()[0]


def test_k35_after_the_push_git_status_is_not_ahead(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert remote_refs(repo)["refs/heads/main"] == commit, "precondition: pushed"
    assert _ref(repo, "refs/remotes/origin/main") == commit
    assert "ahead" not in _status_line(repo), _status_line(repo)
    tracking = out["details"].get("tracking_ref")
    assert tracking is not None, "the result does not report the tracking ref"
    assert (tracking["ref"], tracking["updated"]) == ("refs/remotes/origin/main", True)


def test_k35_the_fetch_refspec_names_the_tracking_ref(engine, proj):
    """The tracking ref is found through the remote's FETCH refspec, never
    assumed to be refs/remotes/<remote>/<branch>."""
    repo, _ = proj
    git(repo, "config", "--replace-all", "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/mirror/*")
    git(repo, "fetch", "-q", "origin")
    old_default = _ref(repo, "refs/remotes/origin/main")
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert _ref(repo, "refs/remotes/mirror/main") == commit
    assert _ref(repo, "refs/remotes/origin/main") == old_default, \
        "a ref outside the fetch refspec was written"
    assert out["details"]["tracking_ref"]["ref"] == "refs/remotes/mirror/main"
    assert "ahead" not in _status_line(repo), _status_line(repo)


def test_k35_a_push_url_of_another_repository_leaves_the_tracking_ref(engine, proj, tmp_path):
    """The approved push URL is not the repository the fetch URL names:
    origin/main describes the fetch repository, which did not receive the
    release — the ref is not touched; a warning, never a failed release."""
    repo, _ = proj
    other = tmp_path / "other.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(other))
    git(repo, "remote", "set-url", "--push", "origin", str(other))
    before = _ref(repo, "refs/remotes/origin/main")
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert git(other, "rev-parse", "refs/heads/main") == commit, "precondition: pushed there"
    assert _ref(repo, "refs/remotes/origin/main") == before
    tracking = out["details"].get("tracking_ref")
    assert tracking is not None and tracking["updated"] is False, tracking
    assert any("tracking" in w.lower() or "refs/remotes/origin/main" in w
               for w in out["warnings"]), out["warnings"]


def test_k35_the_observed_tip_is_written_not_the_release_commit(engine, proj, tmp_path):
    """The branch moved on at the destination before the push (it already
    contains the release commit — only the tag goes): the tracking ref gets
    the tip the destination SHOWS, never the release SHA."""
    repo, _ = proj
    remote = tmp_path / "remote.git"
    later: dict[str, str] = {}

    def move_remote_branch(point: str) -> None:
        if point != "after_tagged":
            return
        commit = git(repo, "rev-parse", "v1.1.0^{commit}")
        later["sha"] = git(repo, "commit-tree", f"{commit}^{{tree}}", "-p", commit,
                           "-m", "later work")
        git(repo, "push", "-q", str(remote), f"{later['sha']}:refs/heads/main")

    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=move_remote_branch)
    assert out["outcome"] == "completed"
    assert remote_refs(repo)["refs/heads/main"] == later["sha"], "precondition"
    assert _ref(repo, "refs/remotes/origin/main") == later["sha"]
    assert out["details"]["tracking_ref"]["updated"] is True


def test_k35_resume_that_pushes_refreshes_the_tracking_ref(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_tagged"))
    assert get_release(engine, SLUG, "1.1.0")["status"] == "tagged"
    out = resume(engine, repo)
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert _ref(repo, "refs/remotes/origin/main") == commit
    assert "ahead" not in _status_line(repo), _status_line(repo)
    assert out["details"]["tracking_ref"]["updated"] is True


def test_k35_a_symbolic_tracking_ref_is_never_written_through(engine, proj, tmp_path):
    """refs/remotes/origin/main is a SYMBOLIC ref onto the checked-out local
    branch: `git update-ref` would follow it and move refs/heads/main — the
    name check alone is not enough. Skipped with a warning naming the target."""
    repo, _ = proj
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    git(repo, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")
    remote = tmp_path / "remote.git"
    later: dict[str, str] = {}

    def move_remote_branch(point: str) -> None:
        if point != "after_tagged":
            return
        commit = git(repo, "rev-parse", "v1.1.0^{commit}")
        later["sha"] = git(repo, "commit-tree", f"{commit}^{{tree}}", "-p", commit,
                           "-m", "later work")
        git(repo, "push", "-q", str(remote), f"{later['sha']}:refs/heads/main")

    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=move_remote_branch)
    assert out["outcome"] == "completed"
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert git(repo, "rev-parse", "refs/heads/main") == commit, \
        "the local branch was moved through the symbolic tracking ref"
    assert git(repo, "symbolic-ref", "refs/remotes/origin/main") == "refs/heads/main"
    tracking = out["details"]["tracking_ref"]
    assert tracking["updated"] is False, tracking
    assert any("symbolic" in w and "refs/heads/main" in w for w in out["warnings"]), \
        out["warnings"]


def test_k35_no_push_touches_no_tracking_ref(engine, proj):
    repo, _ = proj
    before = _ref(repo, "refs/remotes/origin/main")
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True)
    assert out["outcome"] == "completed"
    assert _ref(repo, "refs/remotes/origin/main") == before
    assert not (out["details"].get("tracking_ref") or {}).get("updated")


def test_k35_workflow_doc_describes_the_tracking_ref_refresh():
    doc = _flat((ROOT / "docs" / "release-workflow.md").read_text(encoding="utf-8"))
    assert "tracking_ref" in doc
    assert "fetch" in doc and "refs/remotes/" in doc


# ═════════════════════════════════════════════════════════════════════════════
# K-36 — the range confirmation belongs to the plan only
# ═════════════════════════════════════════════════════════════════════════════


def _range_needs_confirmation(engine) -> None:
    """current_version 1.0.1 while the nearest release tag is v1.0.0 — the
    proof range starts at another tag than the current version's."""
    with engine.begin() as conn:
        conn.execute(sa.update(projects).where(projects.c.slug == SLUG)
                     .values(current_version="1.0.1"))


def _range_confirm_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "range" in w.lower() and re.search(r"\bconfirm", w)]


def test_k36_plan_keeps_the_confirmation_and_its_machine_flag(engine, proj):
    repo, _ = proj
    _range_needs_confirmation(engine)
    a = proven(engine, repo, "feature a")
    plan = build_plan(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a])
    assert plan["range"]["confirm_required"] is True
    assert plan["range"]["base_tag"] == "v1.0.0"
    assert _range_confirm_warnings(plan["warnings"]), plan["warnings"]


def test_k36_prepare_and_apply_report_the_bound_range_as_information(engine, proj):
    repo, _ = proj
    _range_needs_confirmation(engine)
    a = proven(engine, repo, "feature a")

    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepared = prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                               plan_digest=digest)
    assert _range_confirm_warnings(prepared["warnings"]) == [], \
        f"prepare asks again for a range the plan digest already binds: {prepared['warnings']}"
    assert any("v1.0.0" in w for w in prepared["warnings"]), \
        f"prepare does not report the range base at all: {prepared['warnings']}"

    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    assert _range_confirm_warnings(out["warnings"]) == [], out["warnings"]
    assert any("v1.0.0" in w for w in out["warnings"]), out["warnings"]


def test_k36_strict_alias_still_refuses_an_unconfirmed_range(engine, proj):
    """Guard: the strict alias path (no plan digest) keeps refusing a range
    that needs confirmation — the wording change never weakens it."""
    repo, _ = proj
    _range_needs_confirmation(engine)
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="proof range starts at v1.0.0"):
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                        strict=True)
    assert get_pending_proposal(engine, SLUG) is None


def test_k36_digest_still_binds_the_range(engine, proj):
    """Guard: a range base that changes after the approval voids the digest."""
    repo, _ = proj
    _range_needs_confirmation(engine)
    a = proven(engine, repo, "feature a")

    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    git(repo, "tag", "v1.0.1")
    with pytest.raises(ReleaseRefused, match="differs from the approved plan"):
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                        plan_digest=digest)


# ═════════════════════════════════════════════════════════════════════════════
# K-37 — the version sync changes only version files; /dct-init
# ═════════════════════════════════════════════════════════════════════════════


def test_k37_checkout_changed_refusal_names_the_changed_files(engine, proj):
    repo, _ = proj
    scripts = repo / "scripts" / "dct-version-sync.sh"
    scripts.write_text(
        "#!/bin/bash\nset -e\n"
        'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n'
        f'echo published >> "{repo}/pyproject.toml"\n'
        f'echo leaked > "{repo}/RELEASED.txt"\n')
    git(repo, "add", "scripts/dct-version-sync.sh")
    git(repo, "commit", "-q", "-m", "chore: a sync script that publishes")
    a = proven(engine, repo, "feature a")

    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(ReleaseRefused) as exc:
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                        plan_digest=digest)
    message = str(exc.value)
    assert "pyproject.toml" in message and "RELEASED.txt" in message, \
        f"the refusal does not name the files the script changed: {message}"
    assert "nothing changed" not in message.lower()
    assert "checkout_changed" in exc.value.details
    assert get_pending_proposal(engine, SLUG) is None


def _init() -> str:
    return _skill("dct-init")


def test_k37_dct_init_never_recommends_wrapping_a_release_command():
    body = _init()
    assert not re.search(r'"Wrap\b', body), "dct-init still offers to wrap an existing script"
    recommended = [line for line in body.splitlines()
                   if re.search(r"\bwrap", line, re.IGNORECASE) and "recommended" in line]
    assert recommended == [], f"wrapping is still recommended: {recommended}"
    assert "`release:`" not in body, "a `release:` make/just target is still a sync candidate"


def _warns_against_wrapping(text: str) -> bool:
    return any(re.search(r"\b(never|not|don't|must not)\b", s, re.IGNORECASE)
               and re.search(r"\bwrap", s, re.IGNORECASE)
               and re.search(r"\b(release|publish)", s, re.IGNORECASE)
               for s in _sentences(text))


def _only_version_files(text: str) -> bool:
    return any(re.search(r"\bonly\b", s, re.IGNORECASE)
               and re.search(r"version[- ]files?", s, re.IGNORECASE)
               for s in _sentences(text))


@pytest.mark.parametrize("text", [
    pytest.param(_init, id="dct-init"),
    pytest.param(_fallbacks, id="fallbacks"),
])
def test_k37_docs_require_a_script_that_changes_only_version_files(text):
    body = text()
    assert _only_version_files(body), "no requirement that the sync changes ONLY version files"
    assert _warns_against_wrapping(body), "no warning against wrapping a release/publish command"


@pytest.mark.parametrize("text", [
    pytest.param(_init, id="claude"),
    pytest.param(lambda: _codex("dct-init"), id="codex"),
    pytest.param(lambda: _workflow_fallback("dct-init"), id="fallback"),
])
def test_k37_dct_init_detects_the_registration_before_any_setup_question(text):
    body = text()
    step2 = body.index("Step 2: Probe current project registration")
    probe = body.index("Call `mcp__dct__list_projects`", step2)
    for setup in ("Conflicting project artifacts", "version-sync script"):
        assert setup in body, f"precondition: {setup!r} still described"
        assert probe < body.index(setup, step2), \
            f"the registration is probed after the setup step {setup!r}"
    if text is _init:
        assert "permissions.allow" in body[step2:]
    else:
        assert "permissions.allow" not in body
    assert "Project is already registered. What now?" not in body


def test_k37_dct_init_drops_the_final_what_next_question():
    body = _init()
    assert "What next?" not in body
    assert "Propose a concrete next action" not in body


# ═════════════════════════════════════════════════════════════════════════════
# Small skill fixes
# ═════════════════════════════════════════════════════════════════════════════


# The /commit threshold and the /track-update reason prose live with the
# other prose tests of those skills (tests/test_skill_prose_changelog.py).


def _resolve_fallback() -> str:
    return _workflow_fallback("track-resolve")


RESOLVE_DIALOGS = [
    pytest.param(lambda: _skill("track-resolve"), id="skill"),
    pytest.param(_resolve_fallback, id="fallback"),
]


@pytest.mark.parametrize("text", RESOLVE_DIALOGS)
def test_track_resolve_stops_on_a_pending_gate_and_never_proceeds_anyway(text):
    flat = _flat(text())
    assert "proceed anyway" not in flat.lower()
    stop = [s for s in _sentences(flat) if re.search(r"\bgate", s, re.IGNORECASE)
            and re.search(r"\bstop", s, re.IGNORECASE)]
    assert stop, "a pending gate is not a STOP"
    assert re.search(r"complete_checkpoint|reject_checkpoint", flat), \
        "the stop does not name what clears the gate"


@pytest.mark.parametrize("text", RESOLVE_DIALOGS)
def test_track_resolve_resolves_an_unambiguous_request_without_a_question(text):
    flat = _flat(text())
    assert re.search(r"(without|no) (a |any )?confirmation", flat, re.IGNORECASE), \
        "an unambiguous resolve still asks for confirmation"
    assert re.search(r"one line", flat, re.IGNORECASE), "no one-line acknowledgement"


def test_track_resolve_skill_has_no_unconditional_confirm_step():
    assert not re.search(r"^### Step \d+: Confirm\b", _skill("track-resolve"), re.MULTILINE)


# ── core / MCP: update_item(reason=...) — one operation, one transaction ────


@pytest.fixture
def item(engine, seed_project):
    return create_item(engine, "test", "issue", "Parser crashes on empty files")


def test_update_with_reason_writes_the_note_and_the_change(engine, item):
    out = update_item(engine, item["id"], priority="P1", reason="blocking v0.3")
    assert out["priority"] == "P1"
    assert [n["note"] for n in list_notes(engine, item["id"])] == ["blocking v0.3"]


def test_update_refused_inside_the_transaction_writes_no_note(engine, item, seed_project):
    """A tag confusable with an existing one rolls the WHOLE update back —
    the reason must not survive as an orphan note."""
    create_item(engine, "test", "todo", "other", tags=["tests"])
    with pytest.raises(ValueError):
        update_item(engine, item["id"], priority="P1", tags=["test"], reason="why")
    assert get_item(engine, item["id"])["priority"] != "P1"
    assert list_notes(engine, item["id"]) == []


def test_update_with_an_invalid_field_writes_no_note(engine, item):
    with pytest.raises(ValueError):
        update_item(engine, item["id"], status="bogus", reason="why")
    assert list_notes(engine, item["id"]) == []


def test_update_of_an_unknown_item_with_reason_writes_nothing(engine, item):
    assert update_item(engine, 999_999, priority="P1", reason="why") is None
    assert list_notes(engine, item["id"]) == []


def test_reason_alone_is_not_an_update(engine, item):
    with pytest.raises(ValueError):
        update_item(engine, item["id"], reason="only a reason")
    assert list_notes(engine, item["id"]) == []


def test_update_without_reason_writes_no_note(engine, item):
    add_note(engine, item["id"], "earlier")
    update_item(engine, item["id"], priority="P3")
    assert [n["note"] for n in list_notes(engine, item["id"])] == ["earlier"]


def test_mcp_update_item_takes_the_reason_in_one_call(srv, engine):
    created = srv.create_item(item_type="issue", title="Parser crashes", project="test")
    ack = srv.update_item(item_id=created["id"], status="in_progress",
                          reason="reproducible on 0-byte input")
    assert "error" not in ack, ack
    assert ack["status"] == "in_progress"
    assert [n["note"] for n in list_notes(engine, created["id"])] == [
        "reproducible on 0-byte input"]


def test_mcp_update_item_docstring_documents_reason(srv):
    import inspect

    assert "reason" in inspect.signature(srv.update_item).parameters
    assert re.search(r"^\s+reason:", srv.update_item.__doc__, re.MULTILINE)


# ── edge cases added with the fix ───────────────────────────────────────────


@pytest.mark.parametrize(("refspecs", "expected"), [
    pytest.param(["+refs/heads/*:refs/remotes/origin/*"],
                 ([("refs/remotes/origin/main", True)], False), id="default-glob"),
    pytest.param(["refs/heads/main:refs/remotes/origin/main"],
                 ([("refs/remotes/origin/main", False)], False), id="exact-not-forced"),
    pytest.param(["+refs/heads/dev:refs/remotes/origin/dev"], ([], False), id="other-branch"),
    pytest.param(["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/main"],
                 ([("refs/remotes/origin/main", True)], True), id="negative-excludes"),
    pytest.param(["+refs/heads/*:refs/remotes/a/*", "+refs/heads/m*:refs/remotes/b/m*"],
                 ([("refs/remotes/a/main", True), ("refs/remotes/b/main", True)], False),
                 id="two-destinations"),
    pytest.param(["+refs/heads/main"], ([], False), id="no-destination"),
    pytest.param(["+refs/heads/*/*:refs/remotes/o/*/*"], ([], False), id="two-stars-ignored"),
])
def test_k35_fetch_refspec_mapping(refspecs, expected):
    from dct.workflows.release.effects import _tracking_refs

    assert _tracking_refs(refspecs, "refs/heads/main") == expected


def test_k35_two_tracking_destinations_update_nothing_and_warn(engine, proj):
    """The fetch refspecs map the branch to two local refs: which one the
    owner reads is unknown — neither is written, a warning says so."""
    repo, _ = proj
    git(repo, "config", "--add", "remote.origin.fetch", "+refs/heads/main:refs/remotes/extra/main")
    before = _ref(repo, "refs/remotes/origin/main")
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    assert _ref(repo, "refs/remotes/origin/main") == before
    assert out["details"]["tracking_ref"]["updated"] is False
    assert any("several refs" in w for w in out["warnings"]), out["warnings"]


def test_k37_plain_project_refusal_names_the_changed_files(engine, tmp_path):
    """Outside git the isolated copy is compared with the project directory:
    a script that writes into the project directory itself is refused with
    the files named."""
    plain = plain_project(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    script = plain / "scripts" / "dct-version-sync.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f'#!/bin/bash\nset -e\necho leaked > "{plain}/RELEASED.txt"\n')
    a = add_entry(engine, CL_SLUG, "added", "feature a")["id"]
    digest = approved_plan_digest(engine, CL_SLUG, plain, version="1.1.0", entry_ids=[a])
    with pytest.raises(ReleaseRefused) as exc:
        prepare_release(engine, CL_SLUG, cwd=str(plain), version="1.1.0", entry_ids=[a],
                        plan_digest=digest)
    assert "RELEASED.txt" in str(exc.value)
    assert exc.value.details["checkout_changed"] == ["RELEASED.txt"]


def test_update_with_a_blank_reason_is_refused_and_writes_nothing(engine, item):
    with pytest.raises(ValueError):
        update_item(engine, item["id"], priority="P1", reason="   ")
    assert get_item(engine, item["id"])["priority"] != "P1"
    assert list_notes(engine, item["id"]) == []


def test_mcp_update_item_with_a_blank_reason_returns_an_error(srv, engine):
    created = srv.create_item(item_type="issue", title="Parser crashes", project="test")
    ack = srv.update_item(item_id=created["id"], priority="P1", reason="   ")
    assert "error" in ack
    assert list_notes(engine, created["id"]) == []


def test_cli_update_reason_writes_the_note(cli, item, monkeypatch):
    run_way(monkeypatch, f"dct update {item['id']} --priority P1 --reason 'blocks the beta'")
    assert get_item(cli, item["id"])["priority"] == "P1"
    assert [n["note"] for n in list_notes(cli, item["id"])] == ["blocks the beta"]


# ═════════════════════════════════════════════════════════════════════════════
# Delta review of the phase-1 fix — F1 … F6 (failing first)
# ═════════════════════════════════════════════════════════════════════════════


# ── F2 (K-37): the refusal names the refs and the staged files ─────────────


def _commit_sync_script(repo: Path, body: str) -> None:
    """A committed version sync that edits pyproject.toml, then runs `body`."""
    (repo / "scripts" / "dct-version-sync.sh").write_text(
        "#!/bin/bash\nset -e\n"
        'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n'
        + body)
    git(repo, "add", "scripts/dct-version-sync.sh")
    git(repo, "commit", "-q", "-m", "chore: a sync script that does more")


def _prepare_refusal(engine, repo: Path) -> ReleaseRefused:
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(ReleaseRefused) as exc:
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                        plan_digest=digest)
    assert "checkout_changed" in exc.value.details, "precondition: the checkout refusal"
    assert get_pending_proposal(engine, SLUG) is None
    return exc.value


def _names_state_keys_as_refs(message: str) -> bool:
    return bool(re.search(r"ref\(s\)[^;]*\b(before|after)\b", message))


def test_k37_a_sync_that_tags_is_refused_with_the_tag_named(engine, proj):
    """`git tag` inside the isolated worktree writes the SHARED ref store:
    the refusal names refs/tags/<tag>, never the diff's internal keys."""
    repo, _ = proj
    _commit_sync_script(repo, "git tag sneaky-tag\n")
    exc = _prepare_refusal(engine, repo)
    message = str(exc)
    assert git(repo, "tag", "--list", "sneaky-tag") == "sneaky-tag", "precondition: tagged"
    assert "refs/tags/sneaky-tag" in message, f"the refusal does not name the tag: {message}"
    assert not _names_state_keys_as_refs(message), \
        f"the refusal names the state keys before/after as refs: {message}"


def test_k37_a_sync_that_commits_in_the_checkout_names_the_file_and_the_branch(engine, proj):
    """The script adds and commits a file in the user's checkout: the status
    stays clean, only HEAD, the branch ref and the INDEX change — the
    refusal must still name the file (from the index) and the moved ref."""
    repo, _ = proj
    _commit_sync_script(
        repo,
        f'echo leaked > "{repo}/SNEAKY.txt"\n'
        f'git -C "{repo}" add SNEAKY.txt\n'
        f'git -C "{repo}" -c user.name=x -c user.email=x@example.invalid '
        f'commit -q --no-verify -m sneaky\n')
    exc = _prepare_refusal(engine, repo)
    message = str(exc)
    assert git(repo, "log", "-1", "--format=%s") == "sneaky", "precondition: committed"
    assert git(repo, "status", "--porcelain") == "", "precondition: an index-only change"
    assert "SNEAKY.txt" in message, f"the refusal does not name the staged file: {message}"
    assert "refs/heads/main" in message, f"the refusal does not name the moved branch: {message}"
    assert not _names_state_keys_as_refs(message), message


def test_k37_a_sync_that_stages_in_the_checkout_names_the_file(engine, proj):
    """Guard: a file staged in the user's checkout is named."""
    repo, _ = proj
    _commit_sync_script(repo, f'echo leaked > "{repo}/STAGED.txt"\n'
                              f'git -C "{repo}" add STAGED.txt\n')
    message = str(_prepare_refusal(engine, repo))
    assert "STAGED.txt" in message, message
    assert not _names_state_keys_as_refs(message), message


def test_k37_describe_checkout_change_reads_what_git_state_produces(proj):
    """Producer -> consumer: _describe_checkout_change reads what
    _state_diff(_git_state(before), _git_state(after)) produces — a new tag,
    a moved branch and a committed file are named; unchanged refs and files
    are not."""
    from dct.workflows.release.effects import (
        _describe_checkout_change,
        _git_state,
        _state_diff,
    )

    repo, _ = proj
    before = _git_state(str(repo))
    git(repo, "tag", "sneaky")
    (repo / "SNEAKY.txt").write_text("leaked\n")
    git(repo, "add", "SNEAKY.txt")
    git(repo, "commit", "-q", "-m", "sneaky")
    text = _describe_checkout_change(_state_diff(before, _git_state(str(repo))))
    assert "refs/heads/main" in text and "refs/tags/sneaky" in text, text
    assert "refs/tags/v1.0.0" not in text, f"an unchanged ref is reported: {text}"
    assert "SNEAKY.txt" in text, text
    assert "pyproject.toml" not in text, f"an unchanged file is reported: {text}"
    assert not _names_state_keys_as_refs(text), text


# ── F3 (K-35): a git READ failure in the refresh is a warning ──────────────


def test_k35_a_git_read_failure_in_the_refresh_completes_with_a_warning(
        engine, proj, monkeypatch):
    """`merge-base --is-ancestor` fails (GitReadError: a timeout) while the
    tracking ref is refreshed — AFTER the record is 'pushed'. The release
    completes; the ref is left and a warning says so (never an unfinished
    release that every resume repeats)."""
    from dct.core import gitinfo

    repo, _ = proj
    # A fetch refspec without `+`: the refresh must check the ancestry.
    git(repo, "config", "--replace-all", "remote.origin.fetch",
        "refs/heads/main:refs/remotes/origin/main")
    before = _ref(repo, "refs/remotes/origin/main")
    real = gitinfo.git_is_ancestor
    armed = {"on": False, "raised": 0}

    def flaky(*args, **kwargs):
        if armed["on"]:
            armed["on"] = False
            armed["raised"] += 1
            raise gitinfo.GitReadError("`git merge-base --is-ancestor` timed out")
        return real(*args, **kwargs)

    monkeypatch.setattr(gitinfo, "git_is_ancestor", flaky)

    def arm(point: str) -> None:
        if point == "after_pushed":
            armed["on"] = True

    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=arm)
    assert armed["raised"] == 1, "precondition: the refresh read the ancestry"
    assert out["outcome"] == "completed", out
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"
    assert _ref(repo, "refs/remotes/origin/main") == before
    assert out["details"]["tracking_ref"]["updated"] is False
    assert any("refs/remotes/origin/main" in w and "git fetch" in w for w in out["warnings"]), \
        out["warnings"]


# ── F4 (K-35): negative GLOB refspecs exclude the branch ───────────────────


@pytest.mark.parametrize(("refspecs", "source", "expected"), [
    pytest.param(["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/release/*"],
                 "refs/heads/release/1.x",
                 ([("refs/remotes/origin/release/1.x", True)], True), id="negative-glob"),
    pytest.param(["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/ma*"],
                 "refs/heads/main",
                 ([("refs/remotes/origin/main", True)], True), id="negative-glob-prefix"),
    pytest.param(["^refs/heads/*", "+refs/heads/*:refs/remotes/origin/*"],
                 "refs/heads/main",
                 ([("refs/remotes/origin/main", True)], True), id="negative-glob-first"),
    pytest.param(["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/release/*"],
                 "refs/heads/main",
                 ([("refs/remotes/origin/main", True)], False), id="negative-glob-other"),
    pytest.param(["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/release/*"],
                 "refs/heads/release",
                 ([("refs/remotes/origin/release", True)], False), id="negative-glob-no-slash"),
])
def test_k35_negative_glob_refspec_mapping(refspecs, source, expected):
    from dct.workflows.release.effects import _tracking_refs

    assert _tracking_refs(refspecs, source) == expected


def test_k35_a_negative_glob_refspec_leaves_the_tracking_ref(engine, proj):
    """`git fetch` does not maintain a ref a negative glob excludes — dct
    must not write it either."""
    repo, _ = proj
    git(repo, "config", "--add", "remote.origin.fetch", "^refs/heads/ma*")
    before = _ref(repo, "refs/remotes/origin/main")
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["outcome"] == "completed"
    assert _ref(repo, "refs/remotes/origin/main") == before, "an excluded ref was written"
    assert not (out["details"].get("tracking_ref") or {}).get("updated")


# ── F5 (K-35): only a fast-forward, only under refs/remotes/ ───────────────


def test_k35_the_refresh_never_moves_the_tracking_ref_backwards(engine, proj, tmp_path):
    """A `git fetch` between the push proof (ls-remote) and the write moved
    origin/main to newer work: the tip dct observed is OLDER — writing it
    would move the ref backwards, whatever the `+` of the refspec."""
    repo, _ = proj
    remote = tmp_path / "remote.git"
    newer: dict[str, str] = {}

    def fetch_meanwhile(point: str) -> None:
        if point != "after_pushed":
            return
        commit = git(repo, "rev-parse", "v1.1.0^{commit}")
        newer["sha"] = git(repo, "commit-tree", f"{commit}^{{tree}}", "-p", commit,
                           "-m", "newer work")
        git(repo, "push", "-q", str(remote), f"{newer['sha']}:refs/heads/main")
        git(repo, "fetch", "-q", "origin")

    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=fetch_meanwhile)
    assert out["outcome"] == "completed"
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert out["details"]["tracking_ref"]["tip"] == commit, "precondition: the older tip"
    assert _ref(repo, "refs/remotes/origin/main") == newer["sha"], \
        "the tracking ref was moved backwards to the observed tip"
    assert out["details"]["tracking_ref"]["updated"] is False


def test_k35_a_mirror_refspec_never_writes_a_local_branch(engine, proj, tmp_path):
    """`+refs/*:refs/*` maps the pushed branch onto the LOCAL refs/heads/main:
    dct writes only under refs/remotes/ — the user's branch (and HEAD) stay
    where the release left them; a warning says `git fetch` settles it."""
    repo, _ = proj
    git(repo, "config", "--replace-all", "remote.origin.fetch", "+refs/*:refs/*")
    remote = tmp_path / "remote.git"
    later: dict[str, str] = {}

    def move_remote_branch(point: str) -> None:
        if point != "after_tagged":
            return
        commit = git(repo, "rev-parse", "v1.1.0^{commit}")
        later["sha"] = git(repo, "commit-tree", f"{commit}^{{tree}}", "-p", commit,
                           "-m", "later work")
        git(repo, "push", "-q", str(remote), f"{later['sha']}:refs/heads/main")

    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=move_remote_branch)
    assert out["outcome"] == "completed"
    commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    assert remote_refs(repo)["refs/heads/main"] == later["sha"], "precondition"
    assert git(repo, "rev-parse", "refs/heads/main") == commit, \
        "the release force-wrote the user's local branch"
    assert git(repo, "rev-parse", "HEAD") == commit
    assert out["details"]["tracking_ref"]["updated"] is False
    assert any("git fetch" in w for w in out["warnings"]), out["warnings"]


def test_k35_workflow_doc_states_fast_forward_and_refs_remotes_only():
    doc = _flat((ROOT / "docs" / "release-workflow.md").read_text(encoding="utf-8"))
    assert re.search(r"fast-forward", doc, re.IGNORECASE) and \
        re.search(r"tracking[^.]{0,300}(never|not)[^.]{0,40}forc|"
                  r"(never|not)[^.]{0,40}forc[^.]{0,300}tracking", doc, re.IGNORECASE), \
        "the doc does not state that the tracking ref is only fast-forwarded, never forced"
    assert re.search(r"only[^.]{0,120}`?refs/remotes/`?", doc), \
        "the doc does not state that only refs under refs/remotes/ are written"


# ── F1 (K-37): /dct-init checks the sync script of a registered project ────
# The owner's decisions of 2026-10-12: a registered project's /dct-init reads
# and SHOWS its configuration (allowlist, version sync, commit-line
# preferences) without reconfiguring it (K-67 (4)); the commit-line
# preferences are asked ONCE and stored (K-55); no "Leave NULL" a detected
# version would overwrite (K-67 (3)); scripts/version-sync.sh is run by the
# release, scripts/dct-version-sync.sh takes precedence (K-64).


def _init_section(start: str, end: str) -> str:
    body = _flat(_init())
    at = body.index(start)
    return body[at:body.index(end, at)]


def _registered() -> str:
    return _init_section("If matched", "If no match")


def test_dct_init_shows_a_registered_projects_configuration_without_reconfiguring():
    part = _registered()
    for shown in ("permissions.allow", "scripts/dct-version-sync.sh", "scripts/version-sync.sh",
                  "commit_co_authored_by", "commit_session_link"):
        assert shown in part, f"the registered status does not show {shown}"
    assert re.search(r"\bnot answered\b", part)
    assert re.search(r"\bnothing is reconfigured\b", part, re.IGNORECASE), part
    assert "dct project add" not in part
    for m in re.finditer(r"AskUserQuestion|\bask\b", part):
        around = part[max(0, m.start() - 200):m.end() + 200]
        assert re.search(r"not answered", around), f"a registered project gets a setup question: {around}"


def _commit_lines_question() -> str:
    body = _flat(_init())
    found = [m.start() for m in re.finditer(r'header: "Commit lines"', body)]
    assert len(found) == 1, "not exactly one commit-lines question"
    return body[max(0, found[0] - 600):found[0] + 900]


def test_dct_init_asks_the_commit_lines_once_and_stores_them():
    from dct.cli import build_parser

    question = _commit_lines_question()
    assert "Co-Authored-By" in question and re.search(r"session link", question)
    stored = re.search(r"dct project trailers <slug> --co-authored-by (on\|off|<[^>]+>) "
                       r"--session-link (on\|off|<[^>]+>)", _flat(_init()))
    assert stored, "the answer is not stored with `dct project trailers`"
    args = build_parser().parse_args(["project", "trailers", "proj-a", "--co-authored-by", "on",
                                      "--session-link", "off"])
    assert (args.co_authored_by, args.session_link) == ("on", "off")
    body = _flat(_init())
    assert re.search(r"\bonce\b", question) and re.search(
        r"(answered|stored)[^.]{0,120}\bnever asked again\b|\bnever asked again\b[^.]{0,120}"
        r"(answered|stored)", body), "the question is not limited to an unanswered preference"
    assert re.search(r"own rules[^.]{0,160}(win|forbid)", body), \
        "a project's own rules (e.g. a session-link ban) do not win over a stored answer"


@pytest.mark.parametrize("text", [
    pytest.param(_init, id="claude"),
    pytest.param(lambda: _codex("dct-init"), id="codex"),
    pytest.param(lambda: _workflow_fallback("dct-init"), id="fallback"),
])
def test_dct_init_never_promises_null_for_a_detected_version(text):
    """`dct project add` stores the detected version when --version is left
    out, so nothing may promise an unset one where a version is detected:
    the registration passes the version the "Register" preview showed, and
    leaves --version out only when none was detected or given."""
    body = _flat(text())
    at = body.index("Always pass the version")
    registration = body[at:body.index("Run via", at)]
    assert not re.search(r"\bNULL\b|\bunset\b|\bleave (it )?empty\b", registration,
                         re.IGNORECASE), registration
    assert re.search(r"no version detected or given, leave `--version` out", registration)
    assert not re.search(r"Leave NULL", body)


def _case2() -> str:
    return _init_section("Case 2", "Case 3")


def test_dct_init_tells_the_truth_about_scripts_version_sync_sh():
    """K-64: the release runs scripts/version-sync.sh itself (when there is
    no scripts/dct-version-sync.sh, which takes precedence), before the
    screen — so it must change only the version files; for it no option
    says "the release bumps no files", and "Skip" skips the configuration,
    it does not switch the script off."""
    part = _case2()
    exact = part[part.index("exactly `scripts/version-sync.sh`"):]
    exact = exact[:exact.index("Any other name")]
    assert re.search(r"\b(runs|run) it\b[^.]{0,120}\bautomatically\b|\bautomatically\b[^.]{0,120}"
                     r"\bruns\b", exact), exact
    assert re.search(r"dct-version-sync\.sh[^.]{0,60}takes precedence", exact)
    assert re.search(r"\bonly\b[^.]{0,40}version files", exact)
    assert "bumps no files" not in exact
    skip = re.search(r'"Skip[^"]*"[^"]{0,300}', exact)
    assert skip and re.search(r"\b(still|keeps?)\b[^.]{0,80}\bruns?\b|does not (switch|turn)",
                              skip.group(0)), skip and skip.group(0)
    assert re.search(r"never overwrit|not overwrit", _flat(_init())), \
        "generating the script may overwrite an existing one"


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_fallbacks, id="fallbacks"),
])
def test_the_release_dialog_never_reads_the_version_script_before_each_preview(text):
    """K-64: the rejected duty to read the script before every preview stays
    out — the isolation and the checkout check are the guard."""
    assert not re.search(r"\bread\b[^.]{0,60}(version script|version-sync)[^.]{0,80}\bbefore\b"
                         r"[^.]{0,40}preview", _flat(text()), re.IGNORECASE)
