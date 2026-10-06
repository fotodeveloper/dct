"""#876 round 4 — the owner's decisions on the final review of the
changelog-file fix (cards K-26 … K-30), pinned as behaviour BEFORE the fix.

Contract assumed by these tests:

- K-26 (decision a): the expected work-tree bytes of the changelog are the
  CHECKOUT FORM OF WHAT GIT STORES for the release commit — what a fresh
  checkout of that commit writes. After every release the work tree equals
  a fresh clone's file, `git status` is clean (stat cache busted) and the
  NEXT plan's changelog pre-check passes; two consecutive releases both
  complete. Covered: a CRLF changelog under `* text=auto` (the regression),
  `*.md text=auto eol=crlf`, `ident`, and the plain LF case (dct's own
  repository — it behaves exactly as before: the work tree is the blob).
- K-27: `ambiguous` always names a next step — a link candidate whose
  target is inadmissible is dropped from the choices, and then that link's
  replace-link way and the off way are offered; every offered command
  leads to a resolvable state.
- K-28 (case-insensitive file systems): a `--detect` way is offered only
  when the detected path also passes the work-tree check; a directory found
  at the resolved path through a case-insensitive match is its own problem
  (`not_a_file`) with true ways — never not_baseline "move it away (or
  commit it)" pointing at a TRACKED directory.
- K-29: `core.safecrlf=true` never stops dct's own id computations — a CRLF
  changelog under `text=auto` releases completely.
- K-30: the refusal tables of SKILL.md and the fallbacks split
  `details.index_lock` into the pre-stamp case (run the same apply again)
  and the post-stamp case (resume).
- Round-3 report, same class as K-24 (2): outside git the `content`
  problem's ways name no git step and no commit.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import UTC
from pathlib import Path

import pytest
from conftest import approved_plan_digest, approved_proposal_digest, approved_push, git
from test_release_changelog_file_round2 import (
    LATIN1,
    PREWRITTEN,
    blob_bytes,
    commit_files,
    run_way,
)
from test_release_changelog_file_round3 import (
    CRLF_HISTORY,
    TEXT_AUTO,
    _no_git_steps,
    assert_refused_before_the_stamp,
    crlf_text_auto_repo,
    fresh_checkout,
    snapshot,
    status,
)
from test_release_changelog_path import (
    SLUG,
    assert_completed_into,
    commands,
    expected_text,
    fs_case_sensitive,
    make_repo,
    plain_project,
    prepare,
    proven,
    release,
)
from test_release_effects import HISTORY, released_section

from dct.core.changelog import add_entry, list_entries
from dct.core.projects import (
    get_project,
    reset_changelog_path,
    set_changelog_path,
    set_manages_changelog,
)
from dct.core.releases import get_release
from dct.workflows.release import build_plan
from dct.workflows.release.effects import ReleaseRefused, apply_release

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

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


# ── helpers: releases of any version ─────────────────────────────────────────


def plan_v(engine, root: Path, ids: list[int], version: str) -> dict:
    return build_plan(engine, SLUG, cwd=str(root), version=version, entry_ids=ids)


def release_v(engine, root: Path, ids: list[int], version: str) -> dict:
    """apply of `version` with the owner's approvals (plan, proposal, push)."""
    plan = approved_plan_digest(engine, SLUG, root, version=version, entry_ids=ids)
    proposal = approved_proposal_digest(engine, SLUG, root, plan_digest=plan, version=version,
                                        entry_ids=ids)
    return apply_release(engine, SLUG, cwd=str(root), version=version, entry_ids=ids,
                         confirm=True, plan_digest=plan, proposal_digest=proposal,
                         **approved_push(root))


def date_of(engine, version: str) -> str:
    created = get_release(engine, SLUG, version)["created_at"]
    if created.tzinfo is not None:
        created = created.astimezone(UTC)
    return created.date().isoformat()


def history_with(engine, history: str, released: list[tuple[str, str]]) -> str:
    """`history` (a '# Changelog' document, LF) with the sections of
    `released` [(version, entry text)] inserted, newest first."""
    head = "# Changelog\n\n"
    assert history.startswith(head)
    sections = "".join(released_section(date_of(engine, v), v, f"- {text}\n") + "\n"
                       for v, text in reversed(released))
    return head + sections + history[len(head):]


def assert_release_completed(engine, root: Path, rel: str, version: str, ids: list[int],
                             out: dict) -> None:
    """The release of `version` is complete as dct's ledger and git see it."""
    assert out["outcome"] == "completed", out
    record = get_release(engine, SLUG, version)
    assert record["status"] == "completed" and record["entry_ids"] == sorted(ids)
    assert record["manifest"]["changelog"]["path"] == rel
    assert get_project(engine, SLUG)["current_version"] == version
    stamped = {e["id"]: e["version"] for e in list_entries(engine, SLUG) if e["id"] in ids}
    assert stamped == {i: version for i in ids}
    assert git(root, "rev-parse", f"v{version}^{{commit}}") == git(root, "rev-parse", "HEAD")


def assert_like_a_fresh_checkout(root: Path, rel: str) -> None:
    """The work tree's changelog is EXACTLY what a fresh clone of HEAD
    writes (K-26 decision a), and git sees no difference once its stat
    cache is busted."""
    path = root / rel
    fresh = fresh_checkout(root, rel)
    assert path.read_bytes() == fresh, (
        f"the work tree's {rel} is not what a fresh checkout of the release commit writes "
        f"(work {path.read_bytes()[:60]!r}… vs fresh {fresh[:60]!r}…)")
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    assert status(root) == "", f"git sees a difference after the release: {status(root)!r}"


# ── K-26: the work tree is the checkout form of what git stores ──────────────


IDENT_HISTORY = HISTORY.replace("- first\n", "- first $Id$\n")

# name → (changelog file, its committed bytes, .gitattributes added LATER or None,
#         the history text the blob holds once normalised)
LAYOUTS = {
    "crlf_text_auto": ("CHANGELOG.md", CRLF_HISTORY, TEXT_AUTO["text_auto"], HISTORY),
    "crlf_md_text_auto_eol_crlf": ("CHANGELOG.md", CRLF_HISTORY,
                                   TEXT_AUTO["md_text_auto_eol_crlf"], HISTORY),
    "lf_ident": ("CHANGELOG.md", IDENT_HISTORY.encode(), "CHANGELOG.md ident\n",
                 IDENT_HISTORY),
    # dct's own repository: a lowercase LF changelog.md, no attributes
    "lf_plain": ("changelog.md", HISTORY.encode(), None, HISTORY),
}


def layout_repo(engine, tmp_path, kind: str) -> tuple[Path, str, str]:
    """(project root, changelog rel, history text) of a LAYOUTS repository —
    clean, and already in the form a fresh checkout writes."""
    rel, data, attributes, history = LAYOUTS[kind]
    root, _ = make_repo(engine, tmp_path, {})
    commit_files(root, {rel: data}, message="docs: changelog")
    if attributes:
        commit_files(root, {".gitattributes": attributes}, message="chore: attributes")
        # the owner's checkout after the attributes landed (ident expands
        # $Id$ only on checkout; a CRLF blob is written back as it is)
        (root / rel).unlink()
        git(root, "checkout", "HEAD", "--", rel)
    assert status(root) == "", f"precondition: a clean work tree ({status(root)!r})"
    assert (root / rel).read_bytes() == fresh_checkout(root, rel), \
        "precondition: the starting work tree is what a fresh checkout writes"
    return root, rel, history


def release_and_check(engine, root: Path, rel: str, history: str, version: str,
                      released: list[tuple[str, str]], text: str) -> list[tuple[str, str]]:
    """Plan (its changelog pre-check passes), release `version` with a new
    proven entry `text`, then assert the complete state: ledger, the
    committed text, the work tree = a fresh checkout, git status clean."""
    a = proven(engine, root, text)
    found = plan_v(engine, root, [a], version)["changelog_file"]
    assert found["problem"] is None and found["path"] == rel, \
        f"the plan of {version} refuses the changelog left by the previous release: {found}"
    out = release_v(engine, root, [a], version)
    assert_release_completed(engine, root, rel, version, [a], out)
    released = [*released, (version, text)]
    expected = history_with(engine, history, released)
    blob = blob_bytes(root, f"HEAD:{rel}").decode()
    assert blob.replace("\r\n", "\n") == expected, f"the release commit of {version} " \
        f"does not hold the history with every released section"
    assert_like_a_fresh_checkout(root, rel)
    return released


@pytest.mark.parametrize("kind", sorted(LAYOUTS))
def test_k26_consecutive_releases_each_leave_a_fresh_checkout(engine, tmp_path, kind):
    """Review repro (both reviewers): under `* text=auto` with a CRLF blob,
    release 1.1.0 completed but left the work tree CRLF while git stored
    LF; the plan of 1.2.0 then refused it as not_baseline / `unfiltered`
    (on main it went through). Every layout: three plans pass and two
    releases complete, each leaving exactly a fresh checkout."""
    root, rel, history = layout_repo(engine, tmp_path, kind)
    released = release_and_check(engine, root, rel, history, "1.1.0", [], "feature a")
    released = release_and_check(engine, root, rel, history, "1.2.0", released, "feature b")
    c = proven(engine, root, "feature c")
    found = plan_v(engine, root, [c], "1.3.0")["changelog_file"]
    assert found["problem"] is None, f"the third plan refuses: {found}"
    assert [v for v, _ in released] == ["1.1.0", "1.2.0"]


def test_k26_guard_plain_lf_changelog_is_written_exactly_as_before(engine, tmp_path):
    """Guard (dct's own repository): no attributes, LF — the work tree, the
    committed blob and the composed text are the same bytes."""
    root, rel, history = layout_repo(engine, tmp_path, "lf_plain")
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, rel, out)
    expected = expected_text(engine, history).encode()
    assert (root / rel).read_bytes() == expected
    assert blob_bytes(root, f"HEAD:{rel}") == expected


def test_k26_crlf_under_text_auto_next_release_is_not_refused_as_unfiltered(engine, tmp_path):
    """The exact review reproduction: after the CRLF + `* text=auto`
    release, apply of the next version is not refused before the stamp."""
    root = crlf_text_auto_repo(engine, tmp_path, TEXT_AUTO["text_auto"])
    a = proven(engine, root, "feature a")
    assert release_v(engine, root, [a], "1.1.0")["outcome"] == "completed"
    b = proven(engine, root, "feature b")
    found = plan_v(engine, root, [b], "1.2.0")["changelog_file"]
    assert (found["problem"], found.get("baseline")) == (None, None), \
        f"the release left a work tree its own pre-check refuses: {found}"
    out = release_v(engine, root, [b], "1.2.0")
    assert_release_completed(engine, root, "CHANGELOG.md", "1.2.0", [b], out)


# ── K-27: `ambiguous` never comes out without a way ──────────────────────────


def _links_outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CHANGELOG.md").write_text(HISTORY)
    return outside, outside / "CHANGELOG.md"


def _every_command_resolves(engine, root: Path, a: int, found: dict, monkeypatch) -> None:
    """Every offered command, executed through the CLI, leads to a plan
    whose changelog file is settled — or deliberately off."""
    for command in commands(found):
        if not command:
            continue
        run_way(monkeypatch, command)
        after = plan_v(engine, root, [a], "1.1.0")["changelog_file"]
        if after["managed"]:
            assert after["problem"] is None and after["path"], \
                f"the offered `{command}` leads to {after['problem']}: {after['ways']}"
        set_manages_changelog(engine, SLUG, True)
        reset_changelog_path(engine, SLUG)


def test_k27_two_inadmissible_link_candidates_still_offer_ways(cli, tmp_path, monkeypatch):
    """Review repro: CHANGELOG.md → an absolute path outside, NEWS.md →
    ../outside/CHANGELOG.md. Before the fix: ambiguous with ways [] — the
    plan, prepare and apply refused naming no next step."""
    engine = cli
    outside, target = _links_outside(tmp_path)
    root, _ = make_repo(engine, tmp_path, {})
    commit_files(root, links={"CHANGELOG.md": str(target), "NEWS.md": "../outside/CHANGELOG.md"})
    a = proven(engine, root, "feature a")
    found = plan_v(engine, root, [a], "1.1.0")["changelog_file"]
    assert found["problem"] == "ambiguous"
    assert found["ways"], "ambiguous with no next step at all"
    assert f"dct project changelog {SLUG} off" in commands(found), found["ways"]
    replaced = [w["what"] for w in found["ways"] if w["way"] == "replace_link"]
    for link in ("CHANGELOG.md", "NEWS.md"):
        assert any(link in what for what in replaced), \
            f"no replace-link way for the dropped candidate {link}: {found['ways']}"
    exc = assert_refused_before_the_stamp(engine, root, [a], outside=outside)
    assert "next step" in str(exc), f"the refusal names no next step: {exc}"
    _every_command_resolves(engine, root, a, found, monkeypatch)


def test_k27_a_dropped_link_beside_a_regular_candidate_keeps_its_ways(
        cli, tmp_path, monkeypatch):
    """One inadmissible link candidate beside a regular one: the regular
    file is offered AND the dropped link's replace-link way and the off
    way (owner decision: whenever a candidate is dropped)."""
    engine = cli
    _, target = _links_outside(tmp_path)
    root, _ = make_repo(engine, tmp_path, {"CHANGES.md": HISTORY})
    commit_files(root, links={"CHANGELOG.md": str(target)})
    a = proven(engine, root, "feature a")
    found = plan_v(engine, root, [a], "1.1.0")["changelog_file"]
    assert found["problem"] == "ambiguous"
    offered = commands(found)
    assert f"dct project changelog-path {SLUG} CHANGES.md" in offered, found["ways"]
    assert f"dct project changelog {SLUG} off" in offered, found["ways"]
    assert any(w["way"] == "replace_link" and "CHANGELOG.md" in w["what"]
               for w in found["ways"]), found["ways"]
    for command in filter(None, offered):  # the replace-link way has no command
        assert "CHANGELOG.md" not in command.split()[-1], f"offers the link itself: {command}"
    _every_command_resolves(engine, root, a, found, monkeypatch)


# ── K-28: a directory found through a case-insensitive name ──────────────────


def _case_insensitive_dir_repo(engine, tmp_path) -> Path:
    if fs_case_sensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    root, _ = make_repo(engine, tmp_path, {})
    commit_files(root, {"CHANGELOG.md/keep.txt": "x\n"})
    assert status(root) == ""
    return root


def _no_step_moves_the_tracked_directory(found: dict) -> None:
    for way in found["ways"]:
        assert "move CHANGELOG.md away" not in way["what"], \
            f"a step would move the TRACKED directory CHANGELOG.md: {way}"
        assert "commit it if it is your changelog" not in way["what"], way


def test_k28_detect_is_offered_only_when_it_leads_to_an_admissible_path(
        cli, tmp_path, monkeypatch):
    """Review repro: tracked directory CHANGELOG.md/, stored CHANGELOG.md →
    not_a_file offering `--detect`; detection settles changelog.md, which
    the case-insensitive disk opens as the TRACKED directory → not_baseline
    "move CHANGELOG.md away (or commit it …)". Every offered command must
    lead to a settled file (or deliberately off)."""
    engine = cli
    root = _case_insensitive_dir_repo(engine, tmp_path)
    set_changelog_path(engine, SLUG, "CHANGELOG.md")
    a = proven(engine, root, "feature a")
    found = plan_v(engine, root, [a], "1.1.0")["changelog_file"]
    assert found["problem"] == "not_a_file", found
    _no_step_moves_the_tracked_directory(found)
    for command in commands(found):
        if not command:
            continue
        run_way(monkeypatch, command)
        after = plan_v(engine, root, [a], "1.1.0")["changelog_file"]
        _no_step_moves_the_tracked_directory(after)
        if after["managed"]:
            assert after["problem"] is None, \
                f"the offered `{command}` leads to {after['problem']}: {after['ways']}"
        set_manages_changelog(engine, SLUG, True)
        set_changelog_path(engine, SLUG, "CHANGELOG.md")


@pytest.mark.parametrize("setting", ["detection", "explicit_lowercase"])
def test_k28_directory_at_the_path_through_case_is_its_own_problem(
        engine, tmp_path, setting):
    """The resolved changelog.md is absent from the tree, but the
    case-insensitive disk opens the TRACKED directory CHANGELOG.md there:
    a `not_a_file` problem (component file) with true ways — never
    not_baseline "move CHANGELOG.md away (or commit it …)"."""
    root = _case_insensitive_dir_repo(engine, tmp_path)
    if setting == "explicit_lowercase":
        set_changelog_path(engine, SLUG, "changelog.md")
    a = proven(engine, root, "feature a")
    found = plan_v(engine, root, [a], "1.1.0")["changelog_file"]
    assert found["problem"] == "not_a_file", \
        f"a tracked directory reported as {found['problem']}: {found['ways']}"
    assert found.get("component") == "file"
    _no_step_moves_the_tracked_directory(found)
    assert f"dct project changelog {SLUG} off" in commands(found)
    tracked_before = snapshot(root / "CHANGELOG.md")
    exc = assert_refused_before_the_stamp(engine, root, [a], outside=tmp_path / "none")
    assert "move CHANGELOG.md away" not in str(exc)
    assert snapshot(root / "CHANGELOG.md") == tracked_before


# ── K-29: core.safecrlf=true ─────────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(TEXT_AUTO))
def test_k29_safecrlf_true_crlf_changelog_releases_completely(engine, tmp_path, kind):
    """Review repro: with core.safecrlf=true dct's own `git hash-object -w
    --path` died ("CRLF would be replaced by LF") — a raw GitWriteError
    before the stamp, while the owner's own `git add` succeeds. dct checks
    text preservation itself: the release completes, twice."""
    root = crlf_text_auto_repo(engine, tmp_path, TEXT_AUTO[kind])
    git(root, "config", "core.safecrlf", "true")
    released = release_and_check(engine, root, "CHANGELOG.md", HISTORY, "1.1.0", [],
                                 "feature a")
    release_and_check(engine, root, "CHANGELOG.md", HISTORY, "1.2.0", released, "feature b")


# ── K-30: details.index_lock before vs after the stamp ───────────────────────


def _rows(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|") and needle in line]


@pytest.mark.parametrize("doc", ["dct/skills/release/SKILL.md", "docs/assistant-fallbacks.md"])
def test_k30_index_lock_rows_split_before_and_after_the_stamp(doc):
    """Before the stamp nothing is recorded — resume only warns "nothing to
    resume"; the way is the SAME apply again. After the stamp: resume."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    rows = _rows(text, "details.index_lock")
    assert rows, f"{doc}: no refusal row names details.index_lock"
    for row in rows:
        way = row.rstrip().rstrip("|").rsplit("|", 1)[-1].strip()
        assert way != "wait for it, then resume", \
            f"{doc}: the index_lock row still says only 'wait for it, then resume': {row}"
    joined = re.sub(r"\s+", " ", " ".join(rows))
    assert "before the stamp" in joined and "after the stamp" in joined, \
        f"{doc}: the index_lock rows do not name both cases: {joined}"
    assert re.search(r"(same|the) (`dct release )?apply`? again", joined), \
        f"{doc}: the pre-stamp case must say to run the same apply again: {joined}"
    assert "resume" in joined, f"{doc}: the post-stamp case must say resume: {joined}"


# ── the `content` problem outside git (same class as K-24 (2)) ───────────────


@pytest.mark.parametrize("kind", ["not_utf8", "different_text"])
def test_content_ways_outside_git_name_no_git_step_and_no_commit(engine, tmp_path, kind):
    """Outside git there is nothing to commit: the `content` ways say fix
    the file and plan again — before the fix they said "commit it as work
    in progress"."""
    plain = plain_project(engine, tmp_path, {})
    (plain / "CHANGELOG.md").write_bytes(LATIN1 if kind == "not_utf8"
                                         else PREWRITTEN[kind].encode())
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "content"
    assert found["ways"], "no next step"
    _no_git_steps(found)


def test_content_way_outside_git_followed_releases(engine, tmp_path):
    """The offered way, followed (re-encode as UTF-8, nothing committed —
    there is no git), lets the release complete."""
    plain = plain_project(engine, tmp_path, {})
    (plain / "CHANGELOG.md").write_bytes(LATIN1)
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    assert exc.value.details["changelog_file"]["content"] == "not_utf8"
    (plain / "CHANGELOG.md").write_bytes(LATIN1.decode("latin-1").encode("utf-8"))
    out = release(engine, plain, [a])
    assert_completed_into(engine, plain, "CHANGELOG.md", out)
