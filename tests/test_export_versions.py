"""ONE semantic version selector for export and list (K-55).

`1.1`, `1.1.0` and `v1.1` are the same version under the contract
(same_version), even though normalize_version keeps `1.1` and `1.1.0`
apart. The export must therefore find EVERY entry of an equal version and
render ONE block headed by the authoritative spelling — the release
record's version when it exists, else (a legacy release without a record)
the stored spelling of its first-stamped entry; nothing is invented and no
stored row is rewritten.

Runs on SQLite and on the PG lane.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import run_cli

from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import get_release
from dct.db import changelog_entries
from dct.export import export_changelog_md


def _stamp_raw(engine, eid, version, when):
    """A legacy stamp written before the contract (no record, any spelling)."""
    with engine.begin() as conn:
        conn.execute(changelog_entries.update().where(changelog_entries.c.id == eid)
                     .values(version=version, released_at=when))


def _headers(md: str) -> list[str]:
    return [line for line in md.splitlines() if line.startswith("## [")]


@pytest.fixture
def legacy(engine):
    create_project(engine, slug="proj-a", name="Project A", current_version="1.1.0")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    first = add_entry(engine, "proj-a", "added", "first, stored as 1.1")["id"]
    second = add_entry(engine, "proj-a", "fixed", "second, stored as 1.1.0")["id"]
    _stamp_raw(engine, first, "1.1", t0)
    _stamp_raw(engine, second, "1.1.0", t0 + timedelta(hours=1))
    return engine, first, second


@pytest.mark.parametrize("requested", ["1.1", "1.1.0", "v1.1", " V1.1.0 "])
def test_single_version_export_finds_every_legacy_spelling(legacy, requested):
    engine, _, _ = legacy
    md = export_changelog_md(engine, "proj-a", version=requested)
    assert _headers(md) == ["## [1.1] - 2026-01-01"]
    assert "- first, stored as 1.1" in md and "- second, stored as 1.1.0" in md


def test_full_export_renders_one_block_for_equal_versions(legacy):
    engine, _, _ = legacy
    md = export_changelog_md(engine, "proj-a")
    assert _headers(md) == ["## [1.1] - 2026-01-01"]


def test_legacy_rows_keep_their_stored_spelling(legacy):
    engine, first, second = legacy
    export_changelog_md(engine, "proj-a", version="1.1.0")
    by_id = {e["id"]: e["version"] for e in list_entries(engine, "proj-a")}
    assert by_id == {first: "1.1", second: "1.1.0"}
    assert get_release(engine, "proj-a", "1.1") is None   # nothing invented


@pytest.mark.parametrize("requested", ["1.1", "1.1.0", "v1.1.0"])
def test_list_entries_version_filter_is_semantic(legacy, requested):
    engine, first, second = legacy
    assert sorted(e["id"] for e in list_entries(engine, "proj-a", version=requested)) == [
        first, second]


def test_unknown_or_different_version_renders_nothing(legacy):
    engine, _, _ = legacy
    assert export_changelog_md(engine, "proj-a", version="1.1.1") == ""
    assert export_changelog_md(engine, "proj-a", version="latest") == ""


def test_unparseable_legacy_version_matches_only_itself(engine):
    create_project(engine, slug="proj-a", name="Project A")
    eid = add_entry(engine, "proj-a", "added", "odd")["id"]
    _stamp_raw(engine, eid, "2026-rc", datetime(2026, 2, 1, tzinfo=UTC))
    assert _headers(export_changelog_md(engine, "proj-a", version="2026-rc")) == [
        "## [2026-rc] - 2026-02-01"]
    assert export_changelog_md(engine, "proj-a", version="2026") == ""


# ── the full v1.6.0 flow: core + CLI + export ────────────────────────────────


def test_v_prefixed_release_flow_core_cli_export(cli_engine, monkeypatch, capsys):
    engine = cli_engine
    create_project(engine, slug="proj-v", name="V", current_version="1.5.0")
    a = add_entry(engine, "proj-v", "added", "shipped in 1.6")["id"]
    # A legacy row of the SAME version in yet another spelling (pre-contract).
    legacy = add_entry(engine, "proj-v", "fixed", "legacy v-spelled row")["id"]

    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "v1.6.0", "--entries", str(a)])
    out = capsys.readouterr().out
    assert "as 1.6.0" in out and "current version: 1.6.0" in out
    _stamp_raw(engine, legacy, "v1.6", datetime.now(UTC))
    record = get_release(engine, "proj-v", "v1.6.0")
    assert record["version"] == "1.6.0"
    assert get_project(engine, "proj-v")["current_version"] == "1.6.0"

    # Re-running under another spelling is the same release (no-op, resume).
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "1.6", "--entries", str(a)])
    out = capsys.readouterr().out
    assert "already released" in out and "version-sync skipped" not in out

    for requested in ("v1.6.0", "1.6", "1.6.0"):
        md = export_changelog_md(engine, "proj-v", version=requested)
        assert [h.split(" - ")[0] for h in _headers(md)] == ["## [1.6.0]"]
        assert "- shipped in 1.6" in md and "- legacy v-spelled row" in md
    assert [h.split(" - ")[0] for h in _headers(export_changelog_md(engine, "proj-v"))] == [
        "## [1.6.0]"]
    with engine.begin() as conn:
        stored = conn.execute(sa.select(changelog_entries.c.version)
                              .where(changelog_entries.c.id == legacy)).scalar()
    assert stored == "v1.6"   # history is never rewritten


def test_core_release_report_keeps_requested_and_current_apart(engine):
    create_project(engine, slug="proj-v", name="V", current_version="1.5.0")
    add_entry(engine, "proj-v", "added", "x")
    out = release_entries(engine, "proj-v", "v1.6.0")
    assert (out["version"], out["current_version"], out["release"]["version"]) == (
        "1.6.0", "1.6.0", "1.6.0")
    again = release_entries(engine, "proj-v", "1.6")
    assert again["release"]["id"] == out["release"]["id"]
    assert again["version"] == "1.6" and again["current_version"] == "1.6.0"
