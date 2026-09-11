"""Host-client integration adapters (ADR #51 §6.3).

One module per client implements base.ClientIntegration: claude.py preserves
the legacy installer; codex.py provides MCP, skills and managed instructions.
manager.py dispatches lifecycle operations; doctor.py checks runtime drift.
Core/db/services/workflows/mcp must never import from this package."""
