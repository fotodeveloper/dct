# {{host.skill-prefix}}track-resolve — Resolve/Close Item

## Workflow

### Step 1: Identify item(s)

**ID normalization** — accept all of these forms:
- `#42`, `42`, `ITEM-42`, `item-42`, `0042`

Parse by: strip leading `#`, strip `ITEM-` / `item-` prefix (case-insensitive), strip leading zeros, cast to integer.
Regex: `^(#|ITEM-|item-|ITEM_|item_)?0*(\d+)$` — capture group 2 is the numeric id.

**Batch resolution** — user may pass multiple IDs separated by whitespace or commas:
```
{{host.skill-prefix}}track-resolve #42 #45 #51          ← three items in one call
{{host.skill-prefix}}track-resolve 42, 45, 51            ← comma form
{{host.skill-prefix}}track-resolve ITEM-42 #45           ← mixed forms
```
Split input on whitespace and commas, normalize each id, then call `resolve_item` once per id.
Ask which item only when it is ambiguous (no id given and several open items
fit the description).

Call `mcp__dct__get_item(item_id)` for each id to fetch current state.

### Step 2: Pending gates — complete what is proven, STOP on the rest

{{shared.closing-gates}}

### Step 3: Determine resolution
- **resolved**: Fixed, implemented, completed
- **wont_fix**: Intentionally not addressing

Build resolution note from context:
- If a commit was just made: "Fixed in commit {hash}"
- If decision: "Decision: {reason}"
- Ask only when the status or the reason is genuinely unclear

### Step 4: Execute — no confirmation question
An unambiguous instruction ("close #42", "#42 is done", "wont fix #45 —
out of scope") resolves without a confirmation question: call
`mcp__dct__resolve_item(item_id, status, resolution)` at once.

### Step 5: Acknowledge in one line
```
✓ #42 → resolved (Fixed in commit abc1234) · to reopen: {{host.skill-prefix}}track-update #42 reopen
```
One line per item, ENDING with how to reopen it, in words the user can type
back — never left out, the write had no question; add `· 2 task checkpoints
left open` when there are. A wrong resolve is corrected by reopening the
item: `{{host.skill-prefix}}track-update #42 reopen` (`update_item(42, status="open")`, which
also clears `resolved_at`).

## Rules

{{shared.language}}

{{shared.consent}}


{{shared.discovery}}
