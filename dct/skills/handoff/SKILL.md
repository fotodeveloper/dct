---
name: handoff
description: >-
  [dct] End-of-session workflow — flag next-sprint items and leave a handoff
  prompt. Use when: "handoff", "end session", "wrap up" — in any language.
---

# /handoff — Session Handoff

Finalize a coding session by (a) marking which items belong to the next sprint
and (b) optionally writing a prompt for the next session to pick up.

No ToolSearch preload needed for the main path: `get_context_snapshot`,
`handoff_work` and `list_items` are alwaysLoad residents.
(`promote_checkpoint_to_item`, `resolve_item` fallbacks: `resolve_item` is
resident; only `promote_checkpoint_to_item` needs a ToolSearch load — do it
lazily, ONLY when the user actually picks the promote option.)

## Workflow

### Step 1: Preflight (ONE parallel message)

Gather all state in a single parallel message:
- `mcp__dct__get_context_snapshot(project="")` — CWD auto-detect; returns all
  active sprints (`sprint.active_sprints`, `primary_sprint_id`), member items
  with `cp`, their `checkpoints`, `stale_in_progress`, pending `gates`, live
  `decisions`, `recent_changes` and `summary`. Gates and decisions FEED THE
  DRAFT: an unresolved gate or open ADR question belongs in the handoff text.
- `mcp__dct__list_items(project="", search="CC task list", tag="cc-task-list")` —
  the cc-task-list mirror probe (see Step 2, question 3). The `tag` filter runs
  SERVER-SIDE; no client-side post-filtering is needed. The tag dictionary is
  closed: if `cc-task-list` was never registered the call returns
  `{"error": ...}` — that means "no mirror items exist", so treat it as an empty
  result and carry on, never as a failed preflight.

**Fallback only when project is unresolved** (`get_context_snapshot` errors
because CWD is not a registered project): `mcp__dct__list_projects()` +
`AskUserQuestion` ("Which project is this handoff for?" → slugs + "Cancel"),
then re-run preflight with the chosen slug.

**Empty project fallback** — if all sections are empty (no items at all, or
everything resolved), offer via `AskUserQuestion`:
- question: "Nothing to propose — project has no open or recent items. What now?"
- options: `"Skip sprint proposal — just write handoff prompt"` / `"Cancel /handoff"` / `"Run /track-list to browse state first"`.

### Step 2: Merged decision (ONE AskUserQuestion, up to 4 questions)

Ask all setup decisions in a SINGLE `AskUserQuestion` call. Include only the
questions that apply. There is NO "write a prompt?" question here — the draft
itself is the question, in Step 3 (asking twice about the same thing wastes a
decision; user call, 2026-07-15):

1. **Sprint membership** — sprints are MULTI-SESSION (ADR #57): a handoff
   designates the NEXT SESSION'S SLICE, it does not re-plan the sprint. The
   session edge is therefore ADDITIVE by default and "untouched" is the
   default answer. Propose additions from: all P1 open items not yet in the
   sprint + top 3-5 P2 open items with existing checkpoints + still-open items
   from `recent_changes`.
   - question: "Sprint membership for the next session?"
   - options:
     - "Sprint untouched" (DEFAULT/recommended) — carry the sprint as-is.
     - "Add discovered items (22, 6)" — the proposed ADD list.
     - "Cut scope (remove #7)" — the proposed REMOVE list. Removing a
       resolved / wont_fix member is REFUSED by the server — terminal members
       are the sprint's completion history, not scope (the escape hatch is
       `remove_item_from_sprint`).
     - "Replan — REPLACE open membership (22, 21, 6)" — conscious replanning
       only. Surface the DIFF in the question text ("removing: #7; adding:
       #22") and state that resolved members STAY as history.
     - "Custom list (type in)" — ask which mode it is (add / remove / replace).
   - There is NO "empty sprint" option: emptying a sprint is `clear_sprint`'s
     job and `handoff_work` rejects an empty replace.
2. **Target sprint** (include ONLY when `summary.active_sprint_count >= 2` and
   the sprint is being modified): "Which active sprint gets these items?" —
   one option per `sprint.active_sprints` entry (`#id (code) — name, size N`).
   `handoff_work` REFUSES to guess among several actives (and so does every
   current-sprint helper), so this must be decided before the finale — it is
   also the sprint the "Skip the handoff" branch writes to.
3. **Scope tag**:
   - question: "Optional scope tag (disambiguates parallel sessions)?"
   - options: "No scope" | "sprint-v<next>" | "refactor-<area>" | "Other (type in)".
4. **CC task disposition** (include ONLY if the Step 1 probe found a
   `cc-task-list` item — title `"CC task list — session <id>"`, from the
   `dct/hooks/dct-task-mirror.sh` mirror):
   - question: f"Session item #{id} has N CC task-list checkpoints. What to do?"
   - options:
     - "Resolve — one-off, archive it" → `resolve_item(id, status="resolved", resolution="session ephemeral, archived on /handoff")`
     - "Promote N checkpoints to real items" → `promote_checkpoint_to_item` per checkpoint (load it via ToolSearch first)
     - "Keep — revisit next session" → leave as-is; next /pickup surfaces it
     - "Skip"

Handle "Custom list" / "Other" scope with a follow-up free-text `AskUserQuestion`.

### Step 3: Draft approval (TWO TURNS — print the draft, THEN ask)

ALWAYS auto-draft once Step 2 resolves — the draft must reflect the Step 2
answers (sprint composition, scope, cc-task outcome). Draft 2-4 sentences: what
was accomplished (resolved items / recent changelog), what's next (first 1-2
sprint items), any open design questions — plus pending GATES and open
DECISIONS from the snapshot (the next session must honor them without
transcript access).

The approval is TWO TURNS and this is NON-NEGOTIABLE. **Why: the
`AskUserQuestion` dialog truncates and hides long text**, so a draft carried
inside the question, an option label, an option description or an option
`preview` is a draft the user CANNOT read — they would be approving text they
have never seen.

**Turn 1 — print the draft as plain message text, nothing else.** No tool call
in this turn. Emit the FULL draft prompt verbatim (and the full
`context_summary` if one was drafted), clearly delimited so the boundaries are
unambiguous:

```
Draft handoff prompt:
--- BEGIN DRAFT ---
<the full prompt text, verbatim>
--- END DRAFT ---

Draft context_summary:            (only when one was drafted)
--- BEGIN CONTEXT SUMMARY ---
<the full context_summary, verbatim>
--- END CONTEXT SUMMARY ---
```

**Turn 2 — ONE single-select `AskUserQuestion` that REFERENCES the draft above
and contains NONE of its content.** Question text and every option label and
description stay short and self-contained; do NOT use the `preview` field for
the draft:
- question: "Save the draft handoff prompt shown above?"
- options:
  - "Save the draft shown above as-is" (recommended)
  - "Edit — I will type changes" — collect the changes in a free-text
    follow-up, then REPEAT Step 3 from Turn 1 with the revised draft (the user
    must read the revision before approving it too).
  - "I'll dictate" — user writes the prompt themselves (free-text follow-up).
  - "Skip the handoff" — sprint flags only, no handoff row.
- NEVER embed draft content in the question or in any option field. The dialog
  references what the previous turn printed; the printed turn is what the user
  reads.

### Step 4: Finale (ONE atomic call)

```
mcp__dct__handoff_work(
    prompt=<approved text>,
    created_by=f"claude-{model_id} @ {project_slug} {isoformat_utc_now()}Z",
    project="",
    item_ids=<"__keep__" (default) | "22,21,6" ONLY for a conscious replan>,
    add_item_ids=<"22,6" when the user chose "Add discovered items">,
    remove_item_ids=<"7" when the user chose "Cut scope">,
    sprint=<id-or-code, ONLY when Step 2 question 2 picked one>,
    scope=<scope or "">,
)
```

Sprint membership and the handoff row are written in ONE DB transaction —
a failed sprint write leaves NO handoff (and vice versa), so there is no
ordering to manage and no partial state to report. Mapping from Step 2
question 1: "Sprint untouched" → send nothing (`item_ids="__keep__"`, the
default, no membership write); "Add" / "Cut scope" → `add_item_ids` /
`remove_item_ids` (they may be combined); "Replan" → `item_ids=<CSV>`, which
is mutually exclusive with the additive pair. **NEVER send `item_ids=""`** —
an empty replace errors by design (emptying a sprint is `clear_sprint`'s job).

Branch: Step 3 ended in **"Skip the handoff"** → `handoff_work` does not apply
(prompt is required by design). Then, if and only if the sprint changed:
- **≤ 1 active sprint** → `mcp__dct__set_sprint(project, item_ids=...)` alone
  (replace) — it targets the single active sprint unambiguously.
- **≥ 2 active sprints** → `set_sprint` (and every other current-sprint write)
  now ERRORS rather than guessing which sprint you meant. Write to the sprint
  chosen in Step 2 question 2 explicitly, one item at a time:
  `mcp__dct__assign_item_to_sprint(item_id, sprint_id)` /
  `mcp__dct__remove_item_from_sprint(item_id, sprint_id)` (both deferred —
  ToolSearch-load them only in this branch).
If the sprint was kept too, there is nothing to write at all.

Remember the returned `handoff_id`.

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
- Never modify sprint membership without user confirming the list. The
  default is "untouched" — a sprint spans sessions (ADR #57); a handoff
  designates the next slice, it does not re-plan by default.
- Terminal (resolved / wont_fix) members are the sprint's completion history:
  a replace leaves them in place and a remove of one is refused. Never try to
  "clean up" a sprint by dropping resolved items — that destroys
  `sprint_completion`.
- Never write a handoff prompt without user confirming content (the Step 3
  two-turn approval is that confirmation: printed draft, then dialog).
- **Long text NEVER goes inside an `AskUserQuestion`.** The dialog truncates
  and hides it. Any handoff prompt, context_summary or snapshot the user must
  read is printed as plain message text in its own turn FIRST; the dialog that
  follows only references it ("shown above") with short, self-contained
  options.
- `created_by` MUST be non-empty; include model id + project + UTC timestamp
  (`handoff_work` refuses blanks).
- With several active sprints, the target sprint is a USER decision (Step 2
  question 2) — `handoff_work` errors rather than guessing, and so do the
  current-sprint helpers (`set_sprint`, `clear_sprint`,
  `add_item_to_current_sprint`, `remove_item_from_current_sprint`). Never
  pick one silently.
- Unknown/foreign item ids make `handoff_work` fail atomically (nothing
  written) — report the error verbatim and re-ask, don't retry blindly.
- Round-trip budget: preflight = 1 parallel message (2 calls), finale = 1 call.
  If you find yourself sequencing `set_sprint` → `create_handoff`, you are off
  the rails (that pair survives ONLY in the "Skip the handoff" branch).
