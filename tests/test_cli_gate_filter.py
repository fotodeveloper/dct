"""`dct list --gate` (#947): items awaiting a gate checkpoint, sprint or not."""

import pytest
from conftest import run_cli

from dct.core.checkpoints import add_checkpoint
from dct.core.items import create_item


def test_list_gate_filters(cli_engine, monkeypatch, capsys):
    waiting = create_item(cli_engine, "proj-a", "feature", "Awaiting dogfood")
    add_checkpoint(cli_engine, waiting["id"], "Fresh-session check", kind="dogfood")
    create_item(cli_engine, "proj-a", "feature", "Nothing pending")

    run_cli(monkeypatch, ["list", "proj-a", "--gate", "any"])
    out = capsys.readouterr().out
    assert "Awaiting dogfood" in out and "Nothing pending" not in out

    run_cli(monkeypatch, ["list", "proj-a", "--gate", "review"])
    assert "Awaiting dogfood" not in capsys.readouterr().out


def test_list_gate_refuses_a_non_gate_kind(cli_engine, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["list", "proj-a", "--gate", "task"])
    assert exc.value.code == 1
    assert "gate" in capsys.readouterr().err
