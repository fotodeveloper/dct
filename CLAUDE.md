# dct — Deterministic Context Tracker

Cross-project work tracking tool for Claude Code. Items, changelog, notes — all in one database.

## Stack

- **Python 3.11+**, SQLAlchemy Core (NOT ORM), argparse
- **Database**: PostgreSQL (primary) or SQLite (fallback/OSS)
- **MCP**: FastMCP via `mcp[cli]`, **user-scoped** (`~/.claude.json`)
- **Config**: `~/.claude/dct.toml`

## Commands

```bash
# Development
.venv/bin/pytest -v                    # Run tests (in-memory SQLite)
.venv/bin/dct --version                # Check version

# MCP server (smoke-test — boots, lists tools)
uv run --no-project --with 'mcp[cli]' --with sqlalchemy --with 'psycopg[binary]' \
  python -c "from dct.server import mcp; print([t.name for t in mcp._tool_manager._tools.values()])"
```

## Architecture

```
dct/config.py            — Config loading (~/.claude/dct.toml + env var)
dct/db.py                — SQLAlchemy Core tables + engine factory + migrations
dct/core/projects.py     — Projects CRUD
dct/core/items.py        — Items / notes / checkpoints CRUD
dct/core/changelog.py    — Changelog entries CRUD + release stamping
dct/core/sprint.py       — Current-sprint ops (set/clear/list/review); delegates to item_sprints
dct/core/handoff.py      — Session handoffs (cross-session prompt ledger)
dct/core/version.py      — Version bump + project-file autodetect
dct/core/checkpoints.py  — Item checkpoint helpers

dct/core/ids.py          — ULID generation (v0.2+)
dct/core/sprints.py      — First-class sprints + item_sprints + current-sprint resolution (single source of truth)
dct/core/decisions.py    — ADR / sprint-amendment / inline / open-question records (v0.2+)
dct/core/plans/          — Plans subsystem (v0.2+)
  parser.py              — Mechanical markdown parser (pure)
  classify.py            — claude --print wrapper for uncertain sections
  crud.py                — Plans + sections + checkpoints CRUD
  ingest.py              — End-to-end pipeline: parse → ULID inject → upsert

dct/cli.py               — argparse CLI entry point
dct/server.py            — FastMCP MCP server (47 tools)
dct/export.py            — Keep a Changelog MD export
dct/setup.py             — Init workflow: register user-scope MCP, install hooks/skills,
                           auto-wire hooks into ~/.claude/settings.json (idempotent merge;
                           `dct hooks wire` re-runs it — docs/hooks.md)
```

Core functions take `engine` as first parameter (no global state). All return dicts.
All tables have `deleted_at` for soft deletes (ledger pattern).

### Global-by-design scope model

dct is a **cross-project tool** — every surface is user-scoped, not per-project:

| Artefact | Location | Mechanism |
|---|---|---|
| Database config | `~/.claude/dct.toml` | TOML read by `load_config()` |
| MCP server | `~/.claude.json` | `claude mcp add --scope user` — CC precedence: local > project .mcp.json > **user** > plugin |
| Skills | `~/.claude/skills/<name>` | Symlinks → `<dct_root>/skills/<name>` (so repo updates = instant skill updates) |
| Hooks | `~/.claude/hooks/dct-*.sh` | Symlinks → `<dct_root>/hooks/` (like skills, so repo edits go live without reinstall; any existing legacy copy is replaced in place on re-init; wired in `~/.claude/settings.json`). v0.2.0 adds `dct-plan-autoscan.sh` alongside the two changelog hooks. The blocking changelog hook is **opt-in per project**: it enforces only projects with `manages_changelog=TRUE` — toggle with `dct project changelog <slug> on/off`. New projects default ON; the migration backfills existing projects from their dct changelog history (a project keeping its own changelog opts out automatically). |

This means: register dct once, available in every CC session. Opening a new project directory needs zero dct setup.

### Project registration — `/dct-init` is the canonical entry point

Two layers, one entry point:

- **`dct init`** (CLI, `dct/setup.py`) — global, one-time bootstrap: registers
  MCP at user scope, symlinks skills, copies hooks. Idempotent probes, safe
  to re-run.
- **`dct project add <slug> <path>`** (CLI) — per-project registration:
  inserts a projects row, autodetects version via `detect_version()`. The path
  is stored canonically (`os.path.realpath`) and `get_project_by_path`
  canonicalizes both sides before matching, so CWD detection works under
  symlinked roots (e.g. `/tmp`→`/private/tmp`) instead of silently failing —
  a silent-detect-miss would turn the changelog hook off for that repo.
- **`dct project rename <slug> [--slug X] [--name Y] [--description Z] [--path P]`**
  (CLI) — in-place rename of slug/name/description/path without losing
  `project_id`. All items, changelog entries, checkpoints and handoffs
  keep their FK (rename is cosmetic). Guarded against slug collisions
  with live projects; soft-deleted rows don't block.

**Both are orchestrated by the `/dct-init` skill** (`skills/dct-init/SKILL.md`)
— the recommended entry point for humans. Dual-mode:

1. Probes bootstrap state (`~/.claude.json` MCP, skills symlinks, hooks).
   Runs `dct init` if incomplete.
2. Probes MCP allowlist (`permissions.allow` in `~/.claude/settings.json`).
   Offers to inject `mcp__dct__*` to stop per-call permission prompts.
3. **Step 2.5** (v0.4.0) probes project-local CC artefacts for conflicts
   with global dct — shadowing skills, duplicate hooks, mentions of other
   trackers in CLAUDE.md. Each conflict resolved via AskUserQuestion (keep
   local / backup-and-remove / integrate / skip).
4. **Step 2.6** (v0.4.0) probes for version-sync scripts (`scripts/*`,
   Makefile, justfile, Taskfile) and offers three outcomes: generate dct
   template, wrap existing script with thin dct shim, or skip. All generated
   scripts carry a `# dct-version-sync template vN` sentinel on line 2 so
   re-runs short-circuit via existence check.
5. Probes current project registration (CWD longest-prefix match).
   If unregistered, collects slug / name / version interactively via
   AskUserQuestion with smart defaults (basename, title-case, `detect_version`
   result).
6. Calls `dct project add` under the hood.

Every decision is an AskUserQuestion — no free-text prompts, no plain Y/N.
Single source of truth: the skill never re-implements MCP/skill/hook
installation or DB writes; it orchestrates existing CLI commands.

Design rationale: previously the split between "run `dct init` once" and
"run `dct project add` per-project" was awkward for users and invisible
to new contributors. `/dct-init` collapses them into one mental model
("initialize dct here") while preserving the CLI surface for
non-interactive environments.

### Plan lifecycle (v0.2.0)

Plans, specs, roadmaps, ADRs, retros, and backlogs are indexed in DB while
their markdown content remains the source of truth for prose and code blocks.
State (checkpoint done/rejected, section status, sprint assignment) lives in
DB only — MD brackets are structural anchors.

**Identity**: ULIDs in HTML comments — `<!-- dct:plan:<ULID> -->`,
`<!-- dct:section:<ULID> parent=<ULID> -->`, `<!-- dct:cp:<ULID> parent=<ULID> -->`
— survive heading rename / reorder and allow cross-plan copy detection.

**Three entrypoints for indexing**:

1. `/dct-import` skill — explicit initial import in a project. Discovers
   candidate files (plans, specs, ADRs, roadmaps, backlogs, retros), offers
   a multi-select via `AskUserQuestion`, then runs the full ingest pipeline
   per selected file. Stale-file skip list (node_modules, .venv, archive, …).

2. `dct plan rescan <file>` CLI — manual refresh for a single file.
   Idempotent via `source_hash` short-circuit. Used as the hook entrypoint
   and for post-edit sync from non-CC environments.

3. `hooks/dct-plan-autoscan.sh` (PostToolUse) — passive background sync.
   After any Edit/Write/MultiEdit on a plan-like MD file, fires
   `dct plan rescan <file> --quiet --resolve-uncertain` detached
   (`nohup ... & disown`) so the user never waits. Handles brand-new
   plans (ULID injection on first save) and re-sync of already-tracked.

**First-import vs re-sync** (`plans.imported_at` gate):
- First import: parser reads `[x]`/`[ ]` as historical `status` for
  checkpoints. One-time event — captures pre-dct state.
- Re-sync: MD checkbox state is IGNORED. DB is authoritative;
  state mutations go through `complete_plan_checkpoint` /
  `reject_plan_checkpoint` MCP tools or CLI.

**Uncertain classification**: when regex heuristics can't classify a
section heading (e.g. "Appendix — decisions"), it lands in DB with
`section_type='uncertain'` + `needs_llm_review=TRUE`. Optional resolution
via `claude --print --model <haiku>` (config knob
`[plans].auto_resolve_uncertain`, default true). Graceful fallback: if
`claude` binary is not in PATH, sections remain uncertain and are
surfaced by `/plan-status` / `/plan-resolve-uncertain`.

**Promotion**: a plan section or checkpoint can be converted to a standalone
tracked item via `mcp__dct__promote_section_to_item` /
`mcp__dct__promote_checkpoint_to_item`. The source row is back-linked via
`converted_to_item_id` and preserved in place.

### Version detection on project add

`dct project add <slug> <path>` auto-detects `current_version` when `--version`
is not supplied. Detection order (first hit wins):

1. `<path>/VERSION` — raw single line, stripped
2. `<path>/pyproject.toml` — `[project].version` (PEP 621)
3. `<path>/package.json` — `.version` (npm/yarn)
4. `<path>/Cargo.toml` — `[package].version` (rust)
5. `<path>/src-tauri/tauri.conf.json` — `.version` (Tauri v2) or `.package.version` (Tauri v1)

Detection is best-effort — malformed files are silently skipped so
registration never fails on unrelated parse errors. When no pattern matches:

- **tty** (interactive shell): prompt with `0.0.1 / 0.1.0 / 1.0.0` shortcuts or free-form input (default: `0.1.0`).
- **non-tty** (Claude Code, CI, pipes): project is registered with `current_version=NULL`
  and a warning asks the caller to run `dct project set-version <slug> <version>` later.

Rationale: registering a project with NULL version was a silent trap —
`/release` later crashed with "no current_version". The autodetect +
tty-safe fallback closes that gap. Helper lives in `dct/core/version.py::detect_version`;
regression tests in `tests/test_version.py::TestDetectVersion`.

## Conventions

- Code and docs: English
- Communication: Polish
- Commit format: conventional commits (`feat:`, `fix:`, `refactor:`)
- All changes need a changelog entry before committing
- Tests: pytest with in-memory SQLite fixture

## Hook conventions

Hooks in `hooks/*.sh` run in environments that may lack a tty (Claude Code,
CI, git pre-commit) and must not hang. Follow these five rules:

### Rule 1 — No interactive prompts without tty check

Any `read -p` / `read` with a user prompt MUST be gated:

```bash
if [ -t 0 ]; then
    read -rp "Continue? [y/N] " answer
else
    answer="n"  # safe default when non-interactive
fi
```

Without this gate, the hook hangs indefinitely when launched by Claude Code
(no stdin attached) or CI.

### Rule 2 — Consume stdin when the hook protocol supplies it

Claude Code hooks receive JSON on stdin. Drain it first even if the hook
does not inspect every field:

```bash
input=$(cat)
```

Leaving stdin blocked can deadlock subsequent `git` or `dct` invocations.

### Rule 3 — Graceful degradation when dependencies are missing

A hook must never block the host operation because an optional tool is
absent. Probe and no-op instead:

```bash
command -v jq >/dev/null || exit 0
command -v dct >/dev/null || exit 0
```

Hooks are best-effort guardrails; when they cannot run, the user's commit
or tool call still proceeds.

### Rule 4 — Exit codes carry meaning

- `exit 0` — allow the operation (passthrough, including "not applicable")
- `exit 2` — block with structured JSON reason (Claude Code protocol)
- Any other non-zero — treated as generic failure; avoid

### Rule 5 — Strip message content before flag inspection

When scanning `git commit` commands for flags (`--amend`, etc.), strip
`-m "..."` / `--message=...` content first so quoted message text does not
produce false matches:

```bash
flags=$(echo "$command" | sed 's/ -m [^ ]*//g; s/ -m ".*"//g; s/ --message[= ][^ ]*//g')
case "$flags" in *"--amend"*) exit 0 ;; esac
```

See `hooks/check-changelog.sh` for a reference implementation that follows
all five rules.

## MCP tool conventions

FastMCP exposes each tool's **docstring verbatim as the tool description** the
calling model reads — the docstring IS the schema the model fills in. A thin or
inaccurate docstring makes the model guess; that is how `create_decision`'s four
undocumented content fields became a serialization-mangle source. Every
`@mcp.tool()` docstring MUST:

- **Document every parameter with its role.** No signature parameter may be
  undocumented, and no documented parameter may be absent from the signature.
  Spell out each free-text field's distinct purpose.
- **Make the call structure obvious.** For multi-field free-text tools, state
  that each field is a SEPARATE top-level parameter (this is what averts the
  nested-parameter mangle the boundary guard rejects). Name enums and defaults.
- **Describe the return shape** — `{cols, rows}` (list views) vs a full semantic
  dict (`get_*`) vs `{"error": ...}` (and what triggers the error path).
- **Stay in sync with the code.** When you change a signature, enum, default,
  return shape, or core behavior, update the docstring in the SAME change. A
  stale docstring is worse than none — it confidently misleads the model. The
  v0.7 audit found 7 such drifts (e.g. `update_item` documenting clearable
  fields it doesn't expose; `review_sprint` citing a `/track-review` skill that
  does not exist), so this is a real, recurring failure mode — not hypothetical.

House style: terse, information-dense, no fluff. Exemplars: `create_decision`,
`create_item`, `add_changelog`, `update_item`.

## Bug-find protocol

When any bug, API surprise, or inconsistency is discovered during work,
patch the **system**, not the call-site. Three steps in the same turn:

1. **Fix the root cause in code.** Edit the function / schema / config so
   that the pathological state cannot recur. Do not rely on the caller
   to retry, re-invoke a sibling function, or work around the defect.
2. **Update documentation.** Extend the relevant docstring, this
   `CLAUDE.md`, a `SKILL.md`, or the MCP tool description so the next
   contributor (human or Claude) cannot repeat the mistake.
3. **Add a regression test.** Lock in the invariant — the same class of
   bug must fail loudly if reintroduced.

Red flag: any time you find yourself calling one function to fix what
another should have done correctly (e.g. `update_item(status="resolved")`
leaves `resolved_at=null`, so you also call `resolve_item(...)`), stop
and fix the first function. Double-invocation patterns are defects, not
workflows. Document them only via regression tests.

## Gate checkpoints

User-facing features (new skill, new CLI command, new MCP tool, new hook)
cannot be marked resolved until the **user** has executed the feature
end-to-end in a fresh CC session. Self-review (tests pass, CLI outputs
correct, my own probe) is necessary but not sufficient — UX gaps only
surface under real user interaction.

Enforcement lives in the code, not in convention:

- `item_checkpoints.kind` column (v0.4.0, migration 004). Default `'task'`
  = normal, non-blocking. Any kind other than `'task'` or `'cc-task'` is
  a **gate**.
- `resolve_item` refuses to transition an item to terminal status while any
  gate checkpoint is `pending`. Error message lists pending gates with
  their kinds and points the caller at this section.
- Gates must be **completed** (done) OR **rejected** (not applicable).
  Rejecting a gate preserves the ledger and records that it was
  consciously skipped.

Known gate kinds (open list — add project-specific kinds as needed,
unknown kinds are conservatively treated as gates):

| Kind | Use when |
|---|---|
| `dogfood` | user-facing feature awaiting fresh-session validation |
| `security` | security review required before resolve |
| `approval` | external stakeholder sign-off |
| `review` | code review / PR approval |
| `cc-task` | **not a gate** — mirrored Claude Code TaskCreate (ephemeral) |

**Rule for every new user-facing feature item**: the last checkpoint MUST
be `kind='dogfood'` with text like `"User validates <feature> in fresh session"`.
Without it, `resolve_item` will throw — forcing the item to stay open
until the user confirms via /pickup next session (or rejects as N/A).

Exceptions:
- Internal refactors / docs changes / CI tweaks — no user-facing surface
  to validate; no dogfood gate needed.
- Bug fixes with clear reproduction and test — the regression test IS the
  validation; no dogfood gate needed.
- Features shipped behind a flag not yet enabled — dogfood gate deferred
  until the flag is flipped.

When in doubt: add the gate. Rejecting ("not applicable, N/A reason: ...")
is always available and preserves audit trail.

## OSS hygiene — anonymize while writing

dct is published as OSS. Any identifier that ships in the repo — tests,
docs, skill examples, README, docstrings, CLI help text — is visible to
every user of the tool. Never commit author-specific data: real slugs from
other projects, personal brand names, home-directory paths
(`/Users/<name>`, `/home/<name>`, `C:\Users\<name>`), or version strings
tied to a specific private project's release cadence.

Write code with anonymous placeholders from the first keystroke:

| Kind | Good | Bad |
|---|---|---|
| Slug | `proj-a`, `proj-b`, `mono`, `sub`, `myproj` | real slugs from other projects |
| Display name | `Project A`, `My Project`, `Example` | "SuperTool Pro", brand names |
| Path | `/tmp/proj-a`, `tmp_path` fixture | `/Users/<name>/dev/...` |
| Version | `1.0`, `1.1`, `0.1.0` (generic semver) | real private-project versions |
| Rename fixtures | `old-slug` → `new-slug`, name = `example.io` (keeps dot coverage) | ex-names with real brand |

**Enforcement**:

1. Think before typing — if it's a private name, swap it before the
   keystroke, not after during review.
2. Pre-commit check (manual — not yet automated): run the sniff regex
   over touched files before staging:

   ```bash
   rg -l '<private-slug-1>|<private-slug-2>|/Users/|/home/|C:\\\\Users|<any-other-private-slug>' \
      -- tests skills README.md CLAUDE.md dct/
   ```

   Swap the `<private-slug-*>` placeholders for your own private project
   slugs — the list is your registered projects (`list_projects`) minus the
   current one. Empty output = clean. Any hit = swap to placeholder or
   justify as `## Historical`.
3. Pre-release block (future): `/release` Step 0 runs
   the audit automatically and refuses to stamp if it finds leaks.

**Intentionally exempt** (not rewriteable without harming content):

- `changelog.md` — shipped history, rewriting breaks chronology
- `docs/superpowers/specs/*.md` — dated design specs (historical record)
- Commit messages and tags already in git history

If a leak lands in those: note it, move on. Only anonymize going forward.

**Why this matters**: a leaked private slug in a README example looks
unprofessional, leaks project-portfolio info, and forces every OSS user
to wonder "what's this `xyz-tool` thing — do I need it?". Generic placeholders
are also **better pedagogy** — a reader parses `proj-a` as a role, not
as a mystery.

## Skill fallback workflows

Skills in `skills/` are **the preferred** way to drive dct. But skill names can be
renamed, disabled per-session, or missing in future CC versions. Do not couple logic
to skill existence — the workflows below describe what each skill does so Claude can
perform the steps manually via MCP tools if the named skill is unavailable.

### `/release` fallback

1. Detect current project: match CWD against `mcp__dct__list_projects()` results, take longest path prefix.
2. `git status --porcelain` — if non-empty, STOP and tell user to commit WIP first (`/commit`).
3. `mcp__dct__count_unreleased_changelog(project)` — if 0, inform "no unreleased entries" and stop.
4. `mcp__dct__list_changelog(project, unreleased_only=true)` — present to user.
5. Ask user via AskUserQuestion: `patch` | `minor` | `major` | explicit version.
6. `mcp__dct__release_changelog(project, version=<explicit>)` or `(project, bump_type=...)` — stamps entries with version and updates `current_version`.
7. Render ONLY the just-released version's section: `mcp__dct__export_changelog(project, version=<V>)` (headerless, single-version). If it returns an `{"error": ...}` dict or `""`, STOP (the DB was already stamped in step 6 — fix the export and re-run step 7, or revert the stamp).
8. **Append-only:** resolve the project's changelog file `F = {project.path}/{changelog_path}` (`changelog_path` comes from the step 1 `list_projects` result, default `changelog.md`; authoritative cross-check `dct project changelog-path <slug>` prints the resolved path). PREPEND that section to `F`, directly below the `# Changelog` header (above the previous top version) — NEVER overwrite/regenerate the whole file (a full-DB export drops pre-adoption history). Bootstrap exception: if `F` does not exist yet, write the full `export_changelog(project, include_unreleased=false)`. Mandatory safety check: re-read `F` and confirm the count of lines starting with `## [` (anchored to line start) rose by exactly 1 — one section added, zero lost — before committing.
9. If `{project.path}/scripts/dct-version-sync.sh` exists, run it with new version. This bumps `pyproject.toml`/`Cargo.toml`/`package.json`/`tauri.conf.json`/etc. per the project-specific template. dct itself bumps `pyproject.toml` + `dct/__init__.py` fallback `__version__`.
10. Stage the changelog file `F` + any files modified by the sync script; commit with literal message `release: vX.Y.Z` — no scope, no description, no suffix.
11. **Tag** the release: `git tag vX.Y.Z` on HEAD. The hook's release bypass already let the commit through; the tag is the canonical marker for git describe / GitHub / PyPI.
12. Tell the user the tag is **local only** — pushing to remote is a separate step they must confirm (`git push origin vX.Y.Z` or `git push --tags`).

### `/commit` fallback

1. Pre-flight: run in parallel — `git status`, `git diff --cached --stat`, `git diff --stat`, `git log --oneline -5`.
2. Detect project via CWD.
3. `mcp__dct__count_unreleased_changelog(project)` — if 0 and non-exempt files staged, prompt user to run `/clog` or add entry inline via `mcp__dct__add_changelog`.
4. Determine commit type + scope + description. Read `current_version` from project.
5. Stage files explicitly (path by path). Never `git add -A`. Never stage `.env`, credentials, binaries.
6. HEREDOC commit: `<type>(<scope>): <description> (v<version>)` with `Co-Authored-By:` trailer.
7. Verify: `git log --oneline -3`, report commit hash.
8. On hook rejection: fix the reported issue, re-stage, create a NEW commit (never `--amend`, never `--no-verify`).

### `/track` (create item) fallback

1. Determine project (CWD detect or ask).
2. Gather: item_type, title, priority, description, component, tags (optional).
3. Present draft to user, confirm.
4. `mcp__dct__create_item(item_type, title, project, ...)` — **REMEMBER the returned `id`**.
5. For feature/improvement items: break work into 3-7 checkpoints via `mcp__dct__add_checkpoint(item_id, text)` — each one concrete and actionable. **REMEMBER each returned `id`**.
6. Report to user: `#<id> <type>/<priority> — <title>` plus checkpoint count.

### `/clog` (changelog entry) fallback

1. Determine project (CWD detect or ask).
2. Gather: category (added/changed/fixed/removed), entry text, component (optional), related_item_id (optional).
3. Present draft, confirm.
4. `mcp__dct__add_changelog(category, entry, project, component, related_item_id)`.
5. Report: `✓ clog #<id> [<category>] — <entry>`.

### `/track-resolve` (resolve/close item) fallback

1. Parse ID(s) from user input. Accept: `#42`, `42`, `ITEM-42`, `0042` — normalize via regex `^(#|ITEM-|item-)?0*(\d+)$`.
2. For each id: `mcp__dct__get_item(id)` — show current state, any unfinished checkpoints.
3. Determine resolution status: `resolved` (fixed/implemented) or `wont_fix` (scope decision).
4. Build resolution note: "Fixed in commit <hash>" if recent commit, or "Decision: <reason>" from context, else ask user.
5. If item has pending checkpoints, ask whether to `reject` them (preserved in ledger) or proceed anyway.
6. `mcp__dct__resolve_item(id, status, resolution)` — set terminal status.
7. Report: `✓ #<id> → <status>`.

These prose descriptions are the authoritative behavior. Skill files in `skills/` are
shortcuts. If they diverge — fix the skill, not this doc.
