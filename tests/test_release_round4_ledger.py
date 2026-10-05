"""Round-4 ledger and core invariants (R8a-1): K-102 (the stamp is the only
writer of versions), K-106 (the stamp binds the expected executor), K-115
(tag / push proof in the ledger, duplicates of one semantic version refuse
writes, mutual consistency of record / consent / proposal / render) and
K-119 M5 / M6 (no core mutation skips the actor check; MCP never abandons a
release dct's effects layer executes).

Core tests run on both lanes (in-memory SQLite and the PG lane); the lock
tests use conftest.LockProbe on a file SQLite database or PostgreSQL. The
effects tests drive the production workflow on real throwaway repositories.
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from conftest import git

from dct.core.changelog import (
    add_entry,
    import_released_entry,
    list_entries,
    release_entries,
)
from dct.core.projects import create_project, get_project, set_release_executor
from dct.core.releases import (
    abandon_release,
    advance_release,
    get_pending_proposal,
    get_release,
    get_version_files,
    proposal_digest,
    reconstruct_release,
    set_pending_proposal,
    verify_release_entry_set,
)
from dct.db import changelog_entries, project_transaction, projects, releases

CAND = "c" * 40
COMMIT = "d" * 40
MANIFEST = {"changelog": {"path": "changelog.md", "sha256": "1" * 64, "section_date": "2026-10-01"}}
SLUG = "proj-a"


@pytest.fixture
def proj(engine):
    create_project(engine, slug=SLUG, name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    return engine


def _add(engine, text, slug=SLUG):
    return add_entry(engine, slug, "added", text)["id"]


def _snapshot(engine):
    """Every row of the release-relevant tables — a refused call leaves all
    of it (timestamps included)."""
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases))


def _raw_entry(engine, version, text="late B", slug=SLUG) -> int:
    """An entry carrying `version` written with RAW SQL — outside every API
    guard (the pre-existing inconsistency the ledger must refuse to build on)."""
    with engine.begin() as conn:
        pid = conn.execute(sa.select(projects.c.id).where(projects.c.slug == slug)).scalar_one()
        return conn.execute(changelog_entries.insert().values(
            project_id=pid, category="added", entry=text, version=version,
        ).returning(changelog_entries.c.id)).scalar_one()


def _consent(ids, version="1.1.0", **extra):
    return {"version": version, "entry_ids": ids, "candidate_sha": CAND, **extra}


# ── K-102: the stamp is the only writer of versions ─────────────────────────


@pytest.mark.parametrize("version", ["1.1.0", "v1.1", "0.9", "9.9.9", ""])
def test_add_entry_refuses_every_explicit_version(proj, version):
    """Public core add_entry never writes a version — not the one a release
    just stamped (B after the stamp of A), not an older, a future or an
    empty one. Only the transactional stamp assigns versions."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], consent=_consent([a]),
                    candidate_sha=CAND)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="only the release stamp"):
        add_entry(proj, SLUG, "added", "late B", version=version)
    with pytest.raises(ValueError, match="only the release stamp"):
        add_entry(proj, SLUG, "added", "late B", version)  # positional, as before
    assert _snapshot(proj) == before
    record = get_release(proj, SLUG, "1.1.0")
    assert record["entry_ids"] == [a] and record["consent"]["entry_ids"] == [a]


def test_add_entry_without_a_version_still_writes_an_unreleased_entry(proj):
    row = add_entry(proj, SLUG, "added", "plain", None)
    assert row["version"] is None and row["released_at"] is None
    assert [e["id"] for e in list_entries(proj, SLUG, unreleased_only=True)] == [row["id"]]


def test_import_released_entry_is_the_separate_named_history_path(proj):
    """Old released history (and fixtures) come in through
    import_released_entry — never through add_entry(version=…)."""
    row = import_released_entry(proj, SLUG, "added", "ancient", "0.9")
    assert row["version"] == "0.9" and row["released_at"] is not None
    assert [e["id"] for e in list_entries(proj, SLUG, version="0.9")] == [row["id"]]
    assert list_entries(proj, SLUG, unreleased_only=True) == []
    assert get_release(proj, SLUG, "0.9") is None     # history, not a record


@pytest.mark.parametrize("spelling", ["1.1.0", "1.1", "v1.1.0"])
def test_import_never_extends_a_recorded_release(proj, spelling):
    """B added after the stamp of A: the import path refuses a version that
    has a live release record (any equivalent spelling) — the record's set
    and its consent are never widened."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], consent=_consent([a]),
                    candidate_sha=CAND)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="has a release record"):
        import_released_entry(proj, SLUG, "added", "late B", spelling)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("version", [None, "", "   ", "2.0.0"])
def test_import_needs_a_version_that_is_already_history(proj, version):
    """No version → refused (that is add_entry); a version above the
    project's current version was never released → refused."""
    before = _snapshot(proj)
    with pytest.raises(ValueError):
        import_released_entry(proj, SLUG, "added", "x", version)
    assert _snapshot(proj) == before


def test_add_entry_takes_the_project_write_lock(concurrent_engine, lock_probe):
    """Writing an entry takes part in the shared project lock: while another
    composed writer (the stamp) holds it, add_entry WAITS — proven by the
    probe, not by a sleep — and writes only after the release."""
    eng = concurrent_engine
    create_project(eng, slug=SLUG, name="Project A", current_version="1.0.0")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with project_transaction(eng, SLUG, write=True):
            rival = pool.submit(lock_probe.capture, add_entry, eng, SLUG, "added", "waits")
            lock_probe.wait_until_blocked()
            assert not rival.done()
        row = rival.result(timeout=20)
    assert not isinstance(row, Exception), row
    assert [e["entry"] for e in list_entries(eng, SLUG)] == ["waits"]


@pytest.mark.parametrize("call", ["move", "same status", "manifest only"])
def test_advance_refuses_a_record_whose_entry_set_no_longer_matches(proj, call):
    """A pre-existing inconsistency (B carries V through raw SQL — outside
    the API): every progress write is refused in ITS OWN transaction, also a
    retry of the current status; no new effect is recorded, the record and
    its consent are never widened to fit."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], consent=_consent([a]),
                    candidate_sha=CAND)
    b = _raw_entry(proj, "1.1.0")
    before = _snapshot(proj)
    kwargs = {"move": ("files_written", {"manifest": MANIFEST}),
              "same status": ("stamped", {}),
              "manifest only": ("stamped", {"manifest": MANIFEST})}[call]
    with pytest.raises(ValueError, match=f"not in the record: #{b}"):
        advance_release(proj, SLUG, "1.1.0", kwargs[0], actor="dct", **kwargs[1])
    assert _snapshot(proj) == before
    record = get_release(proj, SLUG, "1.1.0")
    assert (record["status"], record["entry_ids"], record["manifest"]) == ("stamped", [a], None)
    assert record["consent"]["entry_ids"] == [a]


def test_advance_refuses_a_record_that_lost_an_entry(proj):
    a, b = _add(proj, "A"), _add(proj, "B")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a, b])
    with proj.begin() as conn:  # raw SQL: B loses its stamp
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == b)
                     .values(version=None, released_at=None))
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=f"no longer live with 1.1.0: #{b}"):
        advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST)
    assert _snapshot(proj) == before


# ── K-102: a fresh apply verifies scope and executor before the first effect ─

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def release_repo(engine, tmp_path, git_identity):
    from test_release_effects import make_release_repo

    repo, log = make_release_repo(engine, tmp_path)
    return engine, repo, log


def _at(point: str, action):
    def hook(name: str) -> None:
        if name == point:
            action()
    return hook


@needs_git
def test_fresh_apply_refuses_before_any_effect_when_b_joins_after_the_stamp(release_repo):
    """B is written with V right after the stamp of A (raw SQL — the API
    refuses it): the fresh apply checks the record's full entry set BEFORE
    its first effect — no file, commit, tag or push; the record stays
    'stamped' with exactly [A]; the consent is not widened."""
    from test_release_effects import SLUG as R
    from test_release_effects import apply, snapshot, sync_runs

    engine, repo, log = release_repo
    from test_release_effects import proven

    a = proven(engine, repo, "feature a")
    before = snapshot(repo)
    remote_before = git(repo, "ls-remote", "origin")
    late = []
    with pytest.raises(Exception) as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a],
              on_point=_at("after_stamp", lambda: late.append(_raw_entry(engine, "1.1.0",
                                                                         slug=R))))
    from dct.workflows.release.effects import ReleaseRefused

    assert isinstance(exc.value, ReleaseRefused), exc.value
    assert f"not in the record: #{late[0]}" in str(exc.value)
    assert snapshot(repo) == before, "no effect after the refusal"
    assert git(repo, "ls-remote", "origin") == remote_before
    record = get_release(engine, R, "1.1.0")
    assert (record["status"], record["entry_ids"], record["manifest"].get("changelog")) == (
        "stamped", [a], None)
    assert record["consent"]["entry_ids"] == [a]
    assert sync_runs(log) == ["1.1.0"]  # the isolated prepare only


@needs_git
def test_fresh_apply_refuses_before_any_effect_when_the_record_is_not_dcts(release_repo):
    """The second barrier (K-106): the record the stamp returned carries
    another executor (seeded here with raw SQL right after the stamp) — the
    fresh apply verifies the RECORD's executor before its first effect."""
    from test_release_effects import SLUG as R
    from test_release_effects import apply, proven, snapshot

    from dct.workflows.release.effects import ReleaseRefused

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    before = snapshot(repo)

    def flip():
        with engine.begin() as conn:
            conn.execute(releases.update().values(executor="own"))

    with pytest.raises(ReleaseRefused, match="own release tool") as exc:
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=_at("after_stamp", flip))
    assert exc.value.details["executor"] == "own"
    assert snapshot(repo) == before, "no effect after the refusal"
    assert get_release(engine, R, "1.1.0")["status"] == "stamped"


# ── K-106: the stamp binds the expected executor ────────────────────────────

PROPOSAL = {"paths": ["pyproject.toml"], "base_blobs": {"pyproject.toml": "a" * 40},
            "expected_blobs": {"pyproject.toml": "b" * 40}}


@pytest.mark.parametrize("setting, expected", [("own", "dct"), ("dct", "own")])
def test_stamp_refuses_an_executor_other_than_the_expected_one(proj, setting, expected):
    """Both directions: the caller approved a release executed by `expected`,
    the project's CURRENT setting (read inside the stamp transaction) says
    otherwise — refused BEFORE any write: no entry stamped, no record, the
    version-file contract and the pending proposal untouched."""
    set_release_executor(proj, SLUG, setting)
    a = _add(proj, "A")
    pending = set_pending_proposal(proj, SLUG, PROPOSAL)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=f"expects the executor '{expected}'.*'{setting}'"):
        release_entries(
            proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=expected,
            consent=_consent([a], proposal_sha256=pending["proposal_sha256"]),
            candidate_sha=CAND, consume_proposal=True)
    assert _snapshot(proj) == before
    assert get_release(proj, SLUG, "1.1.0") is None
    assert get_version_files(proj, SLUG)["version_files"] is None
    assert get_pending_proposal(proj, SLUG) == pending
    assert [e["version"] for e in list_entries(proj, SLUG)] == [None]


@pytest.mark.parametrize("executor", ["dct", "own"])
def test_stamp_with_the_expected_executor_records_it(proj, executor):
    set_release_executor(proj, SLUG, executor)
    a = _add(proj, "A")
    out = release_entries(proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=executor)
    assert out["release"]["executor"] == executor


@pytest.mark.parametrize("setting, approved", [("own", "dct"), ("dct", "own")])
def test_the_consent_binds_the_executor_too(proj, setting, approved):
    """A consent that names its executor binds the stamp even without the
    explicit parameter; the two must not disagree."""
    set_release_executor(proj, SLUG, setting)
    a = _add(proj, "A")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=f"expects the executor '{approved}'"):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND,
                        consent=_consent([a], executor=approved))
    with pytest.raises(ValueError, match="expected_executor .* contradicts the consent"):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND,
                        consent=_consent([a], executor=approved), expected_executor=setting)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("bad", ["DCT", "other", " dct", 1])
def test_an_invalid_expected_executor_is_refused_before_any_read(proj, bad):
    a = _add(proj, "A")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="release executor must be one of"):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=bad)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("stamped, later", [("dct", "own"), ("own", "dct")])
def test_the_records_executor_is_immutable_after_the_stamp(proj, stamped, later):
    """A later project-setting change never takes an existing release over
    (both directions): the record keeps its executor; a re-run of the stamp
    is judged against the RECORD, not against today's setting."""
    set_release_executor(proj, SLUG, stamped)
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=stamped)
    set_release_executor(proj, SLUG, later)
    assert get_release(proj, SLUG, "1.1.0")["executor"] == stamped
    again = release_entries(proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=stamped)
    assert again["released"] == 0 and again["release"]["executor"] == stamped
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=f"recorded with the executor '{stamped}'"):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=later)
    assert _snapshot(proj) == before
    advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST)
    assert get_release(proj, SLUG, "1.1.0")["executor"] == stamped


def test_set_release_executor_takes_the_project_write_lock(concurrent_engine, lock_probe,
                                                           monkeypatch):
    """The setter and the stamp share ONE lock: a setter arriving while the
    stamp transaction is open (after its executor comparison, right after
    the record insert) WAITS — the record keeps the executor the stamp saw,
    the setting changes only afterwards."""
    from dct.core import releases as release_records

    eng = concurrent_engine
    create_project(eng, slug=SLUG, name="Project A", current_version="1.0.0")
    a = _add(eng, "A")
    original = release_records.insert_record
    rival = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def insert_then_block_the_setter(*args, **kwargs):
            record = original(*args, **kwargs)
            rival.append(pool.submit(lock_probe.capture, set_release_executor, eng, SLUG,
                                     "own"))
            lock_probe.wait_until_blocked()
            return record

        monkeypatch.setattr(release_records, "insert_record", insert_then_block_the_setter)
        out = release_entries(eng, SLUG, "1.1.0", entry_ids=[a], expected_executor="dct")
        changed = rival[0].result(timeout=20)
    assert not isinstance(changed, Exception), changed
    assert out["release"]["executor"] == "dct"
    assert get_release(eng, SLUG, "1.1.0")["executor"] == "dct"
    assert get_project(eng, SLUG)["release_executor"] == "own"


@needs_git
def test_apply_refuses_when_the_executor_switches_to_own_before_the_stamp(release_repo):
    """The barrier before the stamp (dct → own): another client switches the
    project to `own` after apply's own check and before its stamp — the
    stamp transaction refuses; nothing is stamped, recorded or written."""
    from test_release_effects import SLUG as R
    from test_release_effects import apply, proven, snapshot

    from dct.workflows.release.effects import ReleaseRefused

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    before = snapshot(repo)
    seen = []

    def switch():
        set_release_executor(engine, R, "own")
        seen.append(_snapshot(engine))

    with pytest.raises(ReleaseRefused, match="expects the executor 'dct'"):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=_at("before_stamp", switch))
    assert _snapshot(engine) == seen[0], "the refused stamp wrote nothing"
    assert get_release(engine, R, "1.1.0") is None and get_release(engine, R) is None
    assert [e["version"] for e in list_entries(engine, R)] == [None]
    assert get_pending_proposal(engine, R) is not None    # not consumed
    assert snapshot(repo) == before


def test_an_own_tools_stamp_refuses_when_the_project_switched_to_dct(proj):
    """The other direction (own → dct): the own tool read `own` (the plan),
    the setting flipped to `dct` before its stamp — refused, nothing written;
    with the setting it expected the same call stamps."""
    set_release_executor(proj, SLUG, "own")
    a = _add(proj, "A")
    expected = get_project(proj, SLUG)["release_executor"]      # the tool's read
    set_release_executor(proj, SLUG, "dct")                     # the barrier
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="expects the executor 'own'.*'dct'"):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a], expected_executor=expected)
    assert _snapshot(proj) == before
    set_release_executor(proj, SLUG, "own")
    assert release_entries(proj, SLUG, "1.1.0", entry_ids=[a],
                           expected_executor=expected)["release"]["executor"] == "own"


# ── K-119 M5: no public core mutation skips the actor check ─────────────────


def _dct_record(engine, slug=SLUG) -> int:
    """A record dct's effects layer executes: executor 'dct' AND a consent."""
    a = _add(engine, "A", slug)
    release_entries(engine, slug, "1.1.0", entry_ids=[a], consent=_consent([a]),
                    candidate_sha=CAND)
    return a


def test_advance_without_an_actor_never_advances_a_release_dct_executes(proj):
    """The default actor is 'ledger' and it IS checked: a core caller that
    names no actor cannot record progress on dct's own release."""
    _dct_record(proj)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="dct release resume"):
        advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST)
    assert _snapshot(proj) == before
    out = advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST, actor="dct")
    assert out["release"]["status"] == "files_written"


@pytest.mark.parametrize("actor", [None, "", "owner", "DCT"])
def test_advance_and_abandon_refuse_an_unknown_actor(proj, actor):
    """actor=None is not "unchecked" any more — it is not an actor."""
    _dct_record(proj)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="actor must be one of"):
        advance_release(proj, SLUG, "1.1.0", "stamped", actor=actor)
    with pytest.raises(ValueError, match="actor must be one of"):
        abandon_release(proj, SLUG, "1.1.0", "why", confirm=True, actor=actor)
    assert _snapshot(proj) == before


def test_the_default_actor_advances_a_ledger_owned_record(proj):
    """A record stamped outside `dct release apply` (no consent) is the
    ledger path — the default actor advances it, 'dct' does not."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a])
    with pytest.raises(ValueError, match="not executed by dct"):
        advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST, actor="dct")
    out = advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST)
    assert out["updated"] and out["release"]["status"] == "files_written"


# ── K-119 M6: only the lock-holding local path abandons dct's own release ───


def test_core_abandon_of_a_dct_release_needs_the_effects_actor(proj):
    _dct_record(proj)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="dct release abandon"):
        abandon_release(proj, SLUG, "1.1.0", "changed my mind", confirm=True)
    with pytest.raises(ValueError, match="dct release abandon"):
        abandon_release(proj, SLUG, "1.1.0", "changed my mind", confirm=True, actor="ledger")
    assert _snapshot(proj) == before
    out = abandon_release(proj, SLUG, "1.1.0", "changed my mind", confirm=True, actor="dct")
    assert out["release"]["status"] == "abandoned"


@pytest.mark.parametrize("executor", ["dct", "own"])
def test_a_ledger_owned_record_is_abandoned_by_either_actor(proj, executor):
    """Records the ledger path owns (an own tool's, or one stamped outside
    apply — no consent) keep working through the plain ledger call."""
    set_release_executor(proj, SLUG, executor)
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a])
    out = abandon_release(proj, SLUG, "1.1.0", "tool broke", confirm=True)
    assert out["updated"] and out["release"]["status"] == "abandoned"


def test_mcp_abandon_refuses_a_release_dct_executes(proj, monkeypatch):
    """MCP never guesses the checkout: it cannot take the effects layer's
    operation lock, so it refuses and names the local command; a record the
    ledger path owns is abandoned as before."""
    from dct.mcp import server as S

    monkeypatch.setattr(S, "_get_engine", lambda: proj)
    _dct_record(proj)
    before = _snapshot(proj)
    out = S.abandon_release("1.1.0", "changed my mind", confirm=True, project=SLUG)
    assert "dct release abandon --version 1.1.0" in out["error"]
    assert "operation lock" in out["error"]
    assert _snapshot(proj) == before

    create_project(proj, slug="proj-own", name="Own", current_version="1.0.0")
    set_release_executor(proj, "proj-own", "own")
    b = _add(proj, "B", "proj-own")
    release_entries(proj, "proj-own", "1.1.0", entry_ids=[b])
    done = S.abandon_release("1.1.0", "tool broke", confirm=True, project="proj-own")
    assert done == {"project": "proj-own", "version": "1.1.0", "status": "abandoned",
                    "updated": True}


@pytest.fixture
def concurrent_repo(concurrent_engine, tmp_path, git_identity):
    from test_release_effects import make_release_repo

    repo, log = make_release_repo(concurrent_engine, tmp_path)
    return concurrent_engine, repo, log


@needs_git
def test_effects_never_run_past_an_abandon(concurrent_repo, monkeypatch):
    """A running apply (paused between two effects, holding the operation
    lock) cannot be abandoned behind its back: MCP refuses (it holds no
    lock), the local command refuses (the lock is taken) — so the run
    finishes a release that is NOT abandoned. An abandon is only ever
    granted while no effects command runs, and after it nothing continues:
    resume is a no-op and the world stays as it was."""
    from test_release_effects import SLUG as R
    from test_release_effects import apply, proven, remote_refs, resume, snapshot

    from dct.mcp import server as S
    from dct.workflows.release import effects
    from dct.workflows.release.effects import ReleaseRefused

    engine, repo, _ = concurrent_repo
    monkeypatch.setattr(S, "_get_engine", lambda: engine)
    a = proven(engine, repo, "feature a")
    paused, go = threading.Event(), threading.Event()

    def pause(name: str) -> None:
        if name == "after_files_written":
            paused.set()
            assert go.wait(60), "the test never released the paused apply"

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(apply, engine, repo, version="1.1.0", entry_ids=[a],
                              on_point=pause)
        try:
            assert paused.wait(60), "apply never reached the pause point"
            before = _snapshot(engine)
            out = S.abandon_release("1.1.0", "stop it", confirm=True, project=R)
            assert "dct release abandon" in out["error"]
            with pytest.raises(ReleaseRefused, match="another dct release operation"):
                effects.abandon(engine, R, "1.1.0", "stop it", confirm=True)
            assert _snapshot(engine) == before
            assert get_release(engine, R, "1.1.0")["status"] == "files_written"
        finally:
            go.set()
        assert running.result(timeout=120)["outcome"] == "completed"
    assert get_release(engine, R, "1.1.0")["status"] == "completed"

    # The approved abandon: an INTERRUPTED release (no command running).
    from test_release_effects import Interrupted, interrupt_at

    b = proven(engine, repo, "feature b")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.2.0", entry_ids=[b],
              on_point=interrupt_at("after_files_written"))
    refused = S.abandon_release("1.2.0", "owner said stop", confirm=True, project=R)
    assert "dct release abandon" in refused["error"]
    done = effects.abandon(engine, R, "1.2.0", "owner said stop", confirm=True)
    assert done["outcome"] == "abandoned"
    world = (snapshot(repo), remote_refs(repo), _snapshot(engine))
    assert resume(engine, repo)["outcome"] == "noop"      # nothing continues
    assert (snapshot(repo), remote_refs(repo), _snapshot(engine)) == world
    assert git(repo, "log", "-1", "--format=%s") == "feat: feature b"   # no 1.2.0 commit
    assert git(repo, "tag", "--list", "v1.2.0") == ""


# ── K-115 G4: the tag and the push are PROVEN in the ledger ─────────────────

TAG = {"name": "v1.1.0", "object": COMMIT, "commit": COMMIT, "annotated": False}
TAG_OBJECT = "e" * 40
ANNOTATED = {"name": "v1.1.0", "object": TAG_OBJECT, "commit": COMMIT, "annotated": True}
TARGET = {"remote": "origin", "url": "/tmp/remote.git", "branch": "main", "tag": "v1.1.0"}
PUSH = {"target": TARGET, "tag_commit": COMMIT, "branch_tip": COMMIT}


def _proof(record, field):
    """A stored proof without the ledger's own `recorded_at` stamp."""
    stored = dict(record.get(field) or {})
    assert stored.pop("recorded_at", None), f"{field} carries no recorded_at"
    return stored


def _git_record(engine, *, executor="dct", consent=None, mode="git"):
    """A git-mode record at 'committed' with its release commit; `consent`
    a push target dict, "no-push" (a consent with push_target null) or None
    (a ledger-owned record: no consent)."""
    set_release_executor(engine, SLUG, executor)
    a = _add(engine, "A")
    given = None
    if consent is not None:
        given = _consent([a], push_target=None if consent == "no-push" else consent)
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND,
                    release_mode=mode, consent=given)
    actor = "dct" if given and executor == "dct" else "ledger"
    advance_release(engine, SLUG, "1.1.0", "committed", release_commit_sha=COMMIT,
                    manifest=MANIFEST, actor=actor)
    return actor


@pytest.mark.parametrize("executor", ["dct", "own"])
@pytest.mark.parametrize("target", ["tagged", "pushed", "completed"])
def test_a_git_record_never_passes_tagged_without_the_tag_proof(proj, executor, target):
    """The status alone is not proof: without the verified tag identity no
    git-mode record reaches 'tagged' or anything later — also an own tool's
    record, also by a jump straight to 'completed'."""
    actor = _git_record(proj, executor=executor)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="tag_proof"):
        advance_release(proj, SLUG, "1.1.0", target, actor=actor)
    assert _snapshot(proj) == before
    assert get_release(proj, SLUG, "1.1.0")["status"] == "committed"


@pytest.mark.parametrize("tag", [TAG, ANNOTATED, {**TAG, "name": "1.1"}])
def test_the_verified_tag_identity_is_recorded_with_tagged(proj, tag):
    actor = _git_record(proj, executor="own")
    out = advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=tag, actor=actor)
    assert out["release"]["status"] == "tagged"
    assert _proof(out["release"], "tag_proof") == tag
    again = advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=tag, actor=actor)
    assert again["updated"] is False     # the same identity again is a confirmation
    done = advance_release(proj, SLUG, "1.1.0", "completed", actor=actor)
    assert done["release"]["status"] == "completed"
    assert done["release"]["tag_proof"] == out["release"]["tag_proof"]


@pytest.mark.parametrize("bad, message", [
    ({**TAG, "commit": "f" * 40, "object": "f" * 40}, "release commit"),
    ({**TAG, "object": TAG_OBJECT}, "lightweight"),
    ({**ANNOTATED, "object": COMMIT}, "annotated"),
    ({"name": "v1.1.0", "commit": COMMIT}, "needs the keys"),
    ({**TAG, "extra": 1}, "unknown keys"),
    ({**TAG, "object": "zz"}, "hex commit id"),
    ({**TAG, "name": "refs/tags/v1.1.0"}, "tag name"),
    ({**TAG, "recorded_at": "2026-01-01T00:00:00+00:00"}, "unknown keys"),
])
def test_an_inconsistent_tag_proof_is_refused(proj, bad, message):
    actor = _git_record(proj, executor="own")
    before = _snapshot(proj)
    with pytest.raises((ValueError, TypeError), match=message):
        advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=bad, actor=actor)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("name", ["v1.2.0", "release-1.1.0", "v1.1.0-rc1"])
def test_a_release_dct_executes_is_tagged_with_a_spelling_of_its_version(proj, name):
    """dct's own releases are tagged v<V> (any equivalent spelling of V);
    an own tool's tag name is its own format and only has to be reported."""
    actor = _git_record(proj, consent="no-push")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="not a tag of 1.1.0"):
        advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof={**TAG, "name": name},
                        actor=actor)
    assert _snapshot(proj) == before


def test_an_own_tool_reports_its_own_tag_name(proj):
    actor = _git_record(proj, executor="own")
    out = advance_release(proj, SLUG, "1.1.0", "tagged",
                          tag_proof={**TAG, "name": "release-1.1.0"}, actor=actor)
    assert _proof(out["release"], "tag_proof")["name"] == "release-1.1.0"


def test_the_tag_proof_is_immutable_and_never_premature(proj):
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND, release_mode="git")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="tag_proof is recorded on the move to 'tagged'"):
        advance_release(proj, SLUG, "1.1.0", "committed", release_commit_sha=COMMIT,
                        tag_proof=TAG)
    assert _snapshot(proj) == before
    advance_release(proj, SLUG, "1.1.0", "tagged", release_commit_sha=COMMIT, tag_proof=TAG)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="tag_proof .*immutable"):
        advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=ANNOTATED)
    assert _snapshot(proj) == before


def test_a_consented_push_needs_the_verified_approved_target(proj):
    """The recorded scope includes a push (consent.push_target): 'pushed'
    and 'completed' need the verified target — exactly the approved one, the
    tag there at the release commit."""
    actor = _git_record(proj, consent=TARGET)
    advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=TAG, actor=actor)
    before = _snapshot(proj)
    for status in ("pushed", "completed"):
        with pytest.raises(ValueError, match="push_proof"):
            advance_release(proj, SLUG, "1.1.0", status, actor=actor)
    elsewhere = {**PUSH, "target": {**TARGET, "url": "/tmp/other.git"}}
    with pytest.raises(ValueError, match="not the approved push target"):
        advance_release(proj, SLUG, "1.1.0", "pushed", push_proof=elsewhere, actor=actor)
    with pytest.raises(ValueError, match="release commit"):
        advance_release(proj, SLUG, "1.1.0", "pushed",
                        push_proof={**PUSH, "tag_commit": "f" * 40}, actor=actor)
    assert _snapshot(proj) == before
    out = advance_release(proj, SLUG, "1.1.0", "pushed", push_proof=PUSH, actor=actor)
    assert _proof(out["release"], "push_proof") == PUSH
    with pytest.raises(ValueError, match="push_proof .*immutable"):
        advance_release(proj, SLUG, "1.1.0", "pushed",
                        push_proof={**PUSH, "branch_tip": "f" * 40}, actor=actor)
    done = advance_release(proj, SLUG, "1.1.0", "completed", actor=actor)
    assert done["release"]["status"] == "completed"


def test_an_explicit_no_push_release_completes_without_a_push_and_takes_none(proj):
    """consent.push_target null is the EXPLICIT local release: it completes
    from 'tagged' with the tag proof only; a push proof contradicts it."""
    actor = _git_record(proj, consent="no-push")
    advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=TAG, actor=actor)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="no push was approved"):
        advance_release(proj, SLUG, "1.1.0", "pushed", push_proof=PUSH, actor=actor)
    with pytest.raises(ValueError, match="push_proof"):
        advance_release(proj, SLUG, "1.1.0", "pushed", actor=actor)
    assert _snapshot(proj) == before
    done = advance_release(proj, SLUG, "1.1.0", "completed", actor=actor)
    assert done["release"]["status"] == "completed" and done["release"]["push_proof"] is None


def test_a_record_without_consent_reports_its_own_push_or_none(proj):
    """An own tool's record has no recorded push scope: 'pushed' still needs
    a push proof (its own target), 'completed' from 'tagged' does not."""
    actor = _git_record(proj, executor="own")
    advance_release(proj, SLUG, "1.1.0", "tagged", tag_proof=TAG, actor=actor)
    with pytest.raises(ValueError, match="push_proof"):
        advance_release(proj, SLUG, "1.1.0", "pushed", actor=actor)
    own_target = {"target": {"remote": "upstream", "tag": "v1.1.0"}, "tag_commit": COMMIT}
    out = advance_release(proj, SLUG, "1.1.0", "pushed", push_proof=own_target, actor=actor)
    assert _proof(out["release"], "push_proof") == {
        "target": {"remote": "upstream", "url": None, "branch": None, "tag": "v1.1.0"},
        "tag_commit": COMMIT, "branch_tip": None}


def test_a_release_without_git_has_no_tag_or_push_proof(proj):
    """without_git is its own explicit path: no tag, no push — and a proof
    of either is refused, not stored."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], release_mode="without_git")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="without git"):
        advance_release(proj, SLUG, "1.1.0", "completed", manifest=MANIFEST, tag_proof=TAG)
    with pytest.raises(ValueError, match="without git"):
        advance_release(proj, SLUG, "1.1.0", "completed", manifest=MANIFEST, push_proof=PUSH)
    assert _snapshot(proj) == before
    done = advance_release(proj, SLUG, "1.1.0", "completed", manifest=MANIFEST)
    assert (done["release"]["status"], done["release"]["tag_proof"]) == ("completed", None)


def _legacy_status(engine, status):
    """A record written before the proof fields existed: `status` reached
    without any tag / push proof (raw SQL — the API no longer allows it)."""
    a = _add(engine, "A")
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    with engine.begin() as conn:
        conn.execute(releases.update().values(status=status, release_commit_sha=COMMIT))


def test_an_older_tagged_record_needs_the_proof_to_go_on(proj):
    """A missing field is never taken as proof (G3): a record that reached
    'tagged' before the field existed completes only once the tag identity
    is supplied."""
    _legacy_status(proj, "tagged")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="tag_proof"):
        advance_release(proj, SLUG, "1.1.0", "completed")
    assert _snapshot(proj) == before
    done = advance_release(proj, SLUG, "1.1.0", "completed", tag_proof=TAG)
    assert done["release"]["status"] == "completed"
    assert _proof(done["release"], "tag_proof") == TAG


def test_old_completed_history_gets_no_fictitious_confirmation(proj):
    """An old 'completed' record without proofs stays as it is: a repeat is
    a no-op that writes nothing and invents no proof."""
    _legacy_status(proj, "completed")
    before = _snapshot(proj)
    out = advance_release(proj, SLUG, "1.1.0", "completed")
    assert out["updated"] is False and out["release"]["tag_proof"] is None
    assert _snapshot(proj) == before
    with pytest.raises(ValueError, match="terminal"):
        advance_release(proj, SLUG, "1.1.0", "completed", tag_proof=TAG)
    assert _snapshot(proj) == before


@pytest.mark.parametrize("status, kwargs, error", [
    ("committed", {}, None),
    ("tagged", {}, "tag_proof"),
    ("completed", {}, "tag_proof"),
    ("tagged", {"tag_proof": TAG}, None),
    ("completed", {"tag_proof": ANNOTATED}, None),
    ("completed", {"tag_proof": {**TAG, "commit": "f" * 40, "object": "f" * 40}},
     "release commit"),
    ("pushed", {"tag_proof": TAG}, "push_proof"),
    ("pushed", {"tag_proof": TAG, "push_proof": PUSH}, None),
    ("committed", {"tag_proof": TAG}, "recorded on the move to 'tagged'"),
])
def test_reconstruction_follows_the_same_tag_and_push_rules(proj, status, kwargs, error):
    a = _add(proj, "A")
    with proj.begin() as conn:  # a legacy stamp without a record
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version="1.0.0"))
    call = dict(entry_ids=[a], candidate_sha=CAND, release_commit_sha=COMMIT,
                manifest=MANIFEST, status=status, confirm=True, **{
                    k: ({**v, "name": "v1.0.0"} if k == "tag_proof" else
                        {**v, "target": {**v["target"], "tag": "v1.0.0"}})
                    for k, v in kwargs.items()})
    before = _snapshot(proj)
    if error:
        with pytest.raises(ValueError, match=error):
            reconstruct_release(proj, SLUG, "1.0.0", **call)
        assert _snapshot(proj) == before
        return
    record = reconstruct_release(proj, SLUG, "1.0.0", **call)
    assert record["status"] == status
    assert (record["tag_proof"] is not None) == ("tag_proof" in kwargs)
    assert (record["push_proof"] is not None) == ("push_proof" in kwargs)


def test_reconstruction_without_git_takes_no_tag_proof(proj):
    a = _add(proj, "A")
    with proj.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(version="1.0.0"))
    with pytest.raises(ValueError, match="without git"):
        reconstruct_release(proj, SLUG, "1.0.0", entry_ids=[a], candidate_sha=None,
                            release_commit_sha=None, manifest=MANIFEST, status="completed",
                            confirm=True, without_git=True,
                            tag_proof={**TAG, "name": "v1.0.0"})
    record = reconstruct_release(proj, SLUG, "1.0.0", entry_ids=[a], candidate_sha=None,
                                 release_commit_sha=None, manifest=MANIFEST,
                                 status="completed", confirm=True, without_git=True)
    assert record["status"] == "completed" and record["tag_proof"] is None


def test_three_bootstraps_heal_the_proof_columns_then_converge(proj, capsys):
    """Schema change (tag_proof / push_proof): an existing database gets the
    columns on bootstrap 1; bootstraps 2-3 issue no DDL, no write and print
    nothing; the existing record keeps its data and gets no invented proof."""
    import re

    from sqlalchemy import event

    from dct.db import create_tables

    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    columns = ("tag_proof", "push_proof")

    def present():
        return {c["name"] for c in sa.inspect(proj).get_columns("releases")}

    with proj.begin() as conn:
        for column in set(columns) & present():
            conn.execute(sa.text(f"ALTER TABLE releases DROP COLUMN {column}"))
    seen: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.strip())

    event.listen(proj, "before_cursor_execute", listener)
    try:
        create_tables(proj)
        assert set(columns) <= present(), "bootstrap 1 adds the proof columns"
        capsys.readouterr()
        for n in (2, 3):
            seen.clear()
            create_tables(proj)
            assert [s for s in seen if re.match(r"\s*(ALTER|CREATE|DROP)\b", s, re.IGNORECASE)] == [], n
            assert [s for s in seen if re.match(r"\s*(INSERT|UPDATE|DELETE)\b", s, re.IGNORECASE)
                    and re.search(r"\b(releases|projects)\b", s, re.IGNORECASE)] == [], n
            out = capsys.readouterr()
            assert out.out == "" and out.err == "", f"bootstrap {n} printed output"
    finally:
        event.remove(proj, "before_cursor_execute", listener)
    record = get_release(proj, SLUG, "1.1.0")
    assert (record["status"], record["tag_proof"], record["push_proof"]) == (
        "stamped", None, None)


# ── K-115 G4: the effects layer supplies the proofs it verified ─────────────


@needs_git
def test_apply_records_the_verified_tag_and_push(release_repo):
    from test_release_effects import SLUG as R
    from test_release_effects import apply, proven, remote_refs

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a])
    record = get_release(engine, R, "1.1.0")
    head = git(repo, "rev-parse", "HEAD")
    assert record["status"] == "completed" and record["release_commit_sha"] == head
    assert _proof(record, "tag_proof") == {
        "name": "v1.1.0", "object": git(repo, "rev-parse", "refs/tags/v1.1.0"),
        "commit": head, "annotated": False}
    assert _proof(record, "push_proof") == {
        "target": record["consent"]["push_target"],
        "tag_commit": remote_refs(repo)["refs/tags/v1.1.0"],
        "branch_tip": remote_refs(repo)["refs/heads/main"]}
    assert record["consent"]["executor"] == "dct"


@needs_git
def test_a_local_release_records_the_tag_and_no_push(release_repo):
    from test_release_effects import SLUG as R
    from test_release_effects import apply, proven

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True)
    record = get_release(engine, R, "1.1.0")
    assert record["status"] == "completed" and record["consent"]["push_target"] is None
    assert _proof(record, "tag_proof")["name"] == "v1.1.0" and record["push_proof"] is None


@needs_git
def test_resume_records_the_identity_of_an_annotated_tag_it_found(release_repo):
    """The owner created an ANNOTATED tag at the release commit after an
    interruption: resume verifies it and records what it resolves to (the
    tag object) and the commit it targets."""
    from test_release_effects import (
        SLUG as R,
    )
    from test_release_effects import (
        Interrupted,
        apply,
        interrupt_at,
        proven,
        resume,
    )

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True,
              on_point=interrupt_at("after_committed"))
    git(repo, "tag", "-a", "v1.1.0", "-m", "release 1.1.0")
    assert resume(engine, repo)["outcome"] == "completed"
    proof = _proof(get_release(engine, R, "1.1.0"), "tag_proof")
    head = git(repo, "rev-parse", "HEAD")
    assert proof == {"name": "v1.1.0", "object": git(repo, "rev-parse", "refs/tags/v1.1.0"),
                     "commit": head, "annotated": True}
    assert proof["object"] != head


@needs_git
@pytest.mark.parametrize("point", ["after_tagged", "after_pushed"])
def test_resume_supplies_a_proof_an_older_record_lacks(release_repo, point):
    """A record that reached 'tagged' / 'pushed' before the proof fields
    existed (seeded: the proofs removed with raw SQL): resume re-verifies
    the tag and the push in the repository and records them with its next
    progress write — never a completion on the bare status."""
    from test_release_effects import (
        SLUG as R,
    )
    from test_release_effects import (
        Interrupted,
        apply,
        interrupt_at,
        proven,
        resume,
    )

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at(point))
    with engine.begin() as conn:
        conn.execute(releases.update().values(tag_proof=None, push_proof=None))
    assert resume(engine, repo)["outcome"] == "completed"
    record = get_release(engine, R, "1.1.0")
    assert _proof(record, "tag_proof")["commit"] == record["release_commit_sha"]
    assert _proof(record, "push_proof")["target"] == record["consent"]["push_target"]


@needs_git
def test_reconstruct_records_the_tag_it_verified(release_repo):
    from test_release_effects import SLUG as R
    from test_release_effects import apply, proven

    from dct.workflows.release import effects

    engine, repo, _ = release_repo
    a = proven(engine, repo, "feature a")
    apply(engine, repo, version="1.1.0", entry_ids=[a], no_push=True)
    with engine.begin() as conn:  # the release as a legacy one: no record
        conn.execute(releases.delete())
    out = effects.reconstruct(engine, R, "1.1.0", confirm=True, cwd=str(repo))
    head = git(repo, "rev-parse", "HEAD")
    assert out["details"]["evidence"]["tag_proof"] == {
        "name": "v1.1.0", "object": head, "commit": head, "annotated": False}
    record = get_release(engine, R, "1.1.0")
    assert record["status"] == "completed" and record["consent"] is None
    assert _proof(record, "tag_proof") == out["details"]["evidence"]["tag_proof"]
    assert record["push_proof"] is None    # no confirmation invented for old history


# ── K-115 G6: an ambiguous ledger refuses WRITES ────────────────────────────


def _duplicate(engine, spelling="1.1"):
    """A second LIVE record of the same SEMANTIC version under another
    spelling (raw SQL — the textual unique index does not see it)."""
    with engine.begin() as conn:
        row = dict(conn.execute(sa.select(releases)).one()._mapping)
        first = row.pop("id")
        row["version"] = spelling
        second = conn.execute(releases.insert().values(**row)
                              .returning(releases.c.id)).scalar_one()
    return first, second


@pytest.mark.parametrize("write", ["advance", "abandon", "stamp re-run", "reconstruct",
                                   "import", "entry-set gate"])
def test_duplicates_of_one_semantic_version_refuse_every_write(proj, write):
    """Two live records of 1.1.0 (1.1.0 and 1.1): a write never picks the
    first one — it refuses and names both."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    first, second = _duplicate(proj)
    calls = {
        "advance": lambda: advance_release(proj, SLUG, "1.1.0", "files_written",
                                           manifest=MANIFEST),
        "abandon": lambda: abandon_release(proj, SLUG, "1.1", "why", confirm=True),
        "stamp re-run": lambda: release_entries(proj, SLUG, "v1.1.0", entry_ids=[a]),
        "reconstruct": lambda: reconstruct_release(
            proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND, release_commit_sha=None,
            manifest=None, status="stamped", confirm=True),
        "import": lambda: import_released_entry(proj, SLUG, "added", "late", "1.1.0"),
        "entry-set gate": lambda: verify_release_entry_set(proj, SLUG, "1.1.0"),
    }
    before = _snapshot(proj)
    with pytest.raises(ValueError) as exc:
        calls[write]()
    message = str(exc.value)
    assert "2 live records of the same version" in message
    assert f"#{first} (1.1.0" in message and f"#{second} (1.1" in message
    assert _snapshot(proj) == before


def test_duplicates_stay_readable_and_are_named(proj):
    """Diagnostic reads keep working: the record is returned with the
    `duplicates` list naming every live record of that version."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    assert "duplicates" not in get_release(proj, SLUG, "1.1.0")
    first, second = _duplicate(proj)
    for record in (get_release(proj, SLUG, "1.1.0"), get_release(proj, SLUG, "v1.1"),
                   get_release(proj, SLUG)):
        assert record is not None and record["entry_ids"] == [a]
        assert record["duplicates"] == [
            {"id": first, "version": "1.1.0", "status": "stamped"},
            {"id": second, "version": "1.1", "status": "stamped"}]


def test_the_duplicate_refusal_also_guards_a_new_stamp(proj):
    """The same version already recorded twice with ANOTHER set: the stamp
    refuses on the ambiguity instead of comparing with the first record."""
    a, b = _add(proj, "A"), _add(proj, "B")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    _duplicate(proj)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="2 live records of the same version"):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a, b])
    assert _snapshot(proj) == before


# ── K-115 G5 / G9: record, consent, proposal and render agree with EACH OTHER


def _bound_proposal(**changes):
    return {**PROPOSAL, "version": "1.1.0", "candidate_sha": CAND, "release_mode": "git",
            **changes}


@pytest.mark.parametrize("consent, message", [
    (lambda a: _consent([a], version="1.2.0"), "consent.version"),
    (lambda a: _consent([a, a + 100]), "consent.entry_ids"),
    (lambda a: {**_consent([a]), "candidate_sha": "f" * 40}, "consent.candidate_sha"),
    (lambda a: _consent([a], render={"pr_refs": True}), "consent.render"),
    (lambda a: _consent([a], proposal_sha256="9" * 64), "consent.proposal_sha256"),
])
def test_a_stamp_refuses_a_consent_that_contradicts_its_record(proj, consent, message):
    """Each JSON is valid alone; together they describe different releases —
    refused before any write (the stamp transaction rolls back)."""
    a = _add(proj, "A")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=message):
        release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND,
                        consent=consent(a), render={"pr_refs": False})
    assert _snapshot(proj) == before
    assert get_release(proj, SLUG, "1.1.0") is None


@pytest.mark.parametrize("change, message", [
    ({"version": "1.2.0"}, "proposal.version"),
    ({"candidate_sha": "f" * 40}, "proposal.candidate_sha"),
    ({"release_mode": "without_git"}, "proposal.release_mode"),
])
def test_a_stamp_refuses_a_proposal_bound_to_another_release(proj, change, message):
    a = _add(proj, "A")
    proposal = _bound_proposal(**change)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=message):
        release_entries(
            proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND, release_mode="git",
            proposal=proposal,
            consent=_consent([a], proposal_sha256=proposal_digest(proposal)))
    assert _snapshot(proj) == before


def test_a_consistent_record_is_accepted_and_names_its_proposal(proj):
    a = _add(proj, "A")
    proposal = _bound_proposal()
    out = release_entries(
        proj, SLUG, "v1.1.0", entry_ids=[a], candidate_sha=CAND, release_mode="git",
        proposal=proposal, render={"pr_refs": True},
        consent=_consent([a], version="1.1", render={"pr_refs": True},
                         proposal_sha256=proposal_digest(proposal), push_target=TARGET))
    record = out["release"]
    assert record["consent"]["proposal_sha256"] == proposal_digest(record["proposal"])


def test_a_consent_written_later_must_fit_the_record_too(proj):
    """advance_release fills a write-once field — the SAME mutual check."""
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="consent.entry_ids"):
        advance_release(proj, SLUG, "1.1.0", "stamped", consent=_consent([a, a + 1]))
    with pytest.raises(ValueError, match="consent.proposal_sha256"):
        advance_release(proj, SLUG, "1.1.0", "stamped",
                        consent=_consent([a], proposal_sha256="9" * 64))
    with pytest.raises(ValueError, match="proposal.version"):
        advance_release(proj, SLUG, "1.1.0", "stamped",
                        proposal=_bound_proposal(version="3.0.0"))
    assert _snapshot(proj) == before
    out = advance_release(proj, SLUG, "1.1.0", "stamped", consent=_consent([a]))
    assert out["release"]["consent"]["entry_ids"] == [a]


@pytest.mark.parametrize("field, value, message", [
    ("consent", {"version": "1.1.0", "entry_ids": [999], "candidate_sha": CAND},
     "consent.entry_ids"),
    ("consent", {"version": "9.9.9", "entry_ids": None, "candidate_sha": CAND},
     "consent.version"),
    ("render", {"pr_refs": True}, "consent.render"),
])
def test_a_pre_existing_contradiction_blocks_progress(proj, field, value, message):
    """Seeded with raw SQL (outside the API): the record and its consent
    describe different releases — no progress is recorded on it, nothing is
    repaired by widening either side."""
    import json

    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND,
                    consent=_consent([a], render={"pr_refs": False}),
                    render={"pr_refs": False})
    if isinstance(value, dict) and value.get("entry_ids", 0) is None:
        value = {**value, "entry_ids": [a]}
    with proj.begin() as conn:
        conn.execute(releases.update().values({field: json.dumps(value)}))
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=message):
        advance_release(proj, SLUG, "1.1.0", "files_written", manifest=MANIFEST, actor="dct")
    with pytest.raises(ValueError, match=message):
        advance_release(proj, SLUG, "1.1.0", "stamped", actor="dct")
    assert _snapshot(proj) == before


# ── the MCP and CLI surfaces of the ledger path carry the same rules ────────


@pytest.fixture
def mcp(proj, monkeypatch):
    from dct.mcp import server as S

    monkeypatch.setattr(S, "_get_engine", lambda: proj)
    return S


def test_mcp_advance_reports_the_tag_and_the_push(mcp, proj):
    """The own tool's path end to end through the MCP tools: tagged needs
    the tag proof, pushed the push proof; get_release shows both."""
    set_release_executor(proj, SLUG, "own")
    a = _add(proj, "A")
    out = mcp.release_changelog(project=SLUG, version="1.1.0", entry_ids=[a],
                                candidate_sha=CAND, expected_executor="own")
    assert out["release"]["executor"] == "own"
    assert out["release"]["tag_proof"] is None and out["release"]["push_proof"] is None
    mcp.advance_release("1.1.0", "committed", project=SLUG, release_commit_sha=COMMIT)
    refused = mcp.advance_release("1.1.0", "tagged", project=SLUG)
    assert "tag_proof" in refused["error"]
    own_tag = {**TAG, "name": "release-1.1.0"}
    assert mcp.advance_release("1.1.0", "tagged", project=SLUG, tag_proof=own_tag) == {
        "project": SLUG, "version": "1.1.0", "status": "tagged", "updated": True}
    assert "push_proof" in mcp.advance_release("1.1.0", "pushed", project=SLUG)["error"]
    push = {"target": {"remote": "origin", "tag": "release-1.1.0"}, "tag_commit": COMMIT}
    assert mcp.advance_release("1.1.0", "pushed", project=SLUG, push_proof=push)["updated"]
    assert mcp.advance_release("1.1.0", "completed", project=SLUG)["status"] == "completed"
    record = mcp.get_release(project=SLUG, version="1.1.0")["release"]
    assert record["tag_proof"]["name"] == "release-1.1.0"
    assert record["tag_proof"]["recorded_at"] and record["push_proof"]["recorded_at"]
    assert record["push_proof"]["tag_commit"] == COMMIT


@pytest.mark.parametrize("bad", [
    {"tag_proof": ["v1.1.0"]}, {"tag_proof": {"name": "v1.1.0"}},
    {"tag_proof": {**TAG, "annotated": "no"}}, {"push_proof": {"target": {"remote": "o"}}},
    {"push_proof": {"target": "origin", "tag_commit": COMMIT}},
])
def test_mcp_advance_returns_an_error_for_a_malformed_proof(mcp, proj, bad):
    a = _add(proj, "A")
    mcp.release_changelog(project=SLUG, version="1.1.0", entry_ids=[a], candidate_sha=CAND)
    mcp.advance_release("1.1.0", "committed", project=SLUG, release_commit_sha=COMMIT)
    before = _snapshot(proj)
    out = mcp.advance_release("1.1.0", "pushed", project=SLUG, **{"tag_proof": TAG, **bad})
    assert "error" in out and set(out) == {"error"}
    assert _snapshot(proj) == before


def test_mcp_stamp_binds_the_expected_executor(mcp, proj):
    a = _add(proj, "A")
    before = _snapshot(proj)
    out = mcp.release_changelog(project=SLUG, version="1.1.0", entry_ids=[a],
                                expected_executor="own")
    assert "expects the executor 'own'" in out["error"]
    assert "release executor must be one of" in mcp.release_changelog(
        project=SLUG, version="1.1.0", entry_ids=[a], expected_executor="mine")["error"]
    assert _snapshot(proj) == before
    assert mcp.release_changelog(project=SLUG, version="1.1.0", entry_ids=[a],
                                 expected_executor="dct")["release"]["executor"] == "dct"


def test_mcp_names_the_duplicates_on_read_and_refuses_them_on_write(mcp, proj):
    a = _add(proj, "A")
    release_entries(proj, SLUG, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    first, second = _duplicate(proj)
    record = mcp.get_release(project=SLUG, version="1.1.0")["release"]
    assert [d["id"] for d in record["duplicates"]] == [first, second]
    for out in (mcp.advance_release("1.1.0", "files_written", project=SLUG, manifest=MANIFEST),
                mcp.abandon_release("1.1.0", "why", confirm=True, project=SLUG),
                mcp.release_changelog(project=SLUG, version="1.1.0", entry_ids=[a])):
        assert "2 live records of the same version" in out["error"]


def test_cli_release_advance_takes_the_proofs_as_json(cli_engine, tmp_path, monkeypatch,
                                                     capsys):
    """`dct clog release-advance` — the same ledger rules: refused without
    the tag proof, recorded with it; malformed JSON is a clean refusal."""
    import json

    from conftest import run_cli

    engine, slug = cli_engine, "proj-c"   # cli_engine seeds proj-a itself
    create_project(engine, slug=slug, name="Project A", path=str(tmp_path),
                   current_version="1.0.0")
    a = _add(engine, "A", slug)
    release_entries(engine, slug, "1.1.0", entry_ids=[a], candidate_sha=CAND)
    advance_release(engine, slug, "1.1.0", "committed", release_commit_sha=COMMIT)
    monkeypatch.chdir(tmp_path)
    for argv, message in (
        (["tagged"], "tag_proof"),
        (["tagged", "--tag-proof", "{not json"], "--tag-proof must be a JSON object"),
        (["tagged", "--tag-proof", '["v1.1.0"]'], "tag_proof must be a JSON object"),
        (["pushed", "--tag-proof", json.dumps(TAG)], "push_proof"),
    ):
        before = _snapshot(engine)
        with pytest.raises(SystemExit) as exc:
            run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", *argv, "-P", slug])
        assert exc.value.code == 1 and message in capsys.readouterr().err
        assert _snapshot(engine) == before
    run_cli(monkeypatch, ["clog", "release-advance", "1.1.0", "pushed", "-P", slug,
                          "--tag-proof", json.dumps(TAG), "--push-proof", json.dumps(PUSH)])
    assert "committed → pushed" in capsys.readouterr().out
    record = get_release(engine, slug, "1.1.0")
    assert _proof(record, "tag_proof") == TAG and _proof(record, "push_proof") == PUSH
    run_cli(monkeypatch, ["clog", "release-status", "--version", "1.1.0", "-P", slug])
    shown = capsys.readouterr().out
    assert f"tag proof:     v1.1.0 → {COMMIT}" in shown and "push proof:    origin main" in shown
