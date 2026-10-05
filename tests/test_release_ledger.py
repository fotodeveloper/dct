"""The release LEDGER (R6b-1): state machine, immutability, abandonment,
complete record sets, legacy reconstruction, the version-file contract and
the schema heal — K-60, K-61, K-70 (proof map), K-73.

The expectations below are written down by hand from the owner's decisions,
NOT derived from core.releases.TRANSITIONS — a test that recomputes the
table from the code under test would only confirm it.

Runs on in-memory SQLite and on the PG lane.
"""

from __future__ import annotations

import itertools
import re

import pytest
import sqlalchemy as sa
from conftest import ledger_proofs, tag_proof_for
from sqlalchemy import event

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import (
    RELEASE_STATUSES,
    abandon_release,
    advance_release,
    get_release,
    get_version_files,
    reconstruct_release,
    set_version_files,
    validate_proof,
)
from dct.db import changelog_entries, create_tables, projects, releases

CAND = "c" * 40
COMMIT = "d" * 40
OTHER = "e" * 40
MANIFEST = {
    "changelog": {"path": "changelog.md", "sha256": "1" * 64, "section_date": "2026-10-01"},
    "version_files": [{"path": "pyproject.toml", "sha256": "2" * 64}],
    "tree": "3" * 40,
}


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    return engine


def _add(engine, text, slug="proj-a"):
    return add_entry(engine, slug, "added", text)["id"]


def _snapshot(engine):
    """Every changelog, project and release column — proves a call wrote nothing."""
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases)
        )


def _stamp_legacy(engine, entry_id, version, slug="proj-a"):
    """An entry stamped by a release made before records existed."""
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == entry_id)
                     .values(version=version, released_at=sa.func.current_timestamp()))


def _set_current(engine, version, slug="proj-a"):
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == slug)
                     .values(current_version=version))


def _record_count(engine):
    with engine.begin() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(releases)).scalar()


# ── the state machine, pair by pair ──────────────────────────────────────────

FLOW = ["stamped", "files_written", "committed", "tagged", "pushed", "completed"]

# Hand-written from K-60 / K-97: forward along the flow (jumps allowed),
# abandoned from every NON-terminal status, nothing out of completed /
# abandoned. 'pushed' sits between 'tagged' and 'completed'.
LEGAL = {
    "stamped": {"files_written", "committed", "tagged", "pushed", "completed", "abandoned"},
    "files_written": {"committed", "tagged", "pushed", "completed", "abandoned"},
    "committed": {"tagged", "pushed", "completed", "abandoned"},
    "tagged": {"pushed", "completed", "abandoned"},
    "pushed": {"completed", "abandoned"},
    "completed": set(),
    "abandoned": set(),
}


def test_the_status_set_is_the_flow_plus_abandoned():
    assert set(RELEASE_STATUSES) == set(LEGAL)


def _commit_for(status):
    """K-94 B1: from 'committed' on every record not recorded 'without_git'
    carries its release commit (the same value again is a confirmation);
    K-115 G4: from 'tagged' on its tag proof, 'pushed' its push proof."""
    return ledger_proofs(status, COMMIT)


def _reach(engine, status):
    """A version-only release at `status` (with its release commit from 'committed' on)."""
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[])
    if status == "abandoned":
        abandon_release(engine, "proj-a", "1.1.0", "test", confirm=True)
    elif status != "stamped":
        advance_release(engine, "proj-a", "1.1.0", status, **_commit_for(status))


def _move(engine, target):
    if target == "abandoned":
        return abandon_release(engine, "proj-a", "1.1.0", "test", confirm=True)
    return advance_release(engine, "proj-a", "1.1.0", target, **_commit_for(target))


@pytest.mark.parametrize("start,target", list(itertools.product(LEGAL, repeat=2)))
def test_every_status_pair_follows_the_state_machine(proj, start, target):
    _reach(proj, start)
    before = _snapshot(proj)
    if target == start:
        out = _move(proj, target)  # the same status again: a no-op
        assert out["updated"] is False
        assert _snapshot(proj) == before
    elif target in LEGAL[start]:
        out = _move(proj, target)
        assert out["updated"] is True
        rec = get_release(proj, "proj-a", "1.1.0")
        assert rec["status"] == target
        assert (rec["completed_at"] is not None) is (target == "completed")
        assert (rec["abandoned_at"] is not None) is (target == "abandoned")
    else:
        with pytest.raises(ValueError):
            _move(proj, target)
        assert _snapshot(proj) == before


@pytest.mark.parametrize("start", FLOW)
def test_advance_never_reaches_abandoned(proj, start):
    _reach(proj, start)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="abandon_release"):
        advance_release(proj, "proj-a", "1.1.0", "abandoned")
    assert _snapshot(proj) == before


def test_completed_is_never_reclassified_as_abandoned(proj):
    _reach(proj, "completed")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="never reclassified"):
        abandon_release(proj, "proj-a", "1.1.0", "too late", confirm=True)
    assert _snapshot(proj) == before
    assert get_release(proj, "proj-a", "1.1.0")["abandoned_at"] is None


def test_a_terminal_record_accepts_no_new_effects(proj):
    _reach(proj, "completed")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="terminal"):
        advance_release(proj, "proj-a", "1.1.0", "completed", manifest={"late": "x"})
    assert _snapshot(proj) == before


# ── K-61: candidate immutable, release commit separate and set once ──────────


@pytest.fixture
def git_release(proj):
    a = _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=CAND)
    return proj


def test_candidate_is_never_repinned_by_any_path(git_release):
    release_entries(git_release, "proj-a", "1.1.0",
                    entry_ids=get_release(git_release, "proj-a", "1.1.0")["entry_ids"],
                    candidate_sha=OTHER)
    advance_release(git_release, "proj-a", "1.1.0", "committed", release_commit_sha=COMMIT)
    rec = get_release(git_release, "proj-a", "1.1.0")
    assert (rec["candidate_sha"], rec["release_commit_sha"]) == (CAND, COMMIT)


@pytest.mark.parametrize("target", ["stamped", "files_written"])
def test_release_commit_before_committed_is_refused(git_release, target):
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match="'committed' or later"):
        advance_release(git_release, "proj-a", "1.1.0", target, release_commit_sha=COMMIT)
    assert _snapshot(git_release) == before


@pytest.mark.parametrize("target", ["committed", "tagged", "completed"])
def test_a_git_release_cannot_pass_committed_without_its_commit(git_release, target):
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match="without its release commit"):
        advance_release(git_release, "proj-a", "1.1.0", target)
    assert _snapshot(git_release) == before


def test_release_commit_is_set_once(git_release):
    advance_release(git_release, "proj-a", "1.1.0", "committed", release_commit_sha=COMMIT)
    same = advance_release(git_release, "proj-a", "1.1.0", "committed",
                           release_commit_sha=COMMIT.upper())
    assert same["updated"] is False  # confirmation, case-insensitive
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match="cannot change"):
        advance_release(git_release, "proj-a", "1.1.0", "tagged", release_commit_sha=OTHER)
    assert _snapshot(git_release) == before
    # Later steps need not repeat it.
    out = advance_release(git_release, "proj-a", "1.1.0", "tagged",
                          tag_proof=tag_proof_for(COMMIT))
    assert out["release"]["release_commit_sha"] == COMMIT


@pytest.mark.parametrize("target", ["committed", "tagged", "pushed", "completed"])
def test_a_null_candidate_does_not_mean_without_git(proj, target):
    """K-94 B1: a record without candidate_sha and without a verified
    release_mode is NOT a no-git release — it needs its release commit from
    'committed' on, 'completed' included; nothing written on refusal."""
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="without its release commit"):
        advance_release(proj, "proj-a", "1.1.0", target)
    assert _snapshot(proj) == before


def test_only_a_without_git_release_completes_without_a_commit(proj):
    """K-94 B1: 'without_git' completes without a commit — but only with the
    manifest of its verified artifacts, and never through a git-only status."""
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[], release_mode="without_git")
    before = _snapshot(proj)
    for status in ("committed", "tagged", "pushed"):
        with pytest.raises(ValueError, match="without git"):
            advance_release(proj, "proj-a", "1.1.0", status)
    with pytest.raises(ValueError, match="without git"):
        advance_release(proj, "proj-a", "1.1.0", "completed", release_commit_sha=COMMIT,
                        manifest={"tree": "none"})
    with pytest.raises(ValueError, match="manifest"):
        advance_release(proj, "proj-a", "1.1.0", "completed")
    assert _snapshot(proj) == before
    advance_release(proj, "proj-a", "1.1.0", "files_written", manifest={"tree": "none"})
    out = advance_release(proj, "proj-a", "1.1.0", "completed")
    rec = out["release"]
    assert (rec["status"], rec["release_mode"], rec["release_commit_sha"]) == (
        "completed", "without_git", None)


def test_without_git_mode_contradicting_a_candidate_is_refused(proj):
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="without git"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[], candidate_sha=CAND,
                        release_mode="without_git")
    assert _snapshot(proj) == before


def test_release_mode_is_recorded_once_on_a_legacy_record(proj):
    """A record stamped without a mode (an older caller) may record its
    verified mode once; a different mode later is refused (immutable)."""
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    out = advance_release(proj, "proj-a", "1.1.0", "files_written", release_mode="git")
    assert out["release"]["release_mode"] == "git"
    again = advance_release(proj, "proj-a", "1.1.0", "files_written", release_mode="git")
    assert again["updated"] is False
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="release_mode .*immutable"):
        advance_release(proj, "proj-a", "1.1.0", "files_written", release_mode="without_git")
    assert _snapshot(proj) == before


# ── manifest: write-once per key ─────────────────────────────────────────────


def test_manifest_keys_are_added_then_only_confirmed(git_release):
    first = {"changelog": MANIFEST["changelog"]}
    advance_release(git_release, "proj-a", "1.1.0", "files_written", manifest=first)
    out = advance_release(git_release, "proj-a", "1.1.0", "committed",
                          release_commit_sha=COMMIT, manifest={"tree": MANIFEST["tree"]})
    assert out["release"]["manifest"] == {**first, "tree": MANIFEST["tree"]}
    again = advance_release(git_release, "proj-a", "1.1.0", "committed", manifest=first)
    assert again["updated"] is False


@pytest.mark.parametrize("changed", [
    {"changelog": {**MANIFEST["changelog"], "sha256": "f" * 64}},
    {"changelog": {**MANIFEST["changelog"], "section_date": "2026-10-02"}},
    {"changelog": "changelog.md"},
])
def test_a_recorded_manifest_key_is_never_replaced(git_release, changed):
    advance_release(git_release, "proj-a", "1.1.0", "files_written",
                    manifest={"changelog": MANIFEST["changelog"]})
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match="different value"):
        advance_release(git_release, "proj-a", "1.1.0", "files_written", manifest=changed)
    assert _snapshot(git_release) == before


def test_a_refused_manifest_key_does_not_let_the_status_move(git_release):
    advance_release(git_release, "proj-a", "1.1.0", "files_written",
                    manifest={"tree": MANIFEST["tree"]})
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match="different value"):
        advance_release(git_release, "proj-a", "1.1.0", "committed",
                        release_commit_sha=COMMIT, manifest={"tree": OTHER})
    assert _snapshot(git_release) == before


@pytest.mark.parametrize("bad,match", [
    (["changelog"], "JSON object"),
    ("{}", "JSON object"),
    ({"": 1}, "keys"),
    ({"k" * 65: 1}, "keys"),
    ({"tree": None}, "null"),
    ({"blob": object()}, "JSON-serializable"),
    ({"big": "x" * 70000}, "too long"),
])
def test_malformed_manifest_is_refused_without_writes(git_release, bad, match):
    before = _snapshot(git_release)
    with pytest.raises((ValueError, TypeError), match=match):
        advance_release(git_release, "proj-a", "1.1.0", "files_written", manifest=bad)
    assert _snapshot(git_release) == before


def test_empty_manifest_is_no_change(git_release):
    out = advance_release(git_release, "proj-a", "1.1.0", "stamped", manifest={})
    assert out["updated"] is False


# ── K-61 #16: the record holds EVERY entry carrying its version ──────────────


def test_selected_release_omitting_a_legacy_entry_of_V_is_refused_before_stamping(proj):
    legacy, new = _add(proj, "legacy"), _add(proj, "new")
    _stamp_legacy(proj, legacy, "1.1.0")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=rf"missing from the requested set: #{legacy}\b"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[new])
    assert _snapshot(proj) == before


def test_version_only_release_with_a_legacy_entry_of_V_is_refused(proj):
    legacy = _add(proj, "legacy")
    _stamp_legacy(proj, legacy, "1.1.0")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="missing from the requested set"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    assert _snapshot(proj) == before


def test_legacy_spelling_of_V_counts_as_carrying_it(proj):
    legacy, new = _add(proj, "legacy"), _add(proj, "new")
    _stamp_legacy(proj, legacy, "v1.1")
    with pytest.raises(ValueError, match="missing from the requested set"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[new])


def test_the_complete_set_is_recorded_and_a_later_resume_agrees_with_the_db(proj):
    legacy, new = _add(proj, "legacy"), _add(proj, "new")
    _stamp_legacy(proj, legacy, "1.1.0")
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[new, legacy])["release"]
    # The resume check of the workflow: the live entries carrying V are
    # EXACTLY the record's set — no contradiction can block it.
    with proj.begin() as conn:
        carrying = sorted(conn.execute(sa.select(changelog_entries.c.id).where(
            changelog_entries.c.version == "1.1.0")).scalars())
    assert rec["entry_ids"] == carrying == sorted([legacy, new])
    before = _snapshot(proj)
    again = release_entries(proj, "proj-a", "1.1.0", entry_ids=[legacy, new])
    assert again["release"]["id"] == rec["id"]
    assert _snapshot(proj) == before


def test_mode_all_still_includes_legacy_entries_of_V(proj):
    legacy, new = _add(proj, "legacy"), _add(proj, "new")
    _stamp_legacy(proj, legacy, "1.1.0")
    rec = release_entries(proj, "proj-a", "1.1.0")["release"]
    assert rec["entry_ids"] == sorted([legacy, new])


def test_entries_of_other_versions_do_not_count(proj):
    old, new = _add(proj, "old"), _add(proj, "new")
    _stamp_legacy(proj, old, "1.0.0")
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[new])["release"]
    assert rec["entry_ids"] == [new]


# ── proof map (K-70) ─────────────────────────────────────────────────────────


def test_proof_is_stored_in_canonical_form(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    proof = {a: [COMMIT.upper(), CAND, COMMIT],
             str(b): {"commits": [OTHER], "merge_commit": CAND, "pr": 17}}
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a, b], proof=proof)["release"]
    assert rec["proof"] == {
        str(a): {"commits": sorted([CAND, COMMIT]), "merge_commit": None, "pr": None},
        # K-116 H7: a supplied PR number is stored as the caller's declaration.
        str(b): {"commits": [OTHER], "merge_commit": CAND, "pr": 17, "pr_source": "client"},
    }


def test_proof_may_cover_only_part_of_the_set(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a, b], proof={a: [COMMIT]})
    assert set(rec["release"]["proof"]) == {str(a)}


def test_proof_naming_an_entry_outside_the_set_is_refused_without_writes(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=rf"outside the release set: #{b}\b"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], proof={b: [COMMIT]})
    assert _snapshot(proj) == before


def test_a_rerun_with_a_contradicting_proof_is_refused(proj):
    """K-94 B2: the stored proof is immutable — a re-run passing a DIFFERENT
    proof map is refused (never silently ignored); the identical map, or
    none, is the no-op returning the record."""
    a = _add(proj, "a")
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], proof={a: [COMMIT]})
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="proof .*immutable"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], proof={a: [OTHER]})
    assert _snapshot(proj) == before
    same = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], proof={str(a): [COMMIT]})
    assert same["release"] == first["release"]
    bare = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    assert bare["release"] == first["release"]
    assert _snapshot(proj) == before


def test_a_rerun_adding_a_proof_to_a_record_without_one_is_refused(proj):
    a = _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="proof .*immutable"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], proof={a: [COMMIT]})
    assert _snapshot(proj) == before


@pytest.mark.parametrize("bad", [
    ["x"], {True: [COMMIT]}, {0: [COMMIT]}, {"-1": [COMMIT]}, {"1a": [COMMIT]},
    {1: []}, {1: COMMIT}, {1: ["HEAD"]}, {1: {"commits": [COMMIT], "branch": "x"}},
    {1: {"commits": [COMMIT], "pr": True}}, {1: {"commits": [COMMIT], "pr": 0}},
    {1: {"commits": [COMMIT], "merge_commit": "zz"}}, {1: [COMMIT], "1": [COMMIT]},
])
def test_malformed_proof_is_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        validate_proof(bad)


def test_empty_proof_is_none():
    assert validate_proof(None) is None and validate_proof({}) is None


# ── K-60: abandonment keeps every effect ─────────────────────────────────────


def test_abandon_keeps_the_record_the_stamps_and_the_version(git_release):
    advance_release(git_release, "proj-a", "1.1.0", "files_written",
                    manifest={"tree": MANIFEST["tree"]})
    with git_release.begin() as conn:
        entries_before = [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))]
    project_before = get_project(git_release, "proj-a")
    out = abandon_release(git_release, "proj-a", "1.1.0", "  CI broke the tag  ", confirm=True)
    rec = out["release"]
    assert (rec["status"], rec["abandon_reason"]) == ("abandoned", "CI broke the tag")
    assert rec["abandoned_at"] is not None and rec["completed_at"] is None
    assert rec["manifest"] == {"tree": MANIFEST["tree"]} and rec["candidate_sha"] == CAND
    with git_release.begin() as conn:
        assert [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))] == entries_before
    assert get_project(git_release, "proj-a") == project_before  # no rewind
    assert _record_count(git_release) == 1


@pytest.mark.parametrize("kwargs,match", [
    ({"reason": "x"}, "confirm"),
    ({"reason": "x", "confirm": "yes"}, "confirm"),
    ({"reason": "", "confirm": True}, "reason"),
    ({"reason": " \t\n", "confirm": True}, "reason"),
    ({"reason": "x" * 2001, "confirm": True}, "too long"),
])
def test_abandon_needs_confirmation_and_a_reason(git_release, kwargs, match):
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match=match):
        abandon_release(git_release, "proj-a", "1.1.0", **kwargs)
    assert _snapshot(git_release) == before


def test_abandon_of_an_unknown_version_is_refused(proj):
    with pytest.raises(ValueError, match="No release record"):
        abandon_release(proj, "proj-a", "9.9.9", "x", confirm=True)


def test_repeated_abandon_keeps_the_first_reason(git_release):
    """K-94 B5: the same reason again is the compatible no-op; a DIFFERENT
    reason is refused with a message naming the recorded one (the first
    reason and time stay)."""
    first = abandon_release(git_release, "proj-a", "1.1.0", "first", confirm=True)
    before = _snapshot(git_release)
    again = abandon_release(git_release, "proj-a", "1.1.0", " first ", confirm=True)
    assert again["updated"] is False
    assert again["release"] == first["release"]
    with pytest.raises(ValueError, match=r"already abandoned .*'first'.*first reason stays"):
        abandon_release(git_release, "proj-a", "1.1.0", "second", confirm=True)
    assert _snapshot(git_release) == before


def test_an_abandoned_version_is_never_reused_with_a_different_set(git_release):
    a = get_release(git_release, "proj-a", "1.1.0")["entry_ids"][0]
    abandon_release(git_release, "proj-a", "1.1.0", "dead", confirm=True)
    b = _add(git_release, "b")
    before = _snapshot(git_release)
    for selection in ([a, b], [], None):
        with pytest.raises(ValueError, match="different set.*ABANDONED.*NEW version"):
            release_entries(git_release, "proj-a", "1.1.0", entry_ids=selection)
        assert _snapshot(git_release) == before
    # The same set: a no-op that RETURNS the abandoned record (never resumed).
    # (b is withdrawn so mode-all's "every unreleased entry" adds nothing.)
    with git_release.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == b)
                     .values(deleted_at=sa.func.current_timestamp()))
    before = _snapshot(git_release)
    same = release_entries(git_release, "proj-a", "1.1.0", entry_ids=[a])
    assert same["release"]["status"] == "abandoned" and same["released"] == 0
    assert _snapshot(git_release) == before


def test_work_is_released_again_under_a_new_version(git_release):
    abandon_release(git_release, "proj-a", "1.1.0", "dead", confirm=True)
    b = _add(git_release, "redo")
    rec = release_entries(git_release, "proj-a", "1.1.1", entry_ids=[b])["release"]
    assert (rec["version"], rec["status"]) == ("1.1.1", "stamped")


@pytest.mark.parametrize("target", FLOW)
def test_an_abandoned_record_cannot_be_advanced(git_release, target):
    abandon_release(git_release, "proj-a", "1.1.0", "dead", confirm=True)
    before = _snapshot(git_release)
    with pytest.raises(ValueError, match="terminal"):
        advance_release(git_release, "proj-a", "1.1.0", target, release_commit_sha=COMMIT)
    assert _snapshot(git_release) == before


# ── get_release skips BOTH terminal states ───────────────────────────────────


def test_get_unfinished_skips_completed_and_abandoned_but_explicit_reads_show_them(proj):
    for v in ("1.1.0", "1.2.0", "1.3.0"):
        release_entries(proj, "proj-a", v, entry_ids=[])
    assert get_release(proj, "proj-a")["version"] == "1.3.0"
    abandon_release(proj, "proj-a", "1.3.0", "skip", confirm=True)
    assert get_release(proj, "proj-a")["version"] == "1.2.0"
    advance_release(proj, "proj-a", "1.2.0", "completed",
                    **ledger_proofs("completed", COMMIT, "1.2.0"))
    assert get_release(proj, "proj-a")["version"] == "1.1.0"
    abandon_release(proj, "proj-a", "1.1.0", "skip", confirm=True)
    assert get_release(proj, "proj-a") is None
    assert get_release(proj, "proj-a", "1.3.0")["status"] == "abandoned"
    assert get_release(proj, "proj-a", "1.2.0")["status"] == "completed"


# ── K-60: controlled reconstruction of a legacy release ──────────────────────


@pytest.fixture
def legacy(proj):
    """1.1.0 released before records existed: two entries stamped, current 1.1.0."""
    a, b = _add(proj, "a"), _add(proj, "b")
    _stamp_legacy(proj, a, "1.1.0")
    _stamp_legacy(proj, b, "1.1.0")
    _set_current(proj, "1.1.0")
    return proj, sorted([a, b])


def _reconstruct(engine, ids, **overrides):
    kwargs = {"entry_ids": ids, "candidate_sha": CAND, "release_commit_sha": COMMIT,
              "manifest": MANIFEST, "status": "completed", "confirm": True}
    kwargs.update(overrides)
    version = kwargs.pop("version", "1.1.0")
    if kwargs["status"] in ("tagged", "completed") and not kwargs.get("without_git") \
            and kwargs["release_commit_sha"]:
        # K-115 G4: with git, 'tagged' and later carry the verified tag.
        kwargs.setdefault("tag_proof", tag_proof_for(kwargs["release_commit_sha"],
                                                     f"v{version}"))
    return reconstruct_release(engine, "proj-a", version, **kwargs)


def test_reconstruction_records_without_restamping(legacy):
    engine, ids = legacy
    with engine.begin() as conn:
        entries_before = [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))]
    project_before = get_project(engine, "proj-a")
    rec = _reconstruct(engine, ids, checkout_root="/tmp/proj-a/")
    assert (rec["mode"], rec["status"], rec["entry_ids"]) == ("reconstructed", "completed", ids)
    assert (rec["candidate_sha"], rec["release_commit_sha"]) == (CAND, COMMIT)
    assert rec["manifest"] == MANIFEST and rec["checkout_root"] == "/tmp/proj-a"
    assert rec["completed_at"] is not None
    with engine.begin() as conn:
        assert [tuple(r) for r in conn.execute(
            sa.select(changelog_entries).order_by(changelog_entries.c.id))] == entries_before
    assert get_project(engine, "proj-a") == project_before
    # From now on the version behaves like any recorded one.
    again = release_entries(engine, "proj-a", "1.1.0", entry_ids=ids)
    assert again["release"]["id"] == rec["id"]


def test_reconstruction_of_an_unfinished_legacy_release_can_be_resumed(legacy):
    engine, ids = legacy
    _reconstruct(engine, ids, release_commit_sha=None, manifest={"tree": "3" * 40},
                 status="files_written")
    assert get_release(engine, "proj-a")["version"] == "1.1.0"
    out = advance_release(engine, "proj-a", "1.1.0", "committed", release_commit_sha=COMMIT)
    assert out["release"]["status"] == "committed"


def test_reconstruction_of_a_lower_version_is_allowed(legacy):
    engine, ids = legacy
    _set_current(engine, "1.2.0")
    assert _reconstruct(engine, ids)["version"] == "1.1.0"


def test_version_only_reconstruction_of_the_current_version(proj):
    rec = _reconstruct(proj, [], version="1.0.0")
    assert (rec["version"], rec["entry_ids"]) == ("1.0.0", [])


@pytest.mark.parametrize("case", [
    "no-confirm", "confirm-truthy", "ids-none", "ids-subset", "ids-superset",
    "ids-other-version", "higher-than-current", "lower-version-only", "existing-record",
    "no-candidate", "no-commit-for-completed", "commit-before-committed",
    "no-manifest", "abandoned-status", "unknown-status",
    "without-git-with-sha", "without-git-tagged", "without-git-pushed",
    "unparseable-current", "bad-sha",
])
def test_reconstruction_refuses_every_inconsistency_without_writes(legacy, case):
    engine, ids = legacy
    other = _add(engine, "other")
    overrides: dict = {}
    if case == "no-confirm":
        overrides["confirm"] = False
    elif case == "confirm-truthy":
        overrides["confirm"] = 1
    elif case == "ids-none":
        overrides["entry_ids"] = None
    elif case == "ids-subset":
        overrides["entry_ids"] = ids[:1]
    elif case == "ids-superset":
        overrides["entry_ids"] = [*ids, other]
    elif case == "ids-other-version":
        overrides["entry_ids"] = [other]
    elif case == "higher-than-current":
        _stamp_legacy(engine, other, "1.2.0")
        overrides.update(version="1.2.0", entry_ids=[other])
    elif case == "lower-version-only":
        overrides.update(version="1.0.0", entry_ids=[])
    elif case == "existing-record":
        _reconstruct(engine, ids)
    elif case == "no-candidate":
        overrides["candidate_sha"] = None
    elif case == "no-commit-for-completed":
        overrides["release_commit_sha"] = None
    elif case == "commit-before-committed":
        overrides["status"] = "files_written"
    elif case == "no-manifest":
        overrides["manifest"] = None
    elif case == "abandoned-status":
        overrides["status"] = "abandoned"
    elif case == "unknown-status":
        overrides["status"] = "released"
    elif case == "without-git-with-sha":
        overrides["without_git"] = True
    elif case == "without-git-tagged":
        overrides.update(without_git=True, candidate_sha=None, release_commit_sha=None,
                         status="tagged")
    elif case == "without-git-pushed":
        overrides.update(without_git=True, candidate_sha=None, release_commit_sha=None,
                         status="pushed")
    elif case == "unparseable-current":
        _set_current(engine, "2.0.0-rc1")
    elif case == "bad-sha":
        overrides["candidate_sha"] = "HEAD"
    before = _snapshot(engine)
    with pytest.raises(ValueError):
        _reconstruct(engine, ids, **overrides)
    assert _snapshot(engine) == before


def test_reconstruction_without_git(legacy):
    engine, ids = legacy
    # K-110: without git the manifest carries the scope of the verification
    # (the CONFIRMED contract) — `version_files` alone is not evidence.
    scoped = {**MANIFEST, "verification": {"contract": ["pyproject.toml"]}}
    with pytest.raises(ValueError, match="verification"):
        _reconstruct(engine, ids, candidate_sha=None, release_commit_sha=None,
                     without_git=True)
    rec = _reconstruct(engine, ids, candidate_sha=None, release_commit_sha=None,
                       without_git=True, manifest=scoped)
    assert rec["candidate_sha"] is None and rec["status"] == "completed"
    assert rec["manifest"]["verification"] == {"contract": ["pyproject.toml"]}


def test_reconstruction_uses_the_stored_spelling(proj):
    a = _add(proj, "a")
    _stamp_legacy(proj, a, "v1.1")
    _set_current(proj, "1.1")
    rec = _reconstruct(proj, [a], version="1.1.0")
    assert rec["version"] == "1.1"


def test_reconstruction_names_both_sets_on_mismatch(legacy):
    engine, ids = legacy
    with pytest.raises(ValueError) as exc:
        _reconstruct(engine, ids[:1])
    assert str(ids) in str(exc.value) and str(ids[:1]) in str(exc.value)


# ── K-73: the project's version-file contract ────────────────────────────────


def test_contract_is_unlearned_until_confirmed(proj):
    assert get_version_files(proj, "proj-a") == {
        "version_files": None, "confirmed_at": None, "history": []}
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="confirm"):
        set_version_files(proj, "proj-a", ["pyproject.toml"])
    with pytest.raises(ValueError, match="confirm"):
        set_version_files(proj, "proj-a", ["pyproject.toml"], confirm="yes")
    assert _snapshot(proj) == before


def test_contract_set_confirm_and_change_keeps_a_history(proj):
    first = set_version_files(proj, "proj-a", ["pyproject.toml", "src/x/__init__.py",
                                               "pyproject.toml"], confirm=True)
    assert first["updated"] is True
    assert first["version_files"] == ["pyproject.toml", "src/x/__init__.py"]
    assert first["confirmed_at"] is not None and first["history"] == []
    before = _snapshot(proj)
    same = set_version_files(proj, "proj-a", ["src/x/__init__.py", "pyproject.toml"],
                             confirm=True)
    assert same["updated"] is False and same["confirmed_at"] == first["confirmed_at"]
    assert _snapshot(proj) == before
    changed = set_version_files(proj, "proj-a", ["package.json"], confirm=True)
    assert changed["version_files"] == ["package.json"]
    [entry] = changed["history"]
    assert entry["version_files"] == ["pyproject.toml", "src/x/__init__.py"]
    assert entry["confirmed_at"] is not None and entry["replaced_at"] is not None
    third = set_version_files(proj, "proj-a", [], confirm=True)
    assert third["version_files"] == []
    assert [h["version_files"] for h in third["history"]] == [
        ["pyproject.toml", "src/x/__init__.py"], ["package.json"]]


@pytest.mark.parametrize("bad", [
    "pyproject.toml", None, [""], [" a"], ["a "], ["/abs/path"], ["C:/x"], ["c:x"],
    ["a\\b"], ["../up"], ["a/../b"], ["./a"], ["a//b"], ["a/"], [1],
    ["x" * 501], [f"f{i}" for i in range(201)],
])
def test_invalid_version_file_paths_are_refused_without_writes(proj, bad):
    before = _snapshot(proj)
    with pytest.raises(ValueError):
        set_version_files(proj, "proj-a", bad, confirm=True)
    assert _snapshot(proj) == before


def test_contract_is_project_scoped(proj):
    create_project(proj, slug="proj-b", name="B", path="/tmp/proj-b")
    set_version_files(proj, "proj-a", ["VERSION"], confirm=True)
    assert get_version_files(proj, "proj-b")["version_files"] is None


# ── schema heal on an existing database: three bootstraps ────────────────────

_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
NEW_COLUMNS = {
    "releases": ["release_commit_sha", "manifest", "proof", "abandoned_at", "abandon_reason",
                 "release_mode", "executor", "consent", "proposal", "render"],
    "projects": ["version_files", "version_files_confirmed_at", "version_files_history",
                 "release_executor", "changelog_pr_refs", "release_proposal"],
}


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_three_bootstraps_heal_the_ledger_columns_then_converge(proj, capsys):
    a = _add(proj, "a")
    old = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=CAND)["release"]
    with proj.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            for column in columns:
                conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    for table, columns in NEW_COLUMNS.items():
        assert not set(columns) & _columns(proj, table)

    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.strip())

    event.listen(proj, "before_cursor_execute", _listener)
    try:
        create_tables(proj)  # bootstrap 1 — adds every missing column
        added = [s for s in seen if _DDL.match(s)]
        for table, columns in NEW_COLUMNS.items():
            assert set(columns) <= _columns(proj, table)
            for column in columns:
                assert any(table in s and column in s for s in added), (table, column)
        capsys.readouterr()
        for n in (2, 3):
            seen.clear()
            create_tables(proj)
            assert [s for s in seen if _DDL.match(s)] == [], f"bootstrap {n} issued DDL"
            writes = [s for s in seen if _DML.match(s)
                      and re.search(r"\b(releases|projects)\b", s, re.IGNORECASE)]
            assert writes == [], f"bootstrap {n} wrote: {writes}"
            out = capsys.readouterr()
            assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
    finally:
        event.remove(proj, "before_cursor_execute", _listener)

    rec = get_release(proj, "proj-a", "1.1.0")
    assert {k: rec[k] for k in ("id", "entry_ids", "candidate_sha", "status")} == {
        k: old[k] for k in ("id", "entry_ids", "candidate_sha", "status")}
    assert (rec["release_commit_sha"], rec["manifest"], rec["proof"]) == (None, None, None)
    assert get_version_files(proj, "proj-a")["version_files"] is None
    # The NOT NULL settings healed onto the existing project with their defaults.
    project = get_project(proj, "proj-a")
    assert (project["release_executor"], project["changelog_pr_refs"]) == ("dct", False)
    assert (rec["release_mode"], rec["executor"], rec["consent"], rec["render"]) == (
        None, None, None, None)
    # The healed columns are usable.
    advance_release(proj, "proj-a", "1.1.0", "committed", release_commit_sha=COMMIT,
                    manifest={"tree": "3" * 40})
    abandon_release(proj, "proj-a", "1.1.0", "after heal", confirm=True)
    set_version_files(proj, "proj-a", ["VERSION"], confirm=True)
