"""CLI plan writes use explicit ULID aliases plus a mandatory project guard."""
import json

import pytest
from conftest import run_cli

from dct.core.plans.crud import add_plan_section, create_plan, get_plan_section


def records(engine, project="proj-a"):
    plan = create_plan(engine, project, slug="spec", title="Specification", kind="spec")
    return plan, add_plan_section(engine, plan["id"], "Unclear heading", 2, section_type="uncertain")


def test_ulid_setter_returns_identity_and_idempotent_ack(cli_engine, monkeypatch, capsys):
    _, section = records(cli_engine)
    argv = ["plan", "set-section-type", section["ulid"], "prose", "--project", "proj-a", "--json"]
    run_cli(monkeypatch, argv)
    ack = json.loads(capsys.readouterr().out)
    assert (ack["id"], ack["ulid"], ack["classification_source"], ack["changed"]) == (section["id"], section["ulid"], "manual", True)
    run_cli(monkeypatch, argv)
    assert json.loads(capsys.readouterr().out)["changed"] is False
    assert get_plan_section(cli_engine, section["id"])["heading"] == "Unclear heading"


@pytest.mark.parametrize("alias", ("1", "#1", "not-ulid"))
def test_cli_rejects_integer_or_invalid_alias_without_classification_write(cli_engine, monkeypatch, alias):
    _, section = records(cli_engine)
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["plan", "set-section-type", alias, "prose", "--project", "proj-a"])
    assert exc.value.code != 0
    assert get_plan_section(cli_engine, section["id"])["section_type"] == "uncertain"


def test_missing_project_guard_refuses_before_engine_access(monkeypatch):
    from dct import cli
    monkeypatch.setattr(cli, "_get_engine", lambda: pytest.fail("missing guard opened DB"))
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["plan", "set-section-type", "0" * 26, "prose"])
    assert exc.value.code == 2


def test_cross_project_ulid_refuses_without_change(cli_engine, monkeypatch):
    from dct.core.projects import create_project
    create_project(cli_engine, "other", "Other", "/tmp/other")
    _, section = records(cli_engine)
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["plan", "set-section-type", section["ulid"], "prose", "--project", "other"])
    assert exc.value.code == 1
    assert get_plan_section(cli_engine, section["id"])["section_type"] == "uncertain"


def test_batch_cli_resolves_only_selected_plan_and_returns_actual_remaining(cli_engine, monkeypatch, capsys):
    from dct.config import ClassificationConfig
    from dct.core.plans import classification
    plan, section = records(cli_engine)
    monkeypatch.setattr(classification, "load_config", lambda: type("Config", (), {"classification": ClassificationConfig(provider="disabled")})())
    run_cli(monkeypatch, ["plan", "resolve-uncertain", plan["ulid"], "--project", "proj-a", "--json"])
    ack = json.loads(capsys.readouterr().out)
    assert ack["plan_id"] == plan["id"] and ack["plan_ulid"] == plan["ulid"]
    assert ack["remaining_uncertain"] == 1
    assert ack["unresolved"] == [{"id": section["id"], "reason": "disabled"}]
