"""CLI tag surfaces — `dct tags list/add/merge`, `list --tag`, `update --tags`.

Same harness as the other host-surface files: `cli._get_engine` is pointed at
the in-memory fixture engine and commands are driven through `cli.main()` on a
patched argv, so parser wiring and dispatch are exercised end to end.
Verification goes through the CORE layer wherever the assertion is about state
rather than about what the CLI prints.
"""

import pytest
from conftest import run_cli

from dct import cli
from dct.core.items import create_item, get_item, list_items
from dct.core.tags import add_tag, list_tags

# ── parser wiring ─────────────────────────────────────────────────────────────


def test_tags_subparser_wired():
    parser = cli.build_parser()
    args = parser.parse_args(["tags", "merge", "tests", "test"])
    assert args.command == "tags"
    assert args.tags_command == "merge"
    assert args.from_name == "tests"
    assert args.into_name == "test"


def test_update_tags_help_documents_the_clear_form():
    """`--tags ""` clearing everything is non-obvious — it must be in help."""
    parser = cli.build_parser()
    top = next(a for a in parser._subparsers._group_actions if a.dest == "command")
    help_text = top.choices["update"].format_help()
    assert "FULL REPLACE" in help_text
    assert "clears" in help_text


# ── tags add / list ───────────────────────────────────────────────────────────


def test_tags_add_and_list(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["tags", "add", "Perf", "--description", "speed work"])
    out = capsys.readouterr().out
    assert "created" in out
    assert "perf" in out  # normalized on the way in

    run_cli(monkeypatch, ["tags", "list"])
    out = capsys.readouterr().out
    assert "speed work" in out
    assert "Items" in out
    # The unused tag is listed with an explicit ZERO in the Items column —
    # parsed out of the row the way the sibling count test does, so a stray
    # "0" anywhere else in the table cannot satisfy the assertion.
    perf_row = next(line.split() for line in out.splitlines() if line.startswith("1 "))
    assert perf_row[1] == "perf"  # normalized on the way in
    assert perf_row[2] == "0"


def test_tags_add_is_idempotent(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["tags", "add", "perf"])
    capsys.readouterr()
    run_cli(monkeypatch, ["tags", "add", " PERF "])
    out = capsys.readouterr().out
    assert "exists" in out
    assert [t["name"] for t in list_tags(cli_engine)] == ["perf"]


def test_tags_add_similarity_refusal_exits_1_with_force_hint(
    cli_engine, monkeypatch, capsys
):
    add_tag(cli_engine, "tests")
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["tags", "add", "test"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "similar" in err
    assert "force=true" in err
    assert [t["name"] for t in list_tags(cli_engine)] == ["tests"]


def test_tags_add_force_overrides_the_guard(cli_engine, monkeypatch, capsys):
    add_tag(cli_engine, "tests")
    run_cli(monkeypatch, ["tags", "add", "test", "--force"])
    assert capsys.readouterr().out.startswith("✓")
    assert sorted(t["name"] for t in list_tags(cli_engine)) == ["test", "tests"]


def test_tags_list_counts_items(cli_engine, monkeypatch, capsys):
    create_item(cli_engine, "proj-a", "todo", "One", tags=["perf"])
    create_item(cli_engine, "proj-a", "todo", "Two", tags=["perf"])
    run_cli(monkeypatch, ["tags", "list"])
    out = capsys.readouterr().out
    perf_line = next(line for line in out.splitlines() if line.startswith("1 "))
    assert "perf" in perf_line
    assert "2" in perf_line.split()


def test_tags_list_empty_dictionary(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["tags", "list"])
    assert "(no results)" in capsys.readouterr().out


# ── tags merge ────────────────────────────────────────────────────────────────


def test_tags_merge_moves_items_and_reports_count(cli_engine, monkeypatch, capsys):
    a = create_item(cli_engine, "proj-a", "todo", "One", tags=["tests"])
    b = create_item(cli_engine, "proj-a", "todo", "Two", tags=["tests"])
    add_tag(cli_engine, "test", force=True)

    run_cli(monkeypatch, ["tags", "merge", "tests", "test"])
    out = capsys.readouterr().out
    assert "2 item(s) moved" in out
    assert get_item(cli_engine, a["id"])["tags"] == ["test"]
    assert get_item(cli_engine, b["id"])["tags"] == ["test"]
    assert [t["name"] for t in list_tags(cli_engine)] == ["test"]


def test_tags_merge_unknown_tag_exits_1(cli_engine, monkeypatch, capsys):
    add_tag(cli_engine, "perf")
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["tags", "merge", "nonsense", "perf"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nonsense" in err
    assert "perf" in err  # the suggestion lists what does exist


def test_tags_merge_into_itself_exits_1(cli_engine, monkeypatch, capsys):
    add_tag(cli_engine, "perf")
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["tags", "merge", "perf", "PERF"])
    assert exc.value.code == 1
    assert "itself" in capsys.readouterr().err


# ── list --tag ────────────────────────────────────────────────────────────────


def test_list_tag_filters(cli_engine, monkeypatch, capsys):
    create_item(cli_engine, "proj-a", "todo", "Tagged one", tags=["perf"])
    create_item(cli_engine, "proj-a", "todo", "Untagged one")

    run_cli(monkeypatch, ["list", "proj-a", "--tag", "perf"])
    out = capsys.readouterr().out
    assert "Tagged one" in out
    assert "Untagged one" not in out


def test_list_tag_is_normalized(cli_engine, monkeypatch, capsys):
    create_item(cli_engine, "proj-a", "todo", "Tagged one", tags=["perf"])
    run_cli(monkeypatch, ["list", "proj-a", "--tag", " PERF "])
    assert "Tagged one" in capsys.readouterr().out


def test_list_unknown_tag_exits_1_with_suggestion(cli_engine, monkeypatch, capsys):
    add_tag(cli_engine, "perf")
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["list", "proj-a", "--tag", "nonsense"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nonsense" in err
    assert "perf" in err


def test_list_without_tag_flag_is_unfiltered(cli_engine, monkeypatch, capsys):
    create_item(cli_engine, "proj-a", "todo", "Tagged one", tags=["perf"])
    create_item(cli_engine, "proj-a", "todo", "Untagged one")
    run_cli(monkeypatch, ["list", "proj-a"])
    out = capsys.readouterr().out
    assert "Tagged one" in out
    assert "Untagged one" in out


# ── add --tags / update --tags ────────────────────────────────────────────────


def test_add_tags_csv_still_works(cli_engine, monkeypatch, capsys):
    run_cli(monkeypatch, ["add", "proj-a", "todo", "With tags", "--tags", " perf , db "])
    capsys.readouterr()
    item = list_items(cli_engine, project_slug="proj-a")[0]
    assert get_item(cli_engine, item["id"])["tags"] == ["db", "perf"]


def test_update_tags_replaces(cli_engine, monkeypatch, capsys):
    item = create_item(cli_engine, "proj-a", "todo", "One", tags=["perf", "db"])
    run_cli(monkeypatch, ["update", str(item["id"]), "--tags", "perf,ui"])
    assert capsys.readouterr().out.startswith("✓")
    assert get_item(cli_engine, item["id"])["tags"] == ["perf", "ui"]


def test_update_empty_tags_clears(cli_engine, monkeypatch, capsys):
    item = create_item(cli_engine, "proj-a", "todo", "One", tags=["perf"])
    run_cli(monkeypatch, ["update", str(item["id"]), "--tags", ""])
    capsys.readouterr()
    assert get_item(cli_engine, item["id"])["tags"] == []


def test_update_without_tags_flag_leaves_tags_alone(cli_engine, monkeypatch, capsys):
    item = create_item(cli_engine, "proj-a", "todo", "One", tags=["perf"])
    run_cli(monkeypatch, ["update", str(item["id"]), "--priority", "P1"])
    capsys.readouterr()
    fresh = get_item(cli_engine, item["id"])
    assert fresh["tags"] == ["perf"]
    assert fresh["priority"] == "P1"


def test_update_tags_similarity_refusal_exits_1(cli_engine, monkeypatch, capsys):
    add_tag(cli_engine, "tests")
    item = create_item(cli_engine, "proj-a", "todo", "One", tags=["tests"])
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["update", str(item["id"]), "--tags", "test"])
    assert exc.value.code == 1
    assert "similar" in capsys.readouterr().err
    # Refused writes leave the item's tags untouched.
    assert get_item(cli_engine, item["id"])["tags"] == ["tests"]


def test_update_has_no_short_tags_alias(cli_engine, monkeypatch, capsys):
    """K-25: `-t` means --type on `list` and --tags on `add`; on `update` the
    collision was a footgun, so the short alias is gone. `update <id> -t issue`
    must FAIL loudly, never silently tag the item 'issue'."""
    item = create_item(cli_engine, "proj-a", "todo", "One", tags=["perf"])
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["update", str(item["id"]), "-t", "issue"])
    assert exc.value.code == 2  # argparse: unrecognized arguments
    assert "-t" in capsys.readouterr().err
    assert get_item(cli_engine, item["id"])["tags"] == ["perf"]


def test_update_tags_help_does_not_offer_the_short_alias():
    parser = cli.build_parser()
    top = next(a for a in parser._subparsers._group_actions if a.dest == "command")
    help_text = top.choices["update"].format_help()
    assert "--tags" in help_text
    assert "-t," not in help_text
    assert "-t " not in help_text


def test_show_prints_tags(cli_engine, monkeypatch, capsys):
    item = create_item(cli_engine, "proj-a", "todo", "One", tags=["db", "perf"])
    run_cli(monkeypatch, ["show", str(item["id"])])
    assert "Tags:        db, perf" in capsys.readouterr().out


# ── task mirror (machine caller — force_tags) ─────────────────────────────────


def test_task_mirror_survives_near_duplicate_tags(cli_engine, monkeypatch, capsys):
    """The hook worker is a MACHINE caller: pre-existing near-duplicates of its
    own tag names ('cc-task-lists', 'autos') must not make create_item raise —
    that would turn the advisory similarity guard into a live crash path."""
    import argparse
    import io

    from dct.config import ClaudeIntegrationConfig, Config, IntegrationsConfig

    # Names the similarity guard would flag for BOTH tags the mirror writes.
    add_tag(cli_engine, "cc-task-lists")
    add_tag(cli_engine, "autos")

    monkeypatch.setattr(
        "dct.config.load_config",
        lambda: Config(integrations=IntegrationsConfig(
            claude=ClaudeIntegrationConfig(task_mirror="auto"))),
    )
    payload = (
        '{"tool_name":"TodoWrite","session_id":"s1",'
        '"tool_input":{"todos":[{"content":"alpha"}]}}'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    cli.cmd_internal_task_mirror(argparse.Namespace(session_id="s1", project="proj-a"))
    assert "mirror ok" in capsys.readouterr().out

    mirrored = list_items(cli_engine, project_slug="proj-a", search="CC task list")
    assert len(mirrored) == 1
    assert mirrored[0]["tags"] == ["auto", "cc-task-list"]
