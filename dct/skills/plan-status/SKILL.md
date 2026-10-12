---
name: plan-status
description: >-
  [dct] Cross-plan progress dashboard — per-plan done/total checkpoints,
  uncertain sections, last scan. Use when: "plan status", "plan progress",
  "how far along is the plan", "what’s left in the plans", or the user asks
  about overall progress of planned work — in any language.
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

The table is the final text of the turn — a report with no decision attached,
so no dialog follows it: text written before a dialog can land in the
thinking block, which the user sees only as a one-line summary. End it with one line: "Name a plan to see its sections." When the
user names one: `mcp__dct__get_plan(id)` full dump + structured section
tree.

## Rules

- **Language.** A quoted question, option, screen or report fixes what it
  says, not its wording: write it in the language of the conversation. Keep
  unchanged: identifiers, commands, paths, file contents, commit messages,
  changelog entries, short titles and anything shown verbatim.
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
>   Name a plan to see its sections.
