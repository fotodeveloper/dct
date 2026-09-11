---
name: track
description: "[dct] Create or update tracked issues, todos, features, ideas and improvements."
---
<!-- dct-managed-codex-skill -->

# Track work

Use connected DCT MCP tools, discovering their current schemas when necessary.
Resolve the project from CWD (`project=""`); ask only if unresolved. Search
`list_items` before creating to avoid duplicates. Use `get_item` when an ID is
provided. Select `item_type` from intent: bug → issue, task → todo, new capability
→ feature, proposal → idea, refactor → improvement. Use todo when neutral.

Create via `create_item` with separate top-level fields; never embed tool-call
wrappers in descriptions. Preserve returned integer IDs. Update existing items
instead of duplicating them. For accepted non-trivial implementation add 3–7
checkpoints before coding, complete each as verified, reject abandoned checkpoints
rather than deleting them. Include a user-validation gate for user-facing features
when project conventions require one. Do not claim completion without evidence or
silently bypass pending gates. Use `resolve_item` for closing and report returned
IDs and status. Do not commit or release unless explicitly authorized.
