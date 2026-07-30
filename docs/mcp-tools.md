# dct — MCP Tools Reference

Complete catalogue of the **52 MCP tools** the dct server exposes to Claude Code, grouped by domain. ← back to the [README](../README.md).

The dct MCP server is registered at **user scope** (`~/.claude.json`), so every tool below is callable from any Claude Code session as `mcp__dct__<name>`. Most list/create tools accept an empty `project=""` and auto-detect the project from the session's working directory (longest-prefix match); pass `project=<slug>` to override.

New tools become callable only after the Claude Code session restarts (the MCP server is a per-session subprocess). Run `dct server` in a shell to print the authoritative tool list at any time.

**Response shapes** (see the server's own instructions for the full legend):

- `list_*` → compact `{cols, rows}` — `cols` names the fields once, `rows` are value arrays in the same order. Opt into `format="markdown"` (lossy human table) or `format="dict"` (list of full-key dicts).
- `get_*` → a full semantic dict (single record, all keys, nested notes/checkpoints).
- **Write ops → a minimal ack**, not the full record echo: the created/updated `id` plus a few post-state scalars. Fetch full context deliberately — `get_item(id)` (notes, checkpoints, progress) or `get_handoff(id)` (untruncated prompt + context_summary). Per-tool ack shapes are listed below; failures return `{"error": "..."}`.

**Deferral / `alwaysLoad`**: Claude Code defers all MCP tools by default. dct exempts exactly **13 tools** — the 10 mid-session daily drivers (`create_item`, `get_item`, `list_items`, `update_item`, `resolve_item`, `add_note`, `add_checkpoint`, `complete_checkpoint`, `reject_checkpoint`, `add_changelog`) plus the 3 composed workflow entry points (`get_context_snapshot`, `pickup_work`, `handoff_work` — they ARE the session edge, so the ToolSearch round-trip they used to pay was 100% of pre-work latency) — via the `anthropic/alwaysLoad` `_meta` hint so they load at session start. Every other tool (granular handoffs, sprints, changelog release, plans, decisions) loads on demand through a one-time ToolSearch `select:<name>` round-trip. Takes effect on server restart.

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

**Current-sprint helpers** — operate on the project's primary active sprint
(the most recently started; several sprints may be active in parallel).
**Reads may fall back to the primary sprint; WRITES may not**: `set_sprint`,
`clear_sprint`, `add_item_to_current_sprint` and `remove_item_from_current_sprint`
ERROR when several sprints are active (listing the candidates) rather than
guessing a target — pick one explicitly with `assign_item_to_sprint` /
`remove_item_from_sprint` (or `handoff_work(sprint=<id-or-code>, ...)`).

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

## Handoffs (6)

| Tool | Purpose |
|---|---|
| `create_handoff` | Leave a handoff draft for the next session. |
| `list_handoffs` | List session handoffs for a project; the `prompt` column is a 200-char preview. |
| `get_handoff` | Full handoff dict — untruncated `prompt` + `context_summary` for a single id. |
| `consume_handoff` | Mark a handoff as consumed (preserves the row in the ledger). |
| `delete_handoff` | Soft-delete a handoff (hide from lists, preserve for audit). |
| `update_handoff` | Amend an unconsumed handoff in place — preserves id + created_at. |

`list_handoffs` truncates the `prompt` to a 200-char preview (with a trailing `…` when longer) across all formats; call `get_handoff(id)` for the full text.

## Workflow ops (3)

Composed session-edge operations (#413) — ONE implementation in
`dct/workflows/session.py` shared by MCP, the CLI (`dct pickup` /
`dct handoff create`) and the packaged /pickup + /handoff skills. Each runs in
a single DB transaction. **Prefer these over chaining the granular tools**:
`pickup_work` replaces `review_sprint` + `list_handoffs` + `get_handoff` +
`consume_handoff` (4-8 calls → 1-2); `handoff_work` replaces `set_sprint` +
`create_handoff` (atomic — a failed sprint write leaves no handoff).

| Tool | Purpose |
|---|---|
| `get_context_snapshot` | One composed read: all active sprints (+ per-sprint completion), open member items with `cp`, their checkpoints, pending gates, live decisions (previewed), recent changes, summary. `include` CSV skips sections (`'none'` = headers only). |
| `pickup_work` | Snapshot + consume ONE handoff. `consume` enum: `''` auto (consumes only when exactly one candidate matches — several → caller picks and re-calls with `handoff_id`), `'true'`, `'false'` (pure read; contradictory with `handoff_id`). Snapshot reflects the state AFTER the write; the consumed row rides only in `picked_up` (full prompt). `consumed_by` required for auto/true. A parallel session winning the race on a SERVER-selected target degrades to `consumed: false` (no error, snapshot is post-race); an explicit `handoff_id` still errors "not found or already consumed". |
| `handoff_work` | Sprint membership + handoff in one transaction. Session edges are ADDITIVE by default (ADR #57 — sprints are multi-session): `add_item_ids` / `remove_item_ids` pull items in / cut scope. `item_ids` is the REPLACE mode — `'__keep__'` (default, sprint untouched) / CSV (strict set); `''` or a delimiter-only string ERRORS (emptying a sprint is `clear_sprint`'s job), and replace is mutually exclusive with the additive pair. TERMINAL members (resolved / wont_fix) are the sprint's completion history: immune to replace, and removing one errors. All id lists are strict — unknown/foreign ids abort everything. `sprint` selects among multiple active sprints (id or code; id match takes precedence, an id↔code collision errors); ambiguity errors instead of guessing, and a selector without any membership change errors too. |

Structured data only: snapshots carry facts (no `recommended_action` — the
interpretation belongs to the consuming skill/assistant).

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

## Write-op ack shapes

Write ops return a minimal ack, not the full record. `get_item` / `get_handoff` stay full. Error paths are unchanged (`{"error": "..."}`). `progress` is the item's `{done, pending, active}` checkpoint rollup (rejected excluded).

| Tool | Ack shape |
|---|---|
| `create_item` | `{id, project, item_type, priority, status, title}` |
| `update_item` | post-state `{id, item_type, status, priority, title, component, in_current_sprint, resolved_at, progress}` (a `__clear__`ed field shows `null`) |
| `resolve_item` | `{id, status, resolved_at, progress}` |
| `add_note` | `{id, item_id, item_title}` |
| `add_checkpoint` | `{id, item_id, item_title, kind, status, sort_order}` |
| `complete_checkpoint` / `reject_checkpoint` / `reopen_checkpoint` | `{id, item_id, status, progress}` |
| `add_changelog` | `{id, project, category, component}` |
| `create_handoff` | `{id, project, scope, created_at}` |
| `handoff_work` | `{handoff_id, project, scope, created_at, sprint: {sprint_id, code, open_size, items} \| null}` — no prompt echo |
| `pickup_work` | snapshot + `{picked_up: full handoff \| null, consumed: bool}` (read-heavy op — not a slim ack) |
| `update_handoff` | `{id, updated_fields}` (list of applied field names) |
| `consume_handoff` | `{id, consumed_at}` |
| `create_sprint` / `update_sprint` / `promote_section_to_sprint` | `{id, code, kind, status, name}` |
| `create_decision` / `update_decision` / `supersede_decision` | `{id, ulid, kind, ref_code, status, title}` |
| `create_plan` | `{id, ulid, slug, kind, status, title}` |
| `add_plan_section` | `{id, ulid, heading, depth, section_type, status, sort_order}` |
| `complete_plan_checkpoint` / `reject_plan_checkpoint` | `{id, plan_section_id, status, sort_order}` |
| `promote_section_to_item` / `promote_checkpoint_to_item` | `{id, item_type, priority, status, title}` |

`assign_item_to_sprint` / `remove_item_from_sprint` and the current-sprint add/remove ops return their link rows unchanged (all ids/timestamps, no free-text to strip).

---

**Total: 52 tools.** This list is generated from `dct/mcp/server.py`; if it drifts, `dct server` prints the live set.
