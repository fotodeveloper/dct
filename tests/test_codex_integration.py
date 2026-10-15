"""Codex install/repair contracts on isolated user homes."""

import json
import tomllib
from pathlib import Path

import pytest

from dct.integrations import codex
from dct.skill_render import MARKER, load_manifest, skill_names, validate_frontmatter


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
    assert set(adapter.install_skills()['skipped']) == {'pickup', 'handoff'}
    adapter.uninstall()
    assert (user / 'SKILL.md').read_text() == 'user skill'
    assert modified.read_text() == 'user revision'


def test_packaged_skill_upgrade_and_repeated_install(adapter, tmp_path, monkeypatch):
    adapter.install_skills()
    sources = tmp_path / 'assets'
    sources.mkdir()
    for name in codex.SKILLS:
        (sources / f'codex-{name}.md').write_text((codex.ASSETS / f'codex-{name}.md').read_text() + '\nNew version.\n')
        (sources / f'codex-{name}.yaml').write_bytes((codex.ASSETS / f'codex-{name}.yaml').read_bytes())
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
        assert json.loads(text.split('name: ', 1)[1].splitlines()[0]) == name
    assert 'consume="false"' in (codex.ASSETS / 'codex-pickup.md').read_text()
    assert 'context_summary' in (codex.ASSETS / 'codex-handoff.md').read_text()
    assert 'explicit' in (codex.ASSETS / 'codex-handoff.md').read_text()


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


def test_every_codex_skill_asset_carries_parsed_frontmatter_then_generated_marker():
    """Native discovery requires frontmatter at byte zero; the marker follows it."""
    entries = {entry['name']: entry for entry in load_manifest()['skills']}
    assert tuple(entries) == codex.SKILLS
    for name in codex.SKILLS:
        entry = entries[name]
        text = (codex.ASSETS / f'codex-{name}.md').read_text()
        validate_frontmatter(text, name=name, description=entry['description'],
                             explicit_only=entry['explicit_only'], host='codex')
        _, _, body = text.partition('\n---\n')
        assert body.lstrip().startswith(f'<!-- {MARKER} -->'), name
        metadata = (codex.ASSETS / f'codex-{name}.yaml').read_text()
        assert f'allow_implicit_invocation: {str(not entry["explicit_only"]).lower()}' in metadata


def test_triage_asset_is_client_neutral_and_keeps_consent():
    """#930: the Codex /triage asset gives commands without a host's shell
    prefix and keeps the decision and publication rules of the skill."""
    text = (codex.ASSETS / 'codex-triage.md').read_text()
    flat = ' '.join(text.split()).lower()
    assert '! dct' not in text and '`!' not in text
    for rule in ("decisions come only from the owner's own text",
                 "proposals never become decisions without the owner's acceptance",
                 "note and reject never create items",
                 "commit, push and release are not approved by these answers",
                 "never hand-edit its constants",
                 "return the local artifact and exact next action/command"):
        assert rule in flat, rule
    assert 'suggestions=<suggested.answers>' in flat and 'never the whole `{by, date, answers}` envelope' in flat


def test_triage_is_installed_and_a_user_owned_triage_is_kept(adapter):
    assert 'triage' in adapter.install_skills()['installed']
    adapter.uninstall()
    user = adapter.skills_path / 'triage'
    user.mkdir(parents=True)
    (user / 'SKILL.md').write_text('my own triage')
    result = adapter.install_skills()
    assert 'triage' in result['skipped'] and 'triage' not in result['installed']
    adapter.uninstall()
    assert (user / 'SKILL.md').read_text() == 'my own triage'


def _asset_backlog_call():
    """The keyword arguments of the Codex /triage asset's backlog listing at
    close, as the asset spells them (placeholders kept)."""
    import re

    flat = ' '.join((codex.ASSETS / 'codex-triage.md').read_text().split())
    calls = [m.group(1) for m in re.finditer(r'\b(?:mcp__dct__)?list_findings\(([^()]*)\)', flat)
             if 'decision="backlog"' in m.group(1)]
    assert len(calls) == 1, calls
    return dict(arg.strip().split('=', 1) for arg in calls[0].split(','))


def test_triage_asset_promotes_only_the_closing_cycles_unlinked_backlog(srv):
    """K-35: the asset's backlog call, run as written against two cycles —
    each with backlog findings, one of the closing cycle's already linked to
    an item — yields exactly the closing cycle's unlinked finding once the
    asset's skip rule (an `item_id` is set) is applied."""
    import ast

    def card(fp):
        return {'title': fp, 'fp': fp, 'S': 1, 'O': 1, 'E': 1, 'K': 'S', 'origin': 'pre',
                'type': 'robustness', 'src': 'codex', 'opts': []}

    owner = {'decided_by': 'owner', 'decision_source': 'chat'}
    earlier = srv.open_triage(code='rev-1', topic='t', created_by='me', project='test')['cycle_id']
    srv.record_findings(cycle_id=earlier, findings=[card('old backlog')])
    srv.decide_findings(cycle_id=earlier, decisions={'K-1': {'dec': 'backlog'}}, **owner)
    closing = srv.open_triage(code='rev-2', topic='t', created_by='me', project='test')['cycle_id']
    srv.record_findings(cycle_id=closing, findings=[card('linked'), card('fresh'), card('fix')])
    srv.decide_findings(cycle_id=closing, **owner, decisions={
        'K-1': {'dec': 'backlog'}, 'K-2': {'dec': 'backlog'}, 'K-3': {'dec': 'fix'}})
    rows = srv.list_findings(project='test', cycle_id=closing, limit=0)
    linked = next(r for r in rows['rows'] if r[rows['cols'].index('seq')] == 1)
    assert 'error' not in srv.promote_finding_to_item(linked[rows['cols'].index('id')])

    placeholders = {'<slug>': 'test', '<closing cycle>': closing, "<the closing cycle's id>": closing}
    kwargs = {k: placeholders[v] if v in placeholders else ast.literal_eval(v)
              for k, v in _asset_backlog_call().items()}
    assert set(kwargs) == {'project', 'cycle_id', 'decision', 'limit'}
    out = srv.list_findings(**kwargs)
    found = [dict(zip(out['cols'], r, strict=True)) for r in out['rows']]
    assert {(f['cycle_code'], f['title']) for f in found} == {('rev-2', 'linked'),
                                                             ('rev-2', 'fresh')}
    to_promote = [f['title'] for f in found if f['item_id'] is None]
    assert to_promote == ['fresh']


# --- K-37: the docs' skill lists and counts mirror the real registries -------

_REPO = Path(__file__).resolve().parent.parent


def _claude_skills() -> set[str]:
    """What the Claude installer links: every packaged dir with a SKILL.md."""
    from dct.assets import skills_dir

    return {p.parent.name for p in skills_dir().glob('*/SKILL.md')}


def _backticked(sentence: str) -> set[str]:
    import re

    return set(re.findall(r'`([a-z-]+)`', sentence))


def _sentence(text: str, start: str) -> str:
    flat = ' '.join(text.split())
    at = flat.index(start)
    return flat[at:flat.index('.', at)]


def test_readme_counts_and_lists_the_packaged_skills():
    readme = (_REPO / 'README.md').read_text(encoding='utf-8')
    flat = ' '.join(readme.split())
    claude, count = _claude_skills(), len(skill_names())
    assert claude == set(skill_names()) == set(codex.SKILLS)
    assert f'with {count} slash-command skills' in flat
    assert f'The {count} Claude Code skills, 4 Claude hooks and {count} development-version Codex skills' in flat
    assert f'{count} Codex workflows in development builds' in flat
    assert f'The {count} packaged workflows are available' in flat
    table = readme[readme.index('### Skills'):readme.index('## Global Hooks')]
    import re
    assert set(re.findall(r'^\| `/([a-z-]+)` \|', table, re.MULTILINE)) == claude
    assert _backticked(_sentence(readme, 'Codex currently packages')) == set(codex.SKILLS)
    assert _backticked(_sentence(readme, 'The adapter installs MCP configuration')) == set(codex.SKILLS)


def test_integration_guide_lists_inventory_and_managed_instructions_use_neutral_reference():
    guide = (_REPO / 'docs' / 'integrations.md').read_text(encoding='utf-8')
    count = len(skill_names())
    assert _backticked(_sentence(guide, f'The {count} packaged skills')) == set(codex.SKILLS)
    row = next(line for line in guide.splitlines() if line.startswith('| Managed workflow'))
    packaged = row.split('Packaged:')[1].split('|')[0]
    assert {s.strip() for s in packaged.split(',')} == set(codex.SKILLS)
    assert f'collection of {len(_claude_skills())}' in row
    agents = (codex.ASSETS / 'codex-agents.md').read_text(encoding='utf-8')
    assert '{{reference_path}}' in agents
    assert 'never invent a model or session identifier' in agents


@pytest.mark.parametrize('raw', [
    '# Owner settings\r\nmodel = "example"\r\n',
    '# Mixed owner bytes\r\nmodel = "example"\n[mcp_servers.other]\r\ncommand = "other"\r\n',
    '# Owner header\r\n[mcp_servers.dct]\r\ncommand = "custom"\r\nargs = []\r\n[mcp_servers.dct.env]\r\nCUSTOM = "keep"\r\n',
])
def test_mcp_edits_preserve_exact_owner_line_endings(adapter, raw):
    """Installing absent fields and repeat/uninstall must not normalize owner bytes."""
    adapter.home.mkdir(parents=True)
    adapter.config_path.write_bytes(raw.encode())
    adapter.install_mcp()
    after = adapter.config_path.read_bytes()
    if '[mcp_servers.dct]' not in raw:
        assert after.startswith(raw.encode())
    else:
        for line in raw.splitlines(keepends=True):
            assert line.encode() in after
    assert adapter.install_mcp()['status'] == 'unchanged'
    assert adapter.config_path.read_bytes() == after
    adapter.uninstall()
    remaining = adapter.config_path.read_bytes()
    if '[mcp_servers.dct]' not in raw:
        assert remaining.startswith(raw.encode())
        assert 'dct' not in tomllib.loads(remaining.decode()).get('mcp_servers', {})
    else:
        assert remaining == after


def test_missing_packaged_metadata_does_not_leave_a_retry_blocking_directory(adapter, tmp_path, monkeypatch):
    """A source read failure must not create an unowned partial installation."""
    source = tmp_path / 'source-assets'
    source.mkdir()
    (source / 'codex-action.md').write_text('Workflow')
    monkeypatch.setattr(codex, 'SKILLS', ('action',))
    monkeypatch.setattr(codex, 'ASSETS', source)
    with pytest.raises(FileNotFoundError):
        adapter.install_skills()
    assert not (adapter.skills_path / 'action').exists()
    (source / 'codex-action.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
    assert adapter.install_skills()['installed'] == ['action']
    assert (adapter.skills_path / 'action/agents/openai.yaml').read_text().endswith('false\n')


def test_edited_native_metadata_makes_the_whole_skill_foreign(adapter, tmp_path, monkeypatch):
    source = tmp_path / 'source-assets'
    source.mkdir()
    for extension, text in [('md', 'Workflow'), ('yaml', 'policy:\n  allow_implicit_invocation: false\n')]:
        (source / f'codex-action.{extension}').write_text(text)
    monkeypatch.setattr(codex, 'SKILLS', ('action',))
    monkeypatch.setattr(codex, 'ASSETS', source)
    adapter.install_skills()
    metadata = adapter.skills_path / 'action/agents/openai.yaml'
    metadata.write_text('Owner metadata')
    assert adapter.install_skills()['skipped'] == ['action']
    adapter.uninstall()
    assert metadata.read_text() == 'Owner metadata'


def test_partial_owned_skill_write_can_be_retried_without_claiming_success(adapter, tmp_path, monkeypatch):
    """A failed second asset is reported and its owned partial directory stays retryable."""
    source = tmp_path / 'source-assets'
    source.mkdir()
    (source / 'codex-action.md').write_text('Workflow')
    (source / 'codex-action.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
    monkeypatch.setattr(codex, 'SKILLS', ('action',))
    monkeypatch.setattr(codex, 'ASSETS', source)
    original = codex.atomic_write

    def interrupt(path, text):
        if path.name == 'openai.yaml':
            raise OSError('Interrupted metadata write')
        return original(path, text)

    monkeypatch.setattr(codex, 'atomic_write', interrupt)
    with pytest.raises(OSError, match='Interrupted'):
        adapter.install_skills()
    assert not (adapter.skills_path / 'action/agents/openai.yaml').exists()
    monkeypatch.setattr(codex, 'atomic_write', original)
    assert adapter.install_skills()['installed'] == ['action']
    assert (adapter.skills_path / 'action/agents/openai.yaml').read_bytes() == (source / 'codex-action.yaml').read_bytes()


def test_skill_directory_appearing_after_ownership_check_is_preserved(adapter, tmp_path, monkeypatch):
    source = tmp_path / 'source-assets'
    source.mkdir()
    (source / 'codex-action.md').write_text('Workflow')
    (source / 'codex-action.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
    monkeypatch.setattr(codex, 'SKILLS', ('action',))
    monkeypatch.setattr(codex, 'ASSETS', source)
    destination = adapter.skills_path / 'action'
    original = Path.mkdir

    def race(self, *args, **kwargs):
        if self == destination:
            original(self)
            (self / 'SKILL.md').write_text('Appeared owner skill')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', race)
    assert adapter.install_skills()['skipped'] == ['action']
    assert (destination / 'SKILL.md').read_text() == 'Appeared owner skill'
    assert not (destination / '.dct-managed-copy.json').exists()
