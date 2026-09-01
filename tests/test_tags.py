"""Core tag dictionary + item_tags linkage (#611).

TDD RED: `dct.core.tags` does not exist yet — this file states the contract the
implementation must satisfy. Three layers, in the order the plan builds them:

1. Pure functions — `normalize_tag_name` and the similarity guard vector table.
2. Dictionary ops — `add_tag` (idempotence, reuse, refusal + force,
   resurrection) and `list_tags` (`item_count` exclusions).
3. Item linkage — create/update/get with tags, diff-and-sync, rollback — plus
   `merge_tags`.

Naming hazard (plan §Core): the module-level SQLAlchemy Table is imported as
`tags_table`; every list-of-names parameter is called `names`.
"""

from datetime import UTC, datetime
from difflib import SequenceMatcher

import pytest
import sqlalchemy as sa

from dct.core.items import create_item, get_item, list_items, update_item
from dct.core.projects import create_project
from dct.core.tags import (
    MAX_TAG_LENGTH,
    SIMILARITY_THRESHOLD,
    add_tag,
    canonicalize_tag_name,
    list_tags,
    merge_tags,
    normalize_tag_name,
    similar_tag_names,
)
from dct.db import item_tags
from dct.db import tags as tags_table


@pytest.fixture
def project(engine):
    return create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")


# ── helpers ───────────────────────────────────────────────────────────────────


def _dict_rows(engine, name: str | None = None) -> list[dict]:
    """Raw rows of the tags dictionary — soft-deleted ones INCLUDED (the ledger
    is what several invariants below are about)."""
    q = sa.select(tags_table)
    if name is not None:
        q = q.where(tags_table.c.name == name)
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(q.order_by(tags_table.c.id))]


def _link_rows(engine, item_id: int | None = None) -> list[dict]:
    """Raw item_tags rows, soft-removed ones included."""
    q = sa.select(item_tags)
    if item_id is not None:
        q = q.where(item_tags.c.item_id == item_id)
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


def _soft_delete_tag(engine, tag_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            tags_table.update()
            .where(tags_table.c.id == tag_id)
            .values(deleted_at=datetime.now(UTC))
        )


def _soft_delete_item(engine, item_id: int) -> None:
    from dct.db import items

    with engine.begin() as conn:
        conn.execute(
            items.update().where(items.c.id == item_id).values(deleted_at=datetime.now(UTC))
        )


def _count_by_name(engine) -> dict[str, int]:
    return {t["name"]: t["item_count"] for t in list_tags(engine)}


# ── 1. normalize_tag_name ─────────────────────────────────────────────────────


class TestNormalizeTagName:
    """Contract: str in → canonical str out (lowercase, whitespace collapsed),
    or a loud raise. Never a silent drop — a caller that sent garbage must hear
    about it (the dictionary is the thing that stops fragmentation)."""

    @pytest.mark.parametrize("raw,expected", [
        ("perf", "perf"),
        ("Perf", "perf"),
        ("PERF", "perf"),
        ("  perf  ", "perf"),
        ("\tperf\n", "perf"),
        ("multi   word", "multi word"),
        ("multi\tword", "multi word"),
        ("multi\nword", "multi word"),
        ("  Multi   Word  ", "multi word"),
        ("ŻÓŁĆ", "żółć"),
        ("Ünïcödé Tag", "ünïcödé tag"),
        ("日本語", "日本語"),          # no case mapping — must survive untouched
        ("cc-task-list", "cc-task-list"),
        ("v1.5", "v1.5"),
    ])
    def test_normalizes(self, raw, expected):
        assert normalize_tag_name(raw) == expected

    def test_is_idempotent(self):
        """Property: normalize(normalize(x)) == normalize(x) for every accepted
        input — otherwise a round-trip through the DB could drift."""
        for raw in ("  Perf ", "MULTI   WORD", "ŻÓŁĆ", "日本語"):
            once = normalize_tag_name(raw)
            assert normalize_tag_name(once) == once

    @pytest.mark.parametrize("raw", ["", "   ", "\t", "\n", " \t\n "])
    def test_rejects_empty_and_whitespace_only(self, raw):
        with pytest.raises(ValueError):
            normalize_tag_name(raw)

    @pytest.mark.parametrize("raw", ["a,b", "perf,", ",perf", "a, b, c"])
    def test_rejects_comma(self, raw):
        """A comma is the CSV separator at the CLI boundary — a tag containing
        one could never round-trip through `--tags a,b`."""
        with pytest.raises(ValueError, match="comma"):
            normalize_tag_name(raw)

    def test_accepts_exactly_max_length(self):
        name = "a" * MAX_TAG_LENGTH
        assert normalize_tag_name(name) == name

    def test_rejects_one_over_max_length(self):
        with pytest.raises(ValueError):
            normalize_tag_name("a" * (MAX_TAG_LENGTH + 1))

    def test_length_is_measured_after_normalization(self):
        """Off-by-one at the boundary: padding must not push an otherwise legal
        name over the limit — normalize first, then measure."""
        padded = "  " + ("a" * MAX_TAG_LENGTH) + "  "
        assert len(padded) > MAX_TAG_LENGTH
        assert normalize_tag_name(padded) == "a" * MAX_TAG_LENGTH

    @pytest.mark.parametrize("raw", [123, None, ["perf"], {"perf": 1}, 1.5, True])
    def test_rejects_non_str(self, raw):
        """#31 contract preserved: a non-str element means the caller sent
        something other than list[str], and that is an AssertionError naming
        the list contract — not a ValueError about the tag itself."""
        with pytest.raises(AssertionError, match="tags must be list"):
            normalize_tag_name(raw)  # type: ignore[arg-type]


# ── 1b. similarity guard — the vector table from the plan ─────────────────────

# (candidate, existing, blocked?) — the ratios in the comments are
# SequenceMatcher(None, *sorted(pair), autojunk=False).ratio(). This table IS
# the guard's specification: it pins both what must be caught and what must be
# let through, so a later threshold tweak cannot quietly change either side.
GUARD_VECTORS = [
    ("test", "tests", True),          # .889
    ("tag", "tags", True),            # .857
    ("ui", "uis", True),              # .800 — under threshold, plural rule
    ("security", "securty", True),    # .933 typo
    ("auth", "oauth", True),          # .889 — accepted false positive
    ("refactor", "refactoring", False),  # .842 — just under the threshold
    ("test", "text", False),          # .750
    ("test", "testing", False),       # .727
    ("perf", "performance", False),   # .533
    ("db", "database", False),        # .400
]


class TestSimilarityGuard:
    """`similar_tag_names(name, names)` returns the existing names considered
    confusable with `name` (top-3, ranked by -ratio then name). Empty list = the
    name is free to register."""

    @pytest.mark.parametrize("candidate,existing,blocked", GUARD_VECTORS)
    def test_vector_table(self, candidate, existing, blocked):
        assert bool(similar_tag_names(candidate, [existing])) is blocked

    @pytest.mark.parametrize("candidate,existing,blocked", GUARD_VECTORS)
    def test_guard_is_symmetric(self, candidate, existing, blocked):
        """`sorted()` inside the ratio call is what makes this hold — without it
        SequenceMatcher is order-sensitive and 'test' vs 'tests' would decide
        differently depending on which one was registered first."""
        assert bool(similar_tag_names(existing, [candidate])) is blocked

    def test_ui_uis_is_caught_only_by_the_plural_rule(self):
        """.800 is BELOW the ratio threshold — if the plural rule were dropped
        this pair would silently fragment the dictionary."""
        ratio = SequenceMatcher(None, "uis", "ui", autojunk=False).ratio()
        assert ratio < SIMILARITY_THRESHOLD
        assert similar_tag_names("ui", ["uis"]) == ["uis"]

    def test_refactoring_is_below_threshold_by_design(self):
        ratio = SequenceMatcher(None, "refactor", "refactoring", autojunk=False).ratio()
        assert ratio < SIMILARITY_THRESHOLD
        assert similar_tag_names("refactor", ["refactoring"]) == []

    def test_empty_dictionary_blocks_nothing(self):
        assert similar_tag_names("test", []) == []

    def test_unrelated_names_pass(self):
        assert similar_tag_names("perf", ["db", "docs", "ui", "security"]) == []

    def test_returns_at_most_three_ranked_suggestions(self):
        names = ["tests", "test1", "testy", "teste", "tesst"]
        out = similar_tag_names("test", names)
        assert len(out) == 3
        ranked = sorted(
            names,
            key=lambda n: (-SequenceMatcher(None, *sorted(("test", n)), autojunk=False).ratio(), n),
        )
        assert out == ranked[:3]

    def test_normalizes_the_candidate_before_comparing(self):
        assert similar_tag_names(" TESTS ", ["test"]) == ["test"]


# ── 2. dictionary ops: add_tag ────────────────────────────────────────────────


class TestAddTag:
    def test_creates_and_reports_created(self, engine):
        t = add_tag(engine, "perf")
        assert t["name"] == "perf"
        assert t["created"] is True
        assert isinstance(t["id"], int)

    def test_exact_re_add_is_idempotent(self, engine):
        first = add_tag(engine, "perf")
        second = add_tag(engine, "perf")
        assert second["id"] == first["id"]
        assert second["created"] is False
        assert len(_dict_rows(engine, "perf")) == 1

    def test_normalizes_before_lookup_so_variants_reuse_one_row(self, engine):
        first = add_tag(engine, "perf")
        reused = add_tag(engine, " Perf ")
        assert reused["id"] == first["id"]
        assert reused["name"] == "perf"
        assert reused["created"] is False
        assert len(_dict_rows(engine)) == 1

    def test_similar_name_is_refused_with_an_actionable_message(self, engine):
        add_tag(engine, "test")
        with pytest.raises(ValueError) as exc:
            add_tag(engine, "tests")
        msg = str(exc.value)
        assert "similar" in msg.lower()
        assert "'test'" in msg          # the existing name to reuse
        assert "force=true" in msg      # the escape hatch, verbatim
        assert len(_dict_rows(engine)) == 1  # refusal wrote nothing

    def test_force_creates_the_similar_name(self, engine):
        first = add_tag(engine, "test")
        forced = add_tag(engine, "tests", force=True)
        assert forced["created"] is True
        assert forced["id"] != first["id"]
        assert {t["name"] for t in list_tags(engine)} == {"test", "tests"}

    def test_force_does_not_duplicate_an_exact_existing_name(self, engine):
        """force overrides the SIMILARITY guard, not the UNIQUE dictionary —
        an exact re-add still reuses the row."""
        first = add_tag(engine, "perf")
        again = add_tag(engine, "perf", force=True)
        assert again["id"] == first["id"]
        assert len(_dict_rows(engine, "perf")) == 1

    def test_rejects_invalid_names(self, engine):
        for bad in ("", "   ", "a,b", "a" * (MAX_TAG_LENGTH + 1)):
            with pytest.raises(ValueError):
                add_tag(engine, bad)
        assert _dict_rows(engine) == []

    def test_stores_description(self, engine):
        add_tag(engine, "perf", description="performance work")
        row = next(t for t in list_tags(engine) if t["name"] == "perf")
        assert row["description"] == "performance work"

    def test_resurrects_a_soft_deleted_tag_with_a_stable_id(self, engine, project):
        """Soft delete is a ledger operation: re-adding the same name must revive
        the SAME row (stable id) so every historical link points at it again."""
        item = create_item(engine, "proj-a", "todo", "Tagged", tags=["perf"])
        original_id = add_tag(engine, "perf")["id"]
        _soft_delete_tag(engine, original_id)
        assert get_item(engine, item["id"])["tags"] == []  # hidden while deleted

        revived = add_tag(engine, "perf")

        assert revived["id"] == original_id
        assert len(_dict_rows(engine, "perf")) == 1
        assert _dict_rows(engine, "perf")[0]["deleted_at"] is None
        assert get_item(engine, item["id"])["tags"] == ["perf"]  # links revive

    def test_similarity_guard_ignores_soft_deleted_names(self, engine):
        """A retired name must not block a new one — the guard is about names in
        use, and the retired row will be resurrected on exact re-add anyway."""
        dead = add_tag(engine, "test")["id"]
        _soft_delete_tag(engine, dead)
        created = add_tag(engine, "tests")
        assert created["created"] is True


class TestListTags:
    def test_lists_alphabetically_with_counts(self, engine, project):
        create_item(engine, "proj-a", "todo", "A", tags=["perf", "db"])
        create_item(engine, "proj-a", "todo", "B", tags=["perf"])
        rows = list_tags(engine)
        assert [r["name"] for r in rows] == ["db", "perf"]
        assert _count_by_name(engine) == {"db": 1, "perf": 2}

    def test_unused_tag_is_visible_with_zero_count(self, engine):
        """K-14: a registered-but-unused tag must still show up — otherwise the
        dictionary silently hides names that will still trip the guard."""
        add_tag(engine, "orphan")
        rows = list_tags(engine)
        assert [r["name"] for r in rows] == ["orphan"]
        assert rows[0]["item_count"] == 0

    def test_count_excludes_soft_removed_links(self, engine, project):
        a = create_item(engine, "proj-a", "todo", "A", tags=["perf"])
        create_item(engine, "proj-a", "todo", "B", tags=["perf"])
        update_item(engine, a["id"], tags=[])  # soft-removes a's link
        assert _count_by_name(engine) == {"perf": 1}

    def test_count_excludes_soft_deleted_items(self, engine, project):
        a = create_item(engine, "proj-a", "todo", "A", tags=["perf"])
        create_item(engine, "proj-a", "todo", "B", tags=["perf"])
        _soft_delete_item(engine, a["id"])
        assert _count_by_name(engine) == {"perf": 1}

    def test_soft_deleted_tags_are_not_listed(self, engine, project):
        create_item(engine, "proj-a", "todo", "A", tags=["perf", "db"])
        _soft_delete_tag(engine, add_tag(engine, "db")["id"])
        assert [r["name"] for r in list_tags(engine)] == ["perf"]

    def test_counts_are_global_across_projects(self, engine, project):
        """Plan decision 5: the dictionary is user-scoped, so item_count spans
        every project even though item LISTS are project-scoped."""
        create_project(engine, slug="proj-b", name="Project B", path="/tmp/proj-b")
        create_item(engine, "proj-a", "todo", "A", tags=["perf"])
        create_item(engine, "proj-b", "todo", "B", tags=["perf"])
        assert _count_by_name(engine) == {"perf": 2}


# ── 3. item linkage ───────────────────────────────────────────────────────────


class TestItemTagLinkage:
    def test_create_with_tags_returns_name_ascending(self, engine, project):
        """Plan decision 3: name-ASC everywhere, NOT insertion order."""
        item = create_item(engine, "proj-a", "feature", "Add caching", tags=["perf", "arch"])
        assert item["tags"] == ["arch", "perf"]
        assert get_item(engine, item["id"])["tags"] == ["arch", "perf"]

    def test_create_normalizes_and_dedupes(self, engine, project):
        item = create_item(engine, "proj-a", "todo", "T", tags=[" Perf ", "PERF", "perf"])
        assert item["tags"] == ["perf"]
        assert len(_link_rows(engine, item["id"])) == 1

    def test_create_with_string_tags_still_raises(self, engine, project):
        """#31 contract survives the rewrite — CSV splitting stays at the CLI
        boundary, core is list-only."""
        with pytest.raises(AssertionError, match="tags must be list"):
            create_item(engine, "proj-a", "feature", "X", tags="debug,observability")  # type: ignore[arg-type]

    def test_create_with_a_refused_tag_creates_no_item(self, engine, project):
        """Atomicity: the guard fires INSIDE the create transaction, so a refused
        tag must leave neither an orphan item nor a dictionary row."""
        add_tag(engine, "test")
        before = len(list_items(engine, project_slug="proj-a"))

        with pytest.raises(ValueError, match="force=true"):
            create_item(engine, "proj-a", "todo", "Should not exist", tags=["tests"])

        assert len(list_items(engine, project_slug="proj-a")) == before
        assert [t["name"] for t in list_tags(engine)] == ["test"]
        assert _link_rows(engine) == []

    def test_batch_is_processed_in_input_order(self, engine, project):
        """K-12: ["test","tests"] in ONE call — 'test' registers first, so
        'tests' is the name that gets refused (deterministic, not hash order).
        The whole call then rolls back."""
        with pytest.raises(ValueError) as exc:
            create_item(engine, "proj-a", "todo", "Batch", tags=["test", "tests"])
        assert "'tests'" in str(exc.value)  # the REFUSED name is named
        assert _dict_rows(engine) == []     # rolled back — 'test' is gone too

        with pytest.raises(ValueError) as exc2:
            create_item(engine, "proj-a", "todo", "Batch", tags=["tests", "test"])
        assert "'test'" in str(exc2.value)
        assert _dict_rows(engine) == []

    def test_update_diff_and_sync(self, engine, project):
        item = create_item(engine, "proj-a", "todo", "T", tags=["perf", "db"])
        updated = update_item(engine, item["id"], tags=["perf", "ui"])
        assert updated["tags"] == ["perf", "ui"]
        # db link soft-removed, not deleted (ledger)
        db_id = add_tag(engine, "db")["id"]
        db_link = next(r for r in _link_rows(engine, item["id"]) if r["tag_id"] == db_id)
        assert db_link["removed_at"] is not None

    def test_tags_only_update_is_a_valid_update(self, engine, project):
        item = create_item(engine, "proj-a", "todo", "T")
        updated = update_item(engine, item["id"], tags=["perf"])
        assert updated is not None
        assert updated["tags"] == ["perf"]

    def test_reactivating_a_removed_pair_keeps_exactly_one_row(self, engine, project):
        """The link table has a composite PK — re-adding a removed tag MUST
        reactivate the existing row, never INSERT a second one (lesson #215)."""
        item = create_item(engine, "proj-a", "todo", "T", tags=["perf"])
        update_item(engine, item["id"], tags=[])
        again = update_item(engine, item["id"], tags=["perf"])

        assert again["tags"] == ["perf"]
        rows = _link_rows(engine, item["id"])
        assert len(rows) == 1
        assert rows[0]["removed_at"] is None

    def test_empty_list_clears_tags(self, engine, project):
        item = create_item(engine, "proj-a", "todo", "T", tags=["perf", "db"])
        cleared = update_item(engine, item["id"], tags=[])
        assert cleared["tags"] == []
        assert get_item(engine, item["id"])["tags"] == []

    def test_title_only_update_leaves_tags_alone(self, engine, project):
        item = create_item(engine, "proj-a", "todo", "T", tags=["perf"])
        renamed = update_item(engine, item["id"], title="Renamed")
        assert renamed["title"] == "Renamed"
        assert renamed["tags"] == ["perf"]

    def test_explicit_none_means_no_change(self, engine, project):
        """K-16 sentinel: absent and explicit None both mean 'leave tags alone';
        only [] clears. MCP clients send None for 'omitted'."""
        item = create_item(engine, "proj-a", "todo", "T", tags=["perf"])
        updated = update_item(engine, item["id"], title="Renamed", tags=None)
        assert updated["tags"] == ["perf"]

    def test_update_of_a_missing_item_leaves_no_dictionary_rows(self, engine, project):
        """K-11: the existence guard runs BEFORE any tag resolution, so a typo'd
        item id can never register tags as a side effect."""
        assert update_item(engine, 99999, tags=["ghosttag"]) is None
        assert _dict_rows(engine) == []
        assert _link_rows(engine) == []

    def test_update_with_a_refused_tag_leaves_the_item_untouched(self, engine, project):
        add_tag(engine, "test")
        item = create_item(engine, "proj-a", "todo", "T", tags=["perf"])
        with pytest.raises(ValueError, match="force=true"):
            update_item(engine, item["id"], title="Renamed", tags=["tests"])
        after = get_item(engine, item["id"])
        assert after["title"] == "T"
        assert after["tags"] == ["perf"]

    def test_tag_change_stamps_updated_at(self, engine, project):
        item = create_item(engine, "proj-a", "todo", "T")
        updated = update_item(engine, item["id"], tags=["perf"])
        assert updated["updated_at"] >= item["updated_at"]


# ── 4. merge_tags ─────────────────────────────────────────────────────────────


class TestMergeTags:
    def test_moves_active_links_and_retires_the_source(self, engine, project):
        a = create_item(engine, "proj-a", "todo", "A", tags=["tests"])
        b = create_item(engine, "proj-a", "todo", "B", tags=["tests"])
        add_tag(engine, "test", force=True)

        out = merge_tags(engine, "tests", "test")

        assert out == {"from": "tests", "into": "test", "items_moved": 2}
        assert get_item(engine, a["id"])["tags"] == ["test"]
        assert get_item(engine, b["id"])["tags"] == ["test"]
        assert [t["name"] for t in list_tags(engine)] == ["test"]

    def test_overlapping_link_is_deduped_to_one_row(self, engine, project):
        """An item already carrying BOTH tags must end with exactly one active
        link, not a duplicate PK collision."""
        add_tag(engine, "test")
        item = create_item(engine, "proj-a", "todo", "Both", tags=["test"])
        add_tag(engine, "tests", force=True)
        update_item(engine, item["id"], tags=["test", "tests"])

        out = merge_tags(engine, "tests", "test")

        assert get_item(engine, item["id"])["tags"] == ["test"]
        into_id = add_tag(engine, "test")["id"]
        active = [r for r in _link_rows(engine, item["id"])
                  if r["removed_at"] is None]
        assert len(active) == 1 and active[0]["tag_id"] == into_id
        assert out["items_moved"] == 1

    def test_source_row_is_soft_deleted_not_erased(self, engine, project):
        add_tag(engine, "test")
        add_tag(engine, "tests", force=True)
        merge_tags(engine, "tests", "test")
        rows = _dict_rows(engine, "tests")
        assert len(rows) == 1
        assert rows[0]["deleted_at"] is not None

    def test_historical_links_of_the_source_are_untouched(self, engine, project):
        """Ledger: a link the user removed earlier stays removed with its own
        timestamp — a merge is not a licence to rewrite history."""
        item = create_item(engine, "proj-a", "todo", "Old", tags=["tests"])
        add_tag(engine, "test", force=True)
        update_item(engine, item["id"], tags=[])  # historical, soft-removed
        removed_at = _link_rows(engine, item["id"])[0]["removed_at"]
        assert removed_at is not None

        merge_tags(engine, "tests", "test")

        rows = _link_rows(engine, item["id"])
        assert len(rows) == 1
        assert rows[0]["removed_at"] == removed_at  # not moved, not revived

    def test_item_count_after_merge(self, engine, project):
        create_item(engine, "proj-a", "todo", "A", tags=["tests"])
        add_tag(engine, "test", force=True)
        create_item(engine, "proj-a", "todo", "B", tags=["test"])
        merge_tags(engine, "tests", "test")
        assert _count_by_name(engine) == {"test": 2}

    def test_normalizes_both_names(self, engine, project):
        create_item(engine, "proj-a", "todo", "A", tags=["tests"])
        add_tag(engine, "test", force=True)
        out = merge_tags(engine, "  TESTS ", "Test")
        assert out["from"] == "tests" and out["into"] == "test"

    def test_self_merge_raises(self, engine):
        add_tag(engine, "perf")
        with pytest.raises(ValueError):
            merge_tags(engine, "perf", "perf")
        with pytest.raises(ValueError):
            merge_tags(engine, " Perf ", "perf")  # same tag after normalization

    @pytest.mark.parametrize("from_name,into_name", [
        ("ghost", "perf"),   # unknown source
        ("perf", "ghost"),   # unknown target
    ])
    def test_unknown_tag_raises_naming_the_known_ones(self, engine, from_name, into_name):
        """Closed dictionary, `resolve_project_id` style: the error names the
        unknown tag AND points at what does exist."""
        add_tag(engine, "perf")
        with pytest.raises(ValueError) as exc:
            merge_tags(engine, from_name, into_name)
        msg = str(exc.value)
        assert "ghost" in msg and "perf" in msg

    def test_soft_deleted_tag_is_not_a_valid_merge_end(self, engine):
        add_tag(engine, "perf")
        dead = add_tag(engine, "docs")["id"]
        _soft_delete_tag(engine, dead)
        with pytest.raises(ValueError):
            merge_tags(engine, "docs", "perf")
        with pytest.raises(ValueError):
            merge_tags(engine, "perf", "docs")

    def test_merge_is_not_silently_reversible(self, engine, project):
        """Documented one-way property: resurrecting the source name brings the
        dictionary row back, but the moved links stay on the target."""
        item = create_item(engine, "proj-a", "todo", "A", tags=["tests"])
        add_tag(engine, "test", force=True)
        merge_tags(engine, "tests", "test")

        add_tag(engine, "tests", force=True)  # resurrection

        assert get_item(engine, item["id"])["tags"] == ["test"]
        assert _count_by_name(engine) == {"test": 1, "tests": 0}


# ── 5. K-10: concurrent registration race ─────────────────────────────────────


class TestConcurrentRegistrationRace:
    """The retry-upsert in `_add_tag_conn`: a competing writer wins the race on
    the partial UNIQUE index between our existence probe and our INSERT.

    The interleaving point is the ONLY seam patched — the competing row is
    written with plain SQL, the IntegrityError is raised by the real index, and
    the assertions are about the public contract of `add_tag` / `create_item`
    (reuse the winner's row) rather than about how the retry is coded.
    """

    @staticmethod
    def _race(monkeypatch, name: str, *, description: str | None = None) -> list[int]:
        """Arm one competing INSERT of `name`, committed just before our own.

        Returns a one-element list that receives the competing row's id (the
        list is filled when the race actually fires).
        """
        from dct.core import tags as tags_module

        winner: list[int] = []
        original = tags_module._active_names_conn

        def interleave(conn):
            if not winner:
                row = conn.execute(
                    tags_table.insert()
                    .values(name=name, description=description)
                    .returning(tags_table.c.id)
                ).first()
                winner.append(row.id)
            return original(conn)

        monkeypatch.setattr(tags_module, "_active_names_conn", interleave)
        return winner

    def test_add_tag_reuses_the_row_the_competing_writer_created(
        self, engine, monkeypatch
    ):
        winner = self._race(monkeypatch, "perf")

        out = add_tag(engine, "perf")

        assert winner, "the competing INSERT never fired — test seam is wrong"
        assert out["id"] == winner[0]
        assert out["name"] == "perf"
        assert out["created"] is False  # we lost the race and said so
        assert len(_dict_rows(engine, "perf")) == 1  # no duplicate row

    def test_race_does_not_poison_the_surrounding_transaction(
        self, engine, project, monkeypatch
    ):
        """The SAVEPOINT is what keeps the failed INSERT local: the item write
        that triggered the tag registration must still commit, linked to the
        competing writer's row."""
        winner = self._race(monkeypatch, "perf")

        item = create_item(engine, "proj-a", "todo", "Raced", tags=["perf"])

        assert get_item(engine, item["id"])["tags"] == ["perf"]
        active = [r for r in _link_rows(engine, item["id"]) if r["removed_at"] is None]
        assert len(active) == 1 and active[0]["tag_id"] == winner[0]
        assert _count_by_name(engine) == {"perf": 1}

    def test_race_is_transparent_to_the_normalized_name(self, engine, monkeypatch):
        """The competing writer stores the canonical form; a caller passing a
        dirty spelling of the same tag still lands on that one row."""
        winner = self._race(monkeypatch, "code review")

        out = add_tag(engine, "  Code   REVIEW ")

        assert out == {"id": winner[0], "name": "code review", "created": False}
        assert len(_dict_rows(engine)) == 1

    def test_integrity_error_without_a_competing_row_propagates(
        self, engine, monkeypatch
    ):
        """The retry re-PROBES rather than swallowing: an IntegrityError that no
        active row explains must surface, not be reported as a silent reuse."""
        from dct.core import tags as tags_module

        def boom():
            raise sa.exc.IntegrityError("INSERT INTO tags", {}, Exception("nope"))

        monkeypatch.setattr(tags_module.tags_table, "insert", boom)
        with pytest.raises(sa.exc.IntegrityError):
            add_tag(engine, "perf")
        assert _dict_rows(engine) == []


# ── 6. K-28 / K-36: canonicalize vs normalize ─────────────────────────────────


class TestCanonicalizeTagName:
    """`canonicalize_tag_name` is FORM only — the function every lookup uses.

    The split exists because the dictionary may legally hold names the creation
    rules would refuse today (legacy names backfilled from the frozen
    `items.tags` JSON, K-7). Canonicalizing must never raise on content, and
    must never differ from `normalize_tag_name` for a name that IS registrable.
    """

    @pytest.mark.parametrize("raw,expected", [
        ("Perf", "perf"),
        ("  multi   word ", "multi word"),
        ("multi\tword", "multi word"),          # whitespace controls separate
        ("ŻÓŁĆ", "żółć"),
        ("日本語", "日本語"),
    ])
    def test_agrees_with_normalize_on_registrable_names(self, raw, expected):
        assert canonicalize_tag_name(raw) == expected
        assert normalize_tag_name(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("a,b", "a,b"),                          # comma: refused by normalize
        ("  A , B  ", "a , b"),
        ("x" * (MAX_TAG_LENGTH + 10), "x" * (MAX_TAG_LENGTH + 10)),
        ("", ""),                                # empty: legal answer here
        ("   ", ""),
    ])
    def test_accepts_what_the_creation_rules_refuse(self, raw, expected):
        assert canonicalize_tag_name(raw) == expected
        with pytest.raises(ValueError):
            normalize_tag_name(raw)

    def test_is_idempotent(self):
        for raw in ("  A , B ", "\x1bPerf", "x" * 80, "日本語"):
            once = canonicalize_tag_name(raw)
            assert canonicalize_tag_name(once) == once

    @pytest.mark.parametrize("raw", [None, 3, ["perf"]])
    def test_rejects_non_str(self, raw):
        with pytest.raises(AssertionError, match="list\\[str\\]"):
            canonicalize_tag_name(raw)  # type: ignore[arg-type]

    # K-36 — C0 control characters are never part of a tag name.
    @pytest.mark.parametrize("raw,expected", [
        ("\x1bperf", "perf"),
        ("per\x07f", "perf"),
        ("perf\x7f", "perf"),
        ("\x00perf\x1f", "perf"),
        ("\x1b\x1bperf\x1b", "perf"),
    ])
    def test_strips_c0_controls(self, raw, expected):
        assert canonicalize_tag_name(raw) == expected
        assert normalize_tag_name(raw) == expected

    def test_a_control_only_name_is_still_empty(self):
        assert canonicalize_tag_name("\x1b\x00\x7f") == ""
        with pytest.raises(ValueError, match="empty"):
            normalize_tag_name("\x1b\x00\x7f")

    def test_esc_sequences_never_reach_the_stored_name(self, engine):
        """Adversarial: a name pasted from a colourized terminal. The escape
        bytes are gone from the row — what is left is inert printable text."""
        add_tag(engine, "\x1b[31mperf\x1b[0m")
        stored = [r["name"] for r in _dict_rows(engine)]
        assert stored == ["[31mperf[0m"]
        assert all(ord(ch) >= 0x20 and ch != "\x7f" for ch in stored[0])


class TestLegacyNamesStayUsable:
    """K-28 regression: a dictionary row that breaks today's creation rules —
    a comma, or more than MAX_TAG_LENGTH characters — is still a first-class
    citizen for every READ and ADMIN path. Such rows exist in the wild: the
    #611 backfill imports legacy names verbatim (K-7).
    """

    LONG = "l" + "e" * (MAX_TAG_LENGTH + 10) + "gacy"

    @staticmethod
    def _raw_tag(engine, name: str) -> int:
        """Insert a dictionary row bypassing the API rules (the backfill's
        privilege) — normalize_tag_name would refuse these names."""
        with engine.begin() as conn:
            return conn.execute(
                tags_table.insert().values(name=name).returning(tags_table.c.id)
            ).scalar()

    @staticmethod
    def _raw_link(engine, item_id: int, tag_id: int) -> None:
        with engine.begin() as conn:
            conn.execute(
                item_tags.insert().values(
                    item_id=item_id, tag_id=tag_id, assigned_at=datetime.now(UTC)
                )
            )

    def _legacy_item(self, engine, name: str) -> tuple[int, int]:
        item = create_item(engine, "proj-a", "todo", "Legacy")
        tag_id = self._raw_tag(engine, name)
        self._raw_link(engine, item["id"], tag_id)
        return item["id"], tag_id

    def test_comma_name_is_filterable(self, engine, project):
        item_id, _ = self._legacy_item(engine, "a,b")
        found = list_items(engine, "proj-a", tag="  A,B  ")
        assert [i["id"] for i in found] == [item_id]

    def test_over_long_name_is_filterable(self, engine, project):
        item_id, _ = self._legacy_item(engine, self.LONG)
        found = list_items(engine, "proj-a", tag=self.LONG.upper())
        assert [i["id"] for i in found] == [item_id]

    def test_both_merge_endpoints_accept_legacy_names(self, engine, project):
        item_id, _ = self._legacy_item(engine, "a,b")
        self._raw_tag(engine, self.LONG)

        out = merge_tags(engine, "a,b", self.LONG)

        assert out == {"from": "a,b", "into": self.LONG, "items_moved": 1}
        assert get_item(engine, item_id)["tags"] == [self.LONG]
        assert _count_by_name(engine) == {self.LONG: 1}

    @pytest.mark.parametrize("name", ["a,b", "x" * (MAX_TAG_LENGTH + 1)])
    def test_creating_such_a_name_is_still_refused(self, engine, name):
        """The split relaxes LOOKUPS only — the creation rules are untouched."""
        with pytest.raises(ValueError):
            add_tag(engine, name)
        with pytest.raises(ValueError):
            create_item(engine, "proj-a", "todo", "A", tags=[name])


# ── 7. K-38: the unknown-tag message is bounded and ranked ────────────────────


class TestUnknownTagMessage:
    """A closed dictionary must SAY what it knows — but a dictionary of 200
    names must not be pasted into one error. Up to 3 ranked suggestions, then a
    count and a pointer to list_tags.
    """

    TEN = ("alpha", "beta", "gamma", "delta", "epsilon",
           "zeta", "eta", "theta", "iota", "kappa")

    def _register(self, engine, names) -> None:
        for name in names:
            add_tag(engine, name, force=True)

    def test_lists_at_most_three_names_and_counts_the_rest(self, engine, project):
        self._register(engine, self.TEN)

        with pytest.raises(ValueError) as exc:
            list_items(engine, "proj-a", tag="ghost")

        msg = str(exc.value)
        assert msg.startswith("Unknown tag 'ghost'.")
        listed = [n for n in self.TEN if f"'{n}'" in msg]
        assert len(listed) == 3
        assert "… and 7 more; call list_tags" in msg

    def test_suggestions_are_the_closest_names(self, engine, project):
        self._register(engine, ["alpha", "beta", "gamma", "delta",
                                "security", "securty"])

        with pytest.raises(ValueError) as exc:
            list_items(engine, "proj-a", tag="securit")

        msg = str(exc.value)
        assert "'security'" in msg and "'securty'" in msg
        assert "'alpha'" not in msg
        assert "… and 4 more; call list_tags" in msg

    def test_small_dictionary_is_named_in_full_without_a_tail(self, engine, project):
        self._register(engine, ["perf", "docs"])

        with pytest.raises(ValueError) as exc:
            list_items(engine, "proj-a", tag="ghost")

        msg = str(exc.value)
        assert "'perf'" in msg and "'docs'" in msg
        assert "more; call list_tags" not in msg

    def test_empty_dictionary_says_none(self, engine, project):
        with pytest.raises(ValueError) as exc:
            list_items(engine, "proj-a", tag="ghost")
        assert "(none)" in str(exc.value)

    def test_truncates_an_over_long_offending_name(self, engine, project):
        """A pasted blob must not flood the message — and it is a LOOKUP, so it
        gets here instead of being refused by the length rule (K-28)."""
        self._register(engine, ["perf"])

        with pytest.raises(ValueError) as exc:
            list_items(engine, "proj-a", tag="x" * 400)

        msg = str(exc.value)
        assert f"Unknown tag '{'x' * MAX_TAG_LENGTH}…'" in msg
        assert "x" * (MAX_TAG_LENGTH + 1) not in msg


# ── 8. K-29: concurrent link race ─────────────────────────────────────────────


class TestConcurrentLinkRace:
    """`_ensure_link_conn` runs the same retry-upsert as `_add_tag_conn`: a
    competing writer takes the (item_id, tag_id) PK slot between our probe and
    our INSERT, so the INSERT lives in a SAVEPOINT and an IntegrityError falls
    back to the reactivate branch instead of poisoning the transaction.

    The seam is the probe only — the competing row is written with plain SQL and
    the IntegrityError comes from the REAL composite primary key.
    """

    @staticmethod
    def _race(monkeypatch, item_id: int, tag_id: int, *, removed: bool = False) -> list:
        from dct.core import tags as tags_module

        fired: list = []
        original = tags_module._link_row_conn

        def interleave(conn, probe_item, probe_tag):
            row = original(conn, probe_item, probe_tag)
            if row is None and not fired and (probe_item, probe_tag) == (item_id, tag_id):
                now = datetime.now(UTC)
                conn.execute(
                    item_tags.insert().values(
                        item_id=item_id,
                        tag_id=tag_id,
                        assigned_at=now,
                        removed_at=now if removed else None,
                    )
                )
                fired.append((probe_item, probe_tag))
                return None  # our caller still believes the slot is free
            return row

        monkeypatch.setattr(tags_module, "_link_row_conn", interleave)
        return fired

    def test_active_competing_link_is_reused_not_duplicated(
        self, engine, project, monkeypatch
    ):
        item = create_item(engine, "proj-a", "todo", "Raced")
        tag_id = add_tag(engine, "perf")["id"]
        fired = self._race(monkeypatch, item["id"], tag_id)

        update_item(engine, item["id"], tags=["perf"])

        assert fired, "the competing INSERT never fired — test seam is wrong"
        rows = _link_rows(engine, item["id"])
        assert len(rows) == 1 and rows[0]["removed_at"] is None
        assert get_item(engine, item["id"])["tags"] == ["perf"]

    def test_soft_removed_competing_link_is_reactivated(
        self, engine, project, monkeypatch
    ):
        """The fallback re-reads the row it collided with: a competitor that
        left the slot soft-removed must end ACTIVE, not silently dropped."""
        item = create_item(engine, "proj-a", "todo", "Raced")
        tag_id = add_tag(engine, "perf")["id"]
        fired = self._race(monkeypatch, item["id"], tag_id, removed=True)

        update_item(engine, item["id"], tags=["perf"])

        assert fired
        rows = _link_rows(engine, item["id"])
        assert len(rows) == 1 and rows[0]["removed_at"] is None
        assert get_item(engine, item["id"])["tags"] == ["perf"]

    def test_race_does_not_poison_the_surrounding_transaction(
        self, engine, project, monkeypatch
    ):
        """The SAVEPOINT is the point: the rest of the update must still commit
        (on PostgreSQL an unguarded IntegrityError aborts the transaction)."""
        item = create_item(engine, "proj-a", "todo", "Raced")
        tag_id = add_tag(engine, "perf")["id"]
        self._race(monkeypatch, item["id"], tag_id)

        update_item(engine, item["id"], title="Renamed", tags=["perf", "docs"])

        fresh = get_item(engine, item["id"])
        assert fresh["title"] == "Renamed"
        assert fresh["tags"] == ["docs", "perf"]

    def test_link_integrity_error_without_a_competing_row_propagates(
        self, engine, project, monkeypatch
    ):
        """The fallback re-PROBES rather than swallowing: an IntegrityError no
        row explains must surface, not be reported as a link that exists."""
        from dct.core import tags as tags_module

        item = create_item(engine, "proj-a", "todo", "A")

        def boom():
            raise sa.exc.IntegrityError("INSERT INTO item_tags", {}, Exception("nope"))

        monkeypatch.setattr(tags_module.item_tags, "insert", boom)
        with pytest.raises(sa.exc.IntegrityError):
            update_item(engine, item["id"], tags=["perf"])

        assert _link_rows(engine, item["id"]) == []
        assert get_item(engine, item["id"])["tags"] == []


# ── 9. K-33: description upsert on an existing active row ─────────────────────


class TestAddTagDescriptionUpsert:
    def test_description_is_applied_to_an_existing_active_row(self, engine):
        first = add_tag(engine, "perf")

        out = add_tag(engine, "perf", description="Performance work")

        assert out == {"id": first["id"], "name": "perf", "created": False}
        assert _dict_rows(engine, "perf")[0]["description"] == "Performance work"

    def test_a_later_description_replaces_the_earlier_one(self, engine):
        add_tag(engine, "perf", description="old prose")
        add_tag(engine, "perf", description="new prose")
        assert _dict_rows(engine, "perf")[0]["description"] == "new prose"

    @pytest.mark.parametrize("empty", [None, ""])
    def test_empty_description_never_erases_the_stored_prose(self, engine, empty):
        add_tag(engine, "perf", description="Performance work")
        add_tag(engine, "perf", description=empty)
        assert _dict_rows(engine, "perf")[0]["description"] == "Performance work"

    def test_upsert_does_not_duplicate_the_row(self, engine):
        add_tag(engine, "perf")
        add_tag(engine, "perf", description="Performance work")
        assert len(_dict_rows(engine, "perf")) == 1

    def test_item_write_never_touches_the_description(self, engine, project):
        """`resolve_tag_names_conn` passes no description — tagging an item must
        not blank the prose a human wrote with `dct tags add`."""
        add_tag(engine, "perf", description="Performance work")
        create_item(engine, "proj-a", "todo", "A", tags=["perf"])
        assert _dict_rows(engine, "perf")[0]["description"] == "Performance work"


# ── 10. K-39: migrate-on-write for legacy items.tags JSON ─────────────────────


class TestLegacyTagsMigrateOnWrite:
    """An item with a legacy JSON payload and NO link rows is materialized
    BEFORE the diff, so the ledger records what the write removed.

    Without it, clearing such an item's tags writes nothing at all, the
    bootstrap backfill (guard: "no link rows at all") sees a pristine item and
    re-seeds exactly the tags the user just deleted.
    """

    @staticmethod
    def _set_legacy_json(engine, item_id: int, raw: str) -> None:
        from dct.db import items as items_table

        with engine.begin() as conn:
            conn.execute(
                items_table.update().where(items_table.c.id == item_id).values(tags=raw)
            )

    @staticmethod
    def _backfill(engine) -> None:
        from dct.db import _backfill_item_tags

        _backfill_item_tags(engine)

    def _legacy_item(self, engine, raw: str = '["legacy-a", "Legacy B"]') -> int:
        item = create_item(engine, "proj-a", "todo", "Legacy")
        self._set_legacy_json(engine, item["id"], raw)
        assert _link_rows(engine, item["id"]) == [], "precondition: no link rows"
        return item["id"]

    def test_clearing_leaves_soft_removed_rows_the_backfill_can_see(
        self, engine, project
    ):
        item_id = self._legacy_item(engine)

        update_item(engine, item_id, tags=[])

        rows = _link_rows(engine, item_id)
        assert len(rows) == 2
        assert all(r["removed_at"] is not None for r in rows)
        assert get_item(engine, item_id)["tags"] == []

        self._backfill(engine)

        assert get_item(engine, item_id)["tags"] == []
        assert len(_link_rows(engine, item_id)) == 2

    def test_replacing_soft_removes_the_legacy_names(self, engine, project):
        item_id = self._legacy_item(engine)

        update_item(engine, item_id, tags=["perf"])

        assert get_item(engine, item_id)["tags"] == ["perf"]
        removed = {
            r["tag_id"] for r in _link_rows(engine, item_id) if r["removed_at"]
        }
        assert len(removed) == 2

        self._backfill(engine)
        assert get_item(engine, item_id)["tags"] == ["perf"]

    def test_keeping_a_legacy_name_reuses_its_link_row(self, engine, project):
        """Materialize-then-diff, not materialize-then-replace: a name present
        in both the JSON and the new list keeps ONE row (lesson #215)."""
        item_id = self._legacy_item(engine)

        update_item(engine, item_id, tags=["legacy-a"])

        rows = _link_rows(engine, item_id)
        assert len(rows) == 2
        assert get_item(engine, item_id)["tags"] == ["legacy-a"]

    def test_legacy_names_exempt_from_the_creation_rules(self, engine, project):
        """K-7 names reach the dictionary through the tolerant parser, exactly
        as in the bootstrap backfill — no length rule, no similarity guard."""
        long_name = "g" * (MAX_TAG_LENGTH + 5)
        item_id = self._legacy_item(engine, f'["test", "tests", "{long_name}"]')

        update_item(engine, item_id, tags=[])

        assert set(_count_by_name(engine)) == {"test", "tests", long_name}
        assert len(_link_rows(engine, item_id)) == 3

    def test_unparsable_json_is_a_no_op(self, engine, project):
        item_id = self._legacy_item(engine, "not json at all")

        update_item(engine, item_id, tags=["perf"])

        assert get_item(engine, item_id)["tags"] == ["perf"]
        assert len(_link_rows(engine, item_id)) == 1

    def test_an_item_that_already_owns_links_is_never_re_seeded(
        self, engine, project
    ):
        """The guard is per-ITEM: once the join table has spoken, the frozen
        JSON is history and must not leak back in on the next write."""
        item = create_item(engine, "proj-a", "todo", "A", tags=["perf"])
        self._set_legacy_json(engine, item["id"], '["legacy-a"]')

        update_item(engine, item["id"], tags=["docs"])

        assert get_item(engine, item["id"])["tags"] == ["docs"]
        assert {r["tag_id"] for r in _link_rows(engine, item["id"])} == {
            t["id"] for t in list_tags(engine) if t["name"] in {"perf", "docs"}
        }
