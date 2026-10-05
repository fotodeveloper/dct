"""Durable release records (K-41 variant b) — core layer.

A release record is written in the SAME transaction as the stamp
(release_entries) and then advanced forward-only by the release workflow
(advance_release): stamped → files_written → committed → tagged → completed.
A re-run of a recorded version with the same entry set is a whole-operation
no-op that RETURNS the record (resume); a different set is refused.
"""

import json

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import (
    RELEASE_STATUSES,
    advance_release,
    get_release,
)
from dct.db import changelog_entries, projects, releases

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"


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


def _records(engine):
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(
            sa.select(releases).order_by(releases.c.id))]


def _forget_records(engine):
    """Turn every release into an UNRECORDED legacy one (pre-K-41 history)."""
    with engine.begin() as conn:
        conn.execute(releases.delete())


# ── record written with the stamp ─────────────────────────────────────────────


def test_release_writes_a_stamped_record_with_the_exact_set(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    out = release_entries(proj, "proj-a", "1.1.0", candidate_sha=SHA,
                          checkout_root="/tmp/proj-a/")
    rec = out["release"]
    assert rec["version"] == "1.1.0"
    assert rec["mode"] == "all"
    assert rec["entry_ids"] == sorted([a, b])
    assert rec["candidate_sha"] == SHA
    assert rec["checkout_root"] == "/tmp/proj-a"  # normalize_worktree
    assert rec["status"] == "stamped"
    assert rec["completed_at"] is None
    assert rec["created_at"] is not None and rec["updated_at"] is not None
    [row] = _records(proj)
    assert json.loads(row["entry_ids"]) == sorted([a, b])
    assert row["deleted_at"] is None


def test_selected_release_records_only_the_selection(proj):
    a, _b, c = _add(proj, "a"), _add(proj, "b"), _add(proj, "c")
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[c, a, c])["release"]
    assert (rec["mode"], rec["entry_ids"]) == ("selected", sorted([a, c]))


def test_version_only_release_records_an_empty_set(proj):
    _add(proj, "left unreleased")
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[])["release"]
    assert (rec["mode"], rec["entry_ids"], rec["status"]) == ("version_only", [], "stamped")
    assert json.loads(_records(proj)[0]["entry_ids"]) == []


def test_release_without_sha_or_root_stores_nulls(proj):
    _add(proj, "a")
    rec = release_entries(proj, "proj-a", "1.1.0")["release"]
    assert rec["candidate_sha"] is None and rec["checkout_root"] is None


def test_record_version_is_the_normalized_one(proj):
    _add(proj, "a")
    assert release_entries(proj, "proj-a", "v1.1.0")["release"]["version"] == "1.1.0"


def test_record_carries_provenance_columns(proj):
    _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0")
    row = _records(proj)[0]
    for column in ("actor_type", "actor_client", "session_id", "source"):
        assert column in row


# ── atomicity: never a stamp without a record, never a record without a stamp ─


def _fail_on(engine, needle):
    def _listener(conn, cursor, statement, parameters, context, executemany):
        if needle in " ".join(statement.split()).upper():
            raise RuntimeError(f"injected failure on {needle}")
    event.listen(engine, "before_cursor_execute", _listener)
    return _listener


@pytest.mark.parametrize("needle", ["INSERT INTO RELEASES", "UPDATE PROJECTS"])
def test_failure_after_stamping_rolls_back_stamp_and_record(proj, needle):
    _add(proj, "a")
    before = _snapshot(proj)
    listener = _fail_on(proj, needle)
    try:
        with pytest.raises(Exception, match="injected failure"):
            release_entries(proj, "proj-a", "1.1.0")
    finally:
        event.remove(proj, "before_cursor_execute", listener)
    assert _snapshot(proj) == before
    assert list_entries(proj, "proj-a", unreleased_only=True)  # still unreleased
    assert _records(proj) == []


def test_failure_while_stamping_writes_no_record(proj):
    _add(proj, "a")
    before = _snapshot(proj)
    listener = _fail_on(proj, "UPDATE CHANGELOG_ENTRIES")
    try:
        with pytest.raises(Exception, match="injected failure"):
            release_entries(proj, "proj-a", "1.1.0")
    finally:
        event.remove(proj, "before_cursor_execute", listener)
    assert _snapshot(proj) == before


def test_failure_on_version_only_record_leaves_version_untouched(proj):
    before = _snapshot(proj)
    listener = _fail_on(proj, "INSERT INTO RELEASES")
    try:
        with pytest.raises(Exception, match="injected failure"):
            release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    finally:
        event.remove(proj, "before_cursor_execute", listener)
    assert _snapshot(proj) == before
    assert get_project(proj, "proj-a")["current_version"] == "1.0.0"


@pytest.mark.parametrize("kind", ["refused", "noop-legacy"])
def test_no_record_without_a_stamp(proj, kind):
    """A refused call and a legacy no-op (nothing to stamp) write no record."""
    if kind == "refused":
        _add(proj, "a")
        with pytest.raises(ValueError, match="lower than the current version"):
            release_entries(proj, "proj-a", "0.9.0")
    else:
        out = release_entries(proj, "proj-a", "1.0.0")  # nothing unreleased, V current
        assert out["release"] is None
    assert _records(proj) == []


# ── re-runs: same set → no-op returning the record; different set → refused ───


def test_same_set_rerun_returns_the_record_without_any_write(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a, b], candidate_sha=SHA)
    before = _snapshot(proj)
    again = release_entries(proj, "proj-a", "1.1.0", entry_ids=[b, a])
    assert _snapshot(proj) == before
    assert again["released"] == 0
    assert again["release"] == first["release"]


def test_same_set_rerun_in_mode_all_returns_the_record(proj):
    _add(proj, "a"), _add(proj, "b")
    first = release_entries(proj, "proj-a", "1.1.0")
    before = _snapshot(proj)
    again = release_entries(proj, "proj-a", "1.1.0")
    assert _snapshot(proj) == before
    assert again["release"]["id"] == first["release"]["id"]
    assert again["released"] == 0


def test_same_set_rerun_of_a_version_only_release_returns_the_record(proj):
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    before = _snapshot(proj)
    again = release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    assert _snapshot(proj) == before
    assert again["release"]["id"] == first["release"]["id"]


@pytest.mark.parametrize("mode", ["selected", "version_only", "all"])
def test_same_set_rerun_of_a_lower_recorded_version_is_a_noop_not_a_rewind(proj, mode):
    """The record replaces the 'historical retry refused as rewind' gap."""
    a = _add(proj, "a")
    selection = {"selected": [a], "version_only": [], "all": None}[mode]
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=selection)
    _add(proj, "later")
    release_entries(proj, "proj-a", "1.2.0", entry_ids=[] if mode == "version_only" else None)
    # mode 'all' resolves to "entries carrying 1.1.0 + every unreleased
    # entry" — nothing is unreleased any more, so it equals the record's set.
    before = _snapshot(proj)
    again = release_entries(proj, "proj-a", "1.1.0", entry_ids=selection)
    assert _snapshot(proj) == before
    assert again["release"]["id"] == first["release"]["id"]
    assert again["current_version"] == "1.2.0"
    assert again["version"] == "1.1.0"


def test_recorded_version_only_release_lower_than_current_is_a_noop(proj):
    """Without the record a lower version-only call is a rewind; with it, a resume."""
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    release_entries(proj, "proj-a", "1.2.0", entry_ids=[])
    out = release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    assert out["release"]["version"] == "1.1.0"
    assert get_project(proj, "proj-a")["current_version"] == "1.2.0"


@pytest.mark.parametrize("second", ["superset", "version_only", "all-with-unreleased"])
def test_different_set_rerun_is_refused_without_writes(proj, second):
    a, b = _add(proj, "a"), _add(proj, "b")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    # mode 'all' resolves to {a} ∪ unreleased {b} — a different set.
    selection = {"superset": [a, b], "version_only": [], "all-with-unreleased": None}[second]
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=r"version 1\.1\.0 was released with a different set"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=selection)
    assert _snapshot(proj) == before


def test_different_set_message_names_both_sets(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    with pytest.raises(ValueError) as exc:
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[a, b])
    assert f"[{a}]" in str(exc.value) and f"[{a}, {b}]" in str(exc.value)


def test_different_set_rerun_of_a_lower_version_is_refused_not_rewound(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    release_entries(proj, "proj-a", "1.2.0", entry_ids=[b])
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="different set"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    assert _snapshot(proj) == before


def test_trailing_zero_version_matches_the_record(proj):
    a = _add(proj, "a")
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    again = release_entries(proj, "proj-a", "1.1", entry_ids=[a])
    assert again["release"]["id"] == first["release"]["id"]


def test_rerun_does_not_repin_the_sha(proj):
    """A resume passes the CURRENT HEAD; the record keeps the pinned candidate."""
    a = _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=SHA)
    again = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=SHA2)
    assert again["release"]["candidate_sha"] == SHA


# ── unrecorded legacy releases keep R1a's behaviour ──────────────────────────


def test_legacy_retry_without_record_is_a_noop_returning_no_record(proj):
    a = _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    _forget_records(proj)
    before = _snapshot(proj)
    out = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    assert _snapshot(proj) == before
    assert out["release"] is None


def test_legacy_partial_retry_stamps_the_rest_and_records_the_set(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    _forget_records(proj)
    out = release_entries(proj, "proj-a", "1.1.0", entry_ids=[a, b])
    assert out["released"] == 1
    assert out["release"]["entry_ids"] == sorted([a, b])


def test_legacy_lower_version_without_record_is_still_a_rewind(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    release_entries(proj, "proj-a", "1.2.0", entry_ids=[])
    _forget_records(proj)
    with pytest.raises(ValueError, match="releases never rewind"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[])


def test_legacy_all_mode_append_records_every_entry_of_the_version(proj):
    a = _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0")
    _forget_records(proj)
    b = _add(proj, "b")
    rec = release_entries(proj, "proj-a", "1.1.0")["release"]
    assert rec["entry_ids"] == sorted([a, b])


# ── one live record per version ───────────────────────────────────────────────


def test_unique_live_record_per_project_and_version(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    pid = get_project(proj, "proj-a")["id"]
    with pytest.raises(sa.exc.IntegrityError), proj.begin() as conn:
        conn.execute(releases.insert().values(
            project_id=pid, version="1.1.0", mode="version_only", entry_ids="[]",
            status="stamped"))
    with proj.begin() as conn:  # a soft-deleted record does not block a live one
        conn.execute(releases.update().values(deleted_at=sa.func.current_timestamp()))
        conn.execute(releases.insert().values(
            project_id=pid, version="1.1.0", mode="version_only", entry_ids="[]",
            status="stamped"))


def test_records_are_project_scoped(proj):
    create_project(proj, slug="proj-b", name="B", path="/tmp/proj-b", current_version="1.0.0")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    assert get_release(proj, "proj-b", "1.1.0") is None
    rec = release_entries(proj, "proj-b", "1.1.0", entry_ids=[])["release"]
    assert rec["id"] != get_release(proj, "proj-a", "1.1.0")["id"]


# ── validation of sha / checkout_root before any write ───────────────────────


@pytest.mark.parametrize("bad", ["abc123", "g" * 40, "0" * 65, "0123456 ", "HEAD"])
def test_invalid_candidate_sha_is_refused_without_writes(proj, bad):
    _add(proj, "a")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="candidate_sha"):
        release_entries(proj, "proj-a", "1.1.0", candidate_sha=bad)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("ok", ["abcdef0", "A" * 40, "0" * 64])
def test_candidate_sha_boundaries_accepted_and_lowercased(proj, ok):
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[], candidate_sha=ok)["release"]
    assert rec["candidate_sha"] == ok.lower()


@pytest.mark.parametrize("bad", ["relative/path", "~/repo", "./x"])
def test_relative_checkout_root_is_refused_without_writes(proj, bad):
    _add(proj, "a")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="absolute path"):
        release_entries(proj, "proj-a", "1.1.0", checkout_root=bad)
    assert _snapshot(proj) == before


def test_over_long_checkout_root_is_refused(proj):
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="too long"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[], checkout_root="/" + "x" * 500)
    assert _snapshot(proj) == before


def test_validation_runs_before_a_rerun_returns_the_record(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    with pytest.raises(ValueError, match="candidate_sha"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[], candidate_sha="xyz")


# ── advance_release ───────────────────────────────────────────────────────────


@pytest.fixture
def released(proj):
    _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0", candidate_sha=SHA)
    return proj


def test_statuses_are_ordered_forward():
    assert RELEASE_STATUSES == ("stamped", "files_written", "committed", "tagged", "completed")


def test_advance_one_step(released):
    out = advance_release(released, "proj-a", "1.1.0", "files_written")
    assert out["updated"] is True
    assert out["release"]["status"] == "files_written"
    assert get_release(released, "proj-a", "1.1.0")["status"] == "files_written"


def test_advance_multi_step_jump(released):
    out = advance_release(released, "proj-a", "1.1.0", "committed")
    assert out["release"]["status"] == "committed"


def test_advance_same_status_is_an_idempotent_noop(released):
    advance_release(released, "proj-a", "1.1.0", "files_written")
    before = _snapshot(released)
    out = advance_release(released, "proj-a", "1.1.0", "files_written")
    assert out["updated"] is False
    assert _snapshot(released) == before


def test_advance_backwards_is_refused_without_writes(released):
    advance_release(released, "proj-a", "1.1.0", "tagged")
    before = _snapshot(released)
    with pytest.raises(ValueError, match="backwards"):
        advance_release(released, "proj-a", "1.1.0", "committed")
    assert _snapshot(released) == before


def test_advance_to_completed_stamps_completed_at_once(released):
    assert get_release(released, "proj-a", "1.1.0")["completed_at"] is None
    first = advance_release(released, "proj-a", "1.1.0", "completed")["release"]
    assert first["completed_at"] is not None
    again = advance_release(released, "proj-a", "1.1.0", "completed")
    assert again["updated"] is False
    assert again["release"]["completed_at"] == first["completed_at"]


def test_advance_bumps_updated_at_but_not_created_at(released):
    rec = get_release(released, "proj-a", "1.1.0")
    out = advance_release(released, "proj-a", "1.1.0", "files_written")["release"]
    assert out["created_at"] == rec["created_at"]
    assert out["updated_at"] >= rec["updated_at"]


def test_advance_unknown_version_is_refused(released):
    with pytest.raises(ValueError, match=r"No release record for version 9\.9\.9"):
        advance_release(released, "proj-a", "9.9.9", "files_written")


def test_advance_unknown_status_is_refused(released):
    before = _snapshot(released)
    with pytest.raises(ValueError, match="Invalid release status"):
        advance_release(released, "proj-a", "1.1.0", "shipped")
    assert _snapshot(released) == before


def test_advance_accepts_the_version_contract(released):
    out = advance_release(released, "proj-a", "v1.1", "files_written")
    assert out["release"]["version"] == "1.1.0"


def test_advance_is_project_scoped(released):
    create_project(released, slug="proj-b", name="B", path="/tmp/proj-b")
    with pytest.raises(ValueError, match="No release record"):
        advance_release(released, "proj-b", "1.1.0", "files_written")


@pytest.mark.parametrize("target", ["stamped", "files_written", "committed"])
def test_candidate_sha_repin_allowed_before_committed(released, target):
    """The repin is allowed while the STORED status is below committed — also
    on the transition INTO committed (the skill pins its release commit)."""
    out = advance_release(released, "proj-a", "1.1.0", target, candidate_sha=SHA2)
    assert out["release"]["candidate_sha"] == SHA2
    assert out["updated"] is True


def test_candidate_sha_change_refused_once_committed(released):
    advance_release(released, "proj-a", "1.1.0", "committed")
    before = _snapshot(released)
    with pytest.raises(ValueError, match="candidate_sha cannot change"):
        advance_release(released, "proj-a", "1.1.0", "tagged", candidate_sha=SHA2)
    assert _snapshot(released) == before


def test_same_candidate_sha_after_committed_is_fine(released):
    advance_release(released, "proj-a", "1.1.0", "committed")
    out = advance_release(released, "proj-a", "1.1.0", "tagged", candidate_sha=SHA.upper())
    assert out["release"]["status"] == "tagged"
    assert out["release"]["candidate_sha"] == SHA


def test_advance_invalid_sha_is_refused(released):
    with pytest.raises(ValueError, match="candidate_sha"):
        advance_release(released, "proj-a", "1.1.0", "files_written", candidate_sha="nothex!")


# ── get_release ───────────────────────────────────────────────────────────────


def test_get_release_by_version(released):
    rec = get_release(released, "proj-a", "1.1.0")
    assert rec["version"] == "1.1.0" and rec["status"] == "stamped"
    assert get_release(released, "proj-a", "v1.1") == rec
    assert get_release(released, "proj-a", "2.0.0") is None


def test_get_release_without_version_returns_the_latest_unfinished(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    release_entries(proj, "proj-a", "1.2.0", entry_ids=[])
    assert get_release(proj, "proj-a")["version"] == "1.2.0"
    advance_release(proj, "proj-a", "1.2.0", "completed")
    assert get_release(proj, "proj-a")["version"] == "1.1.0"
    advance_release(proj, "proj-a", "1.1.0", "completed")
    assert get_release(proj, "proj-a") is None


def test_get_release_ignores_soft_deleted_records(released):
    with released.begin() as conn:
        conn.execute(releases.update().values(deleted_at=sa.func.current_timestamp()))
    assert get_release(released, "proj-a", "1.1.0") is None
    assert get_release(released, "proj-a") is None


def test_get_release_with_malformed_version_is_refused(released):
    with pytest.raises(ValueError, match="Unsupported version"):
        get_release(released, "proj-a", "latest")
