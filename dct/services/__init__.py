"""Service layer — reserved for shared application operations above core CRUD.

Created in G0 (CP #1235) as the intended home for the G1 workflow ops, which
ultimately landed in dct.workflows instead (K-10b): they compose conn-level
dct.core privates inside ONE transaction, which is orchestration, not a
service facade. Kept as the seam for future ops shared by MCP and the CLI —
empty on purpose, so nothing here is a dependency of dct.workflows."""
