"""Tests for dct/web/changes.py change_token and the SSE event generator.

change_token must be a cheap composite that:
  - is stable across a no-op (same DB state -> identical token),
  - moves after an INSERT (live row-count half),
  - moves after a SOFT-DELETE (live row-count half drops),
  - moves after an UPDATE (max(updated_at) half advances).

The SSE generator is tested without a live HTTP stream: a fake token source
drives it and we assert a 'stale' frame is emitted exactly when the token changes.
"""

from dct.web.changes import change_token
from dct.web.blueprints.events import _sse_events


def test_change_token_stable_across_noop(engine, seed_project):
    assert change_token(engine) == change_token(engine)


def test_change_token_changes_after_insert(engine, seed_project):
    from dct.core.items import create_item

    before = change_token(engine)
    create_item(engine, "test", "todo", "first task")
    after = change_token(engine)
    assert after != before


def test_change_token_changes_after_soft_delete(engine, seed_project):
    from dct.core.items import create_item, delete_item

    item = create_item(engine, "test", "todo", "to be removed")
    before = change_token(engine)  # captured WITH the row present
    assert delete_item(engine, item["id"]) is True
    after = change_token(engine)   # live row-count half drops by 1
    assert after != before


def test_change_token_changes_after_update(engine, seed_project):
    from dct.core.items import create_item, update_item

    item = create_item(engine, "test", "todo", "original title")
    before = change_token(engine)
    # update_item stamps updated_at = datetime.now(timezone.utc) (microsecond
    # precision), so max(updated_at) advances even within the same wall-clock second.
    update_item(engine, item["id"], title="changed title")
    after = change_token(engine)
    assert after != before


def test_sse_generator_emits_stale_only_on_change():
    # Fake token source: last=t0, then t0 (no-op), then t1 (change), then t1 (no-op).
    tokens = iter(["t0", "t0", "t1", "t1"])
    calls = {"n": 0}

    def src():
        return next(tokens)

    def cont():
        calls["n"] += 1
        return calls["n"] <= 3  # exactly three loop iterations

    frames = list(
        _sse_events(src, poll_interval=0, sleep=lambda _s: None, should_continue=cont)
    )
    joined = "".join(frames)
    assert "event: stale" in joined          # a change was observed
    assert "data: t1" in joined              # carries the new token
    assert joined.count("event: stale") == 1  # only the t0 -> t1 transition fires
