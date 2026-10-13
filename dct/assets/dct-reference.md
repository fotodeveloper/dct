# dct reference — read on demand

The full guide to when an assistant reaches for dct skills and MCP tools.
`dct init` installs it as `~/.claude/dct-reference.md` (a symlink into the
installed package, so upgrades refresh it). The short dct block that
`dct init` attaches to `~/.claude/CLAUDE.md` points here; the assistant reads
this file only when it needs more than the skills' own triggers.

Do not paste this file into `CLAUDE.md`: a rules file is loaded in every
session, while this reference is read on demand.

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
- `/release` — release a version: the typical case is one screen; the local `dct release` workflow stamps, writes the changelog and version files, commits, tags and pushes, each verified
- `/pickup` — start-of-session: pick up the handoff, show sprint, propose first action
- `/handoff` — end-of-session: one draft (prompt, sprint, scope) saved on one answer
- `/plan-status` — cross-plan progress dashboard
- `/dct-import` — migrate markdown-based tracking into dct (user-invoked only)
- `/dct-init` — bootstrap dct + register current project (user-invoked only)
- `/plan-resolve-uncertain` — batch classify uncertain plan sections (user-invoked only)

### Item type triggers (`/track` and `create_item`)

Natural-language phrases map to `item_type`. Use the type the user's language
implies; no type cue in the phrase → `todo`, without a question. Ask about the
type only when the phrase is genuinely ambiguous — it carries cues for two
different types — and the choice changes the scope of the work.

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

**Session start** — user says "continue" / "pickup" / "resume" / "where were we"
→ call `/pickup`. An instruction to continue ("continue", "pickup", "resume")
consumes the single matching handoff without a question and names it in the
report; a status question ("where were we") only reads. Several matching
handoffs → it asks which, shows the chosen one in full and consumes it only
when the user says to continue it. A handoff's next step that is still
current is started at once ("Starting with …"); an old handoff is never a
consent for commits, pushes or other risky steps.

**Tracking writes** — on an unambiguous instruction ("track this bug", "clog
it", "#42 to P1", "close #42") `/track`, `/clog`, `/track-update` and
`/track-resolve` write at once and acknowledge in one line, with how to
correct it. They ask only what they cannot tell: the project, which item, or
a type when the phrase cues two different types that change the scope (no
type cue → `todo`). Closing an item ("close #42", "#42 resolved") goes through
`/track-resolve`: a pending gate checkpoint stops it in plain words, and no
gate is rejected without the user's own "skip it". A bug or feature merely
mentioned during work (no "track this") is not an instruction → suggest
`/track`, don't create it.

**Work on an item starts** — "start working on #N" (`/track-update`, status →
`in_progress`) → when the work is a non-trivial feature or improvement, or the
user asks to break the work down, propose a plan of at least 3 checkpoints via
`add_checkpoint` (3 is a minimum, never a cap — as many as the work needs); an
item that already has checkpoints keeps its plan. A small bug or a one-line
fix starts with no checkpoints; a complex issue or todo may get some, with no
fixed minimum taken from its type. Creating an item adds none.

**Each concrete step completes**
→ `complete_checkpoint(id)` in real-time — do NOT batch completions at end.

**Scope changes, step becomes irrelevant**
→ `reject_checkpoint(id)` (ledger-preserving); never delete.

**Committing** — "commit" / "save this" / "commit this"
→ `/commit` (parallel pre-flight probe, per-branch changelog guard, HEREDOC message with `(vX.Y.Z)`;
ONE screen — files, message, entries — answered Commit / Change / Stop).
Trailers follow the project's stored preferences (`list_projects`:
`commit_co_authored_by`, `commit_session_link`), applied without a question;
a preference not answered yet is asked once and stored with
`dct project trailers`; the project's own rules forbidding a trailer win
(dct's own repository never takes a session link). A path that looks like a
secret (`dct clog candidates --json`: `secret_like`) is never staged; a staged
one stops the commit with the command that unstages it.
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
→ `/release`. The skill drives the local `dct release` workflow, which
computes the whole release, shows it, and stamps, writes, commits, tags and
pushes only on the owner's answer, each effect verified. The typical case is
one screen; an unfinished release, a changelog file to settle or a change of
plan can add a step. Without the skill, follow the `/release` fallback in
dct's `docs/assistant-fallbacks.md` — the authoritative step-by-step. The
command sequence and its flags live only in those two; never release by
hand from memory.

**Session end** — "handoff" / "end session" / "wrap up" / "next session"
→ `/handoff` (one printed draft — the prompt, `Sprint: unchanged`, `Scope: none` —
and one question: Save / Change / Cancel).

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
repo has the authoritative step-by-step prose for `/pickup`, `/handoff`,
`/release`, `/commit`, `/track`, `/clog`, `/track-resolve` — and the shared
`Changelog:` trailer / release-range rules.

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
