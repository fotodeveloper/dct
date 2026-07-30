"""Shared pytest fixtures + the two host-surface test harnesses.

`engine` / `seed_project` are the plain core-layer fixtures. `srv` (MCP) and
`cli_engine` + `run_cli` (CLI) are the harnesses every host-surface test file
used to copy verbatim; they live here so the monkeypatch target — the one
seam through which a test file reaches the in-memory DB — is stated once.
"""

import sys

import pytest

from dct.db import create_tables, get_engine


@pytest.fixture
def engine():
    """In-memory SQLite engine with all tables created."""
    eng = get_engine("sqlite://")
    create_tables(eng)
    return eng


@pytest.fixture
def seed_project(engine):
    """Insert a test project and return its id."""
    from dct.core.projects import create_project

    return create_project(engine, slug="test", name="Test Project", path="/tmp/test")


@pytest.fixture
def srv(engine, monkeypatch):
    """MCP harness: point the server at the fixture engine, seed slug 'test'.

    Tools are called directly (FastMCP's @mcp.tool() leaves the function
    callable) — `server._get_engine` is the only seam that needs patching, so
    the returned module IS the system under test.
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
    than through the CLI's own output."""
    from dct import cli
    from dct.core.projects import create_project

    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    return engine


def run_cli(monkeypatch, argv):
    """Drive the CLI the way a shell would — `cli.main()` on a patched argv —
    so parser wiring and dispatch are exercised end to end. Plain helper, not
    a fixture: it needs the caller's own `monkeypatch`."""
    from dct import cli

    monkeypatch.setattr(sys, "argv", ["dct", *argv])
    cli.main()
