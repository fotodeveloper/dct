# {{host.skill-prefix}}track — Create Tracked Item

{{shared.type-cues}}

## Workflow

### Step 1: Infer the fields

From the conversation context, determine:

- **project**: Detect from CWD using `mcp__dct__list_projects()` (longest path
  prefix); ask only when the project is ambiguous (CWD not registered, or
  several projects fit)
- **item_type**: Infer from the user's phrasing per the table above; no type
  cue → `todo` without a question; ask about the type only when the phrase
  carries cues for two different types and the choice changes the scope
- **title**: Concise summary (max ~80 chars)
- **short_title**: Label — a 2-4 word English noun phrase naming what the item
  is about (≤ 50 chars; no item number, no project name), e.g. "Windows hook
  paths". Always set it; the server refuses a longer one
- **priority**: P1 (blocks work / data loss / security), P2 (important — default), P3 (backlog), future (post-v1)
- **description**: Details, context, steps to reproduce (for issues) — prose, not checkbox lists
- **component**: Module or subsystem (optional)
- **source_file**: Relative path where the item lives (optional)
- **source_lines**: Line range, e.g. "42-58" (optional)
- **tags**: List of labels (optional) — a JSON array, e.g. `["parser","crash"]`.
  Tags are dictionary-backed and **global** (shared by every project): names are
  normalized (trimmed, lowercased) and unknown ones auto-register. A name too
  SIMILAR to an existing tag is REFUSED with a suggestion in the error
  (`Tag 'test' is similar to existing 'tests' — reuse it, or call
  add_tag(name='test', force=true) to create anyway`). Reuse the suggested tag,
  or — when the near-duplicate is genuinely wanted — call
  `mcp__dct__add_tag(name=..., force=true)` first, then retry the create. The
  refusal rolls the whole create back, so no orphan item is left behind.

### Step 2: Create — no confirmation question

First search existing items in the resolved project for the same reported work (`list_items` with relevant search/status filters); inspect a plausible match before creating a duplicate. A request to record an idea records it; it does not start implementation. Mere mention without a tracking instruction only suggests this workflow.

An unambiguous instruction ("track this bug", "dodaj ficzer: dark mode")
creates the item without a confirmation question: call
`mcp__dct__create_item(project, item_type, title, short_title, ...)` at once
with all fields. A wrong field is cheap to fix afterwards, so the
acknowledgement (Step 3) is the check, not a question before the write.

Verify the ack echoes `short_title` (the stored label; null when none). A
MISSING `short_title` key means the MCP server's contract is older than this
skill: report an unsupported (outdated) server contract and ask the user to
restart the MCP server (or upgrade dct). Do NOT retry `create_item` — the item
was created; a retry would duplicate it — and do not claim the label was
saved.
**REMEMBER the returned `id`** — needed for any follow-up (update_item,
resolve_item, add_checkpoint).

{{shared.creation-checkpoints}}

### Step 3: Acknowledge in one line

```
✓ #42 issue/P1 [Parser empty-file crash] — Parser crashes on empty files · to change it: {{host.skill-prefix}}track-update #42 · to drop it: "#42 won't fix"
```

One line: what was created (id, type, priority, label, title; tags as
STORED — trimmed and lowercased — when any were set) — and it ENDS with how
to correct it, in words the user can type back; never left out, the write
had no question. To correct a field, `{{host.skill-prefix}}track-update #42` (`update_item`); an
item that should not exist is closed with `{{host.skill-prefix}}track-resolve` as `wont_fix`.

## Priority mapping

P2 is the fallback ONLY when the user gives no priority cue. Read their
language first — urgency words trump the default.

| User phrasing | priority |
|---|---|
| "blocker", "blokuje", "data loss", "security", "critical", "pilne", "urgent", "krytyczne", "ważne" | **P1** |
| No urgency cue, neutral report | P2 (default) |
| "kiedyś", "backlog", "eventually", "nice to have", "low prio", "niski priorytet" | **P3** |
| "post-v1", "long-term", "someday", "może w przyszłości", "future" | **future** |

Never silently default to P2 when the user said "ważne" or "pilne" — that
would bury a P1 in the backlog.

## Rules

Reject an obsolete task checkpoint within the agreed scope; a gate only on the user's decision (full gate policy: `add_checkpoint`). Never reject a gate independently to close the item.

{{shared.language}}
- **Never ask about type** if the phrase maps cleanly per the table, or
  carries no type cue at all (→ `todo`). The assistant's job is to read the
  language, not interrogate the user.
- **No confirmation question on an unambiguous instruction** — create, then
  acknowledge in one line with how to correct it. A question stays only for
  what the skill genuinely cannot tell: a type between two cued types that
  changes the scope, or the project.
- **REMEMBER the returned id** — it's the handle for every future operation.


{{shared.consent}}


{{shared.discovery}}
