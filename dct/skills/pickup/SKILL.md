---
name: pickup
description: >-
  [dct] Start-of-session workflow — read last handoff, show current sprint,
  propose first action. Use when: "pickup", "resume", "where were we",
  "what’s next", "back to work" — in any language.
---

# /pickup — Session Resume

Open a new session by loading the previous session's handoff prompt + sprint
state in ONE composed READ, then let the user decide what to do with the
handoff before anything is written.

No ToolSearch preload needed: `pickup_work` (and `get_item`) are alwaysLoad
residents.

The flow is TWO-PHASE by design: **read first, consume only after the user
says so.** Consuming a handoff is a write on a ledger the user may want to
keep for audit — /pickup never makes that call on its own.

## Workflow

### Step 1: One composed read (CWD auto-detect)

```
mcp__dct__pickup_work(project="", consume="false")
```

`consume="false"` makes this a PURE READ — nothing is written, so no
`consumed_by` identity is needed (and passing `handoff_id` here is an error).
One call returns everything Step 2 needs — never follow up with
`review_sprint` / `list_handoffs` / `get_handoff`:

- **`handoff.latest`** — the newest unconsumed handoff with its FULL prompt
  (this is the text you show the user); `handoff.others` carries 200-char
  previews of the rest; `handoff.pending_count` says how many are waiting.
- **`picked_up: null` + `consumed: false`** — always, in this phase.
- **`pending_count: 0`** — no handoff; go straight to the first-action question.
- **`sprint`** — ALL active sprints (`active_sprints`, `primary_sprint_id`),
  member items with `cp`, their `checkpoints` (the next pending checkpoint is
  readable directly — no `get_item` needed for the plan preview),
  `stale_in_progress`.
- **`gates`** — pending gate checkpoints of in-sprint items; **`decisions`** —
  live ADRs/open questions with previews; **`summary`** — sprint_size,
  sprint_completion, handoffs_pending, gates_pending.

**Fallback only when project is unresolved** (CWD not registered →
`pickup_work` errors): call `mcp__dct__list_projects()` and ask via
`AskUserQuestion` ("Which project are we working on?" → slugs + "Cancel"),
then re-call `pickup_work(<slug>, consume="false")`.

### Step 2: Merged decision — handoff + first action (ONE AskUserQuestion)

Handle the handoff decision AND the first-action decision in a SINGLE
`AskUserQuestion` call. Embed the full handoff prompt text INSIDE the question
field so the user reads it before deciding — never rely on plain text printed
the same turn.

**Question 1 — handoff** (branch on Step 1's `pending_count`):
- If exactly ONE handoff (`pending_count == 1`) — the user decides whether it
  is followed AND whether it gets consumed:
  - question: "Handoff #N [scope: X] from <created_at> by <created_by>:\n\n
    <handoff.latest.prompt>\n\nPick it up?"
  - options:
    - "Consume and follow" (recommended) — mark it consumed, work its plan
    - "Follow but keep unconsumed (audit)" — work its plan, leave the row
      pending so another session/reviewer still sees it
    - "Start fresh" — ignore its plan; nothing is consumed
- If several handoffs (`pending_count >= 2`):
  - question: "N handoffs waiting. Which to pick up?\n\nNewest #M [scope]:\n<handoff.latest.prompt>"
  - options: one per handoff — `#N [scope] — <preview from handoff.others>` —
    plus "None — start fresh"
- If `pending_count: 0` — skip Question 1 entirely.

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

- User chose "Consume and follow" (single handoff) or picked #N among several →
  ```
  mcp__dct__pickup_work(project="", consume="true", handoff_id=N,
                        include="none",
                        consumed_by="claude-<model_id> @ <project_slug> <iso_utc>Z")
  ```
  `include="none"` makes the consume call cheap (no snapshot re-read) —
  Step 1 already delivered the state.
- User chose "Open #X" → `mcp__dct__get_item(X)` — BATCH it in the SAME
  parallel message as the consume call above.
- "Follow but keep unconsumed (audit)" → write nothing; work the plan you
  already read in Step 1.
- "Start fresh" / "None — start fresh" → consume nothing; optionally
  `delete_handoff(id)` (deferred tool — load only if the user explicitly
  wants a discard).

Then show the sprint items as a compact table (from `sprint.items`) plus
summary: sprint_size, sprint_completion, handoffs_pending, and the opened
item's pending checkpoints (from `sprint.checkpoints`) as the next-step plan.

### Step 4: Report

```
✓ Picked up handoff #N (consumed | left unconsumed for audit)
  Sprint: 3 items, 1/3 done
  Starting: #22 — "Optimize MCP response format"
  Plan: 4 pending checkpoints in this item
```

If the project has plans with pending checkpoints, mention that
**`/plan-status` shows overall plan progress** — a good next call when planned
work is in flight.

## Rules

- **Every decision is an `AskUserQuestion`.** No plain-text Y/N prompts.
- **Consumption happens ONLY after the dialog.** Step 1 is always
  `consume="false"`; NEVER pass `consume="true"` without a user-chosen
  `handoff_id` or an explicit user instruction.
- `consume=""` (auto) and the CLI's `--consume always` are for AUTONOMOUS
  flows (Codex, CI, unattended agents) where no human can be asked — never
  this skill.
- Race note: a parallel session may consume the same handoff between Step 1
  and Step 3. A server-selected consume then degrades to `consumed: false`
  (no error) — just proceed with the fresh snapshot. An explicit `handoff_id`
  (this skill's path) instead errors "not found or already consumed": report
  it verbatim, keep the plan you already read in Step 1, and do NOT retry in
  a loop.
- On empty sprint + no handoffs, don't force-create a sprint — ask user what to do.
- Show resolved items (they surface via `summary.sprint_completion` and
  `recent_changes`) only as context, not as work candidates.
- `consumed_by` MUST mirror /handoff's `created_by` format:
  `claude-<model_id> @ <project_slug> <iso_utc>Z` — required on the Step 3
  consume, meaningless (and unnecessary) on the Step 1 read.
- Round-trip budget: 2 dct calls — the Step 1 read plus the Step 3 consume,
  with `get_item` batched into the same parallel message. No handoff to
  consume (or "keep unconsumed") = 1 call. If you find yourself calling
  `review_sprint` or `get_handoff` here, you are off the rails.
