"""#876 round 2 — the owner's decisions on the second review of the
changelog-file fix (cards K-8 … K-17), pinned as behaviour BEFORE the fix.

Contract assumed by these tests (the implementer follows these names):

- K-9: prepare and apply's pre-checks run the WHOLE changelog composition
  (the function the write uses, `expected_changelog`) on the candidate's
  content BEFORE the stamp — a file that is not UTF-8 or already holds a
  different V section is refused with `details.changelog_file`, problem
  `content`; no trial stamp.
- K-10: an explicit path is format-checked exactly like a detected one:
  a non-Markdown extension → problem `not_markdown`, also for a file that
  does not exist yet.
- K-11: a symlink (detected or explicit) → problem `symlink`, whose ways
  name a `dct project changelog-path <slug> <regular file>` that works; a
  git eol / smudge filter is compared through the filtered content
  (gitinfo.git_cat_blob(..., path=)) and a full release completes.
- K-12: reconstruct / takeover resolve the changelog at the release's own
  commit (and the record's data), never at today's HEAD.
- K-13: several Markdown candidates (case-only variants included) are
  `ambiguous` — `changelog.md` is not privileged; an explicit path decides.
- K-14: projects.changelog_path_explicit (BOOLEAN NOT NULL DEFAULT false),
  added by a one-shot block of _apply_schema_updates that marks a
  non-default stored path explicit and the historical default NOT explicit;
  set_changelog_path() always marks the choice explicit (also for exactly
  `changelog.md`); reset_changelog_path(engine, slug) / `dct project
  changelog-path <slug> --detect` go back to detection; the resolver reads
  the marker (`project["changelog_path_explicit"]`); MCP list_projects
  shows it.
- K-15: the rewrite way names the content rewrite, a Markdown extension
  and the new path setting, and says `git mv` alone converts nothing.
- K-16: a resume refused because the setting changed since the approval
  FIRST offers the command restoring the setting the stored proposal
  binds (which then lets resume finish), abandon last; never for a record
  stamped by the pre-fix code (K-18).
- K-8: a local tag of V (any spelling) is refused by prepare and apply
  BEFORE the stamp.

Every refusal asserts the state: no record, current_version unchanged, no
entry stamped, root files byte-identical (no second changelog).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import approved_plan_digest, approved_push, git, run_cli
from sqlalchemy import event
from test_release_changelog_path import (
    RST,
    SLUG,
    _pretend_pre_fix_resolver,
    _resolver,
    assert_completed_into,
    assert_nothing_released,
    changelog_names,
    commands,
    commit_of,
    expected_text,
    make_repo,
    plain_project,
    plan_of,
    prepare,
    proven,
    release,
    resolve_tree,
    resume,
    root_files,
    tracked_changelogs,
)
from test_release_effects import HISTORY, Interrupted, interrupt_at, released_section

from dct.core.changelog import add_entry
from dct.core.projects import (
    create_project,
    get_project,
    set_changelog_path,
    set_manages_changelog,
)
from dct.core.releases import get_pending_proposal, get_release
from dct.db import changelog_entries, create_tables, projects
from dct.workflows.release import effects
from dct.workflows.release.effects import (
    ReleaseRefused,
    apply_release,
    prepare_release,
    reconstruct,
    resume_release,
)

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


def commit_files(root: Path, files: dict[str, str | bytes] | None = None, *,
                 links: dict[str, str] | None = None, message: str = "chore: layout") -> str:
    """Write `files` (text or raw bytes) and `links` (name → target) under
    `root`, commit them; returns the new HEAD."""
    for rel, data in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data)
    for rel, target in (links or {}).items():
        os.symlink(target, root / rel)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def tags(root: Path) -> str:
    return git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags")


def backticked(message: str) -> list[str]:
    """The `dct …` commands a refusal names, in order."""
    return re.findall(r"`(dct [^`]+)`", message)


def run_way(monkeypatch, command: str) -> None:
    """Execute a next step a refusal offered — through the real CLI parser."""
    argv = shlex.split(command)
    assert argv[0] == "dct", command
    try:
        run_cli(monkeypatch, argv[1:])
    except SystemExit as exc:  # pragma: no cover - only on failure
        assert exc.code in (0, None), f"the offered step `{command}` failed ({exc.code})"


def way_command(resolution: dict, prefix: str) -> str:
    found = [c for c in commands(resolution) if c.startswith(prefix)]
    assert found, f"no way starting with {prefix!r} offered: {resolution['ways']}"
    return found[0]


def blob_bytes(root: Path, spec: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(root), "cat-file", "blob", spec], env=env,
                          capture_output=True, check=True).stdout


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def project_of(**fields) -> dict:
    return {"slug": SLUG, "manages_changelog": True, **fields}


# ── K-9: the whole composition runs BEFORE the stamp ─────────────────────────


LATIN1 = ("# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n\n- caf\xe9\n").encode("latin-1")
PREWRITTEN = {
    "different_text": "# Changelog\n\n## [1.1.0] - TBD\n\n### Added\n\n- hand written\n\n"
                      + HISTORY[13:],
    "equivalent_spelling": "# Changelog\n\n## [1.1] - 2026-03-01\n\n### Added\n\n- feature a\n\n"
                           + HISTORY[13:],
}


def content_layout(engine, tmp_path, kind: str) -> tuple[Path, Path, int]:
    root, log = make_repo(engine, tmp_path, {})
    data = LATIN1 if kind == "not_utf8" else PREWRITTEN[kind]
    commit_files(root, {"CHANGELOG.md": data})
    return root, log, proven(engine, root, "feature a")


@pytest.mark.parametrize("kind", ["not_utf8", "different_text", "equivalent_spelling"])
def test_k9_changelog_content_is_refused_by_prepare_before_anything(engine, tmp_path, kind):
    """K-9: what the write would refuse after the stamp is refused by
    prepare — no proposal, no sync run, nothing released or written."""
    root, log, a = content_layout(engine, tmp_path, kind)
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    found = exc.value.details.get("changelog_file")
    assert found is not None, f"refused for another reason: {exc.value}"
    assert found["problem"] == "content"
    assert "CHANGELOG.md" in str(exc.value)
    assert get_pending_proposal(engine, SLUG) is None
    assert not log.exists(), "the version sync ran although the changelog was refused"
    assert_nothing_released(engine, [a], root, before)


@pytest.mark.parametrize("kind", ["not_utf8", "different_text", "equivalent_spelling"])
def test_k9_full_release_with_bad_changelog_content_never_stamps(engine, tmp_path, kind):
    """K-9 regression (#876 through another door): before the fix apply
    STAMPED, then refused in the changelog step — a 'stamped' record and a
    moved current_version."""
    root, _, a = content_layout(engine, tmp_path, kind)
    before = root_files(root)
    head = git(root, "rev-parse", "HEAD")
    with pytest.raises(ReleaseRefused):
        release(engine, root, [a])
    assert_nothing_released(engine, [a], root, before)
    assert git(root, "rev-parse", "HEAD") == head and tags(root).count("refs/tags/") == 1


def test_k9_apply_pre_check_composes_the_changelog_itself(engine, tmp_path):
    """apply does not rely on prepare: a proposal prepared while dct's
    changelog file was off, the setting turned on → apply's own pre-check
    composes the changelog and refuses its content before the stamp."""
    root, _, a = content_layout(engine, tmp_path, "not_utf8")
    set_manages_changelog(engine, SLUG, False)
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    proposal = prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                               plan_digest=plan)["details"]["proposal_sha256"]
    set_manages_changelog(engine, SLUG, True)
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                      confirm=True, plan_digest=plan, proposal_digest=proposal,
                      **approved_push(root))
    assert (exc.value.details.get("changelog_file") or {}).get("problem") == "content", \
        f"apply's pre-check did not compose the changelog: {exc.value}"
    assert_nothing_released(engine, [a], root, before)


def test_k9_outside_git_bad_content_is_refused_before_the_stamp(engine, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "CHANGELOG.md").write_bytes(LATIN1)
    create_project(engine, slug=SLUG, name="P", path=str(plain), current_version="1.0.0")
    plain = Path(os.path.realpath(plain))
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    before = root_files(plain)
    with pytest.raises(ReleaseRefused):
        release(engine, plain, [a])
    assert_nothing_released(engine, [a], plain, before)


def test_k9_guard_identical_prewritten_section_still_releases(engine, tmp_path):
    """Guard (passes before the fix): the pre-check never refuses what the
    write accepts — a file already holding EXACTLY the section is kept."""
    text = "# Changelog\n\n" + released_section(today()) + "\n" + HISTORY[13:]
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": text})
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert (root / "CHANGELOG.md").read_text() == text


def test_k9_guard_changelog_off_never_reads_the_content(engine, tmp_path):
    """Guard: with dct's changelog file off the content is never composed."""
    root, _, a = content_layout(engine, tmp_path, "not_utf8")
    set_manages_changelog(engine, SLUG, False)
    out = release(engine, root, [a])
    assert out["outcome"] == "completed"
    assert (root / "CHANGELOG.md").read_bytes() == LATIN1


# ── K-8: a local tag of V refuses BEFORE the stamp ───────────────────────────


def stale_tag(root: Path, name: str, *, annotated: bool = False) -> str:
    """A tag of 1.1.0 on an unrelated commit (not an ancestor: the plan's
    range is unaffected) — a leftover of an earlier attempt."""
    orphan = commit_of(root, {"README": "old attempt\n"})
    if annotated:
        git(root, "tag", "-a", "-m", "old", name, orphan)
    else:
        git(root, "tag", name, orphan)
    return orphan


@pytest.mark.parametrize("name,annotated", [
    ("v1.1.0", False), ("1.1.0", False), ("v1.1", False), ("V1.1.0", False), ("v1.1.0", True),
])
def test_k8_prepare_refuses_a_local_tag_of_the_version(engine, tmp_path, name, annotated):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    stale_tag(root, name, annotated=annotated)
    before, before_tags = root_files(root), tags(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    assert name in str(exc.value), f"refused for another reason: {exc.value}"
    assert get_pending_proposal(engine, SLUG) is None
    assert_nothing_released(engine, [a], root, before)
    assert tags(root) == before_tags, "a tag was moved or deleted"


def test_k8_full_release_with_a_local_tag_never_stamps(engine, tmp_path):
    """Before the fix the tag was looked at only in the tag step: the
    record stopped at 'committed' with a release commit on the branch."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    stale_tag(root, "v1.1.0")
    head, before = git(root, "rev-parse", "HEAD"), root_files(root)
    with pytest.raises(ReleaseRefused):
        release(engine, root, [a])
    assert_nothing_released(engine, [a], root, before)
    assert git(root, "rev-parse", "HEAD") == head, "a release commit was made"


def test_k8_apply_refuses_a_tag_created_after_prepare(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    proposal = prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                               plan_digest=plan)["details"]["proposal_sha256"]
    stale_tag(root, "v1.1.0")
    head, before, before_tags = git(root, "rev-parse", "HEAD"), root_files(root), tags(root)
    with pytest.raises(ReleaseRefused) as exc:
        apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                      confirm=True, plan_digest=plan, proposal_digest=proposal,
                      **approved_push(root))
    assert "v1.1.0" in str(exc.value)
    assert_nothing_released(engine, [a], root, before)
    assert git(root, "rev-parse", "HEAD") == head and tags(root) == before_tags


def test_k8_audit_unknown_commit_identity_is_refused_before_the_stamp(
        engine, tmp_path, monkeypatch):
    """K-8 audit (same class): git cannot name the committer — known before
    the stamp, but checked only when the release commit is created."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    proposal = prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                               plan_digest=plan)["details"]["proposal_sha256"]
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text("[user]\n\tuseConfigOnly = true\n")
    before = root_files(root)
    with pytest.raises(ReleaseRefused):
        apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                      confirm=True, plan_digest=plan, proposal_digest=proposal,
                      **approved_push(root))
    assert_nothing_released(engine, [a], root, before)


# ── K-10: an explicit path is format-checked like a detected one ─────────────


@pytest.mark.parametrize("stored,tracked", [
    ("HISTORY.rst", {"HISTORY.rst": RST}),        # tracked
    ("NEWS.txt", {}),                             # does not exist yet
    ("docs/changes", {}),                         # no extension, new
    ("notes.adoc", {"notes.adoc": RST}),          # not a changelog-like name
])
def test_k10_explicit_non_markdown_path_is_refused(engine, tmp_path, stored, tracked):
    out = resolve_tree(engine, tmp_path, tracked, stored=stored)
    assert out["problem"] == "not_markdown", f"Markdown would be written into {stored}"
    assert out["path"] is None and out["found"] == [stored]
    assert f"dct project changelog {SLUG} off" in commands(out)


@pytest.mark.parametrize("stored", ["docs/release-notes.md", "CHANGELOG.MD", "x/NEWS.markdown"])
def test_k10_guard_explicit_markdown_path_with_any_name_is_accepted(engine, tmp_path, stored):
    """The explicit path CHOOSES the file — only its format is checked."""
    out = resolve_tree(engine, tmp_path, {}, stored=stored)
    assert out["problem"] is None and out["path"] == stored and out["source"] == "stored"


def test_k10_explicit_non_markdown_path_is_refused_before_the_stamp(engine, tmp_path):
    root, log = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    set_changelog_path(engine, SLUG, "HISTORY.rst")
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused):
        release(engine, root, [a])
    assert_nothing_released(engine, [a], root, before)
    assert (root / "HISTORY.rst").read_text() == RST
    assert not log.exists()


def test_k10_outside_git_explicit_non_markdown_path_is_refused(engine, tmp_path):
    plain = plain_project(engine, tmp_path, {"HISTORY.rst": RST})
    set_changelog_path(engine, SLUG, "HISTORY.rst")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    before = root_files(plain)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    assert exc.value.details["changelog_file"]["problem"] == "not_markdown"
    assert_nothing_released(engine, [a], plain, before)


# ── K-11: symlinks and git filters ───────────────────────────────────────────


def symlink_repo(engine, tmp_path, target: str = "docs/CHANGELOG.md") -> Path:
    root, _ = make_repo(engine, tmp_path, {"docs/CHANGELOG.md": HISTORY})
    commit_files(root, links={"CHANGELOG.md": target})
    assert git(root, "ls-tree", "HEAD", "CHANGELOG.md").startswith("120000")
    return root


def test_k11_tracked_symlink_is_its_own_problem_with_a_working_way(
        cli, tmp_path, monkeypatch):
    engine = cli
    root = symlink_repo(engine, tmp_path)
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] == "symlink", f"a symlink reported as {found['problem']}"
    assert found["found"] == ["CHANGELOG.md"]
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    assert exc.value.details["changelog_file"]["problem"] == "symlink"
    assert "commit your change" not in str(exc.value), "the not_baseline hint is false here"
    assert_nothing_released(engine, [a], root, before)
    # the way works: the release writes the link's target, the link stays
    run_way(monkeypatch, way_command(found, f"dct project changelog-path {SLUG} "))
    assert get_project(engine, SLUG)["changelog_path"] == "docs/CHANGELOG.md"
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "docs/CHANGELOG.md", out)
    assert os.readlink(root / "CHANGELOG.md") == "docs/CHANGELOG.md"
    assert git(root, "ls-tree", "HEAD", "CHANGELOG.md").startswith("120000")
    assert (root / "docs" / "CHANGELOG.md").read_text() == expected_text(engine)


def test_k11_explicit_path_to_a_symlink_is_refused_too(engine, tmp_path):
    root = symlink_repo(engine, tmp_path)
    set_changelog_path(engine, SLUG, "CHANGELOG.md")
    a = proven(engine, root, "feature a")
    out = plan_of(engine, root, [a])["changelog_file"]
    assert out["problem"] == "symlink"
    before = root_files(root)
    with pytest.raises(ReleaseRefused):
        release(engine, root, [a])
    assert_nothing_released(engine, [a], root, before)


def test_k11_symlink_leaving_the_repository_offers_only_steps_that_work(
        cli, tmp_path, monkeypatch):
    """A link whose target is outside the repository: no changelog-path the
    setter would reject is offered; turning the file off works."""
    engine = cli
    outside = tmp_path / "shared"
    outside.mkdir()
    (outside / "CHANGELOG.md").write_text(HISTORY)
    root = symlink_repo(engine, tmp_path, target=str(outside / "CHANGELOG.md"))
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] == "symlink"
    for command in commands(found):
        assert ".." not in command and str(outside) not in command, command
    run_way(monkeypatch, way_command(found, f"dct project changelog {SLUG} off"))
    out = release(engine, root, [a])
    assert out["outcome"] == "completed"
    assert (outside / "CHANGELOG.md").read_text() == HISTORY


def test_k11_outside_git_symlink_is_refused(engine, tmp_path):
    plain = plain_project(engine, tmp_path, {})
    (plain / "docs").mkdir()
    (plain / "docs" / "CHANGELOG.md").write_text(HISTORY)
    os.symlink("docs/CHANGELOG.md", plain / "CHANGELOG.md")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    before = root_files(plain)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    assert exc.value.details["changelog_file"]["problem"] == "symlink"
    assert_nothing_released(engine, [a], plain, before)
    assert os.readlink(plain / "CHANGELOG.md") == "docs/CHANGELOG.md"


FILTERS = {
    "eol_crlf": ("CHANGELOG.md text eol=crlf\n", HISTORY),
    "ident": ("CHANGELOG.md ident\n", HISTORY + "\n<!-- $Id$ -->\n"),
}


@pytest.mark.parametrize("kind", sorted(FILTERS))
def test_k11_changelog_under_a_git_filter_is_released_completely(engine, tmp_path, kind):
    """The work tree holds the FILTERED content (CRLF / an expanded $Id$):
    it is the candidate's content — the release completes, the committed
    blob is the normalised history with the section, the work tree is what
    git considers clean."""
    attributes, history = FILTERS[kind]
    root, _ = make_repo(engine, tmp_path, {".gitattributes": attributes,
                                           "CHANGELOG.md": history})
    # check the file out again through the filter (as a fresh clone holds it)
    git(root, "rm", "-q", "--cached", "CHANGELOG.md")
    git(root, "checkout", "HEAD", "--", "CHANGELOG.md")
    assert (root / "CHANGELOG.md").read_bytes() != history.encode(), \
        "precondition: the filter changes the work tree's bytes"
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    a = proven(engine, root, "feature a")
    assert plan_of(engine, root, [a])["changelog_file"]["problem"] is None
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert blob_bytes(root, "HEAD:CHANGELOG.md") == expected_text(engine, history).encode()
    assert git(root, "hash-object", "CHANGELOG.md") == git(root, "rev-parse",
                                                          "HEAD:CHANGELOG.md")
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    if kind == "eol_crlf":
        assert (root / "CHANGELOG.md").read_bytes() == \
            expected_text(engine, history).replace("\n", "\r\n").encode()


def test_k11_filter_set_after_the_checkout_gets_a_true_step(engine, tmp_path):
    """The eol attribute arrives AFTER the file was checked out: git still
    calls the LF file clean, but it is not what a checkout writes. Refused
    before the stamp with the step that clears it (never "commit your
    change" — there is nothing to commit); following it, the release
    completes."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    commit_files(root, {".gitattributes": "CHANGELOG.md text eol=crlf\n"})
    assert (root / "CHANGELOG.md").read_bytes() == HISTORY.encode()
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert (found["problem"], found["baseline"]) == ("not_baseline", "unfiltered")
    [way] = found["ways"]
    assert "git checkout HEAD -- CHANGELOG.md" in way["what"]
    assert "commit your change" not in way["what"]
    before = root_files(root)
    with pytest.raises(ReleaseRefused):
        prepare(engine, root, [a])
    assert_nothing_released(engine, [a], root, before)
    git(root, "rm", "--cached", "-q", "--", "CHANGELOG.md")
    git(root, "checkout", "HEAD", "--", "CHANGELOG.md")
    assert plan_of(engine, root, [a])["changelog_file"]["problem"] is None
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert (root / "CHANGELOG.md").read_bytes() == \
        expected_text(engine).replace("\n", "\r\n").encode()


# ── K-12: historical evidence at the release's own commit ────────────────────


def legacy_release(engine, root: Path, *, tag: bool = True) -> tuple[int, str, str]:
    """1.1.0 released by hand before records existed, into changelog.md →
    (entry, candidate, release commit)."""
    a = proven(engine, root, "feature a")
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version="1.1.0", released_at=datetime(2026, 2, 1, 10, tzinfo=UTC)))
        conn.execute(projects.update().values(current_version="1.1.0"))
    candidate = git(root, "rev-parse", "HEAD")
    head = commit_files(root, {
        "changelog.md": "# Changelog\n\n" + released_section("2026-02-01") + "\n" + HISTORY[13:],
        "pyproject.toml": '[project]\nname = "x"\nversion = "1.1.0"\n'},
        message="release: v1.1.0")
    if tag:
        git(root, "tag", "v1.1.0")
    return a, candidate, head


LATER = {
    "renamed": lambda root: (git(root, "mv", "changelog.md", "CHANGELOG.md"),
                             git(root, "commit", "-q", "-m", "docs: rename the changelog")),
    "news_added": lambda root: commit_files(root, {"NEWS.md": "# News\n"},
                                            message="docs: add news"),
}


@pytest.mark.parametrize("later", sorted(LATER))
def test_k12_reconstruct_judges_the_release_at_its_own_commit(engine, tmp_path, later):
    """Today's changelog name (or a NEWS.md added since) never changes the
    verdict on an earlier release. ('news_added' passes before the fix and
    guards K-12 against K-13's ambiguity at HEAD.)"""
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY})
    a, candidate, commit = legacy_release(engine, root)
    LATER[later](root)
    preview = reconstruct(engine, SLUG, "1.1.0", cwd=str(root))
    evidence = preview["details"]["evidence"]
    assert (evidence["candidate_sha"], evidence["release_commit_sha"]) == (candidate, commit)
    assert evidence["manifest"]["changelog"]["path"] == "changelog.md"
    out = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))
    record = out["release"]
    assert record["status"] == "completed" and record["entry_ids"] == [a]
    assert record["manifest"]["changelog"]["path"] == "changelog.md"


def test_k12_takeover_uses_the_recorded_release_not_todays_changelog(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY})
    _, _, commit = legacy_release(engine, root, tag=False)
    record = reconstruct(engine, SLUG, "1.1.0", confirm=True, cwd=str(root))["release"]
    assert record["status"] == "committed"
    LATER["renamed"](root)
    preview = effects.takeover(engine, SLUG, "1.1.0", cwd=str(root))
    assert preview["details"]["proposal"]["changelog"]["path"] == "changelog.md"
    out = effects.takeover(engine, SLUG, "1.1.0", cwd=str(root), confirm=True,
                           takeover_digest=preview["details"]["takeover_digest"])
    assert out["outcome"] == "taken_over"
    done = resume_release(engine, SLUG, cwd=str(root))
    assert done["outcome"] == "completed"
    assert git(root, "rev-parse", "v1.1.0") == commit
    assert tracked_changelogs(root) == ["CHANGELOG.md"], "the rename was undone"


# ── K-13: no privileged spelling among several candidates ────────────────────


@pytest.mark.parametrize("files", [
    {"changelog.md": HISTORY, "ChangeLog.md": HISTORY},         # case-only variants
    {"CHANGELOG.md": HISTORY, "Changelog.markdown": HISTORY},
    {"changelog.md": HISTORY, "NEWS.md": HISTORY, "CHANGES.rst": RST},
])
def test_k13_several_markdown_candidates_are_ambiguous(engine, tmp_path, files):
    out = resolve_tree(engine, tmp_path, files)
    markdown = sorted(n for n in files if n.endswith((".md", ".markdown")))
    assert out["problem"] == "ambiguous" and out["path"] is None
    assert sorted(out["found"]) == markdown
    for name in markdown:
        assert f"dct project changelog-path {SLUG} {name}" in commands(out)


def test_k13_an_explicit_default_spelling_settles_the_ambiguity(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"changelog.md": HISTORY, "CHANGES.md": HISTORY},
                       stored="changelog.md")
    assert out["problem"] is None and out["path"] == "changelog.md"
    assert out["source"] == "stored" and out["explicit"] is True


def test_k13_default_spelling_beside_another_changelog_is_refused_until_chosen(
        cli, tmp_path, monkeypatch):
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY, "CHANGES.md": HISTORY})
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "ambiguous"
    assert_nothing_released(engine, [a], root, before)
    run_way(monkeypatch, f"dct project changelog-path {SLUG} CHANGES.md")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGES.md", out)
    assert (root / "changelog.md").read_text() == HISTORY


# ── K-14: the explicit-choice marker ─────────────────────────────────────────


def test_k14_resolver_reads_the_explicit_marker_not_the_value(tmp_path):
    """The resolver decides by the marker: an explicit `changelog.md` is
    the owner's file even where detection would find another one."""
    (tmp_path / "CHANGES.md").write_text(HISTORY)
    explicit = _resolver()(project_of(changelog_path="changelog.md", **{MARKER: True}),
                           project_root=str(tmp_path))
    assert explicit["explicit"] is True
    assert explicit["source"] == "stored" and explicit["path"] == "changelog.md"
    detect = _resolver()(project_of(changelog_path="changelog.md", **{MARKER: False}),
                         project_root=str(tmp_path))
    assert detect["explicit"] is False
    assert detect["source"] == "detected" and detect["path"] == "CHANGES.md"


def test_k14_new_project_is_not_explicit(engine):
    row = create_project(engine, slug=SLUG, name="C", path="/tmp/proj-cl")
    assert row[MARKER] is False


@pytest.mark.parametrize("value", ["changelog.md", "docs/CHANGELOG.md", "/changelog.md"])
def test_k14_setter_marks_every_choice_explicit(engine, value):
    create_project(engine, slug=SLUG, name="C", path="/tmp/proj-cl")
    row = set_changelog_path(engine, SLUG, value)
    assert row[MARKER] is True
    assert get_project(engine, SLUG)[MARKER] is True


def test_k14_setter_refusal_leaves_the_marker(engine):
    create_project(engine, slug=SLUG, name="C", path="/tmp/proj-cl")
    with pytest.raises(ValueError):
        set_changelog_path(engine, SLUG, "../outside.md")
    assert get_project(engine, SLUG)[MARKER] is False


def test_k14_deliberate_default_spelling_wins_over_detection(engine, tmp_path):
    """D2: `changelog.md` chosen deliberately is written although CHANGES.md
    is tracked — CHANGES.md stays untouched."""
    root, _ = make_repo(engine, tmp_path, {"CHANGES.md": HISTORY})
    set_changelog_path(engine, SLUG, "changelog.md")
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "changelog.md", out)
    assert (root / "CHANGES.md").read_text() == HISTORY
    assert changelog_names(root) == ["CHANGES.md", "changelog.md"]


def test_k14_deliberate_default_spelling_beside_a_non_markdown_changelog(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    set_changelog_path(engine, SLUG, "changelog.md")
    a = proven(engine, root, "feature a")
    assert plan_of(engine, root, [a])["changelog_file"]["problem"] is None
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "changelog.md", out)
    assert (root / "HISTORY.rst").read_text() == RST


def test_k14_reset_goes_back_to_detection(engine, tmp_path):
    """Assumed API: reset_changelog_path(engine, slug) → the default value,
    marker false; None for an unknown slug."""
    from dct.core.projects import reset_changelog_path

    create_project(engine, slug=SLUG, name="C", path="/tmp/proj-cl")
    set_changelog_path(engine, SLUG, "docs/CHANGELOG.md")
    row = reset_changelog_path(engine, SLUG)
    assert (row["changelog_path"], row[MARKER]) == ("changelog.md", False)
    assert reset_changelog_path(engine, "proj-missing") is None


def test_k14_cli_sets_exactly_changelog_md_deliberately(cli, tmp_path, monkeypatch, capsys):
    make_repo(cli, tmp_path, {"CHANGES.md": HISTORY})
    capsys.readouterr()
    run_cli(monkeypatch, ["project", "changelog-path", SLUG, "changelog.md"])
    out = capsys.readouterr().out
    assert get_project(cli, SLUG)[MARKER] is True
    assert "changelog file: changelog.md (stored)" in out
    assert "CHANGES.md (detected)" not in out


def test_k14_cli_detect_flag_resets_the_choice(cli, tmp_path, monkeypatch, capsys):
    make_repo(cli, tmp_path, {"CHANGES.md": HISTORY})
    set_changelog_path(cli, SLUG, "changelog.md")
    capsys.readouterr()
    run_cli(monkeypatch, ["project", "changelog-path", SLUG, "--detect"])
    out = capsys.readouterr().out
    row = get_project(cli, SLUG)
    assert (row["changelog_path"], row[MARKER]) == ("changelog.md", False)
    assert "CHANGES.md (detected)" in out


def test_k14_mcp_list_projects_shows_the_marker(srv):
    from dct.mcp import server

    assert MARKER in server.PROJECT_COLS
    assert MARKER in (server.list_projects.__doc__ or "")
    [row] = [r for r in srv.list_projects(format="dict") if r["slug"] == "test"]
    assert row[MARKER] is False


_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_MARKER_RE = re.compile(rf"\b{MARKER}\b", re.IGNORECASE)


def _cols(engine) -> list[str]:
    return [c["name"] for c in sa.inspect(engine).get_columns("projects")]


def test_k14_migration_heals_once_and_never_overrides_a_later_choice(engine, capsys):
    """Three bootstraps (CLAUDE.md invariant): the first adds the marker —
    a stored non-default path is explicit, the historical default is NOT
    (its intent is unknown) —, the second and third issue no DDL, write no
    marker, print nothing, and keep a choice made after the first."""
    assert MARKER in _cols(engine), "the schema has no explicit-choice marker"
    create_project(engine, slug="proj-a", name="A", path="/tmp/proj-a")
    create_project(engine, slug="proj-b", name="B", path="/tmp/proj-b")
    create_project(engine, slug="proj-c", name="C", path="/tmp/proj-c")
    set_changelog_path(engine, "proj-b", "docs/CHANGELOG.md")
    set_changelog_path(engine, "proj-c", "changelog.md")  # history cannot tell
    with engine.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE projects DROP COLUMN {MARKER}"))
    seen: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *a: seen.append(statement.strip()))

    create_tables(engine)  # bootstrap 1 — heals
    capsys.readouterr()
    assert MARKER in _cols(engine)
    marks = {s: get_project(engine, s)[MARKER] for s in ("proj-a", "proj-b", "proj-c")}
    assert marks == {"proj-a": False, "proj-b": True, "proj-c": False}
    assert get_project(engine, "proj-b")["changelog_path"] == "docs/CHANGELOG.md"

    set_changelog_path(engine, "proj-a", "changelog.md")  # a deliberate choice now
    for n in (2, 3):
        seen.clear()
        create_tables(engine)
        ddl = [s for s in seen if _DDL.match(s)]
        writes = [s for s in seen if _DML.match(s) and _MARKER_RE.search(s)]
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        assert writes == [], f"bootstrap {n} wrote the marker: {writes}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
        marks = {s: get_project(engine, s)[MARKER] for s in ("proj-a", "proj-b", "proj-c")}
        assert marks == {"proj-a": True, "proj-b": True, "proj-c": False}


def test_k14_migration_of_a_database_older_than_the_changelog_path(engine):
    """A pre-#225 database (neither column): one bootstrap adds both; the
    project, which never chose a path, is NOT explicit."""
    assert MARKER in _cols(engine), "the schema has no explicit-choice marker"
    create_project(engine, slug="proj-old", name="Old", path="/tmp/proj-old")
    for column in (MARKER, "changelog_path"):
        with engine.begin() as conn:
            conn.execute(sa.text(f"ALTER TABLE projects DROP COLUMN {column}"))
    create_tables(engine)
    row = get_project(engine, "proj-old")
    assert (row["changelog_path"], row[MARKER]) == ("changelog.md", False)


# ── K-15: the rewrite way names the extension and the setting ────────────────


def test_k15_rewrite_way_names_the_markdown_extension_and_the_new_path(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"HISTORY.rst": RST})
    [rewrite] = [w for w in out["ways"] if w["way"] == "rewrite_markdown"]
    text = rewrite["what"]
    assert ".md" in text, "the rewritten file needs a Markdown extension"
    assert "changelog-path" in text, "the new path must be set"
    assert "git mv" in text and re.search(r"git mv[^.;]*(not|never|no)", text), \
        "must say that git mv alone does not convert the format"
    for way in out["ways"]:
        assert "other than the default" not in way["what"], "K-14 made this restriction false"


def test_k15_following_the_conversion_way_releases(cli, tmp_path, monkeypatch):
    """Rewrite + rename to .md + set the path → the release completes into
    the converted file; no second changelog."""
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    git(root, "mv", "HISTORY.rst", "HISTORY.md")
    (root / "HISTORY.md").write_text(HISTORY)
    git(root, "commit", "-qam", "docs: convert the changelog to Markdown")
    run_way(monkeypatch, f"dct project changelog-path {SLUG} HISTORY.md")
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "HISTORY.md", out)
    assert changelog_names(root) == ["HISTORY.md"]


def _rewrite_passages(text: str, size: int = 1500) -> list[str]:
    """The passages after a `not_markdown` mention that describe the rewrite."""
    starts = [m.start() for m in re.finditer("not_markdown", text)]
    return [text[s:s + size] for s in starts if re.search(r"[Rr]ewrit", text[s:s + size])]


@pytest.mark.parametrize("doc", [
    "dct/skills/release/SKILL.md", "docs/assistant-fallbacks.md", "docs/release-workflow.md"])
def test_k15_docs_describe_the_full_conversion(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    passages = _rewrite_passages(text)
    assert passages, f"{doc}: no not_markdown rewrite passage"
    assert any("git mv" in p and "changelog-path" in p and ".md" in p for p in passages), \
        f"{doc}: the conversion must name the Markdown extension, the new path and that " \
        f"git mv alone converts nothing"
    for stale in ("any name but the default", "any name other than the default"):
        assert stale not in text, f"{doc}: K-14 made `{stale}` false"


# ── K-16: restore the setting first, abandon last ────────────────────────────


def interrupted_release(engine, root: Path, a: int) -> None:
    with pytest.raises(Interrupted):
        release(engine, root, [a], on_point=interrupt_at("after_stamp"))
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"


def assert_restore_first(exc: ReleaseRefused) -> list[str]:
    found = backticked(str(exc))
    assert found, f"no next step named: {exc}"
    assert found[0].startswith("dct project "), f"restore is not offered first: {found}"
    assert found[-1].startswith("dct release abandon"), f"abandon is not last: {found}"
    return found


def test_k16_changed_path_offers_the_restore_first_and_it_works(cli, tmp_path, monkeypatch):
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    interrupted_release(engine, root, a)
    set_changelog_path(engine, SLUG, "CHANGES.md")
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, root)
    assert exc.value.details.get("approved") == "CHANGELOG.md"
    steps = assert_restore_first(exc.value)
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    assert (root / "CHANGELOG.md").read_text() == HISTORY
    assert changelog_names(root) == ["CHANGELOG.md"], "a file was written by the refusal"
    run_way(monkeypatch, steps[0])
    out = resume(engine, root)
    assert out["outcome"] == "completed"
    assert get_release(engine, SLUG, "1.1.0")["manifest"]["changelog"]["path"] == "CHANGELOG.md"
    assert (root / "CHANGELOG.md").read_text() == expected_text(engine)


@pytest.mark.parametrize("approved_on", [True, False])
def test_k16_changelog_switched_after_the_stamp_offers_switching_back(
        cli, tmp_path, monkeypatch, approved_on):
    engine = cli
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    set_manages_changelog(engine, SLUG, approved_on)
    a = proven(engine, root, "feature a")
    interrupted_release(engine, root, a)
    set_manages_changelog(engine, SLUG, not approved_on)
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, root)
    steps = assert_restore_first(exc.value)
    assert steps[0] == f"dct project changelog {SLUG} {'on' if approved_on else 'off'}"
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    assert (root / "CHANGELOG.md").read_text() == HISTORY
    run_way(monkeypatch, steps[0])
    assert resume(engine, root)["outcome"] == "completed"
    if not approved_on:
        assert (root / "CHANGELOG.md").read_text() == HISTORY


def test_k16_pre_fix_record_is_not_promised_a_restore(engine, tmp_path):
    """K-18: a record stamped by the pre-fix code (approved `changelog.md`,
    absent; the candidate tracks CHANGELOG.md) — restoring a setting cannot
    finish it, so no setting command is offered: only abandon."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    git(root, "update-index", "--skip-worktree", "CHANGELOG.md")
    parked = tmp_path / "parked-CHANGELOG.md"
    (root / "CHANGELOG.md").rename(parked)
    with pytest.MonkeyPatch.context() as mp:
        _pretend_pre_fix_resolver(mp)
        with pytest.raises(Interrupted):
            release(engine, root, [a], on_point=interrupt_at("after_stamp"))
    parked.rename(root / "CHANGELOG.md")
    git(root, "update-index", "--no-skip-worktree", "CHANGELOG.md")
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, root)
    steps = backticked(str(exc.value))
    assert [s for s in steps if s.startswith("dct project ")] == [], \
        f"a restore that cannot finish this record is offered: {steps}"
    assert steps and steps[-1].startswith("dct release abandon")
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    assert (root / "CHANGELOG.md").read_text() == HISTORY


@pytest.mark.parametrize("doc", ["dct/skills/release/SKILL.md", "docs/assistant-fallbacks.md"])
def test_k16_refusal_table_names_the_restore_before_abandon(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    [row] = [line for line in text.splitlines()
             if line.startswith("|") and "`details.approved`" in line]
    action = row.rsplit("|", 2)[-2]
    assert "changelog-path" in action or "restore" in action.lower(), row
    first_restore = min(i for i in (action.find("changelog-path"), action.lower().find("restore"))
                        if i >= 0)
    assert first_restore < action.find("abandon"), "abandon must be the last resort"


# ── K-17: every offered next step really clears the refusal ──────────────────


RECOVERABLE = {
    "ambiguous": ({"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY}, None,
                  "dct project changelog-path"),
    "case_variant": ({"CHANGELOG.md": HISTORY}, "Changelog.md", "dct project changelog-path"),
    "not_markdown_path": ({"HISTORY.rst": RST}, None, "dct project changelog-path"),
    "not_markdown_off": ({"HISTORY.rst": RST}, None, "dct project changelog "),
    "explicit_not_markdown": ({"HISTORY.rst": RST}, "HISTORY.rst",
                              "dct project changelog-path"),
}


@pytest.mark.parametrize("case", sorted(RECOVERABLE))
def test_k17_the_offered_step_clears_the_refusal(cli, tmp_path, monkeypatch, case):
    """Run the refusal's own command through the CLI → the release completes;
    every changelog file it did not choose stays byte-identical."""
    engine = cli
    files, stored, prefix = RECOVERABLE[case]
    root, _ = make_repo(engine, tmp_path, files)
    if stored:
        set_changelog_path(engine, SLUG, stored)
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    found = exc.value.details["changelog_file"]
    assert_nothing_released(engine, [a], root, before)
    run_way(monkeypatch, way_command(found, prefix))
    out = release(engine, root, [a])
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    written = (record["manifest"].get("changelog") or {}).get("path")
    for name, data in before.items():
        if name != written and name not in ("pyproject.toml", "f.txt"):
            assert (root / name).read_bytes() == data, f"{name} was modified"
    if written:
        assert written.endswith((".md", ".markdown"))


def test_k17_plan_and_refusal_share_one_resolution(engine, tmp_path):
    """The plan shows exactly what prepare refuses (one resolver)."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY})
    a = proven(engine, root, "feature a")
    shown = plan_of(engine, root, [a])["changelog_file"]
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    assert exc.value.details["changelog_file"] == shown


def test_k17_project_dict_without_marker_is_never_guessed_explicit(engine, tmp_path):
    """A resolution of the stored row (marker false) at the default value is
    detection: one tracked CHANGELOG.md is found — never a NEW changelog.md."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    project = get_project(engine, SLUG)
    assert project[MARKER] is False
    out = _resolver()(project, project_root=str(root), repo_root=str(root),
                      commit=git(root, "rev-parse", "HEAD"))
    assert out["path"] == "CHANGELOG.md" and out["source"] == "detected"


