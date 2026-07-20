"""Service layer — shared application operations above core CRUD.

Born in G0 (CP #1235) so the G1 workflow ops (get_context_snapshot,
pickup_work, handoff_work) start life in their final home, shared by the
MCP layer and the CLI instead of being duplicated in each."""
