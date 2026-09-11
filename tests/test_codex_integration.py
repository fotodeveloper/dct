"""Codex install/repair contracts on isolated user homes."""

import tomllib
from pathlib import Path

import pytest

from dct.integrations import codex


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'custom codex'))
    return codex.CodexIntegration()


def config(adapter, raw):
    adapter.home.mkdir(parents=True, exist_ok=True)
    adapter.config_path.write_text(raw)


def test_install_reinstall_uninstall_preserves_user_content(adapter):
    original = '# User settings\nmodel = "example-model"\n[mcp_servers.other]\ncommand = "other"\n'
    config(adapter, original)
    assert not adapter.detect()
    first = adapter.install_mcp()
    assert Path(first['backup']).read_text() == original
    after = adapter.config_path.read_text()
    assert adapter.install_mcp()['status'] == 'unchanged'
    assert adapter.config_path.read_text() == after
    adapter.install_skills()
    adapter.instructions_path.write_text('# User instructions\n')
    adapter.install_instructions()
    assert adapter.detect()
    result = adapter.uninstall()
    assert 'mcp' in result['removed']
    assert adapter.config_path.read_text().startswith(original)
    assert tomllib.loads(adapter.config_path.read_text())['mcp_servers'] == {'other': {'command': 'other'}}
    assert '# User instructions' in adapter.instructions_path.read_text()
    assert not adapter.detect()


def test_repairs_missing_actor_without_overwriting_custom_settings(adapter):
    raw = '# preserve comments\n[mcp_servers."dct"] # custom\ncommand = "/opt/custom dct"\nargs = ["server", "--custom"]\nenabled = false\n[mcp_servers.dct.env]\nCUSTOM = "retained"\n'
    config(adapter, raw)
    adapter.install_mcp()
    result = tomllib.loads(adapter.config_path.read_text())['mcp_servers']['dct']
    assert result == {'command': '/opt/custom dct', 'args': ['server', '--custom'], 'enabled': False,
                      'env': {'CUSTOM': 'retained', **codex.ACTOR_ENV}}
    assert '# preserve comments' in adapter.config_path.read_text()
    assert '# custom' in adapter.config_path.read_text()
    assert adapter.install_mcp()['status'] == 'unchanged'
    assert adapter.uninstall()['retained'] == ['mcp: pre-existing or customized registration']


@pytest.mark.parametrize('raw', ['[broken', '[mcp_servers]\ndct = 42', '[mcp_servers]\ndct = {command="dct", env={X="x"}}'])
def test_invalid_or_unsupported_layout_is_not_written(adapter, raw):
    config(adapter, raw)
    with pytest.raises(ValueError):
        adapter.install_mcp()
    assert adapter.config_path.read_text() == raw
    assert not list(adapter.home.glob('*.bak*'))


def test_inline_complete_config_needs_no_rewrite(adapter):
    raw = '[mcp_servers]\ndct={command="custom", args=[], env={DCT_ACTOR_CLIENT="codex", DCT_ACTOR_TYPE="agent"}}\n'
    config(adapter, raw)
    assert adapter.install_mcp()['status'] == 'unchanged'


def test_header_inside_multiline_string_is_not_edited(adapter):
    raw = 'text = """\n[mcp_servers.dct.env]\n"""\n[mcp_servers.dct]\ncommand = "dct"\nargs = ["server"]\n'
    config(adapter, raw)
    adapter.install_mcp()
    parsed = tomllib.loads(adapter.config_path.read_text())
    assert parsed['text'] == '[mcp_servers.dct.env]\n'
    assert parsed['mcp_servers']['dct']['env'] == codex.ACTOR_ENV


def test_preserves_custom_actor_and_warns(adapter):
    config(adapter, '[mcp_servers.dct]\ncommand="dct"\nargs=["server"]\n[mcp_servers.dct.env]\nDCT_ACTOR_CLIENT="custom"\n')
    adapter.install_mcp()
    assert tomllib.loads(adapter.config_path.read_text())['mcp_servers']['dct']['env']['DCT_ACTOR_CLIENT'] == 'custom'
    assert next(r for r in adapter.doctor() if r['check'] == 'actor')['status'] == 'error'


def test_customized_managed_registration_is_retained(adapter):
    adapter.install_mcp()
    adapter.config_path.write_text(adapter.config_path.read_text().replace('command = "dct"', 'command = "custom"'))
    assert adapter.uninstall()['retained'] == ['mcp: pre-existing or customized registration']


def test_unknown_and_modified_skills_are_preserved(adapter):
    user = adapter.skills_path / 'pickup'
    user.mkdir(parents=True)
    (user / 'SKILL.md').write_text('user skill')
    assert adapter.install_skills()['skipped'] == ['pickup']
    modified = adapter.skills_path / 'handoff' / 'SKILL.md'
    modified.write_text('user revision')
    assert adapter.install_skills()['skipped'] == ['pickup', 'handoff']
    adapter.uninstall()
    assert (user / 'SKILL.md').read_text() == 'user skill'
    assert modified.read_text() == 'user revision'


def test_packaged_skill_upgrade_and_repeated_install(adapter, tmp_path, monkeypatch):
    adapter.install_skills()
    sources = tmp_path / 'assets'
    sources.mkdir()
    for name in codex.SKILLS:
        (sources / f'codex-{name}.md').write_text((codex.ASSETS / f'codex-{name}.md').read_text() + '\nNew version.\n')
    monkeypatch.setattr(codex, 'ASSETS', sources)
    adapter.install_skills()
    assert (adapter.skills_path / 'pickup' / 'SKILL.md').read_text().endswith('New version.\n')
    assert adapter.install_skills()['skipped'] == []
    assert not list(adapter.skills_path.rglob('*.bak*'))
    adapter.uninstall()
    assert not (adapter.skills_path / 'pickup').exists()


def test_symlink_skill_is_not_followed(adapter, tmp_path):
    outside = tmp_path / 'user-owned'
    outside.mkdir()
    (outside / 'SKILL.md').write_text('do not alter')
    adapter.skills_path.mkdir(parents=True)
    (adapter.skills_path / 'pickup').symlink_to(outside, target_is_directory=True)
    assert 'pickup' in adapter.install_skills()['skipped']
    adapter.uninstall()
    assert (outside / 'SKILL.md').read_text() == 'do not alter'


def test_malformed_instructions_refuse_repair(adapter):
    adapter.home.mkdir()
    adapter.instructions_path.write_text('User text\n' + codex.START)
    with pytest.raises(ValueError):
        adapter.install_instructions()
    assert adapter.instructions_path.read_text() == 'User text\n' + codex.START


def test_doctor_does_not_install_hooks_or_mutate(adapter):
    adapter.home.mkdir()
    hooks = adapter.home / 'hooks.json'
    hooks.write_text('{"command":"bash /missing/dct-hook.sh"}')
    rows = adapter.doctor()
    assert next(row for row in rows if row['check'] == 'hooks')['status'] == 'warning'
    assert not adapter.config_path.exists()
    assert not adapter.skills_path.exists()
    assert hooks.read_text() == '{"command":"bash /missing/dct-hook.sh"}'


def test_remote_mcp_configuration_is_preserved(adapter):
    raw = '[mcp_servers.dct]\nurl="https://example.invalid/mcp"\n'
    config(adapter, raw)
    with pytest.raises(ValueError, match='remote URL'):
        adapter.install_mcp()
    assert adapter.config_path.read_text() == raw


def test_shipped_workflows_use_client_neutral_tools_and_consent():
    for name in codex.SKILLS:
        text = (codex.ASSETS / f'codex-{name}.md').read_text()
        assert 'AskUserQuestion' not in text
        assert 'claude-' not in text
        assert 'name: ' + name in text
    assert 'consume="false"' in (codex.ASSETS / 'codex-pickup.md').read_text()
    assert 'approved text' in (codex.ASSETS / 'codex-handoff.md').read_text()


def test_uninstall_ignores_marker_text_inside_multiline_string(adapter):
    raw = 'example = """\n' + codex.MCP_BLOCK + '\n"""\n' + codex.MCP_BODY + '\n'
    config(adapter, raw)
    assert adapter.uninstall()['retained'] == ['mcp: pre-existing or customized registration']
    assert adapter.config_path.read_text() == raw


def test_doctor_reports_outdated_owned_skills(adapter, tmp_path, monkeypatch):
    adapter.install_skills()
    source = tmp_path / 'new-assets'
    source.mkdir()
    for asset in codex.ASSETS.glob('codex-*.md'):
        (source / asset.name).write_text(asset.read_text() + '\nUpdated.\n')
    monkeypatch.setattr(codex, 'ASSETS', source)
    rows = adapter.doctor()
    assert next(row for row in rows if row['check'] == 'skill:pickup')['status'] == 'error'


def test_doctor_marks_missing_artifacts_as_errors(adapter):
    rows = {row['check']: row['status'] for row in adapter.doctor()}
    assert rows['instructions'] == 'error'
    assert rows['skill:pickup'] == 'error'
    assert rows['actor'] == 'error'


@pytest.mark.parametrize('filename,check', [('AGENTS.md', 'instructions'), ('hooks.json', 'hooks')])
def test_doctor_handles_unreadable_text(adapter, filename, check):
    adapter.home.mkdir()
    (adapter.home / filename).write_bytes(b'\xff')
    rows = {row['check']: row for row in adapter.doctor()}
    assert rows[check]['status'] == 'error'


@pytest.mark.parametrize('content,status', [('# User override\n', 'error'), ('', 'ok'), (' \n', 'ok')])
def test_doctor_detects_instruction_override_without_modifying_it(adapter, content, status):
    adapter.install_instructions()
    override = adapter.home / 'AGENTS.override.md'
    override.write_text(content)
    rows = {row['check']: row for row in adapter.doctor()}
    assert rows['instructions_shadowed']['status'] == status
    adapter.install_instructions()
    assert override.read_text() == content


@pytest.mark.parametrize('kind', ['invalid_utf8', 'directory'])
def test_doctor_reports_instruction_override_read_errors(adapter, kind):
    adapter.install_instructions()
    override = adapter.home / 'AGENTS.override.md'
    if kind == 'directory':
        override.mkdir()
    else:
        override.write_bytes(b'\xff')
    rows = {row['check']: row for row in adapter.doctor()}
    assert rows['instructions_shadowed']['status'] == 'error'
