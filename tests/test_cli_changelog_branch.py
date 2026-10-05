"""CLI changelog surfaces for #801 / #359: `clog add --branch/--worktree`
(+ detection in the caller's cwd), `clog list|count` filters, `clog release
--entries|--no-entries` (+ the version-sync script of the CURRENT checkout),
`clog update|delete`. State is verified through core."""

import shutil
from pathlib import Path

import pytest
from conftest import git, make_git_repo, run_cli

from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import create_project, get_project

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _rows(engine, slug="proj-a"):
    return {r["id"]: r for r in list_entries(engine, slug)}


def _exit_code(monkeypatch, argv) -> int:
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, argv)
    return exc.value.code


# ── clog add ──────────────────────────────────────────────────────────────────


def test_add_explicit_branch_and_worktree(cli_engine, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    run_cli(monkeypatch, ["clog", "add", "added", "x", "-P", "proj-a",
                          "--branch", "feat/x", "--worktree", "/tmp/proj-a"])
    [row] = _rows(cli_engine).values()
    assert (row["branch"], row["worktree"], row["branch_source"]) == (
        "feat/x", "/tmp/proj-a", "client")


def test_add_over_long_branch_exits_nonzero_writes_nothing(cli_engine, monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["clog", "add", "added", "x", "-P", "proj-a",
                                    "--branch", "b" * 256]) == 1
    assert "branch too long" in capsys.readouterr().err
    assert _rows(cli_engine) == {}


@needs_git
def test_add_detects_callers_checkout_as_client(cli_engine, monkeypatch, tmp_path):
    root = make_git_repo(tmp_path / "repo", branch="feat/cli")
    create_project(cli_engine, slug="proj-g", name="G", path=root)
    monkeypatch.chdir(root)
    run_cli(monkeypatch, ["clog", "add", "fixed", "y"])  # project auto-detected
    [row] = _rows(cli_engine, "proj-g").values()
    assert (row["branch"], row["worktree"], row["branch_source"]) == (
        "feat/cli", root, "client")


# ── clog list / count ─────────────────────────────────────────────────────────


@pytest.fixture
def mixed(cli_engine):
    for text, branch in (("x", "feat/x"), ("y", "feat/y"), ("n", None)):
        add_entry(cli_engine, "proj-a", "added", text, branch=branch)
    return cli_engine


@pytest.mark.parametrize("extra,expected", [
    ([], "3"),
    (["--include-unbranched"], "3"),
    (["--branch", "feat/x"], "1"),
    (["--branch", "feat/x", "--include-unbranched"], "2"),
    (["--branch", "feat/none"], "0"),
    (["--worktree", "/tmp/nowhere"], "0"),
])
def test_count_is_a_bare_integer_in_every_mode(mixed, monkeypatch, capsys, extra, expected):
    run_cli(monkeypatch, ["clog", "count", "-P", "proj-a", *extra])
    assert capsys.readouterr().out == expected + "\n"


def test_list_branch_filter_and_branch_column(mixed, monkeypatch, capsys):
    run_cli(monkeypatch, ["clog", "list", "-P", "proj-a", "--branch", "feat/x",
                          "--include-unbranched"])
    out = capsys.readouterr().out
    assert "Branch" in out and "feat/x" in out
    assert "feat/y" not in out


# ── clog release ──────────────────────────────────────────────────────────────


@pytest.fixture
def versioned(cli_engine):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    ids = [add_entry(cli_engine, "proj-v", "added", f"e{i}")["id"] for i in range(3)]
    return cli_engine, ids


def test_release_entries_stamps_only_the_selection(versioned, monkeypatch, capsys):
    engine, ids = versioned
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "--patch",
                          "--entries", f"{ids[0]}, {ids[2]}"])
    out = capsys.readouterr().out
    assert "1.0.0 → 1.0.1 (patch)" in out
    assert "Released 2 entries as 1.0.1 (mode: selected)" in out
    versions = {i: r["version"] for i, r in _rows(engine, "proj-v").items()}
    assert versions == {ids[0]: "1.0.1", ids[1]: None, ids[2]: "1.0.1"}


def test_release_no_entries_is_version_only_even_with_nothing_unreleased(
        cli_engine, monkeypatch, capsys):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "--minor", "--no-entries"])
    assert "mode: version_only" in capsys.readouterr().out
    assert get_project(cli_engine, "proj-v")["current_version"] == "1.1.0"


def test_release_all_guard_still_refuses_an_empty_release(cli_engine, monkeypatch, capsys):
    create_project(cli_engine, slug="proj-v", name="V", current_version="1.0.0")
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-v", "--patch"]) == 1
    assert "No unreleased entries" in capsys.readouterr().err
    assert get_project(cli_engine, "proj-v")["current_version"] == "1.0.0"


@pytest.mark.parametrize("raw", ["", " , ", "1,x"])
def test_release_bad_entries_flag_is_an_error_never_all(versioned, monkeypatch, capsys, raw):
    engine, _ = versioned
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-v", "2.0.0",
                                    "--entries", raw]) == 1
    assert "--entries" in capsys.readouterr().err
    assert all(r["version"] is None for r in _rows(engine, "proj-v").values())


def test_release_entries_and_no_entries_are_mutually_exclusive(versioned, monkeypatch):
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-v", "2.0.0",
                                    "--entries", "1", "--no-entries"]) == 2


def test_release_unknown_entry_errors_and_writes_nothing(versioned, monkeypatch, capsys):
    engine, _ = versioned
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-v", "2.0.0",
                                    "--entries", "424242"]) == 1
    assert "Unknown changelog entry" in capsys.readouterr().err
    assert get_project(engine, "proj-v")["current_version"] == "1.0.0"


def test_release_rewind_refused(versioned, monkeypatch, capsys):
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-v", "0.5.0"]) == 1
    assert "lower than the current version" in capsys.readouterr().err


def _sync_script(checkout: Path) -> None:
    """A fake version-sync script recording the version in ITS checkout."""
    scripts = checkout / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "dct-version-sync.sh").write_text(
        '#!/bin/bash\necho "$1" > "$(dirname "$0")/../synced-version"\n')


@needs_git
def test_release_from_a_linked_worktree_runs_that_checkouts_sync_script(
        cli_engine, monkeypatch, tmp_path):
    main = make_git_repo(tmp_path / "repo")
    linked = tmp_path / "repo" / ".worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat/wt", str(linked))
    _sync_script(Path(main))
    _sync_script(linked)
    create_project(cli_engine, slug="proj-g", name="G", path=main, current_version="1.0.0")
    add_entry(cli_engine, "proj-g", "added", "e")

    monkeypatch.chdir(linked)
    run_cli(monkeypatch, ["clog", "release", "--patch"])
    assert (linked / "synced-version").read_text().strip() == "1.0.1"
    assert not (Path(main) / "synced-version").exists()


@needs_git
def test_release_outside_the_project_checkout_is_refused_before_stamping(
        cli_engine, monkeypatch, tmp_path, capsys):
    """K-34/K-31: a mutating release never silently switches to the main
    checkout when the caller is not in a checkout of the project's repo."""
    main = make_git_repo(tmp_path / "repo")
    _sync_script(Path(main))
    other = make_git_repo(tmp_path / "other")
    _sync_script(Path(other))
    create_project(cli_engine, slug="proj-g", name="G", path=main, current_version="1.0.0")
    add_entry(cli_engine, "proj-g", "added", "e")

    monkeypatch.chdir(other)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"]) == 1
    assert "not a checkout of project 'proj-g'" in capsys.readouterr().err
    assert get_project(cli_engine, "proj-g")["current_version"] == "1.0.0"
    assert all(r["version"] is None for r in _rows(cli_engine, "proj-g").values())
    assert not (Path(main) / "synced-version").exists()
    assert not (Path(other) / "synced-version").exists()


def _relative_sync_script(root: Path) -> None:
    """A sync script that writes a RELATIVE file — it lands in the script's cwd."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "dct-version-sync.sh").write_text('#!/bin/bash\necho "$1" > VERSION\n')


@pytest.fixture
def gitproj(cli_engine, tmp_path):
    main = make_git_repo(tmp_path / "repo")
    _relative_sync_script(Path(main))
    create_project(cli_engine, slug="proj-g", name="G", path=main, current_version="1.0.0")
    return cli_engine, Path(main)


@needs_git
def test_release_sync_script_runs_from_the_project_root_not_the_subdirectory(
        gitproj, monkeypatch):
    engine, main = gitproj
    add_entry(engine, "proj-g", "added", "e")
    sub = main / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"])
    assert (main / "VERSION").read_text().strip() == "1.1.0"
    assert not (sub / "VERSION").exists()


@needs_git
def test_release_historical_retry_leaves_version_files_untouched(
        gitproj, monkeypatch, capsys):
    engine, main = gitproj
    monkeypatch.chdir(main)
    add_entry(engine, "proj-g", "added", "a")
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"])
    add_entry(engine, "proj-g", "added", "b")
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.2.0"])
    assert (main / "VERSION").read_text().strip() == "1.2.0"
    capsys.readouterr()

    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"])  # exits 0
    out = capsys.readouterr().out
    assert "1.1.0 already released" in out
    assert "current version: 1.2.0" in out
    assert "version-sync skipped" in out
    assert (main / "VERSION").read_text().strip() == "1.2.0"
    assert get_project(engine, "proj-g")["current_version"] == "1.2.0"


@needs_git
def test_release_resume_of_the_current_version_reruns_the_sync(gitproj, monkeypatch, capsys):
    """A crash after the DB stamp: re-running the CURRENT version with nothing
    left to stamp (released=0) must still finish the version-sync step."""
    engine, main = gitproj
    add_entry(engine, "proj-g", "added", "a")
    release_entries(engine, "proj-g", "1.1.0")  # DB done, files never synced
    monkeypatch.chdir(main)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"])
    out = capsys.readouterr().out
    assert "1.1.0 already released" in out
    assert "Running scripts/dct-version-sync.sh" in out
    assert (main / "VERSION").read_text().strip() == "1.1.0"


@needs_git
def test_release_version_only_runs_the_sync(gitproj, monkeypatch, capsys):
    _, main = gitproj
    monkeypatch.chdir(main)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-g", "--minor", "--no-entries"])
    out = capsys.readouterr().out
    assert "Released 0 entries as 1.1.0 (mode: version_only)" in out
    assert (main / "VERSION").read_text().strip() == "1.1.0"


@needs_git
def test_release_failing_sync_script_exits_nonzero(cli_engine, monkeypatch, tmp_path, capsys):
    main = Path(make_git_repo(tmp_path / "repo"))
    (main / "scripts").mkdir()
    (main / "scripts" / "dct-version-sync.sh").write_text("#!/bin/bash\nexit 3\n")
    create_project(cli_engine, slug="proj-g", name="G", path=str(main), current_version="1.0.0")
    add_entry(cli_engine, "proj-g", "added", "a")
    monkeypatch.chdir(main)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"]) == 3
    err = capsys.readouterr().err
    assert "version-sync script failed (exit 3)" in err
    assert "re-run" in err


@needs_git
@pytest.mark.parametrize("kind", ["nested", "submodule"])
def test_release_from_a_nested_repo_or_submodule_is_refused(
        gitproj, monkeypatch, tmp_path, capsys, kind):
    engine, main = gitproj
    add_entry(engine, "proj-g", "added", "a")
    if kind == "nested":
        inner = Path(make_git_repo(main / "vendor" / "lib"))
    else:
        lib = make_git_repo(tmp_path / "lib")
        git(main, "-c", "protocol.file.allow=always", "submodule", "add", "-q", lib, "sub")
        inner = main / "sub"
    _relative_sync_script(inner)
    monkeypatch.chdir(inner)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"]) == 1
    assert "not a checkout" in capsys.readouterr().err
    assert not (inner / "VERSION").exists()
    assert not (main / "VERSION").exists()
    assert get_project(engine, "proj-g")["current_version"] == "1.0.0"


@needs_git
def test_release_monorepo_subproject_syncs_in_the_worktree_subdirectory(
        cli_engine, monkeypatch, tmp_path):
    main = Path(make_git_repo(tmp_path / "mono"))
    _relative_sync_script(main / "packages" / "app")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "app")
    linked = tmp_path / "wt-mono"
    git(main, "worktree", "add", "-q", "-b", "feat/app", str(linked))
    create_project(cli_engine, slug="proj-m", name="M",
                   path=str(main / "packages" / "app"), current_version="1.0.0")
    add_entry(cli_engine, "proj-m", "added", "a")
    monkeypatch.chdir(linked)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-m", "1.1.0"])
    assert (linked / "packages" / "app" / "VERSION").read_text().strip() == "1.1.0"
    assert not (main / "packages" / "app" / "VERSION").exists()
    assert not (linked / "VERSION").exists()


@needs_git
def test_release_from_a_deleted_cwd_is_refused(gitproj, monkeypatch, capsys):
    import os

    engine, main = gitproj
    add_entry(engine, "proj-g", "added", "a")
    gone = main / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    os.rmdir(gone)
    assert _exit_code(monkeypatch, ["clog", "release", "-P", "proj-g", "1.1.0"]) == 1
    assert "not a checkout" in capsys.readouterr().err
    assert get_project(engine, "proj-g")["current_version"] == "1.0.0"
    assert not (main / "VERSION").exists()


def test_release_project_path_outside_git_keeps_the_registered_path(
        cli_engine, monkeypatch, tmp_path):
    plain = tmp_path / "plain"
    _relative_sync_script(plain)
    create_project(cli_engine, slug="proj-p", name="P", path=str(plain), current_version="1.0.0")
    add_entry(cli_engine, "proj-p", "added", "a")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-p", "1.1.0"])
    assert (plain / "VERSION").read_text().strip() == "1.1.0"
    assert not (elsewhere / "VERSION").exists()


def test_release_v_prefixed_version_is_reported_normalized(versioned, monkeypatch, capsys):
    engine, _ = versioned
    run_cli(monkeypatch, ["clog", "release", "-P", "proj-v", "v1.1.0"])
    assert "as 1.1.0" in capsys.readouterr().out
    assert get_project(engine, "proj-v")["current_version"] == "1.1.0"


# ── clog update / delete ──────────────────────────────────────────────────────


def test_update_moves_branch_and_clears_with_empty_value(cli_engine, monkeypatch, capsys):
    eid = add_entry(cli_engine, "proj-a", "added", "x", component="core",
                    branch="feat/old", branch_source="server")["id"]
    run_cli(monkeypatch, ["clog", "update", str(eid), "-P", "proj-a",
                          "--branch", "feat/new", "--component", "", "--entry", "better"])
    row = _rows(cli_engine)[eid]
    assert (row["branch"], row["branch_source"], row["component"], row["entry"]) == (
        "feat/new", "client", None, "better")
    assert f"Changelog #{eid} updated" in capsys.readouterr().out


def test_update_item_id_zero_unlinks(cli_engine, monkeypatch):
    from dct.core.items import create_item

    item = create_item(cli_engine, "proj-a", "todo", "T")
    eid = add_entry(cli_engine, "proj-a", "added", "x", related_item_id=item["id"])["id"]
    run_cli(monkeypatch, ["clog", "update", str(eid), "-P", "proj-a", "--item-id", "0"])
    assert _rows(cli_engine)[eid]["related_item_id"] is None


def test_update_released_entry_refused(cli_engine, monkeypatch, capsys):
    eid = add_entry(cli_engine, "proj-a", "added", "x")["id"]
    release_entries(cli_engine, "proj-a", "1.0.0")
    assert _exit_code(monkeypatch, ["clog", "update", str(eid), "-P", "proj-a",
                                    "--entry", "y"]) == 1
    assert "already released" in capsys.readouterr().err


def test_update_nothing_given_refused(cli_engine, monkeypatch, capsys):
    eid = add_entry(cli_engine, "proj-a", "added", "x")["id"]
    assert _exit_code(monkeypatch, ["clog", "update", str(eid), "-P", "proj-a"]) == 1
    assert "Nothing to update" in capsys.readouterr().err


def test_delete_then_delete_again(cli_engine, monkeypatch, capsys):
    eid = add_entry(cli_engine, "proj-a", "added", "x")["id"]
    run_cli(monkeypatch, ["clog", "delete", str(eid), "-P", "proj-a"])
    assert _rows(cli_engine) == {}
    assert _exit_code(monkeypatch, ["clog", "delete", str(eid), "-P", "proj-a"]) == 1
    assert "not found" in capsys.readouterr().err


def test_update_is_project_scoped(cli_engine, monkeypatch, capsys):
    create_project(cli_engine, slug="proj-b", name="B")
    theirs = add_entry(cli_engine, "proj-b", "added", "theirs")["id"]
    assert _exit_code(monkeypatch, ["clog", "delete", str(theirs), "-P", "proj-a"]) == 1
    assert _rows(cli_engine, "proj-b")[theirs]["entry"] == "theirs"
