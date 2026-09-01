"""Regression tests for the lightweight migration runner (db._apply_schema_updates).

Post-#216 the `in_current_sprint` boolean column is GONE — sprint membership is
derived from item_sprints + the active sprint. These tests therefore simulate a
*pre-#216* DB (re-add the legacy column), then verify the migration seeds the
item_sprints model from the boolean and DROPS the column, leaving one source of
truth. They are the SQLite-side guard; PostgreSQL parity is verified live.
"""

import pytest
import sqlalchemy as sa

from dct.core.items import create_item, get_item, update_item
from dct.core.projects import create_project
from dct.db import (
    _apply_schema_updates,
    _migrate_legacy_sprint_flag,
    item_sprints,
    sprints,
)


def _cols(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("items")}


def _add_legacy_column(engine):
    """Re-add the legacy in_current_sprint column to mimic a pre-#216 DB."""
    with engine.begin() as conn:
        conn.execute(sa.text(
            "ALTER TABLE items ADD COLUMN in_current_sprint BOOLEAN NOT NULL DEFAULT 0"
        ))


def _flag_legacy(engine, item_id: int):
    """Set the legacy boolean directly (the only way pre-#216 code did it)."""
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE items SET in_current_sprint = 1 WHERE id = :id"),
            {"id": item_id},
        )


# ── schema runner: drops the legacy column, idempotent ────────────────────────

def test_migration_drops_legacy_in_current_sprint_column(engine):
    _add_legacy_column(engine)
    assert "in_current_sprint" in _cols(engine)

    _apply_schema_updates(engine)

    assert "in_current_sprint" not in _cols(engine)


def test_migration_is_idempotent_after_drop(engine):
    """The column is already gone on a fresh DB; re-running must be a safe no-op."""
    assert "in_current_sprint" not in _cols(engine)
    _apply_schema_updates(engine)
    _apply_schema_updates(engine)
    assert "in_current_sprint" not in _cols(engine)


def test_migration_preserves_existing_data(engine):
    """Existing rows survive the column drop."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", "Existing item before migration")

    _add_legacy_column(engine)
    _apply_schema_updates(engine)

    fetched = get_item(engine, item["id"])
    assert fetched is not None
    assert fetched["title"] == "Existing item before migration"
    assert fetched["in_current_sprint"] is False  # derived, no membership


def test_migrate_legacy_sprint_flag_is_a_noop_without_the_column(engine):
    """On a post-#216 DB (no column) the seed self-disables cleanly."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    # Must not raise even though the column does not exist.
    _migrate_legacy_sprint_flag(engine)
    assert sprints.name  # sanity: tables intact


def test_apply_demotes_active_legacy_sprint_independent_of_column(engine):
    """Regression for the prod state observed 2026-06-28: the drop-column step
    ran before the demote logic existed, leaving the 'legacy' sprint ACTIVE (so
    it wrongly resolved as the current sprint). The demote must heal that even
    though the in_current_sprint column is already gone — it is an independent,
    idempotent invariant, not nested under the column check."""
    from dct.core.sprints import create_sprint, get_sprint
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    # Simulate a prior build's seed: legacy sprint left ACTIVE, column already gone.
    s = create_sprint(engine, "dct", "Pre-0.1 legacy sprint", code="legacy", status="active")
    assert "in_current_sprint" not in _cols(engine)

    _apply_schema_updates(engine)

    healed = get_sprint(engine, s["id"])
    assert healed is not None
    assert healed["status"] == "completed"  # demoted to history
    assert healed["ended_at"] is not None


# ── v0.2.0 tables (migration 003) ─────────────────────────────────────────────

def test_v02_tables_are_created_by_create_tables(engine):
    """create_tables() (via the engine fixture) must build the v0.2 tables."""
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"plans", "plan_sections", "plan_checkpoints",
            "sprints", "item_sprints", "decisions"}.issubset(tables)


def test_v02_plans_has_ulid_unique(engine):
    """plans.ulid must be UNIQUE — duplicates should raise."""
    p = create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    from sqlalchemy.exc import IntegrityError

    from dct.db import plans
    with engine.begin() as conn:
        conn.execute(plans.insert().values(
            ulid="01HX9Z4K5M7PQRSTVWXYZ00001", project_id=p["id"],
            slug="first", title="First", kind="plan",
        ))
        with pytest.raises(IntegrityError):  # sqlite and PG both wrap as sa IntegrityError
            conn.execute(plans.insert().values(
                ulid="01HX9Z4K5M7PQRSTVWXYZ00001", project_id=p["id"],
                slug="second", title="Second", kind="plan",
            ))


# ── legacy boolean → item_sprints seed (then column drop) ─────────────────────

def test_legacy_flag_seeds_completed_history_sprint(engine):
    """A pre-#216 flagged item migrates into a COMPLETED 'legacy' sprint (history,
    not the current sprint), and the column is dropped. The item keeps its
    membership for the record but is NOT current — after #216 the user sets a
    fresh current sprint via /handoff rather than inheriting a migration blob."""
    p = create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", "Legacy flagged")
    _add_legacy_column(engine)
    _flag_legacy(engine, item["id"])

    _apply_schema_updates(engine)  # seed + demote-to-completed + drop

    assert "in_current_sprint" not in _cols(engine)
    with engine.begin() as conn:
        sprint = conn.execute(
            sa.select(sprints).where(sprints.c.project_id == p["id"],
                                     sprints.c.code == "legacy")
        ).first()
        assert sprint is not None
        assert sprint._mapping["name"] == "Pre-0.1 legacy sprint"
        assert sprint._mapping["status"] == "completed"  # history, not current
        link = conn.execute(
            sa.select(item_sprints).where(item_sprints.c.item_id == item["id"])
        ).first()
        assert link is not None and link._mapping["sprint_id"] == sprint._mapping["id"]

    # Membership preserved, but a completed sprint is NOT the current sprint.
    got = get_item(engine, item["id"])
    assert got is not None and got["in_current_sprint"] is False


def test_legacy_seed_is_idempotent(engine):
    """Re-running the seed must not duplicate the sprint or the link."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", "Legacy flagged")
    _add_legacy_column(engine)
    _flag_legacy(engine, item["id"])

    # Seed several times BEFORE the drop (column still present).
    _migrate_legacy_sprint_flag(engine)
    _migrate_legacy_sprint_flag(engine)
    _migrate_legacy_sprint_flag(engine)

    with engine.begin() as conn:
        legacy_sprints = conn.execute(
            sa.select(sprints).where(sprints.c.code == "legacy")
        ).fetchall()
        assert len(legacy_sprints) == 1
        links = conn.execute(
            sa.select(item_sprints).where(item_sprints.c.item_id == item["id"])
        ).fetchall()
        assert len(links) == 1


def test_legacy_seed_skips_when_no_flagged_items(engine):
    """No flagged items ⇒ no 'legacy' sprint created."""
    p = create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    create_item(engine, "dct", "issue", "Not flagged")
    _add_legacy_column(engine)

    _migrate_legacy_sprint_flag(engine)

    with engine.begin() as conn:
        legacy = conn.execute(
            sa.select(sprints).where(sprints.c.project_id == p["id"])
        ).fetchall()
        assert len(legacy) == 0


def test_legacy_seed_multi_project(engine):
    """Each project with flagged items gets its own 'legacy' sprint."""
    p1 = create_project(engine, slug="p1", name="P1", path="/tmp/p1")
    p2 = create_project(engine, slug="p2", name="P2", path="/tmp/p2")
    i1 = create_item(engine, "p1", "issue", "p1 flagged")
    i2 = create_item(engine, "p2", "issue", "p2 flagged")
    _add_legacy_column(engine)
    _flag_legacy(engine, i1["id"])
    _flag_legacy(engine, i2["id"])

    _migrate_legacy_sprint_flag(engine)

    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(sprints.c.project_id).where(sprints.c.code == "legacy")
        ).fetchall()
        assert {r.project_id for r in rows} == {p1["id"], p2["id"]}


def test_legacy_seed_tolerates_soft_removed_link(engine):
    """Regression for the 2026-06-27 crash (#215): a flagged item whose legacy
    link was later soft-removed must NOT crash the seed on re-run. The probe
    spans the full (item_id, sprint_id) PK regardless of removed_at, so the
    re-INSERT never happens and the soft-removed link is left as-is."""
    from dct.core.sprints import remove_item_from_sprint

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", "Flagged then removed")
    _add_legacy_column(engine)
    _flag_legacy(engine, item["id"])

    _migrate_legacy_sprint_flag(engine)  # seeds active legacy sprint + link
    with engine.begin() as conn:
        sprint_id = conn.execute(
            sa.select(sprints.c.id).where(sprints.c.code == "legacy")
        ).scalar_one()

    remove_item_from_sprint(engine, item["id"], sprint_id)  # soft-remove; flag stays 1

    _migrate_legacy_sprint_flag(engine)  # MUST NOT raise (was UniqueViolation)

    with engine.begin() as conn:
        links = conn.execute(
            sa.select(item_sprints).where(item_sprints.c.item_id == item["id"])
        ).fetchall()
    assert len(links) == 1
    assert links[0]._mapping["removed_at"] is not None  # not resurrected


# ── update_item / resolve guards & auto-clear (derived model) ──────────────────

def test_update_item_raises_on_in_current_sprint_kwarg(engine):
    """Direct writes to in_current_sprint via update_item still raise — there is
    no column to write, and membership goes through set/clear/assign/remove."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", "Test")

    with pytest.raises(ValueError, match="deprecated in v0.2.0"):
        update_item(engine, item["id"], in_current_sprint=True)

    with pytest.raises(ValueError, match="assign_item_to_sprint"):
        update_item(engine, item["id"], in_current_sprint=False)


def test_terminal_status_clears_flag_but_keeps_membership(engine):
    """A terminal transition clears the derived in_current_sprint flag, but the
    item_sprints link is RETAINED (K-18, #413) — that link is what makes
    sprint_completion computable. Supersedes the pre-G1 soft-remove contract."""
    from dct.core.sprints import (
        get_current_sprint,
        list_items_in_sprint,
        set_current_sprint_items,
    )

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", "Test")
    set_current_sprint_items(engine, "dct", [item["id"]])
    before = get_item(engine, item["id"])
    assert before is not None and before["in_current_sprint"] is True

    update_item(engine, item["id"], status="resolved")

    got = get_item(engine, item["id"])
    assert got is not None
    assert got["status"] == "resolved"
    assert got["in_current_sprint"] is False
    cur = get_current_sprint(engine, "dct")
    assert cur is not None
    members = list_items_in_sprint(engine, cur["id"])
    assert [m["id"] for m in members] == [item["id"]]  # link retained (ledger)


# ── K-42 backfill: un-remove pre-G1 machine-removed links (#413) ──────────────


def _link_row(engine, item_id: int):
    with engine.begin() as conn:
        return conn.execute(
            sa.select(item_sprints).where(item_sprints.c.item_id == item_id)
        ).first()._mapping


def _simulate_pre_g1_removal(engine, item_id: int):
    """Reproduce the pre-G1 terminal transition: resolve_item stamped
    item_sprints.removed_at with the SAME datetime it wrote to
    items.resolved_at (one shared `now`). G1 keeps the link, so we replay the
    old behaviour by copying resolved_at into removed_at."""
    from dct.db import items

    with engine.begin() as conn:
        resolved_at = conn.execute(
            sa.select(items.c.resolved_at).where(items.c.id == item_id)
        ).scalar_one()
        assert resolved_at is not None, "seed precondition: item must be resolved"
        conn.execute(
            item_sprints.update()
            .where(item_sprints.c.item_id == item_id)
            .values(removed_at=resolved_at)
        )
    return resolved_at


def test_k42_restores_machine_removed_membership(engine):
    """A pre-G1 legacy row (removed_at == items.resolved_at) is restored, so the
    sprint's completion history stops reading 0/N."""
    from dct.core.items import resolve_item
    from dct.core.sprint import get_sprint_review, set_sprint

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    a = create_item(engine, "dct", "feature", "Done long ago")
    b = create_item(engine, "dct", "feature", "Still open")
    set_sprint(engine, "dct", [a["id"], b["id"]])
    resolve_item(engine, a["id"], resolution="Done")
    _simulate_pre_g1_removal(engine, a["id"])
    assert get_sprint_review(engine, "dct", since="all")["summary"][
        "sprint_completion"] == "0/1"  # the damage: member vanished

    _apply_schema_updates(engine)

    assert _link_row(engine, a["id"])["removed_at"] is None
    review = get_sprint_review(engine, "dct", since="all")
    assert review["summary"]["sprint_completion"] == "1/2"
    assert review["summary"]["sprint_size"] == 2


def test_k42_leaves_manual_removals_alone(engine):
    """A deliberate remove_item_from_sprint used a DIFFERENT `now` than
    resolved_at — exact equality is the whole discriminator, so the link must
    NOT be resurrected."""
    from dct.core.items import resolve_item
    from dct.core.sprint import set_sprint
    from dct.core.sprints import get_current_sprint, remove_item_from_sprint

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    a = create_item(engine, "dct", "feature", "Dropped from the sprint")
    set_sprint(engine, "dct", [a["id"]])
    sprint = get_current_sprint(engine, "dct")
    assert sprint is not None
    remove_item_from_sprint(engine, a["id"], sprint["id"])
    resolve_item(engine, a["id"], resolution="Done elsewhere")
    removed_at = _link_row(engine, a["id"])["removed_at"]
    assert removed_at is not None

    _apply_schema_updates(engine)

    assert _link_row(engine, a["id"])["removed_at"] == removed_at


def test_k42_backfill_is_idempotent(engine):
    """Restored rows have removed_at IS NULL and can never match again; a
    manual removal stays removed across repeated runs."""
    from dct.core.items import resolve_item
    from dct.core.sprint import set_sprint
    from dct.core.sprints import get_current_sprint, remove_item_from_sprint

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    a = create_item(engine, "dct", "feature", "Legacy resolved")
    b = create_item(engine, "dct", "feature", "Manually dropped")
    set_sprint(engine, "dct", [a["id"], b["id"]])
    sprint = get_current_sprint(engine, "dct")
    assert sprint is not None
    remove_item_from_sprint(engine, b["id"], sprint["id"])
    resolve_item(engine, a["id"], resolution="Done")
    resolve_item(engine, b["id"], resolution="Done elsewhere")
    _simulate_pre_g1_removal(engine, a["id"])
    b_removed_at = _link_row(engine, b["id"])["removed_at"]

    _apply_schema_updates(engine)
    first = _link_row(engine, a["id"])["assigned_at"]
    _apply_schema_updates(engine)  # second run: pure no-op

    assert _link_row(engine, a["id"])["removed_at"] is None
    assert _link_row(engine, a["id"])["assigned_at"] == first
    assert _link_row(engine, b["id"])["removed_at"] == b_removed_at


def test_k26a_cross_project_link_is_reported_not_deleted(engine, capsys):
    """K-26a is a REPORT: a live cross-project link survives the migration and
    earns a single stderr WARNING naming the count."""
    from dct.core.sprints import create_sprint

    create_project(engine, slug="p1", name="P1", path="/tmp/p1")
    create_project(engine, slug="p2", name="P2", path="/tmp/p2")
    item = create_item(engine, "p1", "feature", "Belongs to p1")
    foreign = create_sprint(engine, "p2", "P2 sprint", code="s2")
    with engine.begin() as conn:  # only legacy damage could create this
        conn.execute(item_sprints.insert().values(
            item_id=item["id"], sprint_id=foreign["id"]))

    _apply_schema_updates(engine)

    err = capsys.readouterr().err
    assert "WARNING" in err and "1 live cross-project sprint link(s)" in err
    link = _link_row(engine, item["id"])
    assert link["removed_at"] is None  # report only — nothing deleted


def test_k26a_silent_without_cross_project_links(engine):
    """Clean DB ⇒ no warning noise on every bootstrap."""
    import contextlib
    import io

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    create_item(engine, "dct", "feature", "A")

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        _apply_schema_updates(engine)

    assert buf.getvalue() == ""


# ── manages_changelog opt-in backfill (#222) ──────────────────────────────────


def _drop_manages_changelog(engine):
    """Simulate a pre-#222 DB by removing the opt-in column."""
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE projects DROP COLUMN manages_changelog"))


def test_manages_changelog_backfill_off_without_dct_history(engine):
    """A project with NO dct changelog entries opts OUT on first migration."""
    from dct.core.projects import project_manages_changelog
    create_project(engine, slug="own", name="Own changelog", path="/tmp/own")
    _drop_manages_changelog(engine)

    _apply_schema_updates(engine)

    assert project_manages_changelog(engine, "own") is False


def test_manages_changelog_backfill_on_with_dct_history(engine):
    """A project that already keeps its changelog in dct stays managed."""
    from dct.core.changelog import add_entry
    from dct.core.projects import project_manages_changelog
    create_project(engine, slug="usesdct", name="Uses dct", path="/tmp/u")
    add_entry(engine, "usesdct", "added", "shipped a thing")
    _drop_manages_changelog(engine)

    _apply_schema_updates(engine)

    assert project_manages_changelog(engine, "usesdct") is True


def test_manages_changelog_migration_does_not_override_explicit_choice(engine):
    """Once the column exists, re-running the migration must NOT re-backfill —
    an explicit opt-in on a project with no changelog history must survive."""
    from dct.core.projects import project_manages_changelog, set_manages_changelog
    create_project(engine, slug="p", name="P", path="/tmp/p")  # no changelog
    set_manages_changelog(engine, "p", True)

    _apply_schema_updates(engine)  # column present ⇒ no-op

    assert project_manages_changelog(engine, "p") is True


# ── changelog_path per-project location (#225) ────────────────────────────────


def _drop_changelog_path(engine):
    """Simulate a pre-#225 DB by removing the changelog_path column."""
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE projects DROP COLUMN changelog_path"))


def _proj_cols(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("projects")}


def test_changelog_path_migration_adds_column_with_default(engine):
    """A pre-#225 DB (no column) gains changelog_path, and existing rows default
    to 'changelog.md' via the ADD COLUMN DEFAULT (no conditional backfill)."""
    from dct.core.projects import get_project
    create_project(engine, slug="legacy", name="Legacy", path="/tmp/legacy")
    _drop_changelog_path(engine)
    assert "changelog_path" not in _proj_cols(engine)

    _apply_schema_updates(engine)

    assert "changelog_path" in _proj_cols(engine)
    assert get_project(engine, "legacy")["changelog_path"] == "changelog.md"


def test_changelog_path_migration_is_idempotent_and_preserves_value(engine):
    """Re-running with the column present is a no-op and must not clobber an
    explicit non-default value."""
    from dct.core.projects import get_project, set_changelog_path
    create_project(engine, slug="p", name="P", path="/tmp/p")
    set_changelog_path(engine, "p", "pkg/changelog.md")

    _apply_schema_updates(engine)
    _apply_schema_updates(engine)

    assert get_project(engine, "p")["changelog_path"] == "pkg/changelog.md"


# ── items.tags JSON → tags / item_tags backfill (#611) ────────────────────────
#
# `items.tags` is frozen-deprecated (DROP at 2.0.0) and the backfill is its only
# sanctioned reader. Every test here writes the column with raw SQL — that is
# what a pre-#611 process did, and it is the ONLY way to produce the input this
# block consumes. The failure mode being guarded is severe: this runs on EVERY
# bootstrap, so a raise here bricks every dct command.


def _set_legacy_tags(engine, item_id: int, raw):
    """Write the frozen JSON column the way pre-#611 code did."""
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE items SET tags = :t WHERE id = :id"),
            {"t": raw, "id": item_id},
        )


def _tag_dict_names(engine) -> list[str]:
    from dct.db import tags as tags_table

    with engine.begin() as conn:
        return [
            r[0] for r in conn.execute(
                sa.select(tags_table.c.name)
                .where(tags_table.c.deleted_at.is_(None))
                .order_by(tags_table.c.name)
            )
        ]


def _tag_link_rows(engine, item_id: int | None = None) -> list[dict]:
    from dct.db import item_tags

    q = sa.select(item_tags)
    if item_id is not None:
        q = q.where(item_tags.c.item_id == item_id)
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


def _items_updated_at(engine, item_id: int):
    from dct.db import items

    with engine.begin() as conn:
        return conn.execute(
            sa.select(items.c.updated_at).where(items.c.id == item_id)
        ).scalar_one()


def _seed_legacy_item(engine, raw, title="Legacy tagged"):
    """A project + item whose frozen column holds `raw` and which has no links."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    item = create_item(engine, "dct", "issue", title)
    _set_legacy_tags(engine, item["id"], raw)
    assert _tag_link_rows(engine, item["id"]) == []  # precondition
    return item


def test_tags_backfill_seeds_links_from_legacy_json(engine):
    """Names are normalized (lowercase/trim) and the link is stamped with the
    item's own updated_at — the tag existed since that edit, not since the
    upgrade, and stamping `now` would falsify every 'recently tagged' read."""
    item = _seed_legacy_item(engine, '["Perf", " arch "]')
    updated_at = _items_updated_at(engine, item["id"])

    _apply_schema_updates(engine)

    got = get_item(engine, item["id"])
    assert got["tags"] == ["arch", "perf"]
    links = _tag_link_rows(engine, item["id"])
    assert len(links) == 2
    assert {link["assigned_at"] for link in links} == {updated_at}
    assert all(link["removed_at"] is None for link in links)


def test_tags_backfill_rerun_is_a_noop(engine):
    """Steady state: every later bootstrap must leave row counts AND timestamps
    exactly as they were — no re-INSERT, no re-stamp."""
    item = _seed_legacy_item(engine, '["perf", "arch"]')
    _apply_schema_updates(engine)
    before = sorted(
        (link["tag_id"], link["assigned_at"], link["removed_at"])
        for link in _tag_link_rows(engine, item["id"])
    )

    _apply_schema_updates(engine)
    _apply_schema_updates(engine)

    after = sorted(
        (link["tag_id"], link["assigned_at"], link["removed_at"])
        for link in _tag_link_rows(engine, item["id"])
    )
    assert after == before
    assert _tag_dict_names(engine) == ["arch", "perf"]


def test_tags_backfill_does_not_reseed_a_cleared_item(engine):
    """The per-ITEM guard (not per-pair): once an item has spoken through the
    join table it owns its tag set. The frozen column still holds the old JSON
    forever, so a per-pair guard would resurrect tags the user deliberately
    removed on every single bootstrap."""
    item = _seed_legacy_item(engine, '["perf", "arch"]')
    _apply_schema_updates(engine)
    update_item(engine, item["id"], tags=[])
    assert get_item(engine, item["id"])["tags"] == []

    _apply_schema_updates(engine)

    assert get_item(engine, item["id"])["tags"] == []


def test_tags_backfill_does_not_reseed_after_retagging(engine):
    item = _seed_legacy_item(engine, '["perf", "arch"]')
    _apply_schema_updates(engine)
    update_item(engine, item["id"], tags=["docs"])

    _apply_schema_updates(engine)

    assert get_item(engine, item["id"])["tags"] == ["docs"]


def test_tags_backfill_collapses_case_variants_within_one_item(engine):
    """['Perf','perf',' PERF '] is ONE tag — three links would violate the
    composite PK, and three dictionary rows would defeat the whole point."""
    item = _seed_legacy_item(engine, '["Perf", "perf", " PERF "]')

    _apply_schema_updates(engine)

    assert get_item(engine, item["id"])["tags"] == ["perf"]
    assert len(_tag_link_rows(engine, item["id"])) == 1
    assert _tag_dict_names(engine) == ["perf"]


@pytest.mark.parametrize("raw", [
    "{not valid",       # not JSON at all
    '"str"',            # valid JSON, not a list
    "[1, 2]",           # list of non-str
    '["", "   "]',      # empty after normalization
    "null",
    "{}",
    None,               # NULL column
    "[]",
    "",                 # empty string
])
def test_tags_backfill_tolerates_adversarial_column_values(engine, raw):
    """Never raise, never write junk. A raise here would break bootstrap for
    EVERY command — the heaviest failure mode of this change."""
    item = _seed_legacy_item(engine, raw)

    _apply_schema_updates(engine)  # must not raise

    assert get_item(engine, item["id"])["tags"] == []
    assert _tag_dict_names(engine) == []
    assert _tag_link_rows(engine) == []


def test_tags_backfill_keeps_good_values_from_a_mixed_list(engine, capsys):
    """Partial garbage drops only the bad elements, and the drop is REPORTED
    with the item id — a silent drop is data loss nobody can audit."""
    item = _seed_legacy_item(engine, '["perf", 5, "", null]')

    _apply_schema_updates(engine)

    assert get_item(engine, item["id"])["tags"] == ["perf"]
    assert str(item["id"]) in capsys.readouterr().err


def test_tags_backfill_preserves_legacy_names_with_commas_and_over_length(engine):
    """K-7: the comma and 50-char rules are API rules for NEW tags. Legacy data
    is history — dropping it silently would be unauditable data loss, so the
    backfill keeps every non-empty string it finds."""
    long_name = "x" * 80
    item = _seed_legacy_item(engine, json.dumps(["Legacy, With Comma", long_name, "  Spaced  "]))

    _apply_schema_updates(engine)

    names = _tag_dict_names(engine)
    assert "legacy, with comma" in names
    assert long_name in names
    assert "spaced" in names
    assert len(_tag_link_rows(engine, item["id"])) == 3


def test_tags_backfill_does_not_apply_the_similarity_guard(engine, capsys):
    """Historical names are not typos to be corrected: 'test' and 'tests' both
    register, and the run REPORTS the pair as a consolidation suggestion
    without blocking the bootstrap or merging anything."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    a = create_item(engine, "dct", "issue", "A")
    b = create_item(engine, "dct", "issue", "B")
    _set_legacy_tags(engine, a["id"], '["test"]')
    _set_legacy_tags(engine, b["id"], '["tests"]')

    _apply_schema_updates(engine)  # must not raise

    assert _tag_dict_names(engine) == ["test", "tests"]
    assert get_item(engine, a["id"])["tags"] == ["test"]
    assert get_item(engine, b["id"])["tags"] == ["tests"]
    err = capsys.readouterr().err
    assert "test" in err and "tests" in err
    assert "duplicate" in err.lower()  # a suggestion, not an action


def test_tags_backfill_picks_up_a_mixed_writer_item_on_the_next_bootstrap(engine):
    """K-8: after an upgrade an OLD MCP process can still create items that
    write the JSON column. The backfill is not one-shot — the next bootstrap
    must adopt them (this is why there is no 'migration done' marker row)."""
    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    _apply_schema_updates(engine)  # bootstrap 1: nothing to migrate

    item = create_item(engine, "dct", "issue", "Written by an old process")
    _set_legacy_tags(engine, item["id"], '["perf"]')
    assert _tag_link_rows(engine, item["id"]) == []  # nothing adopted it yet

    _apply_schema_updates(engine)  # bootstrap 2

    assert get_item(engine, item["id"])["tags"] == ["perf"]
    assert len(_tag_link_rows(engine, item["id"])) == 1  # adopted, not just read
    assert _tag_dict_names(engine) == ["perf"]


def test_tags_backfill_self_disables_once_the_column_is_dropped(engine):
    """At 2.0.0 the frozen column goes away; the block must vanish with it
    rather than crashing every bootstrap on a missing column."""
    from dct.db import _backfill_item_tags

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    create_item(engine, "dct", "issue", "Post-2.0 item")
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE items DROP COLUMN tags"))

    _backfill_item_tags(engine)     # must not raise
    _apply_schema_updates(engine)   # must not raise

    assert _tag_link_rows(engine) == []


# ── K-22 / K-30 / K-34: convergence + bounded similarity report ───────────────


def _write_statements(engine, fn) -> list[str]:
    """Run `fn`, return every INSERT/UPDATE/DELETE the engine actually issued.

    The point of the K-22 steady state is not just "no visible change" but "no
    write transaction at all" — the backfill runs on EVERY dct bootstrap, so a
    per-command write against an unconvergeable item is a permanent tax.
    """
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        head = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in {"INSERT", "UPDATE", "DELETE"}:
            seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        fn()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    return seen


def _backfill_candidate_count(engine) -> int:
    """The backfill's own anti-join: items with a non-empty frozen column and
    no item_tags row at all. Non-zero in steady state == a non-converging run."""
    with engine.begin() as conn:
        return conn.execute(sa.text(
            "SELECT COUNT(*) FROM items i "
            "WHERE i.tags IS NOT NULL AND i.tags NOT IN ('', '[]') "
            "AND NOT EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = i.id)"
        )).scalar()


def _legacy_tags_column(engine, item_id: int):
    with engine.begin() as conn:
        return conn.execute(
            sa.text("SELECT tags FROM items WHERE id = :id"), {"id": item_id}
        ).scalar()


@pytest.mark.parametrize("raw, warns", [
    ("{not valid", True),      # not JSON at all
    ('"str"', True),           # valid JSON, not a list
    ("[1, 2]", True),          # list of non-str
    ('["", "   "]', True),     # empty after normalization
    ("null", True),
    ("{}", True),
    ("[ ]", False),            # parses to an empty list: nothing to drop
])
def test_tags_backfill_converges_on_zero_yield_items(engine, capsys, raw, warns):
    """K-22: an item whose frozen column yields NO usable name can never get an
    item_tags row, so the per-item anti-join keeps re-selecting it forever — a
    write transaction plus a repeated warning on every single dct command.

    Owner decision (a): once the drop has been REPORTED, the backfill — the
    column's only sanctioned writer — blanks it to '[]', so the item leaves the
    candidate set for good. One warning total, then silence.
    """
    from dct.db import _backfill_item_tags

    item = _seed_legacy_item(engine, raw)

    _backfill_item_tags(engine)
    first_err = capsys.readouterr().err
    writes = _write_statements(engine, lambda: _backfill_item_tags(engine))
    _backfill_item_tags(engine)
    later_err = capsys.readouterr().err

    assert first_err.count("dropped") == (1 if warns else 0)
    if warns:
        assert str(item["id"]) in first_err
    assert later_err == ""                       # runs 2 and 3 say nothing
    assert writes == []                          # ... and write nothing
    assert _backfill_candidate_count(engine) == 0
    assert _legacy_tags_column(engine, item["id"]) == "[]"
    assert _tag_dict_names(engine) == []
    assert _tag_link_rows(engine) == []
    assert get_item(engine, item["id"])["tags"] == []


def test_tags_backfill_keeps_the_column_of_a_yielding_item(engine):
    """The K-22 write exception is scoped to ZERO-yield items. An item that
    migrated at least one name leaves the candidate set through its item_tags
    row, so its frozen column stays as history (partial garbage included)."""
    from dct.db import _backfill_item_tags

    raw = '["perf", 5]'
    item = _seed_legacy_item(engine, raw)

    _backfill_item_tags(engine)

    assert get_item(engine, item["id"])["tags"] == ["perf"]
    assert _legacy_tags_column(engine, item["id"]) == raw
    assert _backfill_candidate_count(engine) == 0


def test_tags_backfill_similarity_report_spans_the_whole_dictionary(engine, capsys):
    """K-30: the migrated names are compared against the FULL active dictionary,
    not only against each other. 'test' already exists (registered through the
    normal API), a legacy item brings in 'tests' — the pair must be reported."""
    from dct.db import _backfill_item_tags

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    create_item(engine, "dct", "issue", "Modern", tags=["test"])
    legacy = create_item(engine, "dct", "issue", "Legacy")
    _set_legacy_tags(engine, legacy["id"], '["tests"]')

    _backfill_item_tags(engine)

    err = capsys.readouterr().err
    assert "possible duplicates: test/tests" in err
    assert _tag_dict_names(engine) == ["test", "tests"]


def test_tags_backfill_similarity_report_is_skipped_over_the_scan_limit(engine, capsys):
    """K-34: a huge dictionary must not turn the bootstrap into an O(n^2)
    SequenceMatcher run. Past the limit the report degrades to ONE line
    pointing at `dct tags list` — never a stall, never a silent drop."""
    from dct.db import _REPORT_SCAN_LIMIT, _backfill_item_tags
    from dct.db import tags as tags_table

    create_project(engine, slug="dct", name="DCT", path="/tmp/dct")
    with engine.begin() as conn:
        conn.execute(
            tags_table.insert(),
            [{"name": f"bulk-{i:04d}"} for i in range(_REPORT_SCAN_LIMIT + 1)],
        )
    legacy = create_item(engine, "dct", "issue", "Legacy")
    _set_legacy_tags(engine, legacy["id"], '["bulk-0000s"]')

    _backfill_item_tags(engine)

    err = capsys.readouterr().err
    assert "possible duplicates not scanned" in err
    assert f"({_REPORT_SCAN_LIMIT + 2} names)" in err
    assert "dct tags list" in err
    assert "consider consolidating" not in err
    assert get_item(engine, legacy["id"])["tags"] == ["bulk-0000s"]


# The guard vector table from the #611 plan: the pairs the full SequenceMatcher
# scan flags (left) and the ones it deliberately lets through (right).
_GUARD_FLAGGED = [
    ("test", "tests"),          # .889
    ("tag", "tags"),            # .857
    ("ui", "uis"),              # .800 — plural rule only, below the threshold
    ("security", "securty"),    # .933
    ("auth", "oauth"),          # .889 — different first character
]
_GUARD_PASSED = [
    ("refactor", "refactoring"),
    ("test", "text"),
    ("test", "testing"),
    ("perf", "performance"),
    ("db", "database"),
]


@pytest.mark.parametrize("left, right", _GUARD_FLAGGED)
def test_similarity_shortlist_keeps_every_flagged_pair(left, right):
    """K-34: the prefilter is an optimisation, so it must be a SUPERSET of the
    full scan's hits. A first-character bucket would silently lose auth/oauth —
    the shortlist uses the two bounds that actually cap SequenceMatcher's ratio
    (length and shared characters), which cannot drop a real hit."""
    from dct.core.tags import SIMILARITY_THRESHOLD
    from dct.db import _similarity_shortlist

    assert right in _similarity_shortlist(left, [right], SIMILARITY_THRESHOLD)
    assert left in _similarity_shortlist(right, [left], SIMILARITY_THRESHOLD)


def test_similarity_shortlist_matches_the_full_scan_on_the_vector_table():
    """Property: for every name in the plan's vector table, running _similar on
    the shortlist yields exactly what running it on ALL names yields."""
    from dct.core.tags import SIMILARITY_THRESHOLD, _similar
    from dct.db import _similarity_shortlist

    names = sorted({n for pair in _GUARD_FLAGGED + _GUARD_PASSED for n in pair})
    for name in names:
        full = _similar(name, names)
        short = _similar(name, _similarity_shortlist(name, names, SIMILARITY_THRESHOLD))
        assert short == full, name


def test_similarity_shortlist_drops_obvious_non_candidates():
    """... while actually pruning: nothing that cannot reach the threshold
    survives (otherwise the prefilter buys nothing)."""
    from dct.core.tags import SIMILARITY_THRESHOLD
    from dct.db import _similarity_shortlist

    assert _similarity_shortlist("perf", ["performance", "database", "xyz"],
                                 SIMILARITY_THRESHOLD) == []
