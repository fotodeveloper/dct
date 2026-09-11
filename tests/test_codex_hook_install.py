"""Native Codex hook installation preserves user configuration and ownership."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from dct.integrations import codex, manager


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex home'))
    return codex.CodexIntegration()


def _commands(data):
    return [handler['command'] for groups in data['hooks'].values()
            for group in groups for handler in group.get('hooks', [])]


def test_native_command_parser_preserves_windows_and_posix_paths_with_spaces():
    windows = r'"C:\Program Files\Python\python.exe" -m dct.integrations.codex_hooks mirror'
    posix = "'/opt/Python Runtime/python' -m dct.integrations.codex_hooks mirror"

    assert codex._split_command(windows, windows=True) == [
        r'C:\Program Files\Python\python.exe', '-m', 'dct.integrations.codex_hooks', 'mirror',
    ]
    assert codex._split_command(posix, windows=False) == [
        '/opt/Python Runtime/python', '-m', 'dct.integrations.codex_hooks', 'mirror',
    ]


def test_install_hooks_adds_four_native_definitions_and_preserves_user_hook(adapter):
    adapter.home.mkdir(parents=True)
    user_group = {'matcher': '^Bash$', 'hooks': [{'type': 'command', 'command': 'user-check'}]}
    adapter.hooks_path.write_text(json.dumps({'description': 'user', 'hooks': {
        'PreToolUse': [user_group], 'SessionStart': [{'hooks': [{'type': 'command', 'command': 'hello'}]}],
    }}))

    result = adapter.install_hooks()
    data = json.loads(adapter.hooks_path.read_text())

    assert result['installed'] == list(codex.HOOK_ACTIONS)
    assert data['description'] == 'user'
    assert data['hooks']['PreToolUse'][0] == user_group
    assert data['hooks']['SessionStart'] == [{'hooks': [{'type': 'command', 'command': 'hello'}]}]
    assert len([command for command in _commands(data)
                if 'dct.integrations.codex_hooks' in command]) == 4
    assert data['hooks']['PostToolUse'][0]['hooks'][0]['async'] is True
    assert data['hooks']['PostToolUse'][1]['hooks'][0]['async'] is True
    assert data['hooks']['PreToolUse'][-1]['matcher'] == '^Bash$'
    assert data['hooks']['PostToolUse'][0]['matcher'] == '^apply_patch$'
    assert data['hooks']['PostToolUse'][1]['matcher'] == '^update_plan$'
    for action in codex.HOOK_ACTIONS:
        command = next(command for command in _commands(data) if command.endswith(' ' + action))
        assert shlex.split(command) == [sys.executable, '-m', 'dct.integrations.codex_hooks', action]
    marker = json.loads(adapter.hooks_ownership_path.read_text())
    assert marker['owner'] == 'dct'
    assert set(marker['entries']) == set(codex.HOOK_ACTIONS)


def test_install_hooks_is_idempotent(adapter):
    adapter.install_hooks()
    hooks = adapter.hooks_path.read_bytes()
    ownership = adapter.hooks_ownership_path.read_bytes()

    result = adapter.install_hooks()

    assert result['status'] == 'unchanged'
    assert adapter.hooks_path.read_bytes() == hooks
    assert adapter.hooks_ownership_path.read_bytes() == ownership


def test_refresh_hooks_only_updates_exact_previously_owned_entries(adapter, monkeypatch):
    adapter.install_hooks()
    monkeypatch.setattr(codex.sys, 'executable', '/new python/bin/python')

    result = adapter.refresh_hooks()
    commands = _commands(json.loads(adapter.hooks_path.read_text()))

    assert result['refreshed'] == list(codex.HOOK_ACTIONS)
    assert all(shlex.split(command)[0] == '/new python/bin/python'
               for command in commands if 'dct.integrations.codex_hooks' in command)


def test_refresh_hooks_without_ownership_is_a_noop(adapter):
    adapter.home.mkdir(parents=True)
    original = '{"hooks":{"PreToolUse":[]}}\n'
    adapter.hooks_path.write_text(original)

    assert adapter.refresh_hooks()['status'] == 'not_owned'
    assert adapter.hooks_path.read_text() == original


def test_modified_owned_hook_is_preserved(adapter):
    adapter.install_hooks()
    data = json.loads(adapter.hooks_path.read_text())
    data['hooks']['PreToolUse'][0]['hooks'][0]['command'] = 'customized-command'
    adapter.hooks_path.write_text(json.dumps(data))

    result = adapter.install_hooks()

    current = json.loads(adapter.hooks_path.read_text())
    assert current['hooks']['PreToolUse'][0]['hooks'][0]['command'] == 'customized-command'
    assert 'check-changelog' in result['skipped_modified']


def test_unexpected_hook_shapes_and_invalid_json_are_never_rewritten(adapter):
    adapter.home.mkdir(parents=True)
    for raw in ('{"hooks":[]}', '{broken'):
        adapter.hooks_path.write_text(raw)
        with pytest.raises(ValueError):
            adapter.install_hooks()
        assert adapter.hooks_path.read_text() == raw


def test_inline_dct_hook_refuses_duplicate_json_install(adapter):
    adapter.home.mkdir(parents=True)
    raw_hooks = '{"hooks":{"SessionStart":[]}}\n'
    adapter.hooks_path.write_text(raw_hooks)
    adapter.config_path.write_text(
        '[hooks.state."trusted-entry"]\ntrusted_hash = "sha256:fixture"\n'
        '[[hooks.PreToolUse]]\nmatcher = "^Bash$"\n'
        '[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
        f'command = {json.dumps(codex._module_command("check-changelog"))}\n'
    )

    with pytest.raises(ValueError, match='inline.*check-changelog'):
        adapter.install_hooks()

    assert adapter.hooks_path.read_text() == raw_hooks
    assert not adapter.hooks_ownership_path.exists()


def test_unrelated_inline_hook_does_not_block_json_install(adapter):
    adapter.home.mkdir(parents=True)
    original = ('[hooks.state."trusted-entry"]\ntrusted_hash = "sha256:fixture"\n'
                '[[hooks.PreToolUse]]\nmatcher = "^Bash$"\n'
                '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "user-check"\n')
    adapter.config_path.write_text(original)

    assert adapter.install_hooks()['installed'] == list(codex.HOOK_ACTIONS)
    assert adapter.config_path.read_text() == original


def test_repeated_install_after_runtime_trust_preserves_native_state(adapter):
    adapter.install_hooks()
    state = ('# Codex-managed trust metadata\n'
             '[hooks.state."/tmp/hooks.json:pre_tool_use:0:0"]\n'
             'trusted_hash = "sha256:fixture"\n')
    adapter.config_path.write_text(state)

    result = adapter.install_hooks()

    assert result['status'] == 'unchanged'
    assert adapter.config_path.read_text() == state


def test_invalid_ownership_sidecar_is_never_used(adapter):
    adapter.home.mkdir(parents=True)
    adapter.hooks_path.write_text('{"hooks":{}}')
    adapter.hooks_ownership_path.write_text('{"owner":"someone-else","entries":{}}')

    with pytest.raises(ValueError, match='ownership'):
        adapter.install_hooks()

    assert adapter.hooks_path.read_text() == '{"hooks":{}}'


def test_doctor_separates_installed_definitions_from_runtime_trust(adapter):
    adapter.install_hooks()

    checks = {row['check']: row for row in adapter.doctor()}

    assert checks['hooks']['status'] == 'ok'
    assert checks['hooks_trust']['status'] == 'warning'
    assert '/hooks' in checks['hooks_trust']['message']


def test_doctor_reports_modified_owned_definition(adapter):
    adapter.install_hooks()
    data = json.loads(adapter.hooks_path.read_text())
    data['hooks']['PostToolUse'][0]['matcher'] = '^Write$'
    adapter.hooks_path.write_text(json.dumps(data))

    checks = {row['check']: row for row in adapter.doctor()}

    assert checks['hooks']['status'] == 'error'


def test_doctor_reports_missing_shared_hook_dependency(adapter, monkeypatch):
    adapter.install_hooks()
    available = {'bash': '/bin/bash', 'git': '/usr/bin/git'}
    monkeypatch.setattr(codex.shutil, 'which', available.get)

    checks = {row['check']: row for row in adapter.doctor()}

    assert checks['hook_dependencies']['status'] == 'error'
    assert 'Missing: jq' in checks['hook_dependencies']['message']
    assert 'mirror uses the pinned Python runtime only' in checks['hook_dependencies']['message']


def test_doctor_accepts_owned_hooks_pinned_to_another_available_python(
        adapter, tmp_path, monkeypatch):
    installed_python = tmp_path / 'installed runtime' / 'python'
    installed_python.parent.mkdir()
    installed_python.write_text('')
    installed_python.chmod(0o755)
    verifier_python = codex.sys.executable
    monkeypatch.setattr(codex.sys, 'executable', str(installed_python))
    adapter.install_hooks()
    monkeypatch.setattr(codex.sys, 'executable', verifier_python)

    checks = {row['check']: row for row in adapter.doctor()}

    assert checks['hooks']['status'] == 'ok'
    assert checks['hook_dependencies']['status'] == 'ok'


def test_uninstall_removes_only_unchanged_owned_hooks_and_sidecar(adapter):
    adapter.install_hooks()
    data = json.loads(adapter.hooks_path.read_text())
    data['hooks']['PostToolUse'][0]['matcher'] = '^user-modified$'
    custom = {'matcher': '^Bash$', 'hooks': [{'type': 'command', 'command': 'user-hook'}]}
    data['hooks']['PostToolUse'].append(custom)
    adapter.hooks_path.write_text(json.dumps(data))

    result = adapter.uninstall()
    current = json.loads(adapter.hooks_path.read_text())

    assert not adapter.hooks_ownership_path.exists()
    assert custom in current['hooks']['PostToolUse']
    assert any(group.get('matcher') == '^user-modified$'
               for group in current['hooks']['PostToolUse'])
    assert current['hooks']['PostToolUse'][1] == {'hooks': [], 'matcher': '^update_plan$'}
    assert len([command for command in _commands(current)
                if 'dct.integrations.codex_hooks' in command]) == 1
    assert {item for item in result['removed'] if item.startswith('hook:')} == {
        'hook:check-changelog', 'hook:stop', 'hook:mirror',
    }
    assert 'hook:autoscan: user-owned or modified' in result['retained']


def test_legacy_missing_script_reference_is_repaired_conservatively(adapter):
    hooks_dir = adapter.home / 'hooks'
    hooks_dir.mkdir(parents=True)
    user_policy = hooks_dir / 'user-policy.sh'
    user_policy_group = {'matcher': 'CustomTool', 'hooks': [
        {'type': 'command', 'command': f'bash {shlex.quote(str(user_policy))}'},
    ]}
    legacy_mirror = {'matcher': 'TodoWrite|TaskCreate', 'hooks': [
        {'type': 'command', 'command': f'bash {shlex.quote(str(hooks_dir / "dct-task-mirror.sh"))}'},
    ]}
    data = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': f'bash {shlex.quote(str(hooks_dir / "dct-check-changelog.sh"))}'},
    ]}, user_policy_group, legacy_mirror], 'Stop': [{'hooks': [
        {'type': 'command', 'command': f'bash {shlex.quote(str(hooks_dir / "dct-check-changelog-on-stop.sh"))}'},
    ]}], 'PostToolUse': [{'matcher': 'Edit|Write|MultiEdit', 'hooks': [
        {'type': 'command', 'command': f'bash {shlex.quote(str(hooks_dir / "dct-plan-autoscan.sh"))}'},
    ]}]}}
    adapter.hooks_path.write_text(json.dumps(data))

    result = adapter.install_hooks()
    commands = _commands(json.loads(adapter.hooks_path.read_text()))

    assert result['legacy_repaired'] == sorted(
        codex.LEGACY_HOOK_SCRIPTS - {'dct-task-mirror.sh'})
    assert result['legacy_retired'] == ['dct-task-mirror.sh']
    assert f'bash {shlex.quote(str(user_policy))}' in commands
    current = json.loads(adapter.hooks_path.read_text())
    assert current['hooks']['PreToolUse'][1] == user_policy_group
    assert current['hooks']['PreToolUse'][2] == {
        'hooks': [], 'matcher': 'TodoWrite|TaskCreate',
    }
    assert not any(script in command for script in codex.LEGACY_HOOK_SCRIPTS
                   for command in commands)


def test_existing_legacy_script_reference_is_preserved(adapter):
    hooks_dir = adapter.home / 'hooks'
    hooks_dir.mkdir(parents=True)
    existing = hooks_dir / 'dct-check-changelog-on-stop.sh'
    existing.write_text('# user retained this script')
    command = f'bash {shlex.quote(str(existing))}'
    adapter.hooks_path.write_text(json.dumps({'hooks': {'Stop': [
        {'hooks': [{'type': 'command', 'command': command}]},
    ]}}))

    adapter.install_hooks()

    assert command in _commands(json.loads(adapter.hooks_path.read_text()))


def test_missing_legacy_script_under_custom_matcher_is_preserved(adapter):
    hooks_dir = adapter.home / 'hooks'
    hooks_dir.mkdir(parents=True)
    command = f'bash {shlex.quote(str(hooks_dir / "dct-check-changelog.sh"))}'
    custom = {'matcher': '^CustomTool$', 'hooks': [{'type': 'command', 'command': command}]}
    adapter.hooks_path.write_text(json.dumps({'hooks': {'PreToolUse': [custom]}}))

    adapter.install_hooks()

    current = json.loads(adapter.hooks_path.read_text())
    assert current['hooks']['PreToolUse'][0] == custom
    assert command in _commands(current)


def test_manager_installs_hooks_only_for_adapters_that_support_them():
    calls = []

    class Adapter:
        name = 'codex'
        def install_mcp(self): calls.append('mcp'); return {}
        def install_skills(self): calls.append('skills'); return {}
        def install_instructions(self): calls.append('instructions'); return {}
        def install_hooks(self): calls.append('hooks'); return {}
        def detect(self): return True
        def doctor(self): return []

    result = manager.install_integration(Adapter())

    assert calls == ['mcp', 'skills', 'instructions', 'hooks']
    assert 'install_hooks' in result['steps']


def test_upgrade_refreshes_hooks_only_through_optional_refresh_method(tmp_path, monkeypatch):
    calls = []

    class CodexAdapter:
        name = 'codex'
        instructions_path = tmp_path / 'missing-instructions'
        def detect(self): return True
        def install_skills(self): calls.append('skills'); return {}
        def refresh_hooks(self): calls.append('refresh-hooks'); return {}
        def doctor(self): return [{'check': 'hooks', 'status': 'ok', 'message': 'installed'}]

    class MissingAdapter:
        def detect(self): return False

    monkeypatch.setattr(manager, 'get_integration',
                        lambda name: CodexAdapter() if name == 'codex' else MissingAdapter())

    result = manager.refresh_integrations()

    assert calls == ['skills', 'refresh-hooks']
    assert result[0]['ok'] is True


def test_upgrade_does_not_gate_claude_on_optional_hook_wiring(monkeypatch):
    class ClaudeAdapter:
        name = 'claude'
        def detect(self): return True
        def doctor(self):
            return [{'check': 'hooks', 'status': 'error', 'message': 'not opted in'},
                    {'check': 'hook_assets', 'status': 'ok', 'message': 'installed'}]

    class MissingAdapter:
        def detect(self): return False

    monkeypatch.setattr(manager, 'get_integration',
                        lambda name: ClaudeAdapter() if name == 'claude' else MissingAdapter())
    monkeypatch.setattr('dct.integrations.claude.install_skills', lambda: None)
    monkeypatch.setattr('dct.integrations.claude.install_hook', lambda: None)
    monkeypatch.setattr('dct.integrations.claude.prune_dangling_symlinks', list)

    result = manager.refresh_integrations()

    assert result[0]['ok'] is True
    assert [check['check'] for check in result[0]['checks']] == ['hook_assets']
