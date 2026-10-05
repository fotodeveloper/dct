---
name: track
description: >-
  [dct] Create a tracked item (issue / todo / feature / idea / improvement).
  Use when: "track this", "add issue/todo/feature", "bug", "idea", "refactor"
  — in any language. Type mapping lives in the create_item tool description.
---

# /track — Create Tracked Item

## Type triggers — natural language → item_type

Map the phrase the user actually used to the closest type. Do NOT ask
about type unless the phrase is genuinely ambiguous. When in doubt, prefer
`todo` (the most neutral bucket).

| User says (EN / PL) | item_type | When to pick |
|---|---|---|
| "problem", "bug", "issue", "crash", "broken", "error", "doesn't work", "regression" / "problem", "błąd", "bug", "crash", "nie działa", "popsute", "zepsute" | **issue** | Observable defect in existing code/system. Reproducible wrong behavior. |
| "todo", "task", "action item", "need to", "must do" / "todo", "zadanie", "zrobić", "do zrobienia", "trzeba" | **todo** | Concrete work that's neither a bug nor a new user-visible capability. Generic "stuff to do". |
| "feature", "add X", "new capability", "let's build", "we should have" / "feature", "ficzer", "funkcja", "dodajmy", "zbudujmy", "nowa funkcja" | **feature** | New user-visible capability that doesn't exist yet. |
| "idea", "maybe", "what if", "consider", "brainstorm", "explore" / "pomysł", "idea", "a może", "rozważ", "zastanów się", "pomyślmy" | **idea** | Exploratory — not yet decided to build. May become a feature later. |
| "improvement", "refactor", "polish", "cleanup", "tidy up", "make X nicer" / "ulepszenie", "poprawa", "poprawmy", "polerka", "cleanup", "refactor", "uporządkuj" | **improvement** | Existing thing works but could be better (refactor, UX polish, perf, docs). |

### Examples

| Phrase | item_type | Why |
|---|---|---|
| "dodaj jako problem że parser się wywala na pustych plikach" | issue | "problem" + observable broken behavior |
| "dodaj ficzer, chcę zrobić dark mode" | feature | "ficzer" + new capability |
| "a może pomyślmy nad wtyczkami?" | idea | "a może" + exploratory |
| "trzeba uporządkować ten moduł parsera" | improvement | "uporządkować" + existing code |
| "dodaj todo — wysłać PR do review" | todo | explicit "todo", generic task |

## Workflow

### Step 1: Infer type, then gather rest

From the conversation context, determine:

- **project**: Detect from CWD using `mcp__dct__list_projects()` (longest path prefix), or ask if ambiguous
- **item_type**: Infer from the user's phrasing per the table above
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

### Step 2: Confirm with user

Present the proposed item (short form — no wall of JSON):

```
Track this?
  Type:     issue
  Priority: P1
  Label:    Parser empty-file crash
  Title:    Parser crashes on empty files
  File:     cmd/parse.go:42-58
  Tags:     parser, crash
```

Show the tags as they will be STORED (normalized — trimmed and lowercased), so
the user confirms what actually lands in the dictionary.

Wait for confirmation or corrections. If user said "just add it" or similar,
skip confirmation.

### Step 3: Create

Call `mcp__dct__create_item(project, item_type, title, short_title, ...)` with
all fields.
Verify the ack echoes `short_title`. If it does not, the MCP server is older
than this skill — warn the user to restart the MCP server (or upgrade dct)
and set the label later with `update_item`; do not claim the label was saved.
**REMEMBER the returned `id`** — needed for any follow-up (add_checkpoint,
update_item, resolve_item).

### Step 4: Sketch checkpoints (feature / improvement only)

If `item_type` ∈ {feature, improvement}, IMMEDIATELY after create_item,
call `add_checkpoint` 3-7 times to break the work into concrete steps.
Do this BEFORE starting any code. Remember each checkpoint id.

Skip for `issue`, `todo`, `idea` unless the user explicitly asks for a
checkpoint breakdown.

### Step 5: Report

```
✓ #42 issue/P1 [Parser empty-file crash] — Parser crashes on empty files
```

For feature/improvement with checkpoints:
```
✓ #43 feature/P2 [Dark mode] — Dark mode
  Checkpoints: 5 (ready to track)
```

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

- **Never ask about type** if the phrase maps cleanly per the table. Claude's
  job is to read the language, not interrogate the user.
- **Never silently create** — always show the draft and wait for confirmation
  (exception: user explicitly waived with "just add it", "szybko", "don't ask").
- **REMEMBER the returned id** — it's the handle for every future operation.
- **Checkpoints are mandatory for feature/improvement** — the visible
  plan is the contract. No "I'll add them later" — add before coding.
