# dct — MCP Tools Reference

Complete catalogue of the **48 MCP tools** the dct server exposes to Claude Code, grouped by domain. ← back to the [README](../README.md).

The dct MCP server is registered at **user scope** (`~/.claude.json`), so every tool below is callable from any Claude Code session as `mcp__dct__<name>`. Most list/create tools accept an empty `project=""` and auto-detect the project from the session's working directory (longest-prefix match); pass `project=<slug>` to override.

New tools become callable only after the Claude Code session restarts (the MCP server is a per-session subprocess). Run `dct server` in a shell to print the authoritative tool list at any time.

**Response shapes** (see the server's own instructions for the full legend):

- `list_*` → compact `{cols, rows}` — `cols` names the fields once, `rows` are value arrays in the same order. Opt into `format="markdown"` (lossy human table) or `format="dict"` (list of full-key dicts).
- `get_*` → a full semantic dict (single record, all keys, nested notes/checkpoints).
- Mutations → the updated record dict, or `{"error": "..."}` on failure.

---

## Projects (1)

| Tool | Purpose |
|---|---|
| `list_projects` | List all registered projects (no CWD auto-detection — discovery only). |

Project registration, rename, version, and changelog-location changes are **CLI-only** (`dct project add|rename|set-version|changelog|changelog-path`) — see the [README usage](../README.md#cli).

## Items (6)

| Tool | Purpose |
|---|---|
| `create_item` | Create a tracked item (issue / todo / feature / idea / improvement). |
| `list_items` | List tracked items in compact `{cols, rows}` form. |
| `get_item` | Get full item details — fields, notes, checkpoints, progress. |
| `update_item` | Update one or more mutable fields on an existing item. |
| `resolve_item` | Resolve or close an item (terminal status); auto-clears sprint flag, runs the gate-checkpoint guard. |
| `add_note` | Append a note to an item (append-only ledger). |

## Checkpoints (5)

| Tool | Purpose |
|---|---|
| `add_checkpoint` | Add an acceptance criterion (checkpoint) to an item. |
| `complete_checkpoint` | Mark a checkpoint as done. Real-time; never batch completions. |
| `reject_checkpoint` | Mark a checkpoint as rejected (scope change, N/A) — ledger-preserving. |
| `reopen_checkpoint` | Revert a checkpoint back to pending, typically from done. |
| `list_checkpoints` | List checkpoints for an item in sort order. |

## Changelog (5)

| Tool | Purpose |
|---|---|
| `add_changelog` | Add an unreleased changelog entry (version stays NULL until release). |
| `list_changelog` | List changelog entries for a project. |
| `count_unreleased_changelog` | Count unreleased entries (used by the commit gate). |
| `release_changelog` | Stamp every unreleased entry with a version and bump `current_version`. |
| `export_changelog` | Export changelog as Keep a Changelog markdown. |

## Sprints (11)

**Current-sprint helpers** — operate on the project's single active sprint:

| Tool | Purpose |
|---|---|
| `set_sprint` | Atomically replace the current sprint's membership with the given ids. |
| `clear_sprint` | Unflag every item from the current sprint (fresh planning). |
| `review_sprint` | Session start/end dashboard — in-sprint, recent activity, stale WIP, handoffs. |
| `add_item_to_current_sprint` | Add ONE item to the current sprint, auto-creating it if needed. |
| `remove_item_from_current_sprint` | Remove ONE item from the current sprint (no-op if absent). |

**First-class sprints** — named, lifecycle-tracked units of work:

| Tool | Purpose |
|---|---|
| `create_sprint` | Create a named, time-/scope-boxed sprint under a project. |
| `update_sprint` | Update a sprint's fields, enforcing the lifecycle whitelist. |
| `list_sprints` | List a project's sprints, newest first, optionally filtered. |
| `assign_item_to_sprint` | Link an item to a sprint by explicit id (idempotent). |
| `remove_item_from_sprint` | Soft-remove the item↔sprint link by explicit id. |
| `list_items_in_sprint` | List items linked to a sprint, newest assignment first. |

## Handoffs (5)

| Tool | Purpose |
|---|---|
| `create_handoff` | Leave a handoff draft for the next session. |
| `list_handoffs` | List session handoffs for a project. |
| `consume_handoff` | Mark a handoff as consumed (preserves the row in the ledger). |
| `delete_handoff` | Soft-delete a handoff (hide from lists, preserve for audit). |
| `update_handoff` | Amend an unconsumed handoff in place — preserves id + created_at. |

## Plans (11)

| Tool | Purpose |
|---|---|
| `create_plan` | Register a plan row programmatically (e.g. a DB-only roadmap). |
| `list_plans` | List plans in a project, with optional filters. |
| `get_plan` | Full plan dict with nested sections + checkpoints + progress rollup. |
| `add_plan_section` | Append a section to a plan. |
| `list_plan_sections` | List sections in a plan, optionally filtered by parent or type. |
| `complete_plan_checkpoint` | Mark a plan checkpoint as done. Real-time; never batch. |
| `reject_plan_checkpoint` | Mark a plan checkpoint as rejected (scope change, N/A). |
| `promote_section_to_item` | Convert a plan section into a standalone item, back-linked. |
| `promote_checkpoint_to_item` | Convert a plan checkpoint into a standalone item, back-linked. |
| `promote_section_to_sprint` | Materialize a `## Sprint N` plan section as a first-class sprint. |
| `ingest_plan_file` | Ingest a markdown plan/spec/roadmap/ADR file into dct. |

See [docs/plans.md](plans.md) for the plan lifecycle, ULID anchors, and ingest pipeline.

## Decisions (4)

| Tool | Purpose |
|---|---|
| `create_decision` | Create a decision record (ADR / sprint amendment / inline / open question). |
| `list_decisions` | List decisions newest-first, optionally filtered. |
| `update_decision` | Edit a decision's fields in place (empty string = leave unchanged). |
| `supersede_decision` | Mark one decision as superseded by another. |

---

**Total: 48 tools.** This list is generated from `dct/server.py`; if it drifts, `dct server` prints the live set.
