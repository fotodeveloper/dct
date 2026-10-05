"""Round-3 ledger contracts (R7a): K-86/K-93 A3 (one legacy-spelling
resolver), K-87 (reconstruction checks the FINAL spelling's width), K-92/K-94
B4 (JSON caps in canonical UTF-8 bytes, on the MERGED manifest), K-94 B3
(entry-set gate), the new ledger fields (release_mode, executor, consent,
proposal, render — K-85/K-90/K-97/K-76) and K-93 A2 (the bootstrap heals the
releases table's required index).

Both lanes unless a test says otherwise (PG skips name their reason).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import run_cli
from sqlalchemy import event

from dct.core.changelog import (
    add_entry,
    release_entries,
    resolve_version_spelling,
    spelling_rule,
)
from dct.core.projects import (
    create_project,
    get_project,
    set_changelog_pr_refs,
    set_release_executor,
)
from dct.core.releases import (
    MANIFEST_MAX_BYTES,
    advance_release,
    get_release,
    json_size,
    proposal_digest,
    reconstruct_release,
    validate_consent,
    validate_proposal,
    validate_render,
    verify_release_entry_set,
)
from dct.db import changelog_entries, create_tables, projects, releases
from dct.export import export_changelog_md

CAND = "c" * 40
COMMIT = "d" * 40
MANIFEST = {"changelog": {"path": "changelog.md", "sha256": "1" * 64, "section_date": "2026-10-01"}}
_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    return engine


def _add(engine, text):
    return add_entry(engine, "proj-a", "added", text)["id"]


def _stamp_raw(engine, entry_id, version, released_at):
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == entry_id)
                     .values(version=version, released_at=released_at))


def _set_current(engine, version):
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == "proj-a")
                     .values(current_version=version))


def _snapshot(engine):
    """Every row of every table — a refused call must leave all of it."""
    with engine.begin() as conn:
        out = {}
        for name in sorted(sa.inspect(conn).get_table_names()):
            table = sa.Table(name, sa.MetaData(), autoload_with=conn)
            out[name] = sorted((tuple(r) for r in conn.execute(sa.select(table))), key=repr)
        return out


def _reconstruct(engine, version, ids, **kw):
    kwargs = {"entry_ids": ids, "candidate_sha": CAND, "release_commit_sha": COMMIT,
              "manifest": MANIFEST, "status": "completed", "confirm": True}
    kwargs.update(kw)
    return reconstruct_release(engine, "proj-a", version, **kwargs)


# ── K-86 / K-93 A3: ONE legacy-spelling resolver ─────────────────────────────

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_requesting_1_1_0_for_a_stored_1_1_reconstructs_under_1_1(proj):
    a = _add(proj, "a")
    _stamp_raw(proj, a, "1.1", T0)
    _set_current(proj, "1.1")
    rec = _reconstruct(proj, "1.1.0", [a])
    assert rec["version"] == "1.1"
    with proj.begin() as conn:
        assert conn.execute(sa.select(changelog_entries.c.version)).scalar() == "1.1"


def test_equivalent_spellings_resolve_to_the_first_stamped_entry(proj):
    """Earliest released_at wins whatever the id order; the equivalence map
    keeps every stored spelling (nothing rewritten)."""
    a, b = _add(proj, "a"), _add(proj, "b")
    _stamp_raw(proj, a, "1.1", T0 + timedelta(hours=1))
    _stamp_raw(proj, b, "v1.1.0", T0)
    _set_current(proj, "1.1.0")
    with proj.begin() as conn:
        pid = conn.execute(sa.select(projects.c.id)).scalar()
        resolved = resolve_version_spelling(conn, pid, "1.1")
    assert resolved["authoritative"] == "v1.1.0" and resolved["source"] == "entries"
    assert resolved["spellings"] == {"1.1": [a], "v1.1.0": [b]}
    assert resolved["equivalent"] == ["1.1", "1.1.0", "v1.1.0"] and resolved["several"]
    rec = _reconstruct(proj, "1.1", [a, b])
    assert rec["version"] == "1.1.0"  # the resolved spelling, normalized
    with proj.begin() as conn:
        stored = {r.id: r.version for r in conn.execute(
            sa.select(changelog_entries.c.id, changelog_entries.c.version))}
    assert stored == {a: "1.1", b: "v1.1.0"}


def test_a_released_at_tie_goes_to_the_lowest_id_and_null_time_comes_last(proj):
    a, b, c = _add(proj, "a"), _add(proj, "b"), _add(proj, "c")
    _stamp_raw(proj, a, "1.1.0", None)   # unknown time: after every known one
    _stamp_raw(proj, b, "1.1", T0)
    _stamp_raw(proj, c, "v1.1", T0)      # same time as b, higher id
    entries = [{"id": a, "version": "1.1.0", "released_at": None},
               {"id": b, "version": "1.1", "released_at": T0},
               {"id": c, "version": "v1.1", "released_at": T0}]
    assert spelling_rule([], entries) == "1.1"
    assert spelling_rule([], list(reversed(entries))) == "1.1"
    assert spelling_rule(["1.1.0"], entries) == "1.1.0"  # a record wins
    _set_current(proj, "1.1")
    assert _reconstruct(proj, "1.1.0", [a, b, c])["version"] == "1.1"


def test_export_and_reconstruction_share_the_spelling(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    _stamp_raw(proj, a, "1.1.0", T0 + timedelta(days=1))
    _stamp_raw(proj, b, "1.1", T0)
    _set_current(proj, "1.1")
    before = export_changelog_md(proj, "proj-a", version="1.1.0")
    assert before.startswith("## [1.1] - ")
    rec = _reconstruct(proj, "1.1.0", [a, b])
    assert rec["version"] == "1.1"
    assert export_changelog_md(proj, "proj-a", version="1.1.0") == before


def test_resolver_reports_the_record_and_the_current_version(proj):
    release_entries(proj, "proj-a", "1.2", entry_ids=[])
    with proj.begin() as conn:
        pid = conn.execute(sa.select(projects.c.id)).scalar()
        resolved = resolve_version_spelling(conn, pid, "1.2.0")
        unknown = resolve_version_spelling(conn, pid, "9.9")
    assert (resolved["authoritative"], resolved["source"], resolved["record"]) == (
        "1.2", "record", "1.2")
    assert resolved["current_version"] == "1.2" and resolved["spellings"] == {}
    assert (unknown["authoritative"], unknown["source"], unknown["several"]) == (None, None, False)


def test_a_reconstruction_conflicting_with_a_record_is_still_refused(proj):
    """Equivalent spellings never mask a REAL contradiction: an existing
    record of the version refuses, nothing is re-stamped or rewritten."""
    a = _add(proj, "a")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="already has a release record"):
        _reconstruct(proj, "1.1", [a])
    assert _snapshot(proj) == before


# ── K-87: the FINAL spelling must fit the schema ─────────────────────────────


def test_an_overlong_legacy_spelling_is_refused_without_any_write(proj):
    """SQLite stores an overlong VARCHAR (PostgreSQL never could): the
    reconstruction validates the value it would INSERT — controlled error,
    the full database unchanged (no record, no stamp, no version change)."""
    if proj.dialect.name == "postgresql":
        pytest.skip("PostgreSQL enforces VARCHAR(50): no overlong legacy row can exist")
    long_version = "1.2." + "9" * 60
    a = _add(proj, "a")
    _stamp_raw(proj, a, long_version, T0)
    _set_current(proj, long_version)
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="version too long"):
        _reconstruct(proj, long_version, [a])
    assert _snapshot(proj) == before


# ── K-92 / K-94 B4: caps in canonical UTF-8 bytes, merged manifest ───────────


def _canonical_size(value) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def stamped(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[], candidate_sha=CAND)
    return proj


def _row(engine):
    with engine.begin() as conn:
        return tuple(conn.execute(sa.select(releases)).one())


def test_two_patches_that_fit_alone_cannot_exceed_the_cap_together(stamped):
    advance_release(stamped, "proj-a", "1.1.0", "stamped", manifest={"a": "x" * 40_000})
    before = _row(stamped)
    with pytest.raises(ValueError, match="manifest .*too long"):
        advance_release(stamped, "proj-a", "1.1.0", "files_written",
                        manifest={"b": "y" * 40_000})
    assert _row(stamped) == before  # manifest, status and updated_at unchanged
    assert get_release(stamped, "proj-a", "1.1.0")["status"] == "stamped"


def test_repeating_the_same_patch_is_a_no_op(stamped):
    patch = {"a": "x" * 40_000}
    advance_release(stamped, "proj-a", "1.1.0", "stamped", manifest=patch)
    before = _row(stamped)
    out = advance_release(stamped, "proj-a", "1.1.0", "stamped", manifest=patch)
    assert out["updated"] is False and _row(stamped) == before


@pytest.mark.parametrize("extra,accepted", [(0, True), (1, False)])
def test_the_merged_cap_boundary_is_exact(stamped, extra, accepted):
    first = {"a": "x" * 1000}
    advance_release(stamped, "proj-a", "1.1.0", "stamped", manifest=first)
    # {"a":"x…","b":"<n>"} — sized so the MERGED manifest is cap + extra bytes.
    filler = MANIFEST_MAX_BYTES + extra - _canonical_size({**first, "b": ""})
    second = {"b": "y" * filler}
    assert _canonical_size({**first, **second}) == MANIFEST_MAX_BYTES + extra
    before = _row(stamped)
    if accepted:
        out = advance_release(stamped, "proj-a", "1.1.0", "files_written", manifest=second)
        assert out["release"]["manifest"] == {**first, **second}
        assert json_size(out["release"]["manifest"]) == MANIFEST_MAX_BYTES
    else:
        with pytest.raises(ValueError, match="too long"):
            advance_release(stamped, "proj-a", "1.1.0", "files_written", manifest=second)
        assert _row(stamped) == before


def test_multibyte_characters_count_as_their_utf8_bytes(stamped):
    """32 769 'ż' are fewer CHARACTERS than the cap but 65 538+ BYTES."""
    patch = {"a": "ż" * 32_769}
    assert len(json.dumps(patch, ensure_ascii=False)) < MANIFEST_MAX_BYTES
    before = _row(stamped)
    with pytest.raises(ValueError, match="bytes of canonical JSON"):
        advance_release(stamped, "proj-a", "1.1.0", "files_written", manifest=patch)
    assert _row(stamped) == before


# ── K-94 B3: the entry-set gate before any resume / retry effect ─────────────


def test_entry_set_gate_refuses_an_extra_or_lost_entry(proj):
    a, b = _add(proj, "a"), _add(proj, "b")
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[a], candidate_sha=CAND)
    assert verify_release_entry_set(proj, "proj-a")["entry_ids"] == [a]
    _stamp_raw(proj, b, "1.1", T0)  # a legacy stamp of the same version
    before = _snapshot(proj)
    with pytest.raises(ValueError, match=rf"not in the record: #{b}\b"):
        verify_release_entry_set(proj, "proj-a", "1.1.0")
    with pytest.raises(ValueError, match="no longer matches the record"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[a])
    assert _snapshot(proj) == before
    assert get_release(proj, "proj-a", "1.1.0")["entry_ids"] == [a]  # never extended
    _stamp_raw(proj, b, None, None)
    with proj.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == a)
                     .values(deleted_at=sa.func.current_timestamp()))
    with pytest.raises(ValueError, match=rf"no longer live with 1.1.0: #{a}\b"):
        verify_release_entry_set(proj, "proj-a")


def test_entry_set_gate_without_an_unfinished_record(proj):
    with pytest.raises(ValueError, match="No unfinished release"):
        verify_release_entry_set(proj, "proj-a")


# ── new ledger fields ────────────────────────────────────────────────────────


def test_executor_is_copied_at_stamp_time_and_never_taken_over(proj):
    assert get_project(proj, "proj-a")["release_executor"] == "dct"
    set_release_executor(proj, "proj-a", "own")
    rec = release_entries(proj, "proj-a", "1.1.0", entry_ids=[])["release"]
    assert rec["executor"] == "own"
    set_release_executor(proj, "proj-a", "dct")
    assert get_release(proj, "proj-a", "1.1.0")["executor"] == "own"
    with pytest.raises(ValueError, match="executor"):
        set_release_executor(proj, "proj-a", "mine")
    assert set_release_executor(proj, "nope", "own") is None


def test_render_is_persisted_per_release_from_the_project_setting(proj):
    assert get_project(proj, "proj-a")["changelog_pr_refs"] is False
    first = release_entries(proj, "proj-a", "1.1.0", entry_ids=[])["release"]
    assert first["render"] == {"pr_refs": False}
    set_changelog_pr_refs(proj, "proj-a", True)
    assert get_release(proj, "proj-a", "1.1.0")["render"] == {"pr_refs": False}
    second = release_entries(proj, "proj-a", "1.2.0", entry_ids=[])["release"]
    assert second["render"] == {"pr_refs": True}
    explicit = release_entries(proj, "proj-a", "1.3.0", entry_ids=[],
                               render={"pr_refs": False})["release"]
    assert explicit["render"] == {"pr_refs": False}
    with pytest.raises(TypeError):
        set_changelog_pr_refs(proj, "proj-a", "yes")


PROPOSAL = {
    "paths": ["pyproject.toml", "dct/__init__.py"],
    "base_blobs": {"pyproject.toml": "a" * 40, "dct/__init__.py": None},
    "expected_blobs": {"pyproject.toml": "B" * 40, "dct/__init__.py": "c" * 40},
    "script": {"identity": "scripts/dct-version-sync.sh", "sha256": "e" * 64},
    "candidate_sha": CAND,
}


def test_proposal_and_consent_are_canonical_and_write_once(stamped):
    proposal = validate_proposal(PROPOSAL)
    assert proposal["paths"] == ["dct/__init__.py", "pyproject.toml"]
    assert proposal["expected_blobs"]["pyproject.toml"] == "b" * 40
    digest = proposal_digest(PROPOSAL)
    assert digest == proposal_digest(proposal)
    consent = {"version": "v1.1.0", "entry_ids": [3, 1, 3], "candidate_sha": CAND,
               "proposal_sha256": digest, "render": {"pr_refs": True},
               "push_target": {"remote": "origin", "branch": "main", "tag": "v1.1.0"}}
    assert validate_consent(consent)["entry_ids"] == [1, 3]
    assert validate_consent(consent)["version"] == "1.1.0"
    out = advance_release(stamped, "proj-a", "1.1.0", "stamped",
                          proposal=PROPOSAL, consent=consent)
    rec = out["release"]
    assert rec["proposal"] == proposal and rec["consent"]["proposal_sha256"] == digest
    again = advance_release(stamped, "proj-a", "1.1.0", "stamped",
                            proposal=PROPOSAL, consent=consent)
    assert again["updated"] is False
    before = _row(stamped)
    changed = {**PROPOSAL, "expected_blobs": {**PROPOSAL["expected_blobs"],
                                              "pyproject.toml": "f" * 40}}
    with pytest.raises(ValueError, match="proposal .*immutable"):
        advance_release(stamped, "proj-a", "1.1.0", "stamped", proposal=changed)
    with pytest.raises(ValueError, match="consent .*immutable"):
        advance_release(stamped, "proj-a", "1.1.0", "stamped",
                        consent={**consent, "entry_ids": [1]})
    assert _row(stamped) == before


@pytest.mark.parametrize("bad", [
    {"paths": ["a"], "base_blobs": {}, "expected_blobs": {"a": "a" * 40}},
    {"paths": ["a"], "base_blobs": {"a": None}, "expected_blobs": {"a": None}},
    {"paths": ["/abs"], "base_blobs": {"/abs": None}, "expected_blobs": {"/abs": "a" * 40}},
    {"paths": ["a"], "base_blobs": {"a": None}, "expected_blobs": {"a": "zz"}},
    {"paths": ["a"], "base_blobs": {"a": None}, "expected_blobs": {"a": "a" * 40},
     "extra": 1},
    {"paths": ["a"], "base_blobs": {"a": None}, "expected_blobs": {"a": "a" * 40},
     "script": {"identity": ""}},
    {"paths": ["a"], "base_blobs": {"a": None}, "expected_blobs": {"a": "a" * 40},
     "script": {"identity": "s", "sha256": "E" * 64}},
    {"paths": ["a"], "expected_blobs": {"a": "a" * 40}},
])
def test_malformed_proposals_are_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        validate_proposal(bad)


@pytest.mark.parametrize("bad", [
    {"entry_ids": [1]}, {"version": "1.0"}, {"version": "1.0", "entry_ids": "1"},
    {"version": "nope", "entry_ids": []}, {"version": "1.0", "entry_ids": [0]},
    {"version": "1.0", "entry_ids": [], "push_target": {"branch": "main"}},
    {"version": "1.0", "entry_ids": [], "push_target": {"remote": " origin"}},
    {"version": "1.0", "entry_ids": [], "proposal_sha256": "abc"},
    {"version": "1.0", "entry_ids": [], "who": "me"},
    {"version": "1.0", "entry_ids": [], "render": {"pr_refs": 1}},
])
def test_malformed_consents_are_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        validate_consent(bad)


@pytest.mark.parametrize("bad", [{"pr_refs": "yes"}, {"pr_refs": 1}, {"branches": True}, [1]])
def test_malformed_render_is_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        validate_render(bad)


def test_a_rerun_contradicting_a_recorded_field_is_refused(proj):
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[], candidate_sha=CAND,
                    release_mode="git", render={"pr_refs": True})
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="release_mode .*immutable"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[], release_mode="without_git")
    with pytest.raises(ValueError, match="render .*immutable"):
        release_entries(proj, "proj-a", "1.1.0", entry_ids=[], render={"pr_refs": False})
    assert _snapshot(proj) == before
    same = release_entries(proj, "proj-a", "1.1.0", entry_ids=[], release_mode="git",
                           render={"pr_refs": True})
    assert same["release"]["release_mode"] == "git"


def test_cli_project_release_settings(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["project", "release-executor", "proj-a"])
    assert capsys.readouterr().out.strip() == "proj-a: dct"
    run_cli(monkeypatch, ["project", "release-executor", "proj-a", "own"])
    assert "release-executor → own" in capsys.readouterr().out
    assert get_project(cli_engine, "proj-a")["release_executor"] == "own"
    run_cli(monkeypatch, ["project", "changelog-pr-refs", "proj-a", "on"])
    assert "changelog-pr-refs → on" in capsys.readouterr().out
    assert get_project(cli_engine, "proj-a")["changelog_pr_refs"] is True
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["project", "release-executor", "proj-a", "mine"])
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["project", "changelog-pr-refs", "nope", "off"])
    assert exc.value.code == 1


# ── K-93 A2: the bootstrap heals the releases table's required index ─────────


def _index_names(engine, table):
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def _record_statements(engine) -> list[str]:
    seen: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *a: seen.append(statement.strip()))
    return seen


def test_bootstrap_creates_a_missing_releases_index_then_converges(proj, capsys):
    """A crash between CREATE TABLE releases and CREATE INDEX (SQLite)
    leaves the table without its unique guard — the next bootstrap creates
    it; bootstraps 2-3 issue no DDL, no DML on releases and print nothing."""
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    with proj.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_releases_project_version_live"))
    seen = _record_statements(proj)
    create_tables(proj)
    assert "ix_releases_project_version_live" in _index_names(proj, "releases")
    assert any("ix_releases_project_version_live" in st for st in seen if _DDL.match(st))
    capsys.readouterr()
    for n in (2, 3):
        seen.clear()
        create_tables(proj)
        assert [st for st in seen if _DDL.match(st)] == [], f"bootstrap {n} issued DDL"
        assert [st for st in seen if _DML.match(st) and "releases" in st] == []
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"


def test_duplicate_live_records_never_brick_the_bootstrap(proj, capsys):
    """The unique index refused by duplicate DATA: one warning, rows untouched,
    the bootstrap continues (the core still enforces one live record)."""
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[])
    with proj.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_releases_project_version_live"))
        row = dict(conn.execute(sa.select(releases)).one()._mapping)
        row.pop("id")
        conn.execute(releases.insert().values(**row))
    before = _snapshot(proj)
    create_tables(proj)
    err = capsys.readouterr().err
    assert "ix_releases_project_version_live" in err and "Nothing was changed" in err
    assert "ix_releases_project_version_live" not in _index_names(proj, "releases")
    assert _snapshot(proj) == before


def test_old_unrelated_tables_are_still_not_retrofitted(proj):
    with proj.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_items_project_status_deleted"))
    create_tables(proj)
    assert "ix_items_project_status_deleted" not in _index_names(proj, "items")
