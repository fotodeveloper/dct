"""Append-only client task snapshots, independent of integration adapters.

Claude retains its historical append-every-call behavior. Codex records each
successful update_plan snapshot in a session AND turn scoped item: subagents can
share the host session_id, while their actual turn_id separates their plans.
Native statuses are recorded in checkpoint text, not synchronized back into DCT
statuses. The historical non-gating cc-task kind is intentionally reused.
"""

import contextlib
import json
import os

import sqlalchemy as sa

from dct.core.items import _validate_title_length
from dct.core.projects import get_project_by_path
from dct.core.provenance import Provenance, get_provenance, set_provenance
from dct.core.tags import set_item_tags_conn
from dct.db import item_checkpoints, item_notes, items, project_transaction

_MARKER = 'DCT task mirror snapshot v1\n'


@contextlib.contextmanager
def _mirror_actor(values: dict):
    """Scope provenance for shared tag helpers too; restore the caller on exit."""
    previous = get_provenance()
    try:
        if values:
            set_provenance(Provenance(**values))
        yield
    finally:
        if values:
            set_provenance(previous)


def tasks_from_payload(payload: dict, client: str) -> list[tuple[str, str]]:
    """Normalize native steps without deduplicating identical occurrences."""
    if not isinstance(payload, dict):
        return []
    data = payload.get('tool_input') or {}
    if not isinstance(data, dict):
        return []
    if client == 'codex':
        plan = data.get('plan')
        if not isinstance(plan, list):
            return []
        result = []
        for step in plan:
            if (not isinstance(step, dict) or not isinstance(step.get('step'), str)
                    or not step['step'].strip()
                    or step.get('status') not in ('pending', 'in_progress', 'completed')):
                return []  # malformed snapshots must not be partially mirrored
            result.append((step['step'].strip(), step['status']))
        return result
    if client != 'claude':
        return []
    if isinstance(data.get('todos'), list):
        texts = [row.get('content') or row.get('subject') or ''
                 for row in data['todos'] if isinstance(row, dict)]
    else:
        texts = [data.get('subject') or data.get('description') or data.get('prompt') or '']
    return [(text.strip(), 'pending') for text in texts if isinstance(text, str) and text.strip()]


def mirror_tasks(engine: sa.Engine, payload: dict, *, client: str, project: str = '',
                 session_id: str = '', turn_id: str = '', cwd: str | None = None) -> dict | None:
    """Mirror one validated native event; caller gates config before DB bootstrap.

    The Codex bridge supplies only successful PostToolUse update_plan events and
    sets truthful provenance before calling. Session/turn IDs are actual host
    values, never hashes or invented fallbacks. Missing Codex identity skips.
    Equal steps within a plan are separate checkpoint rows. An exact event
    replay (tool_use_id + ordered snapshot) is skipped; without tool_use_id only
    consecutive identical snapshots are skipped. Changes append a new snapshot,
    preserving prior checkpoints and statuses instead of rewriting history.

    Codex records carry explicit ownership component and native actor/session
    provenance; a user-created item with the same display title is never reused.
    Project-level serialization and one transaction cover item lookup, replay
    detection, checkpoints and the note marker. No schema additions are needed.
    Native completion does not assert DCT acceptance: all mirrored checkpoints
    remain pending and non-gating, with native status explicit in their text.
    """
    tasks = tasks_from_payload(payload, client)
    if not tasks:
        return None
    session_id = session_id or payload.get('session_id') or ''
    turn_id = turn_id or payload.get('turn_id') or ''
    if client == 'codex' and not (isinstance(session_id, str) and session_id.strip()
                                  and isinstance(turn_id, str) and turn_id.strip()):
        return None
    if client == 'claude':
        session_id = session_id or 'unknown'  # permanent legacy CLI behavior
    if not isinstance(session_id, str):
        return None
    if not project:
        detected = get_project_by_path(engine, cwd or os.getcwd())
        if not detected:
            return None
        project = detected['slug']
    prefix = 'Codex' if client == 'codex' else 'CC'
    title = f'{prefix} task list — session {session_id}'
    if client == 'codex':
        title += f' / turn {turn_id}'
    _validate_title_length(title)
    event_id = payload.get('tool_use_id')
    if not isinstance(event_id, str):
        event_id = None
    marker = _MARKER + json.dumps({'event_id': event_id, 'plan': tasks}, ensure_ascii=False)
    provenance = ({'actor_type': 'agent', 'actor_client': 'codex',
                   'session_id': session_id, 'source': 'hook'} if client == 'codex' else {})
    owned = {'component': 'codex-task-mirror', **provenance} if client == 'codex' else {}
    with _mirror_actor(provenance), project_transaction(engine, project, write=True) as (conn, project_id):
        query = sa.select(items.c.id).where(
            items.c.project_id == project_id, items.c.title == title,
            items.c.deleted_at.is_(None),
        )
        if client == 'codex':
            # A familiar display title alone never grants ownership of a row.
            query = query.where(items.c.component == 'codex-task-mirror',
                                items.c.actor_client == 'codex',
                                items.c.session_id == session_id)
        item_id = conn.execute(query.order_by(items.c.id).limit(1)).scalar_one_or_none()
        if item_id is None:
            # Fixed mirror metadata uses the same Core defaults/provenance and
            # shared tag writer as create_item, inside this composed transaction.
            item_id = conn.execute(items.insert().values(
                project_id=project_id, item_type='todo', title=title, **owned,
            ).returning(items.c.id)).scalar_one()
            set_item_tags_conn(conn, item_id, ['codex-task-list' if client == 'codex' else 'cc-task-list', 'auto'], force=True)
        if client == 'codex':
            query = sa.select(item_notes.c.note).where(
                item_notes.c.item_id == item_id, item_notes.c.deleted_at.is_(None),
                item_notes.c.note.startswith(_MARKER),
            )
            if event_id:
                previous = conn.execute(query.where(item_notes.c.note == marker).limit(1)).scalar_one_or_none()
            else:
                previous = conn.execute(query.order_by(item_notes.c.id.desc()).limit(1)).scalar_one_or_none()
            if previous == marker:
                return {'item_id': item_id, 'added': 0, 'replayed': True}
        maximum = conn.execute(sa.select(sa.func.max(item_checkpoints.c.sort_order)).where(
            item_checkpoints.c.item_id == item_id, item_checkpoints.c.deleted_at.is_(None),
        )).scalar_one() or 0
        for index, (text, status) in enumerate(tasks, 1):
            label = f'[codex:{status}]' if client == 'codex' else '[cc]'
            conn.execute(item_checkpoints.insert().values(
                item_id=item_id, text=f'{label} {text}', kind='cc-task',
                sort_order=maximum + index * 10, **provenance,
            ))
        if client == 'codex':
            conn.execute(item_notes.insert().values(item_id=item_id, note=marker, **provenance))
    return {'item_id': item_id, 'added': len(tasks), 'replayed': False}
