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
wrappers in descriptions. Always pass `short_title`: a 2–4 word English noun
phrase naming what the item is about (≤ 50 chars, no item number or project
name). The create ack echoes the stored `short_title` (null when none); if the
key is missing, report an unsupported (outdated) DCT MCP server contract that
needs a restart or upgrade — do not retry the create (the item exists) and do
not claim the label was saved. Preserve returned integer IDs. Update existing
items instead of duplicating them; on `update_item` `short_title` is optional —
omit it (or pass `""`) to keep the stored label, pass it only to change the
label (`'__clear__'` removes it; a whitespace-only value also clears it).
For accepted non-trivial implementation add at least 3 checkpoints before coding (as many as the work needs, no upper cap; none for a small fix), complete each as verified, reject abandoned checkpoints
rather than deleting them — a gate checkpoint is rejected only on the user's decision (full gate policy: the `add_checkpoint` tool description). Include a user-validation gate for user-facing features
when project conventions require one. Do not claim completion without evidence or
silently bypass pending gates. Use `resolve_item` for closing and report returned
IDs and status. Do not commit or release unless explicitly authorized.

Language: A quoted question, option, screen or report fixes what it says, not
its wording: write it in the language of the conversation. Keep unchanged:
identifiers, commands, paths, file contents, commit messages, changelog
entries, short titles and anything shown verbatim.
