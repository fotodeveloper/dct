---
name: dct-import
disable-model-invocation: true
description: |
  [dct] Import existing tracking data from markdown files into dct. Handles checkbox
  TODOs, narrative sprints, custom issue formats (ORCH-N / BUG-N with field
  blocks), plan-as-parent documents (1k+ line planning docs), Keep-a-Changelog
  with or without [Unreleased], and conventional-commit style entries.
  Use when: "dct-import", "importuj", "zaimportuj todos", "migruj dane",
  "import tracking data", "import from markdown", "migrate to dct", "zmigruj",
  "przenieś", "załaduj istniejące todo", "convert roadmap to dct",
  "parse sprint doc", "import issue file".
---

# /dct-import — Import Existing Tracking Data

## Two import paths (v0.2.0)

Markdown files fall into three buckets; each uses a different tool:

| Bucket       | Files                                                       | Tool                                     |
| ------------ | ----------------------------------------------------------- | ---------------------------------------- |
| **Plan-like**| plans, specs, ADRs, roadmaps, retros, backlogs              | `mcp__dct__ingest_plan_file` (v0.2 pipeline) |
| **Issue-like**| ORCH-N / BUG-N custom-field files, TODOS with inline status | v0.1 items + checkpoints (see Pattern Catalog below) |
| **Changelog**| CHANGELOG.md, changelog.md, changelog_unreleased.md         | `mcp__dct__add_changelog`                |

The v0.2 pipeline handles ULID injection, `[x]` historical state, and
optional LLM classification of uncertain sections in one call — you don't
re-implement Pattern Catalog logic for plan-like files.

## Key principle

Claude proposes, user decides via `AskUserQuestion`. Never auto-create
without confirmation. Ambiguous entries are flagged and asked one-by-one.

## Workflow

### Step 1: Detect candidate files

Scan project root (and common subdirs) for typical tracking files. Apply
the stale-file skip list FIRST (see below) — do not even offer skipped
paths to the user.

Typical patterns (file name heuristic):
- **Plan-like** (→ `ingest_plan_file`):
  - `docs/plans/*.md`, `docs/specs/*.md`, `docs/adr/*.md`, `docs/retros/*.md`
  - `ROADMAP.md`, `TODOS.md`, `FUTURE.md`, `BACKLOG.md`
  - `SPRINT*.md`, `PLAN*.md`
- **Issue-like** (→ v0.1 items path):
  - `ISSUES.md`, `BUGS.md`, `*issues*.md`, `*orchestrator*.md`
- **Changelog** (→ `add_changelog`):
  - `CHANGELOG.md`, `changelog.md`, `changelog_unreleased.md`

### Step 2: Classify each file by format

For each candidate, read the first ~60 lines and classify by content.
The Pattern Catalog below lists 10 known formats. A file may match multiple
(e.g. a plan with embedded checkboxes) — pick the dominant one, flag as
composite in Step 4.

### Step 3: Parse per-format

Apply the mapping rules from the Pattern Catalog. Build a draft table of
proposed imports BEFORE any DB write.

### Step 4: Present review — AskUserQuestion pipeline

First `AskUserQuestion`:
- question: "Found N files. Which to import?"
- options: one per file formatted `TODOS.md (304 lines) — checkbox TODOs`,
  plus `"All detected files"`, `"Pick subset (I'll list)"`, `"Cancel"`.
- On subset: follow-up multi-select AskUserQuestion.

For each selected file, show the proposed table then `AskUserQuestion`:
- question: "Proceed with <file>? (N entries, M ambiguous)"
- options:
  - `"Import all as shown"`
  - `"Resolve ambiguities one-by-one"` — one AskUserQuestion per flagged row
  - `"Edit first"` — free-text AskUserQuestion, parse corrections, re-show
  - `"Skip this file"`
  - `"Cancel import"`

### Step 5: Execute

**For plan-like files** (v0.2 pipeline):
- Call `mcp__dct__ingest_plan_file(file_path, project, auto_resolve_uncertain=true)`.
- The tool handles ULID injection, DB upsert, first-import `[x]` historical
  state, and optional LLM classification in one call.
- This DOES modify the MD file by injecting `<!-- dct:plan/section/cp:<ULID> -->`
  HTML comments — this is intentional; the anchors are the stable cross-cut
  between file and DB. Original text / ordering / code blocks are preserved.

For each confirmed issue-like or changelog entry (legacy v0.1 path):
1. Items: `mcp__dct__create_item(...)` — **REMEMBER the returned `id`** per entry.
2. Parent + sub-tasks: after creating the parent item, loop
   `mcp__dct__add_checkpoint(parent_id, text)` for each sub-task —
   remember each checkpoint id. Mark already-done sub-tasks via
   `complete_checkpoint(cp_id)` in a second pass.
3. Changelog entries: `mcp__dct__add_changelog(...)`.
4. Provenance: `mcp__dct__add_note(item_id, f"Imported from {filename} on {date}")`
   for every item. This feeds the post-migration audit trail.
5. For issue-like / changelog: NEVER modify or delete the source markdown file.

### Step 6: Report

Per-file summary: counts by type / priority / done-vs-open. End with
"X items, Y checkpoints, Z changelog entries imported from N files".

---

## Pattern Catalog

### Format A — TODOS.md (checkbox flat list)

```markdown
## P1 — Blockers
- [ ] Fix parser crash on empty files
- [x] Rotate session tokens  ← already done

## P2 — Important
- [ ] Add progress bar
```

Mapping:
- Section heading `## P1 — ...` → `priority="P1"` for items in that section
- `- [ ]` / `- [x]` → `status="open"` / `status="resolved"`
- `✅` or bare `**DONE**` or `STATUS: DONE` at entry end → equivalent to `[x]`
- Line after dash → `title` (strip bold markers, emoji, leading/trailing whitespace)
- `item_type` defaults to `todo` unless entry text matches type triggers
  ("bug", "crash", "error" → issue; "add X" → feature; etc.)

### Format B — Narrative sprint / ROADMAP

```markdown
## Sprint 3 — January push

Goal: ship the parser rewrite, close the 4 blocker bugs, validate perf.

- Parser rewrite from scratch (new AST, incremental indexing)
- Memory leak fix in watcher — blocking prod since Jan 4
- Cross-platform CI matrix
- Performance baseline vs v0.17
```

Mapping (**plan-as-parent** strategy):
- Each `## Sprint N — <title>` or `## Sprint 0.5 — <title>` → ONE parent item:
  `item_type="feature"`, `title="Sprint N — <title>"`, description includes the Goal line
- Each bullet under the sprint → `add_checkpoint(parent_id, bullet_text)`
- Completed sprint (all bullets done, or marked "Done"/"✅ Shipped"):
  resolve parent + complete all checkpoints
- Recognize multilingual section markers: `## Sprint`, `## Iteration`,
  `## Faza`, `## Etap`

### Format C — Custom issue format (ORCH-N, BUG-N)

```markdown
## ORCH-3 — Scheduler race on concurrent writes

**Where**: cmd/scheduler/dispatch.go:142
**Problem**: Two workers claim the same job when heartbeat skipped.
**Fix**: Add advisory lock around SELECT FOR UPDATE SKIP LOCKED.
**Severity**: P1
**Status**: open
```

Mapping:
- Heading → `title` (strip the ID prefix or capture it as tag)
- `**Where**:` → `source_file` (+ `source_lines` if line range present)
- `**Problem**:` + `**Fix**:` → merged into `description`
- `**Severity**:` → `priority` (P1/P2/P3)
- `**Status**:` → `status` (map "open"/"in progress"/"done"/"resolved")
- `item_type="issue"` (that's what this format represents)
- Prefix like `ORCH-3` → `tags=["orch"]` (derive bucket tag from prefix)

### Format D — Plan-as-parent (1.5k+ line planning doc)

Large planning documents (1.5k-2k+ lines) SHOULD NOT be flattened into
hundreds of items. Instead:

1. File → ONE parent item: `item_type="feature"`, title from file H1,
   description = first prose section + link to file path.
2. Each `##` section → `add_checkpoint(parent_id, section_title)`.
3. Each checkbox WITHIN those sections → one more `add_checkpoint` (nested
   via sort_order grouping, e.g. section 1 gets sort 10-19, section 2 gets 20-29).
4. If file has >7 `##` sections, ask user via `AskUserQuestion` whether
   they prefer "one parent per section" (multiple items) instead.

### Format E — Keep-a-Changelog with [Unreleased]

Standard format:
```markdown
## [Unreleased]
### Added
- New Python parser support

### Fixed
- Memory leak in watcher
```

Mapping:
- `### Added/Changed/Fixed/Removed` → `category` directly
- Bullet → `entry`
- Only import `[Unreleased]` section by default (past versions already released)

### Format F — Keep-a-Changelog WITHOUT [Unreleased]

Some projects collapse everything into versioned sections and have no
`[Unreleased]`. Ask user via `AskUserQuestion`:
- question: "No [Unreleased] section found. Import all versions or skip?"
- options:
  - `"Import latest version only (treat as unreleased)"`
  - `"Import all versions, stamped with their versions"` — uses
    `mcp__dct__add_changelog` then `release_changelog(version=X)` per version
  - `"Skip changelog import"`

### Format G — Empty subsections

```markdown
## [Unreleased]
### Added

### Fixed
- Actually something here
```

Skip empty subsections gracefully — do not create placeholder items. If
ALL subsections under [Unreleased] are empty, treat the file as "no
unreleased content" and move on.

### Format H — FUTURE.md / IDEAS.md (prose or loose bullets)

```markdown
## Maybe someday

Would be cool to support Ruby parsers. The challenge is...

Another idea: plugin system for custom languages.
```

Mapping:
- Each H2/bullet → `item_type="idea"`, `priority="future"`
- Description = the prose underneath (first 500 chars)

### Format I — Conventional-commit style entries

```markdown
- feat(parser): add Python support
- fix(watcher): memory leak on large trees
- refactor(graph): extract schema builder
```

Mapping (useful when importing from git log turned into markdown):
- Prefix `feat:` / `feat(scope):` → `category="added"`, `component=<scope>`
- `fix:` → `category="fixed"`
- `refactor:` / `chore:` / `perf:` / `style:` → `category="changed"`
- `revert:` / `remove:` → `category="removed"`
- Text after `:` → `entry`

### Format J — No-changelog graceful case

If the project has NO changelog file, do NOT error. Report: `"No changelog
file found — skipping changelog import"` and continue with items only.

---

## Stale-file skip list

Skip these paths during Step 1 detection — do NOT offer them to the user.
Common sources of already-migrated / archival / generated content:

- `.cursor/plans/`, `.cursor/chats/` — Cursor working files
- `worktrees/`, `.git/worktrees/` — git worktree dirs
- `old_modules*`, `old-*`, `*-archive`, `archive/` — manual archives
- `node_modules/`, `target/`, `dist/`, `build/` — build outputs
- `.venv/`, `venv/`, `__pycache__/` — Python virtualenvs
- `.claude/`, `.gstack/` — plugin internals
- Any file matching `*.bak`, `*.old`, `*~`

User can override ("include archive/old-bugs.md anyway") via explicit path
in their /dct-import invocation.

---

## Edge case handling

### Bilingual / mixed-language titles

Polish, English, mixed — preserve as-is. Do not translate, do not strip
language markers. Example: `"Parser crashes / Parser się wywala"` stays
as the title unchanged.

### Composite files (plan + checkboxes + narrative)

When a file clearly mixes formats, pick the dominant one and note the
mixed nature in the user's review table ("composite: plan-as-parent with
embedded checkboxes"). Usually "plan-as-parent" wins for 500+ line docs.

### Done-status recognition

A sub-task is DONE when ANY of these hold:
- `[x]` or `[X]` prefix
- `✅` marker anywhere in the line
- Literal `**DONE**`, `STATUS: DONE`, `[DONE]`, `(done)`, `— done`
- Line struck through with `~~text~~`

### No sprint / no plan / no checkboxes

If the file is pure prose without structure, offer to import as a single
`item_type="idea"` with the file content as description (truncated to
~1000 chars with link to full file path).

---

## Rules

- **Every user decision is AskUserQuestion** — no plain-text prompts.
- **Plan-like files → `ingest_plan_file`** — v0.2 pipeline owns ULID injection
  and first-import state semantics. Do not re-implement these inline.
- **Issue-like / changelog files → v0.1 items / changelog path** — unchanged,
  uses Pattern Catalog below.
- **Plan-like ingest DOES modify source files** — it injects HTML-comment
  ULID anchors. Users should know this (show diff option if they're unsure).
  Other formats: NEVER modify source files.
- **Provenance notes are mandatory for v0.1 items** — every legacy-path
  item gets `add_note("Imported from <file> on <date>")`. Plan-like entries
  have `plans.source_file` + `plans.imported_at` which serve the same role.
- **Respect stale-file skip list** — do not even offer those to the user.
- **One file, one format** — pick dominant; note composites for the user.
- **Changelog missing is OK** — skip gracefully, never error out.
- **Ambiguities are NEVER silent** — always flag and ask.
