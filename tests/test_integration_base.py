"""Installer regressions across upgrades and user-owned configuration."""

import json
import shlex
from pathlib import Path

import pytest

from dct.integrations import claude, codex


def test_copied_skill_refresh_preserves_user_directory(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    (source / 'pickup').mkdir(parents=True)
    (source / 'pickup' / 'SKILL.md').write_text('v1')
    (source / 'custom').mkdir()
    (source / 'custom' / 'SKILL.md').write_text('package')
    (target / 'custom').mkdir(parents=True)
    (target / 'custom' / 'SKILL.md').write_text('user')
    monkeypatch.setattr(claude, 'SKILLS_DIR', source)
    monkeypatch.setattr(claude, 'discover_skills', lambda: sorted(p.name for p in source.iterdir() if p.is_dir()))
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', target)

    def unavailable(*args, **kwargs):
        raise OSError('symlinks unavailable')

    monkeypatch.setattr(Path, 'symlink_to', unavailable)
    claude.install_skills()
    (source / 'pickup' / 'SKILL.md').write_text('v2')
    claude.install_skills()
    assert (target / 'pickup' / 'SKILL.md').read_text() == 'v2'
    assert (target / 'custom' / 'SKILL.md').read_text() == 'user'


def test_mcp_fallback_preserves_custom_server(tmp_path, monkeypatch):
    path = tmp_path / 'claude.json'
    entry = {'command': '/custom/dct', 'args': ['server'], 'env': {'TOKEN': 'example'},
             'timeout': 99, 'disabled': True}
    path.write_text(json.dumps({'mcpServers': {'dct': entry}}))
    monkeypatch.setattr(claude, 'CLAUDE_JSON', path)
    monkeypatch.setattr(claude.shutil, 'which', lambda name: None)
    claude.register_mcp(None)
    result = json.loads(path.read_text())['mcpServers']['dct']
    assert all(result[key] == value for key, value in entry.items())


def test_hook_shell_command_preserves_space_and_metacharacters(tmp_path, monkeypatch):
    hooks = tmp_path / "space ' and $(literal)"
    monkeypatch.setattr(claude, 'CLAUDE_HOOKS_DIR', hooks)
    path = tmp_path / 'settings.json'
    claude.wire_hooks_into_settings(path)
    for groups in json.loads(path.read_text())['hooks'].values():
        for group in groups:
            for hook in group['hooks']:
                parts = shlex.split(hook['command'])
                assert len(parts) == 2
                assert Path(parts[1]).parent == hooks


def test_managed_copy_modification_is_preserved(tmp_path, monkeypatch):
    from dct.integrations.base import is_managed_copy, mark_managed_copy

    source, target = tmp_path / 'source', tmp_path / 'installed'
    (source / 'pickup').mkdir(parents=True)
    (source / 'pickup' / 'SKILL.md').write_text('package')
    (target / 'pickup').mkdir(parents=True)
    skill = target / 'pickup' / 'SKILL.md'
    skill.write_text('original')
    mark_managed_copy(target / 'pickup')
    assert is_managed_copy(target / 'pickup')
    skill.write_text('user customization')
    monkeypatch.setattr(claude, 'SKILLS_DIR', source)
    monkeypatch.setattr(claude, 'discover_skills', lambda: sorted(p.name for p in source.iterdir() if p.is_dir()))
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', target)
    claude.install_skills()
    assert skill.read_text() == 'user customization'
    assert not is_managed_copy(target / 'pickup')


def test_atomic_write_failure_preserves_original_and_cleans_temporary(tmp_path, monkeypatch):
    import pytest

    from dct.integrations import base

    path = tmp_path / 'config.json'
    path.write_text('original')

    def fail(*args):
        raise OSError('disk failure')

    monkeypatch.setattr(base.os, 'replace', fail)
    with pytest.raises(OSError, match='disk failure'):
        base.atomic_write(path, 'updated')
    assert path.read_text() == 'original'
    assert not list(tmp_path.glob('.config.json.dct-*'))
    assert next(tmp_path.glob('config.json.bak-dct-*')).read_text() == 'original'


def test_uninstall_removes_only_owned_assets(tmp_path, monkeypatch):
    from dct.integrations.base import mark_managed_copy

    source, installed = tmp_path / 'source', tmp_path / 'skills'
    source.mkdir()
    installed.mkdir()
    (source / 'pickup').mkdir()
    (installed / 'pickup').symlink_to(source / 'pickup')
    (installed / 'managed').mkdir()
    (installed / 'managed' / 'SKILL.md').write_text('owned')
    mark_managed_copy(installed / 'managed')
    (installed / 'custom').mkdir()
    (installed / 'custom' / 'SKILL.md').write_text('user')
    foreign = tmp_path / 'other-tool'
    foreign.mkdir()
    (installed / 'foreign').symlink_to(foreign)
    config = tmp_path / 'claude.json'
    config.write_text('{"mcpServers":{"dct":{"command":"custom"}}}')
    monkeypatch.setattr(claude, 'SKILLS_DIR', source)
    monkeypatch.setattr(claude, 'discover_skills', lambda: sorted(p.name for p in source.iterdir() if p.is_dir()))
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', installed)
    monkeypatch.setattr(claude, 'CLAUDE_HOOKS_DIR', tmp_path / 'hooks')
    monkeypatch.setattr(claude, 'CLAUDE_MD', tmp_path / 'CLAUDE.md')
    monkeypatch.setattr(claude, 'CLAUDE_JSON', config)
    monkeypatch.setattr(claude, 'CLAUDE_REFERENCE', tmp_path / 'dct-reference.md')
    result = claude.ClaudeIntegration().uninstall()
    assert result['removed']
    assert not (installed / 'pickup').exists()
    assert not (installed / 'managed').exists()
    assert (installed / 'custom' / 'SKILL.md').read_text() == 'user'
    assert (installed / 'foreign').is_symlink()
    assert json.loads(config.read_text())['mcpServers']['dct']['command'] == 'custom'


def test_claude_detection_requires_dct_and_doctor_reports_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, 'CLAUDE_JSON', tmp_path / 'claude.json')
    monkeypatch.setattr(claude, 'CLAUDE_REFERENCE', tmp_path / 'dct-reference.md')
    monkeypatch.setattr(claude, 'CLAUDE_MD', tmp_path / 'CLAUDE.md')
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(claude, 'CLAUDE_HOOKS_DIR', tmp_path / 'hooks')
    monkeypatch.setattr(claude, 'CLAUDE_SETTINGS', tmp_path / 'settings.json')
    monkeypatch.setattr(claude.shutil, 'which', lambda name: '/usr/bin/claude')
    claude.CLAUDE_JSON.write_text('{}')
    adapter = claude.ClaudeIntegration()
    assert not adapter.detect()
    assert all(row['status'] == 'error' for row in adapter.doctor())


def test_full_claude_adapter_installs_all_required_surfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, 'CLAUDE_JSON', tmp_path / 'claude.json')
    monkeypatch.setattr(claude, 'CLAUDE_REFERENCE', tmp_path / 'dct-reference.md')
    monkeypatch.setattr(claude, 'CLAUDE_MD', tmp_path / 'CLAUDE.md')
    monkeypatch.setattr(claude, 'CLAUDE_SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(claude, 'CLAUDE_HOOKS_DIR', tmp_path / 'hooks')
    monkeypatch.setattr(claude, 'CLAUDE_SETTINGS', tmp_path / 'settings.json')
    monkeypatch.setattr(claude.shutil, 'which', lambda name: None)
    adapter = claude.ClaudeIntegration()
    adapter.install_mcp()
    adapter.install_skills()
    adapter.install_instructions()
    monkeypatch.setattr(claude.shutil, 'which', lambda name, **kwargs: '/tmp/test-dct' if name == 'dct' else None)
    assert adapter.detect()
    assert all(row['status'] == 'ok' for row in adapter.doctor())


# ── K-67: bytes outside the managed markers stay identical ───────────────────


def _claude_attach(path):
    return claude.attach_claude_md_snippet(path)


def _codex_install(path, monkeypatch):
    adapter = codex.CodexIntegration()
    monkeypatch.setattr(adapter, 'instructions_path', path)
    return adapter.install_instructions()


@pytest.fixture(params=['claude', 'codex'])
def client(request, monkeypatch):
    """(install(path) -> result, start marker, end marker) per client."""
    if request.param == 'claude':
        return (_claude_attach, claude.SNIPPET_START, claude.SNIPPET_END)
    return (lambda path: _codex_install(path, monkeypatch), codex.START, codex.END)


def _outside(raw: bytes, start: str, end: str) -> tuple[bytes, bytes]:
    left = raw.index(start.encode())
    right = raw.index(end.encode()) + len(end.encode())
    return raw[:left], raw[right:]


PREFIX = b'# rules\r\n\r\n- keep CRLF\r\n'
SUFFIX = b'\r\n## after\r\nmixed LF line\nno final newline'


def test_refresh_keeps_crlf_prefix_and_suffix_byte_identical(tmp_path, client):
    install, start, end = client
    path = tmp_path / 'rules.md'
    path.write_bytes(PREFIX + f'{start}\r\nOLD\r\n{end}'.encode() + SUFFIX)

    result = install(path)

    raw = path.read_bytes()
    assert result['status'] == 'refreshed'
    assert _outside(raw, start, end) == (PREFIX, SUFFIX)
    block = raw[len(PREFIX):len(raw) - len(SUFFIX)]
    assert b'OLD' not in block
    assert block.count(b'\n') == block.count(b'\r\n')  # one style inside the block


def test_repeat_on_crlf_file_is_unchanged_without_write_or_backup(tmp_path, client):
    install, start, end = client
    path = tmp_path / 'rules.md'
    path.write_bytes(PREFIX + f'{start}\r\nOLD\r\n{end}'.encode() + SUFFIX)
    install(path)
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    backups = set(tmp_path.glob('rules.md.bak-dct-*'))

    result = install(path)

    assert result == {'status': 'unchanged', 'backup': None, 'path': str(path)}
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stamp
    assert set(tmp_path.glob('rules.md.bak-dct-*')) == backups


@pytest.mark.parametrize('existing', [
    b'# rules\r\n- crlf\r\n',
    b'# rules\r\n- no final newline',
    b'# lf rules\n- mixed\r\n- lines\n',
    b'  \n',
])
def test_append_keeps_existing_bytes_as_prefix(tmp_path, client, existing):
    install, _start, end = client
    path = tmp_path / 'rules.md'
    path.write_bytes(existing)

    assert install(path)['status'] == 'added'

    raw = path.read_bytes()
    assert raw.startswith(existing)
    eol = b'\r\n' if existing.startswith(b'# rules\r\n') else b'\n'
    assert raw.endswith(end.encode() + eol)
    tail = raw[len(existing):]
    assert tail.replace(eol, b'').count(b'\n') == 0  # block written in the file's style
    assert install(path)['status'] == 'unchanged'


@pytest.mark.parametrize('broken', ['{start}\r\nno end\r\n', '{end}\r\n', '{end}\r\n{start}\r\n',
                                    '{start}\r\n{start}\r\n{end}\r\n'])
def test_malformed_markers_in_crlf_file_write_nothing(tmp_path, client, broken):
    install, start, end = client
    path = tmp_path / 'rules.md'
    original = PREFIX + broken.format(start=start, end=end).encode()
    path.write_bytes(original)

    with pytest.raises(ValueError, match='nothing was written'):
        install(path)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob('rules.md.bak-dct-*'))


def test_uninstall_keeps_crlf_outside_the_block(tmp_path, monkeypatch):
    path = tmp_path / 'CLAUDE.md'
    monkeypatch.setattr(claude, 'CLAUDE_MD', path)
    for name in ('CLAUDE_JSON', 'CLAUDE_SETTINGS', 'CLAUDE_SKILLS_DIR', 'CLAUDE_HOOKS_DIR', 'CLAUDE_REFERENCE'):
        monkeypatch.setattr(claude, name, tmp_path / name)
    path.write_bytes(PREFIX + f'{claude.SNIPPET_START}\r\nx\r\n{claude.SNIPPET_END}'.encode() + SUFFIX)

    claude.ClaudeIntegration().uninstall()

    assert path.read_bytes() == PREFIX + SUFFIX


def test_codex_uninstall_keeps_crlf_outside_the_block(tmp_path, monkeypatch):
    adapter = codex.CodexIntegration()
    path = tmp_path / 'AGENTS.md'
    monkeypatch.setattr(adapter, 'instructions_path', path)
    monkeypatch.setattr(adapter, 'config_path', tmp_path / 'config.toml')
    monkeypatch.setattr(adapter, 'skills_path', tmp_path / 'skills')
    monkeypatch.setattr(adapter, '_uninstall_hooks', lambda: ([], []))
    path.write_bytes(PREFIX + f'{codex.START}\r\nx\r\n{codex.END}'.encode() + SUFFIX)

    adapter.uninstall()

    assert path.read_bytes() == PREFIX + SUFFIX
