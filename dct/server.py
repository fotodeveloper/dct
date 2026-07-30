"""Compat shim — the MCP server moved to dct.mcp.server (CP #1235).

Kept so pre-restructure references (old registrations, external scripts)
keep importing. New code imports dct.mcp.server. Tests that monkeypatch
module globals MUST target dct.mcp.server — patches on this shim do not
reach the implementation's globals.
"""

from dct.mcp.server import *
from dct.mcp.server import mcp  # noqa: F401
