---
name: plan-resolve-uncertain
disable-model-invocation: true
description: |
  [dct] Batch-resolve plan sections flagged `section_type='uncertain'` during
  import. Offers LLM classification (via `claude --print`) per section or
  manual override. Use when: "plan-resolve-uncertain", "resolve uncertain",
  "sklasyfikuj niejasne", "check uncertain sections", "fix section types",
  "uncertain sections", "classify plan sections".
---

# /plan-resolve-uncertain — Classify Uncertain Plan Sections

## When to invoke

- After `/dct-import` reported `uncertain_remaining > 0`.
- `/pickup` surfaces uncertain sections and user accepts the follow-up.
- User explicitly asks to review / fix uncertain classifications.

## Workflow

### Step 1: Resolve project

Auto-detect from CWD. Confirm via `AskUserQuestion` if ambiguous.

### Step 2: List uncertain sections

Use `mcp__dct__list_plan_sections(plan_id=X, ...)` with `section_type="uncertain"`
for each active plan, OR construct a direct query via the CLI
(`dct plan resolve-uncertain --dry-run` if available).

Build a compact table — it travels inside the Step 3 dialog, as the preview
of each strategy option, on "Details" pages when longer than 15 lines
(Rules: "The screen travels inside the dialog"):
```
plan               depth  heading
─────────────────  ─────  ─────────────────────────────────────
v0.2-impl          2      Phase 0 — Shared foundations
v0.2-impl          3      Open questions for v0.2.1
roadmap            2      Amendments to Spec
```

### Step 3: Strategy choice

`AskUserQuestion`:
- "N uncertain sections. How to resolve?"
- Options:
  - "Auto-classify all via Claude Haiku (≈ $0.001/each)" — default if user is OK with LLM cost
  - "Review one-by-one (manual decision per section)"
  - "Mark all as 'prose' (safe default for narrative sections)"
  - "Skip — leave as uncertain"

### Step 4a: Auto-classify

For each section:
- Call the classifier indirectly via `dct plan rescan <file> --resolve-uncertain`
  OR invoke `classify_uncertain()` equivalent.
- On result: update via `mcp__dct__add_plan_section` isn't right (section exists);
  use a targeted update path: an MCP helper not yet exposed — fall back to
  manual `dct plan set-section-type <ulid> <type>` CLI if present.
- Aggregate: how many resolved, how many still uncertain.

### Step 4b: Review one-by-one

Per section, `AskUserQuestion`:
- Question: "Section `<heading>` — what type?"
- Options: sprint, phase, task, block, milestone, decision, prose, changelog,
  adr_section, reference, generic, (skip).
- Apply choice.

### Step 4c: Mark all as prose

Blanket update — quick escape hatch when user has read through them and
concluded they are all narrative text.

### Step 5: Report

`"Resolved N, remaining M uncertain. Review next session via /plan-status."`

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
- Never silently accept LLM output without user approval at the strategy step.
- Cost-aware — report estimated LLM cost upfront before running batch.
- Graceful fallback if `claude` CLI missing — fall back to manual mode.
- Idempotent — re-running the skill on already-resolved sections is a no-op.
