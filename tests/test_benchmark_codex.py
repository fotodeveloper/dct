"""Offline validity gates for the client benchmark; no live model calls.

Usage fixtures use Codex persisted event_msg/token_count records. Their totals
are session-cumulative; last_token_usage describes one request, not a delta to
add every time the same event is repeated.
"""

import importlib.util
import json
import sys
from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'benchmark_codex.py'
MODEL = 'test-model'
PAYLOAD = {'item_id': 42, 'checkpoint_id': 7, 'next_action': 'Verify migration'}


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location('benchmark_codex', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def usage(input_tokens, cached, output_tokens):
    return {'input_tokens': input_tokens, 'cached_input_tokens': cached,
            'output_tokens': output_tokens,
            'cache_write_input_tokens': 0, 'reasoning_output_tokens': 0, 'total_tokens': input_tokens + output_tokens}


def token_event(total, last):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': total, 'last_token_usage': last, 'model_context_window': 128000,
    }}}


def final_event(payload):
    return {'type': 'item.completed', 'item': {
        'id': 'answer', 'type': 'agent_message', 'text': json.dumps(payload),
    }}


def analyze(benchmark, events):
    return benchmark.analyze_events(
        [{'type': 'turn_context', 'payload': {'model': MODEL}}, *events],
        expected_model=MODEL, expected_payload=PAYLOAD,
    )


def test_cumulative_usage_counts_requests_once_even_when_event_repeats(benchmark):
    first, second = usage(100, 20, 10), usage(150, 30, 17)
    events = [token_event(first, first), token_event(first, first),
              token_event(second, usage(50, 10, 7)), final_event(PAYLOAD)]
    result = analyze(benchmark, events)
    assert result['aggregate_usage'] == {
        'input_tokens': 150, 'cached_input_tokens': 30, 'output_tokens': 17,
    }
    assert result['per_request_usage'] == [
        {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 10},
        {'input_tokens': 50, 'cached_input_tokens': 10, 'output_tokens': 7},
    ]


def test_cumulative_total_wins_over_conflicting_last_request_counter(benchmark):
    events = [token_event(usage(100, 20, 10), usage(100, 20, 10)),
              token_event(usage(150, 30, 17), usage(140, 25, 16)), final_event(PAYLOAD)]
    result = analyze(benchmark, events)
    assert result['aggregate_usage']['input_tokens'] == 150
    # Contradictory counters must not become an apparently trustworthy sample.
    assert not result['valid']
    assert result['errors']


@pytest.mark.parametrize('total', [usage(100, 101, 10), usage(-1, 0, 10)])
def test_impossible_token_counters_invalidate_sample(benchmark, total):
    result = analyze(benchmark, [token_event(total, total), final_event(PAYLOAD)])
    assert not result['valid']
    assert result['errors']


def test_counter_reset_is_not_treated_as_negative_request_usage(benchmark):
    events = [token_event(usage(100, 50, 20), usage(100, 50, 20)),
              token_event(usage(10, 0, 2), usage(10, 0, 2)), final_event(PAYLOAD)]
    result = analyze(benchmark, events)
    assert not result['valid']
    assert all(value >= 0 for row in result['per_request_usage'] for value in row.values())


def test_selected_model_matches_but_does_not_claim_provider_verification(benchmark):
    events = [{'type': 'turn_context', 'payload': {'model': MODEL}},
              token_event(usage(100, 20, 10), usage(100, 20, 10)), final_event(PAYLOAD)]
    result = analyze(benchmark, events)
    assert result['requested_model_matches'] is True
    assert result['response_model_verified'] is False
    assert result['valid'] is True


@pytest.mark.parametrize('model_events', [[], [
    {'type': 'turn_context', 'payload': {'model': 'different-model'}},
], [
    {'type': 'turn_context', 'payload': {'model': MODEL}},
    {'type': 'turn_context', 'payload': {'model': 'different-model'}},
]])
def test_missing_or_mixed_selected_models_invalidate_sample(benchmark, model_events):
    events = model_events + [token_event(usage(100, 20, 10), usage(100, 20, 10)),
                            final_event(PAYLOAD)]
    result = benchmark.analyze_events(events, expected_model=MODEL, expected_payload=PAYLOAD)
    assert result['requested_model_matches'] is False
    assert result['valid'] is False


@pytest.mark.parametrize('payload', [None, {'item_id': 43, 'checkpoint_id': 7,
                                         'next_action': 'Verify migration'},
                                     {'item_id': 42, 'checkpoint_id': 7}])
def test_missing_or_incorrect_result_is_not_accepted(benchmark, payload):
    events = [] if payload is None else [final_event(payload)]
    result = analyze(benchmark, events)
    assert result['response_correct'] is False
    assert result['valid'] is False


def test_exact_result_is_compared_semantically(benchmark):
    reordered = {'next_action': 'Verify migration', 'checkpoint_id': 7, 'item_id': 42}
    assert analyze(benchmark, [final_event(reordered)])['response_correct'] is True


def test_later_wrong_answer_cannot_be_masked_by_earlier_correct_result(benchmark):
    events = [final_event(PAYLOAD), final_event({'item_id': 999})]
    assert analyze(benchmark, events)['response_correct'] is False


@pytest.mark.parametrize('trials', [1, 2, 3])
def test_schedule_balances_arms_and_alternates_pair_order(benchmark, trials):
    order = benchmark.schedule(trials)
    assert Counter(order) == {'file': trials, 'file_mcp': trials, 'mcp': trials}
    pairs = [order[index:index + 3] for index in range(0, len(order), 3)]
    assert all(set(pair) == {'file', 'file_mcp', 'mcp'} for pair in pairs)
    assert all(left != right for left, right in pairwise(pairs))


@pytest.mark.parametrize('trials', [0, -1])
def test_nonpositive_trial_count_is_rejected(benchmark, trials):
    with pytest.raises(ValueError):
        benchmark.schedule(trials)


def test_prepare_isolates_each_arm_from_user_config_and_credentials(benchmark, tmp_path, monkeypatch):
    real_home = tmp_path / 'real-home'
    (real_home / '.codex').mkdir(parents=True)
    (real_home / '.codex' / 'auth.json').write_text('credential-sentinel-do-not-copy')
    (real_home / '.codex' / 'config.toml').write_text('live-config-sentinel-do-not-copy')
    live_config = real_home / 'live-dct.toml'
    live_config.write_text('live-database-sentinel-do-not-copy')
    monkeypatch.setenv('HOME', str(real_home))
    monkeypatch.setenv('CODEX_HOME', str(real_home / '.codex'))
    monkeypatch.setenv('DCT_CONFIG_PATH', str(live_config))
    root = tmp_path / 'benchmark'
    manifest_path = benchmark.prepare(root, model=MODEL, effort='medium', trials=2)
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest['runs']) == 6
    assert len({run['workspace'] for run in manifest['runs']}) == 6
    assert len({run['database_url'] for run in manifest['runs']}) == 6
    for run in manifest['runs']:
        for key in ('workspace', 'home', 'codex_home', 'config_path', 'prompt_path'):
            assert Path(run[key]).resolve().is_relative_to(root.resolve())
        assert 'live-dct' not in run['database_url']
    for path in root.rglob('*'):
        if path.is_file():
            content = path.read_bytes()
            assert b'credential-sentinel-do-not-copy' not in content
            assert b'live-config-sentinel-do-not-copy' not in content
            assert b'live-database-sentinel-do-not-copy' not in content


def test_real_pilot_usage_shape_reconciles_two_requests(benchmark):
    """Counters from the sanitized two-request pilot; no account/session data."""
    first = usage(15725, 12160, 53)
    total = usage(31568, 27648, 61)
    second = usage(15843, 15488, 8)
    result = analyze(benchmark, [token_event(first, first), token_event(total, second),
                                 final_event(PAYLOAD)])
    assert result['valid'] is True
    assert result['aggregate_usage'] == {
        'input_tokens': 31568, 'cached_input_tokens': 27648, 'output_tokens': 61,
    }
    assert sum(row['input_tokens'] for row in result['per_request_usage']) == 31568
    assert sum(row['cached_input_tokens'] for row in result['per_request_usage']) == 27648
    assert sum(row['output_tokens'] for row in result['per_request_usage']) == 61


def test_matching_answer_without_usage_is_not_a_measured_sample(benchmark):
    result = analyze(benchmark, [final_event(PAYLOAD)])
    assert result['response_correct'] is True
    assert result['aggregate_usage'] is None
    assert result['valid'] is False


def test_identical_request_usage_for_two_requests_is_not_deduplicated(benchmark):
    # Requests can cost exactly the same; only repeated cumulative snapshots
    # are duplicates, not repeated last_token_usage values.
    each = usage(100, 20, 10)
    total = usage(200, 40, 20)
    result = analyze(benchmark, [token_event(each, each), token_event(total, each),
                                 final_event(PAYLOAD)])
    assert result['valid'] is True
    assert len(result['per_request_usage']) == 2
    assert result['aggregate_usage']['input_tokens'] == 200


def test_started_completed_tool_and_rollout_wrapper_are_one_invocation(benchmark):
    item = {'id': 'item_1', 'type': 'command_execution',
            'command': '/bin/sh -lc "cat CONTEXT.txt"'}
    events = [
        {'type': 'item.started', 'item': {**item, 'status': 'in_progress',
                                        'aggregated_output': '', 'exit_code': None}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                                             'call_id': 'call_fixture', 'status': 'completed',
                                             'input': 'text(await tools.exec_command({cmd:"cat CONTEXT.txt"}));'}},
        {'type': 'item.completed', 'item': {**item, 'status': 'completed',
                                          'aggregated_output': '{}', 'exit_code': 0}},
    ]
    calls = analyze(benchmark, events)['tool_calls']
    assert len(calls) == 1
    assert calls[0]['name'] == 'shell_command'


def _write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(event) + '\n' for event in events))


@pytest.fixture
def recorded_benchmark(benchmark, tmp_path):
    """A complete synthetic successful recording with no subprocess model calls."""
    root = tmp_path / 'recording'
    manifest_path = benchmark.prepare(root, model=MODEL, effort='medium', trials=1)
    manifest = json.loads(manifest_path.read_text())
    payload = manifest['expected_payload']
    payload_text = Path(manifest['payload_path']).read_text()
    for run in manifest['runs']:
        thread = 'thread-' + run['id']
        rollout = Path(run['codex_home']) / 'sessions' / 'run.jsonl'
        _write_events(rollout, [
            {'type': 'session_meta', 'payload': {'id': thread}},
            {'type': 'turn_context', 'payload': {'model': MODEL, 'effort': 'medium'}},
            token_event(usage(100, 20, 10), usage(100, 20, 10)),
        ])
        if run['arm'] == 'mcp':
            item = {'id': 'read', 'type': 'mcp_tool_call', 'server': 'dct',
                    'tool': 'pickup_work', 'arguments': manifest['read_arguments'],
                    'result': {'content': [{'type': 'text', 'text': payload_text}]},
                    'error': None, 'status': 'completed'}
        else:
            item = {'id': 'read', 'type': 'command_execution', 'command': 'cat snapshot.json',
                    'aggregated_output': payload_text, 'exit_code': 0, 'status': 'completed'}
        _write_events(Path(run['directory']) / 'stdout.jsonl', [
            {'type': 'thread.started', 'thread_id': thread},
            {'type': 'item.completed', 'item': item}, final_event(payload),
        ])
        (Path(run['directory']) / 'status.json').write_text(json.dumps({
            'returncode': 0, 'before_state': '0' * 64, 'after_state': '0' * 64,
        }))
    return root, manifest


def test_complete_recording_has_three_comparable_arms(benchmark, recorded_benchmark):
    root, _ = recorded_benchmark
    result = benchmark.analyze_run(root)
    assert result['all_valid'], result['runs']
    assert all(arm['valid_trials'] == 1 for arm in result['arms'].values())


def test_modified_payload_invalidates_comparison(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = next(run for run in manifest['runs'] if run['arm'] == 'file')
    (Path(run['workspace']) / 'snapshot.json').write_text('{"different":"workload"}')
    result = benchmark.analyze_run(root)
    assert not result['all_valid']
    assert not next(row for row in result['runs'] if row['id'] == run['id'])['valid']


def test_missing_state_evidence_does_not_count_as_unchanged_database(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = manifest['runs'][0]
    (Path(run['directory']) / 'status.json').write_text('{"returncode":0}')
    result = benchmark.analyze_run(root)
    assert not next(row for row in result['runs'] if row['id'] == run['id'])['valid']


def test_unrelated_guardian_rollout_does_not_contaminate_task_cost(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = manifest['runs'][0]
    _write_events(Path(run['codex_home']) / 'sessions' / 'unrelated.jsonl', [
        {'type': 'session_meta', 'payload': {'id': 'unrelated-guardian'}},
        {'type': 'turn_context', 'payload': {'model': 'another-model', 'effort': 'low'}},
        token_event(usage(999, 0, 55), usage(999, 0, 55)),
    ])
    result = benchmark.analyze_run(root)
    row = next(row for row in result['runs'] if row['id'] == run['id'])
    assert row['valid'], row['errors']
    assert row['aggregate_usage']['input_tokens'] == 100


def test_file_control_cannot_substitute_an_mcp_invocation(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = next(run for run in manifest['runs'] if run['arm'] == 'file_mcp')
    path = Path(run['directory']) / 'stdout.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[1]['item'] = {'id': 'read', 'type': 'mcp_tool_call', 'server': 'dct',
                         'tool': 'pickup_work', 'arguments': manifest['read_arguments'],
                         'result': {'content': [{'type': 'text', 'text': Path(manifest['payload_path']).read_text()}]},
                         'error': None, 'status': 'completed'}
    _write_events(path, events)
    result = benchmark.analyze_run(root)
    assert not next(row for row in result['runs'] if row['id'] == run['id'])['valid']


def test_model_answer_cannot_hide_a_different_retrieved_payload(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = next(run for run in manifest['runs'] if run['arm'] == 'mcp')
    path = Path(run['directory']) / 'stdout.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[1]['item']['result']['content'][0]['text'] = '{"different":"retrieved data"}'
    _write_events(path, events)
    result = benchmark.analyze_run(root)
    assert not next(row for row in result['runs'] if row['id'] == run['id'])['valid']


def test_extra_live_mcp_configuration_is_rejected_before_analysis(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    path = Path(manifest['runs'][0]['codex_config_path'])
    with path.open('a') as stream:
        stream.write('\n[mcp_servers.foreign]\ncommand = "foreign-server"\n')
    with pytest.raises(ValueError):
        benchmark.analyze_run(root)


def test_codex_runtime_trust_entry_for_exact_workspace_is_allowed(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = manifest['runs'][0]
    path = Path(run['codex_config_path'])
    with path.open('a') as stream:
        stream.write(f'\n[projects.{json.dumps(run["workspace"])}]\ntrust_level = "trusted"\n')

    assert benchmark.analyze_run(root)['all_valid']


@pytest.mark.parametrize('project_path, body', [
    ('different-workspace', 'trust_level = "trusted"'),
    (None, 'trust_level = "untrusted"'),
    (None, 'trust_level = "trusted"\ncustom = true'),
])
def test_other_runtime_project_configuration_is_rejected(
        benchmark, recorded_benchmark, project_path, body):
    root, manifest = recorded_benchmark
    run = manifest['runs'][0]
    path = Path(run['codex_config_path'])
    project_path = project_path or run['workspace']
    with path.open('a') as stream:
        stream.write(f'\n[projects.{json.dumps(project_path)}]\n{body}\n')

    with pytest.raises(ValueError, match='config'):
        benchmark.analyze_run(root)


def test_edited_config_is_rejected_before_authentication_or_model_call(benchmark, recorded_benchmark, monkeypatch):
    root, manifest = recorded_benchmark
    path = Path(manifest['runs'][0]['codex_config_path'])
    with path.open('a') as stream:
        stream.write('\n[mcp_servers.foreign]\ncommand = "foreign-server"\n')

    def forbidden(*args, **kwargs):
        pytest.fail('tampered configuration reached a subprocess')

    monkeypatch.setattr(benchmark.subprocess, 'run', forbidden)
    # If auth is resolved before validation, this raises FileNotFoundError.
    with pytest.raises(ValueError, match='config'):
        benchmark.run_prepared(root, auth_path=root / 'missing-auth.json')


def test_paired_deltas_keep_raw_and_derived_usage_distinct(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    counters = {'file': usage(100, 20, 10), 'file_mcp': usage(150, 90, 10),
                'mcp': usage(140, 100, 15)}
    for run in manifest['runs']:
        path = Path(run['codex_home']) / 'sessions' / 'run.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[-1] = token_event(counters[run['arm']], counters[run['arm']])
        _write_events(path, events)
    result = benchmark.analyze_run(root)
    assert result['all_valid']
    comparison = result['paired_contrasts'][0]
    assert comparison['file_mcp_minus_file'] == {
        'input_tokens': 50, 'cached_input_tokens': 70, 'output_tokens': 0,
        'uncached_input_tokens': -20,
    }
    assert comparison['mcp_minus_file_mcp'] == {
        'input_tokens': -10, 'cached_input_tokens': 10, 'output_tokens': 5,
        'uncached_input_tokens': -20,
    }
    for row in result['runs']:
        assert 'uncached_input_tokens' not in row['aggregate_usage']
        assert row['derived_usage']['uncached_input_tokens'] == (
            row['aggregate_usage']['input_tokens'] - row['aggregate_usage']['cached_input_tokens'])


def test_invalid_arm_suppresses_both_paired_contrasts(benchmark, recorded_benchmark):
    root, manifest = recorded_benchmark
    run = manifest['runs'][0]
    path = Path(run['directory']) / 'stdout.jsonl'
    with path.open('a') as stream:
        stream.write(json.dumps(final_event({'incorrect': 'answer'})) + '\n')
    pair = benchmark.analyze_run(root)['paired_contrasts'][0]
    assert not pair['valid']
    assert pair['file_mcp_minus_file'] is None
    assert pair['mcp_minus_file_mcp'] is None
