---
name: handoff
description: >-
  [dct] End-of-session workflow — flag next-sprint items and leave a handoff
  prompt. Use when: "handoff", "end session", "wrap up" — in any language.
---

# /handoff — Session Handoff

Finalize a coding session by (a) marking which items belong to the next sprint
and (b) optionally writing a prompt for the next session to pick up.

## Deferred-tool load (do this first)

`review_sprint`, `set_sprint`, and `create_handoff` are deferred — load their
schemas before first use with ONE ToolSearch call:

```
ToolSearch("select:mcp__dct__review_sprint,mcp__dct__set_sprint,mcp__dct__create_handoff")
```

## Workflow

### Step 1: Preflight (ONE parallel message)

After the ToolSearch load, gather all state in a single parallel message:
- `mcp__dct__review_sprint(project="", since="last_release")` — CWD auto-detect;
  returns current `in_sprint`, `recent_activity`, `stale_in_progress`.
- `mcp__dct__list_items(project="", search="CC task list")` — the cc-task-list
  mirror probe (see Step 2, question 3). `list_items` is not deferred and has NO
  `tags` parameter, so filter the returned rows CLIENT-SIDE: keep only rows whose
  `tags` column contains `cc-task-list`.

**Fallback only when project is unresolved** (`review_sprint` errors because CWD
is not a registered project): `mcp__dct__list_projects()` + `AskUserQuestion`
("Which project is this handoff for?" → slugs + "Cancel"), then re-run preflight
with the chosen slug.

**Empty project fallback** — if all sections are empty (no items at all, or
everything resolved), offer via `AskUserQuestion`:
- question: "Nothing to propose — project has no open or recent items. What now?"
- options: `"Skip sprint proposal — just write handoff prompt"` / `"Cancel /handoff"` / `"Run /track-list to browse state first"`.

### Step 2: Merged decision (ONE AskUserQuestion, up to 3 questions)

Ask all setup decisions in a SINGLE `AskUserQuestion` call. Include only the
questions that apply. There is NO "write a prompt?" question here — the draft
itself is the question, in Step 3 (asking twice about the same thing wastes a
decision; user call, 2026-07-15):

1. **Sprint items** — propose: all P1 open items (always) + top 3-5 P2 open items
   with existing checkpoints + still-open items from `recent_activity`.
   - question: "Which items go into the next sprint?"
   - options: proposed list as "22, 21, 6 (proposed)" | "Keep current sprint" | "Empty sprint" | "Custom list (type in)".
   - If replacing a non-empty sprint, surface the DIFF in the question text
     ("removing: #7; adding: #22").
2. **Scope tag**:
   - question: "Optional scope tag (disambiguates parallel sessions)?"
   - options: "No scope" | "sprint-v<next>" | "refactor-<area>" | "Other (type in)".
3. **CC task disposition** (include ONLY if the Step 1 probe found a
   `cc-task-list` item — title `"CC task list — session <id>"`, from the
   `dct/hooks/dct-task-mirror.sh` mirror):
   - question: f"Session item #{id} has N CC task-list checkpoints. What to do?"
   - options:
     - "Resolve — one-off, archive it" → `resolve_item(id, status="resolved", resolution="session ephemeral, archived on /handoff")`
     - "Promote N checkpoints to real items" → `promote_checkpoint_to_item` per checkpoint (or a user-picked subset)
     - "Keep — revisit next session" → leave as-is; next /pickup surfaces it
     - "Skip"

Handle "Custom list" / "Other" scope with a follow-up free-text `AskUserQuestion`.

### Step 3: Draft approval (SEPARATE second AskUserQuestion, draft in PREVIEW)

ALWAYS auto-draft once Step 2 resolves — this single dialog decides both
"whether" and "which text", so the draft must reflect the Step 2 answers
(sprint composition, scope, cc-task outcome). Draft 2-4 sentences: what was
accomplished (resolved items / recent changelog), what's next (first 1-2 sprint
items), any open design questions.

Present ONE single-select question. The FULL draft rides in the option
`preview` field (side-by-side rendering), NOT in the question text — long
content belongs in previews; keep the question and option descriptions short:
- question: "Prompt for the next session — save the drafted text?"
- options:
  - "Save draft" (recommended) — `preview` = THE FULL DRAFT TEXT; description
    notes that corrections can be attached via the option note ("n") and are
    applied BEFORE saving.
  - "Edit first" — `preview` = the same full draft; collect changes in a
    follow-up, then re-present this dialog with the revised draft.
  - "I'll dictate" — user writes the prompt themselves (free-text follow-up).
  - "Skip — no prompt" — sprint flags only, no handoff row.
- The user MUST be able to read the entire draft inside the dialog (the preview
  panel); never rely on plain text printed the same turn.

### Step 4: Finale (SEQUENTIAL — sprint first, then handoff)

Do NOT batch these two writes: the handoff prompt asserts sprint membership, so a
failed `set_sprint` must not still persist a handoff that references a sprint that
was never written. Run them in order, and omit either call that doesn't apply
(skip `set_sprint` on "Keep current sprint"; skip `create_handoff` when Step 3
ended in "Skip — no prompt"):

1. `mcp__dct__set_sprint(project, item_ids="22,21,6")` (empty string = empty sprint).
   If it returns an error, STOP — report the failure and do NOT write the handoff.
2. Only after `set_sprint` succeeds (or was skipped):
   `mcp__dct__create_handoff(project=<slug>, prompt=<text>, created_by=f"claude-{model_id} @ {project_slug} {isoformat_utc_now()}Z", scope=<scope or "">)`

This session-end step tolerates the one extra round-trip. Remember the returned
handoff `id`.

### Step 5: Report

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
- Never write a handoff prompt without user confirming content (the Step 3
  draft-preview dialog is that confirmation).
- `created_by` MUST be non-empty; include model id + project + UTC timestamp.
- On `set_sprint`, if replacing a non-empty sprint, surface the diff ("removing: #7; adding: #22") in the confirmation question.
