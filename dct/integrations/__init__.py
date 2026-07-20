"""Host-client integration adapters (ADR #51 §6.3).

One module per client: claude.py (Claude Code — skills/hooks symlinks, MCP
registration, settings wiring). A future codex.py lands here with the same
shape. Core/db/services/workflows/mcp must never import from this package."""
