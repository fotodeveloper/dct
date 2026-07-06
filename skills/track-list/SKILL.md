---
name: track-list
description: |
  List and search tracked items across projects. Supports priority grouping,
  file hotspots, and cross-project aggregation.
  Use when: "list issues", "show todos", "what's open", "pokaż issues",
  "co mamy", "backlog", "track-list", "all items", "open issues".
---

# /track-list — List Tracked Items

Default: open items in the current project (auto-detected from CWD), grouped
by priority (P1 → P2 → P3 → future), compact `{cols, rows}` format. Resolved
items are hidden unless explicitly requested.

## Routing — direct vs subagent

Pick based on cost, not habit. Main context is precious.

**Run directly (main context)** when:
- Single project, ≤50 open items expected
- Single #ID lookup (`/track-list #42` → `mcp__dct__get_item(42)`)
- Simple filter (priority / type / component / in_sprint)

**Delegate to subagent** (`Agent` tool with `subagent_type=Explore` or `general-purpose`) when:
- **Cross-project aggregation** (no `project` filter, >1 registered project with >20 items each)
- **Hotspot detection** (needs to group by `source_file` across items — Python-side work, not SQL)
- **Cross-source matching** (e.g. "which items share a changelog entry? which are in the sprint AND have stale checkpoints?")
- Expected output >200 rows
- Long free-text search across large description bodies

Subagent returns a concise summary. You relay that summary, not the raw rows — keeps your context lean.

## Workflow

### Step 1: Parse user intent

Extract from the request:
- **project**: explicit slug, `--all`/"all"/"cross-project" → empty (cross-project), otherwise auto-detect from CWD via `mcp__dct__list_projects` longest-prefix match
- **status**: default `"open"`. Accepts `"in_progress"`, `"resolved"`, `"wont_fix"`, or `""` for all. Convenience aliases: `--all-statuses`, `--include-resolved`
- **item_type**: `issue`/`todo`/`feature`/`idea`/`improvement`, or empty
- **priority**: `P1`/`P2`/`P3`/`future`, or empty
- **component**: match exact component name
- **in_current_sprint**: `"true"` (only sprint), `"false"` (exclude sprint), or empty
- **search**: free-text, searched in `title` + `description`
- **limit**: default 50

ID normalization (from `#42`, `42`, `ITEM-42`, `item-42`, `ITEM_42`, `item_42`, `0042` → `42`): regex `^(#|ITEM-|item-|ITEM_|item_)?0*(\d+)$` — case-insensitive prefix strip, leading-zero strip, cast int. Uniform with /track-update and /track-resolve. If a single ID is given, skip listing and go to `mcp__dct__get_item(id)`.

### Step 2: Query

Call `mcp__dct__list_items(project, status, item_type, priority, component, in_current_sprint, search, limit, format="structured")`.

Response shape:
```
{"cols": ["id","item_type","priority","status","in_current_sprint","cp","component","tags","created_at","title","description"],
 "rows": [[22,"improvement","P2","open",false,[0,0],"server",["mcp"],"2026-04-17","Optimize...","..."]]}
```

### Step 3: Present — priority grouping

Group rows by priority (P1 → P2 → P3 → future). Within each group sort by `cp` progress descending (most-done first), then by `created_at` ascending (oldest first).

Render as markdown:

```
## P1 — blocking (2)
| #   | Type  | Sprint | cp    | Component | Title                         |
|-----|-------|--------|-------|-----------|-------------------------------|
| 42  | issue | ●      | 2/3   | parser    | Crash on empty files          |
| 88  | todo  |        | 0/0   | auth      | Rotate session tokens         |

## P2 — important (11)
...

## P3 — backlog (4)
...
```

Use `●` for `in_current_sprint=true`, blank for false. Use `cp` as `done/total` string (e.g. `2/5`) from the raw `[2,5]` array.

### Step 4: Hotspots (only if explicitly asked OR >1 hotspot auto-detected)

A **hotspot** is a file/component with ≥2 open items. Run Python-side aggregation on the rows (no extra DB call).

Trigger automatically when rows.length ≥ 20 AND at least one file has ≥2 items. The thresholds are empirical — below 20 items a hotspot section is noise (few rows, hotspots self-evident); above 20, the extra section reveals structural debt clusters. Otherwise show only on request (`/track-list --hotspots`).

```
## Hotspots (top 3 files by open-item count)
| Path                     | Open | P1 | Titles                              |
|--------------------------|------|----|-------------------------------------|
| dct/server.py            | 4    | 0  | #22, #6, #14, #20                   |
| skills/track-list/SKILL.md | 3  | 0  | #6, #14, #19                        |
| dct/core/items.py        | 2    | 0  | #4, #23 (resolved)                  |
```

If no source_file on items, skip this section.

### Step 5: Cross-project summary (only when no project filter)

When `project` is empty (cross-project mode), prepend a summary line:

```
Cross-project: 37 open items in 4 projects (dct: 13, proj-a: 87, proj-b: 63, proj-c: 35)
```

Then show **top 3 priorities cross-project**, skipping detailed rows (delegate full listings to subagent if user asks).

### Step 6: Final summary line

Always end with a count:
```
Showing 13 open items in dct. Use /track-list --include-resolved to show closed.
```

## Shortcuts

- `/track-list` — open items in current project
- `/track-list #42` — direct `get_item(42)` (skips list)
- `/track-list proj-a` — items in project `proj-a`
- `/track-list --all` — cross-project (delegate to subagent if >100 expected)
- `/track-list P1` — P1-only filter
- `/track-list parser` — search "parser" in title/description
- `/track-list --sprint` — only `in_current_sprint=true`
- `/track-list --hotspots` — force hotspot section
- `/track-list --include-resolved` — include resolved + wont_fix

## Rules

- **Always parse with ID normalization regex**. Do not reject `#42` because of the hash sign.
- **Default hides resolved** — do not surprise the user with closed items unless they ask.
- **Prefer subagent for cross-project** when ≥2 projects have ≥20 open items each. Tell the user: "Cross-project aggregation — delegating to subagent".
- **`cp` column is authoritative** — do not re-query per-item to compute progress (N+1).
- **Don't show `description` in the table** (it's in cols but too long for a grid row). Keep it available for follow-up `get_item` calls. Render only `title` in the grid.
