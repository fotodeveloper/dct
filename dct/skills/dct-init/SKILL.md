---
name: dct-init
disable-model-invocation: true
description: |
  [dct] Initialize dct for the current project — dual-mode: runs global bootstrap if
  missing, then registers the current directory as a dct project. Interactive
  via AskUserQuestion for path / slug / name / version (with smart defaults from
  detect_version). Also offers to inject `mcp__dct__*` into the Claude Code
  permissions allowlist so daily MCP calls stop prompting.
  Use when: "dct-init", "/dct-init", "zainicjalizuj dct", "setup dct here",
  "add this project to dct", "register this project", "dodaj projekt do dct",
  "skonfiguruj dct", "dct setup", "initialize dct", "bootstrap dct".
---

# /dct-init — bootstrap or register

One entry point for dct setup. Replaces the split mental model of
`dct init` (global, once) + `dct project add` (per-project, manual).

## Workflow

### Step 1: Probe global bootstrap state

Check three things in parallel:

1. MCP registration: read `~/.claude.json`; does `mcpServers.dct` exist?
2. Skills: does `~/.claude/skills/track` exist (as symlink or dir)?
3. Hooks: does `~/.claude/hooks/dct-check-changelog.sh` exist?

Derive `bootstrap_complete` = all three present.

If NOT complete:
  AskUserQuestion:
    question: "Global dct bootstrap is incomplete. Run `dct init` now?"
    header: "Bootstrap"
    options:
      - "Yes — run bootstrap (recommended)"
      - "Abort — I'll fix it manually"
  On "yes": Bash `dct init`, capture output, show summary. Re-probe after.
  On "abort": print instructions and exit.

If complete: skip to Step 2.

### Step 2: Probe + offer MCP allowlist injection

Read `~/.claude/settings.json`. Check if `permissions.allow` contains
any entry matching `mcp__dct__*` (wildcard) or any individual
`mcp__dct__<tool>` entry.

If none present:
  AskUserQuestion:
    question: "dct MCP tools aren't on the Claude Code allow list — every call prompts. Add them?"
    header: "Allowlist"
    options:
      - "Yes — global ~/.claude/settings.json (recommended)"
      - "Yes — project-scope .claude/settings.local.json"
      - "No — skip, I'll prompt-approve each time"

  On "global": edit `~/.claude/settings.json`, append
    `"mcp__dct__*"` to `permissions.allow` (preserve order; dedupe).
  On "project": edit `.claude/settings.local.json` in the CWD
    (create file if missing) with the same entry.
  On "no": continue.

If already present: note "✓ allowlist already has dct entries" and continue.

### Step 2.5: Probe project-local CC artefacts for conflicts

Scan CWD for project-local CC artefacts that could conflict with or shadow
the global dct install. Read-only until Step 2.5b.

Check in parallel:

| Location | What to look for | Conflict when… |
|---|---|---|
| `./.claude/skills/<name>/SKILL.md` | names matching dct's skill list (clog, commit, dct-import, dct-init, handoff, pickup, plan-resolve-uncertain, plan-status, release, track, track-list, track-resolve, track-update) | local skill **shadows** global — typically unintentional |
| `./.claude/hooks/*changelog*.sh` | project-local changelog hooks | duplicates global dct hook behavior |
| `./.claude/settings.json` `hooks` section | wires to local hooks | runs in addition to global ones |
| `./CLAUDE.md` / `./.claude/CLAUDE.md` | mentions of other trackers ("linear", "jira", "TODOS.md", "ISSUES.md") | project has an existing tracker — `/dct-import` may help migrate |

If zero conflicts: report "✓ No CC artefact conflicts with global dct" and continue.

For each conflict, ask via `AskUserQuestion`:

  question: f"Conflict: {<describe>} (found at {<path>})"
  header:   "Conflict"
  options:
    - "Keep local, use it (ignore global)" — CC precedence already favors local, no action
    - "Remove local (backup to <path>.bak) and use global dct" — rename file, keep backup
    - "Integrate — wire local into dct workflow" — for hooks: add `exec <global-hook>` at end of local script; for CLAUDE.md: add a "[uses dct]" note and suggest /dct-import
    - "Skip — decide later" — leave the conflict, proceed to Step 3

Each decision is recorded; at the end of Step 2.5, print a short summary:
  "✓ Handled N conflicts: <list>" or "✓ No conflicts" if clean.

### Step 2.6: Probe version-sync script integration

dct's `/release` Step 6 runs `./scripts/dct-version-sync.sh <version>` if
present. We want every registered project to have a version-sync script so
`/release` can bump project files automatically.

Probe in order (first-match wins for the existence check):

1. `./scripts/dct-version-sync.sh` — if line 1 begins with `# dct-version-sync template v`, report "✓ version-sync already integrated" and continue. Otherwise treat as existing-but-unmarked → ask user whether to add sentinel.
2. `./scripts/*version*.sh` or `./scripts/*bump*.sh` or `./scripts/release.sh` — existing non-dct bump script.
3. `./Makefile` or `./justfile` or `./Taskfile.yml` with a `version:` / `bump:` / `release:` target.
4. None of the above — greenfield, no script.

Based on probe result, use `AskUserQuestion`:

**Case 1 — existing dct template (sentinel present)**: skip, already good.

**Case 2 — existing non-dct script**:
  question: f"Found existing {script_path}. Integrate with dct?"
  header: "Version sync"
  options:
    - "Wrap — generate scripts/dct-version-sync.sh that delegates to <script> $1 (recommended)"
    - "Replace — overwrite with dct template (backup original to .bak)"
    - "Keep standalone — /release will skip Step 6; user runs script manually"
    - "Skip for now"

  On "wrap": write minimal shim:
    ```bash
    #!/usr/bin/env bash
    # dct-version-sync template v1 (wraps <original-script>)
    set -euo pipefail
    exec "$(dirname "$0")/<original-script-name>" "$@"
    ```

  On "replace": copy dct's `scripts/dct-version-sync.sh` template, uncomment
  the block matching detected project kind (result of `detect_version` in
  Step 4d: VERSION / pyproject.toml / package.json / Cargo.toml / tauri.conf.json).

**Case 3 — Makefile/justfile/Taskfile target**:
  question: f"Found {tool} target {target_name}. Integrate with dct?"
  header: "Version sync"
  options:
    - "Wrap — generate scripts/dct-version-sync.sh that calls `{tool} {target} $1` (recommended)"
    - "Skip — /release will skip Step 6; user runs {tool} {target} manually"

**Case 4 — no script detected**:
  question: "No version-sync script found. Generate template?"
  header: "Version sync"
  options:
    - f"Generate scripts/dct-version-sync.sh with {detected_kind} block uncommented (recommended)"
    - "Generate all blocks commented (pick manually later)"
    - "Skip"

All generated scripts include the sentinel `# dct-version-sync template v1`
as line 1 (after shebang) so future `/dct-init` runs short-circuit via
Case 1 and do not regenerate.

### Step 3: Probe current project registration

Call `mcp__dct__list_projects`. Let `cwd = os.getcwd()`.

Match: for each project row, check if `cwd == row.path` or
`cwd.startswith(row.path + "/")`. Longest-prefix wins (use
`get_project_by_path` semantics).

If matched:
  Print "✓ Already registered as `<slug>` — current_version=<v>, path=<path>".
  Offer via AskUserQuestion:
    question: "Project is already registered. What now?"
    header: "Registered"
    options:
      - "Nothing — just confirm status"
      - "Update version — call `dct project set-version`"
      - "Show current items — /track-list"
  Run matching action. Exit.

If no match: continue to Step 4.

### Step 4: Collect registration inputs

Derive defaults first (pure functions, no side effects):
  - `default_path` = CWD
  - `default_slug` = `basename(CWD)`, lowercased, non-alnum → `-`
  - `default_name` = title-case of `default_slug` (split on `-`/`_`, capitalize)
  - `(detected_version, source_file)` = `dct.core.version.detect_version(default_path)`

Ask sequentially (four AskUserQuestions bundled in one tool call when possible):

a) Path confirmation:
   question: "Register dct project at this path?"
   header: "Path"
   options:
     - f"Yes — {default_path} (recommended)"
     - "Different path — I'll type it"

b) Slug:
   question: "Project slug (short id used in CLI, URLs, queries)?"
   header: "Slug"
   options:
     - f"{default_slug} (recommended)"
     - "Other — type custom"

c) Name:
   question: "Human-readable display name?"
   header: "Name"
   options:
     - f"{default_name} (recommended)"
     - "Other — type custom"

d) Version:
   If `detected_version` is not None:
     question: f"Starting version? (detected: {detected_version} from {source_file})"
     header: "Version"
     options:
       - f"{detected_version} (detected, recommended)"
       - "Type custom"
       - "Leave NULL (set later via `dct project set-version`)"
   Else:
     question: "Starting version? (none detected)"
     header: "Version"
     options:
       - "0.1.0 (alpha — recommended)"
       - "0.0.1 (pre-alpha)"
       - "1.0.0 (stable)"
       - "Type custom"

For any "Other — type custom" / "Type custom", use the answer's free-text value.

### Step 5: Execute registration

Build command:
```
dct project add <slug> <path> --name "<name>" [--version <vX.Y.Z>]
```

Skip `--version` when user picked "Leave NULL".

Run via Bash. Verify with `mcp__dct__list_projects` — the new slug must appear.

### Step 6: Final report

```
✓ Bootstrap:   mcpServers.dct OK, N skills installed, M hooks installed
✓ Allowlist:   "mcp__dct__*" in ~/.claude/settings.json
✓ Project:     <slug> registered at <path>, version <v>
```

### Step 7: Propose a concrete next action

Registration alone does nothing visible — the project's DB row is empty.
Offer the natural follow-up via `AskUserQuestion`:

  question: "Project is registered. What next?"
  header:   "Next step"
  options:
    - "Import existing MD tracking — run /dct-import now"
      → delegate to the `/dct-import` skill (pass the freshly-registered
         slug implicitly via CWD auto-detect — the skill takes over from
         there with its own AskUserQuestion flow for file selection).
    - "Create first item manually — run /track"
      → delegate to the `/track` skill.
    - "Exit — I'll come back later"
      → print a one-liner hint listing both commands and stop.

**Why this matters**: without the follow-up prompt, users correctly
register their project and then bounce off a blank slate — the DB has a
projects row but nothing else, so `/pickup` returns empty, `/track-list`
returns empty, and it looks like dct "doesn't do anything". For projects
with pre-existing MD tracking (ROADMAP, TODOS, sprint docs, changelog),
`/dct-import` is almost always the right next move and should be
offered proactively, not discovered by hunting.

**Probe before offering**: if the project path contains plausible
tracking files (any of `ROADMAP.md`, `TODOS.md`, `BACKLOG.md`,
`SPRINT*.md`, `PLAN*.md`, `CHANGELOG.md`, `changelog.md`,
`docs/plans/*.md`, `docs/specs/*.md`), make the `/dct-import` option the
**recommended** choice. If none exist, demote it — the recommended
default becomes `/track` for a greenfield project.

## Rules

- **Every decision is AskUserQuestion.** Never plain Y/N via read.
- **Idempotent probes first.** Safe to re-run at any time — re-running
  with everything in place reports ✓ for each check and exits.
- **Smart defaults, no free-text when avoidable.** The "Other — type
  custom" escape hatch covers unusual cases but default path is one click.
- **Single source of truth for bootstrap.** Skill calls `dct init`
  under the hood; never re-implements MCP/skill/hook installation.
- **Single source of truth for registration.** Skill calls
  `dct project add`; never writes the DB directly.
- **Preserve existing allowlist order.** Append, don't sort; dedupe
  before write.
- **No overwrite without confirmation.** If `mcpServers.dct` already
  exists with different config, surface the conflict via AskUserQuestion
  before touching it.
