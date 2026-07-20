"""G0 package restructure contract (ADR #51/#52, CP #1235).

Target namespaces: core / db / services / workflows / mcp / cli /
integrations / assets. Code moves; PUBLIC contracts stay frozen: the `dct`
entry point, MCP tool names, DB schema — and the PHYSICAL skill/hook asset
paths (dct/skills, dct/hooks), because the host's ~/.claude symlinks point
into them; those directories move only when a breaking window opens (2.0.0).
Old import paths (dct.server, dct.setup) remain as thin compat shims.
"""

import importlib
from pathlib import Path


def test_mcp_server_lives_in_mcp_package():
    mod = importlib.import_module("dct.mcp.server")
    assert hasattr(mod, "mcp")


def test_claude_integration_lives_in_integrations_package():
    mod = importlib.import_module("dct.integrations.claude")
    for name in (
        "install_skills",
        "install_hook",
        "register_mcp",
        "wire_hooks_into_settings",
        "prune_dangling_symlinks",
        "SKILLS_DIR",
        "HOOKS_DIR",
    ):
        assert hasattr(mod, name), name


def test_legacy_import_paths_are_compat_shims():
    """dct.server and dct.setup keep importing — external references and old
    registrations must not break."""
    server_shim = importlib.import_module("dct.server")
    setup_shim = importlib.import_module("dct.setup")
    mcp_mod = importlib.import_module("dct.mcp.server")
    claude_mod = importlib.import_module("dct.integrations.claude")
    assert server_shim.mcp is mcp_mod.mcp
    assert setup_shim.install_skills is claude_mod.install_skills


def test_future_service_layers_exist():
    for name in ("dct.services", "dct.workflows"):
        importlib.import_module(name)


def test_assets_indirection_points_at_frozen_dirs():
    """Physical skills/hooks dirs are FROZEN (host symlink targets); the
    assets package is the single indirection the eventual 2.0.0 move will
    flip."""
    assets = importlib.import_module("dct.assets")
    pkg_root = Path(importlib.import_module("dct").__file__).parent
    assert assets.skills_dir() == pkg_root / "skills"
    assert assets.hooks_dir() == pkg_root / "hooks"
    assert assets.skills_dir().is_dir()
    assert assets.hooks_dir().is_dir()


def test_claude_integration_uses_assets_indirection():
    claude_mod = importlib.import_module("dct.integrations.claude")
    assets = importlib.import_module("dct.assets")
    assert claude_mod.SKILLS_DIR == assets.skills_dir()
    assert claude_mod.HOOKS_DIR == assets.hooks_dir()
