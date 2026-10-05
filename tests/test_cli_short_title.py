"""CLI short_title surfaces (#817) — `add --short-title/-S`, `update
--short-title` (omit = unchanged, "" = clear), `show`, and the `list` Label
column. Same harness as test_cli_tags.py; state is verified through core."""

import pytest
from conftest import run_cli

from dct.core.items import create_item, get_item


def _only_item_id(engine):
    from dct.core.items import list_items

    rows = list_items(engine, project_slug="proj-a")
    assert len(rows) == 1
    return rows[0]["id"]


@pytest.mark.parametrize("flag", ["--short-title", "-S"])
def test_add_short_title_both_spellings(cli_engine, monkeypatch, capsys, flag):
    run_cli(monkeypatch, ["add", "proj-a", "todo", "A long title", flag, "Hook paths"])
    assert get_item(cli_engine, _only_item_id(cli_engine))["short_title"] == "Hook paths"


def test_add_without_short_title_stores_null(cli_engine, monkeypatch):
    run_cli(monkeypatch, ["add", "proj-a", "todo", "Plain"])
    assert get_item(cli_engine, _only_item_id(cli_engine))["short_title"] is None


def test_add_too_long_short_title_exits_nonzero_and_writes_nothing(cli_engine, monkeypatch, capsys):
    from dct.core.items import list_items

    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["add", "proj-a", "todo", "T", "-S", "x" * 51])
    assert exc.value.code == 1
    assert "short_title" in capsys.readouterr().err
    assert list_items(cli_engine, project_slug="proj-a") == []


def test_update_short_title_omit_keeps_empty_clears(cli_engine, monkeypatch):
    item = create_item(cli_engine, "proj-a", "todo", "T", short_title="Old")
    iid = str(item["id"])

    run_cli(monkeypatch, ["update", iid, "--short-title", "New"])
    assert get_item(cli_engine, item["id"])["short_title"] == "New"

    run_cli(monkeypatch, ["update", iid, "--priority", "P1"])  # flag omitted
    assert get_item(cli_engine, item["id"])["short_title"] == "New"

    run_cli(monkeypatch, ["update", iid, "--short-title", ""])  # explicit clear
    assert get_item(cli_engine, item["id"])["short_title"] is None


def test_show_prints_short_title_only_when_set(cli_engine, monkeypatch, capsys):
    labelled = create_item(cli_engine, "proj-a", "todo", "T1", short_title="Shown label")
    bare = create_item(cli_engine, "proj-a", "todo", "T2")

    run_cli(monkeypatch, ["show", str(labelled["id"])])
    assert "Short title: Shown label" in capsys.readouterr().out

    run_cli(monkeypatch, ["show", str(bare["id"])])
    assert "Short title:" not in capsys.readouterr().out


def test_list_has_label_column_blank_when_unset(cli_engine, monkeypatch, capsys):
    create_item(cli_engine, "proj-a", "todo", "Labelled title", short_title="My label")
    create_item(cli_engine, "proj-a", "todo", "Bare title")

    run_cli(monkeypatch, ["list", "proj-a"])
    lines = capsys.readouterr().out.splitlines()
    header = lines[0].split()
    assert header == ["#", "Project", "Type", "Pri", "Status", "Label", "Title"]
    label_col = lines[0].index("Label")
    title_col = lines[0].index("Title")
    by_title = {ln[title_col:].strip(): ln[label_col:title_col].strip() for ln in lines[2:]}
    assert by_title == {"Labelled title": "My label", "Bare title": ""}
