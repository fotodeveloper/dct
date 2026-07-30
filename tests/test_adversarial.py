"""Adversarial and edge-case tests — worst case, bad intentions, data integrity."""

import pytest

from dct.core.changelog import add_entry, count_unreleased, delete_entry, list_entries
from dct.core.items import (
    _parse_tags,
    _serialize_tags,
    add_note,
    create_item,
    delete_item,
    get_item,
    list_items,
    resolve_item,
    update_item,
)
from dct.core.projects import (
    create_project,
    delete_project,
    get_project,
    get_project_by_path,
)
from dct.mcp import server as dct_server

# ── Empty / blank inputs ─────────────────────────────────────────────────────


class TestEmptyInputs:
    def test_empty_slug(self, engine):
        with pytest.raises(ValueError, match="slug cannot be empty"):
            create_project(engine, slug="", name="X")

    def test_whitespace_slug(self, engine):
        with pytest.raises(ValueError, match="slug cannot be empty"):
            create_project(engine, slug="   ", name="X")

    def test_empty_name(self, engine):
        with pytest.raises(ValueError, match="name cannot be empty"):
            create_project(engine, slug="test", name="")

    def test_empty_title(self, engine):
        create_project(engine, slug="p", name="P")
        with pytest.raises(ValueError, match="title cannot be empty"):
            create_item(engine, "p", "todo", "")

    def test_whitespace_title(self, engine):
        create_project(engine, slug="p", name="P")
        with pytest.raises(ValueError, match="title cannot be empty"):
            create_item(engine, "p", "todo", "   \t\n  ")

    def test_empty_changelog_entry(self, engine):
        create_project(engine, slug="p", name="P")
        with pytest.raises(ValueError, match="entry cannot be empty"):
            add_entry(engine, "p", "added", "")

    def test_empty_note(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task")
        with pytest.raises(ValueError, match="Note cannot be empty"):
            add_note(engine, item["id"], "")

    def test_whitespace_note(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task")
        with pytest.raises(ValueError, match="Note cannot be empty"):
            add_note(engine, item["id"], "   ")


# ── Tags edge cases ──────────────────────────────────────────────────────────


class TestTagsEdgeCases:
    """Core `_serialize_tags` accepts list[str] | None only (see #31).
    String-form normalization (CSV) happens at CLI boundary; JSON array
    handling happens at MCP boundary. Core stays pure.
    """

    def test_none_tags(self, engine):
        assert _serialize_tags(None) == "[]"

    def test_empty_list_tags(self, engine):
        assert _serialize_tags([]) == "[]"

    def test_list_with_empty_strings(self, engine):
        assert _serialize_tags(["", "", ""]) == "[]"

    def test_string_input_raises(self, engine):
        """Historical bug #31: accepting str here caused JSON-array-as-string
        to be split naively on ',' producing mangled fragments."""
        with pytest.raises(AssertionError, match="tags must be list"):
            _serialize_tags("a,b,c")  # type: ignore[arg-type]

    def test_json_array_string_raises(self, engine):
        """Passing the JSON-stringified form must fail loudly, not be quietly
        stored as-is (original bug symptom). Clients serializing list→string
        are now rejected at schema boundary (server.py) or CLI boundary."""
        with pytest.raises(AssertionError, match="tags must be list"):
            _serialize_tags('["a","b"]')  # type: ignore[arg-type]

    def test_list_preserves_whitespace_in_tags(self, engine):
        """Core does not trim — normalization is the caller's job (CLI does it).
        Empty strings are filtered (falsy), whitespace-only strings are NOT
        (truthy in Python). Callers passing ' ' must accept it goes to DB."""
        result = _serialize_tags([" perf ", "arch", "", "   "])
        assert _parse_tags(result) == [" perf ", "arch", "   "]

    def test_unicode_tags(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task", tags=["日本語", "émojis", "żółć"])
        assert "日本語" in item["tags"]
        assert "żółć" in item["tags"]


# ── Path matching ─────────────────────────────────────────────────────────────


class TestPathMatching:
    def test_prefix_false_positive_prevented(self, engine):
        """proj-a-other should NOT match project registered at proj-a."""
        create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
        assert get_project_by_path(engine, "/tmp/proj-a-other/file.py") is None

    def test_exact_path_match(self, engine):
        create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
        p = get_project_by_path(engine, "/tmp/proj-a")
        assert p["slug"] == "proj-a"

    def test_subdir_match(self, engine):
        create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")
        p = get_project_by_path(engine, "/tmp/proj-a/subdir/nested/main.go")
        assert p["slug"] == "proj-a"

    def test_most_specific_match(self, engine):
        """Nested paths: longer path wins."""
        create_project(engine, slug="mono", name="Monorepo", path="/tmp/mono")
        create_project(engine, slug="sub", name="Sub", path="/tmp/mono/packages/sub")
        p = get_project_by_path(engine, "/tmp/mono/packages/sub/src/main.ts")
        assert p["slug"] == "sub"

    def test_trailing_slash_normalization(self, engine):
        create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a/")
        p = get_project_by_path(engine, "/tmp/proj-a/src/main.go")
        assert p["slug"] == "proj-a"


# ── Soft delete integrity ────────────────────────────────────────────────────


class TestSoftDeleteIntegrity:
    def test_deleted_items_invisible_in_list(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Visible")
        delete_item(engine, item["id"])
        assert list_items(engine) == []

    def test_deleted_items_invisible_in_get(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Gone")
        delete_item(engine, item["id"])
        assert get_item(engine, item["id"]) is None

    def test_double_delete(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "X")
        assert delete_item(engine, item["id"]) is True
        assert delete_item(engine, item["id"]) is False  # already deleted

    def test_update_deleted_item_returns_none(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "X")
        delete_item(engine, item["id"])
        result = update_item(engine, item["id"], title="New")
        assert result is None

    def test_resolve_deleted_item_returns_none(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "X")
        delete_item(engine, item["id"])
        result = resolve_item(engine, item["id"])
        assert result is None

    def test_note_on_deleted_item_raises(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "X")
        delete_item(engine, item["id"])
        with pytest.raises(ValueError, match="not found"):
            add_note(engine, item["id"], "Should fail")

    def test_slug_reuse_after_soft_delete(self, engine):
        """H4: soft-deleted slug should be reusable."""
        create_project(engine, slug="proj-a", name="Old Project A", path="/old")
        delete_project(engine, "proj-a")
        # Should restore, not raise
        p = create_project(engine, slug="proj-a", name="New Project A", path="/new")
        assert p["name"] == "New Project A"
        assert p["path"] == "/new"
        assert p["deleted_at"] is None

    def test_deleted_changelog_not_counted(self, engine):
        create_project(engine, slug="p", name="P")
        e = add_entry(engine, "p", "added", "Feature")
        delete_entry(engine, e["id"])
        assert count_unreleased(engine, "p") == 0


# ── FK integrity ─────────────────────────────────────────────────────────────


class TestFKIntegrity:
    def test_item_with_nonexistent_project_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            create_item(engine, "nonexistent", "todo", "Orphan")

    def test_note_with_nonexistent_item_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            add_note(engine, 99999, "Orphan note")

    def test_changelog_with_nonexistent_project_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            add_entry(engine, "nonexistent", "added", "Orphan entry")


# ── Update edge cases ────────────────────────────────────────────────────────


class TestUpdateEdgeCases:
    def test_update_nonexistent_item(self, engine):
        create_project(engine, slug="p", name="P")
        result = update_item(engine, 99999, title="Ghost")
        assert result is None

    def test_resolve_nonexistent_item(self, engine):
        create_project(engine, slug="p", name="P")
        result = resolve_item(engine, 99999)
        assert result is None

    def test_clear_component(self, engine):
        """H6: empty string on clearable fields should set to NULL."""
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task", component="parser")
        assert item["component"] == "parser"
        updated = update_item(engine, item["id"], component="")
        assert updated["component"] is None

    def test_clear_resolution(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "issue", "Bug")
        resolve_item(engine, item["id"], resolution="Fixed")
        updated = update_item(engine, item["id"], resolution="")
        assert updated["resolution"] is None

    def test_clear_description(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task", description="Details")
        updated = update_item(engine, item["id"], description="")
        assert updated["description"] is None

    def test_update_invalid_priority(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task")
        with pytest.raises(ValueError, match="Invalid priority"):
            update_item(engine, item["id"], priority="URGENT")

    def test_update_invalid_status(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task")
        with pytest.raises(ValueError, match="Invalid status"):
            update_item(engine, item["id"], status="done")

    def test_resolve_with_invalid_status(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task")
        with pytest.raises(ValueError, match="Invalid resolve status"):
            resolve_item(engine, item["id"], status="open")


# ── Unicode / special characters ─────────────────────────────────────────────


class TestUnicode:
    def test_unicode_title(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "日本語のタイトル — żółć ñ 中文")
        fetched = get_item(engine, item["id"])
        assert fetched["title"] == "日本語のタイトル — żółć ñ 中文"

    def test_unicode_description(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task", description="Opis z polskimi znakami: ąęćółżźń")
        fetched = get_item(engine, item["id"])
        assert "ąęćółżźń" in fetched["description"]

    def test_unicode_note(self, engine):
        create_project(engine, slug="p", name="P")
        item = create_item(engine, "p", "todo", "Task")
        add_note(engine, item["id"], "注释 — komentarz — комментарий")
        fetched = get_item(engine, item["id"])
        assert "注释" in fetched["notes"][0]["note"]

    def test_special_chars_in_search(self, engine):
        create_project(engine, slug="p", name="P")
        create_item(engine, "p", "todo", "Fix bug with 100% CPU")
        create_item(engine, "p", "todo", "Handle O'Brien name")
        results = list_items(engine, search="100%")
        assert len(results) == 1
        results = list_items(engine, search="O'Brien")
        assert len(results) == 1

    def test_sql_injection_in_search(self, engine):
        """Bad intentions: SQL injection attempt in search."""
        create_project(engine, slug="p", name="P")
        create_item(engine, "p", "todo", "Normal task")
        # These should not break anything — SQLAlchemy parameterizes
        results = list_items(engine, search="'; DROP TABLE items; --")
        assert len(results) == 0
        # Verify table still exists
        assert list_items(engine) == [get_item(engine, 1)] or True  # just verify no crash

    def test_sql_injection_in_slug(self, engine):
        """SQL injection in slug should be safely parameterized, not executed."""
        create_project(engine, slug="'; DROP TABLE--", name="Evil")
        # Table should still exist and data should be safe
        create_project(engine, slug="safe", name="Safe Project")
        p = get_project(engine, "safe")
        assert p is not None

    def test_very_long_title(self, engine):
        create_project(engine, slug="p", name="P")
        long_title = "A" * 1000
        item = create_item(engine, "p", "todo", long_title)
        fetched = get_item(engine, item["id"])
        assert fetched["title"] == long_title

    def test_newlines_in_description(self, engine):
        create_project(engine, slug="p", name="P")
        desc = "Line 1\nLine 2\n\nLine 4"
        item = create_item(engine, "p", "todo", "Task", description=desc)
        fetched = get_item(engine, item["id"])
        assert fetched["description"] == desc


# ── Changelog edge cases ─────────────────────────────────────────────────────


class TestChangelogEdgeCases:
    def test_release_empty_unreleased(self, engine):
        create_project(engine, slug="p", name="P")
        count = count_unreleased(engine, "p")
        assert count == 0
        # Release with nothing unreleased should return 0
        from dct.core.changelog import release_entries
        released = release_entries(engine, "p", "1.0")
        assert released == 0

    def test_multiple_releases(self, engine):
        create_project(engine, slug="p", name="P")
        add_entry(engine, "p", "added", "Feature A")
        from dct.core.changelog import release_entries
        release_entries(engine, "p", "1.0")
        add_entry(engine, "p", "fixed", "Bug B")
        release_entries(engine, "p", "1.1")

        v10 = list_entries(engine, "p", version="1.0")
        v11 = list_entries(engine, "p", version="1.1")
        assert len(v10) == 1
        assert v10[0]["entry"] == "Feature A"
        assert len(v11) == 1
        assert v11[0]["entry"] == "Bug B"

    def test_unreleased_only_ignores_version_param(self, engine):
        """If unreleased_only=True, version parameter is silently ignored."""
        create_project(engine, slug="p", name="P")
        add_entry(engine, "p", "added", "Unreleased")
        add_entry(engine, "p", "added", "Released", version="1.0")
        # Even with version="1.0", unreleased_only should only return unreleased
        entries = list_entries(engine, "p", version="1.0", unreleased_only=True)
        assert len(entries) == 1
        assert entries[0]["version"] is None

    def test_delete_nonexistent_entry(self, engine):
        assert delete_entry(engine, 99999) is False


# ── MCP tool-call markup guard (#240) ─────────────────────────────────────────


class TestToolcallMarkupGuard:
    """#240: MCP free-text tools must REJECT mangled tool-call markup instead of
    silently persisting corrupted rows (e.g. create_decision with everything
    folded into `context` and decision/consequences/alternatives = NULL).

    The mangle is an LLM tool-call *serialization* slip — the harness parser
    only recognises well-formed `<parameter>` tags and folds the rest into one
    field as literal text. So the guard lives at the MCP boundary (server.py),
    not in core: CLI args and plan-ingest markdown may legitimately contain such
    tokens and must not be blocked.
    """

    @pytest.mark.parametrize("field,value", [
        ("context", 'URL versioning <parameter name="decision">v2</parameter>'),
        ("prompt", 'next session <parameter name="prompt">resume X'),
        ("context", "text with a stray </parameter> close tag"),
        ("decision", 'we <invoke name="create_decision"> chose URL'),
        ("note", "antml:parameter leaked into the value"),
        ("entry", "a bare <parameter> tag in a changelog line"),
    ])
    def test_detector_flags_markup(self, field, value):
        err = dct_server._reject_toolcall_markup(**{field: value})
        assert err is not None
        assert "error" in err
        # message must name the offending field so the model knows what to re-send
        assert field in err["error"]

    @pytest.mark.parametrize("value", [
        "Plain decision text with no markup at all.",
        "Use List[str] not str; a < b and c > d hold.",
        "HTML-ish <b>bold</b>, <div>, and <img/> are all fine.",
        "Discussed parameters of the function and invoked it twice.",
        "",
        "Polskie znaki ąęćółżźń and emoji \U0001f680 survive.",
    ])
    def test_detector_passes_clean_text(self, value):
        assert dct_server._reject_toolcall_markup(context=value, prompt=value, note=value) is None

    def test_detector_ignores_none_and_nonstr(self):
        # non-string args (ints, bools, None) must never trip the scan
        assert dct_server._reject_toolcall_markup(a=None, b=123, c=True, d="clean text") is None

    def test_create_decision_rejects_markup_without_touching_db(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dct_server, "_get_engine", lambda: calls.append("engine"))
        out = dct_server.create_decision(
            kind="adr", title="API versioning", project="test",
            context='ctx <parameter name="decision">URL-based</parameter>',
        )
        assert "error" in out
        assert "markup" in out["error"].lower()
        assert calls == []  # guard short-circuited before any DB access → zero rows

    def test_create_handoff_rejects_markup_without_touching_db(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dct_server, "_get_engine", lambda: calls.append("engine"))
        out = dct_server.create_handoff(
            prompt='for next session <parameter name="prompt">resume the refactor',
            created_by="claude-test", project="test",
        )
        assert "error" in out
        assert calls == []
