"""ONE version contract on every write path (K-54, K-58).

Every path that WRITES a version — release_entries (explicit or bumped),
projects.set_version / `dct project set-version`, create_project /
`dct project add` (explicit or auto-detected) — normalizes it under
dct.core.version, refuses an unsupported format explicitly (no silent
substitute, no invented prerelease semantics) and checks it against the
width of EVERY version column it could land in (projects.current_version,
changelog_entries.version, releases.version — limits read from the schema)
before anything is written.

release_entries additionally refuses to stamp while the STORED
current_version does not parse — after a real historical no-op has been
handled — instead of silently disabling the never-rewind guard.

Runs on SQLite and on the PG lane.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import run_cli

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project, get_project, set_version
from dct.core.version import ensure_version_fits
from dct.db import changelog_entries, projects, releases


def _snapshot(engine):
    with engine.begin() as conn:
        return tuple(
            [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.id))]
            for t in (changelog_entries, projects, releases)
        )


def _force_current(engine, slug, value):
    """Simulate a legacy stored value written before the contract existed."""
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == slug)
                     .values(current_version=value))


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", current_version="1.0.0")
    return engine


# ── release_entries: an unparseable stored current_version ───────────────────


def test_rc_current_version_blocks_a_lower_release_and_writes_nothing(proj):
    """2.0.0-rc1 → 1.0.0: the never-rewind guard must not switch off just
    because the stored version does not parse."""
    _force_current(proj, "proj-a", "2.0.0-rc1")
    add_entry(proj, "proj-a", "added", "a")
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="2.0.0-rc1.*not a supported version"):
        release_entries(proj, "proj-a", "1.0.0")
    assert _snapshot(proj) == before


@pytest.mark.parametrize("kwargs", [
    {"version": "3.0.0"}, {"version": "3.0.0", "entry_ids": []},
    {"bump_type": "patch"}, {"bump_type": "major"},
])
def test_unparseable_current_version_refuses_every_new_stamp(proj, kwargs):
    _force_current(proj, "proj-a", "2.0.0-rc1")
    eid = add_entry(proj, "proj-a", "added", "a")["id"]
    if "entry_ids" in kwargs:
        kwargs = {**kwargs, "entry_ids": [eid]}
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="not a supported version"):
        release_entries(proj, "proj-a", **kwargs)
    assert _snapshot(proj) == before


def test_historical_noop_is_still_a_noop_with_an_unparseable_current(proj):
    """The real historical no-op is resolved BEFORE the parse refusal."""
    add_entry(proj, "proj-a", "added", "a")
    release_entries(proj, "proj-a", "1.1.0")
    with proj.begin() as conn:
        conn.execute(releases.delete())        # an unrecorded (legacy) release
    _force_current(proj, "proj-a", "legacy-2")
    before = _snapshot(proj)
    out = release_entries(proj, "proj-a", "1.1.0")
    assert out["released"] == 0 and out["release"] is None
    assert out["current_version"] == "legacy-2"
    assert _snapshot(proj) == before


# ── column width: limits come from the schema ────────────────────────────────


def test_version_fit_reads_every_version_column_from_the_schema(monkeypatch):
    widths = {projects.c.current_version.type.length,
              changelog_entries.c.version.type.length,
              releases.c.version.type.length}
    narrowest = min(widths)
    ensure_version_fits("1." + "0" * (narrowest - 2))
    with pytest.raises(ValueError, match="version too long"):
        ensure_version_fits("1." + "0" * (narrowest - 1))
    # Narrowing ONE column moves the guard — nothing is typed in.
    monkeypatch.setattr(releases.c.version.type, "length", 5)
    with pytest.raises(ValueError, match="version too long"):
        ensure_version_fits("1.0.10")


# ── set_version / project add / detection ────────────────────────────────────


def test_set_version_stores_the_normalized_form(proj):
    assert set_version(proj, "proj-a", " v1.6.0 ")["current_version"] == "1.6.0"


@pytest.mark.parametrize("bad", ["latest", "2.0.0-rc1", "1.0." + "9" * 47, "", "v"])
def test_set_version_refuses_unsupported_or_too_long_and_writes_nothing(proj, bad):
    before = _snapshot(proj)
    with pytest.raises(ValueError, match="Unsupported version|too long"):
        set_version(proj, "proj-a", bad)
    assert _snapshot(proj) == before


def test_set_version_of_an_unknown_project_is_none(engine):
    assert set_version(engine, "nope", "1.0") is None


def test_create_project_normalizes_an_explicit_version(engine):
    assert create_project(engine, "proj-b", "B", current_version="V2.3")["current_version"] == "2.3"


@pytest.mark.parametrize("bad", ["2.0.0-rc1", "1.0." + "9" * 47])
def test_create_project_refuses_an_unsupported_explicit_version(engine, bad):
    with pytest.raises(ValueError, match="Unsupported version|too long"):
        create_project(engine, "proj-b", "B", current_version=bad)
    assert get_project(engine, "proj-b") is None


def test_undelete_normalizes_too(engine):
    from dct.core.projects import delete_project

    create_project(engine, "proj-b", "B", current_version="1.0")
    delete_project(engine, "proj-b")
    assert create_project(engine, "proj-b", "B", current_version="v1.1")["current_version"] == "1.1"


def test_detected_version_is_normalized(engine, tmp_path):
    (tmp_path / "VERSION").write_text("v1.2.3\n")
    p = create_project(engine, "proj-b", "B", path=str(tmp_path))
    assert p["current_version"] == "1.2.3" and p["_detected_source"] == "VERSION"


@pytest.mark.parametrize("raw", ["1.0.0-beta.2", "1.0." + "9" * 47])
def test_unsupported_detected_version_is_reported_never_substituted(engine, tmp_path, raw):
    (tmp_path / "VERSION").write_text(raw + "\n")
    p = create_project(engine, "proj-b", "B", path=str(tmp_path))
    assert p["current_version"] is None
    assert get_project(engine, "proj-b")["current_version"] is None
    assert "_detected_source" not in p
    unsupported = p["_detected_unsupported"]
    assert unsupported["version"] == raw and unsupported["source"] == "VERSION"
    assert "Unsupported version" in unsupported["reason"] or "too long" in unsupported["reason"]


# ── CLI surfaces ─────────────────────────────────────────────────────────────


def _exit_code(monkeypatch, argv) -> int:
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, argv)
    return exc.value.code


def test_cli_set_version_reports_the_stored_form(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["project", "set-version", "proj-a", "v1.6.0"])
    assert "proj-a → 1.6.0" in capsys.readouterr().out
    assert get_project(cli_engine, "proj-a")["current_version"] == "1.6.0"


def test_cli_set_version_refuses_an_unsupported_version(cli_engine, monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["project", "set-version", "proj-a", "2.0.0-rc1"]) == 1
    assert "Unsupported version" in capsys.readouterr().err
    assert get_project(cli_engine, "proj-a")["current_version"] is None


def test_cli_project_add_warns_about_an_unsupported_detected_version(
        cli_engine, monkeypatch, tmp_path, capsys):
    (tmp_path / "VERSION").write_text("1.0.0-beta.2\n")
    run_cli(monkeypatch, ["project", "add", "proj-b", str(tmp_path)])
    err = capsys.readouterr().err
    assert "1.0.0-beta.2" in err and "VERSION" in err and "not stored" in err
    assert get_project(cli_engine, "proj-b")["current_version"] is None


def test_cli_project_add_refuses_an_unsupported_explicit_version(
        cli_engine, monkeypatch, tmp_path, capsys):
    assert _exit_code(monkeypatch, ["project", "add", "proj-b", str(tmp_path),
                                    "--version", "nightly"]) == 1
    assert "Unsupported version" in capsys.readouterr().err
    assert get_project(cli_engine, "proj-b") is None
