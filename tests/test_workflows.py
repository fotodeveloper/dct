"""G1 workflow ops (#413): get_context_snapshot / pickup_work / handoff_work.

Four-discipline coverage: contract (shape promises), edge (0/1/many,
emptiness), property-ish invariants (POST-consume coherence, read-never-
writes) and adversarial (cross-project ids, atomicity injection, 100k
prompts). Slugs are OSS-hygienic placeholders (proj-a / proj-b).
"""

from datetime import UTC, datetime, timedelta

import pytest

from dct.core.checkpoints import add_checkpoint, set_status
from dct.core.handoff import create_handoff, list_handoffs
from dct.core.items import create_item, get_item, resolve_item
from dct.core.projects import create_project
from dct.core.sprints import (
    create_sprint,
    get_current_sprint,
    list_items_in_sprint,
    set_current_sprint_items,
)
from dct.db import session_handoffs
from dct.workflows.session import (
    SNAPSHOT_SECTIONS,
    get_context_snapshot,
    handoff_work,
    pickup_work,
)

WHO = "claude-test @ proj-a 2026-07-28T12:00Z"


@pytest.fixture
def proj(engine):
    return create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a")


@pytest.fixture
def other(engine):
    return create_project(engine, slug="proj-b", name="Project B", path="/tmp/proj-b")


def _mk_handoff(engine, slug="proj-a", prompt="Continue the work", scope=None):
    return create_handoff(engine, slug, prompt, WHO, scope=scope)


# ── get_context_snapshot: contract ─────────────────────────────────────────────

def test_snapshot_all_sections_key_set(engine, proj):
    snap = get_context_snapshot(engine, "proj-a")
    assert set(snap) == {
        "project", "since", "since_anchor", "sections", "summary",
        "sprint", "handoff", "gates", "decisions", "recent_changes",
    }
    assert snap["sections"] == list(SNAPSHOT_SECTIONS)
    assert snap["project"] == "proj-a"


def test_snapshot_include_subset_omits_sections(engine, proj):
    snap = get_context_snapshot(engine, "proj-a", include=["sprint"])
    assert "sprint" in snap and "handoff" not in snap and "gates" not in snap
    assert snap["sections"] == ["sprint"]
    assert "summary" in snap  # always present


def test_snapshot_include_none_sentinel(engine, proj):
    snap = get_context_snapshot(engine, "proj-a", include=["none"])
    assert snap["sections"] == []
    for s in SNAPSHOT_SECTIONS:
        assert s not in snap
    assert "summary" in snap


def test_snapshot_unknown_section_raises_naming_vocabulary(engine, proj):
    with pytest.raises(ValueError, match="Unknown snapshot section.*sprint.*none"):
        get_context_snapshot(engine, "proj-a", include=["sprnit"])


def test_snapshot_unregistered_project_raises(engine):
    with pytest.raises(ValueError):
        get_context_snapshot(engine, "no-such-project")


def test_snapshot_latest_full_others_previewed(engine, proj):
    old = _mk_handoff(engine, prompt="x" * 5000)
    new = _mk_handoff(engine, prompt="y" * 5000)
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["handoff"]["latest"]["id"] == new["id"]
    assert snap["handoff"]["latest"]["prompt"] == "y" * 5000  # untruncated
    others = snap["handoff"]["others"]
    row = others["rows"][0]
    assert row[others["cols"].index("id")] == old["id"]
    preview = row[others["cols"].index("prompt")]
    assert len(preview) == 200 and preview.endswith("…")
    assert snap["handoff"]["pending_count"] == 2


def test_snapshot_sprint_section_multi_sprint(engine, proj):
    from dct.core.sprints import assign_item_to_sprint
    a = create_item(engine, "proj-a", "feature", "A", priority="P1")
    b = create_item(engine, "proj-a", "feature", "B")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    s2 = create_sprint(engine, "proj-a", "S2", code="s2", status="active")
    # Explicit targeting — ambiguous current-sprint writes are refused (K-33).
    assign_item_to_sprint(engine, a["id"], s1["id"])
    assign_item_to_sprint(engine, b["id"], s2["id"])

    snap = get_context_snapshot(engine, "proj-a")
    sprint = snap["sprint"]
    assert sprint["primary_sprint_id"] == s2["id"]
    assert [s["id"] for s in sprint["active_sprints"]] == [s2["id"], s1["id"]]
    items_tbl = sprint["items"]
    got = {
        (r[items_tbl["cols"].index("id")], r[items_tbl["cols"].index("sprint_id")])
        for r in items_tbl["rows"]
    }
    assert got == {(a["id"], s1["id"]), (b["id"], s2["id"])}
    assert snap["summary"]["active_sprint_count"] == 2
    assert snap["summary"]["sprint_size"] == 2


def test_snapshot_sprint_items_pin_their_columns_and_never_leak_tags(engine, proj):
    """K-23 regression guard: the snapshot's sprint-item rows are a PINNED
    projection (`_query_sprint_items_with_cp` selects explicit columns), so a
    tagged item must not widen the table nor smuggle the frozen `items.tags`
    column in. Both the {cols,rows} table and the checkpoint table are checked
    — cols is the wire contract every skill indexes into.
    """
    a = create_item(engine, "proj-a", "feature", "A", tags=["alpha", "beta"])
    add_checkpoint(engine, a["id"], "Step one")
    set_current_sprint_items(engine, "proj-a", [a["id"]])

    tbl = get_context_snapshot(engine, "proj-a")["sprint"]["items"]

    assert tbl["cols"] == ["id", "sprint_id", "priority", "status", "cp", "title"]
    assert "tags" not in tbl["cols"]
    assert tbl["rows"] and all(len(r) == len(tbl["cols"]) for r in tbl["rows"])
    assert dict(zip(tbl["cols"], tbl["rows"][0]))["id"] == a["id"]

    # The conn-level reader itself must not carry a `tags` key either — the
    # {cols,rows} projection is a formatter, not the guard.
    from dct.core.projects import resolve_project_id
    from dct.core.sprint import _query_sprint_items_with_cp

    with engine.begin() as conn:
        raw = _query_sprint_items_with_cp(conn, resolve_project_id(conn, "proj-a"))
    assert [set(r) for r in raw] == [{"id", "sprint_id", "priority", "status", "cp", "title"}]


def test_snapshot_sprint_checkpoints_expose_next_step(engine, proj):
    """K-9 / plan #12 §18: the next pending checkpoint is readable from ONE
    snapshot — no follow-up get_item required."""
    a = create_item(engine, "proj-a", "feature", "A")
    done = add_checkpoint(engine, a["id"], "Step one", sort_order=10)
    set_status(engine, done["id"], "done")
    pend = add_checkpoint(engine, a["id"], "Step two", sort_order=20)
    rej = add_checkpoint(engine, a["id"], "Abandoned", sort_order=30)
    set_status(engine, rej["id"], "rejected")
    set_current_sprint_items(engine, "proj-a", [a["id"]])

    snap = get_context_snapshot(engine, "proj-a")
    tbl = snap["sprint"]["checkpoints"]
    rows = [
        dict(zip(tbl["cols"], r)) for r in tbl["rows"]
    ]
    assert [r["id"] for r in rows] == [done["id"], pend["id"]]  # rejected hidden
    assert rows[1]["status"] == "pending" and rows[1]["text"] == "Step two"


def test_snapshot_gates_filtering(engine, proj):
    """dogfood gate on an in-sprint item shows; task/cc-task never gate; a gate
    on a non-sprint item stays out."""
    a = create_item(engine, "proj-a", "feature", "In sprint", priority="P1")
    add_checkpoint(engine, a["id"], "Plain task", kind="task")
    add_checkpoint(engine, a["id"], "Mirror", kind="cc-task")
    gate = add_checkpoint(engine, a["id"], "Fresh-session dogfood", kind="dogfood")
    outside = create_item(engine, "proj-a", "feature", "Outside")
    add_checkpoint(engine, outside["id"], "Security review", kind="security")
    set_current_sprint_items(engine, "proj-a", [a["id"]])

    snap = get_context_snapshot(engine, "proj-a")
    tbl = snap["gates"]
    rows = [dict(zip(tbl["cols"], r)) for r in tbl["rows"]]
    assert [r["checkpoint_id"] for r in rows] == [gate["id"]]
    assert rows[0]["kind"] == "dogfood" and rows[0]["item_title"] == "In sprint"
    assert snap["summary"]["gates_pending"] == 1


def test_snapshot_decisions_filter_and_preview(engine, proj):
    from dct.core.decisions import create_decision, update_decision

    keep = create_decision(
        engine, "proj-a", kind="adr", title="Keep me",
        decision="D" * 500, status="accepted",
    )
    prop = create_decision(engine, "proj-a", kind="open_question", title="Open Q")
    dead = create_decision(engine, "proj-a", kind="adr", title="Dead", status="accepted")
    update_decision(engine, dead["id"], status="superseded")

    snap = get_context_snapshot(engine, "proj-a")
    tbl = snap["decisions"]
    rows = [dict(zip(tbl["cols"], r)) for r in tbl["rows"]]
    ids = [r["id"] for r in rows]
    assert keep["id"] in ids and prop["id"] in ids and dead["id"] not in ids
    kept = next(r for r in rows if r["id"] == keep["id"])
    assert len(kept["decision_preview"]) == 200 and kept["decision_preview"].endswith("…")


# ── get_context_snapshot: edge ─────────────────────────────────────────────────

def test_snapshot_no_active_sprint_and_read_never_writes(engine, proj):
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["sprint"]["primary_sprint_id"] is None
    assert snap["sprint"]["active_sprints"] == []
    assert snap["summary"]["sprint_completion"] == "0/0"
    # A read must never create a sprint.
    assert get_current_sprint(engine, "proj-a") is None


def test_snapshot_all_handoffs_consumed(engine, proj):
    from dct.core.handoff import consume_handoff
    h = _mk_handoff(engine)
    consume_handoff(engine, h["id"], WHO)
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["handoff"]["latest"] is None
    assert snap["handoff"]["pending_count"] == 0


def test_snapshot_empty_project(engine, proj):
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["summary"]["total_open_in_project"] == 0
    assert snap["sprint"]["items"]["rows"] == []


# ── pickup_work ────────────────────────────────────────────────────────────────

def test_pickup_default_never_consumes_without_identity(engine, proj):
    """Strict no-silent-downgrade: auto mode without consumed_by raises."""
    _mk_handoff(engine)
    with pytest.raises(ValueError, match="consumed_by cannot be empty"):
        pickup_work(engine, "proj-a")


def test_pickup_consume_false_is_pure_read(engine, proj):
    h = _mk_handoff(engine)
    out = pickup_work(engine, "proj-a", consume=False)
    assert out["consumed"] is False and out["picked_up"] is None
    assert out["handoff"]["latest"]["id"] == h["id"]  # still unconsumed
    assert out["handoff"]["pending_count"] == 1


def test_pickup_auto_consumes_single_candidate_post_consume_snapshot(engine, proj):
    """Owner 2026-07-28: exactly ONE handoff → auto-consume. K-3/4: the
    snapshot reflects the state AFTER the write — picked_up is the only place
    the consumed row lives."""
    h = _mk_handoff(engine, prompt="Full text " * 50)
    out = pickup_work(engine, "proj-a", consumed_by=WHO)
    assert out["consumed"] is True
    assert out["picked_up"]["id"] == h["id"]
    assert out["picked_up"]["prompt"] == "Full text " * 50  # untruncated
    assert out["picked_up"]["consumed_by"] == WHO
    assert out["picked_up"]["consumed_at"] is not None
    # POST-consume: gone from the handoff section and the counters.
    assert out["handoff"]["latest"] is None
    assert out["handoff"]["pending_count"] == 0
    assert out["summary"]["handoffs_pending"] == 0


def test_pickup_auto_with_many_candidates_consumes_nothing(engine, proj):
    h1 = _mk_handoff(engine, prompt="first")
    h2 = _mk_handoff(engine, prompt="second")
    out = pickup_work(engine, "proj-a", consumed_by=WHO)
    assert out["consumed"] is False and out["picked_up"] is None
    assert out["handoff"]["pending_count"] == 2
    assert out["handoff"]["latest"]["id"] == h2["id"]
    assert h1["id"] in [r[0] for r in out["handoff"]["others"]["rows"]]


def test_pickup_auto_with_zero_candidates(engine, proj):
    out = pickup_work(engine, "proj-a", consumed_by=WHO)
    assert out["consumed"] is False and out["picked_up"] is None


def test_pickup_consume_true_takes_newest(engine, proj):
    _mk_handoff(engine, prompt="older")
    new = _mk_handoff(engine, prompt="newer")
    out = pickup_work(engine, "proj-a", consume=True, consumed_by=WHO)
    assert out["picked_up"]["id"] == new["id"]
    assert out["handoff"]["pending_count"] == 1


def test_pickup_consume_true_no_candidate_raises(engine, proj):
    with pytest.raises(ValueError, match="No unconsumed handoff"):
        pickup_work(engine, "proj-a", consume=True, consumed_by=WHO)


def test_pickup_scope_filter_selects_and_stays_coherent(engine, proj):
    """K-3: scope-matched consumption; handoff.latest cannot point at a
    different (newer, unscoped) record than the one the scope targeted."""
    scoped_old = _mk_handoff(engine, prompt="scoped old", scope="g1")
    _mk_handoff(engine, prompt="unscoped newer")  # newer, but not in scope
    out = pickup_work(engine, "proj-a", scope="g1", consumed_by=WHO)
    assert out["picked_up"]["id"] == scoped_old["id"]  # single in-scope → auto
    # Post-consume, no g1 handoff remains → latest (scope-coherent) is None,
    # while the unscoped one is still visible among the others.
    assert out["handoff"]["latest"] is None
    assert out["handoff"]["pending_count"] == 1


def test_pickup_explicit_handoff_id(engine, proj):
    old = _mk_handoff(engine, prompt="older")
    _mk_handoff(engine, prompt="newer")
    out = pickup_work(engine, "proj-a", handoff_id=old["id"], consumed_by=WHO)
    assert out["picked_up"]["id"] == old["id"]


def test_pickup_handoff_id_from_other_project_rejected(engine, proj, other):
    """K-1 adversarial: an explicit id must not consume another project's
    handoff — same message as a missing id, nothing consumed."""
    foreign = create_handoff(engine, "proj-b", "Foreign prompt", WHO)
    with pytest.raises(ValueError, match=f"Handoff #{foreign['id']} not found or already consumed"):
        pickup_work(engine, "proj-a", handoff_id=foreign["id"], consumed_by=WHO)
    assert list_handoffs(engine, "proj-b")[0]["consumed_at"] is None


def test_pickup_already_consumed_id_exact_message(engine, proj):
    from dct.core.handoff import consume_handoff
    h = _mk_handoff(engine)
    consume_handoff(engine, h["id"], WHO)
    with pytest.raises(ValueError, match=f"Handoff #{h['id']} not found or already consumed"):
        pickup_work(engine, "proj-a", handoff_id=h["id"], consumed_by=WHO)


def test_pickup_consume_false_with_handoff_id_contradiction(engine, proj):
    h = _mk_handoff(engine)
    with pytest.raises(ValueError, match="contradictory"):
        pickup_work(engine, "proj-a", consume=False, handoff_id=h["id"])


def test_pickup_created_at_tie_higher_id_wins(engine, proj):
    h1 = _mk_handoff(engine, prompt="first")
    h2 = _mk_handoff(engine, prompt="second")
    with engine.begin() as conn:
        conn.execute(
            session_handoffs.update()
            .where(session_handoffs.c.id.in_([h1["id"], h2["id"]]))
            .values(created_at=h1["created_at"])
        )
    out = pickup_work(engine, "proj-a", consume=True, consumed_by=WHO)
    assert out["picked_up"]["id"] == h2["id"]


def test_pickup_include_none_cheap_confirm(engine, proj):
    h = _mk_handoff(engine)
    out = pickup_work(
        engine, "proj-a", consume=True, handoff_id=h["id"],
        consumed_by=WHO, include=["none"],
    )
    assert out["consumed"] is True and out["picked_up"]["id"] == h["id"]
    assert "sprint" not in out and "handoff" not in out
    assert "summary" in out


# ── handoff_work ───────────────────────────────────────────────────────────────

def test_handoff_work_sets_sprint_and_creates_handoff(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    out = handoff_work(
        engine, "proj-a", prompt="Next: do B", created_by=WHO,
        item_ids=[a["id"], b["id"]], scope="g1",
    )
    assert out["sprint"]["open_size"] == 2 and out["sprint"]["items"] == sorted([a["id"], b["id"]])
    assert out["handoff"]["scope"] == "g1"
    assert list_handoffs(engine, "proj-a")[0]["id"] == out["handoff"]["id"]
    cur = get_current_sprint(engine, "proj-a")
    assert cur is not None and cur["id"] == out["sprint"]["sprint_id"]


def test_handoff_work_open_size_excludes_terminal_members(engine, proj):
    """K-45: the ack counts OPEN members only and names the field open_size —
    the snapshot's active_sprints[].size keeps counting open + terminal
    history (ADR #57), so the two must not share a name."""
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    c = create_item(engine, "proj-a", "feature", "C")
    handoff_work(
        engine, "proj-a", prompt="seed", created_by=WHO,
        item_ids=[a["id"], b["id"]],
    )
    resolve_item(engine, a["id"])
    out = handoff_work(
        engine, "proj-a", prompt="next", created_by=WHO,
        add_item_ids=[c["id"]],
    )
    assert out["sprint"]["open_size"] == 2
    assert out["sprint"]["items"] == sorted([b["id"], c["id"]])
    assert "size" not in out["sprint"]
    snap = get_context_snapshot(engine, "proj-a", include=["sprint"])
    assert snap["sprint"]["active_sprints"][0]["size"] == 3


def test_handoff_work_item_ids_none_touches_nothing(engine, proj):
    """K-7: None means ZERO sprint mutation — no auto-created sprint either."""
    out = handoff_work(engine, "proj-a", prompt="Note only", created_by=WHO)
    assert out["sprint"] is None
    assert get_current_sprint(engine, "proj-a") is None


def test_handoff_work_empty_replace_rejected(engine, proj):
    """K-27(b): empty replace is not a thing — emptying is clear_sprint's job,
    and delimiter garbage must never silently wipe a sprint."""
    a = create_item(engine, "proj-a", "feature", "A")
    set_current_sprint_items(engine, "proj-a", [a["id"]])
    with pytest.raises(ValueError, match="Empty replace is not supported"):
        handoff_work(engine, "proj-a", prompt="Wrap", created_by=WHO, item_ids=[])
    cur = get_current_sprint(engine, "proj-a")
    assert cur is not None
    assert [m["id"] for m in list_items_in_sprint(engine, cur["id"])] == [a["id"]]
    assert list_handoffs(engine, "proj-a") == []  # nothing written at all


def test_handoff_work_replace_keeps_terminal_members(engine, proj):
    """K-24 (ADR #57): a session-end replace swaps OPEN members only —
    resolved members are the sprint's history and completion survives."""
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    c = create_item(engine, "proj-a", "feature", "C")
    set_current_sprint_items(engine, "proj-a", [a["id"], b["id"]])
    resolve_item(engine, a["id"], resolution="done")

    out = handoff_work(
        engine, "proj-a", prompt="Replan onto B+C", created_by=WHO,
        item_ids=[b["id"], c["id"]],
    )
    assert out["sprint"]["items"] == sorted([b["id"], c["id"]])  # open view

    snap = get_context_snapshot(engine, "proj-a")
    assert snap["summary"]["sprint_completion"] == "1/3"  # a still counted
    cur = get_current_sprint(engine, "proj-a")
    members = {m["id"] for m in list_items_in_sprint(engine, cur["id"])}
    assert members == {a["id"], b["id"], c["id"]}


def test_handoff_work_additive_add_and_remove(engine, proj):
    """ADR #57: the additive session edge — add pulls items in, remove cuts
    open scope, both in the same transaction as the handoff."""
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    c = create_item(engine, "proj-a", "feature", "C")
    set_current_sprint_items(engine, "proj-a", [a["id"], b["id"]])

    out = handoff_work(
        engine, "proj-a", prompt="Pull C in, cut B", created_by=WHO,
        add_item_ids=[c["id"]], remove_item_ids=[b["id"]],
    )
    assert out["sprint"]["items"] == sorted([a["id"], c["id"]])


def test_handoff_work_remove_terminal_member_rejected(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    set_current_sprint_items(engine, "proj-a", [a["id"]])
    resolve_item(engine, a["id"], resolution="done")
    with pytest.raises(ValueError, match="terminal member"):
        handoff_work(
            engine, "proj-a", prompt="Cut", created_by=WHO,
            remove_item_ids=[a["id"]],
        )
    assert list_handoffs(engine, "proj-a") == []


def test_handoff_work_replace_and_additive_mutually_exclusive(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    with pytest.raises(ValueError, match="cannot be combined"):
        handoff_work(
            engine, "proj-a", prompt="P", created_by=WHO,
            item_ids=[a["id"]], add_item_ids=[a["id"]],
        )


def test_handoff_work_selector_without_membership_op_rejected(engine, proj):
    """K-29: a typo'd selector must not silently no-op."""
    create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    with pytest.raises(ValueError, match="no membership change"):
        handoff_work(engine, "proj-a", prompt="P", created_by=WHO, sprint="s1")
    assert list_handoffs(engine, "proj-a") == []


def test_handoff_work_remove_only_without_active_sprint_rejected(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    with pytest.raises(ValueError, match="no active sprint"):
        handoff_work(
            engine, "proj-a", prompt="P", created_by=WHO,
            remove_item_ids=[a["id"]],
        )


def test_sprint_selector_id_code_collision_rejected(engine, proj):
    """K-25: a numeric code must never shadow another sprint's id (or vice
    versa) — collision is an error, not a silent pick."""
    a = create_item(engine, "proj-a", "feature", "A")
    s1 = create_sprint(engine, "proj-a", "First", status="active")
    create_sprint(engine, "proj-a", "Second", code=str(s1["id"]), status="active")
    with pytest.raises(ValueError, match="ambiguous"):
        handoff_work(
            engine, "proj-a", prompt="P", created_by=WHO,
            item_ids=[a["id"]], sprint=str(s1["id"]),
        )


def test_sprint_selector_rejects_non_decimal_digits(engine, proj):
    """K-25: '²'.isdigit() is True but int() rejects it — must yield the
    contract error, not a raw int() traceback; '٥' must not silently parse."""
    a = create_item(engine, "proj-a", "feature", "A")
    create_sprint(engine, "proj-a", "S", code="s", status="active")
    for weird in ("²", "٥"):
        with pytest.raises(ValueError, match="not an active sprint"):
            handoff_work(
                engine, "proj-a", prompt="P", created_by=WHO,
                item_ids=[a["id"]], sprint=weird,
            )


def test_assign_item_to_sprint_rejects_cross_project(engine, proj, other):
    """K-26: cross-project links are the leak vector — refused at the source."""
    from dct.core.sprints import assign_item_to_sprint
    foreign = create_item(engine, "proj-b", "feature", "Foreign secret")
    s = create_sprint(engine, "proj-a", "S", status="active")
    with pytest.raises(ValueError, match="different projects"):
        assign_item_to_sprint(engine, foreign["id"], s["id"])


def test_snapshot_excludes_legacy_cross_project_links(engine, proj, other):
    """K-26: a poisoned link planted before validation existed must not leak
    the foreign item into this project's snapshot or counters."""
    from dct.db import item_sprints as links
    foreign = create_item(engine, "proj-b", "feature", "SECRET foreign title")
    s = create_sprint(engine, "proj-a", "S", status="active")
    mine = create_item(engine, "proj-a", "feature", "Mine")
    set_current_sprint_items(engine, "proj-a", [mine["id"]])
    with engine.begin() as conn:  # simulate a pre-fix legacy row
        conn.execute(links.insert().values(item_id=foreign["id"], sprint_id=s["id"]))

    snap = get_context_snapshot(engine, "proj-a")
    ids = [r[0] for r in snap["sprint"]["items"]["rows"]]
    assert foreign["id"] not in ids
    assert snap["summary"]["sprint_size"] == 1
    assert snap["sprint"]["active_sprints"][0]["size"] == 1


def test_clear_sprint_keeps_terminal_members(engine, proj):
    """ADR #57 consistency: even an explicit clear removes only OPEN members."""
    from dct.core.sprints import clear_current_sprint
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    set_current_sprint_items(engine, "proj-a", [a["id"], b["id"]])
    resolve_item(engine, a["id"], resolution="done")

    out = clear_current_sprint(engine, "proj-a")
    assert out["cleared"] == 1  # only the open member
    cur = get_current_sprint(engine, "proj-a")
    members = {m["id"] for m in list_items_in_sprint(engine, cur["id"])}
    assert members == {a["id"]}  # history retained


def test_handoff_work_unknown_item_ids_roll_everything_back(engine, proj):
    """K-8: a bad id aborts the WHOLE op — no sprint, no membership, no handoff."""
    a = create_item(engine, "proj-a", "feature", "A")
    with pytest.raises(ValueError, match=r"Unknown item id\(s\).*999999"):
        handoff_work(
            engine, "proj-a", prompt="Broken", created_by=WHO,
            item_ids=[a["id"], 999999],
        )
    assert list_handoffs(engine, "proj-a") == []
    assert get_current_sprint(engine, "proj-a") is None


def test_handoff_work_atomicity_injection(engine, proj, monkeypatch):
    """THE load-bearing test: if the handoff insert blows up, the sprint write
    must roll back — membership exactly as before, zero handoff rows."""
    import dct.core.handoff as handoff_mod

    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    set_current_sprint_items(engine, "proj-a", [a["id"]])
    before_sprint = get_current_sprint(engine, "proj-a")
    assert before_sprint is not None

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(handoff_mod, "_create_handoff_conn", boom)
    with pytest.raises(RuntimeError, match="injected failure"):
        handoff_work(
            engine, "proj-a", prompt="Doomed", created_by=WHO, item_ids=[b["id"]],
        )

    members = [m["id"] for m in list_items_in_sprint(engine, before_sprint["id"])]
    assert members == [a["id"]]  # membership swap rolled back
    assert list_handoffs(engine, "proj-a") == []


def test_handoff_work_blank_prompt_rolls_back_sprint(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    with pytest.raises(ValueError, match="prompt cannot be empty"):
        handoff_work(engine, "proj-a", prompt="   ", created_by=WHO, item_ids=[a["id"]])
    assert get_current_sprint(engine, "proj-a") is None  # auto-create rolled back
    assert list_handoffs(engine, "proj-a") == []


def test_handoff_work_blank_created_by_raises(engine, proj):
    with pytest.raises(ValueError, match="created_by cannot be empty"):
        handoff_work(engine, "proj-a", prompt="P", created_by="  ")


def test_handoff_work_two_active_sprints_requires_selector(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    create_sprint(engine, "proj-a", "S2", code="s2", status="active")
    with pytest.raises(ValueError, match="2 active sprints — pass sprint="):
        handoff_work(engine, "proj-a", prompt="P", created_by=WHO, item_ids=[a["id"]])


def test_handoff_work_sprint_selector_by_code_and_id(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    s2 = create_sprint(engine, "proj-a", "S2", code="s2", status="active")

    out1 = handoff_work(
        engine, "proj-a", prompt="Into s1", created_by=WHO,
        item_ids=[a["id"]], sprint="s1",
    )
    assert out1["sprint"]["sprint_id"] == s1["id"] and out1["sprint"]["code"] == "s1"

    out2 = handoff_work(
        engine, "proj-a", prompt="Into s2", created_by=WHO,
        item_ids=[b["id"]], sprint=str(s2["id"]),
    )
    assert out2["sprint"]["sprint_id"] == s2["id"]
    assert [m["id"] for m in list_items_in_sprint(engine, s1["id"])] == [a["id"]]
    assert [m["id"] for m in list_items_in_sprint(engine, s2["id"])] == [b["id"]]


def test_handoff_work_sprint_selector_not_active_raises(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    create_sprint(engine, "proj-a", "Planned", code="pl")  # planned, not active
    with pytest.raises(ValueError, match="not an active sprint"):
        handoff_work(
            engine, "proj-a", prompt="P", created_by=WHO,
            item_ids=[a["id"]], sprint="pl",
        )


def test_handoff_work_single_active_is_default_target(engine, proj):
    a = create_item(engine, "proj-a", "feature", "A")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    out = handoff_work(
        engine, "proj-a", prompt="P", created_by=WHO, item_ids=[a["id"]],
    )
    assert out["sprint"]["sprint_id"] == s1["id"]


# ── regression + adversarial ───────────────────────────────────────────────────

def test_review_sprint_still_returns_full_prompts(engine, proj):
    """Deliberately unchanged contract: review_sprint dumps FULL prompts for
    every unconsumed handoff — only the new snapshot previews the tail."""
    from dct.core.sprint import get_sprint_review
    _mk_handoff(engine, prompt="z" * 5000)
    review = get_sprint_review(engine, "proj-a")
    tbl = review["unconsumed_handoffs"]
    assert tbl["rows"][0][tbl["cols"].index("prompt")] == "z" * 5000


def test_other_projects_state_never_leaks(engine, proj, other):
    create_handoff(engine, "proj-b", "Foreign", WHO)
    foreign_item = create_item(engine, "proj-b", "feature", "F")
    set_current_sprint_items(engine, "proj-b", [foreign_item["id"]])

    snap = get_context_snapshot(engine, "proj-a")
    assert snap["handoff"]["pending_count"] == 0
    assert snap["sprint"]["items"]["rows"] == []
    out = pickup_work(engine, "proj-a", consumed_by=WHO)
    assert out["picked_up"] is None


def test_100k_prompt_latest_untruncated_others_previewed(engine, proj):
    big = "A" * 100_000
    older = _mk_handoff(engine, prompt=big)
    _mk_handoff(engine, prompt="tiny newer")
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["handoff"]["latest"]["prompt"] == "tiny newer"
    others = snap["handoff"]["others"]
    preview = others["rows"][0][others["cols"].index("prompt")]
    assert others["rows"][0][others["cols"].index("id")] == older["id"]
    assert len(preview) == 200


def test_pickup_resolved_sprint_shows_completion(engine, proj):
    """End-to-end: resolve one of two members, pickup shows 1/2 and only the
    open item remains in the view (K-18 wired through the snapshot)."""
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    set_current_sprint_items(engine, "proj-a", [a["id"], b["id"]])
    resolve_item(engine, a["id"], resolution="done")
    assert get_item(engine, a["id"])["in_current_sprint"] is False

    out = pickup_work(engine, "proj-a", consume=False)
    assert out["summary"]["sprint_completion"] == "1/2"
    ids = [r[0] for r in out["sprint"]["items"]["rows"]]
    assert ids == [b["id"]]


# ── K-34: snapshot row caps ───────────────────────────────────────────────────

def test_handoff_others_capped_and_flagged_truncated(engine, proj):
    """25 unconsumed handoffs → `others` carries the cap (20) and says so;
    pending_count still reports the REAL total, so nothing is hidden."""
    from dct.workflows.session import SNAPSHOT_CAPS

    cap = SNAPSHOT_CAPS["handoff_others"]
    for i in range(25):
        _mk_handoff(engine, prompt=f"handoff {i}")

    snap = get_context_snapshot(engine, "proj-a")
    others = snap["handoff"]["others"]
    assert len(others["rows"]) == cap
    assert others["truncated"] is True
    # The counters are computed over the full set (25 = 1 latest + 24 others).
    assert snap["handoff"]["pending_count"] == 25
    assert snap["summary"]["handoffs_pending"] == 25


def test_uncapped_tables_report_truncated_false(engine, proj):
    """Below every cap: the flag is present and False — a reader can always
    ask `truncated` without guessing whether the key exists."""
    a = create_item(engine, "proj-a", "feature", "A")
    add_checkpoint(engine, a["id"], "Step", kind="dogfood")
    set_current_sprint_items(engine, "proj-a", [a["id"]])
    _mk_handoff(engine)
    _mk_handoff(engine)

    snap = get_context_snapshot(engine, "proj-a")
    assert snap["handoff"]["others"]["truncated"] is False
    assert snap["sprint"]["items"]["truncated"] is False
    assert snap["sprint"]["checkpoints"]["truncated"] is False
    assert snap["sprint"]["stale_in_progress"]["truncated"] is False
    assert snap["gates"]["truncated"] is False


def test_handoff_latest_is_never_capped(engine, proj):
    """K-11 product decision: the caps must not touch the full prompt."""
    for i in range(25):
        _mk_handoff(engine, prompt=f"h{i}")
    newest = _mk_handoff(engine, prompt="B" * 50_000)
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["handoff"]["latest"]["id"] == newest["id"]
    assert snap["handoff"]["latest"]["prompt"] == "B" * 50_000


# ── K-36: JSON-serializable payloads ──────────────────────────────────────────

def test_snapshot_is_json_serializable(engine, proj):
    import json

    a = create_item(engine, "proj-a", "feature", "A")
    add_checkpoint(engine, a["id"], "Gate me", kind="dogfood")
    create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    set_current_sprint_items(engine, "proj-a", [a["id"]])
    _mk_handoff(engine)

    snap = get_context_snapshot(engine, "proj-a")
    json.dumps(snap)  # must not raise — no `default=` hook allowed (K-36)


def test_pickup_with_consumed_handoff_is_json_serializable(engine, proj):
    """picked_up carries consumed_at/created_at — the datetime trap that made
    the MCP surface non-serializable."""
    import json

    a = create_item(engine, "proj-a", "feature", "A")
    set_current_sprint_items(engine, "proj-a", [a["id"]])
    _mk_handoff(engine)

    out = pickup_work(engine, "proj-a", consume=True, consumed_by=WHO)
    assert out["consumed"] is True
    json.dumps(out)
    # ISO strings, not datetimes — and the value round-trips unchanged.
    assert isinstance(out["picked_up"]["consumed_at"], str)
    assert json.loads(json.dumps(out))["picked_up"]["id"] == out["picked_up"]["id"]


def test_active_sprint_started_at_is_isoformat_string(engine, proj):
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    snap = get_context_snapshot(engine, "proj-a")
    started = snap["sprint"]["active_sprints"][0]["started_at"]
    assert snap["sprint"]["active_sprints"][0]["id"] == s1["id"]
    assert isinstance(started, str) and started.startswith("20")


# ── K-37: one scope reader for both consume paths ─────────────────────────────

def test_consume_true_honours_scope_via_shared_reader(engine, proj):
    """consume=True must pick the newest handoff OF THE SCOPE, not the newest
    overall — both consume paths read the same scope-filtered candidate list,
    so dropping the SQL filter fails here AND in the auto test below."""
    want = _mk_handoff(engine, prompt="g1 work", scope="g1")
    _mk_handoff(engine, prompt="unrelated newer", scope="other")

    out = pickup_work(engine, "proj-a", consume=True, scope="g1", consumed_by=WHO)
    assert out["picked_up"]["id"] == want["id"]
    assert out["picked_up"]["scope"] == "g1"


def test_auto_consume_honours_scope_via_shared_reader(engine, proj):
    """Two unconsumed handoffs but only ONE in scope → auto consumes it."""
    want = _mk_handoff(engine, prompt="g1 work", scope="g1")
    _mk_handoff(engine, prompt="other work", scope="other")

    out = pickup_work(engine, "proj-a", scope="g1", consumed_by=WHO)
    assert out["consumed"] is True
    assert out["picked_up"]["id"] == want["id"]


def test_consume_true_with_no_scope_match_raises(engine, proj):
    _mk_handoff(engine, scope="other")
    with pytest.raises(ValueError, match="No unconsumed handoff with scope"):
        pickup_work(engine, "proj-a", consume=True, scope="g1", consumed_by=WHO)


def test_scope_matched_zero_vs_many_pending(engine, proj):
    """scope_matched separates 'nothing for my workstream' (0) from 'several
    pending, pick one' — pending_count alone cannot express that."""
    _mk_handoff(engine, scope="other")
    _mk_handoff(engine, scope="other")

    out = pickup_work(engine, "proj-a", scope="g1", consumed_by=WHO)
    assert out["consumed"] is False
    assert out["handoff"]["scope_matched"] == 0
    assert out["handoff"]["pending_count"] == 2  # busy project, none for me

    _mk_handoff(engine, scope="g1")
    _mk_handoff(engine, scope="g1")
    out2 = pickup_work(engine, "proj-a", scope="g1", consumed_by=WHO)
    assert out2["consumed"] is False  # ambiguous — the human picks
    assert out2["handoff"]["scope_matched"] == 2


def test_scope_matched_absent_without_scope(engine, proj):
    _mk_handoff(engine)
    out = pickup_work(engine, "proj-a", consume=False)
    assert "scope_matched" not in out["handoff"]


def test_scope_matched_counts_post_consume(engine, proj):
    _mk_handoff(engine, scope="g1")
    out = pickup_work(engine, "proj-a", consume=True, scope="g1", consumed_by=WHO)
    assert out["consumed"] is True
    assert out["handoff"]["scope_matched"] == 0


# ── K-41: handoff_id makes scope irrelevant EVERYWHERE ────────────────────────

def test_handoff_id_ignores_scope_in_snapshot_too(engine, proj):
    """The docstring promises "scope is then ignored" — that must hold for the
    returned handoff.latest, not only for candidate selection."""
    target = _mk_handoff(engine, prompt="explicit target", scope="other")
    leftover = _mk_handoff(engine, prompt="newest overall", scope="misc")

    out = pickup_work(
        engine, "proj-a", handoff_id=target["id"], scope="g1", consumed_by=WHO,
    )
    assert out["picked_up"]["id"] == target["id"]
    # latest = newest remaining UNSCOPED handoff, not "newest with scope g1"
    assert out["handoff"]["latest"]["id"] == leftover["id"]
    assert "scope_matched" not in out["handoff"]


# ── K-38: per-sprint size/done vs the DISTINCT summary ────────────────────────

def test_active_sprint_sizes_count_membership_per_sprint(engine, proj):
    """An item in TWO active sprints counts once in EACH sprint's size, but
    only ONCE in summary.sprint_size (DISTINCT items)."""
    from dct.core.sprints import assign_item_to_sprint

    shared = create_item(engine, "proj-a", "feature", "Shared")
    only1 = create_item(engine, "proj-a", "feature", "Only in S1")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    s2 = create_sprint(engine, "proj-a", "S2", code="s2", status="active")
    assign_item_to_sprint(engine, shared["id"], s1["id"])
    assign_item_to_sprint(engine, shared["id"], s2["id"])
    assign_item_to_sprint(engine, only1["id"], s1["id"])

    snap = get_context_snapshot(engine, "proj-a")
    by_id = {s["id"]: s for s in snap["sprint"]["active_sprints"]}
    assert by_id[s1["id"]]["size"] == 2 and by_id[s1["id"]]["done"] == 0
    assert by_id[s2["id"]]["size"] == 1 and by_id[s2["id"]]["done"] == 0
    # 2 DISTINCT items, not 2 + 1.
    assert snap["summary"]["sprint_size"] == 2
    assert snap["summary"]["sprint_completion"] == "0/2"


def test_active_sprint_done_counts_terminal_members(engine, proj):
    """A resolved member stays linked (ADR #57) and shows up as done=1."""
    a = create_item(engine, "proj-a", "feature", "A")
    b = create_item(engine, "proj-a", "feature", "B")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    set_current_sprint_items(engine, "proj-a", [a["id"], b["id"]])
    resolve_item(engine, a["id"], resolution="shipped")

    snap = get_context_snapshot(engine, "proj-a")
    entry = next(s for s in snap["sprint"]["active_sprints"] if s["id"] == s1["id"])
    assert entry["size"] == 2 and entry["done"] == 1
    assert snap["summary"]["sprint_completion"] == "1/2"
    # ... while the items table shows only the OPEN member.
    assert [r[0] for r in snap["sprint"]["items"]["rows"]] == [b["id"]]


# ── K-41: include='none' cannot be combined ───────────────────────────────────

def test_include_none_combined_with_sections_raises(engine, proj):
    with pytest.raises(ValueError, match="'none' must be used alone"):
        get_context_snapshot(engine, "proj-a", include=["none", "sprint"])


def test_include_none_combined_raises_in_pickup_too(engine, proj):
    with pytest.raises(ValueError, match="'none' must be used alone"):
        pickup_work(engine, "proj-a", consume=False, include=["sprint", "none"])


# ── K-21: the consume/snapshot transaction boundary ───────────────────────────

def test_pickup_consume_rolls_back_when_snapshot_assembly_fails(engine, proj, monkeypatch):
    """THE K-21 hole: the consume UPDATE lands BEFORE the snapshot is built, so
    any failure during assembly must undo it. An invalid `since` blows up in
    _build_snapshot's first statement — the handoff has to survive UNCONSUMED,
    or a session start would silently eat the prompt it failed to return.

    The spy makes the test non-vacuous: it proves the write really happened and
    was rolled back, rather than never having been attempted."""
    from dct.core import handoff as handoff_mod

    h = _mk_handoff(engine, prompt="Must survive the crash")
    real = handoff_mod._consume_handoff_conn
    consumed: list[dict] = []

    def spy(conn, handoff_id, consumed_by=None):
        out = real(conn, handoff_id, consumed_by)
        consumed.append({"id": handoff_id, "row": out})
        return out

    monkeypatch.setattr(handoff_mod, "_consume_handoff_conn", spy)

    with pytest.raises(ValueError, match="Invalid `since`"):
        pickup_work(engine, "proj-a", consume=True, consumed_by=WHO, since="garbage")

    # The UPDATE ran and SUCCEEDED inside the transaction ...
    assert [c["id"] for c in consumed] == [h["id"]]
    assert consumed[0]["row"]["consumed_at"] is not None
    # ... and the transaction took it back out.
    still = list_handoffs(engine, "proj-a", unconsumed_only=True)
    assert [r["id"] for r in still] == [h["id"]]
    assert still[0]["consumed_at"] is None and still[0]["consumed_by"] is None


def test_pickup_auto_consume_rolls_back_on_snapshot_failure(engine, proj):
    """Same boundary on the AUTO path (single candidate, no explicit id)."""
    h = _mk_handoff(engine)
    with pytest.raises(ValueError, match="Invalid `since`"):
        pickup_work(engine, "proj-a", consumed_by=WHO, since="not-a-date")
    assert list_handoffs(engine, "proj-a", unconsumed_only=True)[0]["id"] == h["id"]


def test_pickup_explicit_id_consume_rolls_back_on_snapshot_failure(engine, proj):
    h = _mk_handoff(engine)
    with pytest.raises(ValueError, match="Invalid `since`"):
        pickup_work(
            engine, "proj-a", handoff_id=h["id"], consumed_by=WHO, since="7d",
        )
    assert list_handoffs(engine, "proj-a", unconsumed_only=True)[0]["id"] == h["id"]


def test_pickup_invalid_since_is_a_pure_read_failure(engine, proj):
    """consume=false + bad since: still an error, and still nothing written."""
    _mk_handoff(engine)
    with pytest.raises(ValueError, match="Invalid `since`"):
        pickup_work(engine, "proj-a", consume=False, since="garbage")
    assert len(list_handoffs(engine, "proj-a", unconsumed_only=True)) == 1


def test_snapshot_invalid_since_raises_naming_the_vocabulary(engine, proj):
    with pytest.raises(ValueError, match=r"Invalid `since`: 'garbage'"):
        get_context_snapshot(engine, "proj-a", since="garbage")


@pytest.mark.parametrize("since", ["7d", "24 h", "yesterday", "2026-13-45", "-1h"])
def test_snapshot_rejects_unsupported_since_forms(engine, proj, since):
    """'7d' is the trap the CLI help used to advertise; the rest are the shapes
    a model guesses at. All must error, none may silently mean 'all time'."""
    with pytest.raises(ValueError, match="Invalid `since`"):
        get_context_snapshot(engine, "proj-a", since=since)


# ── K-32: _consume_handoff_conn on ONE connection ─────────────────────────────

def test_consume_handoff_conn_returns_none_when_soft_deleted(engine, proj):
    """Contract of the single-connection consume: a row that is gone (or
    already consumed) yields None instead of a half-written dict."""
    from dct.core.handoff import _consume_handoff_conn

    h = _mk_handoff(engine)
    with engine.begin() as conn:
        conn.execute(
            session_handoffs.update()
            .where(session_handoffs.c.id == h["id"])
            .values(deleted_at=datetime.now(UTC))
        )
        assert _consume_handoff_conn(conn, h["id"], WHO) is None


def test_consume_handoff_conn_reads_its_own_uncommitted_write(engine, proj):
    """The reason the helper collapsed to ONE connection: the re-read must see
    the UPDATE from the same (still-open) transaction, so a composed op can
    consume inside its own txn."""
    from dct.core.handoff import _consume_handoff_conn

    h = _mk_handoff(engine)
    with engine.begin() as conn:
        got = _consume_handoff_conn(conn, h["id"], WHO)
        assert got is not None
        assert got["id"] == h["id"] and got["consumed_by"] == WHO
        assert got["consumed_at"] is not None
        # Second consume on the same conn is a no-op — the guard is in the WHERE.
        assert _consume_handoff_conn(conn, h["id"], "someone else") is None


def _delete_then_consume(monkeypatch):
    """Patch the consume helper so the target row is soft-deleted on the SAME
    connection just before the real UPDATE runs — the concurrent-session race
    (a sibling deleted/consumed the handoff between select and update),
    reproduced deterministically."""
    from dct.core import handoff as handoff_mod

    real = handoff_mod._consume_handoff_conn

    def racing(conn, handoff_id, consumed_by=None):
        conn.execute(
            session_handoffs.update()
            .where(session_handoffs.c.id == handoff_id)
            .values(deleted_at=datetime.now(UTC))
        )
        return real(conn, handoff_id, consumed_by)

    monkeypatch.setattr(handoff_mod, "_consume_handoff_conn", racing)


def test_pickup_auto_degrades_when_target_races_away(engine, proj, monkeypatch):
    """K-32: a server-CHOSEN target that vanishes mid-flight must not fail the
    session start — pickup degrades to a plain read of the post-race state."""
    _mk_handoff(engine)
    _delete_then_consume(monkeypatch)

    out = pickup_work(engine, "proj-a", consumed_by=WHO)

    assert out["consumed"] is False and out["picked_up"] is None
    assert out["handoff"]["latest"] is None      # it really is gone
    assert out["handoff"]["pending_count"] == 0
    assert out["summary"]["handoffs_pending"] == 0


def test_pickup_consume_true_degrades_when_target_races_away(engine, proj, monkeypatch):
    """consume=true also picks the target itself → same degradation, no error."""
    _mk_handoff(engine)
    _delete_then_consume(monkeypatch)
    out = pickup_work(engine, "proj-a", consume=True, consumed_by=WHO)
    assert out["consumed"] is False and out["picked_up"] is None


def test_pickup_explicit_id_errors_when_target_races_away(engine, proj, monkeypatch):
    """An EXPLICIT handoff_id asked for THAT row — a race is an error, and the
    whole transaction (the racing delete included) rolls back."""
    h = _mk_handoff(engine)
    _delete_then_consume(monkeypatch)

    with pytest.raises(ValueError, match=f"Handoff #{h['id']} not found or already consumed"):
        pickup_work(engine, "proj-a", handoff_id=h["id"], consumed_by=WHO)

    survivors = list_handoffs(engine, "proj-a", unconsumed_only=True)
    assert [r["id"] for r in survivors] == [h["id"]]


# ── K-38: exact per-sprint size/done VALUES ───────────────────────────────────

def test_active_sprint_sizes_exact_tuples_with_one_resolved(engine, proj):
    """Two active sprints, DISJOINT members, one member resolved: every
    (size, done) pair is asserted exactly, and the DISTINCT summary agrees."""
    from dct.core.sprints import assign_item_to_sprint

    a1 = create_item(engine, "proj-a", "feature", "A1")
    a2 = create_item(engine, "proj-a", "feature", "A2")
    b1 = create_item(engine, "proj-a", "feature", "B1")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    s2 = create_sprint(engine, "proj-a", "S2", code="s2", status="active")
    assign_item_to_sprint(engine, a1["id"], s1["id"])
    assign_item_to_sprint(engine, a2["id"], s1["id"])
    assign_item_to_sprint(engine, b1["id"], s2["id"])
    resolve_item(engine, a1["id"], resolution="shipped")

    snap = get_context_snapshot(engine, "proj-a")
    by_id = {s["id"]: (s["size"], s["done"]) for s in snap["sprint"]["active_sprints"]}
    assert by_id == {s1["id"]: (2, 1), s2["id"]: (1, 0)}
    assert snap["summary"]["sprint_size"] == 3
    assert snap["summary"]["sprint_completion"] == "1/3"
    assert snap["summary"]["active_sprint_count"] == 2
    # The open-work table drops the resolved member from BOTH views.
    assert {r[0] for r in snap["sprint"]["items"]["rows"]} == {a2["id"], b1["id"]}


def test_shared_member_resolved_counts_done_in_every_sprint(engine, proj):
    """K-38 hard case: one item in TWO active sprints, resolved. It is `done`
    for EACH sprint (per-sprint membership) yet contributes ONE unit to the
    DISTINCT summary — the per-sprint tuples must not be summed."""
    from dct.core.sprints import assign_item_to_sprint

    shared = create_item(engine, "proj-a", "feature", "Shared")
    only2 = create_item(engine, "proj-a", "feature", "Only in S2")
    s1 = create_sprint(engine, "proj-a", "S1", code="s1", status="active")
    s2 = create_sprint(engine, "proj-a", "S2", code="s2", status="active")
    assign_item_to_sprint(engine, shared["id"], s1["id"])
    assign_item_to_sprint(engine, shared["id"], s2["id"])
    assign_item_to_sprint(engine, only2["id"], s2["id"])
    resolve_item(engine, shared["id"], resolution="done")

    snap = get_context_snapshot(engine, "proj-a")
    by_id = {s["id"]: (s["size"], s["done"]) for s in snap["sprint"]["active_sprints"]}
    assert by_id == {s1["id"]: (1, 1), s2["id"]: (2, 1)}
    # 2 DISTINCT items (shared + only2), 1 of them terminal — NOT 3 and NOT 2/3.
    assert snap["summary"]["sprint_size"] == 2
    assert snap["summary"]["sprint_completion"] == "1/2"
    assert [r[0] for r in snap["sprint"]["items"]["rows"]] == [only2["id"]]


# ── stale_in_progress: the 7-day boundary ─────────────────────────────────────

def _age_item(engine, item_id, days):
    """Backdate updated_at directly — every core write bumps it to now."""
    from dct.db import items as items_tbl

    with engine.begin() as conn:
        conn.execute(
            items_tbl.update()
            .where(items_tbl.c.id == item_id)
            .values(updated_at=datetime.now(UTC) - timedelta(days=days))
        )


def test_stale_in_progress_boundary_eight_days_in_six_days_out(engine, proj):
    """STALE_DAYS = 7: an in_progress item untouched for 8 days is stale, one
    touched 6 days ago is not — and stale_count matches the table exactly."""
    from dct.core.items import update_item

    old = create_item(engine, "proj-a", "todo", "Forgotten WIP")
    fresh = create_item(engine, "proj-a", "todo", "Recent WIP")
    update_item(engine, old["id"], status="in_progress")
    update_item(engine, fresh["id"], status="in_progress")
    _age_item(engine, old["id"], 8)
    _age_item(engine, fresh["id"], 6)

    snap = get_context_snapshot(engine, "proj-a")
    tbl = snap["sprint"]["stale_in_progress"]
    rows = [dict(zip(tbl["cols"], r)) for r in tbl["rows"]]
    assert [r["id"] for r in rows] == [old["id"]]
    assert rows[0]["title"] == "Forgotten WIP"
    assert isinstance(rows[0]["updated_at"], str)   # JSON-native (K-36)
    assert snap["summary"]["stale_count"] == 1
    assert tbl["truncated"] is False


def test_stale_ignores_non_in_progress_items(engine, proj):
    """Only in_progress rots: an ancient `open` item is backlog, not stale, and
    an ancient resolved one is history."""
    from dct.core.items import update_item

    backlog = create_item(engine, "proj-a", "todo", "Old backlog")
    shipped = create_item(engine, "proj-a", "todo", "Old and done")
    wip = create_item(engine, "proj-a", "todo", "Old WIP")
    update_item(engine, wip["id"], status="in_progress")
    resolve_item(engine, shipped["id"], resolution="done")
    for i in (backlog, shipped, wip):
        _age_item(engine, i["id"], 30)

    snap = get_context_snapshot(engine, "proj-a")
    assert [r[0] for r in snap["sprint"]["stale_in_progress"]["rows"]] == [wip["id"]]
    assert snap["summary"]["stale_count"] == 1


def test_stale_surfaces_through_pickup_not_only_review(engine, proj):
    """The stale list is a session-start signal — it must be reachable from the
    composed op, not only from the legacy review_sprint dashboard."""
    from dct.core.items import update_item
    from dct.core.sprint import get_sprint_review

    wip = create_item(engine, "proj-a", "todo", "Rotting")
    update_item(engine, wip["id"], status="in_progress")
    _age_item(engine, wip["id"], 9)

    out = pickup_work(engine, "proj-a", consume=False)
    assert [r[0] for r in out["sprint"]["stale_in_progress"]["rows"]] == [wip["id"]]
    assert out["summary"]["stale_count"] == 1
    # Same fact, same number, on the legacy surface — one query, no drift.
    review = get_sprint_review(engine, "proj-a")
    assert [r[0] for r in review["stale_in_progress"]["rows"]] == [wip["id"]]


def test_stale_count_absent_section_still_counted(engine, proj):
    """The counter is computed over the FULL set regardless of `include` — a
    summary-only call must still report the magnitude."""
    from dct.core.items import update_item

    wip = create_item(engine, "proj-a", "todo", "Rotting")
    update_item(engine, wip["id"], status="in_progress")
    _age_item(engine, wip["id"], 10)

    snap = get_context_snapshot(engine, "proj-a", include=["none"])
    assert "sprint" not in snap
    assert snap["summary"]["stale_count"] == 1


# ── K-3: scope with no match still shows every pending handoff ────────────────

def test_pickup_scope_no_match_lists_all_unconsumed_in_others(engine, proj):
    """latest is None (nothing in MY workstream) — `others` must then list ALL
    unconsumed handoffs, so the scope filter hides nothing from the reader."""
    a = _mk_handoff(engine, prompt="alpha work", scope="alpha")
    b = _mk_handoff(engine, prompt="beta work", scope="beta")
    c = _mk_handoff(engine, prompt="unscoped work")

    out = pickup_work(engine, "proj-a", scope="g1", consumed_by=WHO)

    assert out["consumed"] is False and out["picked_up"] is None
    assert out["handoff"]["latest"] is None
    others = out["handoff"]["others"]
    assert {r[others["cols"].index("id")] for r in others["rows"]} == {
        a["id"], b["id"], c["id"]
    }
    assert out["handoff"]["scope_matched"] == 0
    assert out["handoff"]["pending_count"] == 3
    # Scope tags ride along so the reader can pick one by workstream.
    scopes = {r[others["cols"].index("scope")] for r in others["rows"]}
    assert scopes == {"alpha", "beta", None}


def test_pickup_scope_match_excludes_only_the_latest_from_others(engine, proj):
    """The complement of the case above: with a match, `others` is everything
    BUT that one row — never a duplicate, never a silent drop."""
    newest = _mk_handoff(engine, prompt="g1 newest", scope="g1")
    older = _mk_handoff(engine, prompt="g1 older", scope="g1")
    with engine.begin() as conn:  # make `newest` genuinely newest
        conn.execute(
            session_handoffs.update()
            .where(session_handoffs.c.id == older["id"])
            .values(created_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
    unrelated = _mk_handoff(engine, prompt="other work", scope="other")

    out = pickup_work(engine, "proj-a", consume=False, scope="g1")

    assert out["handoff"]["latest"]["id"] == newest["id"]
    others = out["handoff"]["others"]
    assert {r[0] for r in others["rows"]} == {older["id"], unrelated["id"]}
    assert out["handoff"]["scope_matched"] == 2   # read-only: nothing consumed


# ── review_sprint: the documented open-vs-membership divergence ───────────────

def test_review_sprint_in_sprint_is_open_only_while_summary_counts_terminal(engine, proj):
    """LOCKED divergence (K-18/K-24): `in_sprint` is the open-work view, while
    summary.sprint_size/sprint_completion count MEMBERSHIP — terminal members
    included. A refactor that "fixes" either side to match the other breaks the
    contract both the dashboard and the snapshot are built on."""
    from dct.core.sprint import get_sprint_review

    done = create_item(engine, "proj-a", "feature", "Shipped")
    doing = create_item(engine, "proj-a", "feature", "Still open")
    dropped = create_item(engine, "proj-a", "feature", "Abandoned")
    set_current_sprint_items(engine, "proj-a", [done["id"], doing["id"], dropped["id"]])
    resolve_item(engine, done["id"], resolution="shipped")
    resolve_item(engine, dropped["id"], status="wont_fix", resolution="dropped")

    review = get_sprint_review(engine, "proj-a")
    assert [r[0] for r in review["in_sprint"]["rows"]] == [doing["id"]]
    assert review["summary"]["sprint_size"] == 3
    assert review["summary"]["sprint_completion"] == "2/3"   # wont_fix IS terminal

    # The composed snapshot reports the SAME numbers from the same helpers.
    snap = get_context_snapshot(engine, "proj-a")
    assert snap["summary"]["sprint_size"] == 3
    assert snap["summary"]["sprint_completion"] == "2/3"
    assert [r[0] for r in snap["sprint"]["items"]["rows"]] == [doing["id"]]


def test_review_sprint_empty_project_reports_zero_over_zero(engine, proj):
    """Boundary: no sprint at all → '0/0', never a ZeroDivision or '0/'."""
    from dct.core.sprint import get_sprint_review

    review = get_sprint_review(engine, "proj-a")
    assert review["in_sprint"]["rows"] == []
    assert review["summary"] == {
        "sprint_size": 0,
        "sprint_completion": "0/0",
        "total_open_in_project": 0,
        "handoffs_pending": 0,
    }
