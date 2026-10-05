"""Client worktree paths are validated by THEIR syntax, not the server's (K-64).

An MCP server on POSIX receives paths from clients on any OS. A Windows
absolute path (`C:\\...`, `C:/...`, UNC `\\\\server\\share\\...`) is absolute
even though posixpath would call it relative; POSIX paths stay POSIX.
Normalization is TEXTUAL by the detected syntax (ntpath vs posixpath) —
never a realpath or existence check on the server. Relative and ambiguous
forms (drive-relative `C:x`, rooted-without-drive `\\x`, `~`) are refused.
"""

from __future__ import annotations

import pytest

from dct.core.changelog import add_entry, list_entries, normalize_worktree
from dct.core.projects import create_project


@pytest.mark.parametrize("raw,stored", [
    ("C:\\Users\\dev\\proj", "C:\\Users\\dev\\proj"),
    ("C:/Users/dev/proj/", "C:\\Users\\dev\\proj"),
    ("c:\\work\\.\\wt\\..\\proj\\", "c:\\work\\proj"),
    ("D:\\", "D:\\"),
    ("\\\\server\\share\\proj", "\\\\server\\share\\proj"),
    ("\\\\server\\share\\a\\..\\proj\\", "\\\\server\\share\\proj"),
    ("/tmp/wt-x/", "/tmp/wt-x"),
    ("/tmp/a/../wt-x", "/tmp/wt-x"),
])
def test_absolute_paths_are_normalized_by_their_own_syntax(raw, stored):
    assert normalize_worktree(raw) == stored


@pytest.mark.parametrize("bad", [
    "C:", "C:proj", "C:..\\proj",          # drive-relative
    "\\proj", "\\proj\\wt",               # rooted, no drive: ambiguous
    "\\\\server", "\\\\server\\",          # UNC without a share
    "proj\\wt", ".\\wt", "..\\wt", "~\\wt", "wt-x", "~/wt-x",
])
def test_relative_or_ambiguous_paths_are_refused(bad):
    with pytest.raises(ValueError, match="worktree must be an absolute path"):
        normalize_worktree(bad)


def test_normalization_is_idempotent():
    for raw in ("C:/Users/dev/proj/", "\\\\srv\\share\\x\\", "/tmp//wt/"):
        once = normalize_worktree(raw)
        assert normalize_worktree(once) == once


def test_windows_path_round_trips_through_store_and_filter(engine):
    create_project(engine, slug="proj-a", name="A")
    eid = add_entry(engine, "proj-a", "added", "x", worktree="C:/Users/dev/proj/")["id"]
    add_entry(engine, "proj-a", "added", "y", worktree="C:\\Users\\dev\\other")
    assert [e["id"] for e in list_entries(engine, "proj-a", worktree="C:\\Users\\dev\\proj")] == [eid]


def test_no_filesystem_access(monkeypatch):
    import os

    def _boom(*a, **k):
        raise AssertionError("client paths are never resolved on the server")

    with monkeypatch.context() as m:
        m.setattr(os.path, "realpath", _boom)
        m.setattr(os.path, "exists", _boom)
        try:
            results = [normalize_worktree("C:\\x\\y"), normalize_worktree("/x/y/")]
        except Exception as exc:  # noqa: BLE001 - asserted below, outside the patch
            results = [exc]
    assert results == ["C:\\x\\y", "/x/y"]
