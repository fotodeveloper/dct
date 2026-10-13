# dct — Testing

How the test suite isolates itself and how to run it against PostgreSQL. ←
back to the [README](../README.md).

## The default suite

```bash
.venv/bin/pytest -q          # in-memory SQLite, no services, no env vars
```

The `engine` fixture (`tests/conftest.py`) hands every test a private
in-memory SQLite database with all tables created.

Suite isolation (#822): the session-scoped, autouse `_suite_isolation` fixture
points `DCT_DATABASE_URL`, `DCT_CONFIG_PATH` and `DCT_DATA_DIR` at a throwaway
SQLite file, config and data dir in a session tmp dir, and puts this tree first
on `PYTHONPATH`. That reaches the suite AND every subprocess it starts: hook
tests run shell hooks that call the `dct` on `PATH`, which would otherwise read
the developer's config and migrate their live database with this tree's code.
Tests that exercise config or URL resolution clear these variables locally with
`monkeypatch.delenv(..., raising=False)`.

## The PostgreSQL lane (#426)

PostgreSQL is the primary backend; the lane runs the SAME suite against a real
server. Setting `DCT_TEST_DATABASE_URL` switches the `engine` fixture from
in-memory SQLite to that URL.

**Only ever point it at a throwaway test database holding no data anyone
needs.** Not being live or production is not enough, and neither is a `_test` /
`_ci` name. The lane's per-test isolation is `DROP SCHEMA public CASCADE`
followed by `CREATE SCHEMA public` on the database the URL names, so every
object in that database is destroyed before each test. The guard below is a
backstop, not permission: a database that passes it is not thereby yours to
destroy.

### The throwaway guard

The session-scoped `pg_lane_target_guard` fixture runs ONCE, before any test
runs DDL, and stops the whole run (`pytest.exit`, return code 3) unless the
target looks like a throwaway database. It refuses when:

- the URL cannot be parsed or names no database;
- the database name carries no `test` / `ci` SEGMENT — `_`-separated, so
  `dct_ci`, `dct_ci_local`, `foo_test` pass while `dct`, `dctest`, `citrus`,
  `dct-ci` are refused;
- the target resolves to the configured LIVE dct database (same host and
  database name; `localhost`, `127.0.0.1`, `::1` and an empty host count as one
  host). The live URL is captured once at conftest import, before
  `_suite_isolation` repoints `DCT_DATABASE_URL`;
- `items` already holds more than 50 rows (`MAX_LEFTOVER_ITEMS`) — that is real
  data, not lane leftovers. The probe is read-only.

The predicate is `tests/conftest.py::throwaway_refusal_reason` (pure, no I/O),
unit-tested in `tests/test_pg_lane.py`.

### Running it

With docker:

```bash
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=dct \
  -e POSTGRES_USER=dct -e POSTGRES_DB=dct_ci --name dct-pg postgres:16
DCT_TEST_DATABASE_URL=postgresql+psycopg://dct:dct@localhost:5433/dct_ci \
  DCT_EXPECT_PG=1 .venv/bin/pytest -q -rs   # ~3.5 min vs ~16 s on SQLite
```

With a local server instead of docker: `createdb dct_ci_local` and point the URL
at it.

`DCT_EXPECT_PG=1` arms the sentinel `test_lane_really_runs_on_postgresql` in
`tests/test_pg_lane.py`: it fails when the `engine` fixture hands out anything
but PostgreSQL, so a `DCT_TEST_DATABASE_URL` that silently stops reaching
conftest cannot turn the lane into a second, green SQLite run. It is set in the
`tests-postgres` job of `.github/workflows/ci.yml` (PostgreSQL 16 service,
database `dct_ci`, Python floor 3.11 only).

### Skipped on the lane

A handful of assertions are SQLite-specific for reasons that are not dct bugs
(collation order, a NUL byte PostgreSQL text cannot hold). They are listed with
the reason for each in `PG_LANE_SKIPS` in `tests/conftest.py`; the lane reports
every skip with that reason (`-rs`), nothing is excluded silently.
