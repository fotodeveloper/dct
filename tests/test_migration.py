"""Regression tests for the lightweight migration runner (db._apply_schema_updates).

Post-#216 the `in_current_sprint` boolean column is GONE — sprint membership is
derived from item_sprints + the active sprint. These tests therefore simulate a
*pre-#216* DB (re-add the legacy column), then verify the migration seeds the
item_sprints model from the boolean and DROPS the column, leaving one source of
truth. They are the SQLite-side guard; PostgreSQL parity is verified live.
"""

import pytest
import sqlalchemy as sa

from dct.db import _apply_schema_updates, _migrate_legacy_sprint_flag
from dct.db import sprints, item_sprints
from dct.core.projects import create_project
from dct.core.items import create_item, get_item, update_item


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
    from dct.db import plans
    with engine.begin() as conn:
        conn.execute(plans.insert().values(
            ulid="01HX9Z4K5M7PQRSTVWXYZ00001", project_id=p["id"],
            slug="first", title="First", kind="plan",
        ))
        with pytest.raises(Exception):  # IntegrityError subclass varies by dialect
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
    from dct.core.sprints import set_current_sprint_items, get_current_sprint
    from dct.core.sprints import list_items_in_sprint

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
    from dct.core.projects import project_manages_changelog
    from dct.core.changelog import add_entry
    create_project(engine, slug="usesdct", name="Uses dct", path="/tmp/u")
    add_entry(engine, "usesdct", "added", "shipped a thing")
    _drop_manages_changelog(engine)

    _apply_schema_updates(engine)

    assert project_manages_changelog(engine, "usesdct") is True


def test_manages_changelog_migration_does_not_override_explicit_choice(engine):
    """Once the column exists, re-running the migration must NOT re-backfill —
    an explicit opt-in on a project with no changelog history must survive."""
    from dct.core.projects import set_manages_changelog, project_manages_changelog
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
    from dct.core.projects import set_changelog_path, get_project
    create_project(engine, slug="p", name="P", path="/tmp/p")
    set_changelog_path(engine, "p", "pkg/changelog.md")

    _apply_schema_updates(engine)
    _apply_schema_updates(engine)

    assert get_project(engine, "p")["changelog_path"] == "pkg/changelog.md"
