---
name: handoff
description: |
  End-of-session workflow — flag items as next-sprint and leave a handoff prompt
  for the next session.
  Use when: "handoff", "zakończ sesję", "do następnej sesji", "end session",
  "hand off", "zrób handoff", "wrap up".
---

# /handoff — Session Handoff

Finalize a coding session by (a) marking which items belong to the next sprint
and (b) optionally writing a prompt for the next session to pick up.

## Workflow

### Step 1: Detect project

Call `mcp__dct__list_projects` and match against CWD (longest path prefix).
If CWD is not a registered project, ask user via `AskUserQuestion`:
  - question: "Which project is this handoff for?"
  - options: list of registered project slugs + "Cancel"

### Step 2: Offer scope tag

Ask via `AskUserQuestion`:
  - question: "Optional scope tag for this handoff (helps disambiguate when multiple sessions run in parallel)?"
  - options: "No scope", "sprint-v<next>", "refactor-<area>", "Other (type in)"
  - If user picks "Other", follow up with free-text AskUserQuestion.

Remember the chosen `scope` (may be empty).

### Step 3: Show current sprint + propose items

Call `mcp__dct__review_sprint(project, since="last_release")` to get:
  - current `in_sprint` list (what's already flagged)
  - `recent_activity` — items touched since last release
  - `stale_in_progress` — items blocked or forgotten

**Empty project fallback** — if all sections are empty (no items at all in
the project, or everything is resolved), offer via `AskUserQuestion`:
- question: "Nothing to propose — project has no open or recent items. What now?"
- options: `"Skip sprint proposal — just write handoff prompt"` / `"Cancel /handoff"` / `"Run /track-list to browse state first"`.

Present a draft sprint proposal based on:
  - All P1 open items (always)
  - Top 3-5 P2 open items with existing checkpoints
  - Any items currently in `recent_activity` that are still open

Ask via `AskUserQuestion`:
  - question: "Which items go into the next sprint? (IDs, comma-separated, or 'keep current', or 'empty sprint')"
  - options: the proposed list formatted as "22, 21, 6 (proposed)" | "Keep current sprint" | "Empty sprint" | "Custom list (type in)"
  - On "Custom list" — follow up with free-text ids.

### Step 4: Set sprint

Parse chosen ids. Call `mcp__dct__set_sprint(project, item_ids="22,21,6")`
(or with empty string for empty sprint, or skip this step if "Keep current").

### Step 5: Optionally write handoff prompt

Ask via `AskUserQuestion`:
  - question: "Write a prompt for the next session?"
  - options:
    - "Yes — I'll dictate" (user types freely)
    - "Yes — auto-draft from sprint context" (assistant drafts, user confirms)
    - "No"

On "Yes — auto-draft":
  - Draft a 2-4 sentence prompt summarizing:
    - What was just accomplished (from resolved items / recent changelog)
    - What's next (first 1-2 sprint items with brief context)
    - Any open design questions worth flagging
  - Present draft, ask `AskUserQuestion` for confirmation:
    - "Save this prompt? [Yes / Edit first / Skip]"

On "Yes — I'll dictate": prompt user for free-text input.

On "No": skip.

### Step 6: Create handoff entry

If prompt was provided, call:
```
mcp__dct__create_handoff(
    project=<slug>,
    prompt=<text>,
    created_by=f"claude-{model_id} @ {project_slug} {isoformat_utc_now()}Z",
    scope=<scope or "">,
)
```

Remember the returned `id`.

### Step 6b: CC task-list mirror cleanup (v0.4.0)

If the TaskCreate mirror hook (`dct/hooks/dct-task-mirror.sh`) is active, the
current session may have spawned a session-scoped item with checkpoints
mirrored from TaskCreate/TodoWrite calls. These items have the tag
`cc-task-list` and title `"CC task list — session <id>"`.

Probe: `mcp__dct__list_items(project=<slug>)` filtered for `tags` containing
`cc-task-list`. For each such item surface via `AskUserQuestion`:

  question: f"Session item #{id} has N CC task-list checkpoints. What to do?"
  header:   "CC task list"
  options:
    - "Resolve — the todos were one-off, archive it"
      → `resolve_item(id, status="resolved", resolution="session ephemeral, archived on /handoff")`
    - "Promote N checkpoints to real items"
      → for each cc-task checkpoint: `promote_checkpoint_to_item` (or user picks subset)
    - "Keep — I'll revisit next session"
      → leave as-is; next session's /pickup will surface it
    - "Skip"

This keeps backlog clean — ephemeral harness-tasks don't accumulate as
permanent items across months of sessions.

### Step 7: Report

```
✓ Handoff #N zapisany
  Sprint: 3 itemy (#22, #21, #6)
  Scope: sprint-v0.2
  Prompt: "Zacznij od #22..." (142 chars)

Next session: /pickup
```

## Rules

- **Every user decision is an `AskUserQuestion` call.** No plain-text prompts.
- Never call `set_sprint` without user confirming the list.
- Never write a handoff prompt without user confirming content.
- `created_by` MUST be non-empty; include model id + project + UTC timestamp.
- On `set_sprint`, if replacing a non-empty sprint, surface the diff ("removing: #7; adding: #22") in the confirmation question.
