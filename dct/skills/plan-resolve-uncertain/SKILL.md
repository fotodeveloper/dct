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

Present a compact table:
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

- Never silently accept LLM output without user approval at the strategy step.
- Cost-aware — report estimated LLM cost upfront before running batch.
- Graceful fallback if `claude` CLI missing — fall back to manual mode.
- Idempotent — re-running the skill on already-resolved sections is a no-op.
