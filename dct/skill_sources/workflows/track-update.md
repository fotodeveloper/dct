# {{host.skill-prefix}}track-update — Update Tracked Item

## Step 1: Identify item(s)

**ID normalization** — accept all of these forms from user:
- `#42`, `42`, `ITEM-42`, `item-42`, `0042`

Parse by: strip leading `#`, strip `ITEM-`/`item-`/`ITEM_`/`item_` prefix
(case-insensitive), strip leading zeros, cast to integer.
Regex: `^(#|ITEM-|item-|ITEM_|item_)?0*(\d+)$` — capture group 2 is the numeric id.

**Batch form** — user may give multiple ids for the SAME change, e.g.
"set #42 #45 to P1" or "w toku: 7, 12, 19". Parse each id separately
(whitespace or comma separated) and apply the same update to all.

Ask which item only when it is ambiguous (no id given and several items fit
the description). Invalid inputs (non-digit body, empty, negative) — reject
with clear message.

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
- "open" / "reopen" / "wznów" → `status="open"` (reopening a resolved / wont_fix item clears `resolved_at` automatically — no separate call)
- "resolved" / "done" / "close" / "zamknij" / "wont fix" → closing is a
  terminal transition: follow the {{host.skill-prefix}}track-resolve procedure (its pending-gate
  check, then `mcp__dct__resolve_item(item_id, status, resolution)`), never
  `update_item` with a terminal status. Apply the full pending-gate procedure below before resolve:

{{shared.closing-gates}}

Sprint membership (NOT via update_item):
- "add to sprint" / "flag for sprint" / "na sprint" / "dodaj do sprintu" →
  `mcp__dct__add_item_to_current_sprint(item_id, project)` — auto-creates the
  current sprint; additive, keeps existing members.
- "remove from sprint" / "out of sprint" / "zdejmij ze sprintu" →
  `mcp__dct__remove_item_from_current_sprint(item_id, project)`.
- To replace the WHOLE current sprint at once: `mcp__dct__set_sprint(project, item_ids)`.

## Step 3: Execute — no confirmation question

An unambiguous instruction ("#42 to P1", "start working on #42", "w toku: 7,
12, 19") is applied without a confirmation question: call
`mcp__dct__update_item(item_id, ..., reason="…")` at once (a batch: once per id
with the same fields). Ask only when the change itself is unclear (which
field, which value). The one-line acknowledgement shows old → new, so a wrong
change is visible and reversible at once.

### The reason travels with the update

When the transition is one of these, pass its reason IN the update call —
`mcp__dct__update_item(item_id, status=…, priority=…, reason="…")`:
- **status** change (to `in_progress`, or back to `open`) — the reason / link
  to commit; a close carries its reason as the `resolution` of `resolve_item`
- **priority** change — what prompted the bump or demotion
- **resolution** set — the commit hash or decision rationale

`reason` becomes an item note written in the SAME transaction as the
update: one operation, and a refused update (a refused tag, an invalid
status) leaves no orphan note behind. This is the audit trail — future
{{host.skill-prefix}}pickup reads these notes via `get_item`.

Title, short_title, component, description, tags edits — a reason is
welcome but not required. A note WITHOUT a field change (e.g. "pause" that
keeps the status) stays `mcp__dct__add_note(item_id, note)`.

### Editing tags

`update_item(item_id, tags=[...])` is a **FULL REPLACE** of the item's tag set,
not an append: omit `tags` to leave it untouched, pass `[]` to clear it. To add
one tag, read the current set with `get_item(id)` first and send the union.

Tags are dictionary-backed and global: names are normalized (trimmed,
lowercased) and unknown ones auto-register, but a name too SIMILAR to an
existing tag is refused with a suggestion in the error (e.g. `test` vs `tests`).
Reuse the suggested tag, or — when the near-duplicate is genuinely wanted — call
`mcp__dct__add_tag(name=..., force=true)` first and retry the update.

For sprint membership changes, call `add_item_to_current_sprint` /
`remove_item_from_current_sprint` (or `set_sprint` for a full replace) — NOT
`update_item`.

## Step 4: Starting work — propose the plan when the work needs one

{{shared.checkpoint-plan}}

## Step 5: Acknowledge in one line

```
✓ #42 priority P2 → P1 · status open → in_progress · to undo: {{host.skill-prefix}}track-update #42 priority P2, open
```

One line per update: what changed, old → new — and it ENDS with how to undo
it, in words the user can type back; never left out, the write had no
question. To correct a wrong change, run `{{host.skill-prefix}}track-update` again with the
previous value (`update_item`). A batch is one line too:

```
✓ #7, #12, #19 status open → in_progress · to undo: {{host.skill-prefix}}track-update #7 #12 #19 open
```

## Rules

{{shared.language}}
- **Every status/priority change carries a reason** (`update_item(…, reason="…")`) — no "I'll explain later" allowed.
- **No confirmation question on an unambiguous instruction** — update, then
  acknowledge in one line (old → new). A batch is one update, not one question per item.
- **ID normalization is uniform** — use the same regex as {{host.skill-prefix}}track-resolve and {{host.skill-prefix}}track-list.
- **Terminal transitions clear the DERIVED flag, not the membership** — a
  resolved / wont_fix item drops out of `in_current_sprint`, but its
  `item_sprints` link STAYS as the sprint's completion history (it is what
  `sprint_completion` counts). Sprint membership is never set via
  `update_item` — `in_current_sprint` is derived and read-only; use
  `assign_item_to_sprint` / `remove_item_from_sprint` for real membership.


{{shared.consent}}


{{shared.discovery}}
