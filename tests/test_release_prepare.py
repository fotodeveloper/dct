"""The release ARTIFACTS contract (K-75, K-76, K-77, K-78, K-94 B3, K-96
D2-D5/D9, K-97 F3) — driven through the production effects layer on real
throwaway git repositories / directories and real bash scripts.

- `prepare` runs the version-sync script ONCE, isolated (a temporary
  detached worktree of the pinned candidate, or a temporary copy of a
  project confirmed outside git), leaves the user's checkout byte-identical
  and persists the PENDING proposal (exact paths, base / expected blobs,
  script identity + sha256, candidate, version, plan digest);
- `apply` consumes ONLY that approved proposal (its digest), records the
  learned contract and the untracked baseline atomically with the stamp,
  and never re-runs the script: it writes the approved blobs, content-
  verified per path (base → write, expected → skip, anything else →
  refuse with nothing written and the index untouched);
- the full artifact set is read ONCE after writing and verified before
  `files_written` (K-77); a project outside git keeps its changelog
  baseline (K-78); a resume verifies the full entry set first (B3).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import approved_plan_digest, git, make_git_repo, run_cli

from dct.core.changelog import add_entry, list_entries, update_entry
from dct.core.projects import create_project, get_project
from dct.core.releases import (
    get_pending_proposal,
    get_release,
    get_version_files,
    proposal_digest,
    set_version_files,
)
from dct.db import changelog_entries
from dct.workflows.release import effects
from dct.workflows.release.effects import (
    ReleaseRefused,
    SyncFailed,
    apply_release,
    prepare_release,
    resume_release,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SLUG = "proj-q"
HISTORY = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n\n- first\n"
PYPROJECT = '[project]\nname = "x"\nversion = "{v}"\n'
SYNC = ('#!/bin/bash\nset -e\necho "$1" >> "{log}"\n'
        'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n')


class Interrupted(Exception):
    """Stands for a crash / kill at an interruption point."""


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The effects layer creates commits."""


def write_script(root: Path, body: str) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    path = scripts / "dct-version-sync.sh"
    path.write_text(body)
    return path


@pytest.fixture
def proj(engine, tmp_path):
    """proj-q: changelog history, pyproject 1.0.0, a committed sync script
    that logs every run to sync.log OUTSIDE the repo; tagged v1.0.0; NO
    version-file contract yet (a first release)."""
    repo = Path(make_git_repo(tmp_path / "proj-q"))
    log = tmp_path / "sync.log"
    (repo / "changelog.md").write_text(HISTORY)
    (repo / "pyproject.toml").write_text(PYPROJECT.format(v="1.0.0"))
    write_script(repo, SYNC.format(log=log))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "chore: setup")
    git(repo, "tag", "v1.0.0")
    create_project(engine, slug=SLUG, name="Q", path=str(repo), current_version="1.0.0")
    return repo, log


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


def replace_script(repo: Path, body: str) -> None:
    write_script(repo, body)
    git(repo, "commit", "-qam", "chore: sync script")


def checkout_state(repo: Path) -> dict:
    """Everything prepare must leave byte-identical: HEAD, refs, the index,
    the status and the bytes of every file in the work tree (untracked
    included)."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = Path(dirpath) / name
            files[str(full.relative_to(repo))] = full.read_bytes()
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "index": git(repo, "ls-files", "-s"),
        "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
        "files": files,
    }


def worktrees(repo: Path) -> list[str]:
    return [line for line in git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]


def prepare(engine, repo, **kw):
    kw.setdefault("plan_digest", approved_plan_digest(engine, SLUG, repo, **kw))
    return prepare_release(engine, SLUG, cwd=str(repo), **kw)


def apply(engine, repo, proposal: str | None, **kw):
    kw.setdefault("confirm", True)
    kw.setdefault("no_push", True)  # no remote: an explicit local release (K-97 F2)
    kw.setdefault("plan_digest", approved_plan_digest(engine, SLUG, repo, **kw))
    return apply_release(engine, SLUG, cwd=str(repo), proposal_digest=proposal, **kw)


def prepared(engine, repo, **kw) -> str:
    """prepare → the proposal digest the owner approves."""
    return prepare(engine, repo, **kw)["details"]["proposal_sha256"]


def interrupt_at(point: str, after: int = 1):
    seen = {"n": 0}

    def hook(name: str) -> None:
        if name == point:
            seen["n"] += 1
            if seen["n"] == after:
                raise Interrupted(point)
    return hook


def runs(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def nothing_stamped(engine, eid: int) -> None:
    assert get_release(engine, SLUG, "1.1.0") is None
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == eid] == [None]


# ── K-76: prepare — isolated, pinned, persisted ──────────────────────────────


def test_prepare_runs_the_sync_isolated_and_leaves_the_checkout_byte_identical(engine, proj):
    repo, log = proj
    (repo / "notes.local").write_text("local scratch\n")  # untracked, stays
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    before = checkout_state(repo)
    out = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    proposal = out["details"]["proposal"]
    after = checkout_state(repo)
    # The ONE thing prepare adds to the repository: the ref that pins the
    # proposal's blobs (K-117) — HEAD, branches, tags, index, status and
    # every file are byte-identical.
    pin = f"refs/dct/release-proposals/s-{SLUG}/{proposal['id']}"
    added = [line for line in after["refs"].splitlines()
             if line not in before["refs"].splitlines()]
    assert [line.split(" ")[0] for line in added] == [pin]
    assert {**after, "refs": before["refs"]} == before
    assert worktrees(repo) == [f"worktree {repo}"]  # the temporary worktree is gone
    assert out["outcome"] == "prepared" and out["action"] == "prepare"
    assert proposal["paths"] == ["pyproject.toml"]
    assert proposal["base_blobs"] == {"pyproject.toml": git(repo, "rev-parse",
                                                            f"{candidate}:pyproject.toml")}
    expected = proposal["expected_blobs"]["pyproject.toml"]
    assert git(repo, "cat-file", "blob", expected) == PYPROJECT.format(v="1.1.0").rstrip("\n")
    script = (repo / "scripts" / "dct-version-sync.sh").read_bytes()
    assert proposal["script"] == {"identity": "scripts/dct-version-sync.sh",
                                  "sha256": hashlib.sha256(script).hexdigest()}
    assert proposal["candidate_sha"] == candidate and proposal["version"] == "1.1.0"
    assert proposal["release_mode"] == "git"
    assert proposal["plan_digest"] == approved_plan_digest(engine, SLUG, repo, version="1.1.0",
                                                           entry_ids=[a])
    assert proposal["changelog"]["path"] == "changelog.md"
    assert out["details"]["proposal_sha256"] == proposal_digest(proposal)
    assert out["details"]["contract"] == {"confirmed": None, "proposed": ["pyproject.toml"],
                                          "change": True}
    pending = get_pending_proposal(engine, SLUG)
    assert pending == {"proposal": proposal, "proposal_sha256": proposal_digest(proposal)}
    assert runs(log) == ["1.1.0"]
    assert get_release(engine, SLUG) is None  # nothing stamped
    json.dumps(out)


def test_prepare_with_a_failing_script_changes_nothing(engine, proj):
    repo, _ = proj
    replace_script(repo, '#!/bin/bash\necho "1.1.0" > pyproject.toml\necho boom >&2\nexit 3\n')
    a = proven(engine, repo, "feature a")
    before = checkout_state(repo)
    with pytest.raises(SyncFailed) as exc:
        prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.exit_code == 3
    assert "boom" in exc.value.details["sync"]["output"]
    assert checkout_state(repo) == before and worktrees(repo) == [f"worktree {repo}"]
    assert get_pending_proposal(engine, SLUG) is None


def test_a_hanging_script_and_its_child_are_killed_within_the_timeout(
        engine, proj, tmp_path, monkeypatch):
    repo, _ = proj
    pidfile = tmp_path / "child.pid"
    replace_script(repo, f'#!/bin/bash\nsleep 60 &\necho $! > "{pidfile}"\nsleep 60\n')
    monkeypatch.setattr(effects, "SYNC_TIMEOUT_SECONDS", 1)
    a = proven(engine, repo, "feature a")
    before = checkout_state(repo)
    started = time.monotonic()
    with pytest.raises(SyncFailed, match="timed out") as exc:
        prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert time.monotonic() - started < 15
    assert exc.value.details["sync"]["timed_out"] is True
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the script's child {child} survived the timeout")
    assert checkout_state(repo) == before and worktrees(repo) == [f"worktree {repo}"]
    assert get_pending_proposal(engine, SLUG) is None


def test_the_captured_output_is_capped_and_the_drop_is_reported(engine, proj, monkeypatch):
    repo, _ = proj
    replace_script(repo, "#!/bin/bash\nprintf 'x%.0s' $(seq 1 1000)\n"
                         'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" '
                         "> pyproject.toml\n")
    monkeypatch.setattr(effects, "SYNC_OUTPUT_MAX_BYTES", 100)
    a = proven(engine, repo, "feature a")
    sync = prepare(engine, repo, version="1.1.0", entry_ids=[a])["details"]["sync"]
    assert sync["output"] == "x" * 100
    assert (sync["output_bytes"], sync["output_dropped_bytes"]) == (1000, 900)


def test_a_script_writing_into_the_users_checkout_is_refused(engine, proj):
    repo, _ = proj
    replace_script(repo, f'#!/bin/bash\necho "$1" > pyproject.toml\n'
                         f'echo hacked >> "{repo}/pyproject.toml"\n')
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="outside its isolated") as exc:
        prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert "pyproject.toml" in json.dumps(exc.value.details["checkout_changed"])
    assert get_pending_proposal(engine, SLUG) is None
    assert worktrees(repo) == [f"worktree {repo}"]


def test_a_script_that_modifies_the_changelog_is_refused_before_any_manifest(engine, proj):
    """K-77 / D2: the contract forbids it AND the real result is checked —
    nothing is persisted, no record, no manifest."""
    repo, _ = proj
    replace_script(repo, SYNC.format(log="/dev/null") + 'echo "- sneaked in" >> changelog.md\n')
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="changelog") as exc:
        prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert exc.value.details["changelog"] == "changelog.md"
    assert get_pending_proposal(engine, SLUG) is None
    nothing_stamped(engine, a)
    with pytest.raises(ReleaseRefused, match="proposal"):
        apply(engine, repo, None, version="1.1.0", entry_ids=[a])
    nothing_stamped(engine, a)


# ── K-96 D9: the version FIELD, not a substring ──────────────────────────────


@pytest.mark.parametrize("name,content,ok", [
    ("pyproject.toml", '[project]\nname = "x"\nversion = "1.1.0"\n', True),
    ("pyproject.toml", ('[project]\nname = "x"\nversion = "9.9.9"\n'
                        'description = "since 1.1.0"\n'), False),
    ("package.json", '{"name": "x", "version": "1.1.0"}\n', True),
    ("package.json", '{"name": "x", "version": "1.0.0", "note": "1.1.0"}\n', False),
    ("VERSION", "1.1.0\n", True),
    ("VERSION", "1.1.0-dev\n", False),
    ("Cargo.toml", '[package]\nname = "x"\nversion = "1.1.0"\n', True),
    ("Cargo.toml", '[package]\nname = "x"\nversion = "1.2.0"\n# 1.1.0\n', False),
])
def test_version_files_are_checked_on_their_version_field(engine, proj, name, content, ok):
    repo, _ = proj
    payload = json.dumps(content)
    replace_script(repo, f"#!/bin/bash\nprintf '%b' {payload} > {name}\n")
    a = proven(engine, repo, "feature a")
    if ok:
        out = prepare(engine, repo, version="1.1.0", entry_ids=[a])
        assert out["details"]["proposal"]["paths"] == [name]
        [check] = out["details"]["checks"]
        assert check["path"] == name and check["check"] == "version_field"
    else:
        with pytest.raises(ReleaseRefused, match="version field") as exc:
            prepare(engine, repo, version="1.1.0", entry_ids=[a])
        assert exc.value.details["path"] == name
        assert get_pending_proposal(engine, SLUG) is None


@pytest.mark.parametrize("after,ok", [
    ("release=1.1.0\ndebug=false\n", True),
    ("release=1.1.0\ndebug=true\n", False),     # a changed line without V
    ("release=1.1.01\ndebug=false\n", False),   # V only as a prefix of another number
])
def test_a_file_without_a_reader_may_change_only_lines_holding_v(engine, proj, after, ok):
    repo, _ = proj
    (repo / "app.cfg").write_text("release=1.0.0\ndebug=false\n")
    git(repo, "add", "app.cfg")
    replace_script(repo, f"#!/bin/bash\nprintf '%b' {json.dumps(after)} > app.cfg\n")
    a = proven(engine, repo, "feature a")
    if ok:
        [check] = prepare(engine, repo, version="1.1.0", entry_ids=[a])["details"]["checks"]
        assert check == {"path": "app.cfg", "check": "lines_with_version", "changed_lines": 1}
    else:
        with pytest.raises(ReleaseRefused, match="app.cfg"):
            prepare(engine, repo, version="1.1.0", entry_ids=[a])


# ── K-76: apply consumes only the approved proposal ──────────────────────────


def test_apply_without_or_with_a_wrong_proposal_digest_stamps_nothing(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="proposal"):
        apply(engine, repo, None, version="1.1.0", entry_ids=[a])
    nothing_stamped(engine, a)
    prepared(engine, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(ReleaseRefused, match="proposal"):
        apply(engine, repo, "f" * 64, version="1.1.0", entry_ids=[a])
    nothing_stamped(engine, a)
    assert get_version_files(engine, SLUG)["version_files"] is None


def test_a_new_candidate_voids_the_approval(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    commit(repo, "chore: later\n")  # the candidate moves; the plan is approved again
    with pytest.raises(ReleaseRefused, match="candidate"):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a])
    nothing_stamped(engine, a)


def test_a_new_prepare_voids_the_earlier_digest(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    first = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    second = prepared(engine, repo, version="1.1.0", no_entries=True)
    assert first != second
    with pytest.raises(ReleaseRefused, match="proposal"):
        apply(engine, repo, first, version="1.1.0", entry_ids=[a])
    nothing_stamped(engine, a)


def test_the_stamp_records_proposal_consent_contract_and_baseline_atomically(engine, proj):
    repo, log = proj
    (repo / "notes.local").write_text("x\n")
    a = proven(engine, repo, "feature a")
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    pending = get_pending_proposal(engine, SLUG)["proposal"]
    with pytest.raises(Interrupted):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_stamp"))
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "stamped"
    assert record["proposal"] == pending
    assert record["consent"]["proposal_sha256"] == approval
    assert record["consent"]["candidate_sha"] == record["candidate_sha"]
    assert record["consent"]["entry_ids"] == [a] and record["consent"]["push_target"] is None
    assert record["manifest"] == {"untracked_baseline": ["notes.local"]}
    assert get_version_files(engine, SLUG)["version_files"] == ["pyproject.toml"]
    assert get_pending_proposal(engine, SLUG) is None  # consumed by the stamp
    out = resume_release(engine, SLUG, cwd=str(repo))
    assert out["outcome"] == "completed"
    assert runs(log) == ["1.1.0"]  # the script never ran after the stamp
    assert "notes.local" not in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_a_refused_stamp_keeps_the_previous_contract_and_the_pending_proposal(engine, proj):
    repo, _ = proj
    set_version_files(engine, SLUG, ["VERSION"], confirm=True)
    a = proven(engine, repo, "feature a")
    out = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["details"]["contract"] == {"confirmed": ["VERSION"],
                                          "proposed": ["pyproject.toml"], "change": True}

    def edit_entry(name):
        if name == "before_stamp":  # an entry changes after every pre-check
            update_entry(engine, SLUG, a, entry="feature a, edited")

    with pytest.raises(ReleaseRefused):
        apply(engine, repo, out["details"]["proposal_sha256"], version="1.1.0",
              entry_ids=[a], on_point=edit_entry)
    nothing_stamped(engine, a)
    assert get_version_files(engine, SLUG)["version_files"] == ["VERSION"]
    assert get_pending_proposal(engine, SLUG)["proposal_sha256"] == \
        out["details"]["proposal_sha256"]


def test_an_empty_contract_is_approved_the_same_way(engine, proj):
    """D4: no script → the proposal names no file; the FIRST empty contract
    is still approved by its digest and learned with the stamp."""
    repo, _ = proj
    git(repo, "rm", "-q", "scripts/dct-version-sync.sh")
    git(repo, "commit", "-q", "-m", "chore: no sync")
    a = proven(engine, repo, "feature a")
    out = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert out["details"]["proposal"]["paths"] == [] and out["details"]["sync"] is None
    with pytest.raises(ReleaseRefused, match="proposal"):
        apply(engine, repo, None, version="1.1.0", entry_ids=[a])
    apply(engine, repo, out["details"]["proposal_sha256"], version="1.1.0", entry_ids=[a])
    assert get_version_files(engine, SLUG)["version_files"] == []
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"


# ── K-75: content-verified retry, never a re-run of the script ───────────────


@pytest.fixture
def two_files(engine, proj):
    """A sync script that writes TWO version files (pyproject.toml, VERSION)."""
    repo, log = proj
    (repo / "VERSION").write_text("1.0.0\n")
    git(repo, "add", "VERSION")
    replace_script(repo, SYNC.format(log=log) + 'echo "$1" > VERSION\n')
    return repo, log


def full_state(repo: Path) -> dict:
    state = checkout_state(repo)
    state["staged"] = git(repo, "diff", "--cached", "--name-only")
    return state


def test_interrupted_between_two_version_files_resumes_without_the_script(engine, two_files):
    repo, log = two_files
    a = proven(engine, repo, "feature a")
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(Interrupted):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_version_file"))
    written = [p for p in ("VERSION", "pyproject.toml")
               if "1.1.0" in (repo / p).read_text()]
    assert len(written) == 1  # one file written, the other still at its base
    out = resume_release(engine, SLUG, cwd=str(repo))
    assert out["outcome"] == "completed"
    assert (repo / "VERSION").read_text() == "1.1.0\n"
    assert 'version = "1.1.0"' in (repo / "pyproject.toml").read_text()
    assert runs(log) == ["1.1.0"]
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("point", ["after_changelog", "after_version_file"])
def test_a_user_edit_between_runs_is_refused_with_files_and_index_unchanged(
        engine, two_files, point):
    repo, log = two_files
    a = proven(engine, repo, "feature a")
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(Interrupted):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at(point))
    target = "VERSION" if "1.1.0" not in (repo / "VERSION").read_text() else "pyproject.toml"
    with open(repo / target, "a", encoding="utf-8") as fh:  # the owner's own edit
        fh.write("# my local note\n")
    before = full_state(repo)
    with pytest.raises(ReleaseRefused, match="not overwritten") as exc:
        resume_release(engine, SLUG, cwd=str(repo))
    assert target in exc.value.details["foreign"]
    assert full_state(repo) == before
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    assert runs(log) == ["1.1.0"]


def test_a_user_edit_of_the_written_changelog_is_refused(engine, two_files):
    repo, _ = two_files
    a = proven(engine, repo, "feature a")
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(Interrupted):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_changelog"))
    text = (repo / "changelog.md").read_text()
    (repo / "changelog.md").write_text(text.replace("- feature a", "- feature A (edited)"))
    before = full_state(repo)
    with pytest.raises(ReleaseRefused, match="not overwritten") as exc:
        resume_release(engine, SLUG, cwd=str(repo))
    assert exc.value.details["foreign"] == ["changelog.md"]
    assert full_state(repo) == before


# ── K-77: one read, one consistent manifest ──────────────────────────────────


def test_a_change_after_writing_is_never_recorded_as_files_written(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])

    def tamper(name):
        if name == "after_version_files":  # a concurrent writer, after the writes
            with open(repo / "changelog.md", "a", encoding="utf-8") as fh:
                fh.write("- foreign line\n")

    with pytest.raises(ReleaseRefused, match="changelog.md"):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a], on_point=tamper)
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "stamped"
    assert set(record["manifest"]) == {"untracked_baseline"}  # no contradictory manifest


# ── K-94 B3: the full entry set before any effect ────────────────────────────


def test_resume_verifies_the_full_entry_set_before_any_effect(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    other = add_entry(engine, SLUG, "added", "other")["id"]
    approval = prepared(engine, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(Interrupted):
        apply(engine, repo, approval, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_stamp"))
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == other)
                     .values(version="1.1.0"))
    before = checkout_state(repo)
    with pytest.raises(ReleaseRefused, match="entry set"):
        resume_release(engine, SLUG, cwd=str(repo))
    assert checkout_state(repo) == before
    assert get_release(engine, SLUG, "1.1.0")["entry_ids"] == [a]  # never widened


# ── CLI: plan → prepare → apply ──────────────────────────────────────────────


def test_cli_prepare_then_apply(engine, proj, monkeypatch, capsys):
    from dct import cli

    repo, _ = proj
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli, "_get_readonly_engine", lambda: engine)
    monkeypatch.chdir(repo)
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    run_cli(monkeypatch, ["release", "prepare", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--plan-digest", digest, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "prepared" and doc["details"]["proposal"]["paths"] == [
        "pyproject.toml"]
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["release", "apply", "--no-push", "-P", SLUG, "--version", "1.1.0",
                              "--entries", str(a), "--confirm", "--plan-digest", digest,
                              "--confirm-version-files"])
    assert exc.value.code == 1
    assert "dct release prepare" in capsys.readouterr().err
    nothing_stamped(engine, a)
    run_cli(monkeypatch, ["release", "apply", "--no-push", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--confirm", "--plan-digest", digest,
                          "--proposal-digest", doc["details"]["proposal_sha256"], "--json"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "completed"


def test_cli_alias_needs_the_approved_proposal_for_every_new_release(engine, proj, monkeypatch,
                                                                    capsys):
    """K-117: the `clog release` alias never prepares or approves a proposal
    itself — a first release AND a later one with an unchanged contract both
    need the digest of an approved `dct release prepare`."""
    from conftest import alias_argv

    from dct import cli

    repo, log = proj
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli, "_get_readonly_engine", lambda: engine)
    monkeypatch.chdir(repo)
    a = proven(engine, repo, "feature a")
    argv = ["clog", "release", "--confirm", "--no-push", "-P", SLUG, "1.1.0"]
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, [*argv, "--json"])
    assert exc.value.code == 1
    doc = json.loads(capsys.readouterr().out)
    assert "--proposal-digest" in doc["details"]["error"]
    nothing_stamped(engine, a)
    assert runs(log) == [] and get_pending_proposal(engine, SLUG) is None
    run_cli(monkeypatch, alias_argv(engine, SLUG, argv))
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"
    b = proven(engine, repo, "feature b")
    again = ["clog", "release", "--confirm", "--no-push", "-P", SLUG, "1.2.0"]
    with pytest.raises(SystemExit):  # the contract is unchanged — still no auto-approval
        run_cli(monkeypatch, again)
    assert get_release(engine, SLUG, "1.2.0") is None
    run_cli(monkeypatch, alias_argv(engine, SLUG, again))
    assert get_release(engine, SLUG, "1.2.0")["entry_ids"] == [b]


# ── K-78 / D5: a project CONFIRMED outside git ───────────────────────────────


@pytest.fixture
def plain(engine, tmp_path):
    root = tmp_path / "plain-q"
    root.mkdir()
    log = tmp_path / "plain-sync.log"
    (root / "changelog.md").write_text(HISTORY)
    (root / "VERSION").write_text("1.0.0\n")
    write_script(root, f'#!/bin/bash\necho "$1" >> "{log}"\necho "$1" > VERSION\n')
    create_project(engine, slug=SLUG, name="Q", path=str(root), current_version="1.0.0")
    return root, log


def tree_bytes(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def test_without_git_prepare_runs_on_a_copy_and_apply_writes_the_approved_files(
        engine, plain, tmp_path):
    root, log = plain
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    before = tree_bytes(root)
    out = prepare(engine, tmp_path, version="1.1.0", entry_ids=[a])
    assert tree_bytes(root) == before and runs(log) == ["1.1.0"]
    proposal = out["details"]["proposal"]
    assert proposal["release_mode"] == "without_git" and proposal["candidate_sha"] is None
    assert proposal["paths"] == ["VERSION"]
    assert proposal["base_blobs"] == {"VERSION": hashlib.sha256(b"1.0.0\n").hexdigest()}
    assert proposal["expected_blobs"] == {"VERSION": hashlib.sha256(b"1.1.0\n").hexdigest()}
    assert proposal["changelog"]["base_sha256"] == hashlib.sha256(HISTORY.encode()).hexdigest()
    apply(engine, tmp_path, out["details"]["proposal_sha256"], version="1.1.0", entry_ids=[a])
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed" and record["release_mode"] == "without_git"
    assert (root / "VERSION").read_text() == "1.1.0\n"
    assert (root / "changelog.md").read_text().count("## [1.1.0]") == 1
    assert runs(log) == ["1.1.0"]  # never re-run after the stamp
    assert get_version_files(engine, SLUG)["version_files"] == ["VERSION"]


def test_without_git_a_changed_script_or_baseline_voids_the_approval(engine, plain, tmp_path):
    root, _ = plain
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    approval = prepared(engine, tmp_path, version="1.1.0", entry_ids=[a])
    script = root / "scripts" / "dct-version-sync.sh"
    original = script.read_text()
    script.write_text(original + "# changed\n")
    with pytest.raises(ReleaseRefused, match="script"):
        apply(engine, tmp_path, approval, version="1.1.0", entry_ids=[a])
    script.write_text(original)
    (root / "VERSION").write_text("1.0.5\n")
    with pytest.raises(ReleaseRefused, match="VERSION"):
        apply(engine, tmp_path, approval, version="1.1.0", entry_ids=[a])
    nothing_stamped(engine, a)


def test_without_git_an_absent_changelog_is_the_recorded_baseline(engine, plain, tmp_path):
    root, _ = plain
    (root / "changelog.md").unlink()
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    out = prepare(engine, tmp_path, version="1.1.0", entry_ids=[a])
    assert out["details"]["proposal"]["changelog"]["base_sha256"] is None
    with pytest.raises(Interrupted):
        apply(engine, tmp_path, out["details"]["proposal_sha256"], version="1.1.0",
              entry_ids=[a], on_point=interrupt_at("after_changelog"))
    written = (root / "changelog.md").read_text()
    (root / "changelog.md").write_text("# Mine\n")  # neither absent nor the expected file
    with pytest.raises(ReleaseRefused, match="not overwritten"):
        resume_release(engine, SLUG, cwd=str(tmp_path))
    assert (root / "changelog.md").read_text() == "# Mine\n"
    (root / "changelog.md").unlink()  # back to the recorded baseline: absent
    assert resume_release(engine, SLUG, cwd=str(tmp_path))["outcome"] == "completed"
    assert (root / "changelog.md").read_text() == written


@pytest.mark.parametrize("damage", ["delete_older_section", "modify_older_section"])
def test_without_git_damaged_history_with_the_new_header_kept_is_refused(
        engine, plain, tmp_path, damage):
    """K-78: the current file is never its own proof — the older history is
    compared with the PERSISTED baseline."""
    root, _ = plain
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    approval = prepared(engine, tmp_path, version="1.1.0", entry_ids=[a])
    with pytest.raises(Interrupted):
        apply(engine, tmp_path, approval, version="1.1.0", entry_ids=[a],
              on_point=interrupt_at("after_changelog"))
    text = (root / "changelog.md").read_text()
    if damage == "delete_older_section":
        damaged = text[:text.index("## [1.0.0]")]
    else:
        damaged = text.replace("- first", "- first (rewritten)")
    assert "## [1.1.0]" in damaged
    (root / "changelog.md").write_text(damaged)
    with pytest.raises(ReleaseRefused, match="not overwritten"):
        resume_release(engine, SLUG, cwd=str(tmp_path))
    assert (root / "changelog.md").read_text() == damaged
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"


def test_cli_prepare_human_output_names_the_files_and_the_next_command(
        engine, proj, monkeypatch, capsys):
    from dct import cli

    repo, _ = proj
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli, "_get_readonly_engine", lambda: engine)
    monkeypatch.chdir(repo)
    a = proven(engine, repo, "feature a")
    run_cli(monkeypatch, ["release", "plan", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a)])
    assert "Next: dct release prepare --version 1.1.0 --entries" in capsys.readouterr().out
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    run_cli(monkeypatch, ["release", "prepare", "-P", SLUG, "--version", "1.1.0",
                          "--entries", str(a), "--plan-digest", digest])
    out = capsys.readouterr().out
    pending = get_pending_proposal(engine, SLUG)["proposal_sha256"]
    assert "version files: pyproject.toml" in out
    assert "pyproject.toml: version_field (1.1.0)" in out
    assert f"--proposal-digest {pending}" in out
