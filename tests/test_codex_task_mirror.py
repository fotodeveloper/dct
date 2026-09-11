"""Native plan mirroring is an append-only observation ledger, never task sync."""

import argparse
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from dct.config import Config, load_config, save_config
from dct.core.items import get_item, list_items, resolve_item
from dct.core.provenance import Provenance, get_provenance, set_provenance
from dct.workflows.task_mirror import mirror_tasks


def payload(*, turn='turn-a', event='event-a', statuses=('pending', 'completed')):
    return {'session_id': 'actual-session', 'turn_id': turn, 'tool_use_id': event,
            'hook_event_name': 'PostToolUse', 'tool_name': 'update_plan',
            'tool_input': {'plan': [{'step': 'Repeated step', 'status': status} for status in statuses]}}


def test_codex_config_is_independent_and_roundtrips(tmp_path):
    config = Config()
    config.integrations.claude.task_mirror = 'off'
    config.integrations.codex.task_mirror = 'ask'
    path = tmp_path / 'dct.toml'
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.integrations.claude.task_mirror == 'off'
    assert loaded.integrations.codex.task_mirror == 'ask'
    path.write_text('[integrations.codex]\ntask_mirror="typo"\n')
    assert load_config(path).integrations.codex.task_mirror == 'off'


def test_snapshot_preserves_duplicate_occurrences_and_native_status(engine, seed_project):
    previous = get_provenance()
    set_provenance(Provenance(actor_type='agent', actor_client='codex', session_id='actual-session', source='hook'))
    try:
        result = mirror_tasks(engine, payload(), client='codex', project='test')
    finally:
        set_provenance(previous)
    assert result['added'] == 2
    item = get_item(engine, result['item_id'])
    checkpoints = item['checkpoints']
    assert len({row['id'] for row in checkpoints}) == 2
    assert {row['text'] for row in checkpoints} == {'[codex:pending] Repeated step', '[codex:completed] Repeated step'}
    assert all(row['status'] == 'pending' and row['kind'] == 'cc-task' for row in checkpoints)
    assert all(row['actor_client'] == 'codex' and row['session_id'] == 'actual-session' for row in checkpoints)
    # Mirrored records must not accidentally become new gate kinds.
    resolve_item(engine, result['item_id'])


def test_exact_replay_is_skipped_but_later_event_appends_snapshot(engine, seed_project):
    first = mirror_tasks(engine, payload(statuses=('pending', 'pending')), client='codex', project='test')
    replay = mirror_tasks(engine, payload(statuses=('pending', 'pending')), client='codex', project='test')
    assert replay == {'item_id': first['item_id'], 'added': 0, 'replayed': True}
    changed = mirror_tasks(engine, payload(event='event-b', statuses=('completed',)), client='codex', project='test')
    assert changed['item_id'] == first['item_id']
    assert len(get_item(engine, first['item_id'])['checkpoints']) == 3
    historical_replay = mirror_tasks(engine, payload(statuses=('pending', 'pending')), client='codex', project='test')
    assert historical_replay['added'] == 0


def test_subagent_turns_sharing_session_do_not_merge_plans(engine, seed_project):
    first = mirror_tasks(engine, payload(turn='turn-a'), client='codex', project='test')
    second = mirror_tasks(engine, payload(turn='turn-b'), client='codex', project='test')
    assert first['item_id'] != second['item_id']


@pytest.mark.parametrize('missing', ['session_id', 'turn_id'])
def test_missing_native_identity_never_fabricates_a_list(engine, seed_project, missing):
    event = payload()
    del event[missing]
    assert mirror_tasks(engine, event, client='codex', project='test') is None
    assert list_items(engine, project_slug='test') == []


@pytest.mark.parametrize('mode', ['off', 'ask'])
def test_codex_worker_gate_precedes_database_bootstrap(monkeypatch, mode):
    from dct import cli

    config = Config()
    config.integrations.codex.task_mirror = mode
    config.integrations.claude.task_mirror = 'auto'
    monkeypatch.setattr('dct.config.load_config', lambda: config)
    monkeypatch.setattr(cli, '_get_engine', lambda: pytest.fail('disabled mirror opened DB'))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload())))
    cli.cmd_internal_task_mirror(argparse.Namespace(client='codex', session_id='', turn_id='', project='test'))


def test_note_failure_rolls_back_the_whole_snapshot(engine, seed_project):
    def fail_note(conn, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith('insert into item_notes'):
            raise RuntimeError('simulated note failure')

    sa.event.listen(engine, 'before_cursor_execute', fail_note)
    try:
        with pytest.raises(RuntimeError, match='note failure'):
            mirror_tasks(engine, payload(), client='codex', project='test')
    finally:
        sa.event.remove(engine, 'before_cursor_execute', fail_note)
    assert list_items(engine, project_slug='test') == []


def test_concurrent_replay_creates_one_snapshot_on_file_sqlite(tmp_path):
    from dct.core.projects import create_project
    from dct.db import create_tables, get_engine

    engine = get_engine('sqlite:///' + str(tmp_path / 'mirror.db'))
    try:
        create_tables(engine)
        create_project(engine, 'test', 'Test', path=str(tmp_path))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: mirror_tasks(engine, payload(), client='codex', project='test'), range(2)))
        assert sorted(row['added'] for row in results) == [0, 2]
        assert len({row['item_id'] for row in results}) == 1
        assert len(get_item(engine, results[0]['item_id'])['checkpoints']) == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize('identity', ['', '  ', 42, None])
def test_invalid_codex_identity_skips_bootstrap(monkeypatch, identity):
    from dct import cli

    event = payload()
    event['turn_id'] = identity
    monkeypatch.setattr('dct.config.load_config', Config)
    monkeypatch.setattr(cli, '_get_engine', lambda: pytest.fail('invalid identity opened DB'))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event)))
    cli.cmd_internal_task_mirror(argparse.Namespace(client='codex', session_id='', turn_id='', project='test'))


def test_no_event_id_replay_dedup_does_not_erase_later_reversion(engine, seed_project):
    first = payload(event=None, statuses=('pending',))
    second = payload(event=None, statuses=('completed',))
    result = mirror_tasks(engine, first, client='codex', project='test')
    assert mirror_tasks(engine, first, client='codex', project='test')['added'] == 0
    assert mirror_tasks(engine, second, client='codex', project='test')['added'] == 1
    assert mirror_tasks(engine, first, client='codex', project='test')['added'] == 1
    assert len(get_item(engine, result['item_id'])['checkpoints']) == 3


def test_manual_same_title_item_is_not_adopted_by_codex_mirror(engine, seed_project):
    from dct.core.items import create_item

    manual = create_item(engine, 'test', 'todo',
                         'Codex task list — session actual-session / turn turn-a')
    first = mirror_tasks(engine, payload(), client='codex', project='test')
    replay = mirror_tasks(engine, payload(), client='codex', project='test')
    assert first['item_id'] != manual['id']
    assert get_item(engine, manual['id'])['checkpoints'] == []
    assert replay['item_id'] == first['item_id']
    assert replay['added'] == 0


def test_codex_rows_never_inherit_another_clients_provenance(engine, seed_project):
    previous = get_provenance()
    set_provenance(Provenance(actor_type='human', actor_client='claude-code', session_id='other-session', source='cli'))
    try:
        result = mirror_tasks(engine, payload(), client='codex', project='test')
        assert get_provenance().actor_client == 'claude-code'
    finally:
        set_provenance(previous)
    item = get_item(engine, result['item_id'])
    assert item['component'] == 'codex-task-mirror'
    for row in [item, *item['checkpoints'], *item['notes']]:
        assert row['actor_client'] == 'codex'
        assert row['actor_type'] == 'agent'
        assert row['session_id'] == 'actual-session'

    from dct.db import tags
    with engine.connect() as conn:
        rows = conn.execute(sa.select(tags).where(tags.c.name.in_(['codex-task-list', 'auto']))).mappings().all()
    assert rows and all(row['actor_client'] == 'codex' for row in rows)
