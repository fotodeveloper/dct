import json
from datetime import datetime, timedelta, timezone

import pytest

from dct.db import (
    get_engine, create_tables, items, item_checkpoints,
    changelog_entries, session_handoffs,
)
from dct.core.projects import create_project
from dct.core.sprints import create_sprint, assign_item_to_sprint, remove_item_from_sprint
from dct.core import analytics


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    eng = get_engine("sqlite://")
    create_tables(eng)
    return eng


def _mk_project(engine, slug):
    return create_project(engine, slug=slug, name=slug.title(), path=f"/tmp/{slug}")["id"]


def _insert_item(engine, project_id, **kw):
    now = datetime.now(timezone.utc)
    vals = dict(
        project_id=project_id,
        item_type=kw.get("item_type", "todo"),
        title=kw.get("title", "x"),
        description=kw.get("description"),
        priority=kw.get("priority", "P2"),
        status=kw.get("status", "open"),
        component=kw.get("component"),
        source_file=kw.get("source_file"),
        tags=kw.get("tags", "[]"),
        created_at=kw.get("created_at", now),
        updated_at=kw.get("updated_at", now),
        resolved_at=kw.get("resolved_at"),
        deleted_at=kw.get("deleted_at"),
    )
    with engine.begin() as conn:
        return conn.execute(items.insert().values(**vals).returning(items.c.id)).scalar()


def _insert_cp(engine, item_id, status="pending", kind="task", deleted=False):
    with engine.begin() as conn:
        conn.execute(item_checkpoints.insert().values(
            item_id=item_id, text="cp", status=status, kind=kind,
            deleted_at=(datetime.now(timezone.utc) if deleted else None),
        ))


def _insert_clog(engine, project_id, category="added", version=None,
                 created_at=None, released_at=None):
    with engine.begin() as conn:
        conn.execute(changelog_entries.insert().values(
            project_id=project_id, category=category, entry="e",
            version=version,
            created_at=created_at or datetime.now(timezone.utc),
            released_at=released_at,
        ))


def _insert_handoff(engine, project_id, consumed=False):
    with engine.begin() as conn:
        conn.execute(session_handoffs.insert().values(
            project_id=project_id, prompt="p", created_by="t",
            consumed_at=(datetime.now(timezone.utc) if consumed else None),
        ))


def _wk(rows, key):
    for r in rows:
        if r["week"] == key:
            return r
    raise AssertionError(f"week {key} not in {[r['week'] for r in rows]}")


def _monday(d):
    return d - timedelta(days=d.weekday())


# ── radar ─────────────────────────────────────────────────────────────────────

def test_radar_empty_project_all_zero(engine):
    _mk_project(engine, "empty")
    r = analytics.radar(engine)
    p = next(x for x in r["projects"] if x["slug"] == "empty")
    assert p["open_total"] == 0
    assert p["by_priority"] == {"P1": 0, "P2": 0, "P3": 0, "future": 0}
    assert p["in_progress"] == p["stale_7d"] == p["stale_30d"] == 0
    assert p["unconsumed_handoffs"] == p["unreleased_changelog"] == 0
    assert r["totals"] == {"open_total": 0, "in_progress": 0, "stale_7d": 0, "unconsumed_handoffs": 0}


def test_radar_all_resolved_project_has_no_open(engine):
    pid = _mk_project(engine, "done")
    _insert_item(engine, pid, status="resolved", priority="P1",
                 resolved_at=datetime.now(timezone.utc))
    _insert_item(engine, pid, status="wont_fix", priority="P2")
    r = analytics.radar(engine)
    p = next(x for x in r["projects"] if x["slug"] == "done")
    assert p["open_total"] == 0
    assert p["in_progress"] == 0
    assert all(v == 0 for v in p["by_priority"].values())


def test_radar_priority_breakdown_counts_only_open(engine):
    pid = _mk_project(engine, "prio")
    for pr in ("P1", "P2", "P2", "P3", "future"):
        _insert_item(engine, pid, status="open", priority=pr)
    _insert_item(engine, pid, status="in_progress", priority="P1")  # not in open_total
    r = analytics.radar(engine)
    p = next(x for x in r["projects"] if x["slug"] == "prio")
    assert p["open_total"] == 5
    assert p["by_priority"] == {"P1": 1, "P2": 2, "P3": 1, "future": 1}
    assert p["in_progress"] == 1


def test_radar_stale_buckets(engine):
    pid = _mk_project(engine, "stale")
    now = datetime.now(timezone.utc)
    _insert_item(engine, pid, status="in_progress", updated_at=now - timedelta(days=8))
    _insert_item(engine, pid, status="open", updated_at=now - timedelta(days=31))
    _insert_item(engine, pid, status="in_progress", updated_at=now - timedelta(days=5))  # fresh
    r = analytics.radar(engine)
    p = next(x for x in r["projects"] if x["slug"] == "stale")
    assert p["stale_7d"] == 2   # 8d + 31d
    assert p["stale_30d"] == 1  # only 31d


def test_radar_handoffs_and_unreleased_changelog(engine):
    pid = _mk_project(engine, "hand")
    _insert_handoff(engine, pid, consumed=False)
    _insert_handoff(engine, pid, consumed=True)   # excluded
    _insert_clog(engine, pid, version=None)        # unreleased
    _insert_clog(engine, pid, version="1.0")       # released, excluded
    r = analytics.radar(engine)
    p = next(x for x in r["projects"] if x["slug"] == "hand")
    assert p["unconsumed_handoffs"] == 1
    assert p["unreleased_changelog"] == 1


def test_radar_soft_deleted_rows_excluded(engine):
    pid = _mk_project(engine, "sd")
    _insert_item(engine, pid, status="open", priority="P1")
    _insert_item(engine, pid, status="open", priority="P1",
                 deleted_at=datetime.now(timezone.utc))  # must NOT count
    r = analytics.radar(engine)
    p = next(x for x in r["projects"] if x["slug"] == "sd")
    assert p["open_total"] == 1
    assert p["by_priority"]["P1"] == 1


def test_radar_cross_project_totals_equal_sum_no_double_count(engine):
    pa = _mk_project(engine, "proj-a")
    pb = _mk_project(engine, "proj-b")
    for _ in range(3):
        _insert_item(engine, pa, status="open")
    for _ in range(2):
        _insert_item(engine, pb, status="open")
    _insert_item(engine, pb, status="in_progress")
    _insert_handoff(engine, pa)
    r = analytics.radar(engine)
    a = next(x for x in r["projects"] if x["slug"] == "proj-a")
    b = next(x for x in r["projects"] if x["slug"] == "proj-b")
    assert a["open_total"] == 3 and b["open_total"] == 2
    assert b["in_progress"] == 1
    assert r["totals"]["open_total"] == a["open_total"] + b["open_total"] == 5
    assert r["totals"]["in_progress"] == a["in_progress"] + b["in_progress"] == 1
    assert r["totals"]["unconsumed_handoffs"] == 1


# ── backlog_health ────────────────────────────────────────────────────────────

def test_backlog_distributions(engine):
    pid = _mk_project(engine, "dist")
    _insert_item(engine, pid, item_type="issue", priority="P1", status="open")
    _insert_item(engine, pid, item_type="todo", priority="P2", status="in_progress")
    _insert_item(engine, pid, item_type="todo", priority="P2", status="resolved")
    h = analytics.backlog_health(engine, "dist")
    assert h["by_type"] == {"issue": 1, "todo": 2}
    assert h["by_priority"] == {"P1": 1, "P2": 2}
    assert h["by_status"] == {"open": 1, "in_progress": 1, "resolved": 1}


def test_backlog_by_component(engine):
    pid = _mk_project(engine, "comp")
    _insert_item(engine, pid, component="db")
    _insert_item(engine, pid, component="db")
    _insert_item(engine, pid, component="api")
    _insert_item(engine, pid, component=None)
    h = analytics.backlog_health(engine, "comp")
    assert h["by_component"][0] == {"component": "db", "count": 2}
    assert {"component": "api", "count": 1} in h["by_component"]
    # None component never produces a row
    assert all(c["component"] is not None for c in h["by_component"])


def test_backlog_aging_boundary_6_7_8_days(engine):
    pid = _mk_project(engine, "age")
    now = datetime.now(timezone.utc)
    _insert_item(engine, pid, status="open", created_at=now - timedelta(days=6))   # lt_1w
    _insert_item(engine, pid, status="open", created_at=now - timedelta(days=7))   # boundary -> w1_4
    _insert_item(engine, pid, status="open", created_at=now - timedelta(days=8))   # w1_4
    _insert_item(engine, pid, status="open", created_at=now - timedelta(days=40))  # m1_3
    _insert_item(engine, pid, status="open", created_at=now - timedelta(days=100)) # gt_3m
    # resolved item must NOT be aged
    _insert_item(engine, pid, status="resolved", created_at=now - timedelta(days=2),
                 resolved_at=now)
    h = analytics.backlog_health(engine, "age")
    assert h["aging"] == {"lt_1w": 1, "w1_4": 2, "m1_3": 1, "gt_3m": 1}


def test_backlog_hotspots_threshold_two(engine):
    pid = _mk_project(engine, "hot")
    for _ in range(2):
        _insert_item(engine, pid, status="open", component="ingest", source_file="a.py")
    _insert_item(engine, pid, status="open", component="solo", source_file="b.py")
    h = analytics.backlog_health(engine, "hot")
    assert h["hotspots"]["components"] == [{"name": "ingest", "count": 2}]
    assert h["hotspots"]["files"] == [{"name": "a.py", "count": 2}]


def test_backlog_tags_defensive_parse(engine):
    pid = _mk_project(engine, "tags")
    _insert_item(engine, pid, status="open", tags=json.dumps(["alpha", "beta"]))
    _insert_item(engine, pid, status="open", tags=json.dumps(["alpha"]))
    _insert_item(engine, pid, status="open", tags="{not valid json")  # must not crash
    _insert_item(engine, pid, status="open", tags=None)               # must not crash
    h = analytics.backlog_health(engine, "tags")
    counts = {t["tag"]: t["count"] for t in h["tags"]}
    assert counts == {"alpha": 2, "beta": 1}


def test_backlog_checkpoint_stats(engine):
    pid = _mk_project(engine, "cp")
    iid = _insert_item(engine, pid, status="open")
    _insert_cp(engine, iid, status="done")
    _insert_cp(engine, iid, status="done")
    _insert_cp(engine, iid, status="pending")
    _insert_cp(engine, iid, status="rejected")
    _insert_cp(engine, iid, status="pending", kind="dogfood")  # gate
    _insert_cp(engine, iid, status="done", kind="security")    # gate
    _insert_cp(engine, iid, status="pending", kind="cc-task")  # NOT a gate
    h = analytics.backlog_health(engine, "cp")
    c = h["checkpoints"]
    assert c["done"] == 3 and c["pending"] == 3 and c["rejected"] == 1
    assert c["gates_by_kind"] == {"dogfood": 1, "security": 1}
    # completion_rate = done / (done + pending) = 3 / 6
    assert c["completion_rate"] == 0.5


def test_backlog_checkpoint_zero_no_division_error(engine):
    _mk_project(engine, "nocp")
    h = analytics.backlog_health(engine, "nocp")
    assert h["checkpoints"]["completion_rate"] == 0.0


def test_backlog_soft_deleted_excluded_everywhere(engine):
    pid = _mk_project(engine, "ghost")
    keep = _insert_item(engine, pid, status="open", priority="P2", component="real",
                        tags=json.dumps(["keep"]))
    _insert_cp(engine, keep, status="pending")
    dead = _insert_item(engine, pid, status="open", priority="P1", component="ghost",
                        tags=json.dumps(["ghost"]), deleted_at=datetime.now(timezone.utc))
    _insert_cp(engine, dead, status="done")  # parent item soft-deleted -> excluded
    h = analytics.backlog_health(engine, "ghost")
    assert "P1" not in h["by_priority"]
    assert all(c["component"] != "ghost" for c in h["by_component"])
    assert all(t["tag"] != "ghost" for t in h["tags"])
    assert h["checkpoints"]["done"] == 0
    assert h["checkpoints"]["pending"] == 1


# ── velocity ──────────────────────────────────────────────────────────────────

def test_velocity_window_length_default_weeks(engine):
    _mk_project(engine, "v0")
    v = analytics.velocity(engine, "v0")
    assert len(v["resolved_per_week"]) == 26
    assert len(v["created_per_week"]) == 26
    assert len(v["backlog_growth"]) == 26
    assert len(v["changelog_per_week"]) == 26
    # empty project -> every bucket zero
    assert all(b["count"] == 0 for b in v["resolved_per_week"])


def test_velocity_buckets_and_backlog_growth(engine):
    pid = _mk_project(engine, "v1")
    now = datetime.now(timezone.utc)
    this_monday = _monday(now.date())
    prev2 = (this_monday - timedelta(days=14)).isoformat()
    cur = this_monday.isoformat()

    # A: created now, resolved now
    _insert_item(engine, pid, status="resolved", created_at=now, resolved_at=now)
    # B: created 14 days ago, still open
    _insert_item(engine, pid, status="open", created_at=now - timedelta(days=14))

    v = analytics.velocity(engine, "v1", weeks=4)
    assert _wk(v["resolved_per_week"], cur)["count"] == 1
    assert _wk(v["created_per_week"], cur)["count"] == 1
    assert _wk(v["created_per_week"], prev2)["count"] == 1
    assert _wk(v["backlog_growth"], cur)["net"] == 0          # 1 created - 1 resolved
    assert _wk(v["backlog_growth"], prev2)["net"] == 1        # 1 created - 0 resolved


def test_velocity_changelog_and_release_cadence(engine):
    pid = _mk_project(engine, "v2")
    now = datetime.now(timezone.utc)
    _insert_clog(engine, pid, category="added", created_at=now)
    _insert_clog(engine, pid, category="fixed", created_at=now - timedelta(days=7))
    _insert_clog(engine, pid, category="changed", version="1.0",
                 created_at=now - timedelta(days=3), released_at=now - timedelta(days=3))
    _insert_clog(engine, pid, category="removed", version="1.1",
                 created_at=now - timedelta(days=1), released_at=now - timedelta(days=1))

    v = analytics.velocity(engine, "v2", weeks=8)
    totals = {"added": 0, "changed": 0, "fixed": 0, "removed": 0}
    for w in v["changelog_per_week"]:
        for k in totals:
            totals[k] += w[k]
    assert totals == {"added": 1, "changed": 1, "fixed": 1, "removed": 1}
    assert [r["version"] for r in v["release_cadence"]] == ["1.0", "1.1"]
    assert all(isinstance(r["released_at"], str) for r in v["release_cadence"])


def test_velocity_sprint_completion_excludes_deleted_and_removed(engine):
    pid = _mk_project(engine, "v3")
    now = datetime.now(timezone.utc)
    a = _insert_item(engine, pid, status="resolved", created_at=now, resolved_at=now)
    b = _insert_item(engine, pid, status="open", created_at=now)
    far = now - timedelta(days=400)
    # `d` is assigned while live, then soft-deleted -> link survives but the
    # item must be excluded from size (assign_item_to_sprint refuses to link a
    # row whose deleted_at is already set, so we delete AFTER assignment).
    d = _insert_item(engine, pid, status="resolved", created_at=far, resolved_at=far)
    e = _insert_item(engine, pid, status="resolved", created_at=far, resolved_at=far)

    sp = create_sprint(engine, "v3", name="S1", code="s1")["id"]
    assign_item_to_sprint(engine, a, sp)
    assign_item_to_sprint(engine, b, sp)
    assign_item_to_sprint(engine, d, sp)   # then soft-deleted below -> not counted
    assign_item_to_sprint(engine, e, sp)
    remove_item_from_sprint(engine, e, sp)  # link removed -> not counted
    with engine.begin() as conn:
        conn.execute(items.update().where(items.c.id == d).values(deleted_at=now))

    v = analytics.velocity(engine, "v3", weeks=4)
    row = next(r for r in v["sprint_completion"] if r["code"] == "s1")
    assert row["size"] == 2   # a + b only
    assert row["done"] == 1   # a resolved
