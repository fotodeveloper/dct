"""Tests for the dct init installer (setup.py)."""

import dct.integrations.claude as setup

_ALL_HOOKS = [
    "dct-check-changelog.sh",
    "dct-check-changelog-on-stop.sh",
    "dct-plan-autoscan.sh",
    "dct-task-mirror.sh",
]


def test_install_hook_creates_symlinks(tmp_path, monkeypatch):
    """install_hook symlinks each dct hook to the repo source (#224) so a fix in
    the repo is live without reinstall (no copy drift — the thing that masked the
    #223 clog-count bug timeline)."""
    dst_dir = tmp_path / "claude-hooks"
    monkeypatch.setattr(setup, "CLAUDE_HOOKS_DIR", dst_dir)

    assert setup.install_hook(config=None) is True

    link = dst_dir / "dct-check-changelog.sh"
    assert link.is_symlink()
    assert link.resolve() == (setup.HOOKS_DIR / "check-changelog.sh").resolve()

    # every installed hook is a symlink, never a copy
    for name in _ALL_HOOKS:
        p = dst_dir / name
        if p.is_symlink() or p.exists():
            assert p.is_symlink(), f"{name} must be a symlink, not a copy"


def test_install_hook_replaces_existing_copy(tmp_path, monkeypatch):
    """A pre-#224 regular-file copy is migrated to a symlink in place."""
    dst_dir = tmp_path / "claude-hooks"
    dst_dir.mkdir(parents=True)
    monkeypatch.setattr(setup, "CLAUDE_HOOKS_DIR", dst_dir)

    stale = dst_dir / "dct-check-changelog.sh"
    stale.write_text("# stale copy from a pre-#224 install\n")
    assert stale.is_file() and not stale.is_symlink()

    setup.install_hook(config=None)

    assert stale.is_symlink()
    assert stale.resolve() == (setup.HOOKS_DIR / "check-changelog.sh").resolve()
