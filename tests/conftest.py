"""Shared pytest fixtures + the two host-surface test harnesses.

`engine` / `seed_project` are the plain core-layer fixtures. `srv` (MCP) and
`cli_engine` + `run_cli` (CLI) are the harnesses every host-surface test file
used to copy verbatim; they live here so the monkeypatch target — the one
seam through which a test file reaches the in-memory DB — is stated once.
"""

import os
import re
import shutil
import subprocess
import sys

import pytest
import sqlalchemy as sa

from dct.db import create_tables, get_engine

# PG test lane (#426). Unset (the default) → in-memory SQLite, one fresh
# database per test. Set → the whole suite runs against that PostgreSQL URL,
# which MUST point at a THROWAWAY database: isolation is a per-test
# `DROP SCHEMA public CASCADE` + `CREATE SCHEMA public`, so every object in
# it is destroyed between tests.
PG_TEST_URL = os.environ.get("DCT_TEST_DATABASE_URL", "")

# --- Throwaway-target guard (K-31) -----------------------------------------
# The lane's isolation step is `DROP SCHEMA public CASCADE` on whatever
# DCT_TEST_DATABASE_URL names — one typo away from wiping a real database.
# The guard below runs ONCE per session, BEFORE any DDL, and refuses anything
# that does not look like a throwaway target.

# A throwaway database carries a `test` or `ci` SEGMENT in its name:
# dct_ci, dct_ci_local, foo_test pass; dct, dctest, citrus do not.
_THROWAWAY_NAME_RE = re.compile(r"(^|_)(test|ci)(_|$)")

# One test never leaves more than a handful of items behind; a target already
# holding more than this is somebody's real data, not lane leftovers.
MAX_LEFTOVER_ITEMS = 50

_LOCAL_HOST_ALIASES = {"", "localhost", "127.0.0.1", "::1"}


def _norm_host(url: sa.engine.URL) -> str:
    host = (url.host or "").lower()
    return "localhost" if host in _LOCAL_HOST_ALIASES else host


def _db_name(url: sa.engine.URL) -> str:
    """Last path segment of the URL's database component."""
    return (url.database or "").rstrip("/").rsplit("/", 1)[-1]


def throwaway_refusal_reason(
    url: str, live_url: str = "", item_count: int | None = None
) -> str | None:
    """Why `url` must NOT be used as the PG lane target — or None if it may.

    Pure predicate (no I/O), so it is unit-testable without a PostgreSQL
    server; `item_count` is the caller's probe of `items` (None = the table
    does not exist / could not be counted).

    Refuses when: the URL is unparseable or names no database; the database
    name carries no `test`/`ci` segment; the target is the configured LIVE
    dct database (same host+database as `live_url`); or `items` already holds
    more than MAX_LEFTOVER_ITEMS rows.
    """
    try:
        parsed = sa.engine.make_url(url)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
        return f"the URL could not be parsed ({exc})"

    name = _db_name(parsed)
    if not name:
        return "the URL names no database"
    if not _THROWAWAY_NAME_RE.search(name):
        return (
            f"database name {name!r} carries no '_test'/'_ci' segment "
            "(expected something like 'dct_ci' or 'dct_ci_local')"
        )

    if live_url:
        try:
            live = sa.engine.make_url(live_url)
        except Exception:  # noqa: BLE001 - an unparseable live URL clashes with nothing
            live = None
        if live is not None and _db_name(live) == name and _norm_host(live) == _norm_host(parsed):
            return (
                f"it resolves to the configured LIVE dct database "
                f"({_norm_host(parsed)}/{name})"
            )

    if item_count is not None and item_count > MAX_LEFTOVER_ITEMS:
        return (
            f"the target already holds {item_count} rows in `items` "
            f"(> {MAX_LEFTOVER_ITEMS}) — that is real data, not lane leftovers"
        )
    return None


def _live_database_url() -> str:
    """The configured live dct database URL, or '' when it cannot be read."""
    try:
        from dct.config import load_config

        return load_config().database_url or ""
    except Exception:  # noqa: BLE001 - a missing/broken config must not block the lane
        return ""


# Captured ONCE, at conftest import — structurally BEFORE `_suite_isolation`
# repoints DCT_DATABASE_URL / DCT_CONFIG_PATH at a throwaway file (#822).
# Read later, the guard would compare the lane target against the suite's own
# SQLite file and never recognise the developer's real database.
LIVE_DATABASE_URL = _live_database_url()


def _probe_item_count(url: str) -> int | None:
    """Rows in `items` on the target — None when absent or unreadable.

    Read-only: no DDL runs before the guard has passed.
    """
    eng = get_engine(url)
    try:
        with eng.connect() as conn:
            if not sa.inspect(conn).has_table("items"):
                return None
            return conn.execute(sa.text("SELECT count(*) FROM items")).scalar_one()
    except Exception:  # noqa: BLE001 - connection errors surface in the engine fixture
        return None
    finally:
        eng.dispose()


def pg_lane_refusal_reason(url: str, item_count: int | None) -> str | None:
    """`throwaway_refusal_reason` against the import-time LIVE identity."""
    return throwaway_refusal_reason(url, LIVE_DATABASE_URL, item_count)


@pytest.fixture(scope="session")
def pg_lane_target_guard():
    """Refuse a non-throwaway PG lane target ONCE, before any test runs DDL."""
    if not PG_TEST_URL:
        return
    reason = pg_lane_refusal_reason(PG_TEST_URL, _probe_item_count(PG_TEST_URL))
    if reason:
        pytest.exit(
            "DCT_TEST_DATABASE_URL must point at a THROWAWAY database "
            f"({reason}). Each test runs DROP SCHEMA public CASCADE on it.",
            returncode=3,
        )


# --- Suite isolation (#822) -------------------------------------------------
# Hook tests run shell hooks that call the PATH `dct`; with the developer's
# environment that binary reads their config and migrates their LIVE database
# with this tree's code. Every subprocess the suite starts must instead see a
# throwaway SQLite file + config and import THIS tree's `dct` package.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def _suite_isolation(tmp_path_factory):
    """Point DCT_* at a session tmp dir and put this tree first on PYTHONPATH.

    Tests that exercise config/URL resolution from files or env absence clear
    these locally with `monkeypatch.delenv(..., raising=False)`.
    """
    tmp = tmp_path_factory.mktemp("dct-suite")
    overrides = {
        "DCT_DATABASE_URL": f"sqlite:///{tmp / 'suite.db'}",
        "DCT_CONFIG_PATH": str(tmp / "dct.toml"),
        "DCT_DATA_DIR": str(tmp / "data"),
        "PYTHONPATH": os.pathsep.join(
            p for p in (REPO_ROOT, os.environ.get("PYTHONPATH", "")) if p
        ),
    }
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield tmp
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def engine(pg_lane_target_guard):
    """Engine with all tables created — in-memory SQLite, or the PG test lane.

    See PG_TEST_URL above for the PostgreSQL variant; the default path is
    unchanged (a private in-memory SQLite database per test). On the PG lane
    the session-scoped `pg_lane_target_guard` has already vetted the target.
    """
    if not PG_TEST_URL:
        eng = get_engine("sqlite://")
        create_tables(eng)
        yield eng
        return

    eng = get_engine(PG_TEST_URL)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
        create_tables(eng)
        yield eng
    finally:
        eng.dispose()


# Tests whose ASSERTION is SQLite-specific — they cannot pass on PostgreSQL for
# reasons that are not dct bugs. Keyed by node id suffix, each with the reason
# the PG lane reports; nothing is excluded silently.
PG_LANE_SKIPS = {
    "test_adversarial.py::TestTagsEdgeCases::test_unicode_tags":
        "asserts SQLite's BINARY collation order of tag names; PostgreSQL "
        "orders by the database collation (ICU/libc), so the expected "
        "sequence is dialect-defined, not a dct invariant",
    "test_cli_workflows.py::test_pickup_strips_control_chars_from_prompt_and_titles":
        "seeds a NUL (0x00) byte in text columns; PostgreSQL text can never "
        "hold NUL, so the payload cannot be written in the first place",
}


def pytest_collection_modifyitems(items):
    """On the PG lane (#426), skip the handful of SQLite-specific assertions."""
    if not PG_TEST_URL:
        return
    for item in items:
        for suffix, reason in PG_LANE_SKIPS.items():
            if item.nodeid.endswith(suffix):
                item.add_marker(pytest.mark.skip(reason=f"PG lane: {reason}"))


def decorated_tool_count() -> int:
    """K-94 B9: the expected MCP tool count, DERIVED from the source (one
    `@mcp.tool(` decorator per tool in dct/mcp/server.py) — independent of the
    registry the tests compare it with, and never a hard-coded constant."""
    with open(os.path.join(REPO_ROOT, "dct", "mcp", "server.py"), encoding="utf-8") as fh:
        return len(re.findall(r"^@mcp\.tool\(", fh.read(), flags=re.MULTILINE))


@pytest.fixture
def seed_project(engine):
    """Insert a test project and return its id."""
    from dct.core.projects import create_project

    return create_project(engine, slug="test", name="Test Project", path="/tmp/test")


@pytest.fixture
def srv(engine, monkeypatch):
    """MCP harness: point the server at the fixture engine, seed slug 'test'.

    Tools are called directly (the SDK's @mcp.tool() decorator returns the
    function unchanged) — `server._get_engine` is the only seam that needs
    patching, so the returned module IS the system under test.
    """
    from dct.core.projects import create_project
    from dct.mcp import server as S

    monkeypatch.setattr(S, "_get_engine", lambda: engine)
    create_project(engine, slug="test", name="Test Project", path="/tmp/test")
    return S


@pytest.fixture
def cli_engine(engine, monkeypatch):
    """CLI harness: point `cli._get_engine` at the fixture engine, seed slug
    'proj-a'. Returns the engine so tests verify through the CORE layer rather
    than through the CLI's own output. The read-only seam (K-84,
    `cli._get_readonly_engine`) points at the same engine; the read-only
    path itself is tested through a real subprocess in test_readonly_engine."""
    from dct import cli
    from dct.core.projects import create_project

    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    monkeypatch.setattr(cli, "_get_readonly_engine", lambda: engine)
    return engine


def run_cli(monkeypatch, argv):
    """Drive the CLI the way a shell would — `cli.main()` on a patched argv —
    so parser wiring and dispatch are exercised end to end. Plain helper, not
    a fixture: it needs the caller's own `monkeypatch`."""
    from dct import cli

    monkeypatch.setattr(sys, "argv", ["dct", *argv])
    cli.main()


def git(repo, *args: str) -> str:
    """Run git in `repo` with a hermetic identity (no global signing / hooks)
    and return stdout. For tests that build throwaway repositories."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        env=env, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_identity(monkeypatch, tmp_path_factory):
    """Commits the release effects layer creates (dct.core.gitinfo.git_write)
    carry a test identity and see no user / system git config (signing,
    hooks) — set through the writer's passthrough variables, so the tests
    do not depend on the machine's ~/.gitconfig (CI has none)."""
    cfg = tmp_path_factory.mktemp("gitcfg") / "config"
    cfg.write_text("")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@t")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git_commit_proving(repo, entry_id: int, path: str = "proof.txt") -> str:
    """Commit a change carrying the `Changelog: <id>` trailer; return its SHA."""
    target = os.path.join(str(repo), path)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(f"change for #{entry_id}\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "-m", f"feat: change {entry_id}\n\nChangelog: {entry_id}")
    return git(repo, "rev-parse", "HEAD")


def approved_plan_digest(engine, slug: str, cwd, **kw) -> str:
    """The snapshot_digest of `dct release plan` for the selection an apply
    call is about to use (K-80: apply requires the APPROVED digest) — the
    tests' stand-in for the owner reading and approving the plan. A plan
    without a snapshot (a resume plan) or one that cannot be built yields a
    digest that matches nothing (apply then refuses for its own reason)."""
    from dct.core.gitinfo import CheckoutVerificationError
    from dct.workflows.release import build_plan

    selection = {k: kw[k] for k in ("version", "bump_type", "entry_ids", "no_entries") if k in kw}
    if selection.get("entry_ids") is not None:
        selection["entry_ids"] = list(selection["entry_ids"])
    try:
        digest = build_plan(engine, slug, cwd=str(cwd), **selection)["snapshot_digest"]
    except (ValueError, CheckoutVerificationError):
        digest = None
    return digest or "0" * 64


def approved_proposal_digest(engine, slug: str, cwd, *, plan_digest: str, **kw) -> str | None:
    """The proposal_sha256 of `dct release prepare` for the same version,
    selection and approved plan digest (K-76: apply consumes only the
    APPROVED proposal) — the tests' stand-in for the owner reviewing the
    prepared file set. A prepare that is refused (nothing to prepare: the
    version is already recorded or lower than the current one, the plan
    digest is stale, …) yields None — apply then decides on its own (a
    no-op re-run, or a refusal of a new stamp without a proposal)."""
    from dct.workflows.release.effects import ReleaseRefused, prepare_release

    selection = {k: kw[k] for k in ("version", "bump_type", "entry_ids", "no_entries") if k in kw}
    if selection.get("entry_ids") is not None:
        selection["entry_ids"] = list(selection["entry_ids"])
    try:
        out = prepare_release(engine, slug, cwd=str(cwd), plan_digest=plan_digest, **selection)
    except ReleaseRefused:
        return None
    return out["details"]["proposal_sha256"]


def alias_proposal(engine, slug: str, cwd, **selection) -> list[str]:
    """`["--proposal-digest", P]` for the `clog release` alias (K-117: it
    needs the APPROVED proposal and never prepares one itself) — the tests'
    stand-in for the owner running `dct release prepare` for exactly the
    plan the alias derives (the same version / bump and selection; no
    selection = every unreleased entry) and approving its digest. A prepare
    that is refused (unproven entries, nothing to prepare, a failing sync
    …) yields [] — the alias then decides on its own (a refusal of a new
    release without a proposal, or a no-op re-run)."""
    from dct.workflows.release.effects import ReleaseRefused, prepare_release

    selection = {k: selection[k] for k in ("version", "bump_type", "entry_ids", "no_entries")
                 if k in selection}
    if selection.get("entry_ids") is not None:
        selection["entry_ids"] = list(selection["entry_ids"])
    try:
        out = prepare_release(engine, slug, cwd=None if cwd is None else str(cwd), strict=True,
                              **selection)
    except ReleaseRefused:
        return []
    return ["--proposal-digest", out["details"]["proposal_sha256"]]


_ALIAS_VALUE_FLAGS = ("-P", "--project", "--entries", "--push-remote", "--push-branch",
                      "--push-url", "--proposal-digest")


def alias_argv(engine, slug: str, argv: list[str], cwd=None) -> list[str]:
    """The argv of a `dct clog release …` call plus the approved proposal
    for exactly that call (alias_proposal; the selection is read from the
    argv, the checkout is `cwd` or the process cwd — chdir first). An argv
    whose --entries value does not parse is returned unchanged (the CLI
    refuses it)."""
    selection: dict = {}
    rest = argv[argv.index("release") + 1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in _ALIAS_VALUE_FLAGS:
            if token == "--entries":
                try:
                    selection["entry_ids"] = [int(part) for part in rest[index + 1].split(",")]
                except ValueError:
                    return list(argv)
            index += 2
            continue
        if token in ("--patch", "--minor", "--major"):
            selection["bump_type"] = token[2:]
        elif token == "--no-entries":
            selection["no_entries"] = True
        elif not token.startswith("-"):
            selection["version"] = token
        index += 1
    return [*argv, *alias_proposal(engine, slug, cwd, **selection)]


def tag_proof_for(commit: str, name: str = "v1.1.0", *, tag_object: str | None = None) -> dict:
    """A tag proof as a ledger-path caller reports it (K-115 G4,
    core.releases.validate_tag_proof): a lightweight tag `name` at `commit`,
    or an annotated one when `tag_object` is given."""
    return {"name": name, "object": tag_object or commit, "commit": commit,
            "annotated": tag_object is not None}


def push_proof_for(commit: str, tag: str = "v1.1.0", remote: str = "origin") -> dict:
    """A push proof as a ledger-path caller reports it (K-115 G4)."""
    return {"target": {"remote": remote, "tag": tag}, "tag_commit": commit}


def ledger_proofs(status: str, commit: str, version: str = "1.1.0") -> dict:
    """The advance_release / reconstruct_release kwargs a record WITH git
    needs to be at `status` (K-94 B1 + K-115 G4): the release commit from
    'committed' on, the tag proof from 'tagged' on, the push proof for
    'pushed'. {} below 'committed'."""
    flow = ("stamped", "files_written", "committed", "tagged", "pushed", "completed")
    rank = flow.index(status) if status in flow else -1
    out: dict = {}
    if rank >= flow.index("committed"):
        out["release_commit_sha"] = commit
    if rank >= flow.index("tagged"):
        out["tag_proof"] = tag_proof_for(commit, f"v{version}")
    if status == "pushed":
        out["push_proof"] = push_proof_for(commit, f"v{version}")
    return out


def make_git_repo(path, branch: str = "main"):
    """A git repo at `path` on `branch` with one commit; returns the realpath."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    (path / "README").write_text("x\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return os.path.realpath(path)


def require_tools(*tools: str) -> None:
    """The ONE missing-tool guard for tests that drive shell hooks (K-45, K-65).

    Locally a missing tool (jq, git, bash) SKIPS the calling test; under CI
    (GitHub Actions sets CI=true) it FAILS instead, so a runner without jq can
    never turn the hook suites silently green. Call it at the top of a test,
    or use the `hook_tools` fixture / a module-level autouse fixture.
    """
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if not missing:
        return
    if os.environ.get("CI"):
        pytest.fail(
            f"CI is set but {missing} missing — the hook tests that need them "
            f"would otherwise be skipped silently", pytrace=False)
    pytest.skip(f"{' and '.join(missing)} required by the hooks")


@pytest.fixture
def hook_tools():
    """bash + jq for a test that runs a dct hook (see require_tools)."""
    require_tools("bash", "jq")


# --- Lock contention (K-64 / K-91) ------------------------------------------
# ONE mechanism for every race test of the release family: the first writer
# holds the project write lock, the probe PROVES the second writer is blocked
# on it (never a sleep, never "the future is not done yet"), then the first
# releases and the test checks the outcome.


@pytest.fixture
def concurrent_engine(engine, tmp_path):
    """The guarded PG lane, or a file SQLite database with real connections
    (an in-memory database cannot be shared by two connections)."""
    if engine.dialect.name == "postgresql":
        yield engine
        return
    eng = get_engine(f"sqlite:///{tmp_path / 'race.db'}")
    create_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


class LockProbe:
    """Positive, sleep-free proof that a competitor transaction is BLOCKED on
    the project write lock while the holder still holds it (K-64, K-91).

    The competitor runs through `run()` in a worker thread; the probe watches
    that thread's statements on the engine:
    - PostgreSQL: project_transaction polls `pg_try_advisory_lock`; a SECOND
      attempt from the competitor exists only because the first one was
      refused — the lock was held when it tried.
    - SQLite: right before the competitor's `BEGIN IMMEDIATE` a separate
      connection with a zero busy timeout tries to take the same write lock;
      "database is locked" proves the holder still holds it at that moment,
      so the competitor's statement can only return after the release.
    `blocked` is set on that proof; `free` records a probe that found the
    lock FREE (the holder lost it — a failure, never retried away). The
    bounded wait (`wait_until_blocked`) exposes a missing synchronisation
    instead of hanging.
    """

    def __init__(self, engine):
        import threading

        self.engine = engine
        self.blocked = threading.Event()
        self.free = threading.Event()
        self.thread_id: int | None = None
        self.attempts = 0
        sa.event.listen(engine, "before_cursor_execute", self._observe)

    def close(self):
        sa.event.remove(self.engine, "before_cursor_execute", self._observe)

    def run(self, fn, *args, **kwargs):
        import threading

        self.thread_id = threading.get_ident()
        return fn(*args, **kwargs)

    def capture(self, fn, *args, **kwargs):
        """run() returning the exception instead of raising it — the
        competitor's outcome (a controlled refusal) is the assertion."""
        try:
            return self.run(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            return exc

    def _observe(self, conn, cursor, statement, parameters, context, executemany):
        import threading

        if threading.get_ident() != self.thread_id:
            return
        if "pg_try_advisory_lock" in statement:
            self.attempts += 1
            if self.attempts >= 2:
                self.blocked.set()
        elif statement.strip().upper() == "BEGIN IMMEDIATE":
            self._probe_sqlite_write_lock()

    def _probe_sqlite_write_lock(self):
        import sqlite3

        probe = sqlite3.connect(self.engine.url.database, timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                self.blocked.set()
                return
            raise
        else:
            probe.execute("ROLLBACK")
            self.free.set()
        finally:
            probe.close()

    def wait_until_blocked(self, timeout: float = 15) -> None:
        assert self.blocked.wait(timeout), (
            "the competitor never reached the held lock"
            + (" — the probe found it FREE" if self.free.is_set() else ""))
        assert not self.free.is_set()


@pytest.fixture
def lock_probe(concurrent_engine):
    probe = LockProbe(concurrent_engine)
    try:
        yield probe
    finally:
        probe.close()
