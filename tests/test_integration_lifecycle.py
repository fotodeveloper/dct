"""Lifecycle diagnostics must report partial or stale installations honestly."""

import json
import sys

from dct import cli
from dct.integrations import claude, manager
from dct.integrations.base import mark_managed_copy


def test_claude_mcp_rejects_disabled_and_missing_binary(tmp_path, monkeypatch):
    path = tmp_path / 'claude.json'
    monkeypatch.setattr(claude, 'CLAUDE_JSON', path)
    monkeypatch.setattr(claude, 'CLAUDE_MD', tmp_path / 'instructions')
    monkeypatch.setattr(claude, 'CLAUDE_SETTINGS', tmp_path / 'settings')
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(claude, 'CLAUDE_HOOKS_DIR', tmp_path / 'hooks')
    monkeypatch.setattr(claude.shutil, 'which', lambda *args, **kwargs: None)
    for entry in ({'command': 'missing', 'args': ['server']},
                  {'command': sys.executable, 'disabled': True, 'args': ['server']}):
        path.write_text(json.dumps({'mcpServers': {'dct': entry}}))
        checks = claude.ClaudeIntegration().doctor()
        assert any(c['status'] == 'error' and c['check'].startswith('mcp') for c in checks)


def test_refresh_reports_stale_skills_without_requiring_instruction_opt_in(monkeypatch):
    class Adapter:
        name = 'codex'
        from pathlib import Path
        instructions_path = Path('/nonexistent/dct-instruction-test')
        def detect(self): return True
        def install_skills(self): return {'skipped': ['pickup']}
        def doctor(self):
            return [{'check': 'skill:pickup', 'status': 'error', 'message': 'modified'},
                    {'check': 'instructions', 'status': 'error', 'message': 'not opted in'}]

    class Missing:
        def detect(self): return False

    monkeypatch.setattr(manager, 'get_integration', lambda name: Adapter() if name == 'codex' else Missing())
    result = manager.refresh_integrations()[0]
    assert result['ok'] is False
    assert 'modified' in result['error']
    assert 'not opted in' not in result['error']


def test_uninstall_text_reports_manual_cleanup_instead_of_ready(monkeypatch, capsys):
    class Adapter:
        def uninstall(self):
            return {'status': 'partial', 'removed': [],
                    'message': 'MCP settings preserved for manual review'}

    monkeypatch.setattr(manager, 'get_integration', lambda name: Adapter())
    cli.cmd_integrate(cli.build_parser().parse_args(['integrate', 'uninstall', 'claude']))
    output = capsys.readouterr().out
    assert 'manual review' in output
    assert 'ready' not in output


def test_claude_doctor_rejects_old_managed_skill(tmp_path, monkeypatch):
    source, installed = tmp_path / 'source', tmp_path / 'installed'
    (source / 'pickup').mkdir(parents=True)
    (source / 'pickup' / 'SKILL.md').write_text('v2')
    (installed / 'pickup').mkdir(parents=True)
    (installed / 'pickup' / 'SKILL.md').write_text('v1')
    mark_managed_copy(installed / 'pickup')
    monkeypatch.setattr(claude, 'SKILLS_DIR', source)
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', installed)
    for name in ('CLAUDE_JSON', 'CLAUDE_MD', 'CLAUDE_SETTINGS', 'CLAUDE_HOOKS_DIR'):
        monkeypatch.setattr(claude, name, tmp_path / name)
    checks = claude.ClaudeIntegration().doctor()
    assert next(c for c in checks if c['check'] == 'skills')['status'] == 'error'


def test_claude_mcp_uses_configured_path_environment(tmp_path, monkeypatch):
    executable = tmp_path / 'only-in-configured-path'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o755)
    for name in ('CLAUDE_JSON', 'CLAUDE_MD', 'CLAUDE_SETTINGS', 'CLAUDE_HOOKS_DIR', 'CLAUDE_SKILLS_DIR'):
        monkeypatch.setattr(claude, name, tmp_path / name)
    claude.CLAUDE_JSON.write_text(json.dumps({'mcpServers': {'dct': {
        'command': executable.name, 'args': ['server'], 'env': {'PATH': str(tmp_path)},
    }}}))
    assert next(c for c in claude.ClaudeIntegration().doctor() if c['check'] == 'mcp')['status'] == 'ok'
