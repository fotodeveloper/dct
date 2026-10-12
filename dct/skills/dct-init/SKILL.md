---
name: dct-init
disable-model-invocation: true
description: |
  [dct] Initialize dct for the current project — dual-mode: runs global bootstrap if
  missing, then registers the current directory as a dct project. Interactive
  via AskUserQuestion for path / slug / name / version (with smart defaults from
  detect_version) and, once, the commit-line preferences (Co-Authored-By, a
  session link). Also offers to inject `mcp__dct__*` into the Claude Code
  permissions allowlist so daily MCP calls stop prompting. A registered
  project gets its configuration status, never a reconfiguration.
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

If complete: continue to Step 2.

### Step 2: Probe current project registration (before any setup question)

Call `mcp__dct__list_projects`. Let `cwd = os.getcwd()`.

Match: for each project row, check if `cwd == row.path` or
`cwd.startswith(row.path + "/")`. Longest-prefix wins (use
`get_project_by_path` semantics).

If matched — the project is already set up: read its configuration and
SHOW it; nothing is reconfigured (no allowlist edit, no script generated or
changed, no registration). Read-only, in parallel:

- the allowlist — Step 3's probe of `permissions.allow` in
  `~/.claude/settings.json`: present / missing;
- the version sync — Step 5's probe: `scripts/dct-version-sync.sh` (with or
  without the dct sentinel), else `scripts/version-sync.sh` (the release
  runs it as the version sync), else none;
- the commit-line preferences — the row's `commit_co_authored_by` and
  `commit_session_link`: on / off / not answered (null).

```
✓ Already registered as `<slug>` — current_version=<v>, path=<path>
  Allowlist:     mcp__dct__* in ~/.claude/settings.json
  Version sync:  scripts/dct-version-sync.sh (dct template)
  Commit lines:  Co-Authored-By on · session link off
  Change the version: `dct project set-version <slug> <v>` · items: /track-list
```

The other states, one line each: "Allowlist: missing — add mcp__dct__* to
permissions.allow in ~/.claude/settings.json"; "Version sync:
scripts/dct-version-sync.sh without the dct sentinel", "Version sync:
scripts/version-sync.sh — run by every release as the version sync, before
you approve" or "Version sync: none — template: dct's
scripts/dct-version-sync.sh"; "Commit lines: not answered".

Then stop. The one exception: a commit-line preference that is not
answered (the project was registered before dct stored them) — ask the
question of Step 8 once and store the answer; nothing else is asked.

If no match: continue to Step 3.

### Step 3: Probe + offer MCP allowlist injection

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

### Step 4: Probe project-local CC artefacts for conflicts

Scan CWD for project-local CC artefacts that could conflict with or shadow
the global dct install. Read-only until the owner decides each conflict.

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
    - "Skip — decide later" — leave the conflict, proceed to Step 5

Each decision is recorded; at the end of Step 4, print a short summary:
  "✓ Handled N conflicts: <list>" or "✓ No conflicts" if clean.

### Step 5: Probe version-sync script integration

dct's release workflow (`dct release prepare`, behind `/release`) runs the
committed `./scripts/dct-version-sync.sh <version>` if present — it takes
precedence — else a script named exactly `./scripts/version-sync.sh`,
automatically, with no setting to switch it on — once, in an isolated
temporary worktree — and the owner approves the files it changes as the
project's version-file contract at the first release
(`docs/release-workflow.md`).

**The script changes ONLY the version files** (the version fields of
`pyproject.toml`, `package.json`, `Cargo.toml`, …) and nothing else.
`prepare` runs it BEFORE the owner sees the release's consent screen, and
the isolated worktree is not a sandbox: the script can still reach the
network, the user's home directory or any absolute path. Never wrap a
release, publish or deploy command (an existing `release.sh`, a `release`
make / just / task target, `npm publish`, `cargo publish`, `twine upload`,
a tag or a push) as the version sync — it could publish before anyone
approved the release. A script that writes into the checkout itself is
refused by prepare, with the changed files named.

Probe in order (first-match wins for the existence check):

1. `./scripts/dct-version-sync.sh` — if line 1 begins with `# dct-version-sync template v`, it is integrated: report "✓ version-sync already integrated" and continue. Without the sentinel → treat as existing-but-unmarked: ask whether to add the sentinel.
2. `./scripts/*version*.sh` or `./scripts/*bump*.sh` — an existing non-dct bump script.
3. None of the above — greenfield, no script.

Based on probe result, use `AskUserQuestion`:

**Case 1 — existing dct template (sentinel present)**:
skip, already good.

**Case 2 — existing non-dct script**: read it first, to see which version
files it edits. It is never wrapped: the dct script carries its own
version-file edits, so it is safe to run before the consent screen whatever
the other script does. Branch on the path found:

- exactly `scripts/version-sync.sh` — dct's release already runs it
  automatically as the version sync (when there is no
  `scripts/dct-version-sync.sh`, which takes precedence), BEFORE the owner's
  consent screen, so it must change only the version files. Say so plainly:
    question: "scripts/version-sync.sh is run by every dct release as the version sync, before you approve the release. Keep it that way?"
    header: "Version sync"
    options:
      - "Generate scripts/dct-version-sync.sh from the dct template — it takes precedence, so the release no longer runs scripts/version-sync.sh (recommended)"
      - "Keep it — the release runs scripts/version-sync.sh as it is (it must change only the version files)"
      - "Skip — configure nothing now; the release still runs scripts/version-sync.sh"
- Any other name (`scripts/bump-version.sh`, …) — the release never runs
  it:
    question: f"Found existing {script_path}. Integrate with dct?"
    header: "Version sync"
    options:
      - "Generate scripts/dct-version-sync.sh from the dct template — it edits only the version files {script_path} edits (recommended)"
      - "Keep standalone — the release workflow bumps no files; the project releases those files with its own tool"
      - "Skip for now"

  On "generate": copy dct's `scripts/dct-version-sync.sh` template, uncomment
  the blocks for the version files the existing script edits (or, when that
  is unclear, the block matching the detected project kind — `detect_version`
  in Step 6d: VERSION / pyproject.toml / package.json / Cargo.toml /
  tauri.conf.json). The original script stays untouched.

**Case 3 — no script detected**:
  question: "No version-sync script found. Generate template?"
  header: "Version sync"
  options:
    - f"Generate scripts/dct-version-sync.sh with {detected_kind} block uncommented (recommended)"
    - "Generate all blocks commented (pick manually later)"
    - "Skip"

All generated scripts include the sentinel `# dct-version-sync template v1`
as line 1 (after shebang) so future `/dct-init` runs short-circuit via
Case 1 and do not regenerate. Generating never overwrites an existing
`scripts/dct-version-sync.sh`: Case 1 finds it first, and only the owner's
answer to its sentinel question changes it.

### Step 6: Collect registration inputs

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
   Else:
     question: "Starting version? (none detected)"
     header: "Version"
     options:
       - "0.1.0 (alpha — recommended)"
       - "0.0.1 (pre-alpha)"
       - "1.0.0 (stable)"
       - "Type custom"

For any "Other — type custom" / "Type custom", use the answer's free-text value.

### Step 7: Execute registration

Build command:
```
dct project add <slug> <path> --name "<name>" --version <vX.Y.Z>
```

Always pass the chosen `--version`: without it `dct project add` stores the
version it detects itself, so there is no "no version" choice when one is
detected (change it later with `dct project set-version`).

Run via Bash. Verify with `mcp__dct__list_projects` — the new slug must appear.

### Step 8: Commit-line preferences — one question, asked once

Which lines /commit adds to every commit of this project. Asked once, at
the project's first configuration (and for a registered project whose
preferences are not answered yet — Step 2). Once stored, never asked
again: /commit applies the stored answer without asking.

  question: "Which lines should commits in <slug> carry? I ask once — /commit applies your answer to every commit."
  header: "Commit lines"
  options:
    - "Co-Authored-By, no session link (recommended)"
    - "Neither"
    - "Both — Co-Authored-By and a link to the assistant session"
    - "Session link only"

The project's own rules win over a stored yes: when its CLAUDE.md forbids
one of these lines (a public repository that bans session links), say so
in the question and leave out the options that add it.

Store the answer (on = the line is added, off = never added):
```
dct project trailers <slug> --co-authored-by on|off --session-link on|off
```

### Step 9: Final report

```
✓ Bootstrap:    mcpServers.dct OK, N skills installed, M hooks installed
✓ Allowlist:    "mcp__dct__*" in ~/.claude/settings.json
✓ Project:      <slug> registered at <path>, version <v>
✓ Commit lines: Co-Authored-By on · session link off
  Next: /dct-import (tracking files found) · /track to add the first item
```

The report ends the skill — no closing question. The `Next:` line is a
hint, not a menu: name `/dct-import` first when the project path holds
plausible tracking files (any of `ROADMAP.md`, `TODOS.md`, `BACKLOG.md`,
`SPRINT*.md`, `PLAN*.md`, `CHANGELOG.md`, `changelog.md`,
`docs/plans/*.md`, `docs/specs/*.md`), else `/track` alone. Registration
alone leaves an empty project (`/pickup` and `/track-list` return
nothing), so the hint says where to go — the owner runs it when they want.

## Rules

- **Language.** A quoted question, option, screen or report fixes what it
  says, not its wording: write it in the language of the conversation. Keep
  unchanged: identifiers, commands, paths, file contents, commit messages,
  changelog entries, short titles and anything shown verbatim.
- **The screen travels inside the dialog.** What the owner must read before
  answering is the preview of the option it belongs to, at most 15 lines as
  the dialog shows them. The dialog wraps long lines itself, about 60
  characters wide: a paragraph is one line, never broken by hand (a second
  break leaves single words on a line), and a list or table line stays under
  60 characters. Message text written before a dialog is never the screen: it
  can land in the thinking block, which the owner sees only as a one-line
  summary, so no question or option says "shown above". A longer screen is
  split into pages of at most 15 lines: the option's preview shows page 1, and
  one more single-select question, "Details", has one option per page (2 to
  4), each with that page as its preview; its answer is not a decision.
- **Every decision is AskUserQuestion.** Never plain Y/N via read.
- **Idempotent probes first.** Safe to re-run at any time — an already
  registered project gets its configuration status (Step 2) and no setup
  question; only a commit-line preference that is not answered yet is
  asked, once.
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
