"""K-76 / K-96 D3 — the ledger side of the approved version-file proposal:
the project's PENDING proposal (set_pending_proposal / get_pending_proposal)
and the stamp that consumes it ATOMICALLY (core release_entries
consume_proposal=True): the record's proposal + consent, the learned
version-file contract and the initial manifest (untracked baseline) are
written in the stamp transaction, or nothing is. Both lanes."""

from __future__ import annotations

import pytest

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import (
    get_pending_proposal,
    get_release,
    get_version_files,
    proposal_digest,
    set_pending_proposal,
    set_version_files,
    validate_proposal,
)

CAND = "c" * 40
BLOB_A, BLOB_B = "a" * 40, "b" * 40


def _proposal(paths=("pyproject.toml",), **extra):
    return {"paths": list(paths), "base_blobs": {p: BLOB_A for p in paths},
            "expected_blobs": {p: BLOB_B for p in paths},
            "script": {"identity": "scripts/dct-version-sync.sh", "sha256": "e" * 64},
            "candidate_sha": CAND, "version": "1.1.0", "plan_digest": "d" * 64,
            "release_mode": "git",
            "changelog": {"path": "changelog.md", "base_blob": BLOB_A, "base_sha256": "f" * 64},
            **extra}


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="A", path="/tmp/proj-a", current_version="1.0.0")
    return engine


def _consent(digest, ids):
    return {"version": "1.1.0", "entry_ids": ids, "candidate_sha": CAND,
            "proposal_sha256": digest, "render": {"pr_refs": False}, "push_target": None}


def test_the_release_context_keys_are_validated():
    full = validate_proposal(_proposal())
    assert full["version"] == "1.1.0" and full["release_mode"] == "git"
    assert full["changelog"] == {"path": "changelog.md", "base_blob": BLOB_A,
                                 "base_sha256": "f" * 64}
    absent = validate_proposal(_proposal(changelog={"path": "changelog.md", "base_blob": None,
                                                    "base_sha256": None}))
    assert absent["changelog"]["base_blob"] is None
    for bad in ({"version": "1.x"}, {"plan_digest": "XYZ"}, {"release_mode": "svn"},
                {"changelog": {"path": "../changelog.md", "base_blob": None,
                               "base_sha256": None}}):
        with pytest.raises((ValueError, TypeError)):
            validate_proposal(_proposal(**bad))


def test_pending_proposal_round_trip_and_replacement(proj):
    assert get_pending_proposal(proj, "proj-a") is None
    first = set_pending_proposal(proj, "proj-a", _proposal())
    assert first["proposal_sha256"] == proposal_digest(_proposal())
    assert get_pending_proposal(proj, "proj-a") == first
    second = set_pending_proposal(proj, "proj-a", _proposal(paths=("VERSION",)))
    assert second["proposal_sha256"] != first["proposal_sha256"]
    assert get_pending_proposal(proj, "proj-a") == second  # a new prepare replaces it
    with pytest.raises((ValueError, TypeError)):
        set_pending_proposal(proj, "proj-a", {"paths": "x"})
    assert get_pending_proposal(proj, "proj-a") == second


def test_the_stamp_consumes_the_pending_proposal_atomically(proj):
    a = add_entry(proj, "proj-a", "added", "a")["id"]
    pending = set_pending_proposal(proj, "proj-a", _proposal())
    out = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=CAND,
                          release_mode="git", consent=_consent(pending["proposal_sha256"], [a]),
                          manifest={"untracked_baseline": ["notes.local"]},
                          consume_proposal=True)
    record = out["release"]
    assert record["proposal"] == pending["proposal"]
    assert record["consent"]["proposal_sha256"] == pending["proposal_sha256"]
    assert record["manifest"] == {"untracked_baseline": ["notes.local"]}
    assert get_version_files(proj, "proj-a")["version_files"] == ["pyproject.toml"]
    assert get_pending_proposal(proj, "proj-a") is None


@pytest.mark.parametrize("case", ["no_pending", "other_digest", "no_digest", "stamp_refused"])
def test_without_the_approved_pending_proposal_nothing_is_written(proj, case):
    a = add_entry(proj, "proj-a", "added", "a")["id"]
    set_version_files(proj, "proj-a", ["VERSION"], confirm=True)
    digest = None
    if case != "no_pending":
        digest = set_pending_proposal(proj, "proj-a", _proposal())["proposal_sha256"]
    consent = _consent({"other_digest": "0" * 64, "no_digest": None}.get(case, digest), [a])
    kwargs = {}
    if case == "stamp_refused":
        kwargs["stampable_ids"] = []  # the plan binding refuses inside the transaction
    with pytest.raises(ValueError):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=CAND,
                        release_mode="git", consent=consent, consume_proposal=True,
                        manifest={"untracked_baseline": []}, **kwargs)
    assert get_release(proj, "proj-a", "1.1.0") is None
    assert get_project(proj, "proj-a")["current_version"] == "1.0.0"
    assert get_version_files(proj, "proj-a")["version_files"] == ["VERSION"]  # kept
    if digest:
        assert get_pending_proposal(proj, "proj-a")["proposal_sha256"] == digest


def test_a_noop_rerun_needs_no_pending_proposal(proj):
    """A recorded release re-run (and a historical no-op) returns before any
    insert: consume_proposal never blocks it."""
    a = add_entry(proj, "proj-a", "added", "a")["id"]
    pending = set_pending_proposal(proj, "proj-a", _proposal())
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=CAND,
                            consent=_consent(pending["proposal_sha256"], [a]),
                            consume_proposal=True)
    again = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], consume_proposal=True)
    assert again["release"]["id"] == first["release"]["id"] and again["released"] == 0
