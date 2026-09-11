"""Production-server cross-client acceptance, not an actual agent-session test.

Fresh processes exchange only database-backed handoffs. The harness acts as MCP
clients; labels exercise provenance, not model behavior or desktop discovery.
"""

import json
import re

import pytest
import sqlalchemy as sa
from test_server_wire import WireClient, isolated_wire_environment

from dct.core.projects import create_project
from dct.db import create_tables, get_engine


@pytest.fixture
def wire_project(tmp_path):
    env = isolated_wire_environment(tmp_path)
    project = tmp_path / 'project with spaces'
    project.mkdir()
    engine = get_engine(env['DCT_DATABASE_URL'])
    create_tables(engine)
    create_project(engine, slug='proj-a', name='Project A', path=str(project))
    yield env, project, engine
    engine.dispose()


def client(env, project, actor, session):
    return WireClient({**env, 'DCT_ACTOR_CLIENT': actor,
                       'DCT_ACTOR_TYPE': 'agent', 'DCT_SESSION_ID': session}, project, actor)


def table_rows(table):
    return [dict(zip(table['cols'], row)) for row in table['rows']]


@pytest.mark.parametrize('origin,target', [('claude-code', 'codex'), ('codex', 'claude-code')])
def test_production_handoff_roundtrip_between_fresh_clients(wire_project, origin, target):
    env, project, engine = wire_project
    with client(env, project, origin, 'session-origin') as source:
        source.initialize()
        item = source.call('create_item', item_type='feature', title='Resume portable integration', project='')
        assert 'error' not in item, item
        checkpoint = source.call('add_checkpoint', item_id=item['id'], text='Verify the isolated next step')
        assert 'error' not in checkpoint, checkpoint
        prompt = f"Continue item #{item['id']} at checkpoint #{checkpoint['id']}; verify the isolated next step."
        written = source.call('handoff_work', project='', prompt=prompt,
                              created_by=origin + ' session-origin', add_item_ids=str(item['id']))
        assert 'error' not in written, written
    # The second process receives no transcript, source responses or prompt.
    with client(env, project, target, 'session-target') as destination:
        destination.initialize()
        snapshot = destination.call('pickup_work', project='', consume='false')
        assert snapshot['consumed'] is False
        assert snapshot['handoff']['latest']['prompt'] == prompt
        assert snapshot['handoff']['latest']['id'] == written['handoff_id']
        assert any(row['id'] == item['id'] for row in table_rows(snapshot['sprint']['items']))
        assert any(row['id'] == checkpoint['id'] and row['text'] == 'Verify the isolated next step'
                   for row in table_rows(snapshot['sprint']['checkpoints']))
        selected = snapshot['handoff']['latest']['id']
        consumed = destination.call('pickup_work', project='', consume='true', handoff_id=selected,
                                    consumed_by=target + ' session-target', include='none')
        assert consumed['consumed'] is True
        duplicate = destination.call('pickup_work', project='', consume='true', handoff_id=selected,
                                     consumed_by=target + ' session-target', include='none')
        assert 'already consumed' in duplicate['error']
        reply = destination.call('handoff_work', project='', prompt='Verify completion of the next checkpoint.',
                                 created_by=target + ' session-target')
        assert 'error' not in reply, reply
    # Inspect persisted creation provenance from the production CLI entry point;
    # consumption updates consumed_by while deliberately preserving the creator.
    with engine.connect() as connection:
        rows = connection.execute(sa.text(
            'SELECT id, actor_type, actor_client, session_id, source, consumed_by '
            'FROM session_handoffs ORDER BY id')).mappings().all()
        assert tuple(rows[0][key] for key in ('actor_type', 'actor_client', 'session_id', 'source')) == (
            'agent', origin, 'session-origin', 'mcp')
        assert rows[0]['consumed_by'] == target + ' session-target'
        assert tuple(rows[1][key] for key in ('actor_type', 'actor_client', 'session_id', 'source')) == (
            'agent', target, 'session-target', 'mcp')
        for table in ('items', 'item_checkpoints'):
            row = connection.execute(sa.text(f'SELECT actor_client, session_id, source FROM {table}')).one()
            assert tuple(row) == (origin, 'session-origin', 'mcp')


def exchange_metrics(exchanges):
    """Stable JSON byte proxy: normalize timestamps and request IDs, never tokens."""
    def normalized(value):
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if isinstance(value, str):
            return re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?',
                          '2000-01-01T00:00:00Z', value)
        return value

    size = 0
    for request, response in exchanges:
        for payload in (request, response):
            payload = normalized({**payload, 'id': 0})
            size += len(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    return {'tool_calls': len(exchanges), 'normalized_json_bytes': size}


def test_composed_resume_measurement(wire_project, record_property):
    """A deterministic small fixture compares overlapping resume workloads.

    Granular tools may repeat context or expose more fields; bytes quantify these
    actual responses, not semantic equivalence or model-token savings.
    """
    env, project, _ = wire_project
    with client(env, project, 'codex', 'benchmark-session') as wire:
        wire.initialize()
        item = wire.call('create_item', project='', item_type='todo', title='Benchmark resume')
        wire.call('add_checkpoint', item_id=item['id'], text='Next action')
        handoff = wire.call('handoff_work', project='', prompt='Continue at the pending checkpoint.',
                            created_by='benchmark-session', add_item_ids=str(item['id']))
        wire.tool_exchanges.clear()
        composed = wire.call('pickup_work', project='', consume='false', include='sprint,handoff')
        composed_metrics = exchange_metrics(wire.tool_exchanges)
        assert composed['handoff']['latest']['id'] == handoff['handoff_id']
        wire.tool_exchanges.clear()
        wire.call('review_sprint', project='')
        wire.call('list_handoffs', project='')
        read = wire.call('get_handoff', handoff_id=handoff['handoff_id'])
        wire.call('list_checkpoints', item_id=item['id'])
        granular_metrics = exchange_metrics(wire.tool_exchanges)
        assert read['prompt'] == composed['handoff']['latest']['prompt']
        assert composed_metrics['tool_calls'] == 1
        assert granular_metrics['tool_calls'] == 4
        assert composed_metrics['normalized_json_bytes'] > 0
        assert granular_metrics['normalized_json_bytes'] > 0
        report = {'composed': composed_metrics, 'granular': granular_metrics}
        record_property('resume_measurement', json.dumps(report, sort_keys=True))
        print('\nResume measurement (bytes, not tokens): ' + json.dumps(report, sort_keys=True))
