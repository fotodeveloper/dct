---
name: pickup
description: >-
  [dct] Start-of-session workflow — read last handoff, show current sprint,
  propose first action. Use when: "pickup", "resume", "where were we",
  "what’s next", "back to work" — in any language.
---

# /pickup — Session Resume

Open a new session by loading the previous session's handoff prompt + current
sprint state, then propose a concrete first action.

## Deferred-tool load (do this first)

`review_sprint`, `consume_handoff`, and `get_handoff` are deferred — load their
schemas before first use with ONE ToolSearch call:

```
ToolSearch("select:mcp__dct__review_sprint,mcp__dct__consume_handoff,mcp__dct__get_handoff")
```

## Workflow

### Step 1: Fetch review (CWD auto-detect)

Happy path is a SINGLE call — `review_sprint("")` auto-detects the project from
CWD (empty project string), no `list_projects` needed:

```
mcp__dct__review_sprint(project="", since="last_release")
```

Returns:
- `in_sprint` — currently flagged items
- `recent_activity` — items changed since anchor
- `stale_in_progress` — forgotten in-progress items
- `unconsumed_handoffs` — unread drafts (with prompt previews) from previous sessions

**Fallback only when project is unresolved** (CWD not a registered project →
`review_sprint` errors): call `mcp__dct__list_projects()` and ask via
`AskUserQuestion` ("Which project are we working on?" → slugs + "Cancel"), then
re-call `review_sprint(<slug>, since="last_release")`.

### Step 2: Merged decision — handoff + first action (ONE AskUserQuestion)

Handle the handoff pick-up decision AND the first-action decision in a SINGLE
`AskUserQuestion` call (two questions in one dialog). Embed the full handoff
prompt text INSIDE the question field so the user reads it before deciding —
never rely on plain text printed the same turn.

**Question 1 — handoff** (skip this question entirely if 0 unconsumed handoffs):
- If exactly 1 handoff:
  - question: "Handoff #N [scope: X] from <created_at> by <created_by>:\n\n<prompt>\n\nPick up this handoff?"
  - options:
    - "Yes — consume and follow it"
    - "Yes — follow it but keep unconsumed (for audit)"
    - "No — start fresh, discard this handoff"
- If multiple handoffs:
  - question: "N handoffs waiting. Which to pick up?"
  - options: one per handoff formatted `#N [scope] — <prompt preview>` + "None — start fresh"
  - (fetch the full text of the chosen one with `get_handoff(id)` before acting)

**Question 2 — first action** (always present):
- question: "First action for this session?"
- options:
  - "Open #<first-sprint-item> and start on its checkpoints"
  - "Review all sprint items first"
  - "Look at stale in_progress first (#X)"
  - "Something else — tell me what"

**Empty-sprint variants** — replace Question 2's options when the sprint is empty:
- Sprint empty BUT open items exist: "Pick sprint items now — run /handoff" / "Browse open items — /track-list" / "Just start working — I'll tell you what".
- Sprint empty AND no open items: "Create the first item — run /track" / "Import existing tracking data — run /dct-import" / "Exit — not the right project".

### Step 3: Act on the answers (ONE parallel message)

Batch the follow-up calls in a single parallel message:
- If the user chose to consume a handoff → `mcp__dct__consume_handoff(id, consumed_by=<session_id>)`
- If the user chose "Open #X" → `mcp__dct__get_item(X)`

("keep unconsumed" → use the prompt content but do NOT stamp consumed_at;
"start fresh" → optionally `delete_handoff(id)`, soft, or leave it.)

Then show `in_sprint` as a compact table (format from `review_sprint.in_sprint`)
plus summary: sprint_size, sprint_completion, handoffs_pending, and the opened
item's checkpoints as the next-step plan.

### Step 4: Report

```
✓ Picked up handoff #N
  Sprint: 3 items, 0/3 done
  Starting: #22 — "Optimize MCP response format"
  Plan: 4 pending checkpoints in this item
```

If the project has plans with pending checkpoints (visible in
`review_sprint`/`get_item` context), mention that **`/plan-status` shows overall
plan progress** — a good next call when planned work is in flight.

## Rules

- **Every decision is an `AskUserQuestion`.** No plain-text Y/N prompts.
- Don't auto-consume handoffs — user must approve (consume vs keep vs discard).
- On empty sprint + no handoffs, don't force-create a sprint — ask user what to do.
- Show resolved items from `review_sprint.recent_activity` only as context, not as
  work candidates (they're done).
- `consumed_by` should mirror `/handoff`'s `created_by` format:
  `claude-<model_id> @ <project_slug> <iso_utc>Z`.
