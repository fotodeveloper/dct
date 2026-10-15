# {{host.skill-prefix}}track-list — List Tracked Items

Default: open items in the current project (auto-detected from CWD), grouped
by priority (P1 → P2 → P3 → future), compact `{cols, rows}` format. Resolved
items are hidden unless explicitly requested.

## Optional execution delegation

{{host.delegation}}

A single project/filter or id lookup runs directly. Large cross-project aggregation/hotspot or cross-source analysis may benefit from authorized delegation; the same explicit project filters and read contract apply. Return a concise summary with counts/limits rather than dumping rows.

## Workflow

### Step 1: Parse user intent

Extract from the request:
- **project**: explicit slug; `--all`/"all"/"cross-project" → enumerate registered projects and query each explicit slug; otherwise auto-detect from CWD via `mcp__dct__list_projects` longest-prefix match
- **status**: default `"open"`. Accepts `"in_progress"`, `"resolved"`, `"wont_fix"`, or `""` for all. Convenience aliases: `--all-statuses`, `--include-resolved`
- **item_type**: `issue`/`todo`/`feature`/`idea`/`improvement`, or empty
- **priority**: `P1`/`P2`/`P3`/`future`, or empty
- **component**: match exact component name
- **in_current_sprint**: `"true"` (only sprint), `"false"` (exclude sprint), or empty
- **search**: free-text, searched in `title` + `description` (server-side — a brief row may match on a description it does not show)
- **limit**: default 50

ID normalization (from `#42`, `42`, `ITEM-42`, `item-42`, `ITEM_42`, `item_42`, `0042` → `42`): regex `^(#|ITEM-|item-|ITEM_|item_)?0*(\d+)$` — case-insensitive prefix strip, leading-zero strip, cast int. Uniform with {{host.skill-prefix}}track-update and {{host.skill-prefix}}track-resolve. If a single ID is given, skip listing and go to `mcp__dct__get_item(id)`.

### Step 2: Query

An empty `project` means CWD auto-detection; from a registered checkout it is NOT cross-project. For an explicit cross-project request, call `list_projects()` and `list_items(project=<each slug>, ...)`, retaining project attribution while merging. A missing project on an ordinary request requires a choice, not silent cross-project fallback. Respect each list limit and disclose when the result is capped; do not claim a capped row count is the total.

Call `mcp__dct__list_items(project, status, item_type, priority, component, in_current_sprint, search, tag, limit, format="structured")`. `view` defaults to `"brief"` (no `description`); pass `view="full"` only when the task genuinely needs descriptions.

`tag` narrows to items carrying that tag (server-side; the dictionary is global, the listing stays project-scoped). An unknown tag returns `{"error": ...}` naming the closest known tags (up to 3, plus a count of the rest and a pointer to `list_tags`) — surface it, do not silently fall back to an unfiltered list.

Response shape (brief view):
```
{"cols": ["id","item_type","priority","status","in_current_sprint","cp","component","tags","created_at","short_title","title"],
 "rows": [[22,"improvement","P2","open",false,[0,0],"server",["mcp"],"2026-04-17","List payload size","Optimize..."]]}
```

### Step 3: Present — priority grouping

Group rows by priority (P1 → P2 → P3 → future). Within each group sort by `cp` progress descending (most-done first), then by `created_at` ascending (oldest first).

Render as markdown:

```
## P1 — blocking (2)
| #   | Type  | Sprint | cp    | Component | Label                   | Title                         |
|-----|-------|--------|-------|-----------|-------------------------|-------------------------------|
| 42  | issue | ●      | 2/3   | parser    | Parser empty-file crash | Crash on empty files          |
| 88  | todo  |        | 0/0   | auth      | —                       | Rotate session tokens         |

## P2 — important (11)
...

## P3 — backlog (4)
...
```

Use `●` for `in_current_sprint=true`, blank for false. Use `cp` as `done/total` string (e.g. `2/5`) from the raw `[2,5]` array. Label is `short_title`; render `—` when it is null.

### Step 4: Hotspots (when requested or available component rows justify it)

A **hotspot** is a file/component with ≥2 open items. Aggregate component hotspots from the brief rows. File hotspots need `source_file`, absent from brief structured rows: fetch `list_items(..., format="dict", view="full")` only when file-level analysis was requested, or use deliberate `get_item` reads for a small selected set. Do not manufacture file hotspots from missing fields.

Component analysis may trigger when rows.length ≥ 20 AND at least one component has ≥2 items. File-level analysis is explicit. The thresholds are empirical — below 20 items a hotspot section is noise (few rows, hotspots self-evident); above 20, the extra section reveals structural debt clusters. Otherwise show only on request (`{{host.skill-prefix}}track-list --hotspots`).

```
## Hotspots (top 3 files by open-item count)
| Path                     | Open | P1 | Titles                              |
|--------------------------|------|----|-------------------------------------|
| dct/mcp/server.py            | 4    | 0  | #22, #6, #14, #20                   |
| dct/skills/track-list/SKILL.md | 3 | 0 | #6, #14, #19                      |
| dct/core/items.py        | 2    | 0  | #4, #23 (resolved)                  |
```

If no source_file on items, skip this section.

### Step 5: Cross-project summary (only when no project filter)

After merging the explicit per-project queries for a cross-project request, prepend a summary line:

```
Cross-project: 37 open items in 4 projects (proj-a: 13, proj-b: 8, proj-c: 9, proj-d: 7)
```

Then show **top 3 priorities cross-project**, skipping detailed rows (use authorized delegation for full listings when supported).

### Step 6: Final summary line

Always end with a count:
```
Showing 13 open items in dct. Use {{host.skill-prefix}}track-list --include-resolved to show closed.
```

## Shortcuts

- `{{host.skill-prefix}}track-list` — open items in current project
- `{{host.skill-prefix}}track-list #42` — direct `get_item(42)` (skips list)
- `{{host.skill-prefix}}track-list proj-a` — items in project `proj-a`
- `{{host.skill-prefix}}track-list --all` — cross-project, explicit query per registered project
- `{{host.skill-prefix}}track-list P1` — P1-only filter
- `{{host.skill-prefix}}track-list parser` — search "parser" in title/description
- `{{host.skill-prefix}}track-list --sprint` — only `in_current_sprint=true`
- `{{host.skill-prefix}}track-list --hotspots` — force hotspot section
- `{{host.skill-prefix}}track-list --include-resolved` — include resolved + wont_fix

## Rules

{{shared.language}}
- **Always parse with ID normalization regex**. Do not reject `#42` because of the hash sign.
- **Default hides resolved** — do not surprise the user with closed items unless they ask.
- Delegate only when authorized and available; otherwise perform the same explicit project reads directly.
- **`cp` column is authoritative** — do not re-query per-item to compute progress (N+1).
- **Descriptions are not in the default listing** (brief view). Fetch them with `get_item(id)` or `view="full"` only when the task needs them; never render `description` in the grid.


{{shared.consent}}


{{shared.discovery}}
