"""CLI `dct sprint` subcommand group — create/list/show/update + verb aliases.

The CLI resolves its engine through `cli._get_engine()`; tests monkeypatch it to
the in-memory fixture engine so no real DB is touched. Commands are driven the
same way a shell would — through `cli.main()` with a patched argv — so the
parser wiring and dispatch are exercised end to end.
"""

import sys

import pytest

from dct import cli
from dct.core.projects import create_project
from dct.core.sprints import create_sprint, get_sprint, list_sprints


@pytest.fixture
def cli_engine(engine, monkeypatch):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
    monkeypatch.setattr(cli, "_get_engine", lambda: engine)
    return engine


def run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["dct", *argv])
    cli.main()


# ── parser wiring ─────────────────────────────────────────────────────────────

def test_sprint_subparser_wired():
    parser = cli.build_parser()
    args = parser.parse_args(["sprint", "update", "7", "--status", "deferred"])
    assert args.command == "sprint"
    assert args.sprint_command == "update"
    assert args.id == 7
    assert args.status == "deferred"


def test_sprint_verb_alias_sets_forced_status():
    parser = cli.build_parser()
    args = parser.parse_args(["sprint", "defer", "7"])
    assert args.sprint_command == "defer"
    assert args._forced_status == "deferred"


# ── create / show / list ──────────────────────────────────────────────────────

def test_sprint_create_and_show(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["sprint", "create", "First", "--project", "proj-a", "--code", "s1"])
    out = capsys.readouterr().out
    assert "Sprint #" in out
    assert "First" in out

    rows = list_sprints(cli_engine, project_slug="proj-a")
    assert len(rows) == 1
    sid = rows[0]["id"]

    run_cli(monkeypatch, ["sprint", "show", str(sid)])
    out = capsys.readouterr().out
    assert "First" in out
    assert "s1" in out


def test_sprint_create_status_help_lists_all_legal_statuses():
    """The --status help must document every status create_sprint accepts
    (planned/active/completed/abandoned) and flag deferred as invalid — not
    understate it as only planned/active (regression guard)."""
    parser = cli.build_parser()
    # Drill into the `sprint create` subparser's own help.
    top = next(a for a in parser._subparsers._group_actions if a.dest == "command")
    sprint_parser = top.choices["sprint"]
    sprint_sub = next(
        a for a in sprint_parser._subparsers._group_actions
        if getattr(a, "dest", None) == "sprint_command"
    )
    create_help = sprint_sub.choices["create"].format_help()
    for legal in ("planned", "active", "completed", "abandoned"):
        assert legal in create_help
    assert "deferred" in create_help  # flagged as NOT valid


def test_sprint_create_terminal_status_via_cli(cli_engine, monkeypatch, capsys):
    """completed/abandoned really are legal creation statuses, matching help."""
    run_cli(monkeypatch, ["sprint", "create", "Retro", "--project", "proj-a",
                          "--status", "completed"])
    out = capsys.readouterr().out
    assert "completed" in out
    rows = list_sprints(cli_engine, project_slug="proj-a", status="completed")
    assert [r["name"] for r in rows] == ["Retro"]


def test_sprint_list_shows_all(cli_engine, monkeypatch, capsys):
    create_sprint(cli_engine, "proj-a", "Planned A")
    create_sprint(cli_engine, "proj-a", "Active B", status="active")
    run_cli(monkeypatch, ["sprint", "list", "--project", "proj-a"])
    out = capsys.readouterr().out
    assert "Planned A" in out
    assert "Active B" in out


def test_sprint_list_status_filter(cli_engine, monkeypatch, capsys):
    s = create_sprint(cli_engine, "proj-a", "Parked", status="active")
    run_cli(monkeypatch, ["sprint", "update", str(s["id"]), "--status", "deferred"])
    capsys.readouterr()
    run_cli(monkeypatch, ["sprint", "list", "--project", "proj-a", "--status", "deferred"])
    out = capsys.readouterr().out
    assert "Parked" in out


# ── update + verb aliases ─────────────────────────────────────────────────────

def test_sprint_update_status(cli_engine, monkeypatch, capsys):
    s = create_sprint(cli_engine, "proj-a", "S", status="active")
    run_cli(monkeypatch, ["sprint", "update", str(s["id"]), "--status", "deferred"])
    out = capsys.readouterr().out
    assert "deferred" in out
    assert get_sprint(cli_engine, s["id"])["status"] == "deferred"


def test_sprint_defer_alias(cli_engine, monkeypatch, capsys):
    s = create_sprint(cli_engine, "proj-a", "S", status="active")
    run_cli(monkeypatch, ["sprint", "defer", str(s["id"])])
    assert get_sprint(cli_engine, s["id"])["status"] == "deferred"


def test_sprint_activate_alias(cli_engine, monkeypatch, capsys):
    s = create_sprint(cli_engine, "proj-a", "S")  # planned
    run_cli(monkeypatch, ["sprint", "activate", str(s["id"])])
    got = get_sprint(cli_engine, s["id"])
    assert got["status"] == "active"
    assert got["started_at"] is not None


def test_sprint_complete_alias(cli_engine, monkeypatch, capsys):
    s = create_sprint(cli_engine, "proj-a", "S", status="active")
    run_cli(monkeypatch, ["sprint", "complete", str(s["id"])])
    got = get_sprint(cli_engine, s["id"])
    assert got["status"] == "completed"
    assert got["ended_at"] is not None


# ── error paths exit non-zero ─────────────────────────────────────────────────

def test_sprint_illegal_transition_exits_nonzero(cli_engine, monkeypatch, capsys):
    s = create_sprint(cli_engine, "proj-a", "S")  # planned
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["sprint", "update", str(s["id"]), "--status", "deferred"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "Illegal sprint status transition" in err


def test_sprint_show_missing_exits_nonzero(cli_engine, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["sprint", "show", "9999"])
    assert exc.value.code != 0
