"""PG test lane (#426) safety net — guard predicate + dialect sentinel.

The lane isolates tests with `DROP SCHEMA public CASCADE` on whatever
DCT_TEST_DATABASE_URL names, so `throwaway_refusal_reason` (K-31) decides
whether that URL may be touched at all. It is a pure function: everything
below runs on the default SQLite lane too, WITHOUT a PostgreSQL server.

`test_lane_really_runs_on_postgresql` (K-32) is the other half — it catches
the silent failure where the env var stops reaching conftest and the
"PostgreSQL" job quietly re-runs the SQLite suite.
"""

import os

import pytest
from conftest import MAX_LEFTOVER_ITEMS, throwaway_refusal_reason

LIVE = "postgresql+psycopg://dct@localhost:5432/dct"


class TestThrowawayNameRule:
    """Rule 1: the database name must carry a `test`/`ci` SEGMENT."""

    @pytest.mark.parametrize(
        "name",
        ["dct_ci", "dct_ci_local", "foo_test", "test", "ci", "test_dct", "ci_dct_2"],
    )
    def test_accepts_throwaway_names(self, name):
        assert throwaway_refusal_reason(f"postgresql+psycopg://u:p@h:5432/{name}") is None

    @pytest.mark.parametrize(
        "name",
        ["dct", "dctest", "citrus", "production", "dct-ci", "dctci", "dct_cid"],
    )
    def test_refuses_non_throwaway_names(self, name):
        reason = throwaway_refusal_reason(f"postgresql+psycopg://u:p@h:5432/{name}")
        assert reason is not None
        assert "_test" in reason and "_ci" in reason

    def test_refuses_url_without_database(self):
        assert "names no database" in (
            throwaway_refusal_reason("postgresql+psycopg://u:p@h:5432/") or ""
        )

    def test_refuses_unparseable_url(self):
        assert "could not be parsed" in (throwaway_refusal_reason("::not a url::") or "")

    def test_refuses_the_live_default_database(self):
        """The exact URL this repo's own live DB uses must never be accepted."""
        assert throwaway_refusal_reason(LIVE) is not None


class TestLiveDatabaseRule:
    """Rule 2: never the configured live database, even under a lane-ish name."""

    def test_refuses_when_host_and_name_match_live(self):
        url = "postgresql+psycopg://u:p@localhost:5432/dct_ci"
        reason = throwaway_refusal_reason(url, live_url=url)
        assert reason is not None
        assert "LIVE" in reason

    def test_host_aliases_count_as_the_same_host(self):
        reason = throwaway_refusal_reason(
            "postgresql+psycopg://u:p@127.0.0.1:5432/dct_ci",
            live_url="postgresql+psycopg://u:p@localhost:5432/dct_ci",
        )
        assert reason is not None and "LIVE" in reason

    def test_same_name_on_another_host_is_fine(self):
        assert (
            throwaway_refusal_reason(
                "postgresql+psycopg://u:p@db.example.com:5432/dct_ci",
                live_url="postgresql+psycopg://u:p@localhost:5432/dct_ci",
            )
            is None
        )

    def test_sqlite_live_url_clashes_with_nothing(self):
        assert (
            throwaway_refusal_reason(
                "postgresql+psycopg://u:p@localhost:5432/dct_ci",
                live_url="sqlite:////srv/u/.local/share/dctools/dct.db",
            )
            is None
        )

    def test_unparseable_live_url_does_not_block_a_valid_target(self):
        assert (
            throwaway_refusal_reason(
                "postgresql+psycopg://u:p@h:5432/dct_ci", live_url="::garbage::"
            )
            is None
        )


class TestRowCountRule:
    """Rule 3: a populated `items` table means this is somebody's real data."""

    URL = "postgresql+psycopg://u:p@h:5432/dct_ci"

    @pytest.mark.parametrize("count", [None, 0, 1, MAX_LEFTOVER_ITEMS])
    def test_accepts_empty_or_leftover_sized_targets(self, count):
        assert throwaway_refusal_reason(self.URL, item_count=count) is None

    @pytest.mark.parametrize("count", [MAX_LEFTOVER_ITEMS + 1, 10_000])
    def test_refuses_a_populated_target(self, count):
        reason = throwaway_refusal_reason(self.URL, item_count=count)
        assert reason is not None
        assert str(count) in reason

    def test_name_rule_wins_over_the_row_count(self):
        """A bad name is refused even when the target is empty."""
        reason = throwaway_refusal_reason(LIVE, item_count=0)
        assert reason is not None and "segment" in reason


def test_refusal_reason_is_pure(monkeypatch):
    """No environment read, no connection: the same inputs, the same answer."""
    monkeypatch.setenv("DCT_TEST_DATABASE_URL", "postgresql://x@y/z")
    monkeypatch.setenv("DCT_DATABASE_URL", "postgresql://x@y/z")
    url = "postgresql+psycopg://u:p@h:5432/dct_ci"
    assert throwaway_refusal_reason(url) is None
    assert throwaway_refusal_reason(url) is None


@pytest.mark.skipif(
    os.environ.get("DCT_EXPECT_PG") != "1",
    reason="sentinel for the PostgreSQL lane only; set DCT_EXPECT_PG=1 to arm it",
)
def test_lane_really_runs_on_postgresql(engine):
    """K-32: with DCT_EXPECT_PG=1 the suite MUST be on a real PG server.

    Without this, a DCT_TEST_DATABASE_URL that stops reaching conftest turns
    the PostgreSQL CI job into a second, silently green SQLite run.
    """
    assert engine.dialect.name == "postgresql", (
        "DCT_EXPECT_PG=1 but the engine fixture handed out "
        f"{engine.dialect.name!r} — DCT_TEST_DATABASE_URL did not reach conftest"
    )
