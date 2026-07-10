---
name: pickup
description: |
  Start-of-session workflow — read last session's handoff, show current sprint,
  and propose the first action.
  Use when: "pickup", "gdzie byliśmy", "resume", "where were we", "start session",
  "co robimy", "kontynuuj", "podejmij od",
  "co dalej", "kontynuujmy", "wracam", "jestem z powrotem",
  "dalej", "idziemy dalej", "next up", "what's next", "back to work".
---

# /pickup — Session Resume

Open a new session by loading the previous session's handoff prompt + current
sprint state, then propose a concrete first action.

## Workflow

### Step 1: Detect project

`mcp__dct__list_projects` → match CWD (longest prefix).
If not registered, ask via `AskUserQuestion`:
  - question: "Which project are we working on?"
  - options: list of slugs + "Cancel"

### Step 2: Fetch review

Call `mcp__dct__review_sprint(project, since="last_release")`.

Returns:
- `in_sprint` — currently flagged items
- `recent_activity` — items changed since anchor
- `stale_in_progress` — forgotten in-progress items
- `unconsumed_handoffs` — unread drafts from previous sessions

### Step 3: Handle unconsumed handoffs

**If there are 0 handoffs** — skip to Step 4.

**If there is exactly 1 handoff**, present its prompt and ask via `AskUserQuestion`:
  - question: "Handoff #N [scope: X] from <created_at> by <created_by>:\n\n<prompt>\n\nPick up this handoff?"
  - options:
    - "Yes — consume and follow it"
    - "Yes — follow it but keep unconsumed (for audit)"
    - "No — start fresh, discard this handoff"
    - "Show other recent handoffs"

**If there are multiple**, ask via `AskUserQuestion`:
  - question: "N handoffs waiting. Which to pick up?"
  - options: one per handoff formatted `#N [scope] — <first 80 chars of prompt>` + "None — start fresh"

On picked handoff:
- "Yes — consume" → `mcp__dct__consume_handoff(id, consumed_by=<session_id>)`
- "keep unconsumed" → use prompt content, don't stamp consumed_at
- "start fresh" → optionally ask to `delete_handoff(id)` (soft) or just leave

### Step 4: Show sprint

Present `in_sprint` as a compact table (use format from `review_sprint.in_sprint`).
Show summary: sprint_size, sprint_completion, handoffs_pending.

If sprint is empty BUT there are open items in the project, ask via `AskUserQuestion`:
  - question: "Sprint is empty (but N items open). What next?"
  - options:
    - "Pick sprint items now — run /handoff style proposal"
    - "Browse open items — /track-list"
    - "Just start working — I'll tell you what"

If sprint is empty AND there are NO open items in the project, ask via `AskUserQuestion`:
  - question: "Project is empty — nothing to pick up. What now?"
  - options:
    - "Create the first item — run /track"
    - "Import existing tracking data — run /dct-import"
    - "Exit — not the right project"

### Step 5: Propose first action

Combine:
- Handoff prompt (if consumed) — primary intent
- First in_sprint item by priority — concrete entry point
- Any stale_in_progress — optional "unstick first" path

Present via `AskUserQuestion`:
  - question: "First action for this session?"
  - options:
    - "Open #<first-sprint-item> and start on its checkpoints"
    - "Review all sprint items first"
    - "Look at stale in_progress first (#X)"
    - "Something else — tell me what"

### Step 6: Report and open context

Based on user choice, run the matching action:
- "Open #X" → `mcp__dct__get_item(X)` + show checkpoints as next-step plan
- "Review all" → full sprint listing
- "Stale first" → `mcp__dct__get_item(<stale>)`
- "Something else" → wait for user input

Report:
```
✓ Picked up handoff #N
  Sprint: 3 items, 0/3 done
  Starting: #22 — "Optimize MCP response format"
  Plan: 4 pending checkpoints in this item
```

## Rules

- **Every decision is an `AskUserQuestion`.** No plain-text Y/N prompts.
- Don't auto-consume handoffs — user must approve (consume vs keep vs discard).
- On empty sprint + no handoffs, don't force-create a sprint — ask user what to do.
- Show resolved items from `review_sprint.recent_activity` only as context, not as
  work candidates (they're done).
- `consumed_by` should mirror `/handoff`'s `created_by` format:
  `claude-<model_id> @ <project_slug> <iso_utc>Z`.
