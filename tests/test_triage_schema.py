"""#909 — schema of the triage findings ledger (triage_cycles / findings /
finding_options) and the generalised row lock (lock_row / lock_cycle_row).

Contract (spec #909 §2): three soft-delete ledger tables with provenance;
uniqueness of a cycle code per project, a finding seq per cycle and an option
key per finding holds among LIVE rows only; every VARCHAR width is the one the
spec names and is enforced through ensure_fits (refused, never truncated); a
database that predates the ledger gains it on its first bootstrap and the
second and third bootstraps issue no DDL, write nothing and print nothing
(AGENTS.md three-bootstrap invariant). The record / decide / close writers
serialise on the cycle row (lock_cycle_row), proven here at the primitive
level with the K-64 / K-91 pattern of tests/test_gate_race.py: the competitor
is PROVEN blocked before the holder commits — never a sleep as evidence.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import hold_row_and_race
from sqlalchemy import event

from dct.core.limits import ensure_fits
from dct.core.projects import create_project
from dct.db import (
    PROVENANCE_COLUMNS,
    TRIAGE_LEDGER_COLUMNS,
    SchemaNotReady,
    check_schema_ready,
    create_tables,
    finding_options,
    findings,
    lock_cycle_row,
    lock_row,
    metadata,
    projects,
    triage_cycles,
)

LEDGER = ("triage_cycles", "findings", "finding_options")

_DDL = re.compile(r"^\s*(ALTER|CREATE|DROP)\b", re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_LEDGER_NAMES = re.compile(r"\b(triage_cycles|findings|finding_options)\b", re.IGNORECASE)

# The widths spec #909 §2 names — a frozen copy on purpose: the test proves
# the schema matches the approved contract, ensure_fits proves it is enforced.
SPEC_WIDTHS = {
    "triage_cycles": {
        "code": 100, "topic": 200, "base_commit": 64, "artifact_url": 500,
        "created_by": 200, "closed_source": 200,
    },
    "findings": {
        "fingerprint": 500, "fingerprint_norm": 500, "title": 500,
        "reachability": 1, "fix_cost": 1, "origin": 8, "finding_type": 16,
        "suggested_decision": 16, "suggested_opt": 4, "suggested_by": 100,
        "area": 500, "reviewer": 100, "class_rec": 16, "class_override": 16,
        "decision": 16, "decision_opt": 4, "decided_by": 200, "decision_source": 200,
        # K-23: the decision writer's provenance — the widths of the
        # creation-provenance block it mirrors.
        "decision_actor_type": 16, "decision_actor_client": 64,
        "decision_session_id": 128, "decision_actor_source": 16,
    },
    "finding_options": {"key": 4},
}
TABLES = {"triage_cycles": triage_cycles, "findings": findings,
          "finding_options": finding_options}
# K-23: the decision writer's provenance, column → the creation-provenance
# column whose type it mirrors.
DECISION_WRITER = {
    "decision_actor_type": "actor_type",
    "decision_actor_client": "actor_client",
    "decision_session_id": "session_id",
    "decision_actor_source": "source",
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _project(eng, slug="proj-t") -> int:
    return create_project(eng, slug, slug.upper())["id"]


def _cycle(conn, project_id, code="rev-1") -> int:
    return conn.execute(triage_cycles.insert().values(
        project_id=project_id, code=code, topic="review of X", created_by="claude-code",
    )).inserted_primary_key[0]


def _finding(conn, cycle_id, seq=1, **extra) -> int:
    values = {
        "cycle_id": cycle_id, "seq": seq, "fingerprint": "Inv violated at B by T.",
        "fingerprint_norm": "inv violated at b by t", "title": "Data lost on close",
        "impact": 2, "reachability": "?", "evidence": 1, "fix_cost": "S",
        "origin": "diff", "finding_type": "correctness",
        "reviewer": "codex", "class_rec": "verify", **extra,
    }
    return conn.execute(findings.insert().values(**values)).inserted_primary_key[0]


def _option(conn, finding_id, key="a") -> int:
    return conn.execute(finding_options.insert().values(
        finding_id=finding_id, key=key, text="remove the cause",
    )).inserted_primary_key[0]


def _soft_delete(conn, table, row_id) -> None:
    conn.execute(table.update().where(table.c.id == row_id)
                 .values(deleted_at=datetime.now(UTC)))


def _record_all(engine) -> list[str]:
    seen: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _listener)
    return seen


def _drop_ledger(engine) -> None:
    for name in reversed(LEDGER):  # FK order: options → findings → cycles
        with engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {name}"))


def _assert_quiet_bootstraps(engine, capsys) -> None:
    seen = _record_all(engine)
    for n in (2, 3):
        seen.clear()
        create_tables(engine)
        ddl = [s for s in seen if _DDL.match(s)]
        # Older steady-state blocks issue idempotent zero-row UPDATEs of their
        # own tables; the invariant here is that nothing touches the ledger
        # (same scoping as tests/test_migration_guarantees.py).
        writes = [s for s in seen if _DML.match(s) and _LEDGER_NAMES.search(s)]
        assert ddl == [], f"bootstrap {n} issued DDL: {ddl}"
        assert writes == [], f"bootstrap {n} wrote: {writes}"
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", f"bootstrap {n} printed output"


# ── shape of the schema ──────────────────────────────────────────────────────


def test_a_fresh_bootstrap_creates_the_ledger_with_provenance_and_soft_delete(engine):
    inspector = sa.inspect(engine)
    assert set(LEDGER) <= set(inspector.get_table_names())
    for name in LEDGER:
        cols = {c["name"]: c for c in inspector.get_columns(name)}
        for col in (*PROVENANCE_COLUMNS, "created_at", "updated_at", "deleted_at"):
            assert col in cols, f"{name}.{col}"
        assert cols["deleted_at"]["nullable"] is True


def test_rubric_fields_use_full_names_and_the_reviewer_is_not_the_provenance_source(engine):
    cols = {c["name"]: c for c in sa.inspect(engine).get_columns("findings")}
    for full in ("impact", "reachability", "evidence", "fix_cost", "origin", "finding_type"):
        assert cols[full]["nullable"] is False, full
    # K-5 / K-23: the decision's origin and its writer are NULL until a
    # decision is written.
    for column in ("decided_by", "decision_source", "decided_at", *DECISION_WRITER):
        assert cols[column]["nullable"] is True, column
    for short in ("s", "o", "e", "k", "type"):
        assert short not in cols
    # `source` stays the provenance column (VARCHAR 16, nullable); the
    # reviewer has its own NOT NULL column.
    assert cols["source"]["nullable"] is True
    assert cols["reviewer"]["nullable"] is False


def test_live_uniqueness_indexes_exist_after_bootstrap(engine):
    inspector = sa.inspect(engine)
    expected = {
        "triage_cycles": {("ix_triage_cycles_project_code_live", ("project_id", "code"))},
        "findings": {("ix_findings_cycle_seq_live", ("cycle_id", "seq"))},
        "finding_options": {("ix_finding_options_finding_key_live", ("finding_id", "key"))},
    }
    for name, wanted in expected.items():
        unique = {(ix["name"], tuple(ix["column_names"]))
                  for ix in inspector.get_indexes(name) if ix["unique"]}
        assert wanted <= unique, (name, unique)
    plain = {ix["name"] for ix in inspector.get_indexes("findings")}
    assert {"ix_findings_cycle", "ix_findings_fingerprint_norm"} <= plain


def test_defaults_of_a_minimal_row(engine):
    pid = _project(engine)
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
        fid = _finding(conn, cid)
        oid = _option(conn, fid)
        cycle = conn.execute(sa.select(triage_cycles).where(triage_cycles.c.id == cid)).one()
        finding = conn.execute(sa.select(findings).where(findings.c.id == fid)).one()
        option = conn.execute(sa.select(finding_options).where(finding_options.c.id == oid)).one()
    assert cycle.rubric_version == 2
    assert cycle.opened_at is not None and cycle.closed_at is None
    # Template flags: only `violation` defaults to true.
    assert (finding.conflict, finding.violation, finding.dispute, finding.open_design) == (
        False, True, False, False)
    assert finding.decision is None and finding.first_seen_id is None and finding.item_id is None
    assert (finding.decided_at, finding.decided_by, finding.decision_source) == (None, None, None)
    assert all(getattr(finding, c) is None for c in DECISION_WRITER)
    assert (option.is_root, option.is_recommended) == (False, False)


# ── uniqueness among LIVE rows only ──────────────────────────────────────────


def test_a_cycle_code_is_unique_per_project_among_live_rows(engine):
    pid, other = _project(engine), _project(engine, "proj-u")
    with engine.begin() as conn:
        first = _cycle(conn, pid, "rev-877-r2")
        _cycle(conn, other, "rev-877-r2")  # another project: same code is fine
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _cycle(conn, pid, "rev-877-r2")
    with engine.begin() as conn:
        _soft_delete(conn, triage_cycles, first)
        _cycle(conn, pid, "rev-877-r2")  # the soft-deleted row freed the code


def test_a_finding_seq_is_unique_per_cycle_among_live_rows(engine):
    pid = _project(engine)
    with engine.begin() as conn:
        c1, c2 = _cycle(conn, pid, "rev-1"), _cycle(conn, pid, "rev-2")
        first = _finding(conn, c1, seq=7)
        _finding(conn, c2, seq=7)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _finding(conn, c1, seq=7)
    with engine.begin() as conn:
        _soft_delete(conn, findings, first)
        _finding(conn, c1, seq=7)


def test_an_option_key_is_unique_per_finding_among_live_rows(engine):
    pid = _project(engine)
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
        f1, f2 = _finding(conn, cid, seq=1), _finding(conn, cid, seq=2)
        first = _option(conn, f1, "a")
        _option(conn, f2, "a")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _option(conn, f1, "a")
    with engine.begin() as conn:
        _soft_delete(conn, finding_options, first)
        _option(conn, f1, "a")


def test_first_seen_and_promotion_links_reference_real_rows(engine):
    from dct.core.items import create_item

    pid = _project(engine)
    item_id = create_item(engine, "proj-t", "issue", "promoted finding")["id"]
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
        first = _finding(conn, cid, seq=1)
        again = _finding(conn, cid, seq=2, first_seen_id=first, item_id=item_id)
        row = conn.execute(sa.select(findings.c.first_seen_id, findings.c.item_id)
                           .where(findings.c.id == again)).one()
    assert tuple(row) == (first, item_id)
    fks = {(fk["constrained_columns"][0], fk["referred_table"])
           for fk in sa.inspect(engine).get_foreign_keys("findings")}
    assert {("cycle_id", "triage_cycles"), ("item_id", "items"),
            ("first_seen_id", "findings")} <= fks


# ── widths: the spec's numbers, enforced by ensure_fits ──────────────────────


@pytest.mark.parametrize("table_name,column,width", [
    (t, c, w) for t, cols in SPEC_WIDTHS.items() for c, w in cols.items()
])
def test_every_varchar_has_the_spec_width_and_is_refused_one_past_it(table_name, column, width):
    table = TABLES[table_name]
    assert table.c[column].type.length == width
    ensure_fits(table, {column: "x" * width})  # exactly the limit fits
    ensure_fits(table, {column: "ż" * width})  # characters, not bytes
    with pytest.raises(ValueError, match=rf"^{column} too long: {width + 1} chars, max {width}"):
        ensure_fits(table, {column: "x" * (width + 1)})


def test_no_varchar_of_the_ledger_is_left_out_of_the_spec_table():
    """A new VARCHAR column must be added to SPEC_WIDTHS (and to the spec) —
    provenance columns excepted (their widths belong to _provenance_columns)."""
    for name, table in TABLES.items():
        varchars = {c.name for c in table.columns
                    if isinstance(c.type, sa.String) and c.type.length is not None
                    and c.name not in PROVENANCE_COLUMNS}
        assert varchars == set(SPEC_WIDTHS[name]), name


def test_the_decision_writer_columns_have_the_types_of_the_creation_provenance():
    """K-23: decision_session_id is the provenance session_id's type (and so
    are the other three) — one value fits both or neither."""
    for column, creation in DECISION_WRITER.items():
        assert repr(findings.c[column].type) == repr(findings.c[creation].type), column


# ── bootstrap invariants ─────────────────────────────────────────────────────


def test_a_fresh_database_bootstraps_quietly_twice_more(engine, capsys):
    capsys.readouterr()
    _assert_quiet_bootstraps(engine, capsys)


def test_a_database_without_the_ledger_heals_once_then_stays_quiet(engine, capsys):
    pid = _project(engine)
    _drop_ledger(engine)
    with pytest.raises(SchemaNotReady, match="triage_cycles"):
        check_schema_ready(engine)  # a read-only command refuses the old schema

    seen = _record_all(engine)
    create_tables(engine)  # bootstrap 1 — creates the ledger with its indexes
    capsys.readouterr()
    created = [s for s in seen if _DDL.match(s) and _LEDGER_NAMES.search(s)]
    assert sum(s.upper().startswith("CREATE TABLE") for s in created) == 3
    check_schema_ready(engine)
    with engine.begin() as conn:  # usable end to end, unique guard included
        _cycle(conn, pid, "rev-1")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _cycle(conn, pid, "rev-1")

    _assert_quiet_bootstraps(engine, capsys)


def test_a_ledger_table_missing_its_unique_index_is_healed(engine, capsys):
    """INDEX_HEALED_TABLES: SQLite commits CREATE TABLE and CREATE INDEX
    separately — a crash in between must not leave the ledger without its
    live-uniqueness guard on every later bootstrap."""
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_findings_cycle_seq_live"))
    create_tables(engine)
    names = {ix["name"] for ix in sa.inspect(engine).get_indexes("findings")}
    assert "ix_findings_cycle_seq_live" in names
    capsys.readouterr()
    _assert_quiet_bootstraps(engine, capsys)


# ── K-4 / K-5: required rubric fields, decision origin, old ledgers ──────────


@pytest.mark.parametrize("column", ["origin", "finding_type"])
def test_a_finding_without_its_origin_or_type_is_refused_by_the_schema(engine, column):
    pid = _project(engine)
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _finding(conn, cid, **{column: None})
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(findings)).scalar_one() == 0


def _recreate_ledger_before_k4_k5(engine) -> None:
    """Replace the ledger with its shape before K-4 / K-5: origin and
    finding_type nullable, no decided_by / decision_source (what a
    development database bootstrapped by the earlier build holds)."""
    _drop_ledger(engine)
    old = sa.MetaData()
    for table in metadata.sorted_tables:
        if table.name != "findings":
            table.to_metadata(old)
    columns = []
    later = {name for name, _ddl in TRIAGE_LEDGER_COLUMNS["findings"]}
    assert later == {"decided_by", "decision_source", *DECISION_WRITER}
    for column in findings.columns:
        if column.name in later:
            continue
        copy = column._copy()
        if column.name in ("origin", "finding_type"):
            copy.nullable = True
        columns.append(copy)
    sa.Table("findings", old, *columns)
    old.create_all(engine, tables=[old.tables[name] for name in LEDGER])


def _nullable(engine, column) -> bool:
    cols = {c["name"]: c for c in sa.inspect(engine).get_columns("findings")}
    return cols[column]["nullable"]


def test_an_old_ledger_gains_the_decision_columns_and_the_required_fields_once(engine, capsys):
    pid = _project(engine)
    _recreate_ledger_before_k4_k5(engine)
    with engine.begin() as conn:  # a complete pre-K-4 row survives the upgrade
        fid = _finding(conn, _cycle(conn, pid))
    with pytest.raises(SchemaNotReady, match="findings.decided_by"):
        check_schema_ready(engine)

    seen = _record_all(engine)
    create_tables(engine)  # bootstrap 1
    out = capsys.readouterr()
    assert out.err == "", out.err
    check_schema_ready(engine)
    assert _nullable(engine, "decided_by") and _nullable(engine, "decision_source")
    assert all(_nullable(engine, c) for c in DECISION_WRITER)
    with engine.connect() as conn:
        row = conn.execute(sa.select(findings).where(findings.c.id == fid)).one()
    assert (row.origin, row.finding_type, row.decided_by, row.decision_source) == (
        "diff", "correctness", None, None)
    assert all(getattr(row, c) is None for c in DECISION_WRITER)
    if engine.dialect.name == "postgresql":
        assert not _nullable(engine, "origin") and not _nullable(engine, "finding_type")
        assert any("SET NOT NULL" in s.upper() for s in seen)
    else:
        # SQLite cannot add NOT NULL without a table rebuild: the column stays
        # nullable, silently; write-time validation is the guard there.
        assert _nullable(engine, "origin") and _nullable(engine, "finding_type")
        assert not any("SET NOT NULL" in s.upper() for s in seen)

    _assert_quiet_bootstraps(engine, capsys)


def test_null_rubric_values_are_never_guessed_and_block_only_their_constraint(engine, capsys):
    pid = _project(engine)
    _recreate_ledger_before_k4_k5(engine)
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
        complete = _finding(conn, cid, seq=1)
        gap = _finding(conn, cid, seq=2, origin=None)
        gone = _finding(conn, cid, seq=3, origin=None, deleted_at=datetime.now(UTC))
    capsys.readouterr()

    for _ in range(2):  # reported on every bootstrap until fixed, never healed by a guess
        create_tables(engine)
        err = capsys.readouterr().err
        with engine.connect() as conn:
            origins = dict(conn.execute(sa.select(findings.c.id, findings.c.origin)).all())
        assert origins == {complete: "diff", gap: None, gone: None}
        if engine.dialect.name == "postgresql":
            assert err.count("WARNING") == 1, err
            assert "findings.origin" in err and f"#{gap}" in err and f"#{gone}" in err
            assert "2 row(s)" in err
            assert _nullable(engine, "origin")
            # The column without NULLs is not held back by its neighbour.
            assert not _nullable(engine, "finding_type")
        else:
            assert err == ""
        check_schema_ready(engine)

    with engine.begin() as conn:  # the owner fills in the real values
        conn.execute(findings.update().where(findings.c.origin.is_(None))
                     .values(origin="pre"))
    create_tables(engine)
    assert capsys.readouterr().err == ""
    if engine.dialect.name == "postgresql":
        assert not _nullable(engine, "origin")
    _assert_quiet_bootstraps(engine, capsys)


def test_the_null_warning_names_at_most_ten_rows(engine, capsys):
    if engine.dialect.name != "postgresql":
        pytest.skip("the NOT NULL heal is PostgreSQL-only (SQLite keeps the column nullable)")
    pid = _project(engine)
    _recreate_ledger_before_k4_k5(engine)
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
        ids = [_finding(conn, cid, seq=n, finding_type=None) for n in range(1, 13)]
    capsys.readouterr()
    create_tables(engine)
    err = capsys.readouterr().err
    named = ", ".join(f"#{i}" for i in ids[:10])
    assert f"12 row(s) have no value ({named} (+2 more))" in err


def _recreate_findings_without_the_decision_writer(engine) -> None:
    """The ledger as the K-5 build left it: decided_by / decision_source
    present, the four K-23 decision-writer columns missing."""
    _drop_ledger(engine)
    old = sa.MetaData()
    for table in metadata.sorted_tables:
        if table.name != "findings":
            table.to_metadata(old)
    sa.Table("findings", old, *(c._copy() for c in findings.columns
                                if c.name not in DECISION_WRITER))
    old.create_all(engine, tables=[old.tables[name] for name in LEDGER])


def test_a_ledger_without_the_decision_writer_gains_it_once_never_backfilled(engine, capsys):
    """K-23: the four columns are added by bootstrap 1 (both backends); a
    decision recorded before them stays unattributed — NULL, never copied
    from the finding's creation provenance or guessed — and bootstraps 2-3
    issue no DDL, write nothing and print nothing."""
    pid = _project(engine)
    _recreate_findings_without_the_decision_writer(engine)
    with engine.begin() as conn:
        fid = _finding(conn, _cycle(conn, pid), decision="fix", decided_at=datetime.now(UTC),
                       decided_by="owner", decision_source="owner chat",
                       actor_type="agent", actor_client="codex", session_id="s-1",
                       source="mcp")
    with pytest.raises(SchemaNotReady, match="findings.decision_actor_type"):
        check_schema_ready(engine)

    seen = _record_all(engine)
    create_tables(engine)  # bootstrap 1
    out = capsys.readouterr()
    assert out.err == "", out.err
    altered = [s for s in seen if s.upper().startswith("ALTER TABLE FINDINGS")]
    assert sorted(re.search(r"ADD COLUMN (?:IF NOT EXISTS )?(\w+)", s).group(1) for s in altered) == sorted(
        DECISION_WRITER), altered
    assert not [s for s in seen if _DML.match(s) and _LEDGER_NAMES.search(s)]
    check_schema_ready(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(findings).where(findings.c.id == fid)).one()
    assert all(getattr(row, c) is None for c in DECISION_WRITER)
    assert (row.actor_client, row.decided_by) == ("codex", "owner")

    _assert_quiet_bootstraps(engine, capsys)


# ── lock_row / lock_cycle_row ────────────────────────────────────────────────


def test_lock_row_reports_a_missing_or_soft_deleted_row(engine):
    pid = _project(engine)
    with engine.begin() as conn:
        cid = _cycle(conn, pid)
        gone = _cycle(conn, pid, "rev-gone")
        _soft_delete(conn, triage_cycles, gone)
    with engine.begin() as conn:
        assert lock_cycle_row(conn, cid) is True
    with engine.begin() as conn:
        assert lock_cycle_row(conn, gone) is False
    with engine.begin() as conn:
        assert lock_row(conn, triage_cycles, 424242) is False


def test_a_close_holding_the_cycle_row_makes_a_late_record_see_the_closed_cycle(
    concurrent_engine, lock_probe,
):
    """The record-vs-close race at the primitive level: the holder locks the
    cycle and closes it; a competitor that records a finding into an OPEN
    cycle is proven blocked on the row, then reads the committed close and
    refuses — no finding lands in a closed cycle."""
    eng = concurrent_engine
    pid = _project(eng)
    with eng.begin() as conn:
        cid = _cycle(conn, pid)

    def record_into_open_cycle():
        with eng.begin() as conn:
            assert lock_cycle_row(conn, cid)
            closed_at = conn.execute(sa.select(triage_cycles.c.closed_at)
                                     .where(triage_cycles.c.id == cid)).scalar_one()
            if closed_at is not None:
                raise ValueError(f"cycle {cid} is closed")
            return _finding(conn, cid)

    outcome = hold_row_and_race(
        eng, lock_probe, lambda conn: lock_cycle_row(conn, cid), record_into_open_cycle,
        lambda conn: conn.execute(triage_cycles.update().where(triage_cycles.c.id == cid)
                                  .values(closed_at=datetime.now(UTC), closed_source="chat")))

    assert isinstance(outcome, ValueError), outcome
    with eng.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(findings)
                             .where(findings.c.cycle_id == cid)).scalar_one()
    assert count == 0


# K-21: the PostgreSQL row lock is FOR NO KEY UPDATE.


class _CompilingConn:
    """Just enough of a Connection for lock_row on a chosen dialect: every
    statement is compiled with that dialect and kept; the row lookup finds
    nothing. Proves what lock_row emits without a server of that dialect."""

    def __init__(self, dialect):
        self.dialect = dialect
        self.sql: list[str] = []

    def execute(self, statement):
        self.sql.append(str(statement.compile(dialect=self.dialect)))
        return self

    def first(self):
        return None


def test_lock_row_takes_for_no_key_update_on_postgresql_not_key_share():
    from sqlalchemy.dialects import postgresql

    conn = _CompilingConn(postgresql.dialect())
    assert lock_row(conn, triage_cycles, 1) is False
    timeout, query = conn.sql
    assert timeout == "SET LOCAL lock_timeout = '5000ms'"
    assert query.rstrip().endswith("FOR NO KEY UPDATE"), query
    assert "KEY SHARE" not in query and "FOR UPDATE" not in query.replace("NO KEY UPDATE", "")


def _project_lock_holder(engine, pid):
    """A connection holding lock_row on project `pid` inside an open transaction."""
    holder = engine.connect()
    holder.begin()
    assert lock_row(holder, projects, pid)
    return holder


def test_a_held_row_lock_lets_a_foreign_key_insert_through_but_not_a_second_lock(engine):
    """K-21 on PostgreSQL: while one transaction holds lock_row on a project,
    another inserts a cycle referencing it (FK check = FOR KEY SHARE)
    without waiting, while a second lock_row on the same row and a soft
    delete of it still wait for the holder (mutual exclusion kept). SQLite
    reserves the whole database instead — no row-level semantics to test."""
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level lock modes exist on PostgreSQL only")
    pid = _project(engine)
    holder = _project_lock_holder(engine, pid)
    try:
        with engine.connect() as other:
            with other.begin():
                other.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                cid = _cycle(other, pid, "rev-fk")  # FK key-share: not blocked
            assert cid
            # lock_row's own clause (compiled above) under a short timeout,
            # and the soft delete every ledger uses.
            for attempt in (
                lambda c: c.execute(sa.text(
                    "SELECT id FROM projects WHERE id = :id FOR NO KEY UPDATE"), {"id": pid}),
                lambda c: c.execute(sa.text(
                    "UPDATE projects SET deleted_at = now() WHERE id = :id"), {"id": pid}),
            ):
                with pytest.raises(sa.exc.OperationalError, match="lock timeout"), \
                        other.begin():
                    other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
                    attempt(other)
    finally:
        holder.rollback()
        holder.close()
