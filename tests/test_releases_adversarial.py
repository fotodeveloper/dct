"""Adversarial pass over release stamping + release records (K-41/K-44, R5).

What test_releases.py / test_changelog_branch.py do not already pin:
- two releases of the same project racing on two real connections: at most
  one record per version (under the version contract, 1.5 == 1.5.0), the
  loser gets a controlled refusal or the winner's record, never a second row;
- the ledger invariant "a record's entry_ids == the entries carrying its
  version" across a whole release history;
- column limits on the version a release writes (String(50));
- version-contract properties (idempotent normalization, ordering, bump);
- every (from, to) status pair of advance_release.

Runs on in-memory / file SQLite and on the PG lane.
"""

from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from functools import cmp_to_key
from threading import Event

import pytest
import sqlalchemy as sa

from dct.core import releases as release_records
from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import RELEASE_STATUSES, advance_release, get_release
from dct.core.version import (
    bump,
    compare_versions,
    normalize_version,
    same_version,
)
from dct.db import changelog_entries, create_tables, get_engine, projects, releases


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    return engine


def _add(engine, text, slug="proj-a"):
    return add_entry(engine, slug, "added", text)["id"]


def _snapshot(engine):
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases)
        )


def _live_records(engine, slug="proj-a"):
    with engine.begin() as conn:
        pid = conn.execute(sa.select(projects.c.id).where(projects.c.slug == slug)).scalar_one()
        return [release_records.record_to_dict(r) for r in conn.execute(
            sa.select(releases).where(releases.c.project_id == pid,
                                      releases.c.deleted_at.is_(None))
            .order_by(releases.c.id))]


# ── concurrency: two releases of one project on two connections ──────────────


@pytest.fixture
def concurrent_engine(engine, tmp_path):
    """The guarded PG lane, or a file SQLite database with real connections."""
    if engine.dialect.name == "postgresql":
        yield engine
        return
    eng = get_engine(f"sqlite:///{tmp_path / 'race.db'}")
    create_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _race(monkeypatch, eng, first_call, second_call):
    """Run `first_call`; right after it inserted its record (still inside its
    transaction, holding the project write lock) start `second_call` on
    another connection and give it time to block on the lock. Returns
    (first result, second result-or-exception)."""
    original = release_records.insert_record
    started = []

    with ThreadPoolExecutor(max_workers=1) as pool:
        def insert_then_start_rival(*args, **kwargs):
            record = original(*args, **kwargs)
            if not started:
                started.append(pool.submit(_capture, second_call))
                Event().wait(0.5)  # the rival must be waiting on the lock now
            return record

        monkeypatch.setattr(release_records, "insert_record", insert_then_start_rival)
        first = first_call()
        second = started[0].result(timeout=20)
    return first, second


def _capture(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
        return exc


def test_racing_releases_of_one_version_with_different_sets_one_wins(
    concurrent_engine, monkeypatch,
):
    eng = concurrent_engine
    create_project(eng, "proj-r", "R", current_version="1.0.0")
    a = _add(eng, "a", "proj-r")
    b = _add(eng, "b", "proj-r")
    first, second = _race(
        monkeypatch, eng,
        lambda: release_entries(eng, "proj-r", "1.1.0", entry_ids=[a]),
        lambda: release_entries(eng, "proj-r", "1.1", entry_ids=[b]),
    )
    assert first["release"]["entry_ids"] == [a]
    assert isinstance(second, ValueError), second
    assert "different set" in str(second)
    [record] = _live_records(eng, "proj-r")
    assert record["entry_ids"] == [a]
    carrying = {e["id"]: e["version"] for e in list_entries(eng, "proj-r")}
    assert carrying == {a: "1.1.0", b: None}, "the loser stamped nothing"


def test_racing_releases_of_one_version_with_the_same_set_share_one_record(
    concurrent_engine, monkeypatch,
):
    eng = concurrent_engine
    create_project(eng, "proj-r", "R", current_version="1.0.0")
    a = _add(eng, "a", "proj-r")
    first, second = _race(
        monkeypatch, eng,
        lambda: release_entries(eng, "proj-r", "1.5"),
        lambda: release_entries(eng, "proj-r", "v1.5.0"),
    )
    assert not isinstance(second, Exception), second
    assert second["released"] == 0
    assert second["release"] == first["release"]
    assert len(_live_records(eng, "proj-r")) == 1
    assert [e["version"] for e in list_entries(eng, "proj-r")] == ["1.5"]
    assert first["release"]["entry_ids"] == [a]


def test_lower_version_released_during_a_higher_one_is_refused_not_a_rewind(
    concurrent_engine, monkeypatch,
):
    eng = concurrent_engine
    create_project(eng, "proj-r", "R", current_version="1.0.0")
    a = _add(eng, "a", "proj-r")
    first, second = _race(
        monkeypatch, eng,
        lambda: release_entries(eng, "proj-r", "1.2.0", entry_ids=[a]),
        lambda: release_entries(eng, "proj-r", "1.1.0", entry_ids=[]),
    )
    assert isinstance(second, ValueError), second
    assert "lower than the current version 1.2.0" in str(second)
    assert get_project(eng, "proj-r")["current_version"] == "1.2.0"
    assert [r["version"] for r in _live_records(eng, "proj-r")] == ["1.2.0"]
    assert first["release"]["entry_ids"] == [a]


# ── ledger invariant across a release history ────────────────────────────────


def _assert_ledger_consistent(engine, slug="proj-a"):
    """Every live record's entry_ids is exactly the set of live entries that
    carry its version, every stamped version has a record, and
    current_version is the highest recorded version."""
    recs = _live_records(engine, slug)
    entries = list_entries(engine, slug)
    for rec in recs:
        carrying = {e["id"] for e in entries if same_version(e["version"], rec["version"])}
        assert set(rec["entry_ids"]) == carrying, rec
    stamped_versions = {e["version"] for e in entries if e["version"] is not None}
    for v in stamped_versions:
        assert any(same_version(v, r["version"]) for r in recs), f"{v} stamped without a record"
    if recs:
        highest = max((r["version"] for r in recs), key=cmp_to_key(compare_versions))
        assert same_version(get_project(engine, slug)["current_version"], highest)


def test_record_sets_match_the_stamped_entries_across_a_whole_history(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    release_entries(proj, "proj-a", "1.1.0")                     # all
    _assert_ledger_consistent(proj)
    c, d = _add(proj, "c"), _add(proj, "d")
    release_entries(proj, "proj-a", "v1.2", entry_ids=[c])       # selected
    _assert_ledger_consistent(proj)
    release_entries(proj, "proj-a", "1.3.0", entry_ids=[])       # version-only
    _assert_ledger_consistent(proj)
    # Resumes under any spelling of the version: whole-operation no-ops.
    # (Mode 'all' is not retried here: `d` is still unreleased, so its
    # resolved set would differ — that refusal is pinned elsewhere.)
    for retry, ids in (("1.1.0", [a, b]), ("1.1", [b, a]), ("v1.2.0", [c]), ("1.3", [])):
        before = _snapshot(proj)
        assert release_entries(proj, "proj-a", retry, entry_ids=ids)["released"] == 0
        assert _snapshot(proj) == before
    with pytest.raises(ValueError, match="different set"):
        release_entries(proj, "proj-a", "1.2.0", entry_ids=[c, d])
    out = release_entries(proj, "proj-a", bump_type="patch")     # all, from 1.3.0
    assert out["version"] == "1.3.1" and out["release"]["entry_ids"] == [d]
    _assert_ledger_consistent(proj)
    assert {e["id"]: e["version"] for e in list_entries(proj, "proj-a")} == {
        a: "1.1.0", b: "1.1.0", c: "1.2", d: "1.3.1"}


def test_rerun_of_a_completed_release_returns_it_completed_and_writes_nothing(proj):
    """Resuming a finished release must never reset its progress."""
    _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0")
    done = advance_release(proj, "proj-a", "1.1.0", "completed")["release"]
    before = _snapshot(proj)
    out = release_entries(proj, "proj-a", "1.1.0")
    assert _snapshot(proj) == before
    assert out["release"] == done
    assert out["release"]["status"] == "completed"


# ── String(50) limit on the version a release writes ─────────────────────────

_TOO_LONG = "1.0." + "9" * 47  # 51 chars once normalized


@pytest.mark.xfail(strict=True, reason=(
    "BUG: release_entries never runs ensure_fits on the version — a 51+ char "
    "version is stored silently on SQLite (raw DataError on PG) in "
    "changelog_entries.version / projects.current_version / releases.version "
    "(String(50)) — dct/core/changelog.py:439-457 — for owner triage"))
@pytest.mark.parametrize("how", ["explicit", "bump"])
def test_a_version_longer_than_its_column_is_refused_without_writes(proj, how):
    _add(proj, "a")
    if how == "bump":
        # 50 chars — fits — but its patch bump carries into a 51st character.
        current = "1.0." + "9" * 46
        assert len(current) == 50
        with proj.begin() as conn:
            conn.execute(projects.update().values(current_version=current))
        kwargs = {"bump_type": "patch"}
    else:
        kwargs = {"version": _TOO_LONG}
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="too long"):
        release_entries(proj, "proj-a", **kwargs)
    assert _snapshot(proj) == before


def test_a_version_of_exactly_the_column_width_is_stored_untruncated(proj):
    _add(proj, "a")
    version = "1.0." + "9" * 46
    out = release_entries(proj, "proj-a", version)
    assert out["release"]["version"] == version
    assert get_project(proj, "proj-a")["current_version"] == version
    assert get_release(proj, "proj-a", version)["version"] == version


# ── version contract: properties over a corpus ───────────────────────────────

ACCEPTED = [
    "0.0", "0.1", "1.0", "1.0.0", "v1.0.0", "V2.3", "01.002.0003", " 1.2.3 ",
    "\t1.2\n", " 1.4.0 ", "10.20.30", "999999.0.1", "1.5", "1.5.0",
    "1.10", "1.9.9", "2.0", "0.18", "0.18.1",
]
REFUSED = [
    "１.２.３",           # fullwidth digits
    "¹.²",               # superscript digits (str.isdigit() is True)
    "1_0.2",             # int() accepts underscores, the contract does not
    "1.2 .3",       # whitespace INSIDE the version
    "1.2.3\x00",         # NUL
    "v 1.2",             # space after the prefix
    "v1.2.3v",
    "1.2.3.",
    "0x1.2",
    None, 12, 1.5,
]


@pytest.mark.parametrize("raw", ACCEPTED)
def test_normalization_is_idempotent_and_preserves_identity(raw):
    norm = normalize_version(raw)
    assert normalize_version(norm) == norm
    assert norm == norm.strip() and not norm.lower().startswith("v")
    assert same_version(raw, norm) and compare_versions(raw, norm) == 0


@pytest.mark.parametrize("raw", REFUSED)
def test_out_of_contract_versions_are_refused_never_coerced(raw):
    with pytest.raises(ValueError, match="Unsupported version"):
        normalize_version(raw)


def test_comparison_is_a_consistent_total_order_over_the_corpus():
    for a, b in itertools.product(ACCEPTED, repeat=2):
        ab, ba = compare_versions(a, b), compare_versions(b, a)
        assert ab == -ba, (a, b)
        assert (ab == 0) == same_version(a, b), (a, b)
    for a, b, c in itertools.product(ACCEPTED[:12], repeat=3):
        if compare_versions(a, b) <= 0 and compare_versions(b, c) <= 0:
            assert compare_versions(a, c) <= 0, (a, b, c)


@pytest.mark.parametrize("raw", ACCEPTED)
@pytest.mark.parametrize("kind", ["patch", "minor", "major"])
def test_bump_always_moves_forward_to_a_normalized_version(raw, kind):
    bumped = bump(raw, kind)
    assert compare_versions(bumped, raw) == 1
    assert normalize_version(bumped) == bumped


@pytest.mark.parametrize("raw", REFUSED[:-3])
def test_release_with_an_out_of_contract_version_writes_nothing(proj, raw):
    _add(proj, "a")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="Unsupported version"):
        release_entries(proj, "proj-a", raw)
    assert _snapshot(proj) == before


# ── advance_release: every (from, to) pair ───────────────────────────────────


@pytest.mark.parametrize("start,target", list(itertools.product(RELEASE_STATUSES, repeat=2)))
def test_advance_moves_forward_only_for_every_status_pair(proj, start, target):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    advance_release(proj, "proj-a", "1.1.0", start)
    before = _snapshot(proj)
    if RELEASE_STATUSES.index(target) < RELEASE_STATUSES.index(start):
        with pytest.raises(ValueError, match="backwards"):
            advance_release(proj, "proj-a", "1.1.0", target)
        assert _snapshot(proj) == before
        assert get_release(proj, "proj-a", "1.1.0")["status"] == start
        return
    out = advance_release(proj, "proj-a", "1.1.0", target)
    assert out["updated"] is (target != start)
    rec = get_release(proj, "proj-a", "1.1.0")
    assert rec["status"] == target
    assert (rec["completed_at"] is not None) is (target == "completed")


def test_advance_of_a_soft_deleted_record_is_refused_without_writes(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    with proj.begin() as conn:
        conn.execute(releases.update().values(deleted_at=sa.func.current_timestamp()))
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="No release record"):
        advance_release(proj, "proj-a", "1.1.0", "files_written")
    assert _snapshot(proj) == before
