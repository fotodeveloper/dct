---
name: track-update
description: >-
  [dct] Update a tracked item — status, priority, fields, sprint membership.
  Use when: "update item #N", "change priority", "start working on #N", "wip",
  "add/remove from sprint" — in any language.
---

# /track-update — Update Tracked Item

## Step 1: Identify item(s)

**ID normalization** — accept all of these forms from user:
- `#42`, `42`, `ITEM-42`, `item-42`, `0042`

Parse by: strip leading `#`, strip `ITEM-`/`item-`/`ITEM_`/`item_` prefix
(case-insensitive), strip leading zeros, cast to integer.
Regex: `^(#|ITEM-|item-|ITEM_|item_)?0*(\d+)$` — capture group 2 is the numeric id.

**Batch form** — user may give multiple ids for the SAME change, e.g.
"set #42 #45 to P1" or "w toku: 7, 12, 19". Parse each id separately
(whitespace or comma separated) and apply the same update to all. Ask
confirmation ONCE for the batch, not per-item.

Invalid inputs (non-digit body, empty, negative) — reject with clear message.

For each id, call `mcp__dct__get_item(item_id)` to fetch current state.

## Step 2: Determine changes

From user request, identify which fields to change:
- `title`, `short_title`, `status`, `priority`, `component`, `description`, `resolution`

`short_title` is the item's label: a 2-4 word English noun phrase (≤ 50 chars,
no item number, no project name — longer is refused). It is OPTIONAL on
update: omit it (or pass `""`) to keep the stored label — pass it only when
the user asks to change the label, never re-send or invent one on a status /
priority / field update. Pass `'__clear__'` to clear it (same convention as
`component` / `description` / `resolution`); a whitespace-only value is not
"omitted" — it strips to empty and clears the label too.

`update_item` does NOT change sprint membership — `in_current_sprint` is a
derived, read-only field. Sprint changes go through dedicated tools
(see "Sprint membership" below).

Status transitions:
- "start" / "zacznij" / "rozpocznij" / "w toku" / "wip" / "in progress" → `status="in_progress"`
- "pause" / "wstrzymaj" / "block" / "zablokuj" → keep status, add note explaining pause
- "open" / "reopen" / "wznów" → `status="open"` (also clears `resolved_at` automatically — see CLAUDE.md terminal invariant)

Sprint membership (NOT via update_item):
- "add to sprint" / "flag for sprint" / "na sprint" / "dodaj do sprintu" →
  `mcp__dct__add_item_to_current_sprint(item_id, project)` — auto-creates the
  current sprint; additive, keeps existing members.
- "remove from sprint" / "out of sprint" / "zdejmij ze sprintu" →
  `mcp__dct__remove_item_from_current_sprint(item_id, project)`.
- To replace the WHOLE current sprint at once: `mcp__dct__set_sprint(project, item_ids)`.

## Step 3: Confirm

Show before/after (batch shows list):
```
#42 — Parser crashes on empty files
  Priority: P2 → P1
  Status:   open → in_progress
  + Note:   "Reproducible on 0-byte input, blocking v0.3"
```

Wait for confirmation.

## Step 4: Execute

### When add_note is MANDATORY

Call `mcp__dct__add_note(item_id, note)` BEFORE the `update_item` call when the
transition is one of:
- **status** change (especially to `in_progress` or any terminal) — note the reason / link to commit
- **priority** change — note what prompted the bump or demotion
- **resolution** set — note the commit hash or decision rationale

This is the audit trail — future /pickup will read these notes via `get_item`.

### When add_note is OPTIONAL

Title, short_title, component, description, tags edits — notes welcome but not required.

### Editing tags

`update_item(item_id, tags=[...])` is a **FULL REPLACE** of the item's tag set,
not an append: omit `tags` to leave it untouched, pass `[]` to clear it. To add
one tag, read the current set with `get_item(id)` first and send the union.

Tags are dictionary-backed and global: names are normalized (trimmed,
lowercased) and unknown ones auto-register, but a name too SIMILAR to an
existing tag is refused with a suggestion in the error (e.g. `test` vs `tests`).
Reuse the suggested tag, or — when the near-duplicate is genuinely wanted — call
`mcp__dct__add_tag(name=..., force=true)` first and retry the update.

Then call `mcp__dct__update_item(item_id, ...)` with changed fields.
For batch updates, loop per-id with the same kwargs.

For sprint membership changes, call `add_item_to_current_sprint` /
`remove_item_from_current_sprint` (or `set_sprint` for a full replace) — NOT
`update_item`.

## Step 5: Report

Single:
```
✓ #42 updated — priority P1, status in_progress
```

Batch:
```
✓ 3 items updated — priority P1, status in_progress
  #7, #12, #19
```

## Rules

- **Every status/priority change gets a note** — no "I'll explain later" allowed.
- **Batch confirmations are one-shot** — do NOT ask per-item if user clearly grouped them.
- **ID normalization is uniform** — use the same regex as /track-resolve and /track-list.
- **Terminal transitions clear the DERIVED flag, not the membership** — a
  resolved / wont_fix item drops out of `in_current_sprint`, but its
  `item_sprints` link STAYS as the sprint's completion history (it is what
  `sprint_completion` counts). Sprint membership is never set via
  `update_item` — `in_current_sprint` is derived and read-only; use
  `assign_item_to_sprint` / `remove_item_from_sprint` for real membership.
