"""Compat shim — the Claude Code integration moved to
dct.integrations.claude (CP #1235).

Kept so pre-restructure references keep importing. New code imports
dct.integrations.claude. Tests that monkeypatch module globals (SKILLS_DIR,
CLAUDE_*, _still_symlink) MUST target dct.integrations.claude — patches on
this shim do not reach the implementation's globals.
"""

from dct.integrations.claude import *  # noqa: F401,F403
from dct.integrations.claude import (  # noqa: F401
    CLAUDE_HOOKS_DIR,
    CLAUDE_JSON,
    CLAUDE_SETTINGS,
    CLAUDE_SKILLS_DIR,
    HOOKS_DIR,
    SKILLS_DIR,
    _still_symlink,
)
