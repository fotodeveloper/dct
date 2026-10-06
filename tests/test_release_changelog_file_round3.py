"""#876 round 3 — the owner's decisions on the third review of the
changelog-file fix (cards K-19 … K-25), pinned as behaviour BEFORE the fix.

Contract assumed by these tests:

- K-19: EVERY component of the changelog path is checked, in the
  candidate tree AND on disk: a symlink, a regular file or a submodule
  where a directory is expected (or a directory / submodule where the file
  is expected) refuses BEFORE the stamp. The write guards the project
  boundary again (also for a monorepo sub-project): a path that leaves the
  project at write time is refused, never written through.
- K-20: the expected WORK-TREE bytes and the id of the content git STORES
  after its filters are separate. A changelog committed with CRLF under
  `* text=auto` is released completely (a correct normalisation is
  allowed); a filter path whose clean form does not keep the composed
  text is refused BEFORE the stamp.
- K-21: a historical release is judged by the recorded verified manifest
  first, else by the evidence of its own commit — never by today's path or
  detection mode; several matching files refuse; the entries and the
  untouched earlier history are still verified.
- K-22: the marker column is added by db._add_column_if_missing; the
  backfill is convergent (every bootstrap), touches only non-default paths
  whose marker is unset, writes nothing once the state is right, and two
  concurrent bootstraps both succeed.
- K-23: the restore hint claims only "a setting consistent with the
  approved path"; with a CURRENT manual choice it proposes the manual
  choice of the approved file (never a silent --detect); every proposed
  command, executed, lets resume finish.
- K-24: the ambiguous choices lead only to admissible paths; outside git
  no step names git or a commit; SKILL.md / fallbacks name the
  `unfiltered` not_baseline case; docs/mcp-tools.md names
  changelog_path_explicit.
- K-25: a foreign index.lock that exists before the stamp refuses BEFORE
  it; a lock appearing later stops at the commit and resume finishes.

Every refusal before the stamp asserts the STATE: no record,
current_version unchanged, no entry stamped, files byte-identical, nothing
created or changed outside the project.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import approved_plan_digest, approved_push, git
from sqlalchemy import event
from test_migration_guarantees import (  # noqa: F401 - two_engines is a fixture
    _bootstrap_concurrently,
    _hold_both_at,
    two_engines,
)
from test_release_changelog_file_round2 import (
    backticked,
    blob_bytes,
    commit_files,
    interrupted_release,
    run_way,
)
from test_release_changelog_path import (
    RST,
    SLUG,
    assert_completed_into,
    assert_nothing_released,
    changelog_names,
    commands,
    expected_text,
    make_repo,
    plain_project,
    plan_of,
    prepare,
    proven,
    release,
    resume,
    root_files,
)
from test_release_effects import HISTORY, Interrupted, interrupt_at, released_section

from dct import db
from dct.core.changelog import add_entry
from dct.core.projects import (
    create_project,
    get_project,
    reset_changelog_path,
    set_changelog_path,
    set_manages_changelog,
)
from dct.core.releases import get_release
from dct.db import changelog_entries, create_tables, projects
from dct.workflows.release import effects
from dct.workflows.release.effects import (
    ReleaseRefused,
    apply_release,
    prepare_release,
    reconstruct,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

ROOT = Path(__file__).resolve().parent.parent
MARKER = "changelog_path_explicit"


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The effects layer creates commits."""


@pytest.fixture
def cli(engine, monkeypatch):
    from dct import cli as cli_module

    monkeypatch.setattr(cli_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli_module, "_get_readonly_engine", lambda: engine)
    return engine


# ── helpers ──────────────────────────────────────────────────────────────────


def snapshot(directory: Path) -> dict[str, bytes | str]:
    """Every entry below `directory` (links not followed): {rel: bytes} for
    files, {rel: 'link -> target'} for links, {rel: 'dir'} for directories
    — a temp file left behind or a file written through a link shows."""
    out: dict[str, bytes | str] = {}
    if not directory.exists():
        return out
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            full = Path(current) / name
            rel = str(full.relative_to(directory))
            if full.is_symlink():
                out[rel] = f"link -> {os.readlink(full)}"
            elif full.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = full.read_bytes()
    return out


def status(root: Path) -> str:
    return git(root, "status", "--porcelain", "--untracked-files=all")


def nothing_outside(outside: Path, before: dict) -> None:
    assert snapshot(outside) == before, \
        f"a file was created or changed outside the project: {sorted(snapshot(outside))}"


def explicit(engine, rel: str) -> None:
    set_changelog_path(engine, SLUG, rel)
    assert get_project(engine, SLUG)[MARKER] is True


def assert_refused_before_the_stamp(engine, root: Path, ids: list[int], *, outside: Path,
                                    repo: Path | None = None) -> ReleaseRefused:
    """A full release (prepare + apply) refuses with a ReleaseRefused —
    never a raw GitWriteError / OSError — and leaves no trace: nothing
    stamped or recorded, the project's and the repository's files
    byte-identical, nothing written outside."""
    before, outside_before = root_files(root), snapshot(outside)
    repo_before = root_files(repo) if repo else None
    head = git(root, "rev-parse", "HEAD") if (root / ".git").exists() or repo else None
    with pytest.raises(ReleaseRefused) as exc:
        release(engine, root, ids)
    assert_nothing_released(engine, ids, root, before)
    nothing_outside(outside, outside_before)
    if repo is not None:
        assert root_files(repo) == repo_before, "a repository file outside the project changed"
    if head is not None:
        assert git(root, "rev-parse", "HEAD") == head, "a release commit was made"
    return exc.value


# ── K-19: every path component, in the candidate tree AND on disk ────────────


def test_k19_parent_symlink_to_an_outside_directory_is_refused_before_the_stamp(
        engine, tmp_path):
    """Review repro (a): `docs` is a TRACKED symlink to a directory outside
    the repository. Before the fix: plan clean, apply stamped, wrote
    CHANGELOG.md OUTSIDE through the link, then died with a raw
    GitWriteError."""
    outside = tmp_path / "outside-docs"
    outside.mkdir()
    root, _ = make_repo(engine, tmp_path, {})
    commit_files(root, links={"docs": str(outside)})
    explicit(engine, "docs/CHANGELOG.md")
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] is not None and found["path"] is None, \
        "a path beyond a symlinked directory was accepted by the plan"
    for command in commands(found):
        assert "docs/CHANGELOG.md" not in command, f"offers the path beyond the link: {command}"
    assert_refused_before_the_stamp(engine, root, [a], outside=outside)
    assert list(outside.iterdir()) == []


def test_k19_parent_in_repo_directory_symlink_is_refused_with_true_steps(
        cli, tmp_path, monkeypatch):
    """Review repro (c): `docs -> site/docs` (in-repo), the real file
    site/docs/CHANGELOG.md tracked. Before the fix the refusal was
    not_baseline with the harmful step "move docs/CHANGELOG.md away" (it
    would move the tracked site/docs/CHANGELOG.md). Every offered
    changelog-path, executed, leads to an admissible path."""
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"site/docs/CHANGELOG.md": HISTORY})
    commit_files(root, links={"docs": "site/docs"})
    explicit(engine, "docs/CHANGELOG.md")
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] is not None and found["path"] is None
    assert found["problem"] != "not_baseline", "a link component reported as a work-tree change"
    for way in found["ways"]:
        assert "move docs/CHANGELOG.md away" not in way["what"], way
    exc = assert_refused_before_the_stamp(engine, root, [a], outside=tmp_path / "none")
    assert "move docs/CHANGELOG.md away" not in str(exc)
    assert (root / "site" / "docs" / "CHANGELOG.md").read_text() == HISTORY
    for command in [c for c in commands(found)
                    if c.startswith(f"dct project changelog-path {SLUG} ")]:
        run_way(monkeypatch, command)
        after = plan_of(engine, root, [a])["changelog_file"]
        assert after["problem"] is None, f"the offered `{command}` leads to {after['problem']}"
        set_changelog_path(engine, SLUG, "docs/CHANGELOG.md")


@pytest.mark.parametrize("layout", ["parent_file", "parent_submodule", "leaf_submodule",
                                    "leaf_directory"])
def test_k19_non_directory_component_or_non_file_leaf_is_refused_before_the_stamp(
        engine, tmp_path, layout):
    """Review repros (b) and (d): a regular file or a submodule where a
    directory is expected (and a submodule / directory where the file is
    expected). Before the fix: a raw FileExistsError / GitReadError after
    (or instead of) a clean refusal, the record stamped."""
    root, _ = make_repo(engine, tmp_path, {})
    sha = git(root, "rev-parse", "HEAD")
    if layout == "parent_file":
        commit_files(root, {"docs": "not a directory\n"})
        rel = "docs/CHANGELOG.md"
    elif layout == "leaf_directory":
        commit_files(root, {"CHANGELOG.md/keep.txt": "x\n"})
        rel = "CHANGELOG.md"
    else:
        name = "docs" if layout == "parent_submodule" else "CHANGELOG.md"
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{sha},{name}")
        git(root, "commit", "-q", "-m", "chore: submodule")
        (root / name).mkdir()
        rel = "docs/CHANGELOG.md" if layout == "parent_submodule" else "CHANGELOG.md"
    assert status(root) == "", f"precondition: a clean work tree ({status(root)!r})"
    explicit(engine, rel)
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] is not None and found["path"] is None, \
        f"{layout}: {rel} was accepted as a writable changelog file"
    snap = snapshot(root)
    assert_refused_before_the_stamp(engine, root, [a], outside=tmp_path / "none")
    assert {k: v for k, v in snapshot(root).items() if not k.startswith(".git")} == \
        {k: v for k, v in snap.items() if not k.startswith(".git")}, "the work tree changed"


def test_k19_ignored_symlink_on_disk_where_the_candidate_has_nothing(engine, tmp_path):
    """The candidate holds no `docs`; on DISK an ignored `docs` symlink
    leads outside the repository. The tree check alone sees a new file —
    the disk walk must refuse it before the stamp."""
    outside = tmp_path / "outside-docs"
    outside.mkdir()
    root, _ = make_repo(engine, tmp_path, {".gitignore": "/docs\n"})
    os.symlink(str(outside), root / "docs")
    assert status(root) == ""
    explicit(engine, "docs/CHANGELOG.md")
    a = proven(engine, root, "feature a")
    assert_refused_before_the_stamp(engine, root, [a], outside=outside)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("layout", ["parent_symlink_outside", "parent_file"])
def test_k19_outside_git_components_are_checked_on_disk(engine, tmp_path, layout):
    outside = tmp_path / "outside-docs"
    outside.mkdir()
    plain = plain_project(engine, tmp_path, {})
    if layout == "parent_symlink_outside":
        os.symlink(str(outside), plain / "docs")
    else:
        (plain / "docs").write_text("not a directory\n")
    explicit(engine, "docs/CHANGELOG.md")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    before = snapshot(plain)
    assert_refused_before_the_stamp(engine, plain, [a], outside=outside)
    assert snapshot(plain) == before
    assert list(outside.iterdir()) == []


def test_k19_monorepo_component_symlink_leaving_the_project_is_refused(engine, tmp_path):
    """A sub-project's `docs` links to a directory of the REPOSITORY outside
    the project: the release would write another project's file."""
    app, _ = make_repo(engine, tmp_path, {"shared/CHANGELOG.md": HISTORY},
                       subdir="packages/app")
    repo = app.parent.parent
    commit_files(app, links={"docs": "../../shared"})
    explicit(engine, "docs/CHANGELOG.md")
    a = proven(engine, app, "feature a")
    found = plan_of(engine, app, [a])["changelog_file"]
    assert found["problem"] not in (None, "not_baseline"), \
        "a link component reported as a work-tree change (its step would move shared's file)"
    shared = snapshot(repo / "shared")
    exc = assert_refused_before_the_stamp(engine, app, [a], outside=repo / "shared", repo=repo)
    assert "move packages/app/docs/CHANGELOG.md away" not in str(exc)
    assert snapshot(repo / "shared") == shared


def test_k19_monorepo_stored_path_escaping_the_project_is_refused(engine, tmp_path):
    """A stored value the setter would reject (written by another client or
    by hand) never leads the write out of the sub-project."""
    app, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY}, subdir="packages/app")
    repo = app.parent.parent
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == SLUG)
                     .values(changelog_path="../../CHANGELOG.md", **{MARKER: True}))
    a = proven(engine, app, "feature a")
    assert_refused_before_the_stamp(engine, app, [a], outside=tmp_path / "none", repo=repo)
    assert (repo / "CHANGELOG.md").read_text() == HISTORY


def _swap_for_link(directory: Path, parked: Path, target: str):
    """on_point hook: right after the stamp, `directory` is moved to
    `parked` and replaced by a symlink to `target` (a change the pre-checks
    could not see)."""

    def hook(name: str) -> None:
        if name == "after_stamp":
            directory.rename(parked)
            os.symlink(target, directory)

    return hook


@pytest.mark.parametrize("monorepo", [False, True])
def test_k19_write_time_guard_never_writes_through_a_link_that_appeared(
        engine, tmp_path, monorepo):
    """The write guards the project boundary AGAIN: a parent directory
    replaced by a link AFTER the pre-checks (holding a file byte-identical
    to the baseline, so the classification says "write") is refused at the
    write — a ReleaseRefused, nothing written through the link —; restored,
    resume finishes. (Accepted: the record is already stamped.)"""
    subdir = "packages/app" if monorepo else ""
    prefix = "packages/app/" if monorepo else ""
    root, _ = make_repo(engine, tmp_path, {f"{prefix}docs/CHANGELOG.md": HISTORY,
                                           "shared/keep.txt": "x\n"}, subdir=subdir)
    repo = root.parent.parent if monorepo else root
    if monorepo:
        outside, target = repo / "shared", "../../shared"
    else:
        outside = tmp_path / "outside-docs"
        outside.mkdir()
        target = str(outside)
    (outside / "CHANGELOG.md").write_text(HISTORY)
    outside_before = snapshot(outside)
    explicit(engine, "docs/CHANGELOG.md")
    a = proven(engine, root, "feature a")
    parked = tmp_path / "parked-docs"
    with pytest.raises(ReleaseRefused):
        release(engine, root, [a], on_point=_swap_for_link(root / "docs", parked, target))
    nothing_outside(outside, outside_before)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    (root / "docs").unlink()
    parked.rename(root / "docs")
    (outside / "CHANGELOG.md").unlink()
    out = resume(engine, root)
    assert out["outcome"] == "completed"
    assert get_release(engine, SLUG, "1.1.0")["manifest"]["changelog"]["path"] == \
        f"{prefix}docs/CHANGELOG.md"
    assert (root / "docs" / "CHANGELOG.md").read_text() == expected_text(engine)


# ── K-20: work-tree bytes vs the content git stores ──────────────────────────


CRLF_HISTORY = HISTORY.replace("\n", "\r\n").encode()
TEXT_AUTO = {"text_auto": "* text=auto\n", "md_text_auto_eol_crlf": "*.md text=auto eol=crlf\n"}


def crlf_text_auto_repo(engine, tmp_path, attributes: str) -> Path:
    """changelog committed with CRLF line endings; LATER `text=auto` — git
    keeps the CRLF blob as it is, `git status` is clean."""
    root, _ = make_repo(engine, tmp_path, {})
    commit_files(root, {"CHANGELOG.md": CRLF_HISTORY}, message="docs: changelog")
    commit_files(root, {".gitattributes": attributes}, message="chore: attributes")
    assert blob_bytes(root, "HEAD:CHANGELOG.md") == CRLF_HISTORY
    assert (root / "CHANGELOG.md").read_bytes() == CRLF_HISTORY
    assert status(root) == ""
    return root


def fresh_checkout(root: Path, rel: str) -> bytes:
    """The bytes a FRESH checkout of HEAD writes for `rel` — an independent
    oracle: a new clone of the repository (git's own checkout, the
    attributes of the tree, no stat cache, no dct code)."""
    import tempfile

    dest = Path(tempfile.mkdtemp(prefix="fresh-checkout-", dir=root.parent)) / "clone"
    git(root.parent, "clone", "-q", str(root), str(dest))
    assert git(dest, "rev-parse", "HEAD") == git(root, "rev-parse", "HEAD")
    return (dest / rel).read_bytes()


def assert_consistent_with_git(engine, root: Path) -> None:
    """The release's result as git's rules see it: after forcing git to
    re-read the file's content (stat cache busted) nothing differs from
    HEAD; the work tree and the committed blob both hold the history with
    the section (line endings aside); the work tree is EXACTLY what a fresh
    checkout of the release commit writes (K-26 decision a — under `*
    text=auto` the earlier CRLF history is normalised, as a checkout of
    the LF blob git stores would write it)."""
    path = root / "CHANGELOG.md"
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    assert status(root) == "", f"git sees a difference after the release: {status(root)!r}"
    expected = expected_text(engine).encode()
    work = path.read_bytes()
    blob = blob_bytes(root, "HEAD:CHANGELOG.md")
    assert work.replace(b"\r\n", b"\n") == expected, "the work tree is not history + section"
    assert blob.replace(b"\r\n", b"\n") == expected, "the commit is not history + section"
    assert work == fresh_checkout(root, "CHANGELOG.md"), \
        "the work tree is not what a fresh checkout of the release commit writes"


@pytest.mark.parametrize("kind", sorted(TEXT_AUTO))
def test_k20_crlf_changelog_under_text_auto_is_released_completely(engine, tmp_path, kind):
    """Regression vs main: before the fix apply stamped, wrote, then
    refused "differs from the approved content" — record stamped,
    current_version moved, every resume refused the same way."""
    root = crlf_text_auto_repo(engine, tmp_path, TEXT_AUTO[kind])
    a = proven(engine, root, "feature a")
    assert plan_of(engine, root, [a])["changelog_file"]["problem"] is None
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert get_project(engine, SLUG)["current_version"] == "1.1.0"
    assert git(root, "rev-parse", "v1.1.0^{commit}") == git(root, "rev-parse", "HEAD")
    assert_consistent_with_git(engine, root)


@pytest.mark.parametrize("point", ["after_stamp", "after_changelog"])
def test_k20_crlf_under_text_auto_resumes_after_an_interruption(engine, tmp_path, point):
    root = crlf_text_auto_repo(engine, tmp_path, TEXT_AUTO["text_auto"])
    a = proven(engine, root, "feature a")
    with pytest.raises(Interrupted):
        release(engine, root, [a], on_point=interrupt_at(point))
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    out = resume(engine, root)
    assert out["outcome"] == "completed", out
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"
    assert_consistent_with_git(engine, root)


def test_k20_filter_that_does_not_keep_the_composed_text_is_refused_before_the_stamp(
        engine, tmp_path):
    """The render + filter path is checked before the stamp: a clean filter
    that rewrites the release's own text (not a line-ending
    normalisation) would commit a section other than the entries'. Before
    the fix: stamped, then refused after the write."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    git(root, "config", "filter.mangle.clean", "sed s/feature/FEATURE/")
    git(root, "config", "filter.mangle.smudge", "cat")
    commit_files(root, {".gitattributes": "CHANGELOG.md filter=mangle\n"})
    assert "feature" not in HISTORY and status(root) == ""
    a = proven(engine, root, "feature a")
    assert_refused_before_the_stamp(engine, root, [a], outside=tmp_path / "none")
    assert (root / "CHANGELOG.md").read_text() == HISTORY


# ── K-21: a historical release by its own evidence ──────────────────────────


def legacy_commit(engine, root: Path, files: dict[str, str], *, tag: bool = True
                  ) -> tuple[int, str, str]:
    """1.1.0 released by hand before records existed: the entry stamped in
    the database, a `release: v1.1.0` commit holding `files` (+ the
    version file) → (entry, candidate, release commit)."""
    from datetime import UTC, datetime

    a = proven(engine, root, "feature a")
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version="1.1.0", released_at=datetime(2026, 2, 1, 10, tzinfo=UTC)))
        conn.execute(projects.update().values(current_version="1.1.0"))
    candidate = git(root, "rev-parse", "HEAD")
    head = commit_files(root, {**files,
                               "pyproject.toml": '[project]\nname = "x"\nversion = "1.1.0"\n'},
                        message="release: v1.1.0")
    if tag:
        git(root, "tag", "v1.1.0")
    return a, candidate, head


RELEASED = "# Changelog\n\n" + released_section("2026-02-01") + "\n" + HISTORY[13:]
NOT_DETECTED = "docs-CHANGELOG.md"  # not a changelog-like name: detection never finds it


def rename_and_choose(engine, root: Path, old: str, new: str) -> None:
    git(root, "mv", old, new)
    git(root, "commit", "-q", "-m", "docs: rename the changelog")
    explicit(engine, new)


LATER_CHOICE = {
    # renamed to a name detection never finds, then chosen explicitly
    "renamed_and_chosen": lambda engine, root: rename_and_choose(
        engine, root, "changelog.md", NOT_DETECTED),
    # another file, present at the parent, chosen explicitly afterwards
    "other_file_chosen": lambda engine, root: explicit(engine, "NOTES.md"),
}


@pytest.mark.parametrize("later", sorted(LATER_CHOICE))
def test_k21_reconstruct_ignores_an_explicit_path_chosen_after_the_release(
        engine, tmp_path, later):
    """Review repro: before the fix the explicit path resolved at the parent
    turned the release's real changelog into a "foreign change" — the tag
    "is not a verifiable release commit"."""
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY, "NOTES.md": "# Notes\n"})
    a, candidate, commit = legacy_commit(engine, root, {"changelog.md": RELEASED})
    LATER_CHOICE[later](engine, root)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    evidence = preview["details"]["evidence"]
    assert (evidence["candidate_sha"], evidence["release_commit_sha"]) == (candidate, commit)
    assert evidence["manifest"]["changelog"]["path"] == "changelog.md"
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))
    record = out["release"]
    assert record["status"] == "completed" and record["entry_ids"] == [a]
    assert record["manifest"]["changelog"]["path"] == "changelog.md"


def test_k21_release_that_created_its_changelog_under_another_name(engine, tmp_path):
    """The release commit CREATED its changelog as CHANGES.md (no file at
    the parent, no setting): its own commit is the evidence — detection's
    default name at the parent (changelog.md, absent) never decides.
    (Interpretation of K-21: "independent of today's path and of detect
    mode"; the history state is `bootstrap`.)"""
    root, _ = make_repo(engine, tmp_path, {})
    _, _, commit = legacy_commit(engine, root, {"CHANGES.md": RELEASED})
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    evidence = preview["details"]["evidence"]
    assert evidence["release_commit_sha"] == commit
    assert evidence["manifest"]["changelog"]["path"] == "CHANGES.md"
    # the evidence carries the history verdict in its recorded scope
    # (manifest.verification / verification), not as a top-level key
    assert evidence["verification"]["history"] == "bootstrap"
    assert [c["history"] for c in evidence["candidates"]] == [{"state": "bootstrap"}]


def test_k21_guard_recorded_manifest_wins_over_a_later_explicit_path(engine, tmp_path):
    """Guard (passes before the fix): a record's verified manifest names the
    release's file — a takeover after a rename + explicit choice uses it."""
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY})
    _, _, commit = legacy_commit(engine, root, {"changelog.md": RELEASED}, tag=False)
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))["release"]
    assert record["status"] == "committed"
    rename_and_choose(engine, root, "changelog.md", NOT_DETECTED)
    preview = effects.takeover(engine, SLUG, "1.1.0", cwd=str(root))
    assert preview["details"]["proposal"]["changelog"]["path"] == "changelog.md"
    out = effects.takeover(engine, SLUG, "1.1.0", cwd=str(root), confirm=True,
                           takeover_digest=preview["details"]["takeover_digest"])
    assert out["outcome"] == "taken_over"
    assert effects.resume_release(engine, SLUG, cwd=str(root))["outcome"] == "completed"
    assert git(root, "rev-parse", "v1.1.0") == commit
    tracked = git(root, "ls-tree", "--name-only", "HEAD").splitlines()
    assert NOT_DETECTED in tracked and "changelog.md" not in tracked, "the rename was undone"


def test_k21_guard_two_files_holding_the_section_are_refused(engine, tmp_path):
    """Guard (refused before the fix too, for the contract): a release
    commit whose changes hold the V section in TWO changelog-like files is
    never guessed — refused, nothing recorded."""
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY, "CHANGES.md": HISTORY})
    legacy_commit(engine, root, {"changelog.md": RELEASED, "CHANGES.md": RELEASED})
    explicit(engine, NOT_DETECTED)
    with pytest.raises(ReleaseRefused) as exc:
        reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    assert "CHANGES.md" in str(exc.value) and "changelog.md" in str(exc.value)
    assert get_release(engine, SLUG, "1.1.0") is None


def test_k21_altered_earlier_history_is_still_refused(engine, tmp_path):
    """One version section is not enough: the release commit also edited
    the 1.0.0 section. After a rename + explicit choice the refusal must
    still be the HISTORY check of the release's own file (details.history
    of the candidate: state `changed`) — not a contract mismatch caused by
    today's setting."""
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY})
    legacy_commit(engine, root, {"changelog.md": RELEASED.replace("- first", "- first (ed.)")})
    rename_and_choose(engine, root, "changelog.md", NOT_DETECTED)
    with pytest.raises(ReleaseRefused) as exc:
        reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    states = [(c.get("history") or {}).get("state")
              for c in exc.value.details.get("candidates") or []]
    assert states == ["changed"], \
        f"refused for another reason than the release file's history: {exc.value}"
    assert get_release(engine, SLUG, "1.1.0") is None


# ── K-22: the marker migration ───────────────────────────────────────────────


_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_MARKER_RE = re.compile(rf"\b{MARKER}\b", re.IGNORECASE)


def _record(engine) -> list[str]:
    seen: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *a: seen.append(statement.strip()))
    return seen


def _marks(engine, *slugs: str) -> dict[str, tuple[str, bool]]:
    return {s: (get_project(engine, s)["changelog_path"], get_project(engine, s)[MARKER])
            for s in slugs}


def _assert_quiet(engine, seen: list[str], capsys, runs=(2, 3)) -> None:
    for n in runs:
        seen.clear()
        create_tables(engine)
        ddl = [s for s in seen if _DDL.match(s)]
        writes = [s for s in seen if _DML.match(s) and _MARKER_RE.search(s)]
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        assert writes == [], f"bootstrap {n} wrote the marker: {writes}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"


def _drop_marker(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE projects DROP COLUMN {MARKER}"))


def test_k22_marker_column_is_added_by_the_duplicate_safe_helper(engine, monkeypatch):
    """Owner decision: the existing _add_column_if_missing (K-25: IF NOT
    EXISTS + lock_timeout on PostgreSQL, duplicate-tolerant on SQLite)."""
    _drop_marker(engine)
    calls: list[tuple[str, str]] = []
    real = db._add_column_if_missing

    def spy(eng, table, column, ddl_type):
        calls.append((table, column))
        return real(eng, table, column, ddl_type)

    monkeypatch.setattr(db, "_add_column_if_missing", spy)
    create_tables(engine)
    assert ("projects", MARKER) in calls, "the marker is added by a raw ALTER TABLE"
    assert MARKER in [c["name"] for c in sa.inspect(engine).get_columns("projects")]


def test_k22_a_later_write_by_an_older_client_is_healed(engine, capsys):
    """After the migration an older dct (no marker) stores a non-default
    path: the marker stays false and the new code would silently DETECT
    instead. The next bootstrap heals it to explicit; a reset (--detect),
    the default and an explicit `changelog.md` are never touched; the
    following bootstraps write nothing."""
    for slug in ("proj-old", "proj-default", "proj-reset", "proj-chosen"):
        create_project(engine, slug=slug, name=slug, path=f"/tmp/{slug}")
    set_changelog_path(engine, "proj-reset", "docs/CHANGELOG.md")
    reset_changelog_path(engine, "proj-reset")
    set_changelog_path(engine, "proj-chosen", "changelog.md")
    with engine.begin() as conn:  # what an older client's setter writes
        conn.execute(projects.update().where(projects.c.slug == "proj-old")
                     .values(changelog_path="docs/CHANGELOG.md"))
    assert get_project(engine, "proj-old")[MARKER] is False
    seen = _record(engine)
    create_tables(engine)
    capsys.readouterr()
    assert _marks(engine, "proj-old", "proj-default", "proj-reset", "proj-chosen") == {
        "proj-old": ("docs/CHANGELOG.md", True),
        "proj-default": ("changelog.md", False),
        "proj-reset": ("changelog.md", False),
        "proj-chosen": ("changelog.md", True),
    }
    _assert_quiet(engine, seen, capsys)
    assert get_project(engine, "proj-old")[MARKER] is True


def test_k22_detect_reset_stays_a_reset_across_bootstraps(engine, capsys):
    """--detect writes the default path with the marker false: no bootstrap
    ever re-marks it (the backfill touches only NON-default paths)."""
    create_project(engine, slug="proj-a", name="A", path="/tmp/proj-a")
    set_changelog_path(engine, "proj-a", "docs/CHANGELOG.md")
    _drop_marker(engine)
    seen = _record(engine)
    create_tables(engine)
    capsys.readouterr()
    assert _marks(engine, "proj-a") == {"proj-a": ("docs/CHANGELOG.md", True)}
    reset_changelog_path(engine, "proj-a")
    _assert_quiet(engine, seen, capsys, runs=(2, 3, 4))
    assert _marks(engine, "proj-a") == {"proj-a": ("changelog.md", False)}


def test_k22_guard_right_state_is_never_written(engine, capsys):
    """Guard: a database already in the right state — the first bootstrap
    of the new code writes nothing either (a convergent backfill guarded
    by a read, never an unconditional UPDATE)."""
    create_project(engine, slug="proj-a", name="A", path="/tmp/proj-a")
    create_project(engine, slug="proj-b", name="B", path="/tmp/proj-b")
    set_changelog_path(engine, "proj-b", "docs/CHANGELOG.md")
    seen = _record(engine)
    _assert_quiet(engine, seen, capsys, runs=(1, 2, 3))


def test_k22_two_concurrent_bootstraps_both_succeed_and_heal(two_engines):  # noqa: F811
    """A hook's CLI call and an MCP restart bootstrap the upgraded database
    at the same instant (both saw the column missing). Before the fix the
    second raw ALTER crashed its bootstrap (duplicate column). Both
    lanes: a file SQLite database here, the throwaway PostgreSQL one on
    the PG lane."""
    first, _ = two_engines
    create_project(first, slug="proj-a", name="A", path="/tmp/proj-a")
    create_project(first, slug="proj-b", name="B", path="/tmp/proj-b")
    set_changelog_path(first, "proj-b", "docs/CHANGELOG.md")
    _drop_marker(first)
    _hold_both_at(two_engines, re.compile(rf"ADD COLUMN .*{MARKER}", re.IGNORECASE))
    errors = _bootstrap_concurrently(two_engines)
    assert errors == [None, None], errors
    assert [c["name"] for c in sa.inspect(first).get_columns("projects")].count(MARKER) == 1
    assert _marks(first, "proj-a", "proj-b") == {
        "proj-a": ("changelog.md", False), "proj-b": ("docs/CHANGELOG.md", True)}


# ── K-23: the restore hint — a setting consistent with the approved path ─────


def _interrupt_then(engine, root: Path, a: int, later) -> ReleaseRefused:
    interrupted_release(engine, root, a)
    later(engine)
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, root)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    return exc.value


RESTORE = {
    # approved by DETECTION; now a manual choice → the manual approved file
    "detected_then_manual": (
        {"CHANGELOG.md": HISTORY}, None,
        lambda e: set_changelog_path(e, SLUG, "CHANGES.md"),
        "CHANGELOG.md", [f"dct project changelog-path {SLUG} CHANGELOG.md"]),
    # approved by an EXPLICIT path; now another manual choice (review repro)
    "manual_then_manual": (
        {"CHANGELOG.md": HISTORY}, "CHANGELOG.md",
        lambda e: set_changelog_path(e, SLUG, "CHANGES.md"),
        "CHANGELOG.md", [f"dct project changelog-path {SLUG} CHANGELOG.md"]),
    # switched off AND a manual choice: both are restored, still manually
    "off_and_manual": (
        {"CHANGELOG.md": HISTORY}, None,
        lambda e: (set_manages_changelog(e, SLUG, False),
                   set_changelog_path(e, SLUG, "CHANGES.md")),
        "CHANGELOG.md", [f"dct project changelog {SLUG} on",
                         f"dct project changelog-path {SLUG} CHANGELOG.md"]),
    # approved explicitly; now detection, which cannot yield it (guard)
    "manual_then_detect": (
        {"CHANGELOG.md": HISTORY}, "docs/notes.md",
        lambda e: reset_changelog_path(e, SLUG),
        "docs/notes.md", [f"dct project changelog-path {SLUG} docs/notes.md"]),
}


@pytest.mark.parametrize("case", sorted(RESTORE))
def test_k23_every_proposed_command_executed_lets_resume_finish(
        cli, tmp_path, monkeypatch, case):
    engine = cli
    files, approve_with, later, approved, expected = RESTORE[case]
    root, _ = make_repo(engine, tmp_path, files)
    if approve_with:
        explicit(engine, approve_with)
    a = proven(engine, root, "feature a")
    exc = _interrupt_then(engine, root, a, later)
    was_manual_after = get_project(engine, SLUG)[MARKER]
    assert exc.details.get("approved") == approved
    restore = exc.details.get("restore")
    assert restore == expected, f"proposed {restore}, expected {expected}"
    if was_manual_after:
        assert not any("--detect" in c for c in restore), \
            "a current manual choice was switched to detection silently"
    for command in restore:
        run_way(monkeypatch, command)
    out = resume(engine, root)
    assert out["outcome"] == "completed"
    assert get_release(engine, SLUG, "1.1.0")["manifest"]["changelog"]["path"] == approved
    if was_manual_after:
        assert get_project(engine, SLUG)[MARKER] is True, "the owner's manual mode was dropped"


def test_k23_monorepo_restore_names_the_project_relative_path(cli, tmp_path, monkeypatch):
    engine = cli
    app, _ = make_repo(engine, tmp_path, {"packages/app/CHANGES.md": HISTORY},
                       subdir="packages/app")
    a = proven(engine, app, "feature a")
    exc = _interrupt_then(engine, app, a, lambda e: set_changelog_path(e, SLUG, "NEWS.md"))
    assert exc.details.get("approved") == "packages/app/CHANGES.md"
    assert exc.details.get("restore") == [f"dct project changelog-path {SLUG} CHANGES.md"]
    for command in exc.details["restore"]:
        run_way(monkeypatch, command)
    assert resume(engine, app)["outcome"] == "completed"
    assert (app / "CHANGES.md").read_text() == expected_text(engine)


def test_k23_hint_claims_only_a_setting_consistent_with_the_approved_path(engine, tmp_path):
    """The proposal does not record which setting produced the path: the
    refusal never claims to restore "the setting the approval was made
    with"."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    explicit(engine, "CHANGELOG.md")
    a = proven(engine, root, "feature a")
    exc = _interrupt_then(engine, root, a, lambda e: set_changelog_path(e, SLUG, "CHANGES.md"))
    message = str(exc)
    assert "approval was made with" not in message, message
    assert "consistent with the approved" in message.lower(), message


@pytest.mark.parametrize("doc", ["dct/skills/release/SKILL.md", "docs/assistant-fallbacks.md",
                                 "docs/release-workflow.md"])
def test_k23_docs_never_claim_the_setting_the_approval_was_made_with(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "approval was made with" not in re.sub(r"\s+", " ", text), \
        f"{doc}: the old mode is not recorded — say 'a setting consistent with the approved path'"


# ── K-24: four small points ──────────────────────────────────────────────────


def test_k24_ambiguous_choices_lead_only_to_admissible_paths(cli, tmp_path, monkeypatch):
    """(1) A tracked symlink candidate beside a regular one: every offered
    changelog-path, executed, settles the file (never into `symlink`)."""
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"docs/CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY})
    commit_files(root, links={"CHANGELOG.md": "docs/CHANGELOG.md"})
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] == "ambiguous"
    offered = [c for c in commands(found) if c.startswith(f"dct project changelog-path {SLUG} ")]
    assert offered, found["ways"]
    for command in offered:
        run_way(monkeypatch, command)
        after = plan_of(engine, root, [a])["changelog_file"]
        assert after["problem"] is None, f"the offered `{command}` leads to {after['problem']}"
        reset_changelog_path(engine, SLUG)


_GIT_STEP = re.compile(r"\bgit\b|\bcommit")


def _no_git_steps(found: dict) -> None:
    for way in found["ways"]:
        text = f"{way['what']} {way.get('command') or ''}"
        assert not _GIT_STEP.search(text), f"a git step offered outside git: {way}"


def test_k24_outside_git_conversion_names_no_git_step_and_works(cli, tmp_path, monkeypatch):
    """(2) Outside git there is no `git mv` and nothing to commit: the
    conversion says rename + rewrite + choose; followed, it releases."""
    engine = cli
    plain = plain_project(engine, tmp_path, {"HISTORY.rst": RST})
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "not_markdown"
    _no_git_steps(found)
    [rewrite] = [w for w in found["ways"] if w["way"] == "rewrite_markdown"]
    [choose] = [c for c in backticked(rewrite["what"]) if "changelog-path" in c]
    (plain / "HISTORY.rst").rename(plain / "HISTORY.md")
    (plain / "HISTORY.md").write_text(HISTORY)
    run_way(monkeypatch, choose)
    out = release(engine, plain, [a])
    assert_completed_into(engine, plain, "HISTORY.md", out)
    assert changelog_names(plain) == ["HISTORY.md"]


def test_k24_outside_git_symlink_ways_name_no_git_step(engine, tmp_path):
    plain = plain_project(engine, tmp_path, {})
    (plain / "docs").mkdir()
    (plain / "docs" / "CHANGELOG.md").write_text(HISTORY)
    os.symlink("docs/CHANGELOG.md", plain / "CHANGELOG.md")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "symlink"
    _no_git_steps(found)


def _passages(text: str, needle: str, size: int = 700) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [flat[m.start():m.start() + size] for m in re.finditer(re.escape(needle), flat)]


@pytest.mark.parametrize("doc", ["dct/skills/release/SKILL.md", "docs/assistant-fallbacks.md"])
def test_k24_docs_distinguish_a_content_change_from_a_filter_recheckout(doc):
    """(3) not_baseline `baseline: "unfiltered"` has nothing to commit — the
    step is checking the file out again through its filters."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    hits = [p for p in _passages(text, "not_baseline") if "unfiltered" in p]
    assert hits, f"{doc}: no not_baseline passage names the `unfiltered` case"
    assert any("filter" in p and ("out again" in p or "checkout" in p) for p in hits), \
        f"{doc}: the unfiltered case must name checking the file out again through its filters"


def test_k24_mcp_tools_list_projects_names_the_explicit_marker():
    """(4) changelog_path alone no longer says whether dct detects."""
    text = (ROOT / "docs/mcp-tools.md").read_text(encoding="utf-8")
    [row] = [line for line in text.splitlines() if line.startswith("| `list_projects`")]
    assert MARKER in row, "list_projects row does not name changelog_path_explicit"
    assert "changelog.md` = detect" not in row, "the value no longer decides detection"


# ── K-25: a lock present before the stamp ────────────────────────────────────


def _prepared(engine, root: Path, a: int) -> tuple[str, str]:
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    proposal = prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                               plan_digest=plan)["details"]["proposal_sha256"]
    return plan, proposal


def _apply(engine, root: Path, a: int, plan: str, proposal: str, **kw) -> dict:
    return apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                         confirm=True, plan_digest=plan, proposal_digest=proposal,
                         **kw, **approved_push(root))


def _index_lock(root: Path) -> Path:
    return root / git(root, "rev-parse", "--git-path", "index.lock")


def test_k25_index_lock_present_before_apply_is_refused_before_the_stamp(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    plan, proposal = _prepared(engine, root, a)
    lock = _index_lock(root)
    lock.write_bytes(b"")
    before, head = root_files(root), git(root, "rev-parse", "HEAD")
    with pytest.raises(ReleaseRefused) as exc:
        _apply(engine, root, a, plan, proposal)
    assert exc.value.details.get("index_lock"), f"refused for another reason: {exc.value}"
    assert_nothing_released(engine, [a], root, before)
    assert git(root, "rev-parse", "HEAD") == head
    assert lock.exists(), "dct removed a foreign index.lock"
    lock.unlink()  # the other git process finished
    out = _apply(engine, root, a, plan, proposal)
    assert_completed_into(engine, root, "CHANGELOG.md", out)


def test_k25_guard_lock_appearing_after_the_stamp_stops_at_the_commit_and_resumes(
        engine, tmp_path):
    """Guard of the ACCEPTED limit: a lock that appears during the
    operation stops the release after the stamp (no trial commit, hooks
    never run twice); resume finishes once it is gone."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    plan, proposal = _prepared(engine, root, a)
    lock = _index_lock(root)

    def appear(name: str) -> None:
        if name == "after_files_written":
            lock.write_bytes(b"")

    with pytest.raises(ReleaseRefused) as exc:
        _apply(engine, root, a, plan, proposal, on_point=appear)
    assert exc.value.details.get("index_lock")
    assert get_release(engine, SLUG, "1.1.0")["status"] == "files_written"
    assert lock.exists()
    lock.unlink()
    assert resume(engine, root)["outcome"] == "completed"


def test_k25_docs_list_the_index_lock_among_apply_pre_checks():
    """The extent of the guarantee is documented: an existing lock is a
    pre-check of apply (one appearing later is not)."""
    text = (ROOT / "docs/release-workflow.md").read_text(encoding="utf-8")
    start = text.index("1. **Pre-checks** — strict checkout")
    block = text[start:text.index("2. **Stamp**", start)]
    assert "index.lock" in block, "apply's pre-checks do not name an existing index.lock"
