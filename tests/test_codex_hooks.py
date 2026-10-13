"""Native Codex hooks preserve policy, truthful provenance and safe no-ops."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import require_tools

from dct.integrations import codex_hooks as hooks

pytestmark = pytest.mark.slow


def event(tmp_path, tool='Bash', phase='PreToolUse', **extra):
    return {'cwd': str(tmp_path), 'session_id': 'real-session', 'turn_id': 'real-turn',
            'model': 'test-model', 'hook_event_name': phase, 'tool_name': tool,
            'tool_input': {}, **extra}


def test_irrelevant_events_never_start_policy_or_worker(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('Irrelevant event attempted work')
    monkeypatch.setattr(hooks, '_run_policy', unexpected)
    monkeypatch.setattr(hooks, '_run_mirror', unexpected)
    assert hooks.handle_event('mirror', event(tmp_path, 'Bash', 'PostToolUse')) == (0, None, '')
    assert hooks.handle_event('autoscan', event(tmp_path, 'update_plan', 'PostToolUse')) == (0, None, '')
    assert hooks.handle_event('check-changelog', event(tmp_path, tool_input={'command': 'git status'})) == (0, None, '')


def test_deny_is_translated_to_codex_stderr(tmp_path, monkeypatch):
    denied = {'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'Add changelog first'}}
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: subprocess.CompletedProcess([], 2, json.dumps(denied), ''))
    code, output, error = hooks.handle_event('check-changelog', event(tmp_path, tool_input={'command': 'git commit -m "change"'}))
    assert (code, output, error) == (2, None, 'Add changelog first')


def test_broken_policy_does_not_falsely_block_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: subprocess.CompletedProcess([], 2, 'broken JSON', 'failure'))
    code, output, error = hooks.handle_event('check-changelog', event(tmp_path, tool_input={'command': 'git commit'}))
    assert code == 0 and error == ''
    assert 'systemMessage' in output


def test_stop_reminder_is_json_and_never_requests_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: subprocess.CompletedProcess([], 0, '\nAdd a changelog.\n', ''))
    assert hooks.handle_event('stop', event(tmp_path, phase='Stop')) == (0, {'systemMessage': 'Add a changelog.'}, '')


@pytest.mark.parametrize('style', ['workdir', 'cd', 'git-C'])
def test_commit_policy_runs_in_actual_literal_workdir(tmp_path, monkeypatch, style):
    target = tmp_path / 'other project'
    target.mkdir()
    inputs = {'command': 'git commit -m "change"'}
    if style == 'workdir':
        inputs['workdir'] = str(target)
    elif style == 'cd':
        inputs['command'] = f'cd "{target}" && git commit -m "change"'
    else:
        inputs['command'] = f'git -C "{target}" commit -m "change"'
    seen = []
    def policy(name, payload, cwd, env, timeout):
        seen.append((cwd, payload['tool_input']['command']))
        return subprocess.CompletedProcess([], 0, '', '')
    monkeypatch.setattr(hooks, '_run_policy', policy)
    hooks.handle_event('check-changelog', event(tmp_path, tool_input=inputs))
    assert seen and seen[0][0] == target.resolve()
    assert seen[0][1].startswith('git commit')


def test_dynamic_shell_cwd_is_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: pytest.fail('wrong-project policy'))
    code, _, _ = hooks.handle_event('check-changelog', event(tmp_path, tool_input={'command': 'cd "$OTHER" && git commit'}))
    assert code == 0


def test_patch_headers_preserve_spaces_moves_and_exclude_deletes(tmp_path):
    directory = tmp_path / 'docs/plans'
    directory.mkdir(parents=True)
    for name in ('new plan.md', 'renamed plan.md', 'deleted.md'):
        (directory / name).write_text('# Plan')
    patch = '''*** Begin Patch
*** Add File: docs/plans/new plan.md
+# New
*** Update File: docs/plans/old plan.md
*** Move to: docs/plans/renamed plan.md
@@
-old
+new
*** Delete File: docs/plans/deleted.md
*** End Patch'''
    assert hooks.patch_paths(patch, tmp_path) == [directory / 'new plan.md', directory / 'renamed plan.md']


@pytest.mark.parametrize('response', [None, {'exit_code': 1}, {'isError': True}, 'Failed to apply patch'])
def test_failed_or_unconfirmed_patch_never_launches_rescan(tmp_path, monkeypatch, response):
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: pytest.fail('failed patch mutated DB'))
    payload = event(tmp_path, 'apply_patch', 'PostToolUse', tool_response=response,
                    tool_input={'command': '*** Begin Patch\n*** Add File: ROADMAP.md\n+x\n*** End Patch'})
    assert hooks.handle_event('autoscan', payload) == (0, None, '')


def test_successful_patch_passes_truthful_session_to_shared_policy(tmp_path, monkeypatch):
    plan = tmp_path / 'ROADMAP.md'
    plan.write_text('# Plan')
    seen = []
    def policy(name, payload, cwd, env, timeout):
        seen.append((name, payload, env))
        return subprocess.CompletedProcess([], 0, '', '')
    monkeypatch.setattr(hooks, '_run_policy', policy)
    payload = event(tmp_path, 'apply_patch', 'PostToolUse',
                    tool_response='Success. Updated the following files:\nM ROADMAP.md',
                    tool_input={'command': '*** Begin Patch\n*** Update File: ROADMAP.md\n@@\n-old\n+new\n*** End Patch'})
    assert hooks.handle_event('autoscan', payload) == (0, None, '')
    assert len(seen) == 1
    assert seen[0][0] == 'dct-plan-autoscan.sh'
    assert seen[0][1]['tool_name'] == 'Edit'
    assert seen[0][1]['tool_input']['file_path'] == str(plan)
    assert seen[0][2]['DCT_ACTOR_CLIENT'] == 'codex'
    assert seen[0][2]['DCT_SESSION_ID'] == 'real-session'


def test_autoscan_without_session_identity_never_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: pytest.fail('invented session'))
    payload = event(tmp_path, 'apply_patch', 'PostToolUse', session_id='',
                    tool_response={'success': True}, tool_input={'command': '*** Begin Patch\n*** End Patch'})
    assert hooks.handle_event('autoscan', payload) == (0, None, '')


def isolated_env(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    temp = tmp_path / 'tmp'
    temp.mkdir()
    config = home / 'dct.toml'
    database = home / 'dct.db'
    config.write_text('[database]\nurl = ' + json.dumps('sqlite:///' + str(database)) + '\n')
    return {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
            'DCT_CONFIG_PATH': str(config), 'DCT_DATABASE_URL': 'sqlite:///' + str(database),
            'TMPDIR': str(temp), 'PYTHONPATH': str(Path(__file__).resolve().parent.parent),
            'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}


def test_subprocess_irrelevant_event_does_not_create_database(tmp_path):
    env = isolated_env(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'dct.integrations.codex_hooks', 'mirror'],
                            input=json.dumps(event(tmp_path, 'Bash', 'PostToolUse')),
                            text=True, capture_output=True, env=env, timeout=10, check=False)
    assert result.returncode == 0 and result.stdout == ''
    assert not (tmp_path / 'home/dct.db').exists()


def test_subprocess_reuses_real_changelog_policy(tmp_path):
    require_tools('git', 'bash', 'jq')
    from dct.core.projects import create_project
    from dct.db import create_tables, get_engine
    env = isolated_env(tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    engine = get_engine(env['DCT_DATABASE_URL'])
    create_tables(engine)
    create_project(engine, 'test', 'Test', path=str(project))
    engine.dispose()
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    (project / 'app.py').write_text('print("test")\n')
    subprocess.run(['git', '-C', str(project), 'add', 'app.py'], check=True)
    payload = event(project, tool_input={'command': 'git commit -m "change"'})
    result = subprocess.run([sys.executable, '-m', 'dct.integrations.codex_hooks', 'check-changelog'],
                            input=json.dumps(payload), text=True, capture_output=True,
                            env=env, cwd=project, timeout=15, check=False)
    assert result.returncode == 2
    assert result.stdout == ''
    assert 'changelog' in result.stderr.lower()


def test_successful_mirror_worker_preserves_identity_and_replay_id(tmp_path):
    import sqlalchemy as sa

    from dct.core.projects import create_project
    from dct.db import create_tables, get_engine

    env = isolated_env(tmp_path)
    engine = get_engine(env['DCT_DATABASE_URL'])
    create_tables(engine)
    create_project(engine, 'test', 'Test', path=str(tmp_path))
    payload = event(tmp_path, 'update_plan', 'PostToolUse', tool_use_id='actual-tool-call',
                    tool_input={'plan': [{'step': 'Verify result', 'status': 'completed'}]},
                    tool_response='Plan updated')
    for _ in range(2):
        result = subprocess.run([sys.executable, '-m', 'dct.integrations.codex_hooks', 'mirror'],
                                input=json.dumps(payload), text=True, capture_output=True,
                                env=env, timeout=15, check=False)
        assert result.returncode == 0 and result.stdout == '', result.stdout + result.stderr
    with engine.connect() as conn:
        rows = conn.execute(sa.text('SELECT actor_client, session_id, source FROM item_checkpoints')).all()
        assert [tuple(row) for row in rows] == [('codex', 'real-session', 'hook')]
        checkpoint = conn.execute(sa.text('SELECT text, kind, status FROM item_checkpoints')).one()
        assert tuple(checkpoint) == ('[codex:completed] Verify result', 'cc-task', 'pending')
        title = conn.execute(sa.text('SELECT title FROM items')).scalar_one()
        assert 'real-turn' in title and 'real-session' in title
    engine.dispose()


@pytest.mark.parametrize('mode', ['ask', 'off'])
def test_mirror_config_gate_precedes_database_bootstrap(tmp_path, mode):
    env = isolated_env(tmp_path)
    with Path(env['DCT_CONFIG_PATH']).open('a') as stream:
        stream.write(f'\n[integrations.codex]\ntask_mirror = "{mode}"\n')
    payload = event(tmp_path, 'update_plan', 'PostToolUse', tool_response='Plan updated',
                    tool_input={'plan': [{'step': 'No writes', 'status': 'pending'}]})
    result = subprocess.run([sys.executable, '-m', 'dct.integrations.codex_hooks', 'mirror'],
                            input=json.dumps(payload), text=True, capture_output=True,
                            env=env, timeout=15, check=False)
    assert result.returncode == 0 and result.stdout == ''
    assert not (tmp_path / 'home/dct.db').exists()


def test_subprocess_autoscan_uses_shared_plan_filter_and_actual_provenance(tmp_path):
    require_tools('bash', 'jq')
    import sqlalchemy as sa

    from dct.core.projects import create_project
    from dct.db import create_tables, get_engine

    env = isolated_env(tmp_path)
    project = tmp_path / 'project'
    directory = project / 'docs/plans'
    directory.mkdir(parents=True)
    plan = directory / 'native plan.md'
    plan.write_text('# Native plan\n\n## Implementation\n\n- [ ] Verify native event\n')
    nonplan = project / 'notes.md'
    nonplan.write_text('# Notes\n\n- [ ] Do not ingest\n')
    engine = get_engine(env['DCT_DATABASE_URL'])
    create_tables(engine)
    create_project(engine, 'test', 'Test', path=str(project))
    payload = event(project, 'apply_patch', 'PostToolUse',
                    tool_response='Success. Updated the following files:\nA docs/plans/native plan.md\nA notes.md',
                    tool_input={'command': '*** Begin Patch\n*** Add File: docs/plans/native plan.md\n+# Native plan\n*** Add File: notes.md\n+# Notes\n*** End Patch'})
    result = subprocess.run([sys.executable, '-m', 'dct.integrations.codex_hooks', 'autoscan'],
                            input=json.dumps(payload), text=True, capture_output=True,
                            env=env, cwd=project, timeout=15, check=False)
    assert result.returncode == 0 and result.stdout == '', result.stdout + result.stderr
    deadline = time.monotonic() + 8
    rows = []
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            rows = conn.execute(sa.text('SELECT actor_client, session_id, source FROM plans')).all()
        if rows:
            break
        time.sleep(0.05)
    assert [tuple(row) for row in rows] == [('codex', 'real-session', 'cli')]
    assert nonplan.read_text() == '# Notes\n\n- [ ] Do not ingest\n'
    engine.dispose()


def test_timeout_fails_open_with_visible_diagnostic(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('policy', 1)
    monkeypatch.setattr(hooks, '_run_policy', timeout)
    code, output, error = hooks.handle_event('check-changelog', event(tmp_path, tool_input={'command': 'git commit'}))
    assert code == 0 and error == ''
    assert 'timeout' in output['systemMessage']


def test_patch_cannot_rescan_absolute_parent_or_symlink_escape(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    outside = tmp_path / 'ROADMAP.md'
    outside.write_text('# Foreign plan')
    (project / 'ROADMAP.md').symlink_to(outside)
    patch = f'''*** Begin Patch
*** Update File: {outside}
@@
-old
+new
*** Update File: ../ROADMAP.md
@@
-old
+new
*** Update File: ROADMAP.md
@@
-old
+new
*** End Patch'''
    assert hooks.patch_paths(patch, project) == []


def test_patch_from_subdirectory_can_reach_plan_in_same_git_worktree(tmp_path):
    if not shutil.which('git'):
        pytest.skip('git unavailable')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subdirectory = tmp_path / 'src'
    subdirectory.mkdir()
    plan = tmp_path / 'ROADMAP.md'
    plan.write_text('# Plan')
    patch = '*** Begin Patch\n*** Update File: ../ROADMAP.md\n@@\n-old\n+new\n*** End Patch'
    assert hooks.patch_paths(patch, subdirectory) == [plan]


@pytest.mark.parametrize('suffix', ['echo done', 'cd /nonexistent-directory-dct-test', 'cd "$OTHER"'])
def test_later_unknown_segment_cannot_bypass_earlier_commit(tmp_path, monkeypatch, suffix):
    denied = {'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'Add changelog first'}}
    monkeypatch.setattr(hooks, '_run_policy', lambda *a, **kw: subprocess.CompletedProcess([], 2, json.dumps(denied), ''))
    payload = event(tmp_path, tool_input={'command': f'git commit -m change && {suffix}'})
    assert hooks.handle_event('check-changelog', payload) == (2, None, 'Add changelog first')


def test_known_prefix_preserves_resolved_directory_before_unknown_suffix(tmp_path):
    target = tmp_path / 'other project'
    target.mkdir()
    payload = event(tmp_path, tool_input={'command': f'cd "{target}" && git commit -m change && echo done'})
    assert hooks._shell_contexts(payload, tmp_path) == [(target.resolve(), 'git commit -m change')]


def test_unknown_prefix_still_prevents_guessing_later_commit_directory(tmp_path):
    payload = event(tmp_path, tool_input={'command': 'source user-env.sh && git commit -m change'})
    assert hooks._shell_contexts(payload, tmp_path) == []


def test_native_execution_wrapper_dispatches_successful_patch(tmp_path, monkeypatch):
    plan = tmp_path / 'ROADMAP.md'
    plan.write_text('# Plan\n')
    calls = []
    def policy(name, payload, cwd, env, timeout):
        calls.append(payload['tool_input']['file_path'])
        return subprocess.CompletedProcess([], 0, '', '')
    monkeypatch.setattr(hooks, '_run_policy', policy)
    payload = event(tmp_path, 'apply_patch', 'PostToolUse',
                    tool_input={'command': '*** Begin Patch\n*** Update File: ROADMAP.md\n@@\n-old\n+new\n*** End Patch'},
                    tool_response=f'Exit code: 0\nWall time: 0.2 seconds\nOutput:\nSuccess. Updated the following files:\nM {plan}\n')
    assert hooks.handle_event('autoscan', payload) == (0, None, '')
    assert calls == [str(plan)]


@pytest.mark.parametrize('response', [
    'Exit code: 1\nWall time: 0.2 seconds\nOutput:\nSuccess. Updated the following files:\nM ROADMAP.md\n',
    'Exit code: 0\nOutput:\nSuccess. Updated the following files:\nM ROADMAP.md\n',
    'Exit code: 0\nWall time: unknown seconds\nOutput:\nSuccess. Updated the following files:\nM ROADMAP.md\n',
    'Failure\nExit code: 0\nWall time: 0.2 seconds\nOutput:\nSuccess. Updated the following files:\nM ROADMAP.md\n',
    'Exit code: 0\nWall time: 0.2 seconds\nOutput:\nExit code: 1\nWall time: 0.1 seconds\nOutput:\nSuccess. Updated the following files:\nM ROADMAP.md\n',
    {'exit_code': 0, 'output': 'Exit code: 1\nWall time: 0.2 seconds\nOutput:\nSuccess. Updated the following files:\nM ROADMAP.md\n'},
])
def test_failed_malformed_or_nested_execution_wrapper_is_not_success(response):
    assert hooks._successful({'tool_response': response}, 'apply_patch') is False
