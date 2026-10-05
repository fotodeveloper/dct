---
name: clog
description: >-
  [dct] Add a changelog entry to the dct database. Use when: "clog",
  "changelog", "log this change", or right after finishing any change
  ("just fixed/added X") — in any language.
---

# /clog — Add Changelog Entry

## Workflow

### Step 1: Gather from context
From the current work, determine:
- **project**: Auto-detect from CWD, or ask
- **category**: added, changed, fixed, removed (see mapping below)
- **entry**: Concise description of the change (English, ~1 sentence)
- **component**: Module/subsystem (optional)
- **source_file**: Main file changed (optional)
- **source_lines**: Line range in that file, e.g. "42-58" (optional)
- **related_item_id**: If this change resolves a tracked item (optional)
- **branch** / **worktree**: Read in YOUR current working directory:
  `git branch --show-current` (empty output = detached HEAD → omit `branch`)
  and `git rev-parse --show-toplevel`. Always pass them — the MCP server
  runs in the session start directory, which may be another checkout or
  worktree, so its own guess can name the wrong branch.

### Step 2: Confirm
```
Changelog entry:
  [added] New Python parser support (parser)
  File: cmd/parse.go
  Branch: feat/x (/tmp/proj-a)
```
Wait for confirmation.

### Step 3: Create
Call `mcp__dct__add_changelog(project, category, entry, branch, worktree, ...)`.
The ack echoes `branch` and `branch_source`; if `branch` is missing from the
ack, the MCP server is older than this skill — tell the user to restart it.
If the ack says `branch_source: "server"` (it then carries a `warning`), the
server filled in ITS OWN checkout, not yours — the entry is NOT confirmed.
Correct it right away against your actual checkout:
`mcp__dct__update_changelog(id, branch=<yours>, worktree=<yours>)` (both
fields; on a detached HEAD pass only `worktree`). `/commit` never attaches a
server-detected entry without that confirmation.

### Step 4: Report
```
✓ Changelog #15 [added] — New Python parser support
```

## Category mapping
| Change type | category |
|---|---|
| New feature, new file, new capability | added |
| Refactor, rename, restructure, update behavior | changed |
| Bug fix, crash fix, correction | fixed |
| Deleted code, removed feature, deprecated | removed |

## Conventional commit → category
| Prefix | category |
|---|---|
| feat: | added |
| fix: | fixed |
| refactor:, chore:, perf: | changed |
| revert: | removed |

## Multiple entries

If a commit touches multiple areas, create multiple changelog entries —
one per logical change. Example: refactor that also fixes a bug and adds
a helper:

```
✓ Changelog #20 [changed] — Extracted shared auth validation (core/auth)
✓ Changelog #21 [fixed]   — Token refresh race condition on concurrent login (core/auth)
✓ Changelog #22 [added]   — is_expired() helper on Session model (core/auth)
```

Each entry is a separate `add_changelog` call. Don't concatenate changes
into one entry — it makes the generated changelog.md harder to skim at
release time.
