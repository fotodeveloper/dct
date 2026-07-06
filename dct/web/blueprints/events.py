"""SSE live-refresh stream and liveness probe (read-only)."""

import time

from flask import Blueprint, Response

bp = Blueprint("events", __name__)


def _sse_events(token_source, *, poll_interval, sleep=time.sleep, should_continue=None):
    """Yield SSE frames; emit an `event: stale` frame whenever the token changes.

    token_source:   zero-arg callable returning the current change token (str).
    poll_interval:  seconds to sleep between polls.
    sleep:          injectable sleep (tests pass a no-op).
    should_continue: zero-arg predicate gating the loop. Defaults to an infinite
                     stream (real request); tests pass a bounded predicate.

    Pure: no Flask / DB imports, so it is unit-testable with a fake token source.
    """
    if should_continue is None:
        should_continue = lambda: True  # noqa: E731 — infinite stream for a live request
    last = token_source()
    # Opening comment defeats proxy buffering and confirms the stream is live.
    yield ": connected\n\n"
    while should_continue():
        sleep(poll_interval)
        current = token_source()
        if current != last:
            last = current
            yield f"event: stale\ndata: {current}\n\n"
        else:
            yield ": keep-alive\n\n"


@bp.route("/events")
def stream():
    """text/event-stream: poll change_token, push a 'stale' event on change."""
    from dct.config import load_config
    from dct.web.app import current_engine
    from dct.web.changes import change_token

    engine = current_engine()  # capture in request context, before the generator runs
    web_cfg = getattr(load_config(), "web", None)
    poll_ms = getattr(web_cfg, "refresh_poll_ms", 2000)
    poll = max(int(poll_ms), 250) / 1000.0  # floor avoids hammering the DB

    # NOTE: this is an unbounded generator — with Flask threaded=True each open
    # tab pins one worker thread polling change_token() every `poll` seconds for
    # the life of the connection. Acceptable for the localhost single-user v1;
    # revisit (max lifetime / backoff) before any multi-client exposure.
    def gen():
        yield from _sse_events(lambda: change_token(engine), poll_interval=poll)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "keep-alive",
        },
    )


@bp.route("/healthz")
def healthz():
    """Liveness probe."""
    return {"status": "ok"}
