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
lazily, ONLY when a Change asks to promote the CC tasks.)

## Workflow

### Step 1: Preflight (ONE parallel message)

Gather all state in a single parallel message:
- `mcp__dct__get_context_snapshot(project="")` — CWD auto-detect; returns all
  active sprints (`sprint.active_sprints`, `primary_sprint_id`), member items
  with `cp`, their `checkpoints`, `stale_in_progress`, pending `gates`, live
  `decisions`, `recent_changes` and `summary`. Gates and decisions FEED THE
  DRAFT: an unresolved gate or open ADR question belongs in the handoff text.
- `mcp__dct__list_items(project="", search="CC task list", tag="cc-task-list")` —
  the cc-task-list mirror probe (see the `CC tasks:` line in Step 2). The `tag`
  filter runs SERVER-SIDE; no client-side post-filtering is needed. The tag
  dictionary is closed: if `cc-task-list` was never registered the call
  returns `{"error": ...}` — that means "no mirror items exist", so treat it as
  an empty result and carry on, never as a failed preflight.

**Fallback only when project is unresolved** (`get_context_snapshot` errors
because CWD is not a registered project): `mcp__dct__list_projects()` +
`AskUserQuestion` ("Which project is this handoff for?" → slugs + "Cancel"),
then re-run preflight with the chosen slug.

### Step 2: The draft and ONE question (TWO TURNS — print, THEN ask)

Auto-draft at once — no setup questions. Draft 2-4 sentences: what was
accomplished (resolved items / recent changelog), what's next (first 1-2
sprint items), any open design questions — plus pending GATES and open
DECISIONS from the snapshot (the next session must honor them without
transcript access). An empty project (no open or recent items) still gets a
short draft that says so.

The sprint and scope decisions are LINES OF THE DRAFT, with their defaults —
never questions of their own:

- **`Sprint: unchanged`** — the default. Sprints are MULTI-SESSION (ADR #57):
  a handoff designates the NEXT SESSION'S SLICE, it does not re-plan the
  sprint. Add a `Could add:` line with the candidates — P1 open items not yet
  in the sprint, the top few P2 open items with existing checkpoints, still-open
  items from `recent_changes` — so the user can take them through "Change".
- **`Scope: none`** — the default; a scope tag only disambiguates parallel
  sessions.
- **`CC tasks: keep #N`** — ONLY when the Step 1 probe found a `cc-task-list`
  item (title `"CC task list — session <id>"`, from the
  `dct/hooks/dct-task-mirror.sh` mirror): kept for the next /pickup unless a
  Change resolves it or promotes its checkpoints.

The approval is TWO TURNS and this is NON-NEGOTIABLE. **Why: the
`AskUserQuestion` dialog truncates and hides long text**, so a draft carried
inside the question, an option label, an option description or an option
`preview` is a draft the user CANNOT read — they would be approving text they
have never seen.

**Turn 1 — print the draft as plain message text, nothing else.** No tool call
in this turn. Emit the FULL draft prompt verbatim (and the full
`context_summary` if one was drafted), clearly delimited, followed by the
decision lines:

```
Draft handoff:
--- BEGIN DRAFT ---
<the full prompt text, verbatim>
--- END DRAFT ---
Sprint: unchanged
Could add: #22, #6 (P1, not in the sprint)
Scope: none
CC tasks: keep #31            (only when the probe found one)

Draft context_summary:        (only when one was drafted)
--- BEGIN CONTEXT SUMMARY ---
<the full context_summary, verbatim>
--- END CONTEXT SUMMARY ---
```

**Turn 2 — ONE single-select `AskUserQuestion` that REFERENCES the draft above
and contains NONE of its content.** Question text and every option label and
description stay short and self-contained; do NOT use the `preview` field for
the draft:
- question: "Save the handoff shown above?"
- options:
  - "Save the handoff shown above" (recommended)
  - "Change — I will say what" — prompt text, sprint, scope or CC tasks
  - "Cancel — write nothing"
- NEVER embed draft content in the question or in any option field. The dialog
  references what the previous turn printed; the printed turn is what the user
  reads.

**"Change"** → collect what to change in a free-text follow-up (in plain
words: "add #22", "cut #7", "replan to 22, 21, 6", "scope g1", "resolve the CC
tasks", "sprint only, no prompt", new prompt text), apply it to the draft, then
REPEAT Step 2 from Turn 1: print the revised draft again and ask again — the
user must read the revision before saving it too. A sprint change shows on the
`Sprint:` line as `add #22, #6` / `cut #7` / `replace → #22, #21, #6
(removing #7; resolved members stay as history)`. Removing a resolved /
wont_fix member is REFUSED by the server — terminal members are the sprint's
completion history, not scope (the escape hatch is `remove_item_from_sprint`).
There is NO "empty sprint" change: emptying a sprint is `clear_sprint`'s job
and `handoff_work` rejects an empty replace.

**Target sprint** — when a Change modifies the sprint AND
`summary.active_sprint_count >= 2`, ask "Which active sprint gets these
items?" (one option per `sprint.active_sprints` entry: `#id (code) — name,
size N`) before reprinting, and show the answer as a `Target sprint:` line.
`handoff_work` REFUSES to guess among several actives (and so does every
current-sprint helper).

**"Cancel"** → nothing is written: no handoff, no sprint change.

### Step 3: Finale (ONE atomic call, after "Save")

```
mcp__dct__handoff_work(
    prompt=<saved text>,
    created_by=f"claude-{model_id} @ {project_slug} {isoformat_utc_now()}Z",
    project="",
    item_ids=<"__keep__" (default) | "22,21,6" ONLY for a conscious replan>,
    add_item_ids=<"22,6" when the draft says "add">,
    remove_item_ids=<"7" when the draft says "cut">,
    sprint=<id-or-code, ONLY when the draft has a Target sprint line>,
    scope=<scope or "">,
    context_summary=<the approved context_summary, verbatim, or "" when none was drafted>,
)
```

Every line of the saved draft maps to an argument, so nothing the user
approved is lost: the prompt → `prompt`, the `Draft context_summary` block →
`context_summary`, `Sprint:` → `item_ids` / `add_item_ids` /
`remove_item_ids`, `Target sprint:` → `sprint`, `Scope:` → `scope`,
`CC tasks:` → its own calls below. The context_summary is passed verbatim —
exactly the text printed on the draft the user saved; never shorten or
regenerate it after "Save" (the next /pickup prints it as written).

Sprint membership and the handoff row are written in ONE DB transaction —
a failed sprint write leaves NO handoff (and vice versa), so there is no
ordering to manage and no partial state to report. Mapping from the `Sprint:`
line: "unchanged" → send nothing (`item_ids="__keep__"`, the default, no
membership write); "add" / "cut" → `add_item_ids` / `remove_item_ids` (they
may be combined); "replace" → `item_ids=<CSV>`, which is mutually exclusive
with the additive pair. **NEVER send `item_ids=""`** — an empty replace errors
by design (emptying a sprint is `clear_sprint`'s job).

The `CC tasks:` line: "keep" → nothing; "resolve" →
`resolve_item(id, status="resolved", resolution="session ephemeral, archived on /handoff")`;
"promote" → `promote_checkpoint_to_item` per checkpoint (load it via
ToolSearch first).

Branch: the saved draft says **"sprint only, no prompt"** → `handoff_work`
does not apply (prompt is required by design). If the sprint was kept too,
there is nothing to write at all. Otherwise map the `Sprint:` line to the
existing additive tools, one item at a time — an "add" adds, a "cut" cuts, and
nothing else is touched:

- **No active sprint** → "add #22" → `mcp__dct__add_item_to_current_sprint(22,
  project)`, which creates the current sprint; a "cut" has nothing to cut —
  stop and say so ("no active sprint, nothing to cut"); a "replace" →
  `set_sprint`, below.
- **One active sprint** → "add" → `mcp__dct__add_item_to_current_sprint(item_id,
  project)`; "cut" → `mcp__dct__remove_item_from_current_sprint(item_id,
  project)`, only for an OPEN member (the snapshot's `sprint.items` status) —
  a resolved / wont_fix member is the sprint's history: stop and say so
  instead of removing it.
- **Several active sprints** → the `Target sprint:` line names the sprint (the
  Target sprint question above); "add" → `mcp__dct__assign_item_to_sprint(item_id,
  sprint_id)`, "cut" → `mcp__dct__remove_item_from_sprint(item_id, sprint_id)`
  (both deferred — ToolSearch-load them only in this branch, with the two
  current-sprint tools when needed). A missing or ambiguous target stops
  readably — "several active sprints; which one gets #22?" — it is never
  guessed.
- **"replace"** — only when the saved draft says `replace → …` (a replacement
  the user accepted) → `mcp__dct__set_sprint(project, item_ids=<the listed
  ids>)` with no active or one active sprint. With several active sprints
  `set_sprint` errors and no tool replaces a chosen one without a handoff:
  stop and say that this replace needs a prompt (`handoff_work` with
  `sprint=…`).

Never compute a full list for `set_sprint` from a read of the sprint plus
the change (read + replace) — an item another session added meanwhile would
be dropped. An add or a cut is never a replace.

Remember the returned `handoff_id`.

### Step 4: Report

```
✓ Handoff #N zapisany
  Sprint: 3 itemy (#22, #21, #6)
  Scope: sprint-v0.2
  Prompt: "Zacznij od #22..." (142 chars)

Next session: /pickup
```

## Rules

- **Every user decision is an `AskUserQuestion` call.** No plain-text prompts.
- Never modify sprint membership without the user's "Save" on a draft whose
  `Sprint:` line shows the change. The default is "unchanged" — a sprint spans
  sessions (ADR #57); a handoff designates the next slice, it does not re-plan
  by default.
- Terminal (resolved / wont_fix) members are the sprint's completion history:
  a replace leaves them in place and a remove of one is refused. Never try to
  "clean up" a sprint by dropping resolved items — that destroys
  `sprint_completion`.
- Never write a handoff before the user's "Save" on the printed draft (the
  two-turn Step 2: printed draft, then the one question).
- **Long text NEVER goes inside an `AskUserQuestion`.** The dialog truncates
  and hides it. Any handoff prompt, context_summary or snapshot the user must
  read is printed as plain message text in its own turn FIRST; the dialog that
  follows only references it ("shown above") with short, self-contained
  options.
- `created_by` MUST be non-empty; include model id + project + UTC timestamp
  (`handoff_work` refuses blanks).
- With several active sprints, the target sprint is a USER decision (the
  Target sprint question after a sprint Change) — `handoff_work` errors rather
  than guessing, and so do the current-sprint helpers (`set_sprint`,
  `clear_sprint`, `add_item_to_current_sprint`,
  `remove_item_from_current_sprint`). Never pick one silently.
- Unknown/foreign item ids make `handoff_work` fail atomically (nothing
  written) — report the error verbatim and re-ask, don't retry blindly.
- Round-trip budget: preflight = 1 parallel message (2 calls), finale = 1 call.
  If you find yourself sequencing `set_sprint` → `create_handoff`, you are off
  the rails (the "sprint only, no prompt" branch writes no handoff at all,
  and uses `set_sprint` only for an accepted replace).
