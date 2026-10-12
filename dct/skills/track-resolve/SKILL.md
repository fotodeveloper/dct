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
Ask which item only when it is ambiguous (no id given and several open items
fit the description).

Call `mcp__dct__get_item(item_id)` for each id to fetch current state.

### Step 2: Pending gate → STOP

A pending **gate** checkpoint (`kind` other than `task` / `cc-task` —
`dogfood`, `security`, `approval`, `review`, or an unknown kind) blocks the
item: `resolve_item` refuses to close it (and so does `update_item` with a
terminal status). STOP before calling it — there is no "resolve anyway" path,
and no gate is ever rejected on your own initiative.

Tell the user in plain words — no tool names, no `kind` identifiers — what is
still to be checked, one line per gate, its text and number:

```
#42 can't be closed yet — it waits on a check: "try the new command in a fresh session" (#17). Once that check is really done, say "done"; to drop it on purpose, say "skip it". Then I close #42.
```

That answer is the instruction: "done" → `mcp__dct__complete_checkpoint(id)`,
"skip it" → the user's own decision to skip, `mcp__dct__reject_checkpoint(id)`
(ledger-preserved); then `mcp__dct__resolve_item(item_id, status, resolution)`
in the same turn, with no further question, once no gate is left pending.
A "done" or "skip it" covers only the gates named in the stop — with several
gates and an answer that does not say which, ask which. "done" is never a
substitute for the evidence a gate requires: when the gate's text names a
sign-off, a review or a fresh-session test that has not happened, the gate
stays pending and the item stays open.

Pending `task` checkpoints do not block: the item is resolved and the
acknowledgement names how many stay open.

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
✓ #42 → resolved (Fixed in commit abc1234) · to reopen: /track-update #42 reopen
```
One line per item, ENDING with how to reopen it, in words the user can type
back — never left out, the write had no question; add `· 2 task checkpoints
left open` when there are. A wrong resolve is corrected by reopening the
item: `/track-update #42 reopen` (`update_item(42, status="open")`, which
also clears `resolved_at`).

## Rules

- **Language.** A quoted question, option, screen or report fixes what it
  says, not its wording: write it in the language of the conversation. Keep
  unchanged: identifiers, commands, paths, file contents, commit messages,
  changelog entries, short titles and anything shown verbatim.
