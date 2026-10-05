"""Adversarial pass over branch metadata, item labels and checkout resolution (R5).

Rules that must hold for ALL inputs, not just the examples the feature tests
use:
- decide_branch_fields (K-42) over every (existing row, provided fields,
  source) combination;
- the branch / worktree filters form one consistent algebra: count == len(list)
  for every filter, a filter never adds rows, include_unbranched only adds the
  uncertain ones, and no row is hidden from the filter on its own branch;
- an item label never shows a blank legacy short_title anywhere a row
  references the item (end to end through the SQL joins);
- short_title boundaries in characters (emoji, combining marks, Unicode
  whitespace);
- resolve_checkout on directories that are inside a repository but are not a
  checkout of it.
"""

from __future__ import annotations

import itertools
import os
import shutil

import pytest
import sqlalchemy as sa
from conftest import git, make_git_repo

from dct.core import gitinfo
from dct.core.changelog import (
    add_entry,
    count_unreleased,
    decide_branch_fields,
    delete_entry,
    list_entries,
    normalize_worktree,
    release_entries,
)
from dct.core.checkpoints import add_checkpoint
from dct.core.items import create_item, get_item
from dct.core.projects import create_project
from dct.core.sprints import set_current_sprint_items
from dct.db import items as items_tbl
from dct.workflows.session import get_context_snapshot

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture
def proj(engine):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    return engine


# ── decide_branch_fields: the K-42 rule over every combination ───────────────

EXISTING = {
    "new": None,
    "unbranched": {"branch": None, "worktree": None, "branch_source": None},
    "client_full": {"branch": "feat/old", "worktree": "/tmp/wt-old", "branch_source": "client"},
    "client_branch_only": {"branch": "feat/old", "worktree": None, "branch_source": "client"},
    "server_full": {"branch": "main", "worktree": "/tmp/srv", "branch_source": "server"},
    "server_worktree_only": {"branch": None, "worktree": "/tmp/srv", "branch_source": "server"},
}
_ABSENT = object()
BRANCH_INPUTS = [_ABSENT, None, "", "feat/new"]
WORKTREE_INPUTS = [_ABSENT, None, "", "/tmp/wt-new/", "/tmp//a/../wt-new"]


def _cases():
    for (name, existing), b, w, source in itertools.product(
        EXISTING.items(), BRANCH_INPUTS, WORKTREE_INPUTS, ("client", "server")
    ):
        provided = {}
        if b is not _ABSENT:
            provided["branch"] = b
        if w is not _ABSENT:
            provided["worktree"] = w
        yield pytest.param(existing, provided, source,
                           id=f"{name}-b={b if b is not _ABSENT else '_'}"
                              f"-w={w if w is not _ABSENT else '_'}-{source}")


@pytest.mark.parametrize("existing,provided,source", list(_cases()))
def test_decide_branch_fields_holds_every_provenance_rule(existing, provided, source):
    out = decide_branch_fields(provided, existing, source)
    assert set(out) == {"branch", "worktree", "branch_source"}
    server_row = existing is not None and existing.get("branch_source") == "server"
    for key in ("branch", "worktree"):
        if key in provided:
            raw = provided[key]
            want = normalize_worktree(raw) if key == "worktree" else (raw or None)
            assert out[key] == want, f"provided {key} not stored as given"
        elif existing is None or server_row:
            # Omission is never a clear — but an UNCONFIRMED server value is
            # never carried over into a triple the caller vouches for.
            assert out[key] is None, f"unconfirmed {key} carried over"
        else:
            assert out[key] == existing[key], f"omitted {key} was not kept"
    no_value = out["branch"] is None and out["worktree"] is None
    assert out["branch_source"] == (None if no_value else source)


@pytest.mark.parametrize("existing,provided,source",
                         [c for c in _cases() if c.values[2] == "client"])
def test_reapplying_the_same_client_edit_changes_nothing(existing, provided, source):
    once = decide_branch_fields(provided, existing, source)
    assert decide_branch_fields(provided, once, source) == once


# ── filters: one consistent algebra ──────────────────────────────────────────


@pytest.fixture
def varied(proj):
    """Client / server / unbranched rows, a released one and a deleted one."""
    def add(text, **kw):
        return add_entry(proj, "proj-a", "added", text, **kw)["id"]

    ids = {
        "client_x": add("a", branch="feat/x", worktree="/tmp/wt-x"),
        "client_x_nowt": add("b", branch="feat/x"),
        "client_y": add("c", branch="feat/y", worktree="/tmp/wt-y"),
        "server_main": add("d", branch="main", worktree="/tmp/proj-a", branch_source="server"),
        "server_x": add("e", branch="feat/x", worktree="/tmp/wt-x", branch_source="server"),
        "server_detached": add("f", worktree="/tmp/proj-a", branch_source="server"),
        "worktree_only": add("g", worktree="/tmp/wt-y"),
        "none": add("h"),
        "released_x": add("i", branch="feat/x"),
        "deleted_x": add("j", branch="feat/x"),
    }
    release_entries(proj, "proj-a", "1.1.0", entry_ids=[ids["released_x"]])
    delete_entry(proj, "proj-a", ids["deleted_x"])
    return proj, ids


FILTERS = [
    {}, {"include_unbranched": True},
    *({"branch": b, "include_unbranched": inc}
      for b in ("feat/x", "feat/y", "main", "feat/none") for inc in (False, True)),
    *({"worktree": w} for w in ("/tmp/wt-x", "/tmp/wt-x/", "/tmp/proj-a", "/tmp/nowhere")),
    {"branch": "feat/x", "worktree": "/tmp/wt-x", "include_unbranched": True},
]


def _ids(rows):
    return {r["id"] for r in rows}


@pytest.mark.parametrize("flt", FILTERS, ids=lambda f: ",".join(f"{k}={v}" for k, v in f.items()))
def test_count_equals_the_unreleased_list_for_every_filter(varied, flt):
    engine, _ = varied
    rows = list_entries(engine, "proj-a", unreleased_only=True, **flt)
    assert count_unreleased(engine, "proj-a", **flt) == len(rows)
    assert all(r["version"] is None for r in rows)


@pytest.mark.parametrize("branch", ["feat/x", "feat/y", "main", "feat/none"])
def test_branch_filters_only_ever_narrow_and_unbranched_adds_only_uncertain_rows(
    varied, branch,
):
    engine, _ = varied
    everything = list_entries(engine, "proj-a")
    plain = list_entries(engine, "proj-a", branch=branch)
    wide = list_entries(engine, "proj-a", branch=branch, include_unbranched=True)
    assert _ids(plain) <= _ids(wide) <= _ids(everything)
    assert all(r["branch"] == branch for r in plain)
    added = [r for r in wide if r["id"] not in _ids(plain)]
    assert all(r["branch"] is None or r["branch_source"] == "server" for r in added)
    uncertain = {r["id"] for r in everything
                 if r["branch"] is None or r["branch_source"] == "server"}
    assert uncertain <= _ids(wide), "an uncertain row was hidden from include_unbranched"


def test_no_live_row_is_hidden_from_the_filter_on_its_own_metadata(varied):
    engine, ids = varied
    everything = list_entries(engine, "proj-a")
    assert ids["deleted_x"] not in _ids(everything)
    assert len(everything) == len(ids) - 1
    for row in everything:
        if row["branch"] is not None:
            assert row["id"] in _ids(list_entries(engine, "proj-a", branch=row["branch"]))
        if row["worktree"] is not None:
            for spelling in (row["worktree"], row["worktree"] + "/"):
                assert row["id"] in _ids(list_entries(engine, "proj-a", worktree=spelling))


# ── labels: legacy blank short_title never masks the title ───────────────────


def _store_raw_short_title(engine, item_id, value):
    """Bypass K-43 normalization — the state a pre-K-43 write left behind."""
    with engine.begin() as conn:
        conn.execute(items_tbl.update().where(items_tbl.c.id == item_id)
                     .values(short_title=value))


@pytest.mark.parametrize("legacy", ["", " ", "\t", " 　"])
def test_changelog_rows_fall_back_to_the_title_for_a_blank_legacy_label(proj, legacy):
    item = create_item(proj, "proj-a", "todo", "Real title of the item")
    _store_raw_short_title(proj, item["id"], legacy)
    add_entry(proj, "proj-a", "fixed", "x", related_item_id=item["id"])
    [row] = list_entries(proj, "proj-a")
    assert row["item_short_title"] == "Real title of the item"


@pytest.mark.parametrize("legacy", ["", "   ", "\n"])
def test_snapshot_checkpoint_rows_fall_back_to_the_title_for_a_blank_legacy_label(
    proj, legacy,
):
    item = create_item(proj, "proj-a", "feature", "Sprint item with a real title")
    _store_raw_short_title(proj, item["id"], legacy)
    set_current_sprint_items(proj, "proj-a", [item["id"]])
    add_checkpoint(proj, item["id"], "Validate in a fresh session", kind="dogfood")
    snap = get_context_snapshot(proj, "proj-a")
    referencing = []
    for section in (snap["gates"], snap["sprint"]["checkpoints"]):
        cols = section["cols"]
        referencing += [dict(zip(cols, r)) for r in section["rows"]]
    assert referencing, "the fixture must produce rows that reference the item"
    assert {r["item_short_title"] for r in referencing} == {"Sprint item with a real title"}


# ── short_title boundaries counted in characters ─────────────────────────────


@pytest.mark.parametrize("label,stored", [
    ("🚀" * 50, "🚀" * 50),                         # 50 astral code points fit
    ("é" * 25, "é" * 25),               # 50 code points incl. combining marks
    ("  　", None),                   # Unicode-only whitespace → NULL
    (" Label　", "Label"),
])
def test_short_title_fits_and_strips_by_characters(proj, label, stored):
    item = create_item(proj, "proj-a", "todo", "T", short_title=label)
    assert get_item(proj, item["id"])["short_title"] == stored


@pytest.mark.parametrize("label", ["🚀" * 51, "é" * 25 + "x", "　" * 51])
def test_short_title_over_50_characters_is_refused_whatever_they_are(proj, label):
    with pytest.raises(ValueError, match="short_title too long: 51 chars, max 50"):
        create_item(proj, "proj-a", "todo", "T", short_title=label)
    with proj.begin() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(items_tbl)).scalar() == 0


# ── resolve_checkout: inside a repository, but not a checkout of it ──────────


@needs_git
def test_resolve_checkout_from_inside_the_git_dir_is_no_context(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    assert gitinfo.resolve_checkout(root, os.path.join(root, ".git")) is None
    assert gitinfo.resolve_checkout(root, os.path.join(root, ".git", "refs")) is None


@needs_git
def test_resolve_checkout_with_a_file_as_cwd_is_no_context(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    assert gitinfo.resolve_checkout(root, os.path.join(root, "README")) is None


@needs_git
def test_resolve_checkout_without_git_on_path_is_no_context(tmp_path, monkeypatch):
    root = make_git_repo(tmp_path / "repo")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert gitinfo.resolve_checkout(root, root) is None


@needs_git
def test_project_registered_at_a_linked_worktree_resolves_from_the_main_checkout(tmp_path):
    root = make_git_repo(tmp_path / "repo")
    linked = tmp_path / "wt"
    git(root, "worktree", "add", "-q", "-b", "feat/wt", str(linked))
    out = gitinfo.resolve_checkout(str(linked), root)
    assert (out["repo_root"], out["project_root"], out["branch"]) == (root, root, "main")


@needs_git
def test_project_registered_through_a_symlink_maps_into_the_worktree(tmp_path):
    root = make_git_repo(tmp_path / "mono")
    (tmp_path / "mono" / "apps" / "web").mkdir(parents=True)
    link = tmp_path / "web-link"
    link.symlink_to(tmp_path / "mono" / "apps" / "web")
    linked = tmp_path / "wt"
    git(root, "worktree", "add", "-q", "-b", "feat/web", str(linked))
    out = gitinfo.resolve_checkout(str(link), str(linked))
    real_linked = os.path.realpath(linked)
    assert out["repo_root"] == real_linked
    assert out["project_root"] == os.path.join(real_linked, "apps", "web")
