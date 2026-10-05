"""Round 4 — the alias and the pinned blobs (K-117, K-116 H2).

- The `clog release` alias REQUIRES an approved, specific proposal
  (--proposal-digest): equal paths and a checked version field never stand
  in for the owner's consent to the content, and the approved proposal must
  be bound to the exact plan the alias derives (H2).
- Every blob a proposal needs is pinned by a reachable git object — a ref
  under refs/dct/release-proposals/<project>/<proposal id> — from prepare
  until the release is completed / abandoned or the pending proposal is
  replaced; only resources no other proposal or record uses are cleaned
  (identical files share a blob). Same lifetime for the non-git blob store.
- J2 / J3: the version sync runs isolated, only the candidate's script; a
  dependency missing from the isolated copy is a CLEAR error naming the
  environment contract — never a silent run in the user's checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import approved_plan_digest, approved_push, git, push_argv, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    apply,
    assert_released,
    interrupt_at,
    make_release_repo,
    proven,
    sync_runs,
    use_cli,
)

from dct import paths as dct_paths
from dct.core.changelog import add_entry
from dct.core.projects import create_project
from dct.core.releases import get_pending_proposal, get_release
from dct.workflows.release import effects
from dct.workflows.release.effects import ReleaseRefused, SyncFailed

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

PIN_PREFIX = "refs/dct/release-proposals/"


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """Every test here creates commits through the effects layer."""


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


def prepare(engine, repo, **selection) -> dict:
    digest = approved_plan_digest(engine, SLUG, repo, **selection)
    return effects.prepare_release(engine, SLUG, cwd=str(repo), plan_digest=digest,
                                   **selection)["details"]


def alias(engine, repo, **kw):
    """The alias with the approved push choice (K-123, conftest.approved_push)."""
    return effects.apply_release(engine, SLUG, strict=True, confirm=True, cwd=str(repo),
                                 **approved_push(repo), **kw)


def pins(repo: Path) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/dct")
    return dict(line.split(" ") for line in out.splitlines())


def gc(repo: Path) -> None:
    """Drop every unreachable object NOW (what `git gc` does after its grace
    period)."""
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")


def has_object(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "--batch-check", "--batch-all-objects").find(sha) != -1


# ── the alias needs the approved proposal ────────────────────────────────────


def test_the_alias_never_approves_a_proposal_by_itself(engine, proj):
    """The confirmed contract equals what the sync would change — still no
    consent to the CONTENT: without --proposal-digest nothing is prepared,
    stamped or run."""
    repo, log = proj
    a = proven(engine, repo, "feature a")
    with pytest.raises(ReleaseRefused, match="--proposal-digest") as exc:
        alias(engine, repo, version="1.1.0")
    assert "dct release prepare" in str(exc.value)
    assert get_release(engine, SLUG, "1.1.0") is None
    assert get_pending_proposal(engine, SLUG) is None  # the alias prepared nothing
    assert sync_runs(log) == [] and pins(repo) == {}
    assert a  # the entry stays unreleased
    from dct.core.changelog import list_entries

    assert [e["version"] for e in list_entries(engine, SLUG) if e["id"] == a] == [None]


def test_the_alias_releases_the_approved_proposal_of_its_own_plan(engine, proj):
    repo, log = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    out = alias(engine, repo, version="1.1.0", proposal_digest=details["proposal_sha256"])
    assert out["outcome"] == "completed"
    assert_released(engine, repo, candidate, [a])
    assert sync_runs(log) == ["1.1.0"]  # once, in prepare — never inside the alias


def test_the_alias_refuses_a_proposal_bound_to_another_plan(engine, proj):
    """H2: the alias takes no --plan-digest — the approved proposal carries
    the plan it was prepared for, and it must be the plan the alias derives
    (here: prepared for a SELECTION, the alias releases every entry)."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    b = proven(engine, repo, "feature b")
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(ReleaseRefused, match="plan") as exc:
        alias(engine, repo, version="1.1.0", proposal_digest=details["proposal_sha256"])
    assert "plan_digest" in exc.value.details["mismatch"]
    assert get_release(engine, SLUG, "1.1.0") is None and b


def test_cli_alias_without_the_proposal_digest_names_the_procedure(engine, proj, monkeypatch,
                                                                   capsys):
    repo, _ = use_cli(proj, engine, monkeypatch)
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["clog", "release", "-P", SLUG, "1.1.0", "--confirm", "--json"])
    assert exc.value.code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["outcome"] == "refused" and "--proposal-digest" in doc["details"]["error"]
    assert get_release(engine, SLUG, "1.1.0") is None
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    run_cli(monkeypatch, ["clog", "release", "-P", SLUG, "1.1.0", "--confirm",
                          "--proposal-digest", details["proposal_sha256"], *push_argv(repo)])
    assert_released(engine, repo, candidate, [a])


# ── blobs pinned by a reachable git object ───────────────────────────────────


def test_prepare_pins_the_proposal_under_a_project_and_proposal_named_ref(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    proposal = details["proposal"]
    identity = proposal.get("id")
    assert identity and identity not in (proposal["version"], details["proposal_sha256"])
    refs = pins(repo)
    assert list(refs) == [f"{PIN_PREFIX}s-{SLUG}/{identity}"]  # the project + the proposal
    assert "1.1.0" not in next(iter(refs))  # … not the version alone
    expected = proposal["expected_blobs"]["pyproject.toml"]
    gc(repo)  # what would have pruned an unreachable approved blob
    assert has_object(repo, expected)
    apply(engine, repo, version="1.1.0", entry_ids=[a],
          plan_digest=details["plan_digest"], proposal_digest=details["proposal_sha256"])
    assert_released(engine, repo, candidate, [a])


def test_a_second_prepare_gets_its_own_identity_even_for_the_same_content(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    first = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    second = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert first["proposal"]["expected_blobs"] == second["proposal"]["expected_blobs"]
    assert first["proposal"].get("id") != second["proposal"].get("id")
    # The replaced pending proposal's pin is gone; the new one holds the blob.
    assert list(pins(repo)) == [f"{PIN_PREFIX}s-{SLUG}/{second['proposal'].get('id')}"]
    gc(repo)
    assert has_object(repo, second["proposal"]["expected_blobs"]["pyproject.toml"])


@pytest.mark.parametrize("ending", ["completed", "abandoned"])
def test_the_pin_lasts_until_the_release_ends(engine, proj, ending):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    ref = f"{PIN_PREFIX}s-{SLUG}/{details['proposal'].get('id')}"
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=details["plan_digest"],
              proposal_digest=details["proposal_sha256"], on_point=interrupt_at("after_stamp"))
    assert list(pins(repo)) == [ref]  # consumed by the stamp: still pinned
    gc(repo)
    if ending == "completed":
        assert effects.resume_release(engine, SLUG, cwd=str(repo))["outcome"] == "completed"
    else:
        effects.abandon(engine, SLUG, "1.1.0", "stop", confirm=True)
    assert pins(repo) == {}


def test_ending_a_release_removes_only_its_own_pin(engine, proj, tmp_path):
    """Another proposal (another project in the same repository) pins the
    SAME blob under its own ref: completing this release must leave that
    ref — and the shared blob — alone."""
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    shared = details["proposal"]["expected_blobs"]["pyproject.toml"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_INDEX_FILE"] = str(tmp_path / "other-index")
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                    f"100644,{shared},expected/pyproject.toml"], env=env, check=True)
    tree = subprocess.run(["git", "-C", str(repo), "write-tree"], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()
    other = f"{PIN_PREFIX}s-proj-other/01OTHERPROPOSAL"
    git(repo, "update-ref", other, tree)
    apply(engine, repo, version="1.1.0", entry_ids=[a], plan_digest=details["plan_digest"],
          proposal_digest=details["proposal_sha256"])
    assert pins(repo) == {other: tree}
    gc(repo)
    assert has_object(repo, shared)


def test_the_pin_ref_does_not_disturb_history_readers(engine, proj):
    repo, _ = proj
    a = proven(engine, repo, "feature a")
    details = prepare(engine, repo, version="1.1.0", entry_ids=[a])
    assert pins(repo)
    assert "release" not in git(repo, "log", "--all", "--format=%s")
    from dct.workflows.release import build_plan

    plan = build_plan(engine, SLUG, cwd=str(repo), version="1.1.0", entry_ids=[a])
    assert plan["snapshot_digest"] == details["plan_digest"]  # the pin changes no plan


# ── the non-git blob store has the same lifetime ─────────────────────────────


def plain_project(engine, tmp_path, slug: str) -> Path:
    root = tmp_path / slug
    (root / "scripts").mkdir(parents=True)
    (root / "changelog.md").write_text(f"# Changelog of {slug}\n")
    (root / "VERSION").write_text("1.0.0\n")
    (root / "scripts" / "dct-version-sync.sh").write_text('#!/bin/bash\necho "$1" > VERSION\n')
    create_project(engine, slug=slug, name=slug, path=str(root), current_version="1.0.0")
    return root


def stored() -> set[str]:
    root = Path(dct_paths.release_store_dir())
    return {p.name for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def plain_prepare(engine, slug: str, root: Path) -> tuple[dict, int]:
    eid = add_entry(engine, slug, "added", "feature")["id"]
    digest = approved_plan_digest(engine, slug, root, version="1.1.0", entry_ids=[eid])
    out = effects.prepare_release(engine, slug, version="1.1.0", entry_ids=[eid],
                                  plan_digest=digest, cwd=str(root))
    return out["details"], eid


def plain_apply(engine, slug: str, root: Path, details: dict, eid: int, **kw):
    return effects.apply_release(
        engine, slug, version="1.1.0", entry_ids=[eid], confirm=True, cwd=str(root),
        plan_digest=details["plan_digest"], proposal_digest=details["proposal_sha256"], **kw)


def test_the_blob_store_keeps_what_another_proposal_still_uses(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("DCT_DATA_DIR", str(tmp_path / "data"))
    one, two = plain_project(engine, tmp_path, "proj-one"), plain_project(engine, tmp_path,
                                                                          "proj-two")
    shared = hashlib.sha256(b"1.1.0\n").hexdigest()  # both sync to the same VERSION
    own = {slug: hashlib.sha256(f"# Changelog of {slug}\n".encode()).hexdigest()
           for slug in ("proj-one", "proj-two")}
    d1, e1 = plain_prepare(engine, "proj-one", one)
    d2, e2 = plain_prepare(engine, "proj-two", two)
    assert stored() == {shared, *own.values()}
    assert plain_apply(engine, "proj-one", one, d1, e1)["outcome"] == "completed"
    # proj-one's own baseline is gone; the content proj-two's PENDING
    # proposal still needs stays — identical files share one blob.
    assert stored() == {shared, own["proj-two"]}
    assert plain_apply(engine, "proj-two", two, d2, e2)["outcome"] == "completed"
    assert (two / "VERSION").read_text() == "1.1.0\n"
    assert stored() == set()


def test_the_blob_store_is_cleaned_on_abandon_and_on_a_replacing_prepare(engine, tmp_path,
                                                                         monkeypatch):
    monkeypatch.setenv("DCT_DATA_DIR", str(tmp_path / "data"))
    root = plain_project(engine, tmp_path, "proj-one")
    first, e1 = plain_prepare(engine, "proj-one", root)
    before = stored()
    # A new prepare (another version) REPLACES the pending proposal.
    digest = approved_plan_digest(engine, "proj-one", root, version="1.2.0", entry_ids=[e1])
    second = effects.prepare_release(engine, "proj-one", version="1.2.0", entry_ids=[e1],
                                     plan_digest=digest, cwd=str(root))["details"]
    replaced = first["proposal"]["expected_blobs"]["VERSION"]
    kept = second["proposal"]["expected_blobs"]["VERSION"]
    assert replaced != kept and replaced in before
    assert replaced not in stored() and kept in stored()  # the replaced proposal's blob is gone
    with pytest.raises(Interrupted):
        effects.apply_release(
            engine, "proj-one", version="1.2.0", entry_ids=[e1], confirm=True, cwd=str(root),
            plan_digest=digest, proposal_digest=second["proposal_sha256"],
            on_point=interrupt_at("after_stamp"))
    assert kept in stored()  # consumed by the stamp: still needed by the record
    effects.abandon(engine, "proj-one", "1.2.0", "stop", confirm=True)
    assert stored() == set()


# ── J2 / J3: the environment contract of the isolated sync ───────────────────


def test_a_dependency_missing_from_the_isolated_copy_is_a_clear_error(engine, proj, tmp_path):
    """The script needs an UNTRACKED file of the checkout (a local venv, a
    build output): the isolated worktree does not have it. The failure names
    the environment contract; the script is never re-run in place."""
    repo, _ = proj
    in_place = tmp_path / "ran-in.log"
    (repo / ".gitignore").write_text("local-tool/\n")
    (repo / "scripts" / "dct-version-sync.sh").write_text(
        "#!/bin/bash\nset -e\n"
        f'pwd -P >> "{in_place}"\n'
        'bash local-tool/bump.sh "$1"\n')
    git(repo, "add", ".gitignore", "scripts/dct-version-sync.sh")
    git(repo, "commit", "-q", "-m", "chore: sync needs a local tool")
    (repo / "local-tool").mkdir()
    (repo / "local-tool" / "bump.sh").write_text(
        'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n')
    a = proven(engine, repo, "feature a")
    before = (repo / "pyproject.toml").read_bytes()
    digest = approved_plan_digest(engine, SLUG, repo, version="1.1.0", entry_ids=[a])
    with pytest.raises(SyncFailed, match="isolated") as exc:
        effects.prepare_release(engine, SLUG, version="1.1.0", entry_ids=[a],
                                plan_digest=digest, cwd=str(repo))
    message = str(exc.value)
    assert "untracked" in message and "never" in message  # the contract, and no fallback
    contract = exc.value.details["environment"]
    assert contract["isolated"] is True and contract["in_place_fallback"] is False
    assert contract["cwd"] != str(repo) and "candidate" in contract["files"]
    assert "local-tool/bump.sh" in exc.value.details["sync"]["output"]
    # It ran exactly once and NOT in the checkout; the checkout is untouched.
    ran_in = in_place.read_text().split()
    assert len(ran_in) == 1 and Path(ran_in[0]) != repo.resolve()
    assert (repo / "pyproject.toml").read_bytes() == before
    assert get_pending_proposal(engine, SLUG) is None and pins(repo) == {}
