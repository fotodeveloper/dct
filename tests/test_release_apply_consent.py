"""`dct release apply --consent C --confirm` (the one-dialog release, phase 2
step 3) — driven through the REAL CLI on throwaway git repositories with a
bare push remote, after a real `dct release prepare --preview`.

What `apply --consent` must guarantee (contract sections A2, E, F):

- it releases EXACTLY the content the preview bound into C: the view it
  derives at the real candidate K equals the view the screen was rendered
  from, and the record's consent carries `content_sha256 == C`;
- a dirty tree is committed by dct itself (the work-in-progress commit W,
  parent H0, tree T1, the approved message) through the repository's hook
  lifecycle on a TEMPORARY index; a hook that changes the content or the
  proven ids refuses BEFORE anything is published, a rejecting hook stops;
- the "with all N" variant makes the empty attestation commit A (parent W
  or H0) proving exactly the approved ids, then releases without a second
  screen;
- every refusal before a commit leaves the world untouched — no stamp, no
  record, no published commit, the user's index and work tree
  byte-identical — and every refusal carries `details.made`;
- interruptions are resumed by running the SAME `apply --consent C` again:
  W / A are adopted (never made twice) and the index sync runs again.

Interruption points are injected by wrapping effects._point (the one
function every point goes through); the action fires ONCE, so the re-run is
not interrupted again. The clock is a dependency: tests that move the date
patch the `datetime` the effects layer or the stamp transaction reads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import git, run_cli
from test_release_effects import (
    HISTORY,
    SLUG,
    assert_released,
    commit,
    make_release_repo,
    proven,
    record_date,
    released_section,
    sync_runs,
)
from test_release_preview import (
    TWO_FILE_SYNC,
    blob_at,
    canonical_digest,
    commit_all,
    hash_bytes,
    head,
    message_file,
    preview_refs,
    ref_exists,
    refs,
    sha256,
    slot,
    tree_of,
    work_files,
    write,
)

from dct.core import releases as ledger
from dct.core.changelog import add_entry, list_entries, release_entries, update_entry
from dct.core.ids import new_ulid
from dct.core.projects import (
    create_project,
    get_project,
    set_changelog_pr_refs,
    set_manages_changelog,
    set_release_executor,
)
from dct.core.releases import get_pending_proposal, get_release, set_version_files
from dct.db import releases
from dct.workflows.release import build_plan, effects
from dct.workflows.release.effects import (
    ATTEST_SUBJECT,
    prepare_release,
    resume_release,
)
from dct.workflows.release.plan import adoptable_chain, read_commits
from dct.workflows.release.proof import proven_ids

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]

NO_MADE = {"work_commit": None, "attest_commit": None}
NEW_POINTS = ("before_work_commit", "after_work_commit", "after_work_index",
              "after_attest_commit")


class Interrupted(Exception):
    """Stands for a crash / kill at an interruption point."""


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """dct makes real commits (W, A, the release commit) in these tests."""


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def rel(engine, tmp_path):
    """proj-r (test_release_effects.make_release_repo): changelog history,
    pyproject 1.0.0, a sync script logging each run, tagged v1.0.0, `main`
    tracking the bare remote tmp_path/remote.git, contract
    ["pyproject.toml"]."""
    repo, log = make_release_repo(engine, tmp_path)
    return {"repo": repo, "log": log, "bare": tmp_path / "remote.git"}


@pytest.fixture
def dirty(engine, rel, tmp_path):
    """One unstaged modification (b.txt) and the entry its message proves —
    no other unreleased entry, so only the shown variant exists."""
    repo = rel["repo"]
    commit_all(repo, {"a.txt": "a0\n", "b.txt": "b0\n"})
    wip = add_entry(engine, SLUG, "added", "work in progress")["id"]
    h0 = head(repo)
    write(repo, "b.txt", "b1\n")
    msg = message_file(tmp_path, f"feat: work in progress\n\nChangelog: {wip}\n")
    return {**rel, "h0": h0, "wip": wip, "msg": msg}


@pytest.fixture
def mixed(engine, rel, tmp_path):
    """Every kind of change (as test_release_preview's fixture): a fully
    staged (a.txt), an unstaged (b.txt) and a partially staged (c.txt:
    index c1, work tree c2) modification, an unstaged deletion (d.txt), a
    mode change (run.sh), an untracked release artifact (VERSION, in the
    contract), an untracked file the owner includes (new.txt) and one left
    out (notes.local). `other` is an unreleased entry the message does not
    name (it waits → the "all" variant attests it)."""
    repo, log = rel["repo"], rel["log"]
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
    msg = message_file(tmp_path, f"feat: work in progress\n\nChangelog: {wip}\n")
    return {**rel, "h0": h0, "wip": wip, "other": other, "msg": msg,
            "argv": ("--message-file", msg, "--include-untracked", "new.txt")}


MIXED_PS = ["VERSION", "a.txt", "b.txt", "c.txt", "d.txt", "new.txt", "run.sh"]


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
    return {"root": root, "log": log}


@pytest.fixture
def cli(engine, monkeypatch, capsys):
    """`dct release <sub> -P proj-r <argv> --json` in `cwd` → (exit code,
    JSON document or None, stderr). Exit 2 = argparse refused the argv."""
    from dct import cli as cli_module

    monkeypatch.setattr(cli_module, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli_module, "_get_readonly_engine", lambda: engine)

    def invoke(cwd, *argv: str, group: str = "release") -> tuple[int, dict | None, str]:
        monkeypatch.chdir(cwd)
        capsys.readouterr()
        code = 0
        sub, rest = argv[0], argv[1:]
        try:
            run_cli(monkeypatch, [group, sub, "-P", SLUG, *map(str, rest), "--json"])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        out = capsys.readouterr()
        if code == 2:
            return code, None, out.err
        return code, json.loads(out.out), out.err

    return invoke


@pytest.fixture
def at_point(monkeypatch):
    """{point: action} — `action()` runs ONCE when the workflow reaches
    `point` (after effects' own check that the point is declared)."""
    actions: dict = {}
    original = effects._point

    def patched(on_point, name):
        original(on_point, name)
        action = actions.pop(name, None)
        if action is not None:
            action()

    monkeypatch.setattr(effects, "_point", patched)
    return actions


def interrupt(point: str):
    def action():
        raise Interrupted(point)
    return action


class Tomorrow(datetime):
    """A clock one day ahead (UTC) — the date moved past midnight."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(days=1)


# ── helpers ──────────────────────────────────────────────────────────────────


def do_preview(cli, cwd, *argv) -> dict:
    code, doc, err = cli(cwd, "prepare", "--preview", *argv)
    assert code == 0, f"the preview was refused ({code}): {doc and doc['details']} {err}"
    assert doc["outcome"] == "preview"
    return doc


def variant(doc: dict, name: str = "shown") -> dict:
    [found] = [v for v in doc["details"]["variants"] if v["name"] == name]
    return found


def apply_consent(cli, cwd, consent: str, *extra, confirm: bool = True):
    argv = ["apply", "--consent", consent, *(["--confirm"] if confirm else []), *extra]
    code, doc, err = cli(cwd, *argv)
    if code == 2:
        pytest.fail(f"the CLI does not accept `dct release apply --consent`: {err.strip()}")
    return code, doc


def applied(cli, cwd, consent: str) -> dict:
    code, doc = apply_consent(cli, cwd, consent)
    assert code == 0, f"apply --consent was refused ({code}): {doc['details']}"
    assert doc["action"] == "apply" and doc["outcome"] == "completed", doc
    json.dumps(doc)
    return doc


def refused_apply(cli, cwd, consent: str, *extra, key: str | None, confirm: bool = True) -> dict:
    code, doc = apply_consent(cli, cwd, consent, *extra, confirm=confirm)
    assert code == 1, f"expected a refusal (exit 1), got {code}: {doc}"
    assert doc["outcome"] == "refused", doc
    if key is not None:
        assert key in doc["details"], (key, sorted(doc["details"]), doc["details"].get("error"))
    assert "made" in doc["details"], "every refusal of apply --consent carries details.made"
    return doc


def step(doc: dict, name: str) -> dict:
    found = [s for s in doc["steps"] if s["step"] == name]
    assert len(found) == 1, (name, doc["steps"])
    return found[0]


def remote_refs(bare: Path) -> list[str]:
    return git(bare, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()


def record_count(engine) -> int:
    with engine.begin() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(releases)).scalar()


def world(engine, repo: Path, bare: Path) -> dict:
    """Everything a refusal must leave as it was. The raw index bytes are
    read FIRST — no git command refreshes them before."""
    index = (repo / ".git" / "index").read_bytes()
    return {
        "index": index,
        "files": work_files(repo),
        "refs": refs(repo),
        "commits": git(repo, "rev-list", "--all", "--count"),
        "remote": remote_refs(bare),
        "records": record_count(engine),
        "entries": {e["id"]: e["version"] for e in list_entries(engine, SLUG)},
        "current_version": get_project(engine, SLUG)["current_version"],
    }


def assert_untouched(engine, before: dict, repo: Path, bare: Path, *, slot_before=None) -> None:
    after = world(engine, repo, bare)
    assert after["index"] == before["index"], "the user's index was written"
    assert after["files"] == before["files"], "the work tree was changed"
    assert after["refs"] == before["refs"], "a ref moved (a commit was published)"
    assert after["commits"] == before["commits"]
    assert after["remote"] == before["remote"], "something was pushed"
    assert after["records"] == before["records"], "a release record was written"
    assert after["entries"] == before["entries"], "an entry was stamped"
    assert after["current_version"] == before["current_version"]
    if slot_before is not None:
        assert slot(engine) == slot_before, "the refused apply changed the preview slot"


def commit_message(repo, commit: str) -> str:
    body = subprocess.run(["git", "-C", str(repo), "cat-file", "commit", commit],
                          capture_output=True, check=True).stdout.decode()
    return body.split("\n\n", 1)[1]


def parents(repo, commit: str) -> list[str]:
    return git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]


def subject(repo, commit: str) -> str:
    return git(repo, "log", "-1", "--format=%s", commit)


def proves(repo, commit: str) -> list[int]:
    [parsed] = read_commits(str(repo), "-1", commit)
    return sorted(proven_ids(parsed))


def pending_consent(engine, repo) -> dict | None:
    return build_plan(engine, SLUG, cwd=str(repo))["pending_consent"]


def hooks_dir(repo: Path) -> Path:
    return repo / ".git" / "hooks"


def install_hook(repo: Path, name: str, body: str) -> Path:
    """An executable bash hook (os.open with mode 0o755 — no shell chmod)."""
    directory = hooks_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/bash\n" + body)
    return path


def index_entries(repo, paths) -> dict[str, str]:
    """{path: 'mode blob'} of the real index for `paths` (absent → missing)."""
    out = {}
    for line in git(repo, "ls-files", "-s", "--", *paths).splitlines():
        meta, _, path = line.partition("\t")
        mode, blob, _stage = meta.split()
        out[path] = f"{mode} {blob}"
    return out


def tree_entries(repo, commit: str, paths) -> dict[str, str]:
    out = {}
    for line in git(repo, "ls-tree", commit, "--", *paths).splitlines():
        meta, _, path = line.partition("\t")
        mode, _kind, blob = meta.split()
        out[path] = f"{mode} {blob}"
    return out


def assert_completed(engine, repo, bare, *, candidate: str, ids: list[int], version: str):
    """The verified end state of a release (any contract, any section)."""
    record = get_release(engine, SLUG, version)
    assert record["status"] == "completed"
    assert record["entry_ids"] == sorted(ids)
    assert record["candidate_sha"] == candidate
    release_commit = head(repo)
    assert parents(repo, release_commit) == [candidate]
    assert subject(repo, release_commit) == f"release: v{version}"
    assert git(repo, "rev-parse", f"v{version}") == release_commit
    assert f"refs/heads/main {release_commit}" in remote_refs(bare)
    assert f"refs/tags/v{version} {release_commit}" in remote_refs(bare)
    stamped = {e["id"]: e["version"] for e in list_entries(engine, SLUG) if e["id"] in ids}
    assert stamped == {i: version for i in ids}
    assert get_project(engine, SLUG)["current_version"] == version
    return record


def alpha(commit: str, made: dict) -> str:
    """α of the contract: the real W / A become @work / @attest."""
    if made.get("work_commit") and commit == made["work_commit"]:
        return "@work"
    if made.get("attest_commit") and commit == made["attest_commit"]:
        return "@attest"
    return commit


# ═════════════════════════════════════════════════════════════════════════════
# Contract constants (G)
# ═════════════════════════════════════════════════════════════════════════════


def test_the_new_effect_step_and_interruption_points_are_declared():
    assert "work_commit" in effects.EFFECT_STEPS and "attest" in effects.EFFECT_STEPS
    assert set(NEW_POINTS) <= set(effects.INTERRUPTION_POINTS)


def test_the_clog_release_alias_does_not_offer_consent(engine, rel, cli):
    """A2: `apply --consent` is not offered on the `clog release` alias."""
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    c = variant(do_preview(cli, repo))["consent_digest"]
    before = world(engine, repo, rel["bare"])
    code, _doc, err = cli(repo, "release", "--consent", c, "--confirm", group="clog")
    assert code == 2 and "--consent" in err
    assert_untouched(engine, before, repo, rel["bare"])


# ═════════════════════════════════════════════════════════════════════════════
# 16. The clean happy path
# ═════════════════════════════════════════════════════════════════════════════


def test_t16_clean_tree_releases_the_bound_content(engine, rel, cli):
    repo, log = rel["repo"], rel["log"]
    a = proven(engine, repo, "feature a")
    h0 = head(repo)
    doc = do_preview(cli, repo)
    shown = variant(doc)
    c, preview_id = shown["consent_digest"], doc["details"]["preview_id"]
    pin = slot(engine)["pin"]

    out = applied(cli, repo, c)
    record = assert_released(engine, repo, h0, [a])
    assert record["consent"]["content_sha256"] == c
    assert record["candidate_sha"] == h0  # nothing to commit, nothing attested
    # P is the digest of the REBOUND proposal: the stored content + the
    # preview id, the real candidate and its plan digest.
    proposal = record["proposal"]
    assert proposal["id"] == preview_id
    assert proposal["candidate_sha"] == h0
    assert record["consent"]["proposal_sha256"] == ledger.proposal_digest(proposal)
    assert proposal["expected_blobs"] == shown["consent"]["version_files"]["expected_blobs"]

    details = out["details"]
    assert details["consent_digest"] == c and details["variant"] == "shown"
    assert details["made"] == NO_MADE
    assert details["candidate_sha"] == h0
    assert details["proposal_sha256"] == record["consent"]["proposal_sha256"]
    assert len(details["plan_digest"]) == 64
    assert not [s for s in out["steps"] if s["step"] in ("work_commit", "attest")]
    # The script ran ONCE, in the preview — apply never runs it.
    assert sync_runs(log) == ["1.1.0"]
    # The slot is consumed and its pin removed.
    assert slot(engine) is None
    assert not ref_exists(repo, pin) and preview_refs(repo) == []


# ═════════════════════════════════════════════════════════════════════════════
# 17. The dirty happy path: dct makes W through the hooks
# ═════════════════════════════════════════════════════════════════════════════


def test_t17_dirty_tree_work_commit_is_exactly_the_bound_tree_and_message(engine, mixed, cli):
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = do_preview(cli, repo, *mixed["argv"], "--no-variants")
    work = doc["details"]["work"]
    c = variant(doc)["consent_digest"]

    out = applied(cli, repo, c)
    w = out["details"]["made"]["work_commit"]
    assert w and out["details"]["made"]["attest_commit"] is None
    assert step(out, "work_commit") == {"step": "work_commit", "result": "done", "commit": w}
    assert parents(repo, w) == [h0]
    assert tree_of(repo, w) == work["tree"]
    assert subject(repo, w) == "feat: work in progress"
    assert commit_message(repo, w) == work["message"]
    assert proves(repo, w) == [mixed["wip"]]
    # The whole file is committed, also its unstaged part; the mode change;
    # the deletion; the included untracked file and the release artifact.
    assert blob_at(repo, w, "c.txt") == hash_bytes(repo, b"c2\n")
    assert git(repo, "ls-tree", w, "--", "run.sh").split()[0] == "100755"
    assert git(repo, "ls-tree", w, "--", "d.txt") == ""
    assert blob_at(repo, w, "new.txt") == hash_bytes(repo, b"new\n")

    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed" and record["candidate_sha"] == w
    assert record["entry_ids"] == [mixed["wip"]]
    assert record["consent"]["content_sha256"] == c
    release_commit = head(repo)
    assert parents(repo, release_commit) == [w]
    # The real index equals the commits for Pₛ (the partially staged c.txt
    # included) — the only thing left is the untracked file that was left out.
    assert index_entries(repo, MIXED_PS) == tree_entries(repo, release_commit, MIXED_PS)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? notes.local"
    assert (repo / "notes.local").read_text() == "scratch\n"
    assert "notes.local" not in git(repo, "log", "--all", "--format=", "--name-only").split()


def test_t17_the_work_commit_runs_the_repositorys_hook_lifecycle(engine, dirty, cli, tmp_path):
    repo = dirty["repo"]
    trace = tmp_path / "hooks.log"
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-push"):
        install_hook(repo, name, f'echo "{name}" >> "{trace}"\n')
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    out = applied(cli, repo, c)
    w = out["details"]["made"]["work_commit"]
    once = ["pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"]
    assert trace.read_text().split() == once + once + ["pre-push"]  # W, release, push
    assert_released(engine, repo, w, [dirty["wip"]], section=released_section(
        record_date(engine), lines="- work in progress\n"))


def test_t17_a_hook_adding_a_body_line_is_accepted(engine, dirty, cli):
    """E3: lines a hook adds that change neither the subject nor the proven
    ids may differ between the preview and the real W."""
    repo = dirty["repo"]
    # As `git commit -s` adds it: a trailer (a one-line message — the release
    # commit's — gets it in a NEW paragraph, so no subject changes).
    install_hook(repo, "commit-msg",
                 "git interpret-trailers --in-place --trailer 'Signed-off-by: t <t@t>' \"$1\"\n")
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    out = applied(cli, repo, c)
    w = out["details"]["made"]["work_commit"]
    assert "Signed-off-by: t <t@t>" in commit_message(repo, w)
    assert proves(repo, w) == [dirty["wip"]]


def test_t17_a_two_line_first_paragraph_is_one_subject_as_git_reads_it(
        engine, dirty, cli, tmp_path):
    """Git's subject (%s) is the whole first paragraph joined with spaces;
    the bound subject, the R8 check and adoption must use the same rule."""
    repo = dirty["repo"]
    msg = message_file(tmp_path, f"feat: first line\ncontinued line\n\nChangelog: "
                                 f"{dirty['wip']}\n", "two-line.txt")
    doc = do_preview(cli, repo, "--message-file", msg)
    assert variant(doc)["consent"]["work"]["subject"] == "feat: first line continued line"
    out = applied(cli, repo, variant(doc)["consent_digest"])
    w = out["details"]["made"]["work_commit"]
    assert subject(repo, w) == "feat: first line continued line"


# ═════════════════════════════════════════════════════════════════════════════
# 18. The "with all N" variant
# ═════════════════════════════════════════════════════════════════════════════


def test_t18_all_variant_on_a_clean_tree_attests_on_h0(engine, rel, cli):
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    w = add_entry(engine, SLUG, "added", "waiting b")["id"]
    h0 = head(repo)
    doc = do_preview(cli, repo)
    every = variant(doc, "all")
    out = applied(cli, repo, every["consent_digest"])
    made = out["details"]["made"]
    attest_commit = made["attest_commit"]
    assert made["work_commit"] is None and attest_commit
    assert out["details"]["variant"] == "all"
    assert step(out, "attest") == {"step": "attest", "result": "done", "commit": attest_commit}
    assert parents(repo, attest_commit) == [h0]
    assert tree_of(repo, attest_commit) == tree_of(repo, h0)  # empty
    assert subject(repo, attest_commit) == ATTEST_SUBJECT
    assert proves(repo, attest_commit) == [w]  # exactly attest.ids (K-107)
    record = assert_completed(engine, repo, bare, candidate=attest_commit, ids=[a, w],
                              version="1.1.0")
    assert record["consent"]["content_sha256"] == every["consent_digest"]


def test_t18_all_variant_on_a_dirty_tree_attests_on_the_work_commit(engine, mixed, cli):
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = do_preview(cli, repo, *mixed["argv"])
    every = variant(doc, "all")
    assert every["consent"]["attest"] == {"ids": [mixed["other"]]}
    out = applied(cli, repo, every["consent_digest"])
    made = out["details"]["made"]
    w, a = made["work_commit"], made["attest_commit"]
    assert parents(repo, w) == [h0] and parents(repo, a) == [w]
    assert tree_of(repo, a) == tree_of(repo, w) == doc["details"]["work"]["tree"]
    assert proves(repo, w) == [mixed["wip"]]  # the WIP message is never extended
    assert proves(repo, a) == [mixed["other"]]
    assert subject(repo, a) == ATTEST_SUBJECT
    assert [s["step"] for s in out["steps"]].index("work_commit") < \
        [s["step"] for s in out["steps"]].index("attest")
    assert_completed(engine, repo, mixed["bare"], candidate=a,
                     ids=[mixed["wip"], mixed["other"]], version="1.1.0")


def test_t18_all_variant_with_a_different_version_releases_that_version(engine, rel, cli):
    """K-c (a): the variant's own V (a fix shown → patch; an added waiting
    entry → minor) is what it releases; the script ran only in the
    preview."""
    repo, log, bare = rel["repo"], rel["log"], rel["bare"]
    f = proven(engine, repo, "small fix", category="fixed")
    w = add_entry(engine, SLUG, "added", "big feature")["id"]
    doc = do_preview(cli, repo)
    assert variant(doc)["consent"]["version"]["target"] == "1.0.1"
    every = variant(doc, "all")
    assert every["consent"]["version"]["target"] == "1.1.0"
    out = applied(cli, repo, every["consent_digest"])
    assert out["version"] == "1.1.0"
    assert_completed(engine, repo, bare, candidate=out["details"]["made"]["attest_commit"],
                     ids=[f, w], version="1.1.0")
    assert get_release(engine, SLUG, "1.0.1") is None
    assert git(repo, "tag", "--list", "v1.0.1") == ""
    assert sync_runs(log) == ["1.0.1", "1.1.0"]  # preview only
    assert 'version = "1.1.0"' in (repo / "pyproject.toml").read_text()


# ═════════════════════════════════════════════════════════════════════════════
# Nothing proven, own executor
# ═════════════════════════════════════════════════════════════════════════════


def test_nothing_proven_shown_variant_is_a_version_only_release(engine, rel, cli):
    repo, bare = rel["repo"], rel["bare"]
    x = add_entry(engine, SLUG, "fixed", "waiting x")["id"]
    y = add_entry(engine, SLUG, "added", "waiting y")["id"]
    h0 = head(repo)
    doc = do_preview(cli, repo)
    shown = variant(doc)
    assert shown["version_only"] is True and shown["consent"]["entries"]["selected"] == []
    out = applied(cli, repo, shown["consent_digest"])
    assert out["details"]["mode"] == "version_only"
    version = shown["consent"]["version"]["target"]
    assert_completed(engine, repo, bare, candidate=h0, ids=[], version=version)
    unreleased = {e["id"] for e in list_entries(engine, SLUG) if e["version"] is None}
    assert {x, y} <= unreleased  # waiting entries are never stamped


def test_nothing_proven_all_variant_attests_every_waiting_entry(engine, rel, cli):
    repo, bare = rel["repo"], rel["bare"]
    x = add_entry(engine, SLUG, "fixed", "waiting x")["id"]
    y = add_entry(engine, SLUG, "added", "waiting y")["id"]
    every = variant(do_preview(cli, repo), "all")
    out = applied(cli, repo, every["consent_digest"])
    a = out["details"]["made"]["attest_commit"]
    assert proves(repo, a) == sorted([x, y])
    assert_completed(engine, repo, bare, candidate=a, ids=[x, y], version="1.1.0")


def test_nothing_proven_with_more_than_four_waiting_attests_only_the_ticked(engine, rel, cli):
    repo, bare = rel["repo"], rel["bare"]
    ids = [add_entry(engine, SLUG, "fixed", f"waiting {n}")["id"] for n in range(5)]
    ticked = ids[:2]
    assert do_preview(cli, repo)["details"]["all_unavailable"] == "too_many"  # → multiSelect
    doc = do_preview(cli, repo, "--attest", ",".join(map(str, ticked)), "--no-variants")
    assert variant(doc)["consent"]["attest"] == {"ids": ticked}
    out = applied(cli, repo, variant(doc)["consent_digest"])
    a = out["details"]["made"]["attest_commit"]
    assert proves(repo, a) == ticked
    assert_completed(engine, repo, bare, candidate=a, ids=ticked, version="1.0.1")
    unreleased = {e["id"] for e in list_entries(engine, SLUG) if e["version"] is None}
    assert set(ids[2:]) <= unreleased


@pytest.mark.parametrize("change,key", [
    (lambda engine, a: set_release_executor(engine, SLUG, "own"), "executor"),
    (lambda engine, a: release_entries(engine, SLUG, "1.1.0", entry_ids=[a]), "unfinished"),
])
def test_executor_own_and_an_unfinished_record_refuse_with_nothing_done(
        engine, dirty, cli, change, key):
    repo, bare = dirty["repo"], dirty["bare"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    kept = slot(engine)
    change(engine, dirty["wip"])
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=key)
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare, slot_before=kept)


# ═════════════════════════════════════════════════════════════════════════════
# 19. Adoption: a re-run of the same apply --consent never makes W / A twice
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("point", ["after_work_commit", "after_work_index"])
def test_t19_an_interrupted_work_commit_is_adopted_by_the_rerun(
        engine, mixed, cli, at_point, point):
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = do_preview(cli, repo, *mixed["argv"], "--no-variants")
    c = variant(doc)["consent_digest"]
    at_point[point] = interrupt(point)
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    assert parents(repo, w) == [h0] and tree_of(repo, w) == doc["details"]["work"]["tree"]
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": None,
                                        "consent": c}
    assert get_release(engine, SLUG) is None
    pending = pending_consent(engine, repo)
    assert pending["consent_digest"] == c and pending["adoptable"] is True
    assert pending["progress"] == {"work_commit": w, "attest_commit": None}

    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}
    assert out["details"]["made"]["work_commit"] == w
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed" and record["candidate_sha"] == w
    assert git(repo, "rev-list", "--count", f"{h0}..{w}") == "1"  # no second W
    assert index_entries(repo, MIXED_PS) == tree_entries(repo, head(repo), MIXED_PS)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? notes.local"


def test_t19_interrupted_before_the_work_commit_makes_it_on_the_rerun(
        engine, dirty, cli, at_point):
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    kept = slot(engine)
    before = world(engine, repo, bare)
    at_point["before_work_commit"] = interrupt("before_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    assert_untouched(engine, before, repo, bare, slot_before=kept)
    assert pending_consent(engine, repo)["consent_digest"] is None  # no progress, a draft
    out = applied(cli, repo, c)
    assert step(out, "work_commit")["result"] == "done"
    assert parents(repo, out["details"]["made"]["work_commit"]) == [h0]


@pytest.mark.parametrize("fixture", ["clean", "dirty"])
def test_t19_an_interrupted_attestation_is_adopted_by_the_rerun(
        engine, rel, cli, at_point, tmp_path, fixture):
    repo = rel["repo"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    ids = [a, waiting]
    argv: tuple = ()
    if fixture == "dirty":
        more = add_entry(engine, SLUG, "added", "more work")["id"]
        ids.append(more)
        write(repo, "f.txt", "more work\n")
        argv = ("--message-file",
                message_file(tmp_path, f"feat: more work\n\nChangelog: {more}\n"))
    h0 = head(repo)
    c = variant(do_preview(cli, repo, *argv), "all")["consent_digest"]
    at_point["after_attest_commit"] = interrupt("after_attest_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    attest_commit = head(repo)
    assert subject(repo, attest_commit) == ATTEST_SUBJECT
    progress = slot(engine)["progress"]
    assert progress["attest_commit"] == attest_commit
    assert pending_consent(engine, repo)["adoptable"] is True

    out = applied(cli, repo, c)
    assert step(out, "attest") == {"step": "attest", "result": "adopted",
                                   "commit": attest_commit}
    if fixture == "dirty":
        assert step(out, "work_commit")["result"] == "adopted"
        assert parents(repo, attest_commit) == [progress["work_commit"]]
    else:
        assert parents(repo, attest_commit) == [h0]
    expected_commits = "2" if fixture == "dirty" else "1"
    assert git(repo, "rev-list", "--count", f"{h0}..{attest_commit}") == expected_commits
    assert get_release(engine, SLUG, "1.1.0")["entry_ids"] == sorted(ids)


def test_t19_a_work_commit_both_variants_share_exposes_the_applied_digest(
        engine, mixed, cli, at_point):
    """G2: W is the same for "Release" and "Release with all N", so the
    commits alone cannot tell which screen was approved — the slot records
    the applied C together with W, and pending_consent exposes THAT digest
    (the dialog offers "finish the release you approved", whose re-run
    syncs the index); the same apply adopts W and finishes the variant."""
    repo = mixed["repo"]
    doc = do_preview(cli, repo, *mixed["argv"])
    every = variant(doc, "all")["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, every)
    pending = pending_consent(engine, repo)
    assert pending["progress"]["work_commit"] == head(repo)
    assert pending["adoptable"] is True
    assert pending["consent_digest"] == every, \
        "progress that holds only the shared W must still name the C that was applied"
    out = applied(cli, repo, every)
    assert step(out, "work_commit")["result"] == "adopted"
    assert step(out, "attest")["result"] == "done"
    assert get_release(engine, SLUG, "1.1.0")["entry_ids"] == sorted(
        [mixed["wip"], mixed["other"]])


def test_t19_a_work_commit_made_by_plain_git_commit_is_adopted(engine, mixed, cli):
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = do_preview(cli, repo, *mixed["argv"], "--no-variants")
    c = variant(doc)["consent_digest"]
    git(repo, "add", "-A", "--", *MIXED_PS)
    git(repo, "commit", "-q", "-F", mixed["msg"])
    plain_w = head(repo)
    assert tree_of(repo, plain_w) == doc["details"]["work"]["tree"]  # precondition
    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted",
                                        "commit": plain_w}
    record = get_release(engine, SLUG, "1.1.0")
    assert record["candidate_sha"] == plain_w and parents(repo, plain_w) == [h0]


@pytest.mark.parametrize("message", ["chore: another subject\n\nChangelog: {wip}\n",
                                     "feat: work in progress\n"])
def test_t19_a_commit_of_the_same_tree_with_another_message_is_not_adopted(
        engine, mixed, cli, tmp_path, message):
    """§E2 step 2: adoption needs the subject AND the proven ids — the tree
    alone is not the approved commit."""
    repo, bare = mixed["repo"], mixed["bare"]
    c = variant(do_preview(cli, repo, *mixed["argv"], "--no-variants"))["consent_digest"]
    git(repo, "add", "-A", "--", *MIXED_PS)
    git(repo, "commit", "-q", "-F",
        message_file(tmp_path, message.format(wip=mixed["wip"]), "other-msg.txt"))
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["expected_parent"] == mixed["h0"]
    assert doc["details"]["base_moved"]["head"] == head(repo)
    assert_untouched(engine, before, repo, bare, slot_before=kept)


# ═════════════════════════════════════════════════════════════════════════════
# 20. The stuck index: foreign staging during the sync
# ═════════════════════════════════════════════════════════════════════════════


def test_t20_foreign_staging_during_the_sync_refuses_then_a_rerun_completes(
        engine, dirty, cli, at_point, tmp_path):
    repo, h0 = dirty["repo"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    foreign = hash_bytes(repo, b"someone else's b\n", write=True)
    at_point["after_work_commit"] = lambda: git(
        repo, "update-index", "--cacheinfo", f"100644,{foreign},b.txt")
    doc = refused_apply(cli, repo, c, key="foreign_staged")
    w = doc["details"]["made"]["work_commit"]
    assert w == head(repo) and parents(repo, w) == [h0]
    assert "commit made, release not started" in doc["details"]["error"]
    assert git(repo, "ls-files", "-s", "--", "b.txt").split()[1] == foreign  # preserved
    assert get_release(engine, SLUG) is None and record_count(engine) == 0
    assert slot(engine)["progress"]["work_commit"] == w

    git(repo, "reset", "-q", "--", "b.txt")  # the owner removes the foreign staging
    out = applied(cli, repo, c)
    assert step(out, "work_commit")["result"] == "adopted"
    assert_released(engine, repo, w, [dirty["wip"]], section=released_section(
        record_date(engine), lines="- work in progress\n"))


# ═════════════════════════════════════════════════════════════════════════════
# 21. Refusals with nothing committed (HEAD stays H0)
# ═════════════════════════════════════════════════════════════════════════════


def _edit_included(m):
    write(m["repo"], "b.txt", "b9\n")
    return "work_changed", lambda d: ("b.txt" in d["paths_changed"]
                                      and d["tree_now"] != d["tree_expected"])


def _dirty_outside(m):
    write(m["repo"], "README", "changed outside the approved paths\n")
    return "work_changed", lambda d: d["new_dirty"] == ["README"]


def _untracked_gone(m):
    (m["repo"] / "new.txt").unlink()
    return "work_changed", lambda d: d["missing_untracked"] == ["new.txt"]


def _index_changed(m):
    blob = hash_bytes(m["repo"], b"staged by someone\n", write=True)
    git(m["repo"], "update-index", "--cacheinfo", f"100644,{blob},b.txt")
    return "index_changed", lambda d: d["paths"] == ["b.txt"]


def _branch_switched(m):
    git(m["repo"], "checkout", "-q", "-b", "elsewhere")
    return "base_moved", lambda d: d["why"] == "branch_switched"


def _detached(m):
    git(m["repo"], "checkout", "-q", "--detach")
    return "base_moved", lambda d: d["why"] == "branch_switched"


def _head_moved(m):
    write(m["repo"], "other.txt", "someone else\n")
    git(m["repo"], "add", "other.txt")
    git(m["repo"], "commit", "-q", "-m", "chore: someone else")
    return "base_moved", lambda d: (d["expected_parent"] == m["h0"]
                                    and d["head"] == head(m["repo"]))


@pytest.mark.parametrize("change", [_edit_included, _dirty_outside, _untracked_gone,
                                    _index_changed, _branch_switched, _detached, _head_moved],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t21_a_changed_checkout_refuses_before_any_commit(engine, mixed, cli, change):
    repo, bare = mixed["repo"], mixed["bare"]
    c = variant(do_preview(cli, repo, *mixed["argv"], "--no-variants"))["consent_digest"]
    kept = slot(engine)
    key, check = change(mixed)
    head_before = head(repo)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=key)
    assert check(doc["details"][key]), doc["details"][key]
    assert doc["details"]["made"] == NO_MADE
    assert head(repo) == head_before
    assert_untouched(engine, before, repo, bare, slot_before=kept)


def _touch(m):
    stamp = os.stat(m["repo"] / "b.txt").st_mtime - 300
    os.utime(m["repo"] / "b.txt", (stamp, stamp))


def _unstage_partial(m):
    git(m["repo"], "reset", "-q", "--", "c.txt")  # the index entry is H0's again


def _stage_fully(m):
    git(m["repo"], "add", "b.txt")  # the index entry is T1's


def _new_untracked(m):
    write(m["repo"], "later.local", "appeared after the preview\n")


@pytest.mark.parametrize("change", [_touch, _unstage_partial, _stage_fully, _new_untracked],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t21_changes_outside_the_bound_content_do_not_refuse(engine, mixed, cli, change):
    """Content equality, not stat equality: a touched file, an index entry
    that is H0's or T1's, an untracked file that appeared later."""
    repo = mixed["repo"]
    doc = do_preview(cli, repo, *mixed["argv"], "--no-variants")
    change(mixed)
    out = applied(cli, repo, variant(doc)["consent_digest"])
    w = out["details"]["made"]["work_commit"]
    assert tree_of(repo, w) == doc["details"]["work"]["tree"]
    assert index_entries(repo, MIXED_PS) == tree_entries(repo, head(repo), MIXED_PS)
    assert "later.local" not in git(repo, "ls-tree", "-r", "--name-only", w).split()


# ═════════════════════════════════════════════════════════════════════════════
# 22. R9a: the pre-commit derivation differs — nothing made
# ═════════════════════════════════════════════════════════════════════════════


def _entry_edited(engine, d, monkeypatch):
    update_entry(engine, SLUG, d["wip"], entry="work in progress, reworded")
    return "entries"


def _entry_added(engine, d, monkeypatch):
    add_entry(engine, SLUG, "fixed", "late fix")
    return "entries"


def _pr_refs_toggled(engine, d, monkeypatch):
    set_changelog_pr_refs(engine, SLUG, True)
    return "changelog"


def _changelog_unmanaged(engine, d, monkeypatch):
    set_manages_changelog(engine, SLUG, False)
    return "changelog"


def _contract_changed(engine, d, monkeypatch):
    set_version_files(engine, SLUG, ["VERSION"], confirm=True)
    return "version_files"


def _next_day(engine, d, monkeypatch):
    monkeypatch.setattr(effects, "datetime", Tomorrow)
    return "changelog"


def _push_url_removed(engine, d, monkeypatch):
    elsewhere = d["bare"].parent / "elsewhere.git"
    git(d["bare"].parent, "init", "-q", "--bare", "-b", "main", str(elsewhere))
    git(d["repo"], "remote", "set-url", "origin", str(elsewhere))
    return "push"


@pytest.mark.parametrize("mutate", [_entry_edited, _entry_added, _pr_refs_toggled,
                                    _changelog_unmanaged, _contract_changed, _next_day,
                                    _push_url_removed],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t22_a_bound_change_refuses_before_the_work_commit(
        engine, dirty, cli, monkeypatch, mutate):
    repo, bare, log = dirty["repo"], dirty["bare"], dirty["log"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    kept = slot(engine)
    runs = sync_runs(log)
    key = mutate(engine, dirty, monkeypatch)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="consent_mismatch")
    mismatch = doc["details"]["consent_mismatch"]
    assert key in mismatch["changed"], mismatch
    assert set(mismatch["diff"]) <= set(mismatch["changed"])
    for leaf in mismatch["diff"].values():
        assert set(leaf) == {"approved", "now"}
    assert doc["details"]["made"] == NO_MADE
    assert head(repo) == dirty["h0"]
    assert_untouched(engine, before, repo, bare, slot_before=kept)
    assert sync_runs(log) == runs  # the script never runs in apply


def test_t22_outside_git_a_changed_changelog_file_refuses(engine, plain, cli, tmp_path):
    root = plain["root"]
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    c = variant(do_preview(cli, tmp_path, "--entries", str(a)))["consent_digest"]
    edited = HISTORY + "\n- edited by hand\n"
    (root / "changelog.md").write_text(edited)
    doc = refused_apply(cli, tmp_path, c, key="consent_mismatch")
    assert "changelog" in doc["details"]["consent_mismatch"]["changed"]
    assert (root / "changelog.md").read_text() == edited
    assert (root / "VERSION").read_text() == "1.0.0\n"
    assert get_release(engine, SLUG) is None


# ═════════════════════════════════════════════════════════════════════════════
# 23. Hooks: R7 / R8 and a rejecting hook refuse before publication
# ═════════════════════════════════════════════════════════════════════════════


def test_t23_a_formatter_in_pre_commit_refuses_before_publication(engine, dirty, cli):
    repo, bare = dirty["repo"], dirty["bare"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    install_hook(repo, "pre-commit", "printf 'formatted\\n' > b.txt\ngit add b.txt\n")
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="hook_changed_tree")
    assert doc["details"]["hook_changed_tree"]["paths"] == ["b.txt"]
    assert doc["details"]["made"] == NO_MADE
    assert head(repo) == dirty["h0"]
    assert (repo / "b.txt").read_text() == "formatted\n"  # dct restores nothing
    after = world(engine, repo, bare)
    assert after["refs"] == before["refs"] and after["commits"] == before["commits"]
    assert after["index"] == before["index"]  # the hook staged into the TEMPORARY index
    assert after["records"] == 0 and after["remote"] == before["remote"]
    assert slot(engine) == kept


def test_t23_a_commit_msg_hook_that_adds_an_id_refuses_before_publication(engine, dirty, cli):
    repo, bare = dirty["repo"], dirty["bare"]
    other = add_entry(engine, SLUG, "fixed", "not in this commit")["id"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"], "--no-variants"))[
        "consent_digest"]
    install_hook(repo, "commit-msg", f'm=$(cat "$1")\nprintf \'%s, {other}\\n\' "$m" > "$1"\n')
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="message_contract")
    contract = doc["details"]["message_contract"]
    assert contract["approved_ids"] == [dirty["wip"]]
    assert contract["proven_ids"] == sorted([dirty["wip"], other])
    assert contract["subject"] == "feat: work in progress"
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare, slot_before=kept)


def test_t23_a_rejecting_hook_stops_with_nothing_made(engine, dirty, cli):
    repo, bare = dirty["repo"], dirty["bare"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    install_hook(repo, "pre-commit", "echo 'refused by policy' >&2\nexit 1\n")
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="hook")
    assert doc["details"]["hook"] == "pre-commit"
    assert "refused by policy" in doc["details"]["git_error"]
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare, slot_before=kept)


# ═════════════════════════════════════════════════════════════════════════════
# 24. R9 after W: W stays, the slot keeps its progress, the skill previews again
# ═════════════════════════════════════════════════════════════════════════════


def test_t24_a_mismatch_after_the_work_commit_keeps_it_and_exposes_the_consent(
        engine, dirty, cli, at_point):
    repo, h0, wip = dirty["repo"], dirty["h0"], dirty["wip"]
    first = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))
    c = first["consent_digest"]
    at_point["after_work_commit"] = lambda: update_entry(engine, SLUG, wip,
                                                         entry="work in progress, reworded")
    doc = refused_apply(cli, repo, c, key="consent_mismatch")
    w = doc["details"]["made"]["work_commit"]
    assert w and w == head(repo) and parents(repo, w) == [h0]  # never reset
    assert "entries" in doc["details"]["consent_mismatch"]["changed"]
    assert "commit made, release not started" in doc["details"]["error"]
    assert record_count(engine) == 0
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == wip] == [None]
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": None,
                                        "consent": c}
    pending = pending_consent(engine, repo)
    assert pending["consent_digest"] == c and pending["adoptable"] is True
    assert pending["progress"]["work_commit"] == w
    assert all(set(v) == {"name", "version"} for v in pending["variants"])
    # The Finish question says WHAT it releases: the variant the commit was
    # made under (its version, entries and attested ids) — never a digest.
    assert pending["applied"] == {
        "name": "shown", "version": first["consent"]["version"]["target"],
        "entries": first["consent"]["entries"]["selected"],
        "attest": (first["consent"]["attest"] or {}).get("ids", [])}
    assert c not in json.dumps(pending["applied"])
    assert pending["checkout_root"] == first["consent"]["checkout_root"]

    # The tree is clean now: the new preview needs no message, and it releases.
    again = do_preview(cli, repo)
    assert again["details"]["work"] is None and again["details"]["base"]["head"] == w
    applied(cli, repo, variant(again)["consent_digest"])
    assert_released(engine, repo, w, [wip], section=released_section(
        record_date(engine), lines="- work in progress, reworded\n"))


def test_t24_a_draft_preview_exposes_no_digest(engine, rel, cli):
    """A3: pending_consent names the variants without digests; consent_digest
    is non-null ONLY once a commit was made under consent."""
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    add_entry(engine, SLUG, "fixed", "waiting")
    doc = do_preview(cli, repo)
    pending = pending_consent(engine, repo)
    assert pending["preview_id"] == doc["details"]["preview_id"]
    assert pending["base_head"] == head(repo)
    assert pending["progress"] == NO_MADE
    assert pending["consent_digest"] is None
    assert pending["applied"] is None  # a draft: nobody said Release yet
    assert pending["variants"] == [{"name": v["name"], "version": v["consent"]["version"]
                                    ["target"]} for v in doc["details"]["variants"]]
    text = json.dumps(pending)
    for v in doc["details"]["variants"]:
        assert v["consent_digest"] not in text


# ═════════════════════════════════════════════════════════════════════════════
# 25. Flag and slot refusals (R1, R2, R3)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def previewed(engine, rel, cli):
    repo = rel["repo"]
    a = proven(engine, repo, "feature a")
    doc = do_preview(cli, repo)
    return {**rel, "a": a, "doc": doc, "c": variant(doc)["consent_digest"]}


@pytest.mark.parametrize("extra,flags", [
    (["--version", "1.2.0"], ["--version"]),
    (["--minor"], ["--minor"]),
    (["--entries", "1"], ["--entries"]),
    (["--no-entries"], ["--no-entries"]),
    (["--plan-digest", "0" * 64], ["--plan-digest"]),
    (["--proposal-digest", "0" * 64], ["--proposal-digest"]),
    (["--push-remote", "origin"], ["--push-remote"]),
    (["--push-branch", "main"], ["--push-branch"]),
    (["--push-url", "/tmp/elsewhere.git"], ["--push-url"]),
    (["--no-push"], ["--no-push"]),
    (["--version", "1.2.0", "--no-push"], ["--no-push", "--version"]),
])
def test_t25_consent_excludes_every_binding_flag(engine, previewed, cli, extra, flags):
    repo, bare = previewed["repo"], previewed["bare"]
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, previewed["c"], *extra, key="consent_flag_conflict")
    assert sorted(doc["details"]["consent_flag_conflict"]) == sorted(flags)
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare, slot_before=kept)


def test_t25_consent_without_confirm_is_refused(engine, previewed, cli):
    repo, bare = previewed["repo"], previewed["bare"]
    kept = slot(engine)
    before = world(engine, repo, bare)
    code, doc = apply_consent(cli, repo, previewed["c"], confirm=False)
    assert code == 1 and doc["outcome"] == "refused"
    assert_untouched(engine, before, repo, bare, slot_before=kept)


def test_t25_no_preview_ever(engine, rel, cli):
    repo, bare = rel["repo"], rel["bare"]
    proven(engine, repo, "feature a")
    before = world(engine, repo, bare)
    refused_apply(cli, repo, "a" * 64, key="no_preview")
    assert_untouched(engine, before, repo, bare)


def test_t25_no_preview_after_discard(engine, previewed, cli):
    repo, bare = previewed["repo"], previewed["bare"]
    code, _, _ = cli(repo, "prepare", "--discard-preview")
    assert code == 0
    before = world(engine, repo, bare)
    refused_apply(cli, repo, previewed["c"], key="no_preview")
    assert_untouched(engine, before, repo, bare)


def test_t25_no_preview_after_a_prepare_replaced_it(engine, previewed, cli):
    from conftest import approved_plan_digest

    repo, bare, a = previewed["repo"], previewed["bare"], previewed["a"]
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a],
                    plan_digest=digest)
    pending = get_pending_proposal(engine, SLUG)
    before = world(engine, repo, bare)
    refused_apply(cli, repo, previewed["c"], key="no_preview")
    assert_untouched(engine, before, repo, bare)
    assert get_pending_proposal(engine, SLUG) == pending  # the D/P proposal is kept


def test_t25_a_rerun_after_completion_changes_nothing(engine, previewed, cli):
    """The slot was consumed by the stamp: the same C again is refused
    (no_preview) or a no-op — never a second release, never a second sync."""
    repo, bare, log = previewed["repo"], previewed["bare"], previewed["log"]
    applied(cli, repo, previewed["c"])
    before = world(engine, repo, bare)
    code, doc = apply_consent(cli, repo, previewed["c"])
    assert (code, doc["outcome"]) in ((1, "refused"), (0, "noop")), doc
    if code == 1:
        assert "no_preview" in doc["details"]
    assert_untouched(engine, before, repo, bare)
    assert sync_runs(log) == ["1.1.0"]


def test_t25_consent_unknown_names_the_current_preview(engine, previewed, cli):
    repo, bare = previewed["repo"], previewed["bare"]
    old_c = previewed["c"]
    newer = do_preview(cli, repo, "--version", "1.2.0")  # other content → other C
    assert variant(newer)["consent_digest"] != old_c
    kept = slot(engine)
    before = world(engine, repo, bare)
    for c in (old_c, "f" * 64):
        doc = refused_apply(cli, repo, c, key="consent_unknown")
        assert doc["details"]["consent_unknown"]["preview_id"] == newer["details"]["preview_id"]
        assert "variants" in doc["details"]["consent_unknown"]
        assert_untouched(engine, before, repo, bare, slot_before=kept)


def test_t25_an_identical_newer_preview_accepts_the_same_digest(engine, previewed, cli):
    """C binds the content, not the preview id: an identical newer preview
    has the same C, and the release is bound to the NEWER preview."""
    repo = previewed["repo"]
    newer = do_preview(cli, repo)
    assert variant(newer)["consent_digest"] == previewed["c"]
    applied(cli, repo, previewed["c"])
    record = get_release(engine, SLUG, "1.1.0")
    assert record["proposal"]["id"] == newer["details"]["preview_id"]


# ═════════════════════════════════════════════════════════════════════════════
# 26. The remote changed after the preview
# ═════════════════════════════════════════════════════════════════════════════


def _remote_tag(d, tmp_path):
    git(d["repo"], "tag", "v1.1.0")
    git(d["repo"], "push", "-q", "origin", "v1.1.0")
    git(d["repo"], "tag", "-d", "v1.1.0")
    return "remote_tags"


def _remote_ahead(d, tmp_path):
    other = tmp_path / "other-clone"
    git(tmp_path, "clone", "-q", str(d["bare"]), str(other))
    write(other, "elsewhere.txt", "x\n")
    git(other, "add", "elsewhere.txt")
    git(other, "commit", "-q", "-m", "feat: elsewhere")
    git(other, "push", "-q", "origin", "main")
    return "remote_branch"


@pytest.mark.parametrize("change", [_remote_tag, _remote_ahead],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t26_a_remote_change_after_the_preview_refuses_before_the_stamp(
        engine, previewed, cli, tmp_path, change):
    repo, bare = previewed["repo"], previewed["bare"]
    git(repo, "push", "-q", "origin", "main")
    key = change(previewed, tmp_path)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, previewed["c"], key=key)
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare)
    assert git(repo, "tag", "--list", "v1.1.0") == ""


def test_r10_blobs_pruned_after_the_pin_was_lost_refuse(engine, previewed, cli):
    """R10: the expected blobs only the preview pin kept are gone (an older
    dct's cleanup, a lost pin + gc) — refused, never re-synced."""
    repo, bare, log = previewed["repo"], previewed["bare"], previewed["log"]
    git(repo, "update-ref", "-d", slot(engine)["pin"])
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    expected = variant(previewed["doc"])["consent"]["version_files"]["expected_blobs"]
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e",
                           expected["pyproject.toml"]], capture_output=True,
                          check=False).returncode != 0  # precondition
    before = world(engine, repo, bare)
    refused_apply(cli, repo, previewed["c"], key="missing_blobs")
    assert_untouched(engine, before, repo, bare)
    assert sync_runs(log) == ["1.1.0"]


# ═════════════════════════════════════════════════════════════════════════════
# 27 / 28. Inside the stamp transaction: R11 preview_replaced, R12 date_changed
# ═════════════════════════════════════════════════════════════════════════════


def _replace_by_a_prepare(engine, d):
    stored = slot(engine)
    content = stored["variants"][d["c"]]["proposal_content"]
    ledger.set_pending_proposal(engine, SLUG, {**content, "id": new_ulid(),
                                               "candidate_sha": head(d["repo"]),
                                               "plan_digest": "0" * 64})


def _replace_by_a_preview(engine, d):
    ledger.set_preview(engine, SLUG, {**slot(engine), "id": new_ulid()})


@pytest.mark.parametrize("replace", [_replace_by_a_prepare, _replace_by_a_preview],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_t27_a_slot_replaced_at_before_stamp_refuses_with_nothing_stamped(
        engine, previewed, cli, at_point, replace):
    repo, bare, a = previewed["repo"], previewed["bare"], previewed["a"]
    remote_before = remote_refs(bare)
    at_point["before_stamp"] = lambda: replace(engine, previewed)
    doc = refused_apply(cli, repo, previewed["c"], key="preview_replaced")
    assert doc["details"]["made"] == NO_MADE
    assert record_count(engine) == 0
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == a] == [None]
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert git(repo, "tag", "--list", "v1.1.0") == ""
    assert remote_refs(bare) == remote_before  # nothing pushed


def test_t28_the_date_at_the_stamp_differs_from_the_bound_date(
        engine, previewed, cli, at_point, monkeypatch):
    repo, a = previewed["repo"], previewed["a"]
    from dct.core import changelog as core_changelog

    at_point["before_stamp"] = lambda: monkeypatch.setattr(core_changelog, "datetime",
                                                           Tomorrow)
    doc = refused_apply(cli, repo, previewed["c"], key="date_changed")
    assert doc["details"]["made"] == NO_MADE
    assert record_count(engine) == 0
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == a] == [None]
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert git(repo, "tag", "--list", "v1.1.0") == ""


def test_t28_core_stamp_checks_the_expected_release_date(engine):
    create_project(engine, slug=SLUG, name="R", path="/tmp/proj-r", current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    with pytest.raises(ValueError, match="date"):
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a], expected_release_date=yesterday)
    assert record_count(engine) == 0
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                    expected_release_date=datetime.now(UTC).date().isoformat())
    assert get_project(engine, SLUG)["current_version"] == "1.1.0"


class _Captured(Exception):
    """Stops apply --consent at its stamp call, with the call's arguments."""

    def __init__(self, args, kwargs):
        super().__init__("captured")
        self.call = (args, kwargs)


def captured_stamp_call(engine, previewed, monkeypatch) -> tuple[tuple, dict]:
    """The exact (args, kwargs) apply --consent passes to the stamp
    (release_entries with consume_preview) for the previewed release —
    nothing stamped."""
    def capture(*args, **kwargs):
        raise _Captured(args, kwargs)

    monkeypatch.setattr(effects, "release_entries", capture)
    with pytest.raises(_Captured) as exc:
        effects.apply_consent(engine, SLUG, previewed["c"], confirm=True,
                              cwd=str(previewed["repo"]))
    monkeypatch.undo()
    assert record_count(engine) == 0
    args, kwargs = exc.value.call
    assert kwargs["consume_preview"]["consent_sha256"] == previewed["c"]
    return args, kwargs


def test_t28_consume_preview_and_consume_proposal_are_exclusive(engine, previewed, monkeypatch):
    """Everything else is the VALID stamp call apply --consent makes (a real
    consume_preview, consent and proposal — the control below stamps with
    it): adding consume_proposal is refused for exactly one reason, the two
    modes' exclusivity, and nothing is written."""
    args, kwargs = captured_stamp_call(engine, previewed, monkeypatch)
    with pytest.raises(ValueError, match="mutually exclusive"):
        release_entries(*args, **{**kwargs, "consume_proposal": True})
    assert record_count(engine) == 0
    assert all(e["version"] is None for e in list_entries(engine, SLUG))
    # Control: the same call without consume_proposal is a valid stamp.
    stamped = release_entries(*args, **kwargs)
    assert stamped["release"]["consent"]["content_sha256"] == previewed["c"]
    assert record_count(engine) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 29. Resume after the stamp
# ═════════════════════════════════════════════════════════════════════════════


def merged_pr(engine, repo, text: str = "feature a", number: int = 7) -> int:
    """An entry proven on feat/a and brought into main by `Merge pull
    request #N` — the render with PR references shows `(#N)`."""
    a = add_entry(engine, SLUG, "added", text, branch="feat/a", branch_source="client")["id"]
    git(repo, "checkout", "-q", "-b", "feat/a")
    commit(repo, f"feat: {text}\n\nChangelog: {a}\n", "a.txt")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", f"Merge pull request #{number} from org/feat/a",
        "feat/a")
    return a


def test_t29_resume_after_the_stamp_completes_from_the_stored_consent(
        engine, rel, cli, at_point):
    """resume never re-derives C: the PR render the consent binds (off)
    survives switching the project setting ON between the stamp and the
    resume. The fixture really changes the render — with the setting on,
    the section names the pull request."""
    repo, log = rel["repo"], rel["log"]
    a = merged_pr(engine, repo)
    h0 = head(repo)
    set_changelog_pr_refs(engine, SLUG, True)
    assert "- feature a (#7)" in variant(do_preview(cli, repo))["display"]["section"]
    set_changelog_pr_refs(engine, SLUG, False)
    shown = variant(do_preview(cli, repo))
    assert "(#7)" not in shown["display"]["section"]
    c = shown["consent_digest"]
    at_point["after_stamp"] = interrupt("after_stamp")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "stamped"
    assert record["consent"]["content_sha256"] == c
    ledger.check_record_consistency(record)  # accepts content_sha256
    runs = sync_runs(log)
    set_changelog_pr_refs(engine, SLUG, True)
    out = resume_release(engine, SLUG, cwd=str(repo))
    assert out["outcome"] == "completed", out["details"]
    assert_released(engine, repo, h0, [a])
    release_commit = get_release(engine, SLUG, "1.1.0")["release_commit_sha"]
    released = subprocess.run(["git", "-C", str(repo), "show", f"{release_commit}:changelog.md"],
                              capture_output=True, check=True).stdout
    assert "(#7)" not in released.decode(), "resume rendered with today's setting"
    assert sha256(released) == shown["consent"]["changelog"]["composed_sha256"]
    assert shown["display"]["section"] in released.decode()
    assert sync_runs(log) == runs, "resume ran the version sync again"


def test_t29_resume_after_the_stamp_of_a_dirty_release(engine, dirty, cli, at_point):
    repo = dirty["repo"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    at_point["after_stamp"] = interrupt("after_stamp")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    record = get_release(engine, SLUG, "1.1.0")
    w = record["candidate_sha"]
    assert parents(repo, w) == [dirty["h0"]] and head(repo) == w
    assert resume_release(engine, SLUG, cwd=str(repo))["outcome"] == "completed"
    assert_released(engine, repo, w, [dirty["wip"]], section=released_section(
        record_date(engine), lines="- work in progress\n"))


# ═════════════════════════════════════════════════════════════════════════════
# 33. Round trip: what the preview bound is what was released
# ═════════════════════════════════════════════════════════════════════════════


def assert_round_trip(engine, repo, shown: dict, out: dict) -> None:
    """The view the screen was rendered from describes the real release."""
    view, made = shown["consent"], out["details"]["made"]
    record = get_release(engine, SLUG, view["version"]["target"])
    assert record["status"] == "completed"
    assert record["consent"]["content_sha256"] == shown["consent_digest"]
    assert shown["consent_digest"] == canonical_digest(view)
    k, release_commit = record["candidate_sha"], record["release_commit_sha"]
    assert record["entry_ids"] == view["entries"]["selected"]
    assert tree_of(repo, k) == view["candidate_tree"]
    # K is H0, or W on H0, or A on W / H0 — nothing else between.
    chain = [made[name] for name in ("attest_commit", "work_commit") if made[name]]
    assert (chain[0] if chain else view["base"]["head"]) == k
    for child, parent in zip(chain, [*chain[1:], view["base"]["head"]][:len(chain)],
                             strict=True):
        assert parents(repo, child) == [parent]
    if view["work"] is not None:
        w = made["work_commit"]
        assert tree_of(repo, w) == view["work"]["tree"] and parents(repo, w) == [
            view["base"]["head"]]
        assert sha256(commit_message(repo, w)) == view["work"]["message_sha256"]
        assert proves(repo, w) == view["work"]["proves"]
    if view["attest"] is not None:
        assert proves(repo, made["attest_commit"]) == view["attest"]["ids"]
    for entry_id, bound in view["entries"]["proof"].items():
        recorded = [alpha(c, made) for c in record["proof"][entry_id]["commits"]]
        assert recorded == bound["commits"], entry_id
    changelog = view["changelog"]
    released = subprocess.run(["git", "-C", str(repo), "show",
                               f"{release_commit}:{changelog['path']}"],
                              capture_output=True, check=True).stdout
    assert blob_at(repo, k, changelog["path"]) == changelog["base_blob"]
    assert sha256(released) == changelog["composed_sha256"]
    assert shown["display"]["section"] in released.decode()
    assert sha256(shown["display"]["section"]) == changelog["section_sha256"]
    for path, blob in view["version_files"]["expected_blobs"].items():
        assert blob_at(repo, release_commit, path) == blob
    for path, blob in view["version_files"]["base_blobs"].items():
        assert (blob_at(repo, k, path) if blob else None) == blob
    push = {key: value for key, value in view["push"].items() if key != "mode"}
    assert record["consent"]["push_target"] == push


def test_t33_round_trip_clean(engine, rel, cli):
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    shown = variant(do_preview(cli, repo))
    assert_round_trip(engine, repo, shown, applied(cli, repo, shown["consent_digest"]))


def test_t33_round_trip_clean_all(engine, rel, cli):
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    add_entry(engine, SLUG, "fixed", "waiting b")
    every = variant(do_preview(cli, repo), "all")
    assert_round_trip(engine, repo, every, applied(cli, repo, every["consent_digest"]))


def test_t33_round_trip_dirty(engine, dirty, cli):
    repo = dirty["repo"]
    shown = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))
    assert_round_trip(engine, repo, shown, applied(cli, repo, shown["consent_digest"]))


def test_t33_round_trip_dirty_all(engine, mixed, cli):
    repo = mixed["repo"]
    every = variant(do_preview(cli, repo, *mixed["argv"]), "all")
    assert_round_trip(engine, repo, every, applied(cli, repo, every["consent_digest"]))


def test_t33_round_trip_outside_git(engine, plain, cli, tmp_path):
    root, log = plain["root"], plain["log"]
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    shown = variant(do_preview(cli, tmp_path, "--entries", str(a)))
    view = shown["consent"]
    out = applied(cli, tmp_path, shown["consent_digest"])
    assert out["details"]["made"] == NO_MADE
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed" and record["entry_ids"] == [a]
    assert record["consent"]["content_sha256"] == shown["consent_digest"]
    released = (root / "changelog.md").read_bytes()
    assert sha256(released) == view["changelog"]["composed_sha256"]
    assert shown["display"]["section"] in released.decode()
    assert sha256((root / "VERSION").read_bytes()) == view["version_files"]["expected_blobs"][
        "VERSION"]
    assert (root / "VERSION").read_text() == "1.1.0\n"
    assert sync_runs(log) == ["1.1.0"]  # the preview's run only
    assert slot(engine) is None


# ── the approved push target is where the release goes (K-39) ────────────────


def bare_seeded(tmp_path: Path, name: str, repo: Path) -> Path:
    """A bare repository holding main and v1.0.0 of `repo` (a fork)."""
    path = tmp_path / name
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(path))
    git(repo, "push", "-q", str(path), "main", "v1.0.0")
    return path


def commit_in(bare: Path, ref: str) -> str | None:
    """The commit `ref` names in `bare` (a tag peeled), None when absent."""
    found = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "-q",
                            f"{ref}^{{commit}}"], capture_output=True, text=True, check=False)
    return found.stdout.strip() or None


def test_k39_an_approved_fork_branch_and_push_url_get_the_release_not_the_upstream(
        engine, rel, cli, tmp_path):
    """The owner approves fork / rel / the fork's PUSH url while `main`
    tracks origin/main: the release commit and its tag land in that
    repository, on `rel` — and NOWHERE else: origin and the fork's fetch
    address keep every ref. The consent and the record name exactly that
    target (a regression that took the upstream default would push to
    origin/main and still pass a check of the CLI result alone)."""
    repo, origin = rel["repo"], rel["bare"]
    fetch_side = bare_seeded(tmp_path, "fork-fetch.git", repo)
    push_side = bare_seeded(tmp_path, "fork-push.git", repo)
    git(repo, "remote", "add", "fork", str(fetch_side))
    git(repo, "remote", "set-url", "--push", "fork", str(push_side))
    assert git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    proven(engine, repo, "feature a")
    before = {"origin": remote_refs(origin), "fetch": remote_refs(fetch_side),
              "push": remote_refs(push_side)}

    shown = variant(do_preview(cli, repo, "--push-remote", "fork", "--push-branch", "rel",
                               "--push-url", str(push_side)))
    target = {"remote": "fork", "branch": "rel", "url": str(push_side), "tag": "v1.1.0"}
    assert shown["consent"]["push"] == {"mode": "push", **target}
    out = applied(cli, repo, shown["consent_digest"])
    assert_round_trip(engine, repo, shown, out)

    record = get_release(engine, SLUG, "1.1.0")
    released = record["release_commit_sha"]
    assert record["consent"]["push_target"] == target
    assert record["push_proof"]["target"] == target
    assert commit_in(push_side, "refs/heads/rel") == released
    assert commit_in(push_side, "refs/tags/v1.1.0") == released
    after = remote_refs(push_side)
    assert set(before["push"]) <= set(after), "a ref of the fork was moved"
    assert sorted(r.split()[0] for r in set(after) - set(before["push"])) == [
        "refs/heads/rel", "refs/tags/v1.1.0"], "the push changed more than the approved refs"
    assert remote_refs(origin) == before["origin"], "the upstream got the release"
    assert remote_refs(fetch_side) == before["fetch"], "the fork's fetch address got the release"


def test_k39_a_local_release_leaves_every_remote_as_it_was(engine, rel, cli, tmp_path):
    """`--no-push`: the release completes locally (commit and tag in the
    checkout), every remote — the upstream and another configured one —
    keeps byte-identical refs, and neither the consent nor the record gets
    a push target (nor a push proof)."""
    repo, origin = rel["repo"], rel["bare"]
    other = bare_seeded(tmp_path, "other.git", repo)
    git(repo, "remote", "add", "other", str(other))
    proven(engine, repo, "feature a")
    before = {"origin": remote_refs(origin), "other": remote_refs(other)}

    shown = variant(do_preview(cli, repo, "--no-push"))
    assert shown["consent"]["push"] == {"mode": "local", "reason": "requested"}
    out = applied(cli, repo, shown["consent_digest"])
    assert out["outcome"] == "completed"

    record = get_release(engine, SLUG, "1.1.0")
    released = record["release_commit_sha"]
    assert record["status"] == "completed"
    assert record["consent"]["push_target"] is None
    assert record["push_proof"] is None
    assert git(repo, "rev-parse", "v1.1.0^{commit}") == released
    assert git(repo, "rev-parse", "main") == released
    assert remote_refs(origin) == before["origin"], "a local release pushed to the upstream"
    assert remote_refs(other) == before["other"], "a local release pushed to another remote"
    assert commit_in(origin, "refs/tags/v1.1.0") is None


# ═════════════════════════════════════════════════════════════════════════════
# Safety review of step 3 (G1–G6): every refusal the contract places BEFORE a
# publication or BEFORE the stamp must happen there — state assertions only.
# ═════════════════════════════════════════════════════════════════════════════


def drop_commit_identity(monkeypatch) -> None:
    """git can no longer name the author / committer (as on a machine with
    no user.name / user.email): the caller's variables go, and the throwaway
    global config forbids guessing one from the host name."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text("[user]\n\tuseConfigOnly = true\n")


def object_exists(repo, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True, check=False).returncode == 0


def assert_nothing_released(engine, repo, bare, ids: list[int]) -> None:
    assert record_count(engine) == 0, "a release record was written (the stamp ran)"
    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] in ids] == \
        [None] * len(ids), "an entry was stamped"
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert git(repo, "tag", "--list", "v1.*") == "v1.0.0"
    assert f"refs/tags/v1.0.0 {git(repo, 'rev-parse', 'v1.0.0')}" in remote_refs(bare)
    assert not [r for r in remote_refs(bare) if "v1.1" in r or "v1.0.1" in r]


# ── G1: the commit identity is checked before the stamp on EVERY consent ─────


def test_g1_clean_shown_variant_without_a_commit_identity_refuses_before_the_stamp(
        engine, rel, cli, monkeypatch):
    """G1: a clean shown variant makes no W / A, yet the release commit
    needs an identity — unknown, it must refuse BEFORE the stamp (as the
    digest path does), never stamp + write files and then stop at the
    release commit with an unfinished record."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    c = variant(do_preview(cli, repo))["consent_digest"]
    kept = slot(engine)
    drop_commit_identity(monkeypatch)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=None)
    assert doc["details"]["made"] == NO_MADE
    assert get_release(engine, SLUG) is None, get_release(engine, SLUG)["status"]
    assert_nothing_released(engine, repo, bare, [a])
    assert_untouched(engine, before, repo, bare, slot_before=kept)
    assert "before the stamp" in doc["details"]["error"], doc["details"]["error"]


def test_g1_an_adopted_work_commit_without_a_commit_identity_refuses_before_the_stamp(
        engine, mixed, cli, at_point, monkeypatch):
    """G1: the re-run of an interrupted apply adopts W (nothing left to
    commit before the stamp) — the identity of the release commit is still
    checked before the stamp."""
    repo, bare = mixed["repo"], mixed["bare"]
    c = variant(do_preview(cli, repo, *mixed["argv"], "--no-variants"))["consent_digest"]
    at_point["after_work_index"] = interrupt("after_work_index")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    progress = slot(engine)["progress"]
    drop_commit_identity(monkeypatch)
    files = work_files(repo)
    doc = refused_apply(cli, repo, c, key=None)
    assert head(repo) == w
    assert get_release(engine, SLUG) is None, get_release(engine, SLUG)["status"]
    assert_nothing_released(engine, repo, bare, [mixed["wip"]])
    assert work_files(repo) == files, "the version files / changelog were written"
    assert slot(engine)["progress"] == progress
    assert "before the stamp" in doc["details"]["error"], doc["details"]["error"]


# ── G2: the applied consent is recorded with the progress ───────────────────


@pytest.mark.parametrize("applied_name", ["shown", "all"])
def test_g2_a_kill_between_the_work_commit_and_the_index_sync_exposes_the_applied_digest(
        engine, mixed, cli, at_point, applied_name):
    """G2: killed after W was published and recorded, before the index
    sync. The progress holds only W, which BOTH variants share — the slot
    must still record which C was applied (progress.consent), so
    pending_consent names it and the dialog reaches the re-run that syncs
    the index (instead of a fresh preview of a stale index that needs a
    message and publishes an EMPTY work commit)."""
    repo = mixed["repo"]
    doc = do_preview(cli, repo, *mixed["argv"])
    c = variant(doc, applied_name)["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    stored = slot(engine)["progress"]
    assert stored["work_commit"] == w
    assert stored.get("consent") == c, f"the slot does not record the applied C: {stored}"
    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is True
    assert pending["consent_digest"] == c

    out = applied(cli, repo, c)
    assert step(out, "work_commit")["result"] == "adopted"
    assert out["details"]["variant"] == applied_name
    assert git(repo, "rev-list", "--count", f"{mixed['h0']}..{w}") == "1"  # no second W
    assert index_entries(repo, MIXED_PS) == tree_entries(repo, head(repo), MIXED_PS)


@pytest.mark.parametrize("applied_name,other_name", [("shown", "all"), ("all", "shown")])
def test_g2_once_progress_exists_another_variants_digest_is_refused(
        engine, mixed, cli, at_point, applied_name, other_name):
    """G2: after a commit was made under C, the other screen of the same
    preview (which would adopt the same W) is not what the owner approved —
    refused before anything: no A, no index sync, no stamp, slot kept."""
    repo, bare = mixed["repo"], mixed["bare"]
    doc = do_preview(cli, repo, *mixed["argv"])
    c, other = variant(doc, applied_name)["consent_digest"], \
        variant(doc, other_name)["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    kept = slot(engine)
    before = world(engine, repo, bare)
    refused = refused_apply(cli, repo, other, key=None)
    assert refused["details"]["made"]["attest_commit"] is None
    assert head(repo) == w
    assert get_release(engine, SLUG) is None
    assert_untouched(engine, before, repo, bare, slot_before=kept)


def test_g2_a_sync_refused_by_foreign_staging_still_exposes_the_applied_digest(
        engine, mixed, cli, at_point):
    """G2: W published, the index sync refused (someone staged b.txt) — the
    owner unstages; pending_consent must name the C applied, so the dialog
    offers the re-run (which syncs the index) and never a fresh preview."""
    repo, h0 = mixed["repo"], mixed["h0"]
    doc = do_preview(cli, repo, *mixed["argv"])
    c = variant(doc)["consent_digest"]
    foreign = hash_bytes(repo, b"someone else's b\n", write=True)
    at_point["after_work_commit"] = lambda: git(
        repo, "update-index", "--cacheinfo", f"100644,{foreign},b.txt")
    refused = refused_apply(cli, repo, c, key="foreign_staged")
    w = refused["details"]["made"]["work_commit"]
    assert w == head(repo) and parents(repo, w) == [h0]
    git(repo, "reset", "-q", "--", "b.txt")
    pending = pending_consent(engine, repo)
    assert pending["consent_digest"] == c, pending
    out = applied(cli, repo, c)
    assert step(out, "work_commit")["result"] == "adopted"
    assert out["details"]["variant"] == "shown"
    assert index_entries(repo, MIXED_PS) == tree_entries(repo, head(repo), MIXED_PS)


# ── G3: a hook that changes files refuses BEFORE the publication ────────────


def test_g3_a_pre_commit_hook_rewriting_a_tracked_file_outside_the_work_refuses_before_w(
        engine, dirty, cli):
    """G3: b.txt is the work; the pre-commit hook rewrites a.txt (tracked,
    outside Pₛ) in the work tree. The temporary index and Pₛ are unchanged,
    yet the contract says a hook that changes files refuses BEFORE the
    publication — not after W is on the branch."""
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    install_hook(repo, "pre-commit", "printf 'rewritten by a hook\\n' > a.txt\n")
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=None)
    assert doc["details"]["made"] == NO_MADE, "W was published before the refusal"
    assert head(repo) == h0
    assert "a.txt" in json.dumps({k: v for k, v in doc["details"].items() if k != "error"})
    after = world(engine, repo, bare)
    assert after["refs"] == before["refs"] and after["commits"] == before["commits"]
    assert after["index"] == before["index"]
    assert after["remote"] == before["remote"] and after["records"] == 0
    assert (repo / "a.txt").read_text() == "rewritten by a hook\n"  # dct restores nothing
    assert slot(engine) == kept


def test_g3_a_hook_leaving_an_untracked_log_is_not_a_changed_file(engine, dirty, cli, tmp_path):
    """G3 guard: the new check covers tracked paths, conflicts and new
    untracked RELEASE ARTIFACTS — a hook that leaves an untracked log file
    (not an artifact) does not stop the release."""
    repo = dirty["repo"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    install_hook(repo, "pre-commit", "echo ran >> .hook-run.log\n")
    out = applied(cli, repo, c)
    w = out["details"]["made"]["work_commit"]
    assert ".hook-run.log" not in git(repo, "ls-tree", "-r", "--name-only", w).split()
    assert_completed(engine, repo, dirty["bare"], candidate=w, ids=[dirty["wip"]],
                     version="1.1.0")


def test_g3_a_pre_commit_hook_rewriting_a_file_during_the_attestation_refuses_before_a(
        engine, rel, cli):
    """G3: the attestation commit runs the same hook lifecycle — a hook
    that rewrites a tracked file refuses BEFORE A is published."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    h0 = head(repo)
    c = variant(do_preview(cli, repo), "all")["consent_digest"]
    install_hook(repo, "pre-commit", "printf 'rewritten by a hook\\n' > f.txt\n")
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=None)
    assert doc["details"]["made"] == NO_MADE, "A was published before the refusal"
    assert head(repo) == h0
    assert "f.txt" in json.dumps({k: v for k, v in doc["details"].items() if k != "error"})
    after = world(engine, repo, bare)
    assert after["refs"] == before["refs"] and after["commits"] == before["commits"]
    assert after["index"] == before["index"]
    assert_nothing_released(engine, repo, bare, [a, waiting])
    assert slot(engine) == kept


# ── G4: a consent without work checks the tree before A ─────────────────────


def _edit_tracked(repo):
    write(repo, "f.txt", "edited after the preview\n")


def _stage_tracked(repo):
    write(repo, "f.txt", "edited after the preview\n")
    git(repo, "add", "f.txt")


@pytest.mark.parametrize("how", ["all", "attest"])
@pytest.mark.parametrize("edit", [_edit_tracked, _stage_tracked],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_g4_an_edit_after_a_clean_preview_refuses_before_the_attestation(
        engine, rel, cli, how, edit):
    """G4: the screen showed a clean tree (work null) and an attestation
    ("with all N", or --attest). An edit made after the preview must refuse
    BEFORE A is made — the staged set is empty, so any tracked change is
    new_dirty."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    h0 = head(repo)
    if how == "all":
        c = variant(do_preview(cli, repo), "all")["consent_digest"]
    else:
        c = variant(do_preview(cli, repo, "--attest", str(waiting), "--no-variants"))[
            "consent_digest"]
    kept = slot(engine)
    edit(repo)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=None)
    assert doc["details"]["made"] == NO_MADE, "A was published before the refusal"
    assert head(repo) == h0
    assert "f.txt" in json.dumps({k: v for k, v in doc["details"].items() if k != "error"})
    assert_nothing_released(engine, repo, bare, [a, waiting])
    assert_untouched(engine, before, repo, bare, slot_before=kept)


# ── G5: a plain commit is adopted only with the approved message ────────────


def test_g5_a_plain_commit_with_a_body_the_owner_never_saw_is_not_adopted(
        engine, mixed, cli, tmp_path):
    """G5: same tree, same subject, same proven ids — but a body the screen
    never showed. Releasing it would record content_sha256 == C for a
    message nobody approved: not adoptable, refused before anything."""
    repo, bare = mixed["repo"], mixed["bare"]
    c = variant(do_preview(cli, repo, *mixed["argv"], "--no-variants"))["consent_digest"]
    git(repo, "add", "-A", "--", *MIXED_PS)
    git(repo, "commit", "-q", "-F", message_file(
        tmp_path, f"feat: work in progress\n\nAlso rewrites the deploy keys.\n\n"
                  f"Changelog: {mixed['wip']}\n", "other-body.txt"))
    plain_w = head(repo)
    assert subject(repo, plain_w) == "feat: work in progress"  # precondition
    assert proves(repo, plain_w) == [mixed["wip"]]
    assert pending_consent(engine, repo)["adoptable"] is False
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["head"] == plain_w
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare, slot_before=kept)


def test_g5_a_plain_commit_whose_message_cleans_to_the_approved_one_is_adopted(
        engine, mixed, cli, tmp_path):
    """G5 guard: the comparison is on the CLEANED message — a verbatim
    commit whose only difference is trailing blank lines is the approved
    commit."""
    repo = mixed["repo"]
    doc = do_preview(cli, repo, *mixed["argv"], "--no-variants")
    c = variant(doc)["consent_digest"]
    git(repo, "add", "-A", "--", *MIXED_PS)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-F", message_file(
        tmp_path, f"feat: work in progress\n\nChangelog: {mixed['wip']}\n\n\n",
        "trailing.txt"))
    plain_w = head(repo)
    assert commit_message(repo, plain_w) != doc["details"]["work"]["message"]  # precondition
    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted",
                                        "commit": plain_w}


def test_g5_a_work_commit_dct_made_keeps_a_hook_added_body_on_adoption(
        engine, dirty, cli, at_point):
    """G5 guard: the subject + ids rule stays for the W dct made itself (the
    recorded progress) — a commit-msg hook's trailer does not block the
    re-run of an interrupted apply."""
    repo = dirty["repo"]
    install_hook(repo, "commit-msg",
                 "git interpret-trailers --in-place --trailer 'Signed-off-by: t <t@t>' \"$1\"\n")
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    assert "Signed-off-by: t <t@t>" in commit_message(repo, w)  # precondition
    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}


# ── G6: a recorded progress commit that no longer exists never crashes ──────


def test_g6_the_plan_survives_a_progress_commit_removed_by_reset_and_gc(
        engine, dirty, cli, at_point):
    """G6: W was recorded, then the owner reset it away and git gc pruned
    it. `dct release plan` is read-only and must never raise: the commit is
    not adoptable, no digest, and a warning names it."""
    repo, h0 = dirty["repo"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    git(repo, "reset", "-q", "--hard", h0)
    git(repo, "update-ref", "-d", "ORIG_HEAD")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")
    assert not object_exists(repo, w)  # precondition
    assert slot(engine)["progress"]["work_commit"] == w

    plan = build_plan(engine, SLUG, cwd=str(repo))
    pending = plan["pending_consent"]
    assert pending["consent_digest"] is None
    assert pending["adoptable"] is False
    assert any(w[:7] in warning for warning in plan["warnings"]), plan["warnings"]

    code, doc, err = cli(repo, "plan")
    assert code == 0, (code, doc, err)
    assert doc["pending_consent"]["consent_digest"] is None


# ═════════════════════════════════════════════════════════════════════════════
# Consent-path gaps after G1–G6 (P1–P5): the window between a publication and
# its progress write, an index-only "work", the clean-tree check on resume,
# adoption of an attestation commit dct did not make, a lost progress commit.
# ═════════════════════════════════════════════════════════════════════════════


def kill_in_post_commit(monkeypatch, engine, *, nth: int = 1) -> list[dict]:
    """The process dies when the `nth` post-commit hook of the run starts
    (Ctrl-C in the user's hook): the git_write that runs it raises
    Interrupted. Returns the list the slot's progress AT THAT MOMENT is
    appended to — what a crash there leaves for the re-run."""
    original = effects.gitinfo.git_write
    seen: list[dict] = []
    calls = {"n": 0}

    def patched(repo_, *args, **kw):
        if "post-commit" in args:
            calls["n"] += 1
            if calls["n"] == nth:
                seen.append(dict(slot(engine)["progress"]))
                monkeypatch.setattr(effects.gitinfo, "git_write", original)
                raise Interrupted("post-commit")
        return original(repo_, *args, **kw)

    monkeypatch.setattr(effects.gitinfo, "git_write", patched)
    return seen


def attest_commits(repo) -> list[str]:
    return [line.split(" ", 1)[0] for line in git(repo, "log", "--all", "--format=%H %s")
            .splitlines() if line.split(" ", 1)[1] == ATTEST_SUBJECT]


# ── P1: progress is recorded before any user code runs after a publication ──


@pytest.mark.parametrize("trailer_hook", [False, True], ids=["no_msg_hook", "commit_msg_trailer"])
def test_p1_a_kill_in_the_post_commit_hook_of_w_leaves_the_progress_recorded(
        engine, dirty, cli, monkeypatch, trailer_hook):
    """P1: W is published, then the user's post-commit hook runs — a kill
    there must find W and the applied C ALREADY in the slot's progress
    (recorded right after the compare-and-swap), so pending_consent names
    C and the re-run adopts W; never an all-null progress that sends the
    dialog to a fresh preview of a stale index. With a commit-msg hook that
    adds a trailer, a W nobody recorded is not even adoptable (G5)."""
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    if trailer_hook:
        install_hook(repo, "commit-msg",
                     "git interpret-trailers --in-place --trailer 'Signed-off-by: t <t@t>' "
                     "\"$1\"\n")
    install_hook(repo, "post-commit", "true\n")
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    seen = kill_in_post_commit(monkeypatch, engine)
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    assert w != h0 and parents(repo, w) == [h0]  # precondition: W was published
    assert seen == [{"work_commit": w, "attest_commit": None, "consent": c}], \
        f"the post-commit hook ran before the progress was recorded: {seen}"
    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is True
    assert pending["consent_digest"] == c, pending

    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}
    assert git(repo, "rev-list", "--count", f"{h0}..{w}") == "1"  # no second W
    assert_completed(engine, repo, bare, candidate=w, ids=[dirty["wip"]], version="1.1.0")
    assert index_entries(repo, ["b.txt"]) == tree_entries(repo, head(repo), ["b.txt"])


def test_p1_a_kill_in_the_post_commit_hook_of_a_leaves_the_progress_recorded(
        engine, rel, cli, monkeypatch):
    """P1 for the attestation commit: A is published, its post-commit hook
    is killed — A and C are already the slot's progress."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    h0 = head(repo)
    install_hook(repo, "post-commit", "true\n")
    c = variant(do_preview(cli, repo), "all")["consent_digest"]
    seen = kill_in_post_commit(monkeypatch, engine)
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    att = head(repo)
    assert att != h0 and subject(repo, att) == ATTEST_SUBJECT  # precondition
    assert seen == [{"work_commit": None, "attest_commit": att, "consent": c}], \
        f"the post-commit hook ran before the progress was recorded: {seen}"
    assert pending_consent(engine, repo)["consent_digest"] == c

    out = applied(cli, repo, c)
    assert step(out, "attest") == {"step": "attest", "result": "adopted", "commit": att}
    assert attest_commits(repo) == [att], "a second attestation commit was made"
    assert_completed(engine, repo, bare, candidate=att, ids=[a, waiting], version="1.1.0")


def test_p1_a_kill_in_the_post_commit_hook_of_a_on_w_keeps_both_commits_recorded(
        engine, mixed, cli, monkeypatch):
    """P1, "with all N" on a dirty tree: W, then A on W — a kill in A's
    post-commit hook finds both W and A (and C) in the progress."""
    repo, h0 = mixed["repo"], mixed["h0"]
    install_hook(repo, "post-commit", "true\n")
    c = variant(do_preview(cli, repo, *mixed["argv"]), "all")["consent_digest"]
    seen = kill_in_post_commit(monkeypatch, engine, nth=2)
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    att = head(repo)
    [w] = parents(repo, att)
    assert parents(repo, w) == [h0] and subject(repo, att) == ATTEST_SUBJECT  # precondition
    assert seen == [{"work_commit": w, "attest_commit": att, "consent": c}], \
        f"the post-commit hook of A ran before A was recorded: {seen}"
    out = applied(cli, repo, c)
    assert step(out, "work_commit")["result"] == "adopted"
    assert step(out, "attest") == {"step": "attest", "result": "adopted", "commit": att}
    assert git(repo, "rev-list", "--count", f"{h0}..{att}") == "2"  # no second W / A


# ── P3: the clean-tree check runs before A on the resume path too ───────────


@pytest.mark.parametrize("point", ["after_work_commit", "after_work_index"])
@pytest.mark.parametrize("edit", ["modify", "delete"])
def test_p3_an_edit_after_an_interrupted_w_refuses_before_the_attestation(
        engine, mixed, cli, at_point, point, edit):
    """P3: W was made and recorded, A is still to be made; after the crash
    the owner edits (or deletes) a tracked file. The re-run must refuse
    BEFORE A is published (work_changed, the path newly dirty) — not
    publish A and then refuse at the clean-tree check of the stamp."""
    repo, bare = mixed["repo"], mixed["bare"]
    c = variant(do_preview(cli, repo, *mixed["argv"]), "all")["consent_digest"]
    at_point[point] = interrupt(point)
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    progress = slot(engine)["progress"]
    assert progress["work_commit"] == w and progress["attest_commit"] is None  # precondition
    if edit == "modify":
        write(repo, "a.txt", "edited after the crash\n")
    else:
        (repo / "a.txt").unlink()
    commits = git(repo, "rev-list", "--all", "--count")
    doc = refused_apply(cli, repo, c, key="work_changed")
    assert doc["details"]["made"]["attest_commit"] is None, "A was published before the refusal"
    assert head(repo) == w
    assert git(repo, "rev-list", "--all", "--count") == commits, "a commit object was referenced"
    assert attest_commits(repo) == []
    assert "a.txt" in doc["details"]["work_changed"]["new_dirty"], doc["details"]["work_changed"]
    assert slot(engine)["progress"] == progress
    assert_nothing_released(engine, repo, bare, [mixed["wip"], mixed["other"]])


def test_p3_guard_an_untracked_non_artifact_after_the_crash_does_not_refuse(
        engine, mixed, cli, at_point):
    """P3 guard: the resume check watches what the G4 check watches —
    tracked paths, conflicts, untracked RELEASE ARTIFACTS; a stray
    untracked file (an editor's backup) does not stop the release."""
    repo = mixed["repo"]
    c = variant(do_preview(cli, repo, *mixed["argv"]), "all")["consent_digest"]
    at_point["after_work_index"] = interrupt("after_work_index")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    write(repo, "a.txt~", "editor backup\n")
    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}
    assert step(out, "attest")["result"] == "done"


# ── P4: a plain attestation commit is adopted only with the approved message ─


def test_p4_a_plain_attestation_commit_with_a_body_the_owner_never_saw_is_not_adopted(
        engine, rel, cli, tmp_path):
    """P4: an empty commit with ATTEST_SUBJECT and exactly the approved ids,
    made with a plain `git commit`, carrying a body the screen never showed.
    Releasing it would record content_sha256 == C for a message nobody
    approved: not adoptable, refused before anything."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    c = variant(do_preview(cli, repo), "all")["consent_digest"]
    git(repo, "commit", "-q", "--allow-empty", "-F", message_file(
        tmp_path, f"{ATTEST_SUBJECT}\n\nAlso rotates the deploy keys.\n\n"
                  f"Changelog: {waiting}\n", "foreign-attest.txt"))
    plain_a = head(repo)
    assert subject(repo, plain_a) == ATTEST_SUBJECT and proves(repo, plain_a) == [waiting]
    assert pending_consent(engine, repo)["adoptable"] is False
    kept = slot(engine)
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["head"] == plain_a
    assert doc["details"]["made"] == NO_MADE
    assert_untouched(engine, before, repo, bare, slot_before=kept)
    assert_nothing_released(engine, repo, bare, [a, waiting])


def test_p4_a_plain_attestation_commit_on_w_with_a_foreign_body_is_not_adopted(
        engine, mixed, cli, at_point, tmp_path):
    """P4 on a dirty tree: dct made W (recorded), the owner then made A by
    hand with a body nobody approved — the A is not adopted."""
    repo, bare = mixed["repo"], mixed["bare"]
    c = variant(do_preview(cli, repo, *mixed["argv"]), "all")["consent_digest"]
    at_point["after_work_index"] = interrupt("after_work_index")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    git(repo, "commit", "-q", "--allow-empty", "-F", message_file(
        tmp_path, f"{ATTEST_SUBJECT}\n\nNot what the screen showed.\n\n"
                  f"Changelog: {mixed['other']}\n", "foreign-attest.txt"))
    plain_a = head(repo)
    assert parents(repo, plain_a) == [w] and proves(repo, plain_a) == [mixed["other"]]
    progress = slot(engine)["progress"]
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["head"] == plain_a
    assert slot(engine)["progress"] == progress
    assert_nothing_released(engine, repo, bare, [mixed["wip"], mixed["other"]])


@pytest.mark.parametrize("cleanup", ["default", "verbatim_trailing_blank_lines"])
def test_p4_guard_a_plain_attestation_commit_with_the_approved_message_is_adopted(
        engine, rel, cli, tmp_path, cleanup):
    """P4 guard: a hand-made A whose message IS attest_message(ids) — as
    stored, or after `git stripspace` — is the approved commit."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    c = variant(do_preview(cli, repo), "all")["consent_digest"]
    text = effects.attest_message([waiting])
    if cleanup == "default":
        git(repo, "commit", "-q", "--allow-empty", "-F", message_file(tmp_path, text, "a.txt"))
    else:
        git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F",
            message_file(tmp_path, text + "\n\n", "a.txt"))
    plain_a = head(repo)
    out = applied(cli, repo, c)
    assert step(out, "attest") == {"step": "attest", "result": "adopted", "commit": plain_a}
    assert_completed(engine, repo, bare, candidate=plain_a, ids=[a, waiting], version="1.1.0")


def test_p4_guard_an_attestation_commit_dct_made_keeps_a_hook_added_trailer_on_adoption(
        engine, rel, cli, at_point):
    """P4 guard: the subject + ids rule stays for the A dct recorded itself —
    a commit-msg hook's Signed-off-by does not block the re-run."""
    repo, bare = rel["repo"], rel["bare"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    install_hook(repo, "commit-msg",
                 "git interpret-trailers --in-place --trailer 'Signed-off-by: t <t@t>' \"$1\"\n")
    c = variant(do_preview(cli, repo), "all")["consent_digest"]
    at_point["after_attest_commit"] = interrupt("after_attest_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    att = head(repo)
    assert "Signed-off-by: t <t@t>" in commit_message(repo, att)  # precondition
    assert slot(engine)["progress"]["attest_commit"] == att
    out = applied(cli, repo, c)
    assert step(out, "attest") == {"step": "attest", "result": "adopted", "commit": att}
    assert_completed(engine, repo, bare, candidate=att, ids=[a, waiting], version="1.1.0")


# ── P5: never a second work commit under a slot with progress ───────────────


@pytest.mark.parametrize("pruned", [True, False], ids=["reset_and_gc", "reset_only"])
def test_p5_a_rerun_after_the_recorded_w_was_reset_away_makes_no_second_w(
        engine, dirty, cli, at_point, pruned):
    """P5: W was made and recorded (progress is write-once), then the owner
    reset HEAD back to H0 keeping the changes (and, in the first case, git
    gc pruned W). The same `apply --consent C` must refuse — a second W
    would be released with nothing recording it — and point to
    --discard-preview; nothing committed, nothing released."""
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    git(repo, "reset", "-q", "--soft", h0)
    if pruned:
        git(repo, "update-ref", "-d", "ORIG_HEAD")
        git(repo, "reflog", "expire", "--expire=now", "--all")
        git(repo, "gc", "-q", "--prune=now")
        assert not object_exists(repo, w)  # precondition
    kept = slot(engine)
    assert kept["progress"]["work_commit"] == w  # precondition
    before = world(engine, repo, bare)
    doc = refused_apply(cli, repo, c, key=None)
    assert head(repo) == h0, "a second work-in-progress commit was published"
    assert doc["details"]["made"]["work_commit"] in (None, w), doc["details"]["made"]
    assert "--discard-preview" in doc["details"]["error"], doc["details"]["error"]
    assert_untouched(engine, before, repo, bare, slot_before=kept)
    assert_nothing_released(engine, repo, bare, [dirty["wip"]])


def _amend_no_edit(repo, h0, w):
    """W amended --no-edit: the same tree, parent and message, another
    author date — an adoptable W that is NOT the recorded one (what `git
    commit --amend --no-edit` makes of W, built without the user's index)."""
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
           "GIT_AUTHOR_DATE": "2001-01-01T00:00:00", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    amended = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", f"{w}^{{tree}}", "-p", h0],
        input=commit_message(repo, w), env=env, capture_output=True, text=True,
        check=True).stdout.strip()
    git(repo, "update-ref", "-m", "commit (amend)", "HEAD", amended, w)


def _reset_soft(repo, h0, w):
    git(repo, "reset", "-q", "--soft", h0)


@pytest.mark.parametrize("move", [_amend_no_edit, _reset_soft],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_p5_the_plan_and_apply_agree_on_a_recorded_w_head_no_longer_stands_on(
        engine, dirty, cli, at_point, move):
    """The plan's pending_consent follows apply's own rule
    (plan.consent_adoption): with a recorded W that HEAD's chain no longer
    names — an adoptable replacement (W amended --no-edit) or a reset to
    H0 — it is NOT adoptable and exposes no digest, because apply refuses
    that digest with base_moved / progress_lost."""
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    assert slot(engine)["progress"]["work_commit"] == w  # precondition
    move(repo, h0, w)
    now = head(repo)
    assert now != w  # precondition
    if now != h0:  # the replacement is one adoptable_chain itself accepts
        approved = slot(engine)["variants"][c]["consent"]
        assert adoptable_chain(str(repo), h0, approved, now, None) == {
            "work_commit": now, "attest_commit": None}

    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is False, pending
    assert pending["consent_digest"] is None, pending

    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["why"] == "progress_lost", doc["details"]
    assert head(repo) == now
    assert_nothing_released(engine, repo, bare, [dirty["wip"]])


def test_p5_guard_the_plan_names_the_recorded_w_head_still_stands_on(
        engine, dirty, cli, at_point):
    """Guard: HEAD IS the recorded W — adoptable, the digest exposed, and
    apply adopts it."""
    repo, h0 = dirty["repo"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is True and pending["consent_digest"] == c, pending
    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}
    assert git(repo, "rev-list", "--count", f"{h0}..{w}") == "1"


# ── S1: a publication behind a failed ref-update confirmation is recorded ───


@pytest.mark.parametrize("failure", ["git_error", "interrupted"])
def test_s1_a_ref_update_that_fails_after_the_ref_moved_still_records_the_commit(
        engine, dirty, cli, monkeypatch, failure):
    """A `reference-transaction` hook runs its "committed" state AFTER the
    ref moved and before `git update-ref` confirms; if it fails, hangs (a
    timeout) or is interrupted, the compare-and-swap raises although W IS
    published. The pinned ref decides: W is recorded as the slot's
    progress (pending_consent names C), post-commit is not run, and the
    re-run adopts W — never a published W recorded nowhere."""
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    marker = repo.parent / "post-commit-ran"
    install_hook(repo, "post-commit", f"touch {marker}\n")
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    original = effects.gitinfo.git_update_ref_cas

    def published_then_failed(*args, **kw):
        original(*args, **kw)
        monkeypatch.setattr(effects.gitinfo, "git_update_ref_cas", original)
        if failure == "interrupted":
            raise Interrupted("reference-transaction")
        raise effects.gitinfo.GitWriteError("`git update-ref` timed out (stopped)")

    monkeypatch.setattr(effects.gitinfo, "git_update_ref_cas", published_then_failed)
    if failure == "interrupted":
        with pytest.raises(Interrupted):
            apply_consent(cli, repo, c)
    else:
        doc = refused_apply(cli, repo, c, key="published_commit")
        assert doc["details"]["published_commit"] == head(repo)
    w = head(repo)
    assert w != h0 and parents(repo, w) == [h0]  # precondition: W was published
    assert slot(engine)["progress"] == {"work_commit": w, "attest_commit": None, "consent": c}
    assert not marker.exists(), "post-commit ran after an unconfirmed publication"
    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is True and pending["consent_digest"] == c, pending

    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}
    assert git(repo, "rev-list", "--count", f"{h0}..{w}") == "1"  # no second W
    assert_completed(engine, repo, bare, candidate=w, ids=[dirty["wip"]], version="1.1.0")


def test_s1_a_reference_transaction_hook_hanging_after_the_update_records_the_commit(
        engine, dirty, cli, monkeypatch):
    """The real hook: `reference-transaction` sleeps in its "committed"
    state past the supervision timeout — update-ref is stopped after the
    ref moved. W is recorded and the re-run (hook removed) adopts it."""
    repo, bare, h0 = dirty["repo"], dirty["bare"], dirty["h0"]
    c = variant(do_preview(cli, repo, "--message-file", dirty["msg"]))["consent_digest"]
    hook = install_hook(repo, "reference-transaction",
                        'if [ "$1" = committed ] && grep -q " refs/heads/"; then sleep 30; fi\n'
                        'exit 0\n')
    monkeypatch.setattr(effects, "HOOK_GIT_TIMEOUT_SECONDS", 3)
    doc = refused_apply(cli, repo, c, key="published_commit")
    w = head(repo)
    assert w != h0 and doc["details"]["published_commit"] == w
    assert slot(engine)["progress"]["work_commit"] == w
    hook.rename(hook.with_name("reference-transaction.off"))
    out = applied(cli, repo, c)
    assert step(out, "work_commit") == {"step": "work_commit", "result": "adopted", "commit": w}
    assert_completed(engine, repo, bare, candidate=w, ids=[dirty["wip"]], version="1.1.0")
