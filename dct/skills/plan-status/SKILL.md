---
name: plan-status
description: |
  Cross-plan progress dashboard for a project. Shows per-plan done/total
  checkpoint counts, uncertain section count, and last-scanned timestamp.
  Use when: "plan-status", "plan progress", "pokaż plany", "co zostało
  w planach", "ile zrobione w planie X", "status planów", "how much of
  the plan is done", "stan planów".
---

# /plan-status — Cross-plan Progress Dashboard

## When to invoke

- User asks about progress across plans in the current project.
- Session start / handoff — show high-level plan health alongside sprint review.
- Before making planning decisions (pick a plan with most pending work, etc.).

## Workflow

### Step 1: Resolve project

Auto-detect from CWD via `mcp__dct__list_projects()` + longest-prefix match.
If not registered, ask via `AskUserQuestion`.

### Step 2: Fetch plans

`mcp__dct__list_plans(project=<slug>, status="active")` — returns `{cols, rows}`.

### Step 3: Enrich each plan with progress

For each plan row, call `mcp__dct__get_plan(plan_id)` to get the `progress`
sub-dict and sections needing review.

### Step 4: Render table

```
#   kind    status  progress  uncertain  last-scanned  title
──  ──────  ──────  ────────  ─────────  ────────────  ─────────────────────
12  plan    active  8/10      2          2026-04-17    Plans/roadmaps schema
17  spec    active  3/3       0          2026-04-15    …
23  adr     active  —         0          2026-04-10    ADR-001: Use ULIDs
```

`progress` format:
- Plans with checkpoints: `done/active` (rejected excluded).
- Plans with only sections and no checkpoints (specs, ADRs): show `—`.

`uncertain`: count of `plan_sections.needs_llm_review=TRUE`.

### Step 5: Optional drill-down

Ask via `AskUserQuestion`:
- "Show details for a specific plan?"
- Options: one per plan (`#12 — Plans/roadmaps schema`), or "Done".
- On selection: `mcp__dct__get_plan(id)` full dump + structured section tree.

## Rules

- Read-only — never modify plans / sections / checkpoints from this skill.
- Respect `format="structured"` default — pretty-print only at the final
  rendering step, not in the MCP calls themselves.
- When no plans exist, emit a one-liner (`"No plans registered in <project>"`)
  and stop — do not suggest creating one unprompted.

## Example dialog

> User: "/plan-status"
>
> Claude:
>   Project: dct (detected from CWD)
>   Plans: 3 active, 0 completed, 0 archived.
>
>   #   kind   status  progress  uncertain  last-scanned  title
>   ── ─────  ──────  ────────  ─────────  ────────────  ─────
>   12  plan   active  6/10      0          2026-04-17    v0.2.0 implementation
>   ...
>
>   Any plan to drill into?
