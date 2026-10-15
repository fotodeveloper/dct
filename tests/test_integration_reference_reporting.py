"""Approved K2/K3/K9: reference conflicts/reporting preserve user content."""
import io
import json
from argparse import Namespace

import pytest
from test_shared_reference import clients as _clients_fixture

from dct import assets, cli, paths
from dct.integrations import claude, reference

clients = _clients_fixture


@pytest.mark.parametrize('canonical_conflict', [False, True])
def test_canonical_and_owned_legacy_copy_are_independently_reported_and_refreshed(clients, canonical_conflict):
    """Canonical refusal cannot strand an owned old alias or rewrite its old pointer."""
    canonical = paths.dct_reference_path()
    if canonical_conflict:
        canonical.parent.mkdir(parents=True)
        canonical.write_text('Owner canonical guide\n')
    claude.CLAUDE_REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    claude._write_marked_copy(assets.claude_snippet_path(), claude.CLAUDE_REFERENCE)
    assert claude.reference_state()['state'] == 'outdated'
    old_pointer = 'Owner preamble\n' + claude.SNIPPET_START + '\nRead ' + str(claude.CLAUDE_REFERENCE) + '\n' + claude.SNIPPET_END + '\nOwner footer\n'
    claude.CLAUDE_MD.write_text(old_pointer)
    before = canonical.read_bytes() if canonical_conflict else None

    result = claude.install_reference()

    assert claude.reference_state()['state'] == 'current'
    assert result['canonical']['path'] == str(canonical)
    assert result['legacy']['path'] == str(claude.CLAUDE_REFERENCE)
    assert result['legacy']['status'] == 'refreshed'
    if canonical_conflict:
        assert result['status'] == result['canonical']['status'] == 'preserved'
        assert canonical.read_bytes() == before
        assert claude.attach_claude_md_snippet()['status'] == 'preserved'
        assert claude.CLAUDE_MD.read_text() == old_pointer
    else:
        # Established flat result remains the legacy alias result.
        assert result['status'] == 'refreshed'
        assert result['path'] == str(claude.CLAUDE_REFERENCE)


def test_interactive_init_handles_preserved_pointer_without_backup_and_reports_both_steps(clients, engine, monkeypatch, capsys):
    """Run the real wizard/attach boundary, replacing only external setup effects."""
    canonical = paths.dct_reference_path()
    canonical.parent.mkdir(parents=True)
    canonical.write_text('Owner canonical guide\n')
    claude.CLAUDE_REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    claude._write_marked_copy(assets.claude_snippet_path(), claude.CLAUDE_REFERENCE)
    claude.CLAUDE_MD.write_text('Owner existing pointer\n')
    before = canonical.read_bytes(), claude.CLAUDE_MD.read_bytes()
    monkeypatch.setattr(cli.sys, 'stdin', type('Terminal', (io.StringIO,), {'isatty': lambda self: True})(''))
    answers = iter(['sqlite', 'n'])
    monkeypatch.setattr('builtins.input', lambda _prompt: next(answers))
    monkeypatch.setattr(cli, 'get_engine', lambda _url: engine)
    monkeypatch.setattr(claude, 'register_mcp', lambda _config: (True, 'fixture registration'))
    monkeypatch.setattr(claude, 'install_skills', lambda _config=None: False)
    monkeypatch.setattr(claude, 'install_hook', lambda _config=None: False)

    cli.cmd_init(Namespace(wire_hooks=False, attach_snippet=True))

    output = capsys.readouterr().out
    assert canonical.read_bytes() == before[0] and claude.CLAUDE_MD.read_bytes() == before[1]
    assert claude.reference_state()['state'] == 'current'
    assert str(canonical) in output and str(claude.CLAUDE_REFERENCE) in output
    assert 'pointer not updated' in output
    assert 'move' in output and 'unused name' in output
    assert 'Traceback' not in output


@pytest.mark.parametrize('client_index', [0, 1], ids=['claude', 'codex'])
def test_plain_uninstall_reports_retained_shared_reference_separately_from_preserved(clients, capsys, client_index):
    """Actual adapter retention must survive the normal non-JSON CLI renderer."""
    reference.install_reference()
    adapter = clients[client_index]
    outcome = adapter.uninstall()
    assert paths.dct_reference_path().exists()
    cli._print_integration_result({'client': adapter.name, 'action': 'uninstall', 'result': outcome}, False)
    output = capsys.readouterr().out
    retained = next(line for line in output.splitlines() if 'shared reference retained' in line)
    assert 'retained:' in retained and 'preserved:' not in retained
    assert str(paths.dct_reference_path()) in retained
    capsys.readouterr()
    cli._print_integration_result({'client': adapter.name, 'action': 'uninstall', 'result': outcome}, True)
    assert json.loads(capsys.readouterr().out)['result'] == outcome


@pytest.mark.parametrize('canonical_conflict', [False, True])
def test_independent_reference_steps_preserve_foreign_legacy_content(clients, canonical_conflict):
    """Refreshing another asset never grants ownership of the legacy path."""
    canonical = paths.dct_reference_path()
    if canonical_conflict:
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b'Foreign canonical\r\n')
    claude.CLAUDE_REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    claude.CLAUDE_REFERENCE.write_bytes(b'Foreign legacy\r\n')
    result = claude.install_reference()
    assert result['legacy']['status'] == 'preserved'
    assert claude.CLAUDE_REFERENCE.read_bytes() == b'Foreign legacy\r\n'
    if canonical_conflict:
        assert result['canonical']['status'] == 'preserved'
        assert canonical.read_bytes() == b'Foreign canonical\r\n'


def test_plain_renderer_keeps_retained_and_preserved_lines_distinct(capsys):
    cli._print_integration_result({'client': 'claude', 'action': 'uninstall',
        'result': {'preserved': ['user-edited.md'], 'retained': ['shared-reference.md']}}, False)
    text = capsys.readouterr().out
    assert 'preserved: user-edited.md' in text
    assert 'retained: shared-reference.md' in text
    assert 'preserved: shared-reference.md' not in text


def test_init_consent_names_the_real_attached_canonical_reference_under_xdg(clients, tmp_path, monkeypatch):
    """The exact question approved by the user must describe the pointer actually written."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'custom config with spaces'))
    monkeypatch.setenv('DCT_CONFIG_PATH', str(tmp_path / 'database-config.toml'))
    data_dir = tmp_path / 'fixture-data'
    data_dir.mkdir()
    monkeypatch.setenv('DCT_DATA_DIR', str(data_dir))
    monkeypatch.setattr(cli.sys, 'stdin', type('Terminal', (io.StringIO,), {'isatty': lambda self: True})(''))
    monkeypatch.setattr(claude, 'register_mcp', lambda _config: (True, 'fixture registration'))
    monkeypatch.setattr(claude, 'install_skills', lambda _config=None: False)
    monkeypatch.setattr(claude, 'install_hook', lambda _config=None: False)
    answers = iter(['sqlite', 'n', 'y'])
    questions = []

    def answer(question):
        questions.append(question)
        if question.startswith('Add a short dct pointer'):
            assert not claude.CLAUDE_MD.exists(), 'pointer was written before its consent question'
        return next(answers)

    monkeypatch.setattr('builtins.input', answer)
    cli.cmd_init(Namespace(wire_hooks=False, attach_snippet=None))

    [question] = [text for text in questions if text.startswith('Add a short dct pointer')]
    canonical = paths.dct_reference_path()
    [pointer_line] = [line for line in claude.CLAUDE_MD.read_text().splitlines()
                      if 'read `' in line and line.endswith('dct-reference.md`.')]
    named_by_pointer = pointer_line.split('read `', 1)[1][:-2]
    assert named_by_pointer == str(canonical)
    assert named_by_pointer in question
    assert '~/.claude/dct-reference.md' not in question
    assert canonical.read_bytes() == assets.dct_reference_path().read_bytes()
