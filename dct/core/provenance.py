"""Write-path provenance context (ADR #51/#52, CP #1236).

Every mutating ledger table carries four nullable columns — actor_type,
actor_client, session_id, source — recording WHO created the row. They are
filled by SQLAlchemy Core column defaults reading the ContextVar below, so
core call sites stay untouched: each ENTRY POINT (CLI main, MCP server)
calls ``set_provenance(from_environment(...))`` exactly once.

Creation provenance only — updates never touch the columns; row-lifecycle
auditing is consciously deferred (ADR #51 consequences).

Client detection is layered for the multi-agent future:
- ``DCT_ACTOR_CLIENT`` / ``DCT_ACTOR_TYPE`` / ``DCT_SESSION_ID`` are the
  neutral self-identification surface — a client adapter (G2: codex) sets
  them in the environment of its dct registration and they always win.
- Heuristics fill the gap for hosts that don't self-identify yet:
  ``CLAUDECODE=1`` marks Claude Code's shell, ``CLAUDE_SESSION_ID`` its
  session.
- Everything unknown stays NULL — honest absence beats guessed attribution.
"""

import os
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Provenance:
    actor_type: str | None = None
    actor_client: str | None = None
    session_id: str | None = None
    source: str | None = None


_current: ContextVar[Provenance] = ContextVar(
    "dct_provenance",
    default=Provenance(),  # noqa: B039 — frozen dataclass, immutable default
)


def set_provenance(prov: Provenance) -> None:
    _current.set(prov)


def get_provenance() -> Provenance:
    return _current.get()


def from_environment(source: str) -> Provenance:
    """Best-effort provenance for an entry point (source: 'cli' | 'mcp')."""
    client = os.environ.get("DCT_ACTOR_CLIENT") or (
        "claude-code" if os.environ.get("CLAUDECODE") else None
    )
    actor_type = os.environ.get("DCT_ACTOR_TYPE") or ("agent" if client else None)
    session_id = os.environ.get("DCT_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    return Provenance(
        actor_type=actor_type,
        actor_client=client,
        session_id=session_id,
        source=source,
    )
