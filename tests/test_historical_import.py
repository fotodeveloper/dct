"""History import preserves old rows without advancing/stamping a release."""
import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import run_cli

from dct.core.changelog import add_entry, list_entries
from dct.core.projects import get_project, set_version
from dct.db import releases


@pytest.mark.parametrize("raw, expected", [("2020-01-02", "2020-01-02T00:00:00+00:00"), ("2020-01-02T03:04:05+02:00", "2020-01-02T01:04:05+00:00"), ("2020-01-02T01:04:05Z", "2020-01-02T01:04:05+00:00")])
def test_mcp_stated_date_preserves_version_and_unreleased_state(srv, engine, raw, expected):
    set_version(engine, "test", "2.0.0")
    waiting = add_entry(engine, "test", "fixed", "Waiting change")
    ack = srv.import_released_changelog("test", "fixed", "Old fix", "v1.0", released_at=raw)
    assert ack["version"] == "v1.0" and ack["released_at"] == expected and ack["released_at_source"] == "stated"
    rows = list_entries(engine, project_slug="test")
    assert {r["id"]: r["version"] for r in rows}[waiting["id"]] is None
    saved = next(r for r in rows if r["id"] == ack["id"])
    assert saved["branch"] is None and saved["worktree"] is None and saved["branch_source"] is None
    assert get_project(engine, "test")["current_version"] == "2.0.0"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(releases)).scalar() == 0


@pytest.mark.parametrize("raw", ("nonsense", "2020-02-30", "2020-01-02T03:04:05", "2020-01-02T03:04:05+99:99"))
def test_malformed_history_timestamp_refuses_before_engine_or_row_write(srv, monkeypatch, raw):
    monkeypatch.setattr(srv, "_get_engine", lambda: pytest.fail("invalid date accessed DB"))
    assert "error" in srv.import_released_changelog("test", "fixed", "Old", "1.0", released_at=raw)


def test_cli_default_date_is_import_time_and_partial_success_is_retained(cli_engine, monkeypatch, capsys):
    set_version(cli_engine, "proj-a", "2.0.0")
    before = datetime.now(UTC)
    run_cli(monkeypatch, ["clog", "import-released", "proj-a", "fixed", "Old", "--version", "v1.0", "--json"])
    ack = json.loads(capsys.readouterr().out)
    assert ack["released_at_source"] == "import_time"
    assert datetime.fromisoformat(ack["released_at"]) >= before
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, ["clog", "import-released", "proj-a", "fixed", "Future", "--version", "3.0", "--json"])
    assert [row["id"] for row in list_entries(cli_engine, project_slug="proj-a")] == [ack["id"]]
    assert get_project(cli_engine, "proj-a")["current_version"] == "2.0.0"


def test_recorded_version_import_refuses_without_extending_exact_release_set(srv, engine):
    """A historical writer cannot append a row to an existing release record."""
    from dct.core.changelog import release_entries
    release_entries(engine, "test", "1.0.0", entry_ids=[])
    ack = srv.import_released_changelog("test", "fixed", "Extra history", "v1.0")
    assert "error" in ack and "release record" in ack["error"]
    assert list_entries(engine, project_slug="test") == []


def test_history_invalid_related_item_refuses_only_that_row(srv, engine):
    """Related history must belong to a live item in the imported project."""
    from dct.core.items import create_item
    from dct.core.projects import create_project
    create_project(engine, "other", "Other", "/tmp/other")
    foreign = create_item(engine, "other", "todo", "Foreign item")
    saved = srv.import_released_changelog("test", "fixed", "Earlier", "1.0")
    refused = srv.import_released_changelog("test", "fixed", "Foreign", "1.0", related_item_id=foreign["id"])
    assert "error" in refused
    assert [row["id"] for row in list_entries(engine, project_slug="test")] == [saved["id"]]
