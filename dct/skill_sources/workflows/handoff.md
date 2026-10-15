# {{host.skill-prefix}}handoff — Session Handoff

Finalize a coding session by (a) marking which items belong to the next sprint
and (b) optionally writing a prompt for the next session to pick up.

{{shared.discovery}}

## Workflow

### Step 1: Preflight (ONE parallel message)

Gather all state in a single parallel message:
- `mcp__dct__get_context_snapshot(project="")` — CWD auto-detect; returns all
  active sprints (`sprint.active_sprints`, `primary_sprint_id`), member items
  with `cp`, their `checkpoints`, `stale_in_progress`, pending `gates`, live
  `decisions`, `recent_changes` and `summary`. Gates and decisions FEED THE
  DRAFT: an unresolved gate or open ADR question belongs in the handoff text.
{{host.task-mirror-probe}}

**Fallback only when project is unresolved** (`get_context_snapshot` errors
because CWD is not a registered project): `mcp__dct__list_projects()` +
{{host.question}} ("Which project is this handoff for?" → slugs + "Cancel"),
then re-run preflight with the chosen slug.

### Step 2: The draft and ONE question (the draft travels inside the dialog)

Auto-draft at once — no setup questions. Draft 2-4 sentences: what was
accomplished (resolved items / recent changelog), what's next (first 1-2
sprint items), any open design questions — plus pending GATES and open
DECISIONS from the snapshot (the next session must honor them without
transcript access). An empty project (no open or recent items) still gets a
short draft that says so.

The sprint and scope decisions are LINES OF THE DRAFT, with their defaults — `Sprint: unchanged` and `Scope: none` — never questions of their own:

- **`Sprint: unchanged`** — the default. Sprints are MULTI-SESSION (ADR #57):
  a handoff designates the NEXT SESSION'S SLICE, it does not re-plan the
  sprint. Add a `Could add:` line with the candidates — P1 open items not yet
  in the sprint, the top few P2 open items with existing checkpoints, still-open
  items from `recent_changes` — so the user can take them through "Change".
- **`Scope: none`** — the default; a scope tag only disambiguates parallel
  sessions.
- **`Native tasks: keep #N`** — ONLY when the Step 1 host probe found an actual mirror item: keep it for the next pickup unless an approved Change resolves it or promotes its checkpoints.

{{host.handoff-screen}}

The screen is the draft with its decision lines — the prompt as written, one
line per paragraph (the dialog wraps it; what is saved is the prompt word for
word), each decision line under 60 characters:

```
--- BEGIN DRAFT ---
<the full prompt text, verbatim>
--- END DRAFT ---
Sprint: unchanged
Could add: #22, #6 (P1, not in the sprint)
Scope: none
Native tasks: keep #31            (only when the probe found one)

--- BEGIN CONTEXT SUMMARY ---  (only when one was drafted)
<the full context_summary, verbatim>
--- END CONTEXT SUMMARY ---
```

ONE {{host.question}}:
- question: "Save this handoff for the next session?"
- options:
  - "Save" (recommended) — its preview: the screen (its page 1 when the
    screen is longer than 15 lines)
  - "Change — I will say what" — prompt text, sprint, scope or native tasks
  - "Cancel — write nothing"
- A screen longer than 15 lines is split into pages of at most 15 lines, and
  the dialog gets a second single-select question, "Details", with one option
  per page ("Page 2 of 3"), each with that page as its preview. Every word of
  the draft and of the context_summary is on a page — nothing is shortened. A
  draft that needs more than 4 pages is too long for a handoff: shorten it
  before asking.
- The question text and the option labels and descriptions carry none of the
  draft: it is only in the previews.

**"Change"** → collect what to change in a free-text follow-up (in plain
words: "add #22", "cut #7", "replan to 22, 21, 6", "scope g1", "resolve the native
tasks", "sprint only, no prompt", new prompt text), apply it to the draft, then
ask Step 2's dialog again with the revised draft in its previews — the user
must read the revision before saving it too. A sprint change shows on the
`Sprint:` line as `add #22, #6` / `cut #7` / `replace → #22, #21, #6
(removing #7; resolved members stay as history)`. Removing a resolved /
wont_fix member is REFUSED by the server — terminal members are the sprint's
completion history, not scope (the escape hatch is `remove_item_from_sprint`).
There is NO "empty sprint" change: emptying a sprint is `clear_sprint`'s job
and `handoff_work` rejects an empty replace.

**Target sprint** — when a Change modifies the sprint AND
`summary.active_sprint_count >= 2`, ask "Which active sprint gets these
items?" (one option per `sprint.active_sprints` entry: `#id (code) — name,
size N`) before asking again, and show the answer as a `Target sprint:` line.
`handoff_work` REFUSES to guess among several actives (and so does every
current-sprint helper).

**"Cancel"** → nothing is written: no handoff, no sprint change.

### Step 3: Finale (ONE atomic call, after "Save")

```
mcp__dct__handoff_work(
    prompt=<saved text>,
    created_by="<known_actor_identity>",
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
approved is lost: the prompt → `prompt`, the `CONTEXT SUMMARY` block →
`context_summary`, `Sprint:` → `item_ids` / `add_item_ids` /
`remove_item_ids`, `Target sprint:` → `sprint`, `Scope:` → `scope`,
`Native tasks:` → its own calls below. The context_summary is passed verbatim —
exactly the text on the draft the user saved; never shorten or regenerate it
after "Save" (the next {{host.skill-prefix}}pickup shows it as written).

Sprint membership and the handoff row are written in ONE DB transaction —
a failed sprint write leaves NO handoff (and vice versa), so there is no
ordering to manage and no partial state to report. Mapping from the `Sprint:`
line: "unchanged" → send nothing (`item_ids="__keep__"`, the default, no
membership write); "add" / "cut" → `add_item_ids` / `remove_item_ids` (they
may be combined); "replace" → `item_ids=<CSV>`, which is mutually exclusive
with the additive pair. **NEVER send `item_ids=""`** — an empty replace errors
by design (emptying a sprint is `clear_sprint`'s job).

The `Native tasks:` line: "keep" → nothing; "resolve" →
`resolve_item(id, status="resolved", resolution="session ephemeral, archived on {{host.skill-prefix}}handoff")`;
"promote" → `promote_checkpoint_to_item` per checkpoint (discover it first).

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
  (both deferred — discover them only in this branch, with the two
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

Next session: {{host.skill-prefix}}pickup
```

## Rules

{{shared.language}}
{{shared.consent}}
- Never modify sprint membership without the user's "Save" on a draft whose
  `Sprint:` line shows the change. The default is "unchanged" — a sprint spans
  sessions (ADR #57); a handoff designates the next slice, it does not re-plan
  by default.
- Terminal (resolved / wont_fix) members are the sprint's completion history:
  a replace leaves them in place and a remove of one is refused. Never try to
  "clean up" a sprint by dropping resolved items — that destroys
  `sprint_completion`.
- Never write a handoff before the user's "Save" on the draft the dialog
  showed (Step 2: the draft in the previews, then the one question).
- **Long text never goes into the question text or an option label or
  description** — they stay short and self-contained. The handoff prompt and
  the context_summary the user must read are previews of that dialog (pages
  when longer than 15 lines), word for word.
- `created_by` MUST be non-empty and truthful; the true client alone is sufficient. Add model/session/link only from the host and project/time only under the shared known-component rule below (`handoff_work` refuses blanks).
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


{{shared.identity}}

{{shared.read-schema}}


## CLI equivalent

`dct handoff create --prompt-file - --add-items 22,6 --scope g1 --context-summary-file summary.md` saves the approved prompt/context summary (`--remove-items` cuts, `--items` replaces; omit all three to keep the sprint). `--context-summary "…"` is the short inline alternative. Only one file argument may read stdin.
