"""`dct release prepare --preview` / `--discard-preview` and the preview slot
(the one-dialog release, phase 2 step 2) — driven through the REAL CLI on
throwaway git repositories, with the stored slot read from the database
column itself.

What the preview must guarantee:

- it binds the release CONTENT (the consent view) and returns its digest
  C = sha256(canonical(view)) — equal content gives the same C, any bound
  change gives another C;
- it computes the exact future tree T1 of the work-in-progress commit in a
  TEMPORARY index: the user's index, work tree and refs stay byte-identical
  (the only addition is the preview pin, a ref to a TREE);
- synthetic commits are never referenced by a ref and are dated now;
- every refusal stores nothing and keeps the earlier slot;
- the "with all N" variant is offered only for 1..4 waiting entries, none
  reverted; its script runs again only when its version differs;
- outside git the proposal comes from the blob store, and the blob-store
  cleanup keeps the preview's blobs;
- `--discard-preview` clears the slot, removes the pin and releases the
  resources.

The core storage (validators, progress, consent `content_sha256`) is at the
end of the module; the migration test is in tests/test_releases_migration.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import approved_plan_digest, git, run_cli
from test_release_effects import (
    HISTORY,
    SLUG,
    commit,
    make_release_repo,
    proven,
    sync_runs,
)

from dct.core import releases as ledger
from dct.core.changelog import add_entry, release_entries, update_entry
from dct.core.projects import (
    create_project,
    set_changelog_pr_refs,
    set_manages_changelog,
    set_release_executor,
)
from dct.core.releases import get_pending_proposal, set_version_files
from dct.db import projects
from dct.workflows.release import build_plan, effects
from dct.workflows.release.effects import ATTEST_SUBJECT, prepare_release

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

# ── the shapes the contract fixes (sections A1, C3, E1) ──────────────────────

DETAILS_KEYS = {"preview_id", "base", "work", "remote", "left_out", "variants",
                "all_unavailable"}
BASE_KEYS = {"head", "ref", "branch", "detached"}
WORK_KEYS = {"paths", "untracked_included", "untracked_left", "message", "subject", "proves",
             "uncertain", "tree"}
REMOTE_KEYS = {"checked", "url", "tags", "branch_ahead", "error"}
VARIANT_KEYS = {"name", "consent_digest", "consent", "version_only", "suggested", "display"}
DISPLAY_KEYS = {"entries", "section", "checks", "contract", "sync", "range", "delta", "warnings"}
SUGGESTION_KEYS = {"bump", "target", "reason", "safe", "taken", "alternatives"}
CONSENT_VIEW_KEYS = {"format", "project", "executor", "release_mode", "checkout_root",
                     "project_root", "version", "base", "work", "attest", "candidate_tree",
                     "range", "entries", "changelog", "version_files", "push"}
VIEW_WORK_KEYS = {"tree", "subject", "message_sha256", "proves", "paths"}
VIEW_RANGE_KEYS = {"base_tag", "base_sha", "approximate", "since", "notice_code"}
VIEW_ENTRIES_KEYS = {"selected", "state", "waiting", "proof"}
VIEW_CHANGELOG_KEYS = {"path", "managed", "base_blob", "composed_sha256", "section_sha256",
                       "date", "pr_refs"}
VIEW_VERSION_FILES_KEYS = {"paths", "base_blobs", "expected_blobs", "script", "contract"}
SLOT_KEYS = {"id", "created_at", "base", "work", "variants", "pin", "progress"}
SLOT_WORK_KEYS = {"index_before", "paths_state", "untracked_included", "message"}
SLOT_VARIANT_KEYS = {"name", "consent", "proposal_content"}
# validate_proposal's shape minus id / candidate_sha / plan_digest (C3).
PROPOSAL_CONTENT_KEYS = set(ledger.PROPOSAL_KEYS) - {"id", "candidate_sha", "plan_digest"}
PREVIEW_PIN_PREFIX = "refs/dct/release-previews/"
ALL_UNAVAILABLE = {"none_waiting", "too_many", "reverted", "no_git", "not_requested"}
PYPROJECT = '[project]\nname = "x"\nversion = "{v}"\n'


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Real commits are made by the fixtures; the preview makes synthetic ones."""


@pytest.fixture
def rel(engine, tmp_path):
    """proj-r (test_release_effects.make_release_repo): changelog history,
    pyproject 1.0.0, a sync script logging each run, tagged v1.0.0, `main`
    tracking a bare remote, contract ["pyproject.toml"]."""
    return make_release_repo(engine, tmp_path)


@pytest.fixture
def plain(engine, tmp_path):
    """proj-r CONFIRMED outside git: changelog history, VERSION 1.0.0 and a
    sync script that writes VERSION and logs each run."""
    root = tmp_path / "plain-r"
    (root / "scripts").mkdir(parents=True)
    log = tmp_path / "plain-sync.log"
    (root / "changelog.md").write_text(HISTORY)
    (root / "VERSION").write_text("1.0.0\n")
    (root / "scripts" / "dct-version-sync.sh").write_text(
        f'#!/bin/bash\necho "$1" >> "{log}"\necho "$1" > VERSION\n')
    create_project(engine, slug=SLUG, name="R", path=str(root), current_version="1.0.0")
    return root, log


@pytest.fixture
def run(engine, monkeypatch, capsys):
    """`dct release prepare -P proj-r <argv> --json` in `cwd` → (exit code,
    JSON document or None, stderr). Exit code 2 = argparse refused the argv
    (the document is then None)."""
    from dct import cli as cli_module

    monkeypatch.setattr(cli_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli_module, "_get_readonly_engine", lambda: engine)

    def invoke(cwd, *argv: str) -> tuple[int, dict | None, str]:
        monkeypatch.chdir(cwd)
        capsys.readouterr()
        code = 0
        try:
            run_cli(monkeypatch, ["release", "prepare", "-P", SLUG, *map(str, argv), "--json"])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        out = capsys.readouterr()
        if code == 2:
            return code, None, out.err
        return code, json.loads(out.out), out.err

    return invoke


# ── helpers ──────────────────────────────────────────────────────────────────


def accepted(code: int, err: str) -> None:
    if code == 2:
        pytest.fail(f"the CLI does not accept the preview flags: {err.strip()}")


def preview(run, cwd, *argv) -> dict:
    """A successful `prepare --preview` → its JSON document."""
    code, doc, err = run(cwd, "--preview", *argv)
    accepted(code, err)
    assert code == 0, f"the preview was refused ({code}): {doc['details'].get('error')}"
    assert doc["action"] == "prepare" and doc["outcome"] == "preview"
    assert doc["project"] == SLUG
    json.dumps(doc)
    return doc


def refused(run, cwd, *argv, key: str | None, code: int = 1) -> dict:
    """A refused `prepare --preview` → its document; `key` must be in details."""
    got, doc, err = run(cwd, "--preview", *argv)
    accepted(got, err)
    assert got == code, f"expected exit {code}, got {got}: {doc}"
    assert doc["outcome"] == ("refused" if code == 1 else "failed")
    if key is not None:
        assert key in doc["details"], (key, sorted(doc["details"]), doc["details"].get("error"))
    return doc


def discard(run, cwd, *argv) -> dict:
    code, doc, err = run(cwd, "--discard-preview", *argv)
    accepted(code, err)
    assert code == 0, doc
    assert doc["action"] == "prepare" and doc["outcome"] == "noop"
    return doc


def variant(doc: dict, name: str = "shown") -> dict:
    [found] = [v for v in doc["details"]["variants"] if v["name"] == name]
    return found


def canonical_digest(view) -> str:
    return hashlib.sha256(json.dumps(view, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def slot(engine) -> dict | None:
    """The stored preview slot, read from projects.release_preview itself."""
    with engine.begin() as conn:
        text = conn.execute(sa.select(projects.c.release_preview)
                            .where(projects.c.slug == SLUG)).scalar()
    return None if text is None else json.loads(text)


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD")


def tree_of(repo, rev: str) -> str:
    return git(repo, "rev-parse", f"{rev}^{{tree}}")


def blob_at(repo, rev: str, path: str) -> str:
    return git(repo, "rev-parse", f"{rev}:{path}")


def hash_bytes(repo, data: bytes, *, write: bool = False) -> str:
    """The blob id of `data` (`write`: also store the blob)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    args = ["hash-object", *(["-w"] if write else []), "--stdin"]
    return subprocess.run(["git", "-C", str(repo), *args], input=data,
                          env=env, capture_output=True, check=True).stdout.decode().strip()


def git_in(repo, data: str, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(repo), *args], input=data, text=True, env=env,
                          capture_output=True, check=True).stdout.strip()


def refs(repo) -> list[str]:
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()


def preview_refs(repo) -> list[str]:
    return [line for line in refs(repo) if line.startswith(PREVIEW_PIN_PREFIX)]


def other_refs(repo) -> list[str]:
    return [line for line in refs(repo) if not line.startswith(PREVIEW_PIN_PREFIX)]


def ref_exists(repo, ref: str) -> bool:
    return any(line.split(" ")[0] == ref for line in refs(repo))


def work_files(repo: Path) -> dict[str, bytes]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = Path(dirpath) / name
            out[str(full.relative_to(repo))] = (os.readlink(full).encode() if full.is_symlink()
                                                 else full.read_bytes())
    return out


def worktrees(repo) -> list[str]:
    return [line for line in git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def commit_all(repo: Path, files: dict[str, str], message: str = "chore: files") -> str:
    for rel_path, text in files.items():
        write(repo, rel_path, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


def message_file(tmp_path: Path, text: str, name: str = "msg.txt") -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def push_url(repo) -> str:
    return git(repo, "remote", "get-url", "--push", "origin")


def unproven_reason(engine, repo, entry_id: int) -> str | None:
    plan = build_plan(engine, SLUG, cwd=str(repo))
    return {e["id"]: e["reason"] for e in plan["entries"]["unproven"]}.get(entry_id)


def revert(repo, sha: str) -> None:
    git(repo, "revert", "--no-edit", sha)


def partial_revert(repo, sha: str, path: str) -> None:
    """A revert message whose change is NOT the inverse of `sha` → the plan
    reports the affected entries as `revert_unconfirmed`."""
    write(Path(repo), path, "something else entirely\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m",
        f'Revert "partial"\n\nThis reverts commit {sha}.\n')


def proving(engine, repo, category: str, text: str) -> tuple[int, str]:
    entry = add_entry(engine, SLUG, category, text)["id"]
    sha = commit(repo, f"{category}: {text}\n\nChangelog: {entry}\n", f"{text}.txt")
    return entry, sha


# ═════════════════════════════════════════════════════════════════════════════
# 5. A clean tree
# ═════════════════════════════════════════════════════════════════════════════


def test_t05_clean_tree_preview_returns_the_exact_shapes(engine, rel, run):
    repo, log = rel
    a = proven(engine, repo, "feature a")
    proof_sha = head(repo)
    h0 = head(repo)
    doc = preview(run, repo)
    details = doc["details"]
    assert doc["version"] == "1.1.0"
    assert [s["step"] for s in doc["steps"]][:2] == ["preview_tree", "sync"]
    assert set(details) == DETAILS_KEYS
    assert details["work"] is None
    assert details["base"] == {"head": h0, "ref": "refs/heads/main", "branch": "main",
                               "detached": False}
    assert set(details["remote"]) == REMOTE_KEYS
    assert details["remote"]["checked"] is True and details["remote"]["error"] is None
    assert details["remote"]["url"] == push_url(repo)
    assert details["remote"]["branch_ahead"] is False and details["remote"]["tags"] == []
    assert details["all_unavailable"] == "none_waiting"
    assert [v["name"] for v in details["variants"]] == ["shown"]

    shown = variant(doc)
    assert set(shown) == VARIANT_KEYS
    assert shown["version_only"] is False
    assert shown["consent_digest"] == canonical_digest(shown["consent"])
    assert shown["suggested"] == {
        "bump": "minor", "target": "1.1.0", "reason": "features", "safe": True, "taken": [],
        "alternatives": [{"bump": "patch", "target": "1.0.1"},
                         {"bump": "major", "target": "2.0.0"}]}

    view = shown["consent"]
    assert set(view) == CONSENT_VIEW_KEYS
    assert view["format"] == 1 and view["project"] == SLUG
    assert view["executor"] == "dct" and view["release_mode"] == "git"
    assert view["checkout_root"] == str(repo) and view["project_root"] == str(repo)
    assert view["version"] == {"current": "1.0.0", "target": "1.1.0"}
    assert view["base"] == {"head": h0, "ref": "refs/heads/main"}
    assert view["work"] is None and view["attest"] is None
    assert view["candidate_tree"] == tree_of(repo, h0)
    assert set(view["range"]) == VIEW_RANGE_KEYS
    assert view["range"]["base_tag"] == "v1.0.0"
    assert view["range"]["base_sha"] == git(repo, "rev-parse", "v1.0.0^{commit}")
    assert view["range"]["approximate"] is False and view["range"]["since"] is None
    assert set(view["entries"]) == VIEW_ENTRIES_KEYS
    assert view["entries"]["selected"] == [a]
    assert set(view["entries"]["state"]) == {str(a)}
    assert view["entries"]["waiting"] == {}
    assert set(view["entries"]["proof"]) == {str(a)}
    assert view["entries"]["proof"][str(a)]["commits"] == [proof_sha]

    changelog = view["changelog"]
    assert set(changelog) == VIEW_CHANGELOG_KEYS
    assert changelog["path"] == "changelog.md" and changelog["managed"] is True
    assert changelog["base_blob"] == blob_at(repo, h0, "changelog.md")
    assert changelog["date"] == today() and changelog["pr_refs"] is False
    assert len(changelog["composed_sha256"]) == 64
    section = shown["display"]["section"]
    assert changelog["section_sha256"] == sha256(section)  # the screen shows what C binds
    assert f"## [1.1.0] - {today()}" in section and "- feature a" in section

    script = (repo / "scripts" / "dct-version-sync.sh").read_bytes()
    assert view["version_files"] == {
        "paths": ["pyproject.toml"],
        "base_blobs": {"pyproject.toml": blob_at(repo, h0, "pyproject.toml")},
        "expected_blobs": {"pyproject.toml": hash_bytes(repo, PYPROJECT.format(v="1.1.0")
                                                        .encode())},
        "script": {"identity": "scripts/dct-version-sync.sh", "sha256": sha256(script)},
        "contract": {"confirmed": ["pyproject.toml"], "change": False}}
    assert view["push"] == {"mode": "push", "remote": "origin", "branch": "main",
                            "url": push_url(repo), "tag": "v1.1.0"}

    display = shown["display"]
    assert set(display) == DISPLAY_KEYS
    assert display["entries"] == {
        "selected": [{"id": a, "category": "added", "entry": "feature a", "component": None}],
        "waiting": []}
    assert display["contract"] == {"confirmed": ["pyproject.toml"],
                                   "proposed": ["pyproject.toml"], "change": False}
    assert display["delta"] is None
    assert [c["path"] for c in display["checks"]] == ["pyproject.toml"]
    assert set(display["range"]) == {"text", "approximate", "notice"}
    assert sync_runs(log) == ["1.1.0"]
    assert get_pending_proposal(engine, SLUG) is None  # a preview is no D/P proposal


def test_t05_clean_tree_slot_is_stored_and_pinned_and_the_checkout_is_untouched(
        engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    h0 = head(repo)
    before_refs, before_files = refs(repo), work_files(repo)
    before_index = (repo / ".git" / "index").read_bytes()
    doc = preview(run, repo)
    assert (repo / ".git" / "index").read_bytes() == before_index
    shown = variant(doc)
    preview_id = doc["details"]["preview_id"]

    stored = slot(engine)
    assert stored is not None, "the preview stored no slot"
    assert set(stored) == SLOT_KEYS
    assert stored["id"] == preview_id
    assert stored["base"] == {"head": h0, "ref": "refs/heads/main",
                              "checkout_root": shown["consent"]["checkout_root"]}
    assert stored["work"] is None
    assert stored["progress"] == {"work_commit": None, "attest_commit": None,
                                  "consent": None}
    assert set(stored["variants"]) == {shown["consent_digest"]}
    stored_variant = stored["variants"][shown["consent_digest"]]
    assert set(stored_variant) == SLOT_VARIANT_KEYS
    assert stored_variant["name"] == "shown"
    assert stored_variant["consent"] == shown["consent"]
    content = stored_variant["proposal_content"]
    assert set(content) == PROPOSAL_CONTENT_KEYS
    assert content["paths"] == ["pyproject.toml"] and content["version"] == "1.1.0"
    assert content["release_mode"] == "git"
    assert content["expected_blobs"] == shown["consent"]["version_files"]["expected_blobs"]
    # The live-proposal set keeps a preview's content (blob-store cleanup, C3).
    assert any(item["proposal"] == content for item in ledger.live_proposals(engine))

    pin = stored["pin"]
    assert pin.startswith(PREVIEW_PIN_PREFIX) and pin.endswith(f"/{preview_id}")
    assert SLUG in pin
    assert git(repo, "cat-file", "-t", pin) == "tree"  # a tree, never a commit
    expected = shown["consent"]["version_files"]["expected_blobs"]["pyproject.toml"]
    # Each variant under its own name (two versions → two blobs per path).
    assert git(repo, "rev-parse", f"{pin}:shown/expected/pyproject.toml") == expected
    # The pin is the ONLY ref added; HEAD, branches, tags, files are untouched.
    added = [line for line in refs(repo) if line not in before_refs]
    assert [line.split(" ")[0] for line in added] == [pin]
    assert other_refs(repo) == [line for line in before_refs
                                if not line.startswith(PREVIEW_PIN_PREFIX)]
    assert head(repo) == h0 and work_files(repo) == before_files
    assert worktrees(repo) == [f"worktree {repo}"]


def test_t05_two_previews_bind_the_same_content_with_different_ids(engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    first = preview(run, repo)
    first_pin = slot(engine)["pin"]
    second = preview(run, repo)
    assert variant(second)["consent_digest"] == variant(first)["consent_digest"]
    assert variant(second)["consent"] == variant(first)["consent"]
    assert second["details"]["preview_id"] != first["details"]["preview_id"]
    stored = slot(engine)
    assert stored["id"] == second["details"]["preview_id"]  # the newer one replaced it
    assert not ref_exists(repo, first_pin), "the replaced preview's pin was not removed"
    assert ref_exists(repo, stored["pin"])
    assert len(preview_refs(repo)) == 1


def test_t05_a_preview_clears_the_pending_proposal_and_unpins_it(engine, rel, run):
    repo, _ = rel
    a = proven(engine, repo, "feature a")
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepared = prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                               plan_digest=digest)
    proposal_pin = effects.pin_ref(SLUG, prepared["details"]["proposal"])
    assert ref_exists(repo, proposal_pin)
    preview(run, repo)
    assert get_pending_proposal(engine, SLUG) is None
    assert not ref_exists(repo, proposal_pin), "the replaced proposal's pin was kept"
    assert slot(engine) is not None


def test_t05_a_prepare_clears_the_preview_slot_and_unpins_it(engine, rel, run):
    """The D/P path and the preview replace each other's slot (C3)."""
    repo, _ = rel
    a = proven(engine, repo, "feature a")
    preview(run, repo)
    preview_pin = slot(engine)["pin"]
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                    plan_digest=digest)
    assert slot(engine) is None
    assert not ref_exists(repo, preview_pin), "the replaced preview's pin was kept"
    assert get_pending_proposal(engine, SLUG) is not None


def _edit_entry(engine, repo, ids):
    update_entry(engine, SLUG, ids["a"], entry="feature a, reworded")
    return (), lambda old, new: old["entries"]["state"] != new["entries"]["state"]


def _add_entry(engine, repo, ids):
    add_entry(engine, SLUG, "fixed", "late fix")
    return (), lambda old, new: set(new["entries"]["state"]) > set(old["entries"]["state"])


def _pr_refs(engine, repo, ids):
    set_changelog_pr_refs(engine, SLUG, True)
    return (), lambda old, new: new["changelog"]["pr_refs"] is True


def _manages_changelog_off(engine, repo, ids):
    set_manages_changelog(engine, SLUG, False)
    # Not null: the section, its date and the render stay bound (no file).
    return (), lambda old, new: new["changelog"]["managed"] is False \
        and new["changelog"]["path"] is None and new["changelog"]["composed_sha256"] is None


def _contract_changed(engine, repo, ids):
    set_version_files(engine, SLUG, ["VERSION"], confirm=True)
    return (), lambda old, new: new["version_files"]["contract"] == {
        "confirmed": ["VERSION"], "change": True}


def _no_push(engine, repo, ids):
    return ("--no-push",), lambda old, new: new["push"] == {"mode": "local",
                                                            "reason": "requested"}


def _other_version(engine, repo, ids):
    return ("--version", "1.2.0"), lambda old, new: new["version"]["target"] == "1.2.0"


@pytest.mark.parametrize("mutate", [_edit_entry, _add_entry, _pr_refs, _manages_changelog_off,
                                    _contract_changed, _no_push, _other_version])
def test_t05_every_bound_change_gives_another_consent_digest(engine, rel, run, mutate):
    """C1: the digest binds the content — edited / added entries, the
    render setting, the managed changelog, the contract, the push target,
    the version."""
    repo, _ = rel
    a = proven(engine, repo, "feature a")
    old = variant(preview(run, repo))
    argv, changed = mutate(engine, repo, {"a": a})
    new = variant(preview(run, repo, *argv))
    assert new["consent_digest"] != old["consent_digest"]
    assert changed(old["consent"], new["consent"]), new["consent"]
    assert new["consent_digest"] == canonical_digest(new["consent"])


# ═════════════════════════════════════════════════════════════════════════════
# 6. A mixed dirty tree
# ═════════════════════════════════════════════════════════════════════════════


TWO_FILE_SYNC = ('#!/bin/bash\nset -e\necho "$1" >> "{log}"\n'
                 'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n'
                 'echo "$1" > VERSION\n')


@pytest.fixture
def mixed(engine, rel, tmp_path):
    """A dirty tree with every kind of change: a fully staged, an unstaged
    and a partially staged modification, an unstaged deletion, a mode
    change, an untracked release artifact (VERSION, in the contract), an
    untracked file the owner includes (new.txt) and one left out
    (notes.local). README is only TOUCHED (stat changed, content not), so a
    preview that refreshes the user's index would rewrite it."""
    repo, log = rel
    (repo / "scripts" / "dct-version-sync.sh").write_text(TWO_FILE_SYNC.format(log=log))
    commit_all(repo, {"a.txt": "a0\n", "b.txt": "b0\n", "c.txt": "c0\n", "d.txt": "d0\n",
                      "run.sh": "#!/bin/sh\necho hi\n"})
    set_version_files(engine, SLUG, ["VERSION", "pyproject.toml"], confirm=True)
    wip = add_entry(engine, SLUG, "added", "work in progress")["id"]
    other = add_entry(engine, SLUG, "fixed", "unrelated fix")["id"]
    h0 = head(repo)
    write(repo, "a.txt", "a1\n")
    git(repo, "add", "a.txt")
    write(repo, "b.txt", "b1\n")
    write(repo, "c.txt", "c1\n")
    git(repo, "add", "c.txt")
    write(repo, "c.txt", "c2\n")
    (repo / "d.txt").unlink()
    os.chmod(repo / "run.sh", 0o755)
    write(repo, "VERSION", "1.0.0\n")
    write(repo, "new.txt", "new\n")
    write(repo, "notes.local", "scratch\n")
    past = time.time() - 120
    os.utime(repo / "README", (past, past))
    msg = message_file(tmp_path, f"feat: work in progress\n\nChangelog: {wip}\n")
    return {"repo": repo, "h0": h0, "wip": wip, "other": other, "msg": msg}


def test_t06_work_paths_tree_and_message_are_exact(engine, mixed, run, tmp_path):
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = preview(run, repo, "--message-file", mixed["msg"], "--include-untracked", "new.txt",
                  "--no-variants")
    work = doc["details"]["work"]
    assert set(work) == WORK_KEYS
    assert sorted(work["paths"], key=lambda p: p["path"]) == [
        {"path": "VERSION", "change": "added", "staged": "none", "auto": True},
        {"path": "a.txt", "change": "modified", "staged": "all", "auto": False},
        {"path": "b.txt", "change": "modified", "staged": "none", "auto": False},
        {"path": "c.txt", "change": "modified", "staged": "part", "auto": False},
        {"path": "d.txt", "change": "deleted", "staged": "none", "auto": False},
        {"path": "new.txt", "change": "added", "staged": "none", "auto": False},
        {"path": "run.sh", "change": "mode", "staged": "none", "auto": False},
    ]
    assert "new.txt" in work["untracked_included"]
    assert "notes.local" not in work["untracked_included"]
    assert work["untracked_left"] == ["notes.local"]
    message = f"feat: work in progress\n\nChangelog: {mixed['wip']}\n"
    assert work["message"] == message
    assert work["subject"] == "feat: work in progress"
    assert work["proves"] == [mixed["wip"]]
    assert work["uncertain"] == [mixed["other"]]

    # Round trip: T1 is what `git add -u && git add -- <included> && git
    # write-tree` gives — computed on a COPY, the user's index never used.
    oracle = tmp_path / "oracle"
    shutil.copytree(repo, oracle, symlinks=True)
    git(oracle, "add", "-u")
    git(oracle, "add", "--", "VERSION", "new.txt")
    t1 = git(oracle, "write-tree")
    assert work["tree"] == t1

    view = variant(doc)["consent"]
    assert set(view["work"]) == VIEW_WORK_KEYS
    listing = {line.split("\t")[1]: line.split("\t")[0].split(" ")
               for line in git(oracle, "ls-tree", "-r", t1).splitlines()}
    pss = ["VERSION", "a.txt", "b.txt", "c.txt", "d.txt", "new.txt", "run.sh"]
    assert view["work"] == {
        "tree": t1, "subject": "feat: work in progress", "message_sha256": sha256(message),
        "proves": [mixed["wip"]],
        "paths": {p: (None if p not in listing else {"mode": listing[p][0],
                                                       "blob": listing[p][2]}) for p in pss}}
    assert view["work"]["paths"]["run.sh"]["mode"] == "100755"
    assert view["work"]["paths"]["d.txt"] is None
    assert view["candidate_tree"] == t1  # no attestation: K* = C*
    assert view["base"] == {"head": h0, "ref": "refs/heads/main"}
    assert view["entries"]["selected"] == [mixed["wip"]]  # proven by the synthetic W
    assert view["entries"]["waiting"] == {str(mixed["other"]): "no_trailer"}
    assert doc["details"]["all_unavailable"] == "not_requested"


def test_t06_the_users_index_work_tree_and_refs_are_byte_identical(engine, mixed, run):
    repo = mixed["repo"]
    index = repo / ".git" / "index"
    before_index = index.read_bytes()  # read raw: no git command refreshes it first
    before_files = work_files(repo)
    before_refs = other_refs(repo)
    doc = preview(run, repo, "--message-file", mixed["msg"], "--include-untracked", "new.txt",
                  "--no-variants")
    assert index.read_bytes() == before_index, "the preview wrote the user's index"
    assert work_files(repo) == before_files
    assert other_refs(repo) == before_refs
    assert head(repo) == mixed["h0"]
    assert worktrees(repo) == [f"worktree {repo}"]
    # The synthetic commit W is reachable from no ref (the pin is a tree).
    subjects = git(repo, "log", "--all", "--format=%s").splitlines()
    assert "feat: work in progress" not in subjects
    stored = slot(engine)
    assert git(repo, "rev-parse", f"{stored['pin']}:work") == doc["details"]["work"]["tree"]


def test_t06_the_slot_stores_the_work_state_for_the_later_checks(engine, mixed, run):
    repo = mixed["repo"]
    doc = preview(run, repo, "--message-file", mixed["msg"], "--include-untracked", "new.txt",
                  "--no-variants")
    stored = slot(engine)
    assert set(stored["work"]) == SLOT_WORK_KEYS
    assert stored["work"]["message"] == doc["details"]["work"]["message"]
    assert "new.txt" in stored["work"]["untracked_included"]
    state = stored["work"]["paths_state"]
    assert state["d.txt"] is None
    assert state["a.txt"] == sha256(b"a1\n")
    assert state["c.txt"] == sha256(b"c2\n")  # the WORK-TREE bytes, not the staged ones
    assert state["new.txt"] == sha256(b"new\n")
    assert {"a.txt", "b.txt", "c.txt", "d.txt", "run.sh"} <= set(stored["work"]["index_before"])
    assert "notes.local" not in state


def _staged_content(repo):
    write(repo, "x.txt", "x1\n")
    git(repo, "add", "x.txt")
    write(repo, "x.txt", "x0\n")


def _staged_removal(repo):
    git(repo, "rm", "-q", "--cached", "x.txt")  # the file stays in the work tree


def _staged_mode(repo):
    git(repo, "update-index", "--chmod=+x", "x.txt")  # the file stays 0644


@pytest.mark.parametrize("stage", [_staged_content, _staged_removal, _staged_mode],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t06_an_index_only_difference_is_nothing_to_commit(engine, rel, run, tmp_path, stage):
    """The index differs from HEAD but the work tree is back at HEAD: the
    future tree T1 equals HEAD's tree, so a work-in-progress commit would be
    EMPTY (and released with the message's proven ids). The preview refuses
    with `index_only: {paths}` — no work block, nothing stored, the user's
    index, work tree and refs byte-identical."""
    repo, _ = rel
    commit_all(repo, {"x.txt": "x0\n"})
    wip = add_entry(engine, SLUG, "added", "wip")["id"]
    h0 = head(repo)
    stage(repo)
    assert git(repo, "diff", "--cached", "--name-only") == "x.txt"  # precondition
    index = repo / ".git" / "index"
    before_index, before_files, before_refs = index.read_bytes(), work_files(repo), refs(repo)
    msg = message_file(tmp_path, f"feat: wip\n\nChangelog: {wip}\n", "index-only.txt")
    doc = refused(run, repo, "--message-file", msg, key="index_only")
    assert doc["details"]["index_only"] == {"paths": ["x.txt"]}
    assert doc["details"].get("work") is None and "variants" not in doc["details"]
    assert slot(engine) is None and preview_refs(repo) == []
    assert index.read_bytes() == before_index, "the refused preview wrote the user's index"
    assert work_files(repo) == before_files
    assert refs(repo) == before_refs and head(repo) == h0
    assert "index" in doc["details"]["error"], doc["details"]["error"]


@pytest.mark.parametrize("stage", [_staged_content, _staged_removal, _staged_mode],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t06_an_index_only_difference_without_a_message_is_refused_as_index_only(
        engine, rel, run, stage):
    """An index-only difference has nothing to commit, so no message is
    asked for: without --message-file the preview refuses with
    `index_only` (never `message_required` first, then `index_only` once a
    message is written), nothing stored, the index untouched."""
    repo, _ = rel
    commit_all(repo, {"x.txt": "x0\n"})
    h0 = head(repo)
    stage(repo)
    index = repo / ".git" / "index"
    before_index = index.read_bytes()
    doc = refused(run, repo, key="index_only")
    assert doc["details"]["index_only"] == {"paths": ["x.txt"]}
    assert "message_required" not in doc["details"]
    assert slot(engine) is None and preview_refs(repo) == []
    assert index.read_bytes() == before_index and head(repo) == h0


def test_t06_several_index_only_paths_are_all_named(engine, rel, run, tmp_path):
    repo, _ = rel
    commit_all(repo, {"x.txt": "x0\n", "y.txt": "y0\n"})
    for name in ("y.txt", "x.txt"):
        write(repo, name, "staged\n")
        git(repo, "add", name)
        write(repo, name, f"{name[0]}0\n")
    doc = refused(run, repo, "--message-file", message_file(tmp_path, "chore: x\n"),
                  key="index_only")
    assert doc["details"]["index_only"] == {"paths": ["x.txt", "y.txt"]}


def test_t06_guard_an_index_only_path_beside_a_real_change_is_left_out_of_the_work(
        engine, rel, run, tmp_path):
    """Guard: when another path really changes, T1 differs from HEAD's
    tree — the preview is accepted and the index-only path is not part of
    the work shown (its future tree entry is HEAD's)."""
    repo, _ = rel
    commit_all(repo, {"x.txt": "x0\n", "y.txt": "y0\n"})
    _staged_content(repo)
    write(repo, "y.txt", "y1\n")
    doc = preview(run, repo, "--message-file", message_file(tmp_path, "chore: y\n"),
                  "--no-variants")
    work = doc["details"]["work"]
    assert [p["path"] for p in work["paths"]] == ["y.txt"]
    assert work["tree"] != tree_of(repo, "HEAD")
    assert "index_only" not in doc["details"]


# ═════════════════════════════════════════════════════════════════════════════
# 7. Every refusal of the preview stores nothing and keeps the earlier slot
# ═════════════════════════════════════════════════════════════════════════════


class Ctx:
    def __init__(self, engine, repo: Path, tmp_path: Path, a: int):
        self.engine, self.repo, self.tmp_path, self.a = engine, repo, tmp_path, a

    def msg(self, text: str = "feat: wip\n") -> str:
        return message_file(self.tmp_path, text, "refusal-msg.txt")

    def dirty(self) -> None:
        write(self.repo, "f.txt", "dirty\n")

    def script(self, body: str) -> None:
        (self.repo / "scripts" / "dct-version-sync.sh").write_text(body)
        git(self.repo, "commit", "-qam", "chore: sync script")


def r_executor(c: Ctx):
    set_release_executor(c.engine, SLUG, "own")
    return [], "executor", 1


def r_unfinished(c: Ctx):
    release_entries(c.engine, SLUG, "1.1.0", entry_ids=[c.a])
    return [], "unfinished", 1


def r_changelog_file(c: Ctx):
    commit_all(c.repo, {"NEWS.md": HISTORY})  # a second changelog → ambiguous
    return [], "changelog_file", 1


def r_local_tag(c: Ctx):
    git(c.repo, "tag", "v1.2.0")
    return ["--version", "1.2.0"], "tags", 1


def r_nothing_to_prepare(c: Ctx):
    return ["--version", "0.9.0"], None, 1


def r_message_required(c: Ctx):
    c.dirty()
    return [], "message_required", 1


def r_nothing_to_commit(c: Ctx):
    return ["--message-file", c.msg()], "nothing_to_commit", 1


def r_message_empty(c: Ctx):
    c.dirty()
    return ["--message-file", c.msg("  \n\n\t\n")], "message_empty", 1


def r_message_unknown_id(c: Ctx):
    c.dirty()
    return ["--message-file", c.msg("feat: wip\n\nChangelog: 999999\n")], \
        "message_foreign_ids", 1


def r_message_other_projects_id(c: Ctx):
    create_project(c.engine, slug="proj-other", name="O", path="/tmp/proj-other")
    theirs = add_entry(c.engine, "proj-other", "added", "theirs")["id"]
    c.dirty()
    return ["--message-file", c.msg(f"feat: wip\n\nChangelog: {theirs}\n")], \
        "message_foreign_ids", 1


def r_message_released_id(c: Ctx):
    from dct.core.changelog import import_released_entry

    old = import_released_entry(c.engine, SLUG, "added", "old", "1.0.0")["id"]
    c.dirty()
    return ["--message-file", c.msg(f"feat: wip\n\nChangelog: {old}\n")], \
        "message_foreign_ids", 1


def _in_progress(marker: str):
    def scenario(c: Ctx):
        path = c.repo / ".git" / marker
        if marker.startswith("rebase"):
            path.mkdir()
            (path / "head-name").write_text("refs/heads/main\n")
        else:
            path.write_text(head(c.repo) + "\n")
        return [], "in_progress", 1
    scenario.__name__ = f"r_in_progress_{marker}"
    return scenario


def r_index_only(c: Ctx):
    original = (c.repo / "f.txt").read_text()
    write(c.repo, "f.txt", "staged, then taken back in the work tree\n")
    git(c.repo, "add", "f.txt")
    write(c.repo, "f.txt", original)
    return ["--message-file", c.msg()], "index_only", 1


def r_conflicted(c: Ctx):
    commit_all(c.repo, {"x.txt": "base\n"})
    blobs = [hash_bytes(c.repo, f"{side}\n".encode(), write=True)
             for side in ("base", "ours", "theirs")]
    git_in(c.repo, "0 " + "0" * 40 + "\tx.txt\n"
           + "".join(f"100644 {b} {stage}\tx.txt\n" for stage, b in enumerate(blobs, 1)),
           "update-index", "--index-info")
    write(c.repo, "x.txt", "<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n")
    return ["--message-file", c.msg()], "conflicted", 1


def r_intent_to_add(c: Ctx):
    write(c.repo, "planned.txt", "later\n")
    git(c.repo, "add", "-N", "planned.txt")
    return ["--message-file", c.msg()], "intent_to_add", 1


def r_submodule(c: Ctx):
    h = head(c.repo)
    (c.repo / "sub").mkdir()
    git(c.repo, "update-index", "--add", "--cacheinfo", f"160000,{h},sub")
    git(c.repo, "commit", "-q", "-m", "chore: gitlink")
    git(c.repo, "update-index", "--cacheinfo", f"160000,{head(c.repo)},sub")
    return ["--message-file", c.msg()], "submodule", 1


def _hidden(flag: str):
    def scenario(c: Ctx):
        write(c.repo, "README", "changed and staged\n")
        git(c.repo, "add", "README")
        git(c.repo, "update-index", flag, "README")
        return ["--message-file", c.msg()], "hidden_index_flags", 1
    scenario.__name__ = f"r_hidden{flag.replace('-', '_')}"
    return scenario


def r_filtered(c: Ctx):
    # A filter name no configuration defines (hermetic: no git-lfs needed);
    # the ATTRIBUTE is what makes `git add` unpredictable.
    commit_all(c.repo, {".gitattributes": "*.bin filter=seal -text\n", "data.bin": "v1\n"})
    write(c.repo, "data.bin", "v2\n")
    return ["--message-file", c.msg()], "filtered_paths", 1


def r_secret_tracked_env(c: Ctx):
    commit_all(c.repo, {".env": "TOKEN=old\n"})
    write(c.repo, ".env", "TOKEN=new\n")
    return ["--message-file", c.msg()], "secret_like", 1


def _secret_untracked(name: str):
    def scenario(c: Ctx):
        c.dirty()
        write(c.repo, name, "-----BEGIN PRIVATE KEY-----\n")
        return ["--message-file", c.msg(), "--include-untracked", name], "secret_like", 1
    scenario.__name__ = f"r_secret_{name.replace('.', '_')}"
    return scenario


def r_untracked_is_tracked(c: Ctx):
    c.dirty()
    return ["--message-file", c.msg(), "--include-untracked", "README"], "bad_untracked", 1


def r_untracked_is_ignored(c: Ctx):
    commit_all(c.repo, {".gitignore": "*.log\n"})
    write(c.repo, "debug.log", "noise\n")
    c.dirty()
    return ["--message-file", c.msg(), "--include-untracked", "debug.log"], "bad_untracked", 1


def r_untracked_is_missing(c: Ctx):
    c.dirty()
    return ["--message-file", c.msg(), "--include-untracked", "nope.txt"], "bad_untracked", 1


def r_attest_proven(c: Ctx):
    return ["--attest", str(c.a)], "not_unproven", 1


def r_attest_reverted(c: Ctx):
    entry, sha = proving(c.engine, c.repo, "fixed", "doomed")
    revert(c.repo, sha)
    assert unproven_reason(c.engine, c.repo, entry) == "reverted"
    c.reverted = entry
    return ["--attest", str(entry)], "not_attestable", 1


def r_entries_unproven(c: Ctx):
    waiting = add_entry(c.engine, SLUG, "fixed", "waiting")["id"]
    return ["--entries", str(waiting)], "unproven", 1


def r_version_unresolved(c: Ctx):
    with c.engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == SLUG)
                     .values(current_version="garbage"))
    return [], "version_unresolved", 1


def r_sync_failed(c: Ctx):
    c.script('#!/bin/bash\necho boom >&2\nexit 3\n')
    return [], "sync", 3


def r_script_writes_the_checkout(c: Ctx):
    c.script(f'#!/bin/bash\necho "$1" > pyproject.toml\necho hacked >> "{c.repo}/README"\n')
    return [], "checkout_changed", 1


REFUSALS = [
    r_executor, r_unfinished, r_changelog_file, r_local_tag, r_nothing_to_prepare,
    r_message_required, r_nothing_to_commit, r_message_empty, r_message_unknown_id,
    r_message_other_projects_id, r_message_released_id, r_index_only,
    _in_progress("MERGE_HEAD"), _in_progress("CHERRY_PICK_HEAD"), _in_progress("REVERT_HEAD"),
    _in_progress("rebase-merge"),
    r_conflicted, r_intent_to_add, r_submodule, _hidden("--assume-unchanged"),
    _hidden("--skip-worktree"), r_filtered, r_secret_tracked_env, _secret_untracked("id_rsa"),
    _secret_untracked("server.pem"), _secret_untracked("credentials-prod.json"),
    _secret_untracked(".env.local"),
    r_untracked_is_tracked, r_untracked_is_ignored, r_untracked_is_missing,
    r_attest_proven, r_attest_reverted, r_entries_unproven, r_version_unresolved,
    r_sync_failed, r_script_writes_the_checkout,
]


@pytest.mark.parametrize("scenario", REFUSALS, ids=lambda f: f.__name__)
def test_t07_each_refusal_stores_nothing_and_keeps_the_earlier_slot(
        engine, rel, run, tmp_path, scenario):
    repo, _ = rel
    a = proven(engine, repo, "feature a")
    preview(run, repo)  # the earlier slot a refusal must keep
    kept = slot(engine)
    kept_pins = preview_refs(repo)
    assert kept is not None and len(kept_pins) == 1
    ctx = Ctx(engine, repo, tmp_path, a)
    argv, key, code = scenario(ctx)
    doc = refused(run, repo, *argv, key=key, code=code)
    assert slot(engine) == kept, "a refused preview changed the stored slot"
    assert preview_refs(repo) == kept_pins, "a refused preview added or removed a pin"
    assert get_pending_proposal(engine, SLUG) is None
    assert worktrees(repo) == [f"worktree {repo}"]
    if key == "message_required":
        required = doc["details"]["message_required"]
        assert set(required) == {"paths", "untracked"} and "f.txt" in required["paths"]
    if key == "not_attestable":
        assert doc["details"]["not_attestable"] == [{"id": ctx.reverted, "reason": "reverted"}]


def test_t07_a_template_env_file_is_not_secret_like(engine, rel, run, tmp_path):
    """Boundary of the secret-like patterns: `.env.example` / `.env.sample`
    are excluded."""
    repo, _ = rel
    commit_all(repo, {".env.example": "TOKEN=\n", ".env.sample": "TOKEN=\n"})
    write(repo, ".env.example", "TOKEN=<your token>\n")
    write(repo, ".env.sample", "TOKEN=<your token>\n")
    doc = preview(run, repo, "--message-file", message_file(tmp_path, "docs: env template\n"))
    assert {p["path"] for p in doc["details"]["work"]["paths"]} == {".env.example",
                                                                    ".env.sample"}


def test_t07_preview_excludes_the_digest_flags(engine, rel, run):
    """A1 flag rules: argparse rejects --plan-digest / --proposal-digest with
    --preview, and --discard-preview with any binding flag."""
    repo, _ = rel
    proven(engine, repo, "feature a")
    preview(run, repo)
    kept = slot(engine)
    for argv in (["--preview", "--plan-digest", "0" * 64],
                 ["--preview", "--proposal-digest", "0" * 64],
                 ["--discard-preview", "--version", "1.2.0"],
                 ["--discard-preview", "--preview"]):
        code, _, err = run(repo, *argv)
        assert code != 0, argv
        assert "unrecognized arguments: --preview" not in err
        assert "unrecognized arguments: --discard-preview" not in err
        assert slot(engine) == kept, argv


# ═════════════════════════════════════════════════════════════════════════════
# 8. The changelog file is checked at the synthetic candidate C*, not at HEAD
# ═════════════════════════════════════════════════════════════════════════════


def test_t08_a_dirty_tracked_changelog_is_part_of_the_work_and_does_not_refuse(
        engine, rel, run, tmp_path):
    repo, _ = rel
    wip = add_entry(engine, SLUG, "added", "history fix")["id"]
    text = (repo / "changelog.md").read_text().replace("- first", "- first (typo fixed)")
    write(repo, "changelog.md", text)
    doc = preview(run, repo, "--message-file",
                  message_file(tmp_path, f"docs: fix history\n\nChangelog: {wip}\n"))
    assert {p["path"] for p in doc["details"]["work"]["paths"]} == {"changelog.md"}
    view = variant(doc)["consent"]
    assert view["changelog"]["base_blob"] == hash_bytes(repo, text.encode())  # = T1's blob
    assert view["work"]["paths"]["changelog.md"]["blob"] == view["changelog"]["base_blob"]


def test_t08_an_included_untracked_second_changelog_refuses_at_the_candidate(
        engine, rel, run, tmp_path):
    repo, _ = rel
    wip = add_entry(engine, SLUG, "added", "news")["id"]
    write(repo, "NEWS.md", HISTORY)  # untracked: no candidate at HEAD, one at C*
    doc = refused(run, repo, "--message-file",
                  message_file(tmp_path, f"docs: news\n\nChangelog: {wip}\n"),
                  "--include-untracked", "NEWS.md", key="changelog_file")
    assert doc["details"]["changelog_file"]["problem"] == "ambiguous"
    assert slot(engine) is None


def test_t08_a_second_changelog_deleted_in_the_work_tree_settles_the_ambiguity(
        engine, rel, run, tmp_path):
    repo, _ = rel
    commit_all(repo, {"NEWS.md": HISTORY})  # ambiguous at HEAD
    wip = add_entry(engine, SLUG, "removed", "news file")["id"]
    (repo / "NEWS.md").unlink()  # gone at C*
    doc = preview(run, repo, "--message-file",
                  message_file(tmp_path, f"chore: drop news\n\nChangelog: {wip}\n"))
    assert variant(doc)["consent"]["changelog"]["path"] == "changelog.md"


# ═════════════════════════════════════════════════════════════════════════════
# 9. The message is cleaned exactly as `git commit -F` cleans it
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("cleanup", ["default", "strip", "verbatim", "whitespace", "scissors"])
def test_t09_message_cleanup_matches_a_real_commit(engine, rel, run, tmp_path, cleanup):
    """A paragraph starting `#<id>` is a comment only for `strip`; under the
    default cleanup of `git commit -F` it stays — and is the subject here."""
    repo, _ = rel
    wip = add_entry(engine, SLUG, "added", "parser")["id"]
    git(repo, "config", "commit.cleanup", cleanup)  # local: wins over any global setting
    raw = (f"#{wip} fix the parser   \n\n\n"
           f"feat: real subject\n\n# a note\n\nChangelog: {wip}\n\n\n")
    msg = message_file(tmp_path, raw)
    write(repo, "parser.txt", "parsed\n")
    doc = preview(run, repo, "--message-file", msg, "--include-untracked", "parser.txt")
    work = doc["details"]["work"]

    oracle = tmp_path / "oracle"
    shutil.copytree(repo, oracle, symlinks=True)
    git(oracle, "add", "-A")
    git(oracle, "commit", "-q", "-F", msg)
    body = subprocess.run(["git", "-C", str(oracle), "cat-file", "commit", "HEAD"],
                          capture_output=True, check=True).stdout.decode()
    cleaned = body.split("\n\n", 1)[1]
    from dct.workflows.release.plan import read_commits
    from dct.workflows.release.proof import proven_ids

    [real] = read_commits(str(oracle), "-1", "HEAD")
    assert work["message"] == cleaned
    # git's own subject of the real commit (%s: the first paragraph, its
    # lines right-trimmed and joined) — what W shows and apply compares.
    assert work["subject"] == git(oracle, "log", "-1", "--format=%s", "HEAD")
    assert work["proves"] == sorted(proven_ids(real))
    assert variant(doc)["consent"]["work"]["message_sha256"] == sha256(cleaned)
    assert work["tree"] == git(oracle, "rev-parse", "HEAD^{tree}")


# ═════════════════════════════════════════════════════════════════════════════
# 10. Approximate range: the synthetic commits are dated NOW
# ═════════════════════════════════════════════════════════════════════════════


def test_t10_entries_the_work_commit_proves_are_selected_in_an_approximate_range(
        engine, run, tmp_path):
    """No release tag → the range is `--since=<oldest unreleased − 1 day>`.
    Every real commit is from 2020; only a synthetic W dated now can fall
    inside the window."""
    repo = tmp_path / "old"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_DATE="2020-01-01T00:00:00Z", GIT_COMMITTER_DATE="2020-01-01T00:00:00Z")

    def old_git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                       env=env, capture_output=True, check=True)

    old_git("init", "-q", "-b", "main")
    write(repo, "changelog.md", HISTORY)
    write(repo, "pyproject.toml", PYPROJECT.format(v="1.0.0"))
    old_git("add", ".")
    old_git("commit", "-q", "-m", "chore: setup")
    repo = Path(os.path.realpath(repo))
    create_project(engine, slug=SLUG, name="R", path=str(repo), current_version="1.0.0")
    wip = add_entry(engine, SLUG, "added", "fresh work")["id"]
    write(repo, "work.txt", "new\n")
    doc = preview(run, repo, "--message-file",
                  message_file(tmp_path, f"feat: fresh work\n\nChangelog: {wip}\n"),
                  "--include-untracked", "work.txt", "--no-push")
    view = variant(doc)["consent"]
    assert view["range"]["approximate"] is True
    assert view["entries"]["selected"] == [wip]
    assert view["entries"]["waiting"] == {}
    assert [e["id"] for e in variant(doc)["display"]["entries"]["selected"]] == [wip]


# ═════════════════════════════════════════════════════════════════════════════
# 11. When "with all N" is offered
# ═════════════════════════════════════════════════════════════════════════════


def test_t11_one_waiting_entry_gives_the_all_variant_with_exact_shapes(engine, rel, run):
    repo, _ = rel
    a = proven(engine, repo, "feature a")
    w = add_entry(engine, SLUG, "added", "waiting b")["id"]
    h0 = head(repo)
    doc = preview(run, repo)
    assert doc["details"]["all_unavailable"] is None
    assert [v["name"] for v in doc["details"]["variants"]] == ["shown", "all"]  # never first
    shown, every = variant(doc), variant(doc, "all")
    assert set(every) == VARIANT_KEYS
    assert shown["consent"]["entries"]["selected"] == [a]
    assert shown["consent"]["entries"]["waiting"] == {str(w): "no_trailer"}
    assert shown["consent"]["attest"] is None
    view = every["consent"]
    assert view["attest"] == {"ids": [w]}
    assert view["entries"]["selected"] == sorted([a, w])
    assert view["entries"]["waiting"] == {}
    assert view["work"] is None
    assert view["candidate_tree"] == tree_of(repo, h0)  # A is empty
    assert view["base"] == {"head": h0, "ref": "refs/heads/main"}
    assert every["consent_digest"] == canonical_digest(view)
    assert every["consent_digest"] != shown["consent_digest"]
    assert every["display"]["delta"]["adds"] == [w]
    stored = slot(engine)
    assert set(stored["variants"]) == {shown["consent_digest"], every["consent_digest"]}
    assert stored["variants"][every["consent_digest"]]["name"] == "all"
    # Nothing is attested by the preview.
    assert head(repo) == h0
    assert ATTEST_SUBJECT not in git(repo, "log", "--all", "--format=%s").splitlines()


@pytest.mark.parametrize("waiting,offered", [(4, True), (5, False)])
def test_t11_at_most_four_waiting_entries(engine, rel, run, waiting, offered):
    repo, _ = rel
    proven(engine, repo, "feature a")
    ids = [add_entry(engine, SLUG, "fixed", f"waiting {n}")["id"] for n in range(waiting)]
    doc = preview(run, repo)
    if offered:
        assert doc["details"]["all_unavailable"] is None
        assert variant(doc, "all")["consent"]["attest"] == {"ids": sorted(ids)}
    else:
        assert doc["details"]["all_unavailable"] == "too_many"
        assert [v["name"] for v in doc["details"]["variants"]] == ["shown"]


def test_t11_nothing_waiting(engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    doc = preview(run, repo)
    assert doc["details"]["all_unavailable"] == "none_waiting"
    assert len(doc["details"]["variants"]) == 1


@pytest.mark.parametrize("how,reason", [("revert", "reverted"),
                                        ("partial", "revert_unconfirmed")])
def test_t11_a_reverted_waiting_entry_withholds_the_all_variant(engine, rel, run, how, reason):
    repo, _ = rel
    proven(engine, repo, "feature a")
    add_entry(engine, SLUG, "fixed", "plain waiting")
    entry, sha = proving(engine, repo, "fixed", "doomed")
    if how == "revert":
        revert(repo, sha)
    else:
        partial_revert(repo, sha, "doomed.txt")
    assert unproven_reason(engine, repo, entry) == reason  # precondition
    doc = preview(run, repo)
    assert doc["details"]["all_unavailable"] == "reverted"
    assert [v["name"] for v in doc["details"]["variants"]] == ["shown"]
    waiting = {row["id"]: row for row in variant(doc)["display"]["entries"]["waiting"]}
    assert waiting[entry]["reason"] == reason


def test_t11_no_variants_flag(engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    add_entry(engine, SLUG, "fixed", "waiting")
    doc = preview(run, repo, "--no-variants")
    assert doc["details"]["all_unavailable"] == "not_requested"
    assert len(doc["details"]["variants"]) == 1


def test_t11_an_attested_id_joins_the_shown_variant_and_the_rest_wait(engine, rel, run):
    """Wt = the ids unproven at C* AFTER the shown --attest; the "all"
    variant attests Wt ∪ --attest."""
    repo, _ = rel
    a = proven(engine, repo, "feature a")
    w1 = add_entry(engine, SLUG, "fixed", "attested now")["id"]
    w2 = add_entry(engine, SLUG, "fixed", "still waiting")["id"]
    doc = preview(run, repo, "--attest", str(w1))
    shown, every = variant(doc), variant(doc, "all")
    assert shown["consent"]["attest"] == {"ids": [w1]}
    assert shown["consent"]["entries"]["selected"] == sorted([a, w1])
    assert shown["consent"]["entries"]["waiting"] == {str(w2): "no_trailer"}
    assert every["consent"]["attest"] == {"ids": sorted([w1, w2])}
    assert every["consent"]["entries"]["selected"] == sorted([a, w1, w2])


# ═════════════════════════════════════════════════════════════════════════════
# 12. The script runs again only when the "all" version differs
# ═════════════════════════════════════════════════════════════════════════════


def test_t12_a_different_all_version_runs_the_script_again_and_fills_the_delta(
        engine, rel, run):
    repo, log = rel
    proven(engine, repo, "small fix", category="fixed")  # shown: patch → 1.0.1
    w = add_entry(engine, SLUG, "added", "big feature")["id"]  # all: minor → 1.1.0
    doc = preview(run, repo)
    assert doc["version"] == "1.0.1"
    shown, every = variant(doc), variant(doc, "all")
    assert shown["consent"]["version"]["target"] == "1.0.1"
    assert every["consent"]["version"]["target"] == "1.1.0"
    assert every["suggested"]["target"] == "1.1.0" and every["suggested"]["reason"] == "features"
    assert sync_runs(log) == ["1.0.1", "1.1.0"]
    assert shown["display"]["delta"] is None
    delta = every["display"]["delta"]
    assert set(delta) == {"version", "adds", "files"}
    assert delta["version"] == "1.1.0" and delta["adds"] == [w]
    assert every["consent"]["version_files"]["expected_blobs"]["pyproject.toml"] == hash_bytes(
        repo, PYPROJECT.format(v="1.1.0").encode())
    assert every["consent"]["push"]["tag"] == "v1.1.0"


def test_t12_the_same_all_version_reuses_the_shown_sync(engine, rel, run):
    repo, log = rel
    proven(engine, repo, "feature a")  # shown: minor → 1.1.0
    w = add_entry(engine, SLUG, "fixed", "small fix")["id"]  # all: still 1.1.0
    doc = preview(run, repo)
    shown, every = variant(doc), variant(doc, "all")
    assert sync_runs(log) == ["1.1.0"]
    assert every["consent"]["version"]["target"] == "1.1.0"
    assert every["consent"]["version_files"] == shown["consent"]["version_files"]
    assert every["display"]["delta"]["adds"] == [w]


# ═════════════════════════════════════════════════════════════════════════════
# 13. The remote (a bare repository)
# ═════════════════════════════════════════════════════════════════════════════


def test_t13_a_remote_tag_of_the_suggested_version_moves_it_to_the_next_patch(
        engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    git(repo, "tag", "v1.1.0")
    git(repo, "push", "-q", "origin", "v1.1.0")
    git(repo, "tag", "-d", "v1.1.0")  # on the remote only
    doc = preview(run, repo)
    shown = variant(doc)
    assert doc["version"] == "1.1.1"
    assert shown["suggested"]["target"] == "1.1.1"
    assert shown["suggested"]["reason"] == "tag_taken" and shown["suggested"]["safe"] is False
    assert shown["suggested"]["taken"] == ["1.1.0"]
    assert shown["consent"]["version"]["target"] == "1.1.1"
    assert shown["consent"]["push"]["tag"] == "v1.1.1"


def test_t13_a_remote_tag_of_an_explicit_version_refuses(engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    git(repo, "tag", "v1.1.0")
    git(repo, "push", "-q", "origin", "v1.1.0")
    git(repo, "tag", "-d", "v1.1.0")
    refused(run, repo, "--version", "1.1.0", key="remote_tag_exists")
    assert slot(engine) is None


def test_t13_a_remote_branch_ahead_refuses(engine, rel, run, tmp_path):
    repo, _ = rel
    proven(engine, repo, "feature a")
    git(repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", push_url(repo), str(other))
    commit(other, "feat: elsewhere\n", "elsewhere.txt")
    git(other, "push", "-q", "origin", "main")
    refused(run, repo, key="remote_ahead")
    assert slot(engine) is None


def test_t13_an_unreachable_remote_is_a_warning_not_a_refusal(engine, rel, run, tmp_path):
    repo, _ = rel
    proven(engine, repo, "feature a")
    url = push_url(repo)
    Path(url).rename(tmp_path / "moved-away.git")
    doc = preview(run, repo)
    remote = doc["details"]["remote"]
    assert remote["checked"] is False and remote["error"]
    assert remote["url"] == url
    assert doc["warnings"], "an unchecked remote must be reported"
    assert variant(doc)["consent"]["push"]["url"] == url


# ═════════════════════════════════════════════════════════════════════════════
# 14. Outside git
# ═════════════════════════════════════════════════════════════════════════════


def test_t14_outside_git_the_proposal_comes_from_the_blob_store(engine, plain, run, tmp_path):
    root, log = plain
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    doc = preview(run, tmp_path, "--entries", str(a))
    details = doc["details"]
    assert details["work"] is None
    assert details["base"]["head"] is None and details["base"]["ref"] is None
    assert details["all_unavailable"] == "no_git"
    view = variant(doc)["consent"]
    assert view["release_mode"] == "without_git"
    assert view["work"] is None and view["attest"] is None and view["push"] is None
    assert view["base"] == {"head": None, "ref": None}
    assert view["candidate_tree"] is None
    assert view["checkout_root"] is None
    assert view["changelog"]["base_blob"] == sha256(HISTORY)
    assert view["version_files"]["paths"] == ["VERSION"]
    assert view["version_files"]["base_blobs"] == {"VERSION": sha256(b"1.0.0\n")}
    assert view["version_files"]["expected_blobs"] == {"VERSION": sha256(b"1.1.0\n")}
    assert sync_runs(log) == ["1.1.0"]
    assert (root / "VERSION").read_text() == "1.0.0\n"  # ran on a copy

    stored = slot(engine)
    assert stored["pin"] is None
    content = stored["variants"][variant(doc)["consent_digest"]]["proposal_content"]
    assert content["release_mode"] == "without_git"
    digest = content["expected_blobs"]["VERSION"]
    assert os.path.exists(effects._store_path(digest))
    assert any(item["proposal"] == content for item in ledger.live_proposals(engine))
    # The cleanup of ANOTHER proposal naming the same blob keeps it: the
    # preview is live.
    other = {**content, "id": "01OTHERPROPOSAL", "candidate_sha": None, "plan_digest": None}
    effects.release_proposal_resources(engine, SLUG, other, None)
    assert os.path.exists(effects._store_path(digest)), "a live preview's blob was removed"


def test_t14_outside_git_attest_refuses(engine, plain, run, tmp_path):
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    refused(run, tmp_path, "--attest", str(a), key="no_git")
    assert slot(engine) is None


# ═════════════════════════════════════════════════════════════════════════════
# 15. --discard-preview
# ═════════════════════════════════════════════════════════════════════════════


def test_t15_discard_clears_the_slot_and_removes_the_pin(engine, rel, run):
    repo, _ = rel
    proven(engine, repo, "feature a")
    doc = preview(run, repo)
    pin = slot(engine)["pin"]
    out = discard(run, repo)
    assert out["details"]["discarded"] == doc["details"]["preview_id"]
    assert slot(engine) is None
    assert not ref_exists(repo, pin)
    assert preview_refs(repo) == []
    again = discard(run, repo)
    assert again["details"]["discarded"] is None


def test_t15_discard_releases_the_blob_store_outside_git(engine, plain, run, tmp_path):
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    doc = preview(run, tmp_path, "--entries", str(a))
    content = slot(engine)["variants"][variant(doc)["consent_digest"]]["proposal_content"]
    stored_blob = effects._store_path(content["expected_blobs"]["VERSION"])
    assert os.path.exists(stored_blob)
    out = discard(run, tmp_path)
    assert out["details"]["discarded"] == doc["details"]["preview_id"]
    assert slot(engine) is None
    assert not os.path.exists(stored_blob), "the discarded preview's blob was kept"
    assert all(item["proposal"] != content for item in ledger.live_proposals(engine))


def test_t15_discard_on_an_empty_slot_is_a_noop(engine, rel, run):
    repo, _ = rel
    assert discard(run, repo)["details"]["discarded"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 30-31. Core: validators, progress, consent content_sha256
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def stored_preview(engine, mixed, run):
    """A real stored slot with work AND two variants (the "all" one)."""
    doc = preview(run, mixed["repo"], "--message-file", mixed["msg"], "--include-untracked",
                  "new.txt")
    assert len(doc["details"]["variants"]) == 2
    return slot(engine)


def test_t30_the_validators_accept_the_stored_slot_unchanged(stored_preview):
    assert ledger.PREVIEW_MAX_BYTES == 262144
    assert ledger.validate_preview(stored_preview) == stored_preview
    for digest, item in stored_preview["variants"].items():
        assert ledger.validate_consent_view(item["consent"]) == item["consent"]
        assert canonical_digest(item["consent"]) == digest


def _first(preview_doc: dict) -> tuple[str, dict]:
    digest = min(preview_doc["variants"])
    return digest, preview_doc["variants"][digest]


def _with(doc: dict, change) -> dict:
    copy = json.loads(json.dumps(doc))
    change(copy)
    return copy


def _rekeyed(doc: dict, change) -> tuple[dict, str]:
    """(slot, key) — the slot with the first variant's CONSENT VIEW changed
    by `change` and re-keyed by `key`, the canonical digest of the CHANGED
    view: the variant key is then consistent, so only the consent view's
    own schema can refuse it (a relaxed schema check would let the slot
    through)."""
    copy = json.loads(json.dumps(doc))
    digest, item = _first(copy)
    assert copy["progress"]["consent"] != digest, "re-keying would orphan the progress"
    change(item["consent"])
    key = canonical_digest(item["consent"])
    copy["variants"][key] = copy["variants"].pop(digest)
    return copy, key


@pytest.mark.parametrize("where,change,message", [
    ("top", lambda p: p.update(extra=1), r"^preview has unknown keys: extra"),
    ("base", lambda p: p["base"].update(extra=1), r"^preview\.base has unknown keys: extra"),
    ("work", lambda p: p["work"].update(extra=1), r"^preview\.work has unknown keys: extra"),
    ("progress", lambda p: p["progress"].update(extra=1),
     r"^preview\.progress has unknown keys: extra"),
    ("variant", lambda p: _first(p)[1].update(extra=1),
     r"^preview\.variants\[…\] has unknown keys: extra"),
])
def test_t30_unknown_keys_of_the_slot_are_refused(stored_preview, where, change, message):
    with pytest.raises(ValueError, match=message):
        ledger.validate_preview(_with(stored_preview, change))


CONSENT_SCHEMA_CASES = [
    ("consent", lambda c: c.update(extra=1), r"^consent view has unknown keys: extra"),
    ("consent.changelog", lambda c: c["changelog"].update(extra=1),
     r"^consent view\.changelog has unknown keys: extra"),
    ("consent.work", lambda c: c["work"].update(extra=1),
     r"^consent view\.work has unknown keys: extra"),
    ("consent.entries", lambda c: c["entries"].update(extra=1),
     r"^consent view\.entries has unknown keys: extra"),
    ("bad_composed", lambda c: c["changelog"].update(composed_sha256="xyz"),
     r"^consent view\.changelog\.composed_sha256 "),
    ("bad_message", lambda c: c["work"].update(message_sha256="G" * 64),
     r"^consent view\.work\.message_sha256 "),
]


@pytest.mark.parametrize("where,change,message", CONSENT_SCHEMA_CASES,
                         ids=[c[0] for c in CONSENT_SCHEMA_CASES])
def test_t30_the_consent_view_schema_refuses_a_consistently_keyed_variant(
        stored_preview, where, change, message):
    """The variant key IS the digest of the broken view, so the digest
    binding cannot be what refuses: the consent view's closed schema must
    (validate_preview through consent_view_digest, and validate_consent_view
    itself), naming the field."""
    broken, key = _rekeyed(stored_preview, change)
    with pytest.raises(ValueError, match=message):
        ledger.validate_preview(broken)
    with pytest.raises(ValueError, match=message):
        ledger.validate_consent_view(broken["variants"][key]["consent"])


def test_t30_re_keying_alone_keeps_the_slot_valid(stored_preview):
    """Control for the cases above: re-keying an UNCHANGED view is
    accepted — the refusals there come from the change, not the method."""
    same, key = _rekeyed(stored_preview, lambda c: None)
    assert key == _first(stored_preview)[0]
    assert ledger.validate_preview(same) == stored_preview


def test_t30_a_variant_key_that_is_not_its_view_digest_is_refused(stored_preview):
    """The digest binding on its own: a VALID view under another key."""
    digest, item = _first(stored_preview)
    not_hex = _with(stored_preview, lambda p: p["variants"].update(
        {"not-a-digest": p["variants"].pop(digest)}))
    wrong = _with(stored_preview, lambda p: p["variants"].update(
        {"0" * 64: p["variants"].pop(digest)}))  # names no consent's canonical digest
    assert ledger.validate_consent_view(item["consent"]) == item["consent"]
    with pytest.raises(ValueError, match=r"^preview\.variants key "):
        ledger.validate_preview(not_hex)
    with pytest.raises(ValueError, match="is not the digest of its consent view"):
        ledger.validate_preview(wrong)
    with pytest.raises(ValueError, match=r"^consent view format must be 1"):
        ledger.validate_consent_view({**item["consent"], "format": 2})


def test_t30_an_oversize_preview_is_refused_never_truncated(engine, stored_preview):
    huge = _with(stored_preview, lambda p: p["work"].update(
        message="x" * (ledger.PREVIEW_MAX_BYTES + 1)))
    with pytest.raises(ValueError, match="too long"):
        ledger.validate_preview(huge)
    with pytest.raises(ValueError):
        ledger.set_preview(engine, SLUG, huge)
    assert slot(engine) == stored_preview  # nothing written


def test_t30_an_old_consent_digests_unchanged_and_content_sha256_is_optional():
    old = {"version": "v1.1", "entry_ids": [3, 1, 3], "candidate_sha": "A" * 40,
           "proposal_sha256": "b" * 64, "render": {"pr_refs": False},
           "push_target": {"remote": "origin", "url": "/tmp/remote.git", "branch": "main",
                           "tag": "v1.1.0"}, "executor": "dct"}
    canonical = ledger.validate_consent(old)
    assert "content_sha256" not in canonical
    # The digest of this consent computed by the code BEFORE content_sha256.
    assert canonical_digest(canonical) == \
        "37ed14b0650c8bd62fc84ea5458d2da7e66bedaffb9102fd2811d9e5763fb9f1"
    assert "content_sha256" in ledger.CONSENT_KEYS
    bound = ledger.validate_consent({**old, "content_sha256": "C" * 64})
    assert bound["content_sha256"] == "c" * 64
    with pytest.raises(ValueError):
        ledger.validate_consent({**old, "content_sha256": "not a digest"})


def test_t30_progress_is_written_once_for_its_own_preview(engine, stored_preview, mixed):
    pid = stored_preview["id"]
    w = mixed["h0"]
    ledger.record_preview_progress(engine, SLUG, pid, work_commit=w)
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": None, "consent": None}
    with pytest.raises(ValueError):
        ledger.record_preview_progress(engine, SLUG, pid, work_commit="f" * 40)
    with pytest.raises(ValueError):
        ledger.record_preview_progress(engine, SLUG, "01SOMEOTHERPREVIEW", attest_commit=w)
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": None, "consent": None}
    ledger.record_preview_progress(engine, SLUG, pid, attest_commit=w)
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": w, "consent": None}


def test_progress_consent_is_one_of_the_variants_and_written_once(engine, stored_preview, mixed):
    """G2: progress.consent names the screen the commits were made under —
    a digest that is no variant of the slot is refused, and once written
    another one is refused (nothing written either time)."""
    pid = stored_preview["id"]
    c, other = list(stored_preview["variants"])
    w = mixed["h0"]
    with pytest.raises(ValueError):
        ledger.record_preview_progress(engine, SLUG, pid, work_commit=w, consent="a" * 64)
    assert slot(engine)["progress"] == {"work_commit": None, "attest_commit": None,
                                        "consent": None}
    ledger.record_preview_progress(engine, SLUG, pid, work_commit=w, consent=c)
    ledger.record_preview_progress(engine, SLUG, pid, consent=c)  # a confirmation
    with pytest.raises(ValueError):
        ledger.record_preview_progress(engine, SLUG, pid, consent=other)
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": None, "consent": c}


def _consent(entry: int, candidate: str, **extra) -> dict:
    return {"version": "1.1.0", "entry_ids": [entry], "candidate_sha": candidate,
            "render": {"pr_refs": False}, "push_target": None, **extra}


def test_t31_a_content_digest_without_a_proposal_digest_is_inconsistent(engine):
    assert "content_sha256" in ledger.CONSENT_KEYS  # else the refusal is an unknown key
    candidate = "a" * 40
    state = {"version": "1.1.0", "entry_ids": [1], "candidate_sha": candidate,
             "proposal": None, "consent": ledger.validate_consent(_consent(
                 1, candidate, proposal_sha256=None, content_sha256="c" * 64))}
    with pytest.raises(ValueError, match="content_sha256"):
        ledger.check_record_consistency(state)


def test_t31_the_stamp_refuses_it_and_writes_nothing(engine):
    assert "content_sha256" in ledger.CONSENT_KEYS  # else the refusal is an unknown key
    create_project(engine, slug=SLUG, name="R", path="/tmp/proj-r", current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    with pytest.raises(ValueError, match="content_sha256"):
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a], candidate_sha="a" * 40,
                        consent=_consent(a, "a" * 40, content_sha256="c" * 64))
    assert ledger.get_release(engine, SLUG) is None


def test_t31_a_content_digest_with_its_proposal_is_recorded(engine):
    create_project(engine, slug=SLUG, name="R", path="/tmp/proj-r", current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    candidate = "a" * 40
    proposal = {"paths": [], "base_blobs": {}, "expected_blobs": {}, "script": None,
                "candidate_sha": candidate, "version": "1.1.0", "release_mode": "git",
                "changelog": None, "id": "01PREVIEWID"}
    consent = _consent(a, candidate, proposal_sha256=ledger.proposal_digest(proposal),
                       content_sha256="c" * 64)
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], candidate_sha=candidate,
                    release_mode="git", proposal=proposal, consent=consent)
    assert ledger.get_release(engine, SLUG, "1.1.0")["consent"]["content_sha256"] == "c" * 64


# ═════════════════════════════════════════════════════════════════════════════
# Review findings of step 2 (regressions)
# ═════════════════════════════════════════════════════════════════════════════


def test_a_change_too_large_for_one_preview_refuses_before_the_sync(engine, rel, run, tmp_path):
    """The slot holds every path of Pₛ several times: a change that cannot
    fit PREVIEW_MAX_BYTES is refused with its own key BEFORE the script
    runs — never a bare "too long" after the sync."""
    repo, log = rel
    wip = add_entry(engine, SLUG, "changed", "rename the package")["id"]
    for n in range(2500):
        write(repo, f"pkg/some_module_directory/file_{n:04d}.py", f"{n}\n")
    before = slot(engine)
    doc = refused(run, repo, "--message-file",
                  message_file(tmp_path, f"refactor: rename\n\nChangelog: {wip}\n"),
                  *[arg for n in range(2500) for arg in (
                      "--include-untracked", f"pkg/some_module_directory/file_{n:04d}.py")],
                  key="preview_too_large")
    big = doc["details"]["preview_too_large"]
    assert big["paths"] == 2500 and big["max"] == ledger.PREVIEW_MAX_BYTES
    assert big["bytes"] > big["max"]
    assert sync_runs(log) == []  # refused before the sync
    assert slot(engine) == before


def test_include_untracked_outside_the_repository_is_a_bad_untracked_path(
        engine, rel, run, tmp_path):
    repo, _ = rel
    wip = add_entry(engine, SLUG, "added", "x")["id"]
    write(repo, "x.txt", "x\n")
    (tmp_path / "outside.txt").write_text("not ours\n")
    doc = refused(run, repo, "--message-file",
                  message_file(tmp_path, f"feat: x\n\nChangelog: {wip}\n"),
                  "--include-untracked", "x.txt", "--include-untracked", "../outside.txt",
                  key="bad_untracked")
    assert doc["details"]["bad_untracked"] == [{"path": "../outside.txt", "reason": "outside"}]


def test_include_untracked_is_relative_to_the_current_directory(engine, rel, run, tmp_path):
    """From a subdirectory a path names a file the way `git status` shows it
    there; an absolute path works too."""
    repo, _ = rel
    wip = add_entry(engine, SLUG, "added", "n")["id"]
    write(repo, "sub/n.txt", "n\n")
    write(repo, "sub/m.txt", "m\n")
    msg = message_file(tmp_path, f"feat: n\n\nChangelog: {wip}\n")
    doc = preview(run, repo / "sub", "--message-file", msg, "--include-untracked", "n.txt",
                  "--include-untracked", str(repo / "sub" / "m.txt"))
    assert doc["details"]["work"]["untracked_included"] == ["sub/m.txt", "sub/n.txt"]


def test_the_range_text_never_shows_a_synthetic_commit(engine, rel, run, tmp_path):
    repo, _ = rel
    wip = add_entry(engine, SLUG, "added", "work")["id"]
    write(repo, "w.txt", "w\n")
    doc = preview(run, repo, "--message-file",
                  message_file(tmp_path, f"feat: work\n\nChangelog: {wip}\n"),
                  "--include-untracked", "w.txt")
    text = variant(doc)["display"]["range"]["text"]
    synthetic = [sha for sha in (variant(doc)["consent"]["entries"]["proof"][str(wip)]
                                 ["commits"]) if sha != "@work"]
    assert synthetic == []
    assert text.endswith("HEAD + your uncommitted changes"), text
    assert head(repo) not in text  # the base is the tag, the end what the release adds


def test_a_broken_upstream_is_not_reported_as_no_upstream(engine, rel, run):
    """The branch HAS an upstream, but its remote does not exist: the local
    release's reason says so (upstream_invalid), never `no_upstream`."""
    repo, _ = rel
    proven(engine, repo, "feature a")
    git(repo, "config", "branch.main.remote", "gone")
    doc = preview(run, repo)
    assert variant(doc)["consent"]["push"] == {"mode": "local", "reason": "upstream_invalid"}
    git(repo, "config", "--unset", "branch.main.remote")
    git(repo, "config", "--unset", "branch.main.merge")
    doc = preview(run, repo)
    assert variant(doc)["consent"]["push"] == {"mode": "local", "reason": "no_upstream"}


def test_head_that_moved_before_the_lock_refuses_the_preview(engine, rel, run, monkeypatch):
    """H0 is the HEAD _locate read BEFORE the operation lock; a commit in
    between is refused (checkout_changed), never built on a stale H0."""
    repo, _ = rel
    proven(engine, repo, "feature a")
    original = effects._locate

    def late_commit(*args, **kwargs):
        located = original(*args, **kwargs)
        commit(repo, "chore: someone else\n", "late.txt")
        return located

    monkeypatch.setattr(effects, "_locate", late_commit)
    before = slot(engine)
    doc = refused(run, repo, key="checkout_changed")
    assert doc["details"]["checkout_changed"]["head"]["after"] == head(repo)
    assert slot(engine) == before


# ═════════════════════════════════════════════════════════════════════════════
# The plan reads the slot's progress read-only — a progress commit that no
# longer resolves is reported, never raised (safety review G6)
# ═════════════════════════════════════════════════════════════════════════════


def test_g6_a_recorded_work_commit_that_no_longer_exists_does_not_break_the_plan(
        engine, mixed, run, tmp_path):
    """G6: the slot records a work commit (made under consent), which was
    then dropped (never referenced, pruned by gc). `build_plan` is
    read-only: it must not raise; the commit is not adoptable, no digest
    is exposed and a warning names the missing commit."""
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = preview(run, repo, "--message-file", mixed["msg"], "--include-untracked", "new.txt",
                  "--no-variants")
    work = doc["details"]["work"]
    msg = message_file(tmp_path, work["message"], "w-msg.txt")
    w = git(repo, "commit-tree", work["tree"], "-p", h0, "-F", msg)
    ledger.record_preview_progress(engine, SLUG, doc["details"]["preview_id"], work_commit=w)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", w], capture_output=True,
                          check=False).returncode != 0  # precondition: W is gone
    plan = build_plan(engine, SLUG, cwd=str(repo))
    pending = plan["pending_consent"]
    assert pending is not None and pending["progress"]["work_commit"] == w
    assert pending["consent_digest"] is None
    assert pending["adoptable"] is False
    assert any(w[:7] in warning for warning in plan["warnings"]), plan["warnings"]


def test_g6_a_recorded_attest_commit_that_no_longer_exists_does_not_break_the_plan(
        engine, rel, run):
    """G6, the attestation side: a recorded A that no longer resolves."""
    repo, _ = rel
    proven(engine, repo, "feature a")
    add_entry(engine, SLUG, "fixed", "waiting b")
    h0 = head(repo)
    doc = preview(run, repo)
    a = subprocess.run(["git", "-C", str(repo), "commit-tree", tree_of(repo, h0), "-p", h0],
                       input=f"{ATTEST_SUBJECT}\n", text=True, capture_output=True,
                       check=True).stdout.strip()
    ledger.record_preview_progress(engine, SLUG, doc["details"]["preview_id"], attest_commit=a)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", a], capture_output=True,
                          check=False).returncode != 0  # precondition: A is gone
    plan = build_plan(engine, SLUG, cwd=str(repo))
    pending = plan["pending_consent"]
    assert pending["consent_digest"] is None and pending["adoptable"] is False
    assert any(a[:7] in warning for warning in plan["warnings"]), plan["warnings"]
