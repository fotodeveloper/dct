"""Which file is the project's changelog (#876) — ONE resolver
(dct.core.changelog_file.resolve_changelog) for plan, prepare, apply,
resume, the CLI and project registration, and every changelog-file
condition that can refuse checked BEFORE the stamp.

The defect: a project without a stored changelog_path got the assumed
name `changelog.md`; the repository tracked `CHANGELOG.md`. plan / prepare
looked the path up in the candidate tree (case-sensitive) and recorded the
changelog as absent; apply STAMPED, then read the path from disk (macOS:
case-insensitive → the existing CHANGELOG.md) and refused as "foreign"
with a false "restore the file(s) yourself" hint — a record left
'stamped', current_version moved, nothing committed. On a case-sensitive
file system the same setup created a SECOND file `changelog.md`.

The contract pinned here:
- no stored path → detection in the project root among TRACKED files
  named CHANGELOG / CHANGES / HISTORY / NEWS / RELEASES (case-insensitive)
  with .md / .markdown (Markdown) or none / .rst / .txt / .adoc (other):
  one Markdown candidate → its exact tracked spelling; none at all → the
  default `changelog.md` (a new file); several Markdown → refused, the
  default spelling not privileged (K-13); only non-Markdown → refused (dct
  never writes Markdown into it, never creates a second changelog beside
  it); a tracked `changelog.md` alone is simply detected (dct's own
  repository);
- a stored path (projects.changelog_path_explicit, K-14) wins when it is
  consistent — also exactly `changelog.md`; a stored path whose
  case-variant is tracked is refused;
- manages_changelog off skips all of it;
- after ANY refusal about the changelog file: no record, no stamped
  entry, current_version unchanged, no file created or changed.

Tests are written so they fail on BOTH file systems before the fix: they
compare exact directory listings (os.listdir spellings), never
os.path.exists of a case-variant.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC
from pathlib import Path

import pytest
from conftest import (
    approved_plan_digest,
    approved_proposal_digest,
    approved_push,
    git,
    make_git_repo,
    run_cli,
)
from test_release_effects import (
    HISTORY,
    Interrupted,
    interrupt_at,
    released_section,
    write_sync_script,
)

from dct.core.changelog import add_entry, list_entries
from dct.core.projects import (
    create_project,
    get_project,
    set_changelog_path,
    set_manages_changelog,
)
from dct.core.releases import get_pending_proposal, get_release, set_version_files
from dct.workflows.release import build_plan
from dct.workflows.release.effects import (
    ReleaseRefused,
    apply_release,
    prepare_release,
    resume_release,
)
from dct.workflows.release.plan import PLAN_KEYS

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SLUG = "proj-cl"
NAMES = ("changelog", "changes", "history", "news", "releases")
RST = "Changelog\n=========\n\n1.0.0 (2026-01-01)\n------------------\n\n* first\n"


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The effects layer creates commits."""


# ── builders ─────────────────────────────────────────────────────────────────


def _resolver():
    """The resolver under test — imported lazily so a missing module fails
    only the tests that call it, not the whole module."""
    from dct.core.changelog_file import resolve_changelog

    return resolve_changelog


def make_repo(engine, tmp_path, files: dict[str, str], *, subdir: str = "",
              slug: str = SLUG) -> tuple[Path, Path]:
    """A repository holding `files` (repo-relative), a project rooted at
    `subdir` (default: the repo root) with pyproject.toml 1.0.0 and a
    committed sync script logging to sync.log (outside the repo), tagged
    v1.0.0, `main` tracking a bare remote; the version-file contract is the
    project's pyproject.toml. No changelog_path is stored. Returns
    (project root, sync log)."""
    repo = Path(make_git_repo(tmp_path / "repo"))
    root = repo / subdir if subdir else repo
    root.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "sync.log"
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n')
    write_sync_script(root, log)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "chore: setup")
    git(repo, "tag", "v1.0.0")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main", "v1.0.0")
    create_project(engine, slug=slug, name="C", path=str(root), current_version="1.0.0")
    prefix = f"{subdir.strip('/')}/" if subdir else ""
    set_version_files(engine, slug, [prefix + "pyproject.toml"], confirm=True)
    return root, log


def proven(engine, root: Path, text: str) -> int:
    """An unreleased entry plus a commit that proves it (trailer)."""
    eid = add_entry(engine, SLUG, "added", text)["id"]
    with open(root / "f.txt", "a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    git(root, "add", "--", "f.txt")
    git(root, "commit", "-q", "--cleanup=verbatim", "-m", f"feat: {text}\n\nChangelog: {eid}\n")
    return eid


def plan_of(engine, root: Path, ids: list[int]) -> dict:
    return build_plan(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=ids)


def prepare(engine, root: Path, ids: list[int]) -> dict:
    digest = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=ids)
    return prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=ids,
                           plan_digest=digest)


def release(engine, root: Path, ids: list[int], **kw) -> dict:
    """apply with the owner's approvals: the plan digest, the proposal of a
    prepare for the same selection, the push target prepare shows."""
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=ids)
    proposal = kw.pop("proposal_digest", None) or approved_proposal_digest(
        engine, SLUG, root, plan_digest=plan, version="1.1.0", entry_ids=ids)
    return apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=ids,
                         confirm=True, plan_digest=plan, proposal_digest=proposal,
                         **kw, **approved_push(root))


def resume(engine, root: Path) -> dict:
    return resume_release(engine, SLUG, cwd=str(root))


def is_changelog_name(name: str) -> bool:
    stem = name.split(".", 1)[0]
    return stem.casefold() in NAMES


def changelog_names(directory: Path) -> list[str]:
    """The EXACT spellings of changelog-like files in `directory` — works on
    any file system (a case-insensitive one never lists a phantom alias)."""
    return sorted(n for n in os.listdir(directory) if is_changelog_name(n))


def tracked_changelogs(repo: Path, rev: str = "HEAD") -> list[str]:
    """The exact spellings of changelog-like files tracked at the repo root."""
    names = git(repo, "ls-tree", "--name-only", rev)
    return sorted(n for n in names.splitlines() if is_changelog_name(n))


def root_files(directory: Path) -> dict[str, bytes]:
    """{name: bytes} of every regular file directly in `directory`."""
    return {n: (directory / n).read_bytes() for n in os.listdir(directory)
            if (directory / n).is_file()}


def assert_nothing_released(engine, ids: list[int], directory: Path,
                            before: dict[str, bytes]) -> None:
    """The invariant of every refusal about the changelog file (owner
    decision 1): nothing stamped, nothing recorded, nothing written."""
    assert get_release(engine, SLUG, "1.1.0") is None, "a refusal left a release record"
    assert get_release(engine, SLUG) is None, "an unfinished record exists after a refusal"
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    versions = {e["id"]: e["version"] for e in list_entries(engine, SLUG) if e["id"] in ids}
    assert versions == {i: None for i in ids}, "an entry carries the version after a refusal"
    assert root_files(directory) == before, "a file was created or changed by a refusal"


def record_date(engine) -> str:
    created = get_release(engine, SLUG, "1.1.0")["created_at"]
    if created.tzinfo is not None:
        created = created.astimezone(UTC)
    return created.date().isoformat()


def expected_text(engine, history: str = HISTORY) -> str:
    """`history` (a '# Changelog' document) with the 1.1.0 section inserted."""
    head = "# Changelog\n\n"
    assert history.startswith(head)
    return head + released_section(record_date(engine)) + "\n" + history[len(head):]


def assert_completed_into(engine, root: Path, path: str, out: dict) -> dict:
    """The release completed and wrote its section into `path` (base-relative)."""
    assert out["outcome"] == "completed", out
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed"
    assert record["proposal"]["changelog"]["path"] == path
    assert record["manifest"]["changelog"]["path"] == path
    steps = [s for s in out["steps"] if s["step"] == "changelog"]
    assert [s.get("path") for s in steps] == [path]
    return record


def fs_case_sensitive(directory: Path) -> bool:
    probe_dir = directory / "case-probe"
    probe_dir.mkdir(exist_ok=True)
    (probe_dir / "probe").write_text("x")
    return not (probe_dir / "PROBE").exists()


def _git_input(repo: Path, data: bytes, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    result = subprocess.run(["git", "-C", str(repo), *args], input=data, env=env,
                            capture_output=True, check=True)
    return result.stdout.decode().strip()


def commit_of(repo: Path, files: dict[str, str]) -> str:
    """A parentless commit whose tree holds exactly `files` (repo-relative),
    built with hash-object / mktree — independent of the work tree, so a
    tree holding BOTH `changelog.md` and `CHANGELOG.md` can be built on a
    case-insensitive file system too."""

    def tree(entries: dict[str, str]) -> str:
        direct: dict[str, str] = {}
        nested: dict[str, dict[str, str]] = {}
        for rel, text in entries.items():
            head, _, rest = rel.partition("/")
            if rest:
                nested.setdefault(head, {})[rest] = text
            else:
                direct[head] = text
        lines = [f"100644 blob {_git_input(repo, t.encode(), 'hash-object', '-w', '--stdin')}"
                 f"\t{n}" for n, t in direct.items()]
        lines += [f"040000 tree {tree(sub)}\t{n}" for n, sub in nested.items()]
        return _git_input(repo, "".join(line + "\n" for line in lines).encode(), "mktree")

    return _git_input(repo, b"", "commit-tree", tree(files), "-m", "layout")


def resolve_tree(engine, tmp_path, files: dict[str, str], *, stored: str | None = None,
                 manages: bool = True, subdir: str = "") -> dict:
    """resolve_changelog for a project whose candidate commit holds exactly
    `files` (the work tree holds none of them)."""
    repo = Path(make_git_repo(tmp_path / "tree-repo"))
    root = repo / subdir if subdir else repo
    root.mkdir(parents=True, exist_ok=True)
    sha = commit_of(repo, {"README": "x\n", **files})
    create_project(engine, slug=SLUG, name="C", path=str(root))
    if stored is not None:
        set_changelog_path(engine, SLUG, stored)
    if not manages:
        set_manages_changelog(engine, SLUG, False)
    return _resolver()(get_project(engine, SLUG), project_root=os.path.realpath(root),
                       repo_root=str(repo), commit=sha)


def commands(resolution: dict) -> list[str]:
    return [w.get("command") or "" for w in resolution["ways"]]


# ── the resolver (unit, on commits built with mktree) ────────────────────────


@pytest.mark.parametrize("name", ["CHANGELOG.md", "CHANGES.md", "History.markdown",
                                  "NEWS.md", "RELEASES.md", "Changes.MD"])
def test_resolver_detects_the_one_tracked_markdown_changelog_in_its_exact_spelling(
        engine, tmp_path, name):
    out = resolve_tree(engine, tmp_path, {name: HISTORY})
    assert out["managed"] is True and out["problem"] is None
    assert out["path"] == name, "detection must keep the TRACKED spelling, never lower-case it"
    assert out["source"] == "detected" and out["format"] == "markdown"
    assert [c["path"] for c in out["candidates"]] == [name]


def test_resolver_without_any_candidate_falls_back_to_the_default_new_file(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"src/app.py": "x\n"})
    assert out["path"] == "changelog.md" and out["source"] == "default"
    assert out["problem"] is None and out["candidates"] == []
    assert out["configured"] == "changelog.md" and out["explicit"] is False


@pytest.mark.parametrize("other", [
    "CHANGELOG-old.md", "changelog.md.bak", "MY_CHANGELOG.md", "CHANGELOGS.md",
    "changelog.mdx", "docs/CHANGELOG.md", "changelog/1.0.md",
])
def test_resolver_ignores_near_miss_names_and_files_outside_the_project_root(
        engine, tmp_path, other):
    """Only an exact stem + a known extension directly in the project root
    is a candidate — a directory, a nested file or a longer name never is."""
    out = resolve_tree(engine, tmp_path, {other: HISTORY})
    assert out["candidates"] == [], f"{other} must not be a changelog candidate"
    assert out["path"] == "changelog.md" and out["source"] == "default"


def test_resolver_keeps_the_tracked_default_spelling(engine, tmp_path):
    """dct's own repository (and every project where dct created the file)
    writes the same file as before — found by detection, the one Markdown
    candidate (K-13: no special rule for the default spelling)."""
    out = resolve_tree(engine, tmp_path, {"changelog.md": HISTORY})
    assert out["path"] == "changelog.md" and out["problem"] is None
    assert out["source"] == "detected"


@pytest.mark.parametrize("other", ["CHANGELOG.md", "CHANGES.md"])
def test_resolver_tracked_default_spelling_is_not_privileged_among_candidates(
        engine, tmp_path, other):
    """K-13 (owner decision, flips D1): a tracked `changelog.md` beside
    another Markdown changelog — a case-only variant included — is
    `ambiguous`, refused until the owner chooses; every file is offered."""
    out = resolve_tree(engine, tmp_path, {"changelog.md": HISTORY, other: HISTORY})
    assert out["problem"] == "ambiguous" and out["path"] is None
    assert sorted(out["found"]) == sorted(["changelog.md", other])
    for name in ("changelog.md", other):
        assert f"dct project changelog-path {SLUG} {name}" in commands(out)


@pytest.mark.parametrize("name", ["HISTORY.rst", "CHANGELOG", "CHANGES.txt", "NEWS.adoc"])
def test_resolver_refuses_to_guess_a_non_markdown_changelog(engine, tmp_path, name):
    out = resolve_tree(engine, tmp_path, {name: RST})
    assert out["problem"] == "not_markdown"
    assert out["path"] is None, "dct must never write Markdown into a non-Markdown changelog"
    assert out["candidates"] == [{"path": name, "format": "other"}]
    assert len(out["ways"]) == 3
    assert any(c.startswith(f"dct project changelog-path {SLUG} ") for c in commands(out))
    assert f"dct project changelog {SLUG} off" in commands(out)


def test_resolver_prefers_the_markdown_file_beside_a_non_markdown_one(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.rst": RST})
    assert out["path"] == "CHANGELOG.md" and out["source"] == "detected"
    assert out["problem"] is None
    assert sorted((c["path"], c["format"]) for c in out["candidates"]) == [
        ("CHANGELOG.md", "markdown"), ("CHANGES.rst", "other")]


def test_resolver_does_not_guess_between_two_markdown_changelogs(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY})
    assert out["problem"] == "ambiguous" and out["path"] is None
    assert sorted(c["path"] for c in out["candidates"]) == ["CHANGELOG.md", "CHANGES.md"]
    assert any(c.startswith(f"dct project changelog-path {SLUG} ") for c in commands(out))


def test_resolver_an_explicit_choice_settles_an_ambiguous_layout(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY},
                       stored="CHANGES.md")
    assert out["path"] == "CHANGES.md" and out["source"] == "stored"
    assert out["problem"] is None and out["explicit"] is True


def test_resolver_explicit_consistent_path_wins(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"docs/CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY},
                       stored="docs/CHANGELOG.md")
    assert out["path"] == "docs/CHANGELOG.md" and out["source"] == "stored"
    assert out["problem"] is None


def test_resolver_explicit_path_not_tracked_is_a_new_file(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"HISTORY.rst": RST}, stored="CHANGELOG.md")
    assert out["path"] == "CHANGELOG.md" and out["source"] == "stored"
    assert out["problem"] is None


@pytest.mark.parametrize("stored,tracked", [
    ("Changelog.md", "CHANGELOG.md"),
    ("docs/changelog.md", "docs/CHANGELOG.md"),
    ("docs/CHANGELOG.md", "Docs/CHANGELOG.md"),
])
def test_resolver_refuses_a_stored_path_whose_case_variant_is_tracked(
        engine, tmp_path, stored, tracked):
    """Never silently rewritten: on macOS the stored spelling would open the
    tracked file, on Linux it would create a second one."""
    out = resolve_tree(engine, tmp_path, {tracked: HISTORY}, stored=stored)
    assert out["problem"] == "case_variant" and out["path"] is None
    assert out["configured"] == stored and out["found"] == [tracked]
    assert any(c.startswith(f"dct project changelog-path {SLUG} ") for c in commands(out))


def test_resolver_with_dct_changelog_off_does_nothing(engine, tmp_path):
    """manages_changelog off skips ALL of it — even a layout that would be
    refused is no problem then."""
    out = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY},
                       manages=False)
    assert out["managed"] is False and out["path"] is None and out["problem"] is None


def test_resolver_in_a_monorepo_looks_only_in_the_project_root(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY, "packages/app/x.py": "x\n"},
                       subdir="packages/app")
    assert out["path"] == "packages/app/changelog.md", "never the repository's root changelog"
    assert out["source"] == "default" and out["candidates"] == []


def test_resolver_in_a_monorepo_detects_the_subprojects_own_changelog(engine, tmp_path):
    out = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY,
                                          "packages/app/CHANGES.md": HISTORY},
                       subdir="packages/app")
    assert out["path"] == "packages/app/CHANGES.md" and out["source"] == "detected"


def test_resolver_reads_the_given_commit_not_head_or_the_work_tree(engine, tmp_path):
    """Deterministic for a resume after HEAD moved: the candidate decides."""
    repo = Path(make_git_repo(tmp_path / "moving"))
    (repo / "CHANGELOG.md").write_text(HISTORY)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "c1")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "CHANGELOG.md", "CHANGES.md")
    git(repo, "commit", "-q", "-m", "rename")
    create_project(engine, slug=SLUG, name="C", path=str(repo))
    project = get_project(engine, SLUG)
    at_candidate = _resolver()(project, project_root=project["path"], repo_root=str(repo),
                               commit=candidate)
    at_head = _resolver()(project, project_root=project["path"], repo_root=str(repo),
                          commit=git(repo, "rev-parse", "HEAD"))
    assert at_candidate["path"] == "CHANGELOG.md"
    assert at_head["path"] == "CHANGES.md"


def test_resolver_outside_git_detects_on_disk(engine, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "CHANGELOG.md").write_text(HISTORY)
    create_project(engine, slug=SLUG, name="C", path=str(plain))
    out = _resolver()(get_project(engine, SLUG), project_root=os.path.realpath(plain))
    assert out["path"] == "CHANGELOG.md" and out["source"] == "detected"


def test_resolver_result_is_json_native_and_stable(engine, tmp_path):
    first = resolve_tree(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.rst": RST})
    assert json.loads(json.dumps(first)) == first
    assert set(first) >= {"managed", "configured", "explicit", "path", "source", "format",
                          "candidates", "problem", "found", "ways"}


# ── the plan reports, read-only ──────────────────────────────────────────────


def test_plan_keys_include_the_changelog_file():
    assert "changelog_file" in PLAN_KEYS


def test_plan_reports_the_detected_changelog_file(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    plan = plan_of(engine, root, [a])
    found = plan["changelog_file"]
    assert found["path"] == "CHANGELOG.md" and found["source"] == "detected"
    assert found["problem"] is None


def test_plan_reports_a_non_markdown_changelog_with_the_three_ways(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] == "not_markdown" and found["path"] is None
    assert len(found["ways"]) == 3
    assert f"dct project changelog {SLUG} off" in commands(found)


def test_plan_reports_an_ambiguous_layout_with_a_warning(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY})
    a = proven(engine, root, "feature a")
    plan = plan_of(engine, root, [a])
    assert plan["changelog_file"]["problem"] == "ambiguous"
    assert any("CHANGES.md" in w and "CHANGELOG.md" in w for w in plan["warnings"])


def test_plan_stores_nothing(engine, tmp_path):
    """Read-only commands use the detected path; they never store it."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    before = get_project(engine, SLUG)
    plan_of(engine, root, [a])
    after = get_project(engine, SLUG)
    assert after["changelog_path"] == before["changelog_path"] == "changelog.md"
    assert after["changelog_path_explicit"] is before["changelog_path_explicit"] is False
    assert after["updated_at"] == before["updated_at"]
    assert get_pending_proposal(engine, SLUG) is None


def test_plan_reports_an_ignored_file_at_the_changelog_path(engine, tmp_path):
    """The candidate holds no changelog, but an IGNORED changelog.md sits
    on disk: what the release would overwrite is not the baseline."""
    root, _ = make_repo(engine, tmp_path, {})
    a = proven(engine, root, "feature a")
    ignore_changelog(root)
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["problem"] == "not_baseline"
    assert found["found"] == ["changelog.md"]


def test_plan_of_the_tracked_default_spelling_is_unchanged(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY})
    a = proven(engine, root, "feature a")
    found = plan_of(engine, root, [a])["changelog_file"]
    assert found["path"] == "changelog.md" and found["source"] == "detected"
    assert found["problem"] is None


# ── the central regression (#876) ────────────────────────────────────────────


def test_uppercase_changelog_is_released_into_the_tracked_file(engine, tmp_path):
    """#876: CHANGELOG.md tracked, nothing stored → the release completes,
    the section lands in CHANGELOG.md, and no second changelog appears —
    neither on disk nor in the release commit."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    candidate = git(root, "rev-parse", "HEAD")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert changelog_names(root) == ["CHANGELOG.md"], "a second changelog file appeared"
    assert tracked_changelogs(root) == ["CHANGELOG.md"]
    assert (root / "CHANGELOG.md").read_text() == expected_text(engine)
    assert set(git(root, "diff", "--name-only", candidate, "HEAD").split()) == {
        "CHANGELOG.md", "pyproject.toml"}
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    assert get_project(engine, SLUG)["current_version"] == "1.1.0"


LAYOUTS = {
    "upper": {"CHANGELOG.md": HISTORY},
    "changes": {"CHANGES.md": HISTORY},
    "mixed_case_markdown": {"Changelog.markdown": HISTORY},
    "rst_only": {"HISTORY.rst": RST},
    "no_extension": {"CHANGELOG": RST},
    "two_markdown": {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY},
    "markdown_and_rst": {"CHANGELOG.md": HISTORY, "NEWS.rst": RST},
    "default_spelling": {"changelog.md": HISTORY},
    "none": {},
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_no_changelog_layout_leaves_a_stamped_record_or_a_second_changelog(
        engine, tmp_path, layout):
    """The SAFETY invariant over every layout (owner decision 1): the
    release either completes — into one existing Markdown changelog, or a
    new one only when the candidate tracks no changelog at all — or is
    refused BEFORE the stamp with nothing recorded and nothing written. A
    non-Markdown changelog is never modified."""
    files = LAYOUTS[layout]
    root, _ = make_repo(engine, tmp_path, files)
    a = proven(engine, root, "feature a")
    names_before = changelog_names(root)
    before = root_files(root)
    try:
        out = release(engine, root, [a])
    except ReleaseRefused:
        assert_nothing_released(engine, [a], root, before)
        return
    assert out["outcome"] == "completed"
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed"
    written = record["manifest"]["changelog"]["path"]
    assert written.endswith((".md", ".markdown")), f"dct wrote Markdown into {written}"
    if names_before:
        assert written in names_before, f"{written} is a new file beside {names_before}"
        assert changelog_names(root) == names_before, "a second changelog file appeared"
    else:
        assert changelog_names(root) == [written]
    for name in names_before:
        if name != written:
            assert (root / name).read_bytes() == before[name], f"{name} was modified"


# ── refusals BEFORE the stamp ────────────────────────────────────────────────


def ignore_changelog(root: Path, text: str = "# my private notes\n") -> None:
    """An IGNORED changelog.md (not in the tree, `status` stays clean)."""
    exclude = Path(git(root, "rev-parse", "--absolute-git-dir")) / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as fh:
        fh.write("/changelog.md\n")
    (root / "changelog.md").write_text(text)
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""


def test_non_markdown_changelog_is_refused_by_prepare_with_three_true_next_steps(
        engine, tmp_path):
    root, log = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "not_markdown"
    message = str(exc.value)
    assert "HISTORY.rst" in message and "changelog.md" in message
    assert "markdown" in message.lower()
    assert f"dct project changelog-path {SLUG}" in message
    assert f"dct project changelog {SLUG} off" in message
    assert get_pending_proposal(engine, SLUG) is None
    assert not log.exists(), "the version sync ran although the changelog was refused"
    assert_nothing_released(engine, [a], root, before)


def test_non_markdown_changelog_is_refused_by_apply_before_the_stamp(engine, tmp_path):
    """A proposal prepared for an explicit path; the setting is reset to
    detection before apply → apply's own pre-check refuses, nothing stamped.
    (K-14: storing `changelog.md` is an explicit choice now — the reset is
    reset_changelog_path.)"""
    from dct.core.projects import reset_changelog_path

    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    a = proven(engine, root, "feature a")
    set_changelog_path(engine, SLUG, "CHANGELOG.md")
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    proposal = prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                               plan_digest=plan)["details"]["proposal_sha256"]
    reset_changelog_path(engine, SLUG)
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                      confirm=True, plan_digest=plan, proposal_digest=proposal,
                      **approved_push(root))
    assert exc.value.details["changelog_file"]["problem"] == "not_markdown"
    assert_nothing_released(engine, [a], root, before)


def test_non_markdown_changelog_with_dct_changelog_off_is_released_untouched(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    set_manages_changelog(engine, SLUG, False)
    a = proven(engine, root, "feature a")
    assert plan_of(engine, root, [a])["changelog_file"]["managed"] is False
    out = release(engine, root, [a])
    assert out["outcome"] == "completed"
    assert [s["result"] for s in out["steps"] if s["step"] == "changelog"] == ["skipped"]
    assert (root / "HISTORY.rst").read_text() == RST
    assert changelog_names(root) == ["HISTORY.rst"]


def test_non_markdown_changelog_with_an_explicit_markdown_path_creates_that_file(
        engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"HISTORY.rst": RST})
    set_changelog_path(engine, SLUG, "CHANGELOG.md")
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert changelog_names(root) == ["CHANGELOG.md", "HISTORY.rst"]
    assert (root / "HISTORY.rst").read_text() == RST
    assert (root / "CHANGELOG.md").read_text().count("## [1.1.0]") == 1


def test_markdown_beside_non_markdown_is_released_into_the_markdown_file(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.rst": RST})
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGELOG.md", out)
    assert (root / "CHANGELOG.md").read_text() == expected_text(engine)
    assert (root / "CHANGES.rst").read_text() == RST
    assert changelog_names(root) == ["CHANGELOG.md", "CHANGES.rst"]


def test_changes_md_only_is_detected_and_released(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGES.md": HISTORY})
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGES.md", out)
    assert (root / "CHANGES.md").read_text() == expected_text(engine)
    assert changelog_names(root) == ["CHANGES.md"]


def test_two_markdown_changelogs_are_refused_until_the_owner_chooses(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY})
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    assert exc.value.details["changelog_file"]["problem"] == "ambiguous"
    message = str(exc.value)
    assert "CHANGELOG.md" in message and "CHANGES.md" in message
    assert f"dct project changelog-path {SLUG}" in message
    assert_nothing_released(engine, [a], root, before)
    # the next step the refusal names really works
    set_changelog_path(engine, SLUG, "CHANGES.md")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "CHANGES.md", out)
    assert (root / "CHANGELOG.md").read_text() == HISTORY


def test_stored_case_variant_is_refused_before_the_stamp(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    set_changelog_path(engine, SLUG, "Changelog.md")
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "case_variant" and found["found"] == ["CHANGELOG.md"]
    message = str(exc.value)
    assert "Changelog.md" in message and "CHANGELOG.md" in message
    assert f"dct project changelog-path {SLUG}" in message
    assert_nothing_released(engine, [a], root, before)
    assert get_project(engine, SLUG)["changelog_path"] == "Changelog.md", "silently rewritten"


def test_dct_changelog_off_skips_detection_entirely(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY, "CHANGES.md": HISTORY})
    set_manages_changelog(engine, SLUG, False)
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert out["outcome"] == "completed"
    assert [s["result"] for s in out["steps"] if s["step"] == "changelog"] == ["skipped"]
    assert (root / "CHANGELOG.md").read_text() == HISTORY
    assert (root / "CHANGES.md").read_text() == HISTORY
    assert changelog_names(root) == ["CHANGELOG.md", "CHANGES.md"]


def test_ignored_changelog_is_refused_by_prepare(engine, tmp_path):
    root, log = make_repo(engine, tmp_path, {})
    a = proven(engine, root, "feature a")
    ignore_changelog(root)
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, [a])
    found = exc.value.details["changelog_file"]
    assert found["problem"] == "not_baseline" and found["found"] == ["changelog.md"]
    assert "restore the file" not in str(exc.value).lower()
    assert not log.exists()
    assert_nothing_released(engine, [a], root, before)


def test_ignored_changelog_appearing_before_apply_is_refused_before_the_stamp(
        engine, tmp_path):
    """File-system independent reproduction of #876's ORDER defect: today
    apply stamps first and only then reads the file on disk."""
    root, _ = make_repo(engine, tmp_path, {})
    a = proven(engine, root, "feature a")
    plan = approved_plan_digest(engine, SLUG, root, version="1.1.0", entry_ids=[a])
    proposal = prepare_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                               plan_digest=plan)["details"]["proposal_sha256"]
    ignore_changelog(root)
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        apply_release(engine, SLUG, cwd=str(root), version="1.1.0", entry_ids=[a],
                      confirm=True, plan_digest=plan, proposal_digest=proposal,
                      **approved_push(root))
    assert_nothing_released(engine, [a], root, before)
    assert exc.value.details["changelog_file"]["problem"] == "not_baseline"
    assert "restore the file" not in str(exc.value).lower()


def test_ignored_changelog_appearing_after_the_stamp_gets_a_true_hint(engine, tmp_path):
    """The file is not dct's and not the baseline: the refusal says what is
    there and never tells the owner to 'restore' a file dct never touched."""
    root, _ = make_repo(engine, tmp_path, {})
    a = proven(engine, root, "feature a")
    with pytest.raises(Interrupted):
        release(engine, root, [a], on_point=interrupt_at("after_stamp"))
    ignore_changelog(root, "# notes\n")
    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, root)
    message = str(exc.value)
    assert "changelog.md" in message
    assert "restore the file" not in message.lower()
    assert (root / "changelog.md").read_text() == "# notes\n"
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"


def _pretend_pre_fix_resolver(mp: pytest.MonkeyPatch) -> None:
    """Make the release assume `changelog.md` the way dct did before #876.
    Before the fix the code itself does that (no resolver exists yet); after
    it, every module's reference to the resolver answers the old way."""
    try:
        from dct.core import changelog_file
    except ImportError:
        return
    real = changelog_file.resolve_changelog

    def old(project, **kw):
        out = real(project, **kw)
        if not out.get("managed"):
            return out
        root = kw["project_root"]
        rel = os.path.relpath(os.path.join(root, "changelog.md"), kw.get("repo_root") or root)
        return {**out, "path": rel.replace(os.sep, "/"), "source": "default",
                "format": "markdown", "candidates": [], "problem": None, "found": [],
                "ways": []}

    for module in list(sys.modules.values()):
        if (getattr(module, "__name__", "") or "").startswith("dct") and \
                getattr(module, "resolve_changelog", None) is real:
            mp.setattr(module, "resolve_changelog", old)


def test_record_stamped_before_the_fix_is_refused_on_resume_without_touching_the_file(
        engine, tmp_path):
    """D3: a record whose approved proposal binds `changelog.md` (absent)
    while its candidate tracks CHANGELOG.md. Resume must refuse with the
    changelog_file resolution and the approved path — never write the
    bootstrap export over CHANGELOG.md's history (macOS) or beside it
    (Linux), never say 'restore'."""
    root, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = proven(engine, root, "feature a")
    # Build the old record on any file system: CHANGELOG.md is hidden from
    # the work tree (skip-worktree keeps `status` clean) while dct prepares
    # and stamps the old way.
    git(root, "update-index", "--skip-worktree", "CHANGELOG.md")
    parked = tmp_path / "parked-CHANGELOG.md"
    (root / "CHANGELOG.md").rename(parked)
    with pytest.MonkeyPatch.context() as mp:
        _pretend_pre_fix_resolver(mp)
        with pytest.raises(Interrupted):
            release(engine, root, [a], on_point=interrupt_at("after_stamp"))
    parked.rename(root / "CHANGELOG.md")
    git(root, "update-index", "--no-skip-worktree", "CHANGELOG.md")
    record = get_release(engine, SLUG, "1.1.0")
    approved = record["proposal"]["changelog"]
    assert (record["status"], approved["path"], approved["base_blob"]) == (
        "stamped", "changelog.md", None), "precondition: the pre-fix record"

    with pytest.raises(ReleaseRefused) as exc:
        resume(engine, root)
    assert "changelog_file" in exc.value.details
    assert exc.value.details.get("approved") == "changelog.md"
    assert "restore the file" not in str(exc.value).lower()
    assert (root / "CHANGELOG.md").read_text() == HISTORY
    assert changelog_names(root) == ["CHANGELOG.md"]
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"


def test_both_case_variants_tracked_are_refused_until_the_owner_chooses(engine, tmp_path):
    """K-13 (flips D1) on a case-sensitive file system (both files can
    exist): refused before the stamp, nothing written; the owner's explicit
    choice — exactly `changelog.md` (K-14) — then decides."""
    if not fs_case_sensitive(tmp_path):
        pytest.skip("both case variants cannot coexist on a case-insensitive file system")
    upper = HISTORY.replace("- first", "- upper")
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY, "CHANGELOG.md": upper})
    a = proven(engine, root, "feature a")
    before = root_files(root)
    with pytest.raises(ReleaseRefused) as exc:
        release(engine, root, [a])
    assert_nothing_released(engine, [a], root, before)
    assert prepare_refusal(engine, root, [a])["problem"] == "ambiguous", exc.value
    set_changelog_path(engine, SLUG, "changelog.md")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "changelog.md", out)
    assert (root / "CHANGELOG.md").read_text() == upper
    assert changelog_names(root) == ["CHANGELOG.md", "changelog.md"]


def prepare_refusal(engine, root: Path, ids: list[int]) -> dict:
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, root, ids)
    return exc.value.details["changelog_file"]


def test_tracked_default_spelling_release_is_unchanged(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {"changelog.md": HISTORY})
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "changelog.md", out)
    assert (root / "changelog.md").read_text() == expected_text(engine)
    assert changelog_names(root) == ["changelog.md"]


def test_no_changelog_at_all_bootstraps_the_default_file(engine, tmp_path):
    root, _ = make_repo(engine, tmp_path, {})
    a = proven(engine, root, "feature a")
    out = release(engine, root, [a])
    assert_completed_into(engine, root, "changelog.md", out)
    text = (root / "changelog.md").read_text()
    assert text.startswith("# Changelog\n") and text.count("## [1.1.0]") == 1
    assert changelog_names(root) == ["changelog.md"]


# ── monorepo ─────────────────────────────────────────────────────────────────


def test_monorepo_subproject_never_writes_the_root_changelog(engine, tmp_path):
    app, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY}, subdir="packages/app")
    repo = app.parent.parent
    a = proven(engine, app, "feature a")
    out = release(engine, app, [a])
    assert_completed_into(engine, app, "packages/app/changelog.md", out)
    assert (repo / "CHANGELOG.md").read_text() == HISTORY
    assert changelog_names(repo) == ["CHANGELOG.md"]
    assert changelog_names(app) == ["changelog.md"]


def test_monorepo_subproject_own_changelog_is_detected(engine, tmp_path):
    app, _ = make_repo(engine, tmp_path, {"CHANGELOG.md": HISTORY,
                                          "packages/app/CHANGES.md": HISTORY},
                       subdir="packages/app")
    repo = app.parent.parent
    a = proven(engine, app, "feature a")
    out = release(engine, app, [a])
    assert_completed_into(engine, app, "packages/app/CHANGES.md", out)
    assert (app / "CHANGES.md").read_text() == expected_text(engine)
    assert (repo / "CHANGELOG.md").read_text() == HISTORY
    assert changelog_names(app) == ["CHANGES.md"]


# ── projects outside git ─────────────────────────────────────────────────────


def plain_project(engine, tmp_path, files: dict[str, str]) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    for name, text in files.items():
        (plain / name).write_text(text)
    create_project(engine, slug=SLUG, name="P", path=str(plain), current_version="1.0.0")
    return Path(os.path.realpath(plain))


def test_outside_git_uppercase_changelog_is_written_in_place(engine, tmp_path):
    plain = plain_project(engine, tmp_path, {"CHANGELOG.md": HISTORY})
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    out = release(engine, plain, [a])
    assert_completed_into(engine, plain, "CHANGELOG.md", out)
    assert changelog_names(plain) == ["CHANGELOG.md"], "renamed or doubled"
    assert (plain / "CHANGELOG.md").read_text() == expected_text(engine)


def test_outside_git_non_markdown_changelog_is_refused(engine, tmp_path):
    plain = plain_project(engine, tmp_path, {"HISTORY.rst": RST})
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    before = root_files(plain)
    with pytest.raises(ReleaseRefused) as exc:
        prepare(engine, plain, [a])
    assert exc.value.details["changelog_file"]["problem"] == "not_markdown"
    assert_nothing_released(engine, [a], plain, before)


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli(engine, monkeypatch):
    from dct import cli as cli_module

    monkeypatch.setattr(cli_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli_module, "_get_readonly_engine", lambda: engine)
    return engine


def test_cli_changelog_path_query_shows_the_effective_file(cli, tmp_path, monkeypatch, capsys):
    make_repo(cli, tmp_path, {"CHANGELOG.md": HISTORY})
    capsys.readouterr()
    run_cli(monkeypatch, ["project", "changelog-path", SLUG])
    out = capsys.readouterr().out
    assert "CHANGELOG.md" in out, "the query must show the file a release would write"
    assert "detected" in out
    assert get_project(cli, SLUG)["changelog_path"] == "changelog.md"


def test_cli_changelog_path_query_reports_a_problem(cli, tmp_path, monkeypatch, capsys):
    make_repo(cli, tmp_path, {"HISTORY.rst": RST})
    capsys.readouterr()
    run_cli(monkeypatch, ["project", "changelog-path", SLUG])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "HISTORY.rst" in text
    assert f"dct project changelog {SLUG} off" in text


def test_cli_project_add_reports_the_detection_and_stores_nothing(
        cli, tmp_path, monkeypatch, capsys):
    repo = Path(make_git_repo(tmp_path / "fresh"))
    (repo / "CHANGELOG.md").write_text(HISTORY)
    (repo / "VERSION").write_text("1.0.0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "setup")
    run_cli(monkeypatch, ["project", "add", "proj-fresh", str(repo)])
    captured = capsys.readouterr()
    assert "CHANGELOG.md" in captured.out + captured.err
    assert get_project(cli, "proj-fresh")["changelog_path"] == "changelog.md"
