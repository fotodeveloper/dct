# dct — CLAUDE.md snippet (drop into `~/.claude/CLAUDE.md`)

After installing dct and running `dct init`, paste the section below into your
personal `~/.claude/CLAUDE.md` so Claude knows when to reach for dct skills
and MCP tools across any project — including ones you haven't registered yet.

Everything described here is already installed by `dct init`. This snippet
only teaches the assistant **when to invoke what**.

---

## Cross-project tracking (dct)

`dct` is a cross-project work tracker — items, checkpoints, changelog,
sprint flags, and session handoffs — all in one user-scoped database.
Registered at `~/.claude.json` (MCP), skills at `~/.claude/skills/`,
hooks at `~/.claude/hooks/`.

### What's available

**Skills** (invoke via `/name`) — all model-facing descriptions carry a `[dct]`
prefix and trigger in any language:
- `/track` — create item (issue / todo / feature / idea / improvement)
- `/track-list` — list/search, priority grouped, hotspot detection
- `/track-update` — edit fields, status, priority, sprint membership
- `/track-resolve` — close (resolved / wont_fix)
- `/clog` — add changelog entry
- `/commit` — conventional commit with `(vX.Y.Z)` suffix; `Changelog:` trailer from `dct clog candidates --json`
- `/release` — one workflow over `dct release plan / attest / prepare / apply / resume` (the workflow stamps, writes, commits, tags and pushes, verified)
- `/pickup` — start-of-session: read handoff, show sprint, propose first action
- `/handoff` — end-of-session: flag sprint + write prompt for next session
- `/plan-status` — cross-plan progress dashboard
- `/dct-import` — migrate markdown-based tracking into dct (user-invoked only)
- `/dct-init` — bootstrap dct + register current project (user-invoked only)
- `/plan-resolve-uncertain` — batch classify uncertain plan sections (user-invoked only)

### Item type triggers (`/track` and `create_item`)

Natural-language phrases map to `item_type`. Use the type the user's language
implies; do not ask unless the phrase is genuinely ambiguous.

| User says | item_type |
|---|---|
| "problem", "bug", "issue", "broken", "crash", "error", "doesn't work", "regression" | `issue` |
| "todo", "task", "need to", "action item", "must do" | `todo` |
| "feature", "add X", "new capability", "let's build", "we should have" | `feature` |
| "idea", "maybe", "what if", "consider", "brainstorm", "explore" | `idea` |
| "improvement", "refactor", "polish", "cleanup", "tidy up", "make X nicer", "simplify" | `improvement` |

Rules of thumb:
- `issue` = something observably wrong in code/system that should be fixed
- `todo` = concrete work item that's neither a bug nor a new capability
- `feature` = new user-visible capability that doesn't exist yet
- `idea` = exploratory, not yet decided to build
- `improvement` = existing thing works but could be better (refactor, UX polish, perf)

Every new item gets a `short_title` label: a 2-4 word English noun phrase
naming what it is about (≤ 50 chars, no item number, no project name — e.g.
"Windows hook paths"). The `create_item` ack echoes the stored `short_title`
(null when none); a missing key means an outdated MCP server contract — report
it and restart the server, but never retry the create (the item exists). On
`update_item` the label is optional: omit it to keep the stored one.

**MCP tools** — 60 total; most important:
- `pickup_work`, `handoff_work`, `get_context_snapshot` — composed session-edge ops; prefer them over chaining the granular tools below
- `list_projects`, `list_items`, `get_item`, `create_item`, `update_item`, `resolve_item`, `add_note`
- `add_checkpoint`, `list_checkpoints`, `complete_checkpoint`, `reject_checkpoint`, `reopen_checkpoint`
- `add_changelog`, `list_changelog`, `count_unreleased_changelog`, `release_changelog`, `update_changelog`, `delete_changelog`, `export_changelog`
- `get_release`, `advance_release`, `abandon_release` — durable release record, the ledger path of a project's own release tool: read an unfinished release, record verified progress, abandon one explicitly (a release dct executes is continued and abandoned only by `dct release resume` / `dct release abandon` — these tools refuse it; deferred; load via ToolSearch)
- `set_sprint`, `clear_sprint`, `review_sprint`, `list_sprints`, `update_sprint`
- `create_handoff`, `list_handoffs`, `get_handoff`, `consume_handoff`, `delete_handoff`
- `add_tag`, `list_tags`, `merge_tag` — global tag dictionary admin (deferred; load via ToolSearch)

### When to auto-invoke

**Session start** — user says "where were we" / "pickup" / "resume" / "continue from last time"
→ call `/pickup` (reads last handoff, shows current sprint, proposes first action).

**During work, bug or feature surfaces** — user describes it without "track this"
→ suggest `/track` (don't auto-create without confirmation).

**Non-trivial feature or improvement accepted**
→ IMMEDIATELY after `create_item`, add 3-7 checkpoints via `add_checkpoint`.
Sketch the work breakdown BEFORE starting code.

**Each concrete step completes**
→ `complete_checkpoint(id)` in real-time — do NOT batch completions at end.

**Scope changes, step becomes irrelevant**
→ `reject_checkpoint(id)` (ledger-preserving); never delete.

**Committing** — "commit" / "save this" / "commit this"
→ `/commit` (parallel pre-flight probe, per-branch changelog guard, HEREDOC message with `(vX.Y.Z)`).
Order: stage the chosen paths first (a deliberate partial staging stays as it
is), THEN pick the trailer from the complete `git diff --cached`, THEN
`git commit` without another `git add`; any later index change means picking
again. The message ends with a `Changelog: <id>, <id>` trailer (ids ascending,
before `Co-Authored-By:`) naming the `dct clog candidates --json` entries
whose text matches the staged diff — `uncertain` ones (server-detected, no
provenance, detached HEAD) only after the owner confirms them and
`update_changelog` sets the real branch/worktree; `/release` stamps only
entries proven that way. Reverts keep git's standard `Revert "…"` message.
`/clog` records `branch` / `worktree` from the agent's own working directory.
If pre-commit hook rejects: fix underlying issue, re-stage, **NEW commit** (never `--amend`, never `--no-verify`).

**Releasing** — "release" / "bump version" / "ship it"
→ `/release` — ONE workflow over the local CLI: `dct release plan --json` (range, proof per entry, unproven entries, collective attestation proposals — and who releases, routed from that ONE result: an unfinished release by its record, `executor.record` — dct's own continues with `dct release resume` on the stored consent, without asking again; an `own` record stays with its own tool; a record without consent → `dct release takeover --version V …` (a new consent the owner approves) or the ledger path — and only a NEW release by the project's setting, `executor.project`: `own` → its own tool releases) → work in progress committed inside the workflow on the owner's consent (the `/commit` procedure, then re-plan) → the owner decides attest / correct / withdraw / keep and the version → `dct release attest` / `dct release plan --version V --entries … --json` (the snapshot the owner approves) → `dct release prepare --version V --entries … --plan-digest <snapshot_digest>` (the version sync runs isolated; the owner approves the file list and the push target) → `dct release apply --version V --entries … --plan-digest <snapshot_digest> --proposal-digest <proposal_sha256> --push-remote <remote> --push-branch <branch> --push-url <url> --confirm` (the three push values of the target the owner approved at prepare — apply never selects one itself — or `--no-push` for an explicit local release: stamp, changelog section, version files, `release: vX.Y.Z` commit through the hooks, tag, push — each verified and recorded by the workflow; an interrupted release → `dct release resume`, which executes the stored consent with no new approval; `dct release abandon --version V --reason "…" --confirm` only on the owner's explicit decision).

**Session end** — "handoff" / "end session" / "wrap up" / "next session"
→ `/handoff` (flag sprint items, optionally write handoff prompt).

### ID memorization pattern

MCP tools that create or fetch records return an integer `id`.
**Always remember it** after `create_item`, `add_checkpoint`, `create_handoff` —
you need it for subsequent `update_item` / `complete_checkpoint` / `consume_handoff`.

Pattern:
```
item = create_item(...)            # item["id"] = 42 — KEEP
cp   = add_checkpoint(42, "X")     # cp["id"] = 7 — KEEP
# ... work ...
complete_checkpoint(7)
```

### Response format cheatsheet

**Write ops return a minimal ack**, not the full record — the created/updated `id`
plus a few post-state scalars (e.g. `create_item` → `{id, project, item_type,
priority, status, title, short_title}`; checkpoint ops → `{id, item_id, status,
progress}`; `add_changelog` → `{id, project, category, component, branch,
branch_source}` + `warning` when the server detected the branch).
**Remember the `id`** for follow-up calls. Need full context? Fetch it deliberately:
`get_item(id)` (fields, notes, checkpoints, progress) or `get_handoff(id)`
(untruncated prompt + context_summary) — both return a full semantic dict.

`list_*` tools return compact `{cols, rows}`:
- `cols` is the full-name column header array (once per response)
- `rows` is an array of value arrays in the same order
- short/fixed-width fields come first, long free text (title, description, entry, prompt) last
- extract a field by `cols.indexOf("title")` → index into each row
- `list_items` defaults to `view="brief"` (…, `short_title`, `title` — no
  `description`); pass `view="full"` or call `get_item(id)` only when the
  descriptions are needed
- `list_handoffs` truncates `prompt` to a 200-char preview → `get_handoff(id)` for full text

Opt-in formats on list-views:
- `format="markdown"` → human-readable table (lossy — no description)
- `format="dict"` → list of dicts (for `list_items` the keys follow `view`:
  brief columns by default, full item rows with `view="full"`)

### ID normalization

Skills accept `#42`, `42`, `ITEM-42`, `0042` — all resolve to `item_id=42`
(regex `^(#|ITEM-|item-)?0*(\d+)$`). `/track-resolve` accepts comma- or
whitespace-separated batch ids.

### Fallback workflows

If a skill name renames or disappears in a future CC version, Claude can
still drive dct via raw MCP calls. `docs/assistant-fallbacks.md` in dct's own
repo has the authoritative step-by-step prose for `/release`, `/commit`,
`/track`, `/clog`, `/track-resolve` — and the shared `Changelog:` trailer /
release-range rules.

### Auto-detect project from CWD

Most MCP tools accept empty `project=""` — they auto-detect from the current
working directory (longest path prefix match against registered projects).
You usually don't need to specify project explicitly inside a project tree.

### Sprint + handoff in practice

- Set sprint: `set_sprint(project, item_ids="22,21,6")` — atomic replace of the
  single active sprint; with several active it ERRORS (as do `clear_sprint` and
  the current-sprint add/remove helpers) — target one explicitly via
  `assign_item_to_sprint` / `remove_item_from_sprint`
- Session edge: `handoff_work(..., add_item_ids=..., remove_item_ids=...)` —
  sprints span sessions, so a handoff ADDS/CUTS by default; `item_ids` (replace)
  is conscious replanning and an empty replace is refused
- Review: `review_sprint(project, since="last_release")` → in_sprint,
  recent_activity, stale_in_progress, unconsumed_handoffs, summary
- Resolving an item clears the DERIVED `in_current_sprint` flag (sprint = active
  work invariant), but its sprint membership is RETAINED as completion history
  and counted by `sprint_completion`
- Park an unfinished sprint: `update_sprint(sprint_id, status="deferred")` —
  keeps `started_at`; reactivate later with `status="active"`. Parallel active
  sprints are allowed; the "current" one is the most recently started (reads
  only — writes refuse to guess). Never mark an unfinished sprint
  completed/abandoned — defer it.
- Multiple parallel handoffs supported (use `scope` tag to disambiguate)
- `consume_handoff(id)` preserves the row — ledger, nothing hard-deleted

### Tags

- Tags are a GLOBAL, dictionary-backed vocabulary (cross-project); items link to it.
- Filter: `list_items(tag="perf")` — composes with `project` scoping; unknown tag
  → error naming the known tags (closed dictionary).
- Edit: `update_item(id, tags=[...])` is a FULL REPLACE (`[]` clears, omit = unchanged);
  to append, `get_item` first and pass the union.
- New names auto-register after normalization (lowercase/trim). A name SIMILAR to an
  existing tag (test vs tests) is refused with a suggestion — reuse the suggested tag,
  or consciously `add_tag(name, force=true)` and retry.
- Consolidate duplicates with `merge_tag(from_name, into_name)` (links move, source
  tag soft-deleted, history preserved).

### What NOT to do

- Never `git add -A` or `git add .` in `/commit` — always explicit paths
- Never `--amend` on hook-rejected commits — the commit didn't happen
- Never `--no-verify` or `--no-gpg-sign` — hooks exist for reasons
- Never batch `complete_checkpoint` at end of task — real-time updates matter
- Never hard-delete; use `reject_checkpoint` or `delete_handoff` (soft)
