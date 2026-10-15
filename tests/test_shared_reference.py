"""Both clients use one dynamically resolved reference without touching foreign files."""
from pathlib import Path

import pytest

from dct import assets, paths
from dct.integrations import claude, codex, reference


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    for name, relative in [('CLAUDE_REFERENCE', '.claude/dct-reference.md'), ('CLAUDE_MD', '.claude/CLAUDE.md'),
                           ('CLAUDE_SKILLS_DIR', '.claude/skills'), ('CLAUDE_HOOKS_DIR', '.claude/hooks'),
                           ('CLAUDE_JSON', '.claude.json'), ('CLAUDE_SETTINGS', '.claude/settings.json')]:
        monkeypatch.setattr(claude, name, tmp_path / relative)
    return claude.ClaudeIntegration(), codex.CodexIntegration()


def test_codex_only_install_has_neutral_reference_and_no_claude_alias(clients):
    _, adapter = clients
    adapter.install_instructions()
    canonical = paths.dct_reference_path()
    assert canonical.read_bytes() == assets.dct_reference_path().read_bytes()
    assert str(canonical) in adapter.instructions_path.read_text()
    assert not claude.CLAUDE_REFERENCE.exists()
    assert reference.reference_state()['state'] == 'current'


def test_changed_xdg_path_is_outdated_until_repaired_without_rewriting_owner_bytes(clients, tmp_path, monkeypatch):
    _, adapter = clients
    adapter.home.mkdir()
    adapter.instructions_path.write_bytes(b'Owner prefix\r\n')
    adapter.install_instructions()
    original = adapter.instructions_path.read_bytes()
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'new-xdg'))
    assert next(row for row in adapter.doctor() if row['check'] == 'instructions')['state'] == 'outdated'
    assert adapter.instructions_path.read_bytes() == original
    adapter.install_instructions()
    assert adapter.instructions_path.read_bytes().startswith(b'Owner prefix\r\n')
    assert str(paths.dct_reference_path()) in adapter.instructions_path.read_text()
    assert next(row for row in adapter.doctor() if row['check'] == 'instructions')['status'] == 'ok'


def test_foreign_canonical_reference_blocks_pointer_rewrite(clients):
    _, adapter = clients
    canonical = paths.dct_reference_path()
    canonical.parent.mkdir(parents=True)
    canonical.write_text('Owner guide')
    adapter.home.mkdir()
    adapter.instructions_path.write_text('Owner instructions')
    result = adapter.install_instructions()
    assert result['status'] == 'preserved'
    assert canonical.read_text() == 'Owner guide'
    assert adapter.instructions_path.read_text() == 'Owner instructions'
    assert 'move' in result['remedy']


@pytest.mark.parametrize('first', ['claude', 'codex'])
def test_either_uninstall_order_retains_shared_reference_and_other_pointer(clients, first):
    claude_adapter, codex_adapter = clients
    claude.install_reference()
    claude.attach_claude_md_snippet()
    codex_adapter.install_instructions()
    canonical = paths.dct_reference_path()
    chosen, other = (claude_adapter, codex_adapter) if first == 'claude' else (codex_adapter, claude_adapter)
    chosen.uninstall()
    assert canonical.read_bytes() == assets.dct_reference_path().read_bytes()
    other_path = codex_adapter.instructions_path if first == 'claude' else claude.CLAUDE_MD
    assert str(canonical) in other_path.read_text()
    other.uninstall()
    assert canonical.exists()


def test_reference_resolution_ignores_database_config_override(clients, tmp_path, monkeypatch):
    monkeypatch.setenv('DCT_CONFIG_PATH', str(tmp_path / 'elsewhere/database.toml'))
    assert paths.dct_reference_path() == paths.config_dir() / 'dct-reference.md'


@pytest.mark.parametrize('kind', ['file', 'symlink'])
@pytest.mark.parametrize('symlinks_available', [True, False])
def test_canonical_reference_creation_race_preserves_intruder_and_victim(clients, tmp_path, monkeypatch, kind, symlinks_available):
    """The shared extraction retains exclusive creation for both copy/link branches."""
    canonical = paths.dct_reference_path()
    victim = tmp_path / 'victim.md'
    victim.write_text('Owner victim')
    original_symlink = Path.symlink_to

    def race(self, target, *args, **kwargs):
        if self == canonical:
            if kind == 'file':
                self.write_text('Appeared guide')
            else:
                original_symlink(self, victim)
            if not symlinks_available:
                raise OSError('Symlinks unavailable')
        return original_symlink(self, target, *args, **kwargs)

    monkeypatch.setattr(Path, 'symlink_to', race)
    assert reference.install_reference()['status'] == 'preserved'
    assert victim.read_text() == 'Owner victim'
    assert canonical.read_text() == ('Appeared guide' if kind == 'file' else 'Owner victim')


def test_canonical_copy_fallback_repeats_and_preserves_edited_copy(clients, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('Symlinks unavailable')
    monkeypatch.setattr(Path, 'symlink_to', unavailable)
    first = reference.install_reference()
    assert first['mode'] == 'copy'
    assert reference.install_reference()['status'] == 'unchanged'
    canonical = paths.dct_reference_path()
    canonical.write_text(canonical.read_text() + 'Owner changed the guide')
    before = canonical.read_bytes()
    assert reference.install_reference()['status'] == 'preserved'
    assert canonical.read_bytes() == before
