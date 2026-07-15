---
name: track-resolve
description: >-
  [dct] Resolve or close a tracked item. Use when: "resolve issue",
  "close item", "done with #42", "wont fix" — in any language.
---

# /track-resolve — Resolve/Close Item

## Workflow

### Step 1: Identify item(s)

**ID normalization** — accept all of these forms:
- `#42`, `42`, `ITEM-42`, `item-42`, `0042`

Parse by: strip leading `#`, strip `ITEM-` / `item-` prefix (case-insensitive), strip leading zeros, cast to integer.
Regex: `^(#|ITEM-|item-|ITEM_|item_)?0*(\d+)$` — capture group 2 is the numeric id.

**Batch resolution** — user may pass multiple IDs separated by whitespace or commas:
```
/track-resolve #42 #45 #51          ← three items in one call
/track-resolve 42, 45, 51            ← comma form
/track-resolve ITEM-42 #45           ← mixed forms
```
Split input on whitespace and commas, normalize each id, then call `resolve_item` once per id.

Call `mcp__dct__get_item(item_id)` for each id to fetch current state.

### Step 2: Determine resolution
- **resolved**: Fixed, implemented, completed
- **wont_fix**: Intentionally not addressing

Build resolution note from context:
- If a commit was just made: "Fixed in commit {hash}"
- If decision: "Decision: {reason}"
- Ask user if context is insufficient

### Step 3: Confirm
```
Resolve #42 — Parser crashes on empty files?
  Status:     open → resolved
  Resolution: Fixed in commit abc1234
```

### Step 4: Execute
Call `mcp__dct__resolve_item(item_id, status, resolution)`.

### Step 5: Report
```
✓ #42 → resolved
```
