# dct — MCP Tools Reference

Complete catalogue of the **76 MCP tools** the dct server exposes to MCP clients, grouped by domain. ← back to the [README](../README.md).

The dct MCP server is registered at **user scope** in the supported client configuration (Claude: `~/.claude.json`; Codex adapter: `$CODEX_HOME/config.toml`). Claude Code exposes tools as `mcp__dct__<name>`; other hosts control their namespaces and discovery. Most list/create tools accept an empty `project=""` and auto-detect the project from the session's working directory (longest-prefix match); pass `project=<slug>` to override.

New tools become callable only after the client reconnects or the session restarts (the MCP server is a per-session subprocess). Run `dct server` in a shell to print the authoritative tool list at any time.

**Response shapes** (see the server's own instructions for the full legend):

- `list_*` → compact `{cols, rows}` — `cols` names the fields once, `rows` are value arrays in the same order. Opt into `format="markdown"` (lossy human table) or `format="dict"` (list of full-key dicts).
- `get_*` → a full semantic dict (single record, all keys, nested notes/checkpoints).
- **Write ops → a minimal ack**, not the full record echo: the created/updated `id` plus a few post-state scalars. Fetch full context deliberately — `get_item(id)` (notes, checkpoints, progress) or `get_handoff(id)` (untruncated prompt + context_summary). Per-tool ack shapes are listed below; failures return `{"error": "..."}`.

**Deferral / `alwaysLoad`**: Claude Code defers all MCP tools by default. dct exempts exactly **13 tools** — the 10 mid-session daily drivers (`create_item`, `get_item`, `list_items`, `update_item`, `resolve_item`, `add_note`, `add_checkpoint`, `complete_checkpoint`, `reject_checkpoint`, `add_changelog`) plus the 3 composed workflow entry points (`get_context_snapshot`, `pickup_work`, `handoff_work` — they ARE the session edge, so the ToolSearch round-trip they used to pay was 100% of pre-work latency) — via the `anthropic/alwaysLoad` `_meta` hint so they load at session start. Every other tool (granular handoffs, sprints, changelog release, plans, decisions, the triage ledger) loads on demand through a one-time ToolSearch `select:<name>` round-trip. Takes effect on server restart.

---

## Projects (1)

| Tool | Purpose |
|---|---|
| `list_projects` | List all registered projects (no CWD auto-detection — discovery only). `changelog_path` with `changelog_path_explicit` (#876 — [release-workflow.md](release-workflow.md#the-changelog-file-876)): true = the owner chose that path (also exactly `changelog.md`), false = dct detects the tracked changelog (the default `changelog.md` is used only when none is found) — the value alone never says which. The path is relative to the project root of the checkout a release runs in (that checkout's toplevel + the project's path relative to its repository toplevel), not to `path` (the main checkout). `commit_co_authored_by` / `commit_session_link`: the stored commit-trailer preferences (a `Co-Authored-By` trailer / a session or artifact link) — null = not answered yet, false = a stored "no", true = a stored "yes"; asked once, stored with `dct project trailers <slug> --co-authored-by on\|off --session-link on\|off`, applied by /commit without asking (a project's own rules forbidding a trailer still win). |

Project registration, rename, version, and changelog-location changes are **CLI-only** (`dct project add|rename|set-version|changelog|changelog-path`) — see the [README usage](../README.md#cli).

## Items (6)

| Tool | Purpose |
|---|---|
| `create_item` | Create a tracked item (issue / todo / feature / idea / improvement). `short_title` = the item's 2-4 word English label (≤ 50 chars, refused not truncated), shown next to the item number everywhere. |
| `list_items` | List tracked items in compact `{cols, rows}` form. `view='brief'` (default) = `id, item_type, priority, status, in_current_sprint, cp, component, tags, created_at, short_title, title`; `view='full'` appends `description` (brief is the default because descriptions dominate a large backlog's payload; `search` still matches descriptions in both views; `format='dict'` is projected onto the brief columns only in the brief view; unknown view → `{"error": ...}`). `tag=<name>` narrows to items carrying that tag (unknown tag → `{"error": ...}` naming the closest known tags — up to 3, plus a count of the rest and a pointer to `list_tags`). `gate='any'` or a gate kind (trimmed, case-insensitive) keeps items with a pending matching gate, in a sprint or not; `task` / `cc-task` are refused. |
| `get_item` | Get full item details — fields, notes, checkpoints, progress. |
| `update_item` | Update one or more mutable fields on an existing item. `short_title` is optional: omit (or `""`) keeps the label, a value sets it (≤ 50 chars before stripping, then stripped; whitespace-only strips to null), `'__clear__'` nulls it. `tags` is a FULL REPLACE of the item's tag set (omit = unchanged, `[]` = clear). `reason` (optional) is the why of the change, written as an item note in the update's transaction (a refused update writes none). A terminal `status` (`resolved` / `wont_fix`) runs the same gate-checkpoint guard as `resolve_item` (one shared check, same message): with a gate pending the whole call is refused — no field, tag or note written, no gate rejected for you; the message names the pending gates and how to handle them. |
| `resolve_item` | Resolve or close an item (terminal status); auto-clears sprint flag, runs the gate-checkpoint guard (the same check `update_item` runs on a terminal status). A missing or soft-deleted item is "not found" before the gate check, as in `update_item`. A refusal (`Cannot resolve item #N: K gate checkpoint(s) pending — …`) names up to 5 pending gates as one-line entries — id, kind and compacted text, quotes and control characters escaped — (``(+M more — get_item(N) or `dct show N` lists them all)`` beyond) and says how to handle them: complete a gate once its recorded condition is met (a person's act — dogfood, approval — only on that person's confirmation), reject one that does not apply only on the user's decision. |
| `add_note` | Append a note to an item (append-only ledger). |

## Checkpoints (5)

| Tool | Purpose |
|---|---|
| `add_checkpoint` | Add an acceptance criterion (checkpoint) to an item. Its description is the packaged gate-checkpoint policy: any kind but `task` / `cc-task` is a gate (unknown kinds too) that blocks closing the item while pending; every new user-facing feature (a new screen, command, endpoint, skill or MCP tool) ends with a `dogfood` gate, except internal refactors/docs/CI, bug fixes with a regression test and features behind a not-yet-enabled flag; when in doubt, add the gate. A gate is completed by its recorded condition — a person's act only on that person's confirmation — and rejected only on the user's decision. Refused: empty text, a blank kind, a kind longer than its 32-char column. |
| `complete_checkpoint` | Mark a checkpoint as done. Real-time; never batch completions. |
| `reject_checkpoint` | Mark a checkpoint as rejected (scope change, N/A) — ledger-preserving. Rejecting a gate is the sanctioned skip, only on the user's decision. |
| `reopen_checkpoint` | Revert a checkpoint back to pending, typically from done; a reopened gate blocks closing the item again. |
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
| `import_released_changelog` | Import one approved historical row via `core.changelog.import_released_entry`; separate project/category/entry/version and optional released_at/component/source_file/source_lines/related_item_id parameters. Original version spelling is preserved; recorded-version/future-version refusals write no row. Stated date = midnight UTC; timestamps require an offset; omitted date is explicitly import time. Ack `{id, project, category, version, released_at, released_at_source}` (`stated` or `import_time`). No branch attribution, release record or current_version change; per-row progress, no automatic dedup or whole-file atomicity. Deferred. |
| `list_changelog` | List changelog entries for a project; optional `branch` / `worktree` / `include_unbranched` filters. Plain `branch` = exact match on the stored branch (any source); `include_unbranched` adds every uncertain row (branch null or `branch_source='server'`). Rows carry `item_short_title` — the related item's label when it is a live item of the project, else null. |
| `count_unreleased_changelog` | Count unreleased entries (used by the commit gate — a gate only, never proof that an entry belongs to a commit); same optional filters. Zero does not block an explicit version-only release or the resume of an unfinished release. |
| `release_changelog` | Stamp all unreleased entries, an explicit `entry_ids` set, or none (`[]`, version only), set `current_version` and write the durable release record (exact entry set, immutable `candidate_sha`, `checkout_root`, optional `proof` map entry → proving commits, status `stamped`) in ONE transaction. A `pr` number in that map is stored as the caller's declaration — `pr_source: "client"`, whatever the map states; only `dct release apply` records `git` ([origin](release-workflow.md#origin-of-a-pr-number-k-116-h7)); more than 50 `associations` of one entry are refused, never cut. A NEW record must hold every entry already carrying the version — an omitted legacy entry is refused, never silently added (K-61). A re-run of a recorded version with the same set returns the record (resume — also below `current_version`; also an `abandoned` record, which is never resumed) while the live entries carrying it still equal the record's set (K-94 B3); a different set, a contradicting `proof` (B2) or a drifted entry set is refused. The record also gets the project's `release_executor` and `changelog_pr_refs` render setting (fields: [release-workflow.md](release-workflow.md#the-release-ledger-record-fields)). A record stamped here carries no consent / approved proposal: `dct release resume` never performs its effects — the caller's own tool (project `release_executor` `own`) performs, verifies and reports them with `advance_release`, or it is abandoned; a record whose release commit already exists can also be handed to dct by the owner with the local `dct release takeover` (K-119 M3: its verified artifacts become the consent, no re-stamp); dct's own effects start from `dct release plan` / `prepare` / `apply` ([executor](release-workflow.md#executor-k-85)). Optional `expected_executor` (`dct` \| `own`, K-106): compared with the project's CURRENT `release_executor` inside the stamp transaction (for a recorded version: with the record's) — a mismatch refuses before any write. A ledger holding several live records of the version refuses (K-115 G6). An unrecorded version never rewinds, and a stored `current_version` outside the version contract refuses every new stamp. Omitted `entry_ids` with nothing unreleased refuses a NEW version — a version-only release needs an explicit `[]` (K-62; older servers set the version silently). |
| `get_release` | Read a release record by `version` (any status, also `completed` / `abandoned`), or — without one — the latest unfinished (non-terminal) release; `{"release": record \| null}` — record fields incl. `release_mode`, `executor`, `consent`, `proposal`, `render`, `tag_proof`, `push_proof` ([ledger](release-workflow.md#the-release-ledger-record-fields)); on an ambiguous ledger (several live records of one version) the oldest record plus `duplicates: [{id, version, status}]` (K-115 G6). Read-only. Deferred. |
| `advance_release` | Move a release record forward after a verified step: `stamped → files_written → committed → tagged → pushed → completed` (multi-step jumps allowed, same status = idempotent no-op, backwards refused, `abandoned` refused, nothing new on a terminal record; `completed` stamps `completed_at`). Records the verified `release_commit_sha` once (from `committed` on; required there — `completed` included — unless the record's `release_mode` is `without_git`, K-94 B1) and merges a write-once `manifest` of verified artifacts (same value = confirmation, different = refused; the merged manifest ≤ 64 KiB of canonical UTF-8 JSON). A status is never proof (K-115 G4): a record with git reaches `tagged` / `pushed` / `completed` only with `tag_proof` (the tag as the caller verified it: `{name, object, commit, annotated}`, its commit = the release commit), `pushed` only with `push_proof` (`{target, tag_commit, branch_tip}`; with a consent the target must be the approved one, and `completed` needs it too when a push was approved) — both write-once ([proofs](release-workflow.md#proof-of-the-tag-and-of-the-push-k-115)). Every call re-checks the record's full entry set (K-102) and the mutual consistency of record / consent / proposal / render / proofs, and a ledger holding several live records of the version refuses (K-115). `candidate_sha` is immutable. The server never checks files. The ledger path (actor `ledger`): a release dct executes — executor `dct` with a recorded consent, stamped by `dct release apply` or taken over with `dct release takeover` — is refused; `dct release resume` records its progress ([executor](release-workflow.md#executor-k-85)). Deferred. |
| `abandon_release` | End an unfinished release as `abandoned` (terminal) with `confirm=true` and a non-empty `reason`. Ledger only: keeps the record and every effect (no rewind, no unstamp); the version stays taken — same-set re-run returns the abandoned record, a different set is refused. A `completed` release is never abandoned. Repeating it with the same reason is a no-op; a different reason is refused (the first stays). NOT for a release dct's effects layer executes (executor `dct` with a recorded consent, stamped by `dct release apply` or taken over with `dct release takeover`): the server never guesses the checkout and cannot take that release's operation lock, so it REFUSES and names the local `dct release abandon --version V --reason "…" --confirm` (K-119 M6); records of the ledger path (an own tool's, or stamped by `release_changelog`) are abandoned here. Deferred. |
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

Actor identity (`created_by` / `consumed_by`) is nonempty free text where required, not a parsed model/project/time format. The true client alone suffices. Include model/session/link only when host-supplied, a project suffix only after a prior successful DCT response or explicit user slug, and UTC only from an actual supplied clock/host time context/time tool. Omit unverified components entirely; never use directory/worktree/task-title guesses, placeholders or approximate time. Creation and consumption follow the same rule. This adds no AUTO preflight; server-created ledger timestamps stay separate.

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
| `get_context_snapshot` | Genuinely read-only end-to-end, including CWD lookup: no bootstrap, file/directory creation, DDL or journal-mode change; missing/old schema refuses with explicit `dct db upgrade`. SQLite WAL/SHM sidecars may be created by SQLite. Registered `readOnlyHint=true` applies only here and implies no guarantee about host approval prompts. One composed read: all active sprints (+ per-sprint completion), open member items with `cp`, their checkpoints, pending gates of every open item (sprint or not; `in_current_sprint` marks membership), live decisions (previewed), recent changes, summary. `include` CSV skips sections (`'none'` = headers only). Item rows (`sprint.items`, `sprint.stale_in_progress`, `recent_changes.items_changed`) carry `short_title` (may be null); rows referencing an item (`gates`, `sprint.checkpoints`) carry `item_short_title` = `short_title`, else the title compacted to 50 chars. |
| `pickup_work` | Snapshot + consume ONE handoff. `consume` enum: `''` auto (consumes only when exactly one candidate matches — several → caller picks and re-calls with `handoff_id`), `'true'`, `'false'` (pure read, including CWD lookup, through the separate read engine; contradictory with `handoff_id`; missing/old schema refuses without bootstrap). AUTO/true remain write-capable/atomic; pickup carries no read-only annotation. Snapshot reflects the state AFTER the write; the consumed row rides only in `picked_up` (full prompt). `consumed_by` is validated (non-empty, ≤ 200 chars) whenever consumption is possible (auto/true), even when nothing ends up consumed. A parallel session winning the race on a SERVER-selected target degrades to `consumed: false` (no error, snapshot is post-race); an explicit `handoff_id` still errors "not found or already consumed". |
| `handoff_work` | Sprint membership + handoff in one transaction. Session edges are ADDITIVE by default (ADR #57 — sprints are multi-session): `add_item_ids` / `remove_item_ids` pull items in / cut scope. `item_ids` is the REPLACE mode — `'__keep__'` (default, sprint untouched) / CSV (strict set); `''` or a delimiter-only string ERRORS (emptying a sprint is `clear_sprint`'s job), and replace is mutually exclusive with the additive pair. TERMINAL members (resolved / wont_fix) are the sprint's completion history: immune to replace, and removing one errors. All id lists are strict — unknown/foreign ids abort everything. `sprint` selects among multiple active sprints (id or code; id match takes precedence, an id↔code collision errors); ambiguity errors instead of guessing, and a selector without any membership change errors too. |

Structured data only: snapshots carry facts (no `recommended_action` — the
interpretation belongs to the consuming skill/assistant).

## Plans (13)

| Tool | Purpose |
|---|---|
| `create_plan` | Register a plan row programmatically (e.g. a DB-only roadmap). |
| `list_plans` | List plans in a project, with optional filters. |
| `get_plan` | Full plan dict with nested sections + checkpoints + progress rollup. |
| `set_plan_section_type` | Explicit manual classification/revocation, separate `section_id: int`, `section_type`, `project` parameters. MCP accepts integer primary keys only; project guards the live section and parent plan. Resolved manual types survive rescans; `uncertain` revokes the override. Ack `{id, ulid, section_type, classification_source, changed}`; repeated application is a no-op. Identity, anchors, heading, state and links are preserved. Deferred. |
| `resolve_uncertain_sections` | Explicit batch for separate `plan_id: int`, `project` parameters. Configured rules/command/disabled behavior; strategy consent before external invocation. Snapshot candidates, provider work outside write transaction, conditional revision/input checks; stale rows skipped. Ack `{plan_id, plan_ulid, resolved:[ids], unresolved:[{id,reason}], skipped:[{id,reason}], remaining_uncertain:n}`. Invalid target/config refuses before invocation; successful per-row progress survives a later provider failure. No source-file/anchor/checkpoint/provider-config changes. Deferred. |
| `add_plan_section` | Append a section to a plan. |
| `list_plan_sections` | List sections in a plan, optionally filtered by parent or type. |
| `complete_plan_checkpoint` | Mark a plan checkpoint as done. Real-time; never batch. |
| `reject_plan_checkpoint` | Mark a plan checkpoint as rejected (scope change, N/A). |
| `promote_section_to_item` | Convert a plan section into a standalone item, back-linked (optional `short_title`). |
| `promote_checkpoint_to_item` | Convert a plan checkpoint into a standalone item, back-linked (optional `short_title`). |
| `promote_section_to_sprint` | Materialize a `## Sprint N` plan section as a first-class sprint. |
| `ingest_plan_file` | Ingest markdown with stable anchors and first-import state. Optional enrichment follows configured rules/command/disabled; never a mandatory Claude call. A manual resolved classification remains owner-controlled until explicitly revoked. |

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

## Triage (13)

The findings ledger of review triage (#909, #930) — ONE implementation in
`dct/workflows/triage.py` shared with the CLI (`dct triage export / page /
decide / accept / close`) and the `/triage` skill. A triage
**cycle** (one review round of a project, unique live `code`) owns its
**findings**; a finding carries the rubric v2 fields (short template keys
`S` / `O` / `E` / `K` / `type` or full names `impact` / `reachability` /
`evidence` / `fix_cost` / `finding_type` — never both for one field), the class
derived by the rubric §4 rule (plus an optional override `cls` with its reason
`clsWhy`), its fix options, an agent's suggestion, the owner's decision with the
comment verbatim, an optional promoted item and `first_seen_id` — the earliest
live finding of the same project with the same normalised fingerprint (case,
whitespace and trailing punctuation ignored). Every write is one transaction:
one invalid entry of a batch writes nothing, and unknown keys are refused by
name. Record / decide / suggest need an OPEN cycle and serialise with the close
on the cycle row. All triage tools are deferred.

**Addressing a cycle** — every tool that takes one (`record_findings`,
`decide_findings`, `decide_findings_from_text`, `accept_suggestions`,
`suggest_findings`, `close_triage`, `update_triage`, `export_triage`,
`list_findings`) takes `cycle_id` (the global id) OR `cycle` (its code, unique
only within a project, so it needs `project`); both or neither is an error, and
a numeric code is never read as an id. An optional `project` on the cycle
writes is the EXPECTED project: a cycle of another project is refused with
nothing written, checked in the write's own transaction. On `export_triage` (a
read) it is checked the same way: a cycle of another project is an error. On
`promote_finding_to_item` the workflow takes no project, so the MCP adapter
checks the finding's project in a read of its own BEFORE the promotion's
transaction — a mismatch is refused before anything is written, but the check
and the promotion are not one atomic step (a finding never changes cycle and a
cycle never changes project, so both see the same owner). Never auto-detected
from CWD there — one server serves every project. Every write ack names its target: `cycle_id` / `finding_id` and
the project slug read from the cycle. This guards against a mistyped id; it is
not an authorisation boundary.

| Tool | Purpose |
|---|---|
| `open_triage` | Open a cycle (`code`, `topic`, `created_by`; optional `scope`, `base_commit`, `artifact_url`, `rubric_version` = 2, range 1..32767, `language` — the page language of the cycle, a code of dct's triage string table: empty = `[triage] language` of `dct.toml`, else `en`; fixed for the cycle's page, rebuilds and accepted-proposal prefix, an unknown code refused naming the shipped ones). A duplicate live code in the project is refused. |
| `record_findings` | Record a JSON array of findings in an open cycle, atomically — the reviewer §2 shape or an exported CARD as it is (`id` 'K-n' → seq, flags at the top level; a NEW finding, decisions never imported). `seq` (1..2147483647) defaults to the next free one. `src` / `source` is the reviewer as free text (stored as `findings.reviewer`); optional `reviewers` = a non-empty list of canonical reviewer tokens (`[a-z0-9][a-z0-9:._-]*`, lowercased, no duplicates) that `reviewer_precision` counts. Refused as a whole on an unknown key, a field under both spellings, a bad enum, a missing `title` / `fp` / `src` / S / O / E / K / `origin` / `type`, two recommended options, a duplicate option key or seq, an exhausted auto seq, a fingerprint whose normal form outgrows 500 chars. |
| `suggest_findings` | An agent's proposals (the page's `SUGGESTED`) as a JSON object keyed by `K-n`: `{dec, opt, note}`; never touches the decision. |
| `decide_findings` | The owner's decisions (the page's `CLOSED.answers`) as a JSON object keyed by `K-n`: `dec` fix / verify / backlog / reject / note / discuss, `opt` an option key of that finding, `note` verbatim. `decided_by` (the person who decided) and `decision_source` (where the owner's words are recorded, e.g. `owner paste 2026-10-13`) are required — an audit record, not authentication; an agent writes here only from the owner's own text, its proposals go to `suggest_findings`. Overwrites while the cycle is open (all three stamps refreshed, together with the writing client's provenance `decision_actor_type` / `decision_actor_client` / `decision_session_id` / `decision_actor_source`, reset as a set on every overwrite); `reject` / `note` are refused while the promoted item is open; returns the still-undecided seqs. |
| `decide_findings_from_text` | The owner's pasted page text verbatim: an optional `Triage …:` header and starting-point line (`Starting point:` / `Punkt wyjścia:`), then one `K-<n>: <label>[ · variant (<k>)][ [annotation]…][ · notes: <note>]` line per card — the words of any language of dct's triage string table (`variant` / `wariant`, `notes` / `uwagi`, labels such as `Fix now` / `Fix teraz`) or `fix` / `verify` / `backlog` / `reject` / `note` / `discuss`. Parsed before anything is written (an error names the line), then written in one transaction like `decide_findings`; only the decided cards change. A no-decision line (`— (no decision)` / `— (brak decyzji)`) with an empty note is skipped (state unchanged, listed in `skipped`); with a note it refuses the whole text. The `Punkt wyjścia:` line is stored verbatim as `decision_text_origin` of every finding the text decides (the form was seeded from proposals — not who wrote each note; a later decision without one resets it). Returns `decide_findings`' ack plus `parsed` and `skipped`. |
| `accept_suggestions` | The owner accepts the proposals as final: every finding with a suggestion gets its `dec` / `opt` and the note `<prefix> <note>` — the accepted-proposal prefix in the cycle's language (`[accepted proposal <by>]`, `[zaakceptowana propozycja <by>]`). `expected_by` (the proposer the owner saw) and `expected_digest` (`SUGGESTED.digest` of the export the PUBLISHED page was built from — never a fresh one) are required — refused when any proposal is by someone else, when the digest recomputed under the cycle lock differs (a card or proposal changed since, the same author's revision included), when none exists, or when a proposed finding is already decided unless `overwrite=true`. Findings without a proposal stay untouched (`without_suggestion`). |
| `update_triage` | Set the open cycle's page link `artifact_url` after the first publish (`"__clear__"` clears it; `""` = nothing to update, an error). |
| `close_triage` | Close the cycle with `source` (where the answers are recorded) and optional `notes`; refused while a finding is undecided (`discuss` counts as decided). Returns the `CLOSED` constant. `CLOSED.answers[K-n]` is `{dec, opt, note}` plus `origin` only when the decision came from a prefilled paste (its "Punkt wyjścia" line). |
| `export_triage` | A semantic dict of the page's constants with the template's field names: `cards` (CARDS), `closed` (CLOSED or null), `suggested` (SUGGESTED or null — with dct's own `digest`, the hash `accept_suggestions` checks) plus the cycle header. Each `suggested.answers[K-n]` also carries `by`, that card's proposer; top-level `suggested.by` is only the latest proposer. This attribution is export metadata: `suggest_findings` still accepts only `dec`, `opt`, `note` per card, with the actual batch proposer separately. Data, not a page: the canonical page build is `dct triage page`, from this same export. Texts come back as recorded, unescaped. |
| `list_findings` | A project's live findings as `{cols, rows}` (`id, cycle_code, seq, S, O, E, K, origin, type, class, decision, decision_opt, item_id, first_seen_id, source, title`); filters `cycle` / `cycle_id` (an unknown cycle is an error), `fingerprint`, `file` (substring of the area; ASCII case-insensitive, `%` / `_` literal), `cls`, `decision`, `source` (the free-text `src`), `reviewer` (a canonical token), `limit`. |
| `finding_history` | Every occurrence of a fingerprint across the project's cycles, chronologically — the recurrence / regression view. |
| `reviewer_precision` | Decided findings counted per reviewer token and decision (`reviewer, decision, n`) — a finding with two tokens counts for both, one without tokens under `(unattributed)`; optional `since` filters the cohort of findings RECORDED since the anchor (`created_at`, not the decision time). |
| `promote_finding_to_item` | Create an item from a finding decided fix / verify / backlog and back-link it, in one transaction; undecided, discuss, reject, note and an already promoted finding are refused. Priority = the rubric outcome unless given. Tags from the dictionary: `security` for a security finding, `unreproduced` only with `unconfirmed=true` on a verify finding plus `checked_conditions` and `reopen_condition` (both go into the description). |

The CLI prints the constants for a page build: `dct triage export (<cycle-code> | --cycle-id N) [--project <slug>] [--json]`
(read-only; the positional cycle is always a code, within `--project` or the
CWD's project; `--cycle-id` checks `--project` only when given). Without
`--json` it prints `const CLOSED = …; const SUGGESTED = …; const CARDS = …;`
through `page_constants_json` — `<`, `>`, `&`, U+2028 and U+2029 as `\uXXXX`
escapes, so no finding text can close the page's `<script>`; `--json` prints
the whole export with the texts as recorded.

The page itself is built by `dct triage page (<cycle-code> | --cycle-id N)
[--project <slug>] [--template <path>] --out <file> [--topic …] [--context …]
[--lede …] [--plain <file>] [--force]` (read-only on the database; no MCP
tool — a whole HTML page is not an MCP payload). It prints `<out>: <n> cards, closed=<yes|no>,
suggested=<by>, digest=<hex>` (or `suggested=none`): the digest of the very
export the page holds, the one `accept` later expects. An empty `--out` is
refused before anything is read. The template comes from `--template`, else
`[triage] template` in `dct.toml`, else the template dct ships
(`dct/assets/triage-page.html`, #991): every visible text and every word of
the decision text come from the string table `dct/assets/triage-strings.json`
in the cycle's language, injected as `const STRINGS`. An override template
written before #991 has no `STRINGS` slot and is refused with the upgrade
path (drop the override, or add the slot); its Polish placeholder names
(`{{TEMAT}}` …) stay accepted as aliases of `{{TOPIC}}` …. An existing `--out` is
replaced only with `--force` (atomically — a failed build leaves it as it was;
the replacement keeps the old regular file's rwx bits, special bits dropped,
while a new file or a replaced symlink gets the umask's default as without
`--force`),
a missing parent directory and the template itself are refused, and a
directory as `--out` writes `<dir>/<safe code>.html` (`[A-Za-z0-9._-]`).
`--plain <file>` (#988) adds the plain-language layer: the JSON file
`{by, date, cards: {"K-n": {title, problem, was, fix, opts: {<key>: text},
codex, amends}}}` is validated against the same export
(`validate_plain_layer`: only the cycle's K-n, each card's own option keys,
the known fields, string texts, no duplicate keys) before anything is
written, then copied byte for byte as `plain.json` beside the page under the
same `--force` rule (a source that already is that file is not rewritten);
one more line prints `<dir>/plain.json: plain layer, <n> cards`. The layer is
a reading aid the template may fetch; it never enters the ledger or the
digest. Every build also writes `cards.json` beside the page — the export it
was built from, byte for byte what `dct triage export --json` prints — for
consultation without the HTML, and prints `<dir>/cards.json: cards export,
<n> cards` as its second line. Every target (page, `cards.json`, `plain.json`)
is checked before the first write under the same `--force` rule, and `--out`
may not be named `cards.json` or `plain.json`: one page per directory.
The owner's answers can also be recorded from a shell: `dct triage decide …
--from-text <file|-> --decided-by <who> --source <where>`, `dct triage accept
… --expected-by <proposer> --expected-digest <hex> --decided-by <who> --source
<where> [--overwrite]`,
`dct triage close … --source <where> [--note <text>]…`.

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
| `release_changelog` | `{project, version, current_version, mode, entry_ids, released, already_released, release: record \| null}` (+ `warning`) — `release` = `{id, version, mode, entry_ids, candidate_sha, checkout_root, release_commit_sha, manifest, proof, status, created_at, updated_at, completed_at, abandoned_at, abandon_reason, release_mode, executor, consent, proposal, render, tag_proof, push_proof}` |
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
| `open_triage` | `{cycle_id, code, project, language}` |
| `record_findings` | `{cycle_id, project, recorded: [{seq, finding_id, first_seen_id}]}` |
| `decide_findings` / `suggest_findings` | `{cycle_id, project, decided, undecided: [seq…]}` / `{cycle_id, project, suggested}` |
| `decide_findings_from_text` | `{cycle_id, project, decided, undecided: [seq…], parsed, skipped: ["K-n"…]}` |
| `accept_suggestions` | `{cycle_id, project, decided, undecided: [seq…], without_suggestion: ["K-n"…], already_decided: ["K-n"…]}` |
| `update_triage` | `{cycle_id, project, code, artifact_url}` |
| `close_triage` | `{cycle_id, project, closed_at, closed}` — `closed` = the page's `CLOSED` constant |
| `promote_finding_to_item` | `{id, project, item_type, priority, status, title, short_title, tags, finding_id}` |
| `add_tag` | `{id, name, created}` (`created` is `false` when an existing tag was reused) |
| `merge_tag` | `{from, into, items_moved}` |

`assign_item_to_sprint` / `remove_item_from_sprint` and the current-sprint add/remove ops return their link rows unchanged (all ids/timestamps, no free-text to strip).

---

**Total: 76 tools.** This list is generated from `dct/mcp/server.py`; if it drifts, `dct server` prints the live set.
