---
name: handoff
description: "[dct] Prepare and save a reviewed handoff for the next session."
---
<!-- dct-managed-codex-skill -->

# Hand off tracked work

Discover the connected DCT MCP tools if necessary. Call
`get_context_snapshot(project="")` for sprint items, checkpoints, recent changes,
gates and decisions. If project resolution fails, list projects and ask the user.

Draft a self-contained prompt stating completed work, pending checkpoints,
verification results, unresolved gates and decisions, and the exact next action.
Include enough context for another client with no conversation transcript.
Show the complete prompt and any context summary as ordinary message text before
requesting approval. Use a separate turn for substantial drafts, with a supported
question UI or a plain-text question. Save only the approved version; explicit
user-provided text and a request to save it already authorize that text.

Default to keeping sprint membership unchanged. Show and obtain approval for any
add/remove/replace proposal. When changing membership with several active sprints,
ask which sprint to target. Never discard terminal members or replace membership
with an empty list.

Call `handoff_work(prompt=<approved text>, created_by=<identity>, project="",
context_summary=<approved summary>)` once. Identity is
`codex @ <project> <UTC time>`; add real model/session identifiers only when the
host supplies them. No invented identifiers. Defaults preserve sprint membership.
For approved changes use `add_item_ids`/`remove_item_ids` CSV strings, or
`item_ids` CSV for a conscious replacement; never combine these modes. Set
`sprint` only with membership changes, and `scope` when an agreed workstream tag
helps parallel sessions distinguish their handoffs.

The composed call writes membership and handoff atomically. On error, report it
without blind retries. If the user skips saving, write no handoff. Report the
returned `handoff_id` and the precise resume point.
