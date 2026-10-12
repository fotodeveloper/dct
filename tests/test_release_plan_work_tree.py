"""The plan checks the changelog file as the preview will check it (K-51).

Before: the plan compared the work tree's changelog file with HEAD, so any
uncommitted edit of it was problem `not_baseline` — the dialog asked about
the changelog file and planned again, which gave the same plan: it never
reached the screen whose work-in-progress commit settles exactly that edit.
The same loop followed "convert it to Markdown" and "replace the link".

Now a new release with a dirty tree resolves and checks the changelog file
at the FUTURE TREE T1 of the work-in-progress commit — the preview's own
scope and tree builder (plan.work_scope + plan.future_tree; the preview's
candidate C* has exactly this tree). What the commit settles is no
problem; what it does not settle (an ignored file at the path) still is,
and the preview keeps checking the file at C* itself. Each settled case
runs plan → preview → ONE `apply --consent` to a completed release whose
work-in-progress commit carries the owner's change.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import git
from test_release_changelog_path import (
    RST,
    ignore_changelog,
    make_repo,
)
from test_release_effects import HISTORY, SLUG, make_release_repo, proven

from dct.core.projects import set_changelog_path
from dct.workflows.release import build_plan, effects
from dct.workflows.release import plan as planning
from dct.workflows.release.effects import ReleaseRefused

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The release makes real commits."""


# ── helpers ──────────────────────────────────────────────────────────────────


def plan_found(engine, root: Path) -> dict:
    return build_plan(engine, SLUG, cwd=str(root))["changelog_file"]


def message(tmp_path: Path, text: str = "docs: tidy the changelog\n") -> str:
    path = tmp_path / "wip-message.txt"
    path.write_text(text)
    return str(path)


def preview(engine, root: Path, msg: str) -> dict:
    return effects.preview_release(engine, SLUG, cwd=str(root), version="1.1.0",
                                   message_file=msg, variants=False)


def release_from_the_screen(engine, root: Path, msg: str) -> dict:
    """The one dialog: preview, then ONE apply --consent of the shown
    variant → the completed result."""
    doc = preview(engine, root, msg)
    assert doc["outcome"] == "preview", doc
    [shown] = doc["details"]["variants"]
    out = effects.apply_consent(engine, SLUG, shown["consent_digest"], confirm=True,
                                cwd=str(root))
    assert out["outcome"] == "completed", out["details"]
    return out


def at(root: Path, rev: str, path: str) -> str:
    return git(root, "show", f"{rev}:{path}")


# ── the three ways the work-in-progress commit settles ───────────────────────


def test_an_uncommitted_edit_of_the_changelog_is_no_plan_problem_and_releases(
        engine, tmp_path):
    """An ordinary edit of the tracked changelog: no problem in the plan
    (before: not_baseline / tracked → the question, plan again, the same
    problem), the preview commits it as work in progress, and one consent
    releases it — the release commit's changelog holds the edit AND the
    new section."""
    repo, _ = make_release_repo(engine, tmp_path)
    proven(engine, repo, "feature a")
    edited = HISTORY.replace("- first", "- first, with its typo fixed")
    (repo / "changelog.md").write_text(edited)

    found = plan_found(engine, repo)
    assert found["problem"] is None, found
    assert found["path"] == "changelog.md"

    out = release_from_the_screen(engine, repo, message(tmp_path))
    released = at(repo, "HEAD", "changelog.md")
    assert "- first, with its typo fixed" in released
    assert "## [1.1.0]" in released and "feature a" in released
    assert git(repo, "status", "--porcelain") == "", "the release left a change behind"
    wip = git(repo, "rev-parse", "HEAD~1")
    assert at(repo, wip, "changelog.md") == edited.rstrip("\n"), \
        "the edit is not in the work-in-progress commit"
    assert out["release"]["version"] == "1.1.0"


def test_a_changelog_converted_to_markdown_is_no_plan_problem_and_releases(
        engine, tmp_path):
    """The conversion way (rewrite + `git mv` to .md + choose the path),
    NOT committed: before, the plan reported not_baseline / absent at
    HISTORY.md (and its way "move HISTORY.md away" was wrong); now the
    work-in-progress commit settles it and the release writes into the
    converted file."""
    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST}, slug=SLUG)
    proven(engine, root, "feature a")
    assert plan_found(engine, root)["problem"] == "not_markdown"
    git(root, "mv", "HISTORY.rst", "HISTORY.md")
    (root / "HISTORY.md").write_text(HISTORY)
    set_changelog_path(engine, SLUG, "HISTORY.md")

    found = plan_found(engine, root)
    assert found["problem"] is None, found
    assert found["path"] == "HISTORY.md"

    release_from_the_screen(engine, root, message(tmp_path, "docs: convert the changelog\n"))
    released = at(root, "HEAD", "HISTORY.md")
    assert "## [1.1.0]" in released and "- first" in released
    assert git(root, "ls-tree", "HEAD", "HISTORY.rst") == "", "the old file is still tracked"


def test_a_changelog_link_replaced_by_the_file_is_no_plan_problem_and_releases(
        engine, tmp_path):
    """The replace-link way (`git rm` the link, `git mv` the target in its
    place), NOT committed: before, the plan still reported the HEAD's
    `symlink`; now the plan resolves at the work-in-progress tree, where the
    path is a regular file, and the release writes it."""
    root, _ = make_repo(engine, tmp_path, {"docs/CHANGELOG.md": HISTORY}, slug=SLUG)
    (root / "CHANGELOG.md").symlink_to("docs/CHANGELOG.md")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore: link")
    proven(engine, root, "feature a")
    assert plan_found(engine, root)["problem"] == "symlink"
    git(root, "rm", "-q", "CHANGELOG.md")
    git(root, "mv", "docs/CHANGELOG.md", "CHANGELOG.md")

    found = plan_found(engine, root)
    assert found["problem"] is None, found
    assert found["path"] == "CHANGELOG.md"

    release_from_the_screen(engine, root, message(tmp_path, "docs: replace the link\n"))
    assert git(root, "ls-tree", "HEAD", "CHANGELOG.md").startswith("100644")
    assert "## [1.1.0]" in at(root, "HEAD", "CHANGELOG.md")


# ── a difference the work-in-progress commit does not settle ─────────────────


def test_an_ignored_file_at_the_changelog_path_stays_a_problem_with_a_dirty_tree(
        engine, tmp_path):
    """A real mismatch: an IGNORED changelog.md the candidate does not hold
    — the work-in-progress commit never takes it along, so the plan
    (with or without other uncommitted work) and the preview both report
    not_baseline / absent, and nothing is stored."""
    root, _ = make_repo(engine, tmp_path, {}, slug=SLUG)
    proven(engine, root, "feature a")
    ignore_changelog(root)
    assert plan_found(engine, root)["problem"] == "not_baseline"
    (root / "f.txt").write_text("other work\n")  # a dirty tree: T1 is built

    found = plan_found(engine, root)
    assert (found["problem"], found["baseline"]) == ("not_baseline", "absent"), found
    with pytest.raises(ReleaseRefused) as exc:
        preview(engine, root, message(tmp_path))
    refused = exc.value.details["changelog_file"]
    assert (refused["problem"], refused["baseline"]) == ("not_baseline", "absent")


def test_with_a_filtered_path_in_the_change_the_plan_checks_against_head(
        engine, tmp_path):
    """No future tree is built over a path with a `filter` attribute (the
    preview commits nothing then — `filtered_paths`): the plan's check stays
    against HEAD, and its way (commit the change yourself) is the true one."""
    repo, _ = make_release_repo(engine, tmp_path)
    (repo / ".gitattributes").write_text("*.bin filter=fake\n")
    (repo / "data.bin").write_text("v0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: data")
    proven(engine, repo, "feature a")
    (repo / "data.bin").write_text("v1\n")
    (repo / "changelog.md").write_text(HISTORY + "\n- edited\n")

    found = plan_found(engine, repo)
    assert (found["problem"], found["baseline"]) == ("not_baseline", "tracked"), found
    with pytest.raises(ReleaseRefused) as exc:
        preview(engine, repo, message(tmp_path))
    assert exc.value.details["filtered_paths"] == ["data.bin"]


# ── one implementation ───────────────────────────────────────────────────────


def test_the_plan_and_the_preview_build_the_same_future_tree(engine, tmp_path):
    """plan.future_tree over plan.work_scope IS the preview's T1 (the tree
    of its work-in-progress candidate) — one builder, not two."""
    repo, _ = make_release_repo(engine, tmp_path)
    proven(engine, repo, "feature a")
    (repo / "changelog.md").write_text(HISTORY + "\n- edited\n")
    (repo / "f.txt").write_text("changed\n")
    head = git(repo, "rev-parse", "HEAD")
    index = git(repo, "ls-files", "-s")
    scope, _ = planning.work_scope(planning.status_v2(str(repo)), "changelog.md",
                                   ["pyproject.toml"])
    assert scope == ["changelog.md", "f.txt"]
    tree = planning.future_tree(str(repo), head, scope)
    doc = preview(engine, repo, message(tmp_path))
    assert doc["details"]["work"]["tree"] == tree
    assert git(repo, "ls-files", "-s") == index, "building the future tree wrote the index"


def test_a_future_tree_git_cannot_build_falls_back_to_head_with_a_warning(
        engine, tmp_path, monkeypatch):
    """A repository whose object store refuses writes: the plan still
    answers — the check is against HEAD (the old, conservative one) and a
    warning says why."""
    repo, _ = make_release_repo(engine, tmp_path)
    proven(engine, repo, "feature a")
    (repo / "changelog.md").write_text(HISTORY + "\n- edited\n")

    def refuse(*args, **kwargs):
        raise planning.gitinfo.GitWriteError("read-only object store")

    monkeypatch.setattr(planning.gitinfo, "git_write", refuse)
    plan = build_plan(engine, SLUG, cwd=str(repo))
    assert plan["changelog_file"]["problem"] == "not_baseline"
    assert any("work-in-progress commit could not be built" in w for w in plan["warnings"])
