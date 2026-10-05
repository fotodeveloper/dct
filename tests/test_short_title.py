"""items.short_title (#817) — the label rule, the write path, the brief view,
and label coverage of every item-bearing row in the session dashboards.

The one rule (dct.core.text.item_label): a row that IS an item carries the raw
`short_title` (may be null); a row that REFERENCES an item carries
`item_short_title` = short_title, else the title compacted to 50 chars.
"""

from datetime import UTC, datetime, timedelta

import pytest

from dct.core.checkpoints import add_checkpoint
from dct.core.items import create_item, get_item, update_item
from dct.core.sprints import get_current_sprint, set_current_sprint_items
from dct.core.text import ITEM_LABEL_CHARS, PREVIEW_CHARS, _preview, item_label
from dct.db import items as items_tbl
from dct.workflows.task_mirror import MIRROR_SHORT_TITLES, mirror_tasks

# ── item_label: the one rule ──────────────────────────────────────────────────


def test_item_label_prefers_short_title():
    assert item_label("Hook paths", "A much longer title about hooks") == "Hook paths"


def test_item_label_exactly_50_char_title_is_untouched():
    title = "t" * 50
    assert item_label(None, title) == title


def test_item_label_51_char_title_becomes_49_chars_plus_ellipsis():
    got = item_label(None, "t" * 51)
    assert got == "t" * 49 + "…"
    assert len(got) == ITEM_LABEL_CHARS


@pytest.mark.parametrize("empty", [None, ""])
def test_item_label_empty_short_title_falls_back_to_title(empty):
    assert item_label(empty, "Title") == "Title"


def test_item_label_none_title_and_no_short_title_is_none():
    assert item_label(None, None) is None


@pytest.mark.parametrize("n", [0, 1, 49, 50, 51, 500])
def test_item_label_never_exceeds_the_column_width(n):
    got = item_label(None, "x" * n)
    assert len(got) <= ITEM_LABEL_CHARS
    assert got.endswith("…") is (n > ITEM_LABEL_CHARS)


def test_preview_default_budget_unchanged_by_the_move():
    assert PREVIEW_CHARS == 200
    assert _preview("q" * 201) == "q" * 199 + "…"


# ── core write path ───────────────────────────────────────────────────────────


@pytest.fixture
def proj(engine):
    from dct.core.projects import create_project

    return create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")


def test_create_empty_short_title_is_stored_as_null(engine, proj):
    item = create_item(engine, "proj-a", "todo", "T", short_title="")
    assert item["short_title"] is None
    assert get_item(engine, item["id"])["short_title"] is None


def test_create_50_multibyte_chars_fit_counted_in_characters(engine, proj):
    label = "ż" * 50  # 100 bytes in UTF-8, 50 characters
    item = create_item(engine, "proj-a", "todo", "T", short_title=label)
    assert get_item(engine, item["id"])["short_title"] == label


def test_create_51_chars_refused_names_the_field_and_writes_nothing(engine, proj):
    import sqlalchemy as sa

    with pytest.raises(ValueError, match=r"short_title too long: 51 chars, max 50"):
        create_item(engine, "proj-a", "todo", "T", short_title="s" * 51)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(items_tbl)).scalar() == 0


def test_update_short_title_set_none_keeps_empty_clears(engine, proj):
    item = create_item(engine, "proj-a", "todo", "T", short_title="Old label")
    iid = item["id"]
    assert update_item(engine, iid, short_title="New label")["short_title"] == "New label"
    # None = not passed → untouched (it rides along with another field).
    assert update_item(engine, iid, priority="P1", short_title=None)["short_title"] == "New label"
    # "" is an explicit clear, and on its own is a valid update.
    assert update_item(engine, iid, short_title="")["short_title"] is None


def test_update_short_title_too_long_refused_row_unchanged(engine, proj):
    item = create_item(engine, "proj-a", "todo", "T", short_title="Keep")
    with pytest.raises(ValueError, match="short_title"):
        update_item(engine, item["id"], short_title="x" * 51)
    assert get_item(engine, item["id"])["short_title"] == "Keep"


# ── task mirror: fixed, meaningful label ──────────────────────────────────────


def test_claude_task_mirror_item_gets_the_fixed_label(engine, seed_project):
    payload = {"tool_input": {"todos": [{"content": "Write the thing"}]}}
    result = mirror_tasks(engine, payload, client="claude", project="test", session_id="s-1")
    item = get_item(engine, result["item_id"])
    assert item["short_title"] == "Claude Code task list mirror"
    assert item["short_title"] != item["title"][:50]  # a label, not a truncated title


def test_codex_task_mirror_item_gets_the_fixed_label(engine, seed_project):
    payload = {
        "session_id": "sess", "turn_id": "turn", "tool_use_id": "ev",
        "tool_input": {"plan": [{"step": "Do it", "status": "pending"}]},
    }
    result = mirror_tasks(engine, payload, client="codex", project="test")
    assert get_item(engine, result["item_id"])["short_title"] == "Codex task list mirror"


def test_mirror_labels_fit_the_column():
    assert all(0 < len(v) <= items_tbl.c.short_title.type.length
               for v in MIRROR_SHORT_TITLES.values())


# ── MCP list_items: brief (default) vs full view ──────────────────────────────

BRIEF = ["id", "item_type", "priority", "status", "in_current_sprint",
         "cp", "component", "tags", "created_at", "short_title", "title"]


@pytest.fixture
def two_items(srv):
    a = srv.create_item("todo", "Alpha", project="test", short_title="Alpha label",
                        description="needle-in-description")
    b = srv.create_item("todo", "Beta", project="test", description="plain")
    return a, b


def test_list_items_default_is_brief_without_description(srv, two_items):
    out = srv.list_items(project="test")
    assert out["cols"] == BRIEF
    assert "description" not in out["cols"]
    rows = {r[0]: dict(zip(out["cols"], r)) for r in out["rows"]}
    assert rows[two_items[0]["id"]]["short_title"] == "Alpha label"
    assert rows[two_items[1]["id"]]["short_title"] is None


def test_list_items_full_view_appends_description(srv, two_items):
    out = srv.list_items(project="test", view="full")
    assert out["cols"] == BRIEF + ["description"]
    rows = {r[0]: dict(zip(out["cols"], r)) for r in out["rows"]}
    assert rows[two_items[0]["id"]]["description"] == "needle-in-description"


def test_list_items_dict_brief_is_projected_onto_brief_cols(srv, two_items):
    out = srv.list_items(project="test", format="dict")
    assert out and all(list(d) == BRIEF for d in out)


def test_list_items_dict_full_keeps_the_full_item_dicts(srv, two_items):
    out = srv.list_items(project="test", format="dict", view="full")
    for key in ("description", "project_id", "source_file", "resolution", "updated_at"):
        assert all(key in d for d in out), key


def test_list_items_markdown_brief_has_no_description_header(srv, two_items):
    out = srv.list_items(project="test", format="markdown")
    header = out.splitlines()[0]
    assert "short_title" in header and "description" not in header


@pytest.mark.parametrize("bad", ["", "BRIEF", "compact", "full "])
def test_list_items_unknown_view_errors_naming_valid_values(srv, bad):
    out = srv.list_items(project="test", view=bad)
    assert set(out) == {"error"}
    assert "brief" in out["error"] and "full" in out["error"]


def test_list_items_search_matches_description_in_brief_view(srv, two_items):
    out = srv.list_items(project="test", search="needle-in")
    assert [r[0] for r in out["rows"]] == [two_items[0]["id"]]
    assert "description" not in out["cols"]
    assert all("needle" not in str(v) for v in out["rows"][0])


def test_list_items_in_sprint_carries_short_title(srv, two_items, engine):
    set_current_sprint_items(engine, "test", [two_items[0]["id"], two_items[1]["id"]])
    sprint_id = get_current_sprint(engine, "test")["id"]
    out = srv.list_items_in_sprint(sprint_id)
    assert out["cols"] == ["id", "item_type", "priority", "status", "short_title", "title"]
    labels = {r[0]: r[4] for r in out["rows"]}
    assert labels == {two_items[0]["id"]: "Alpha label", two_items[1]["id"]: None}


# ── coverage: every item-bearing row in the dashboards carries a label ────────

LONG_TITLE = "An unlabelled item whose title is far longer than fifty characters"

# {cols,rows} tables that carry no item id at all (handoffs, decisions).
NON_ITEM_TABLES = {"handoff.others", "decisions", "unconsumed_handoffs"}


def _tables(node, path=""):
    """Yield (path, table) for every {cols,rows} table nested in a response."""
    if isinstance(node, dict):
        if "cols" in node and "rows" in node:
            yield path, node
            return
        for key, value in node.items():
            yield from _tables(value, f"{path}.{key}" if path else key)


def _age(engine, item_id, days):
    with engine.begin() as conn:
        conn.execute(items_tbl.update().where(items_tbl.c.id == item_id)
                     .values(updated_at=datetime.now(UTC) - timedelta(days=days)))


@pytest.fixture
def labelled_project(srv, engine):
    labelled = srv.create_item("feature", "Labelled in sprint", project="test",
                               short_title="Sprint label")
    bare = srv.create_item("feature", LONG_TITLE, project="test")
    stale_labelled = srv.create_item("todo", "Stale labelled", project="test",
                                     short_title="Stale label")
    stale_bare = srv.create_item("todo", "Stale bare WIP", project="test")
    for it in (stale_labelled, stale_bare):
        srv.update_item(it["id"], status="in_progress")
        _age(engine, it["id"], 30)
    set_current_sprint_items(engine, "test", [labelled["id"], bare["id"]])
    for it in (labelled, bare):
        add_checkpoint(engine, it["id"], "Plain step")
        add_checkpoint(engine, it["id"], "Fresh-session dogfood", kind="dogfood")
    srv.create_handoff(prompt="Continue", created_by="tester", project="test")
    stored = {it["id"]: get_item(engine, it["id"]) for it in
              (labelled, bare, stale_labelled, stale_bare)}
    return stored


def _assert_labelled(response, stored, expect_item_tables):
    seen_item_tables = set()
    for path, tbl in _tables(response):
        if path in NON_ITEM_TABLES:
            continue
        rows = [dict(zip(tbl["cols"], r)) for r in tbl["rows"]]
        assert rows, f"{path} is empty — the fixture must exercise it"
        if "item_id" in tbl["cols"]:
            assert "item_short_title" in tbl["cols"], path
            for r in rows:
                it = stored[r["item_id"]]
                assert r["item_short_title"] == item_label(it["short_title"], it["title"]), path
        else:
            assert "id" in tbl["cols"], path
            assert "short_title" in tbl["cols"], path
            seen_item_tables.add(path)
            for r in rows:
                assert r["short_title"] == stored[r["id"]]["short_title"], path
    assert seen_item_tables == expect_item_tables


def test_snapshot_every_item_row_carries_a_label(srv, labelled_project):
    snap = srv.get_context_snapshot(project="test")
    _assert_labelled(snap, labelled_project, {
        "sprint.items", "sprint.stale_in_progress", "recent_changes.items_changed",
    })
    # Referencing rows fall back to the truncated title when no label is stored.
    gates = [dict(zip(snap["gates"]["cols"], r)) for r in snap["gates"]["rows"]]
    bare_gate = next(g for g in gates if g["item_title"] == LONG_TITLE)
    assert bare_gate["item_short_title"] == LONG_TITLE[:49] + "…"
    assert {g["item_short_title"] for g in gates} >= {"Sprint label"}


def test_review_sprint_every_item_row_carries_a_label(srv, labelled_project):
    review = srv.review_sprint(project="test", since="all")
    _assert_labelled(review, labelled_project, {
        "in_sprint", "recent_activity.items_changed", "stale_in_progress",
    })
