"""Integration writes preserve dotfile symlinks and back up their targets."""

from pathlib import Path

import pytest

from dct.integrations.base import atomic_write
from dct.integrations.codex import CodexIntegration


@pytest.mark.parametrize("filename", ["config.toml", "AGENTS.md"])
def test_atomic_write_preserves_dotfile_symlink_and_target_permissions(tmp_path, filename):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / filename
    target.write_text("original\n")
    target.chmod(0o600)
    link = tmp_path / filename
    link.symlink_to(target)

    backup = atomic_write(link, "updated\n")

    assert link.is_symlink()
    assert link.resolve() == target
    assert target.read_text() == "updated\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert Path(backup).parent == dotfiles
    assert Path(backup).read_text() == "original\n"
    assert atomic_write(link, "updated\n") is None


def test_atomic_write_refuses_dangling_symlink_without_writes(tmp_path):
    target = tmp_path / "missing.toml"
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        atomic_write(link, "new config")

    assert link.is_symlink()
    assert not target.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def test_codex_install_updates_linked_config_without_detaching_dotfiles(tmp_path):
    target = tmp_path / "dotfiles-config.toml"
    target.write_text('model = "custom"\n')
    link = tmp_path / "config.toml"
    link.symlink_to(target)
    adapter = CodexIntegration()
    adapter.config_path = link

    adapter.install_mcp()

    assert link.is_symlink()
    assert '[mcp_servers.dct]' in target.read_text()
    assert 'model = "custom"' in target.read_text()
