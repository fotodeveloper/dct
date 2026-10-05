# dct — MCP Tools Reference

Complete catalogue of the **60 MCP tools** the dct server exposes to Claude Code, grouped by domain. ← back to the [README](../README.md).

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
| `list_projects` | List all registered projects (no CWD auto-detection — discovery only). `changelog_path` is relative to the project root of the checkout a release runs in (that checkout's toplevel + the project's path relative to its repository toplevel), not to `path` (the main checkout). |

Project registration, rename, version, and changelog-location changes are **CLI-only** (`dct project add|rename|set-version|changelog|changelog-path`) — see the [README usage](../README.md#cli).

## Items (6)

| Tool | Purpose |
|---|---|
| `create_item` | Create a tracked item (issue / todo / feature / idea / improvement). `short_title` = the item's 2-4 word English label (≤ 50 chars, refused not truncated), shown next to the item number everywhere. |
| `list_items` | List tracked items in compact `{cols, rows}` form. `view='brief'` (default) = `id, item_type, priority, status, in_current_sprint, cp, component, tags, created_at, short_title, title`; `view='full'` appends `description` (brief is the default because descriptions dominate a large backlog's payload; `search` still matches descriptions in both views; `format='dict'` is projected onto the brief columns only in the brief view; unknown view → `{"error": ...}`). `tag=<name>` narrows to items carrying that tag (unknown tag → `{"error": ...}` naming the closest known tags — up to 3, plus a count of the rest and a pointer to `list_tags`). |
| `get_item` | Get full item details — fields, notes, checkpoints, progress. |
| `update_item` | Update one or more mutable fields on an existing item. `short_title` is optional: omit (or `""`) keeps the label, a value sets it (≤ 50 chars before stripping, then stripped; whitespace-only strips to null), `'__clear__'` nulls it. `tags` is a FULL REPLACE of the item's tag set (omit = unchanged, `[]` = clear). |
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

## Changelog (9)

Entries carry the git `branch` / `worktree` they were written on (`branch_source`
= `client` when the caller supplied them, `server` when the MCP server detected
them in its own cwd; null = unknown). Detection keeps a cwd only when it is a
checkout of the SAME git repository as the project's registered path (same git
common dir — main checkout or any linked worktree, never a nested foreign repo
or a submodule); `worktree` is that checkout's toplevel. It does not identify
the project of a worktree outside the registered path — pass `project`.
A `server` value is UNCERTAIN — it names the server's own checkout, which does
not follow the client's `cd` — so `include_unbranched` filters count it like an
unbranched row, while a plain `branch` filter matches the stored branch exactly.
Stored worktree paths and the `worktree` filter share one textual normalization
(absolute only; normpath, no trailing separator; never `realpath` of a client
path); historical rows are not rewritten.

| Tool | Purpose |
|---|---|
| `add_changelog` | Add an unreleased changelog entry (version stays NULL until release); pass `branch` / `worktree` (absolute path, normalized textually — never resolved on the server). `related_item_id` must be a live item of the same project. A server-detected branch (`branch_source='server'`) adds a `warning` to the ack: confirm or correct it with `update_changelog`. |
| `list_changelog` | List changelog entries for a project; optional `branch` / `worktree` / `include_unbranched` filters. Plain `branch` = exact match on the stored branch (any source); `include_unbranched` adds every uncertain row (branch null or `branch_source='server'`). Rows carry `item_short_title` — the related item's label when it is a live item of the project, else null. |
| `count_unreleased_changelog` | Count unreleased entries (used by the commit gate — a gate only, never proof that an entry belongs to a commit); same optional filters. Zero does not block an explicit version-only release or the resume of an unfinished release. |
| `release_changelog` | Stamp all unreleased entries, an explicit `entry_ids` set, or none (`[]`, version only), set `current_version` and write the durable release record (exact entry set, immutable `candidate_sha`, `checkout_root`, optional `proof` map entry → proving commits, status `stamped`) in ONE transaction. A NEW record must hold every entry already carrying the version — an omitted legacy entry is refused, never silently added (K-61). A re-run of a recorded version with the same set returns the record (resume — also below `current_version`; also an `abandoned` record, which is never resumed) while the live entries carrying it still equal the record's set (K-94 B3); a different set, a contradicting `proof` (B2) or a drifted entry set is refused. The record also gets the project's `release_executor` and `changelog_pr_refs` render setting (fields: [release-workflow.md](release-workflow.md#the-release-ledger-record-fields)). A record stamped here carries no consent / approved proposal: `dct release resume` never performs its effects — the caller's own tool (project `release_executor` `own`) performs, verifies and reports them with `advance_release`, or it is abandoned; dct's own effects start from `dct release plan` / `prepare` / `apply` ([executor](release-workflow.md#executor-k-85)). An unrecorded version never rewinds, and a stored `current_version` outside the version contract refuses every new stamp. Omitted `entry_ids` with nothing unreleased refuses a NEW version — a version-only release needs an explicit `[]` (K-62; older servers set the version silently). |
| `get_release` | Read a release record by `version` (any status, also `completed` / `abandoned`), or — without one — the latest unfinished (non-terminal) release; `{"release": record \| null}` — record fields incl. `release_mode`, `executor`, `consent`, `proposal`, `render` ([ledger](release-workflow.md#the-release-ledger-record-fields)). Read-only. Deferred. |
| `advance_release` | Move a release record forward after a verified step: `stamped → files_written → committed → tagged → pushed → completed` (multi-step jumps allowed, same status = idempotent no-op, backwards refused, `abandoned` refused, nothing new on a terminal record; `completed` stamps `completed_at`). Records the verified `release_commit_sha` once (from `committed` on; required there — `completed` included — unless the record's `release_mode` is `without_git`, K-94 B1) and merges a write-once `manifest` of verified artifacts (same value = confirmation, different = refused; the merged manifest ≤ 64 KiB of canonical UTF-8 JSON). `candidate_sha` is immutable. The server never checks files. The ledger path (actor `ledger`): a release dct executes — executor `dct` with a recorded consent, stamped by `dct release apply` — is refused; `dct release resume` records its progress ([executor](release-workflow.md#executor-k-85)). Deferred. |
| `abandon_release` | End an unfinished release as `abandoned` (terminal) with `confirm=true` and a non-empty `reason`. Ledger only: keeps the record and every effect (no rewind, no unstamp); the version stays taken — same-set re-run returns the abandoned record, a different set is refused. A `completed` release is never abandoned. Repeating it with the same reason is a no-op; a different reason is refused (the first stays). Deferred. |
| `update_changelog` | Amend an unreleased entry (text, category, component, source, related item, branch, worktree). An omitted branch field is kept, except on a server-detected row, whose unconfirmed other field is cleared; both null → `branch_source` null. Serialized with `release_changelog` on the project lock. Deferred. |
| `delete_changelog` | Soft-delete an unreleased entry. Deferred. |
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
| `review_sprint` | Session start/end dashboard — in-sprint, recent activity, stale WIP, handoffs. Every item table carries `short_title` before `title`. |
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
| `list_items_in_sprint` | List items linked to a sprint, newest assignment first (cols `id, item_type, priority, status, short_title, title`). |

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

Length limits (every handoff tool, incl. `pickup_work` / `handoff_work`): `created_by` and `consumed_by` ≤ 200 chars, `scope` ≤ 100. A longer value is refused with `{"error": ...}` naming the field and limit — never truncated, nothing written.

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
| `get_context_snapshot` | One composed read: all active sprints (+ per-sprint completion), open member items with `cp`, their checkpoints, pending gates, live decisions (previewed), recent changes, summary. `include` CSV skips sections (`'none'` = headers only). Item rows (`sprint.items`, `sprint.stale_in_progress`, `recent_changes.items_changed`) carry `short_title` (may be null); rows referencing an item (`gates`, `sprint.checkpoints`) carry `item_short_title` = `short_title`, else the title compacted to 50 chars. |
| `pickup_work` | Snapshot + consume ONE handoff. `consume` enum: `''` auto (consumes only when exactly one candidate matches — several → caller picks and re-calls with `handoff_id`), `'true'`, `'false'` (pure read; contradictory with `handoff_id`). Snapshot reflects the state AFTER the write; the consumed row rides only in `picked_up` (full prompt). `consumed_by` is validated (non-empty, ≤ 200 chars) whenever consumption is possible (auto/true), even when nothing ends up consumed. A parallel session winning the race on a SERVER-selected target degrades to `consumed: false` (no error, snapshot is post-race); an explicit `handoff_id` still errors "not found or already consumed". |
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
| `promote_section_to_item` | Convert a plan section into a standalone item, back-linked (optional `short_title`). |
| `promote_checkpoint_to_item` | Convert a plan checkpoint into a standalone item, back-linked (optional `short_title`). |
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

## Tags (3)

Tags live in a **global** (user-scoped, no `project_id`) dictionary table plus an
`item_tags` link table — one name means the same thing in every project, while
the item lists that use it stay project-scoped. Names are normalized (collapsed
whitespace, lowercase) and unknown names auto-register on first use; a name too
**similar** to an existing one is refused with a structured suggestion, and
`add_tag(name=..., force=true)` is the deliberate override.

| Tool | Purpose |
|---|---|
| `add_tag` | Register a tag in the global dictionary (idempotent; similarity guard, `force=true` overrides; a non-empty `description` is applied when an existing active tag is reused; re-adding a soft-deleted name resurrects the same row with its id — links a `merge_tag` moved stay on the target). |
| `list_tags` | List the global tag dictionary with per-tag `item_count` (counts are global, name-ASC; unused tags show `item_count=0`). |
| `merge_tag` | Fold one tag into another — active links move to the target, the source row is soft-deleted, history is preserved. |

Item-side tag writes stay on the item tools: `create_item(tags=[...])`,
`update_item(tags=[...])` (full replace), `list_items(tag=...)`.

---

## Write-op ack shapes

Write ops return a minimal ack, not the full record. `get_item` / `get_handoff` stay full. Error paths are unchanged (`{"error": "..."}`). `progress` is the item's `{done, pending, active}` checkpoint rollup (rejected excluded).

| Tool | Ack shape |
|---|---|
| `create_item` | `{id, project, item_type, priority, status, title, short_title}` — `short_title` echoes the stored label (a missing key = a server that predates the field and ignored it) |
| `update_item` | post-state `{id, item_type, status, priority, title, short_title, component, in_current_sprint, resolved_at, progress}` (a `__clear__`ed field shows `null`) |
| `resolve_item` | `{id, status, resolved_at, progress}` |
| `add_note` | `{id, item_id, item_title}` |
| `add_checkpoint` | `{id, item_id, item_title, kind, status, sort_order}` |
| `complete_checkpoint` / `reject_checkpoint` / `reopen_checkpoint` | `{id, item_id, status, progress}` |
| `add_changelog` | `{id, project, category, component, branch, branch_source}` (+ `warning` when `branch_source='server'`) |
| `update_changelog` | `{id, project, updated_fields}` |
| `delete_changelog` | `{deleted, id}` |
| `release_changelog` | `{project, version, current_version, mode, entry_ids, released, already_released, release: record \| null}` (+ `warning`) — `release` = `{id, version, mode, entry_ids, candidate_sha, checkout_root, release_commit_sha, manifest, proof, status, created_at, updated_at, completed_at, abandoned_at, abandon_reason, release_mode, executor, consent, proposal, render}` |
| `advance_release` / `abandon_release` | `{project, version, status, updated}` (`updated` is `false` for an idempotent repeat) |
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
| `promote_section_to_item` / `promote_checkpoint_to_item` | `{id, item_type, priority, status, title, short_title}` — `short_title` echoes the stored label (a missing key = a server that predates the field) |
| `add_tag` | `{id, name, created}` (`created` is `false` when an existing tag was reused) |
| `merge_tag` | `{from, into, items_moved}` |

`assign_item_to_sprint` / `remove_item_from_sprint` and the current-sprint add/remove ops return their link rows unchanged (all ids/timestamps, no free-text to strip).

---

**Total: 60 tools.** This list is generated from `dct/mcp/server.py`; if it drifts, `dct server` prints the live set.
