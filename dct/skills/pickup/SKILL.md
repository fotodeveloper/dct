---
name: pickup
description: >-
  [dct] Start-of-session workflow — read last handoff, show current sprint,
  propose first action. Use when: "pickup", "resume", "where were we",
  "what’s next", "back to work" — in any language.
---

# /pickup — Session Resume

Open a new session by loading the previous session's handoff prompt + sprint
state in ONE composed call, then start on the handoff's next step — or show
the handoff inside a dialog and ask, when there is something to decide.

No ToolSearch preload needed for the main path: `pickup_work` (and
`get_item`) are alwaysLoad residents. `get_handoff` and `list_handoffs` are
deferred — load them lazily, only in the several-handoffs branch below.

## Workflow

### Step 1: One composed call (CWD auto-detect)

What the user said decides whether a handoff is consumed:

- **An instruction to continue** — on an unambiguous "continue" / "pickup" /
  "resume" (or `/pickup` itself) the call consumes the single matching
  handoff (AUTO mode). Treating "resume" like "continue" is a proposed
  refinement of the owner's decision, which named only "continue" — say so if
  the user asks why it was consumed.
- **A status question** — "where were we", "what's next", "what's the state"
  → `pickup_work(project="", consume="false")`, a PURE READ, no `consumed_by`
  needed: report the state and say that a handoff is waiting and that
  "continue" picks it up. A request to only look ("show the handoff but keep
  it", an audit) is the same read.
- A word the user merely quotes or mentions ("what does "continue" do?") is
  not a command; only a request addressed to you counts.

```
mcp__dct__pickup_work(project="", consume="",
                      consumed_by="claude-<model_id> @ <project_slug> <iso_utc>Z")
```

`consume=""` is the server's AUTO mode. With exactly ONE matching unconsumed
handoff it consumes that handoff in the same transaction, without a question —
an unambiguous "continue" / "pickup" already says to pick it up — and returns
it as `picked_up` with its FULL prompt (`consumed: true`). With several
matching handoffs it consumes nothing and returns them in the `handoff`
section (Step 2 asks which); with none, nothing is written. When the user
names a workstream, pass `scope=<tag>` so only its handoffs match.

One call returns everything Step 2 needs:

- **`picked_up`** — the consumed handoff row with its FULL prompt (and
  `context_summary`), or `null` when nothing was consumed.
- **`handoff.latest`** — the newest matching handoff still unconsumed after
  the call, with its FULL prompt; `handoff.others` carries 200-char previews
  of every OTHER unconsumed handoff — of any scope, capped (`truncated:
  true` when rows were left out).
- **How many match** — with a scope, `handoff.scope_matched` (only this
  workstream's unconsumed handoffs); without a scope, the global
  `pending_count` of the `handoff` section (all of them). Never branch on `pending_count` when a scope was given.
- **`sprint`** — ALL active sprints (`active_sprints`, `primary_sprint_id`),
  member items with `cp`, their `checkpoints` (the next pending checkpoint is
  readable directly), `stale_in_progress`.
- **`gates`** — pending gate checkpoints of in-sprint items; **`decisions`** —
  live ADRs/open questions with previews; **`summary`** — sprint_size,
  sprint_completion, handoffs_pending, gates_pending.

**Fallback only when project is unresolved** (CWD not registered →
`pickup_work` errors): call `mcp__dct__list_projects()` and ask via
`AskUserQuestion` ("Which project are we working on?" → slugs + "Cancel"),
then re-call `pickup_work(<slug>, ...)` the same way.

### Step 2: Start, or show the handoff inside the dialog

A handoff the user must read before deciding travels INSIDE the dialog
(Rules: "The screen travels inside the dialog"): text printed before a dialog
can land in the thinking block, which the user sees only as a one-line
summary.

- **The auto-start path — no question follows** (one handoff consumed on an
  unambiguous "continue", Step 4 below): the user already read and saved this
  handoff at /handoff. Check its next step (`get_item`, the pending `gates`)
  and start on it in the same turn; the Step 5 report names the handoff and
  the step. "Show the handoff" prints it as the final text of a turn. When the
  check finds the step stale, contradictory or unclear, Step 4's question
  carries the handoff in its previews.
- **The user must decide** (several matching handoffs, Step 3): each handoff
  is the preview of its option.

A handoff as a screen — header line, then the prompt verbatim, one line per
paragraph (the dialog wraps it), then its `context_summary` when present:

```
Handoff #N [scope: X] from <created_at> by <created_by> — consumed
--- BEGIN HANDOFF PROMPT ---
<the full prompt text, verbatim>
--- END HANDOFF PROMPT ---
```

**Several matching handoffs** (nothing consumed; the count from Step 1 ≥ 2):
offer only the matching ones — with a scope, only handoffs of that scope,
never another workstream's. When `handoff.others` is truncated, or holds
fewer matching rows than the count says, the list is not complete: fetch it
first with `mcp__dct__list_handoffs(project="")` (unconsumed, previews; keep
only the matching scope). `handoff.latest` carries the full prompt; read the
full prompt of each other handoff you offer with `mcp__dct__get_handoff(M)` —
a read, without consuming it.

### Step 3: Several handoffs — pick one after reading it

**ONE `AskUserQuestion`, only "which handoff"** (never together with the
first action — that is decided only after the chosen handoff was read):
- question: "Which handoff do we continue? (N waiting)"
- options: "Continue #N" for each matching handoff, newest first, at most 3 —
  each with that handoff's screen as its preview; "None — start fresh". With
  more than 3, the question names the rest by id ("also #12, #9 — name one
  under Other").

A choice made from a text nobody could read is no consent to run its plan:

- **"Continue #N" on a handoff whose whole screen fit in its preview** (at
  most 15 lines) — an unambiguous instruction to continue a handoff the user
  has read in full → only now consume it:
  ```
  mcp__dct__pickup_work(project="", consume="true", handoff_id=N,
                        include="none",
                        consumed_by="claude-<model_id> @ <project_slug> <iso_utc>Z")
  ```
  `include="none"` keeps the consume cheap (Step 1 already delivered the
  state). Then Step 4.
- **"Continue #M" on a longer handoff** (its preview showed only page 1), or
  "show the older one" → nothing is consumed yet: ask ONE question
  "Continue handoff #M?" → "Continue #M" (preview: page 1) / "Back to the
  list" / "None — start fresh", with every page on its "Details" pages.
  Showing it stays a read; "Continue #M" here consumes it as above.
- **"None — start fresh"** → consume nothing; optionally `delete_handoff(id)`
  (deferred tool — load only if the user explicitly wants a discard).

### Step 4: The first step — start it, or ask

After a handoff was consumed on an instruction to continue, read the next step
it names: an item id or a pending checkpoint. Check it against the CURRENT
state before acting — `mcp__dct__get_item(X)` (its status, its checkpoints)
and the pending `gates` of the snapshot.

- **The step is concrete and still current** (the item is open or
  in_progress, the checkpoint pending, no gate in its way, and nothing the
  user said in this session points elsewhere) → start on it at once, without
  a question, and say so: `Starting with #22 — "<next pending checkpoint>";
  tell me if you want something else.`
- **The step is stale, contradictory or unclear** (the item is resolved, the
  checkpoint done, a gate blocks it, two handoffs disagree, the user asked for
  something else, or no step is named) → ask the first action — ONE
  `AskUserQuestion`, the consumed handoff's screen as the preview of its
  first option (pages when longer):
  - question: "First action for this session?"
  - options: "Open #<first-sprint-item> and start on its checkpoints" /
    "Review all sprint items first" / "Look at stale in_progress first (#X)" /
    "Something else — tell me what".

An old handoff is no new consent: a commit, a push, publishing a release or
any destructive step written in its text still waits for the user's own
instruction in this session (and for the consent the skill that performs it
asks for). The handoff names the work, not a permission — and it never
overrides what the user asks for now.

With no handoff at all (or "None — start fresh"), ask the first action the
same way. **Empty-sprint variants** — replace its options when the sprint is
empty:
- Sprint empty BUT open items exist: "Pick sprint items now — run /handoff" / "Browse open items — /track-list" / "Just start working — I'll tell you what".
- Sprint empty AND no open items: "Create the first item — run /track" / "Import existing tracking data — run /dct-import" / "Exit — not the right project".

The Step 5 report carries the sprint items as a compact table (from
`sprint.items`) plus summary: sprint_size, sprint_completion,
handoffs_pending, and the opened item's pending checkpoints (from
`sprint.checkpoints`) as the next-step plan.

### Step 5: Report

```
✓ Picked up handoff #N (consumed) — "<first sentence of its prompt>"
  Sprint: 3 items, 1/3 done
  Starting with #22 — "Optimize MCP response format"
  Plan: 4 pending checkpoints in this item
```

The first line names the consumed handoff by its id (`#N`): the consume
happened without a question, so this is where the user sees it. Consuming only
marks the handoff read — the row and its prompt stay in the ledger. Nothing
consumed → `✓ No handoff consumed` (or `left unconsumed` after a read-only
look).

If the project has plans with pending checkpoints, mention that
**`/plan-status` shows overall plan progress** — a good next call when planned
work is in flight.

## Rules

- **Language.** A quoted question, option, screen or report fixes what it
  says, not its wording: write it in the language of the conversation. Keep
  unchanged: identifiers, commands, paths, file contents, commit messages,
  changelog entries, short titles and anything shown verbatim.
- **The screen travels inside the dialog.** What the owner must read before
  answering is the preview of the option it belongs to, at most 15 lines as
  the dialog shows them. The dialog wraps long lines itself, about 60
  characters wide: a paragraph is one line, never broken by hand (a second
  break leaves single words on a line), and a list or table line stays under
  60 characters. Message text written before a dialog is never the screen: it
  can land in the thinking block, which the owner sees only as a one-line
  summary, so no question or option says "shown above". A longer screen is
  split into pages of at most 15 lines: the option's preview shows page 1, and
  one more single-select question, "Details", has one option per page (2 to
  4), each with that page as its preview; its answer is not a decision.
- **Every decision is an `AskUserQuestion`.** No plain-text Y/N prompts.
- **Long text never goes into the question text or an option label or
  description** — they stay short and self-contained. A handoff prompt or
  context_summary the user must read before deciding is a preview of that
  dialog (pages when longer than 15 lines), word for word. With no dialog
  (the auto-start path of Step 4) the work starts at once and the report
  names the handoff.
- **Two consumes, nothing else.** The Step 1 AUTO consume of the single
  matching handoff on an instruction to continue, and `consume="true",
  handoff_id=N` for the handoff the user chose to continue after reading it in
  full. A status question or a request to only look is `consume="false"`.
  Never auto-pick among several handoffs, and never consume one that was only
  previewed.
- Race note: a parallel session may consume the same handoff first. The AUTO
  consume then degrades to `consumed: false` (no error) — proceed with the
  fresh snapshot. An explicit `handoff_id` instead errors "not found or
  already consumed": report it verbatim, keep the plan you already read, and
  do NOT retry in a loop.
- On empty sprint + no handoffs, don't force-create a sprint — ask user what to do.
- Show resolved items (they surface via `summary.sprint_completion` and
  `recent_changes`) only as context, not as work candidates.
- `consumed_by` MUST mirror /handoff's `created_by` format:
  `claude-<model_id> @ <project_slug> <iso_utc>Z` — required on every call
  that may consume, unnecessary on a `consume="false"` read.
- Round-trip budget: 1 dct call — the Step 1 call, with `get_item` batched
  after it. The several-handoffs branch adds what it needs: `list_handoffs`
  only for an incomplete list, `get_handoff` for each handoff shown in full,
  and the Step 3 consume. `review_sprint` is never needed here.
