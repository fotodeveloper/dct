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

**Skills** (invoke via `/name`):
- `/track` — create item (issue / todo / feature / idea / improvement)
- `/track-list` — list/search, priority grouped, hotspot detection
- `/track-update` — edit fields
- `/track-resolve` — close (resolved / wont_fix)
- `/clog` — add changelog entry
- `/commit` — conventional commit with `(vX.Y.Z)` suffix, changelog-aware
- `/release` — stamp version, export Keep-a-Changelog MD, release commit
- `/dct-import` — migrate markdown-based tracking into dct
- `/handoff` — end-of-session: flag sprint + write prompt for next session
- `/pickup` — start-of-session: read handoff, show sprint, propose first action

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

**MCP tools** — 47 total; most important:
- `list_projects`, `list_items`, `get_item`, `create_item`, `update_item`, `resolve_item`, `add_note`
- `add_checkpoint`, `list_checkpoints`, `complete_checkpoint`, `reject_checkpoint`, `reopen_checkpoint`
- `add_changelog`, `list_changelog`, `count_unreleased_changelog`, `release_changelog`, `export_changelog`
- `set_sprint`, `clear_sprint`, `review_sprint`, `list_sprints`, `update_sprint`
- `create_handoff`, `list_handoffs`, `consume_handoff`, `delete_handoff`

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
→ `/commit` (parallel pre-flight probe, changelog guard, HEREDOC message with `(vX.Y.Z)`).
If pre-commit hook rejects: fix underlying issue, re-stage, **NEW commit** (never `--amend`, never `--no-verify`).

**Releasing** — "release" / "bump version" / "ship it"
→ `/release` (dirty-tree hard stop → stamp DB → export MD → commit `release: vX.Y.Z`).

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

`list_*` tools return compact `{cols, rows}`:
- `cols` is the full-name column header array (once per response)
- `rows` is an array of value arrays in the same order
- short/fixed-width fields come first, long free text (title, description, entry, prompt) last
- extract a field by `cols.indexOf("title")` → index into each row

`get_item`, `get_handoff` return a full semantic dict (single record, full keys).

Opt-in formats on list-views:
- `format="markdown"` → human-readable table (lossy — no description)
- `format="dict"` → list of full-key dicts (legacy shape)

### ID normalization

Skills accept `#42`, `42`, `ITEM-42`, `0042` — all resolve to `item_id=42`
(regex `^(#|ITEM-|item-)?0*(\d+)$`). `/track-resolve` accepts comma- or
whitespace-separated batch ids.

### Fallback workflows

If a skill name renames or disappears in a future CC version, Claude can
still drive dct via raw MCP calls. The `Skill fallback workflows` section
in dct's own repo `CLAUDE.md` has step-by-step prose descriptions for
`/release`, `/commit`, `/track`, `/clog`, `/track-resolve`.

### Auto-detect project from CWD

Most MCP tools accept empty `project=""` — they auto-detect from the current
working directory (longest path prefix match against registered projects).
You usually don't need to specify project explicitly inside a project tree.

### Sprint + handoff in practice

- Set sprint: `set_sprint(project, item_ids="22,21,6")` — atomic replace
- Review: `review_sprint(project, since="last_release")` → in_sprint,
  recent_activity, stale_in_progress, unconsumed_handoffs, summary
- Resolve auto-unflags sprint (sprint = active work invariant)
- Park an unfinished sprint: `update_sprint(sprint_id, status="deferred")` —
  keeps `started_at`, frees the one-active-per-project slot; reactivate later
  with `status="active"` (≤1 active cap enforced). Never mark an unfinished
  sprint completed/abandoned — defer it.
- Multiple parallel handoffs supported (use `scope` tag to disambiguate)
- `consume_handoff(id)` preserves the row — ledger, nothing hard-deleted

### What NOT to do

- Never `git add -A` or `git add .` in `/commit` — always explicit paths
- Never `--amend` on hook-rejected commits — the commit didn't happen
- Never `--no-verify` or `--no-gpg-sign` — hooks exist for reasons
- Never batch `complete_checkpoint` at end of task — real-time updates matter
- Never hard-delete; use `reject_checkpoint` or `delete_handoff` (soft)
