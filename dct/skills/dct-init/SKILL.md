---
name: dct-init
disable-model-invocation: true
description: |
  [dct] Initialize dct for the current project — dual-mode: runs global bootstrap if
  missing, then registers the current directory as a dct project. Often the
  owner's first contact with dct: ONE dialog says what dct is and what
  registering stores, shows the detected name, folder and version, and asks
  only the real choices, each with its why — whether every release writes the
  new version into the project's version file (a small script dct adds), which
  lines commits carry (asked once), and the Claude Code allowlist when it is
  missing. A registered project gets its configuration status, never a
  reconfiguration.
  Use when: "dct-init", "/dct-init", "zainicjalizuj dct", "setup dct here",
  "add this project to dct", "register this project", "dodaj projekt do dct",
  "skonfiguruj dct", "dct setup", "initialize dct", "bootstrap dct".
---

# /dct-init — bootstrap or register

One entry point for dct setup. Replaces the split mental model of
`dct init` (global, once) + `dct project add` (per-project, manual).

This is often the owner's FIRST contact with dct, so it leads by the hand:
the probes run silently, then ONE dialog explains what dct does and what
registering changes, shows what was detected instead of asking for it, and
asks only what the owner really chooses — each option saying in plain words
what it does and why. Everything the owner reads before answering travels
inside that dialog (Rules: "The screen travels inside the dialog").

## Workflow

### Step 1: Probe global bootstrap state

Check three things in parallel:

1. MCP registration: read `~/.claude.json`; does `mcpServers.dct` exist?
2. Skills: does `~/.claude/skills/track` exist (as symlink or dir)?
3. Hooks: does `~/.claude/hooks/dct-check-changelog.sh` exist?

Derive `bootstrap_complete` = all three present.

If NOT complete:
  AskUserQuestion:
    question: "dct isn't fully set up for Claude Code yet (its tools, skills and hooks). Run `dct init` now?"
    header: "Bootstrap"
    options:
      - "Yes — run bootstrap (recommended)" — "Installs dct's MCP server, skills and hooks for your user; nothing in this folder changes."
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
- the version sync — Step 3's probe: `scripts/dct-version-sync.sh` (with or
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
you approve" or "Version sync: none — `dct project version-sync-script
<slug>` adds one"; "Commit lines: not answered".

Then stop: the status is the final text of the turn. The one exception: a
commit-line preference that is not answered (the project was registered
before dct stored them) — ask the commit-lines question of Step 5 once,
alone in its dialog, and store the answer; nothing else is asked, and the
status follows as the final text.

If no match: continue to Step 3.

### Step 3: Probe silently — no question yet

Read-only, in parallel; the owner sees nothing of this but the dialog of
Step 5:

1. **The allowlist**: read `~/.claude/settings.json`; does
   `permissions.allow` contain `mcp__dct__*` (wildcard) or any
   `mcp__dct__<tool>` entry?
2. **Probe project-local CC artefacts** that could conflict with or shadow
   the global dct install:

   | Location | What to look for | Conflict when… |
   |---|---|---|
   | `./.claude/skills/<name>/SKILL.md` | names matching dct's skill list (clog, commit, dct-import, dct-init, handoff, pickup, plan-resolve-uncertain, plan-status, release, track, track-list, track-resolve, track-update) | local skill **shadows** global — typically unintentional |
   | `./.claude/hooks/*changelog*.sh` | project-local changelog hooks | duplicates global dct hook behavior |
   | `./.claude/settings.json` `hooks` section | wires to local hooks | runs in addition to global ones |
   | `./CLAUDE.md` / `./.claude/CLAUDE.md` | mentions of other trackers ("linear", "jira", "TODOS.md", "ISSUES.md") | project has an existing tracker — `/dct-import` may help migrate |

3. **The version-sync script** (first match wins): `./scripts/dct-version-sync.sh`
   — line 2 (after the shebang) is the sentinel `# dct-version-sync template
   v1` → dct's own, nothing to ask; without it, or `./scripts/*version*.sh`
   / `./scripts/*bump*.sh` → an existing script of the project (Step 5,
   Q2); none → no script yet (Step 5, Q2).
4. **What to show instead of asking**: folder = CWD; slug =
   `basename(CWD)`, lowercased, non-alnum → `-`; name = the slug;
   `(detected_version, source_file)` = `dct.core.version.detect_version(folder)`.
5. **Tracking files** for the report's `Next:` line (Step 7).

What the version-sync script is, for the questions below: dct's release
(`dct release prepare`, behind `/release`) runs the committed
`./scripts/dct-version-sync.sh <version>` if present — it takes precedence —
else a script named exactly `./scripts/version-sync.sh`, automatically, with
no setting to switch it on — once, in an isolated temporary worktree — and
the owner approves the files it changes as the project's version-file
contract at the first release (`docs/release-workflow.md`).

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

### Step 4: Conflicts — only when Step 3 found one

If zero conflicts: nothing to ask; the report says "✓ No CC artefact
conflicts with global dct".

Otherwise ONE `AskUserQuestion` before the registration dialog, one question
per conflict (at most 4; more → the next dialog), the conflict's details
(path, what it shadows or duplicates) as the preview of its first option:

  question: f"Conflict: {<describe>} (found at {<path>})"
  header:   "Conflict"
  options:
    - "Keep local, use it (ignore global)" — CC precedence already favors local, no action
    - "Remove local (backup to <path>.bak) and use global dct" — rename file, keep backup
    - "Integrate — wire local into dct workflow" — for hooks: add `exec <global-hook>` at end of local script; for CLAUDE.md: add a "[uses dct]" note and suggest /dct-import
    - "Skip — decide later" — leave the conflict, proceed to Step 5

Each decision is recorded for the Step 7 report: "✓ Handled N conflicts:
<list>".

### Step 5: The registration dialog — ONE AskUserQuestion

Up to four questions in ONE dialog. A question is asked only when it is a
real choice; what was detected is shown, never asked. Plain words: no
"slug", "template" or "sentinel" in a question or an option.

**Q1 — register** (always):

  question: "Register <folder name> in dct?"
  header: "dct"
  options:
    - "Register" (recommended) — its preview:
      ```
      dct keeps this project's work in one place:
        items      tasks, bugs, ideas — /track
        changelog  what changed, as you work — /clog
        handoffs   where you stopped — /handoff, /pickup
        releases   version, changelog, tag, push — /release

      Registering stores in dct's own database
      (nothing in this folder changes):
        name     proj-a
        folder   /tmp/proj-a
        version  0.2.1 (read from pyproject.toml)
      ```
      No version detected: "version  none yet — your first release proposes 0.1.0".
    - "Change the name or version" — "Say what — I ask again with the new values."
    - "Cancel" — "Nothing is registered and nothing changes."

**Q2 — the version file** — the cases of Step 3's script probe:

**Case 1 — dct's own script** (the sentinel is there): no question; the
report says it is in place.

**Case 2 — an existing script of the project**: read it first, to see which
version files it edits. It is never wrapped: the dct script carries its own
version-file edits, so it is safe to run before the consent screen whatever
the other script does. Branch on the path found:

- exactly `scripts/version-sync.sh` — dct's release already runs it
  automatically as the version sync (when there is no
  `scripts/dct-version-sync.sh`, which takes precedence), BEFORE the owner's
  consent screen, so it must change only the version files. Say so plainly:
    question: "Every release runs your scripts/version-sync.sh before you approve it. Keep it that way?"
    header: "Version sync"
    options:
      - "Add scripts/dct-version-sync.sh from dct — it takes precedence, so the release no longer runs scripts/version-sync.sh (recommended)"
      - "Keep it — the release runs scripts/version-sync.sh as it is (it must change only the version files)"
      - "Skip — configure nothing now; the release still runs scripts/version-sync.sh"
- Any other name (`scripts/bump-version.sh`, …) — the release never runs
  it:
    question: f"Every release could also write the new version for you. Add dct's own script next to {script_path}?"
    header: "Version sync"
    options:
      - "Add scripts/dct-version-sync.sh — it edits only the version files {script_path} edits (recommended)"
      - "Keep standalone — the release workflow bumps no files; the project releases those files with its own tool"
      - "Skip for now"

  "Add" writes the script with `dct project version-sync-script <slug>
  --kind <the version file the existing script edits>` (Step 6). When that
  file is not one dct can edit (VERSION, pyproject.toml, package.json,
  Cargo.toml, src-tauri/tauri.conf.json), say so and leave it out of the
  options. The original script stays untouched.

**Case 3 — no script yet**:
- a version file was detected (`source_file`):
    question: f"Should every release write the new version into {source_file} for you?"
    header: "Version sync"
    options:
      - "Yes — add scripts/dct-version-sync.sh (recommended)" — its preview,
        the version line of `source_file` filled in:
        ```
        dct adds one small file: scripts/dct-version-sync.sh

        What it does: every release writes the new version into pyproject.toml — only this line:
          version = "0.2.1"  →  version = "0.3.0"
        It never builds, tags, pushes or publishes anything.

        When it runs: once per release, before you approve it, in a temporary copy of the project; the release screen shows the change it makes. It works once it is committed — /commit takes it along with your work.

        Why: the released package then carries the same version as its tag and its changelog section.
        ```
      - f"No — I change {source_file} myself" — its preview:
        ```
        Releases still write the changelog section, the tag and the push; pyproject.toml keeps 0.2.1 until you change it yourself.

        You can add the script later:
          dct project version-sync-script <slug>
        ```
- no version file at all: no question — there is nothing to write into;
  the report says so.

Generating never overwrites an existing `scripts/dct-version-sync.sh`:
`dct project version-sync-script` refuses when the file exists, and only
the owner's answer to Case 2 changes anything there.

**Q3 — commit lines** (a new project, and a registered project whose
preferences are not answered yet — Step 2). Which lines /commit adds to
every commit of this project. Asked once; once stored, never asked again:
/commit applies the stored answer without asking.

  question: "Which lines should commits in <project> carry? I ask once — /commit applies your answer to every commit."
  header: "Commit lines"
  options:
    - "Co-Authored-By, no session link (recommended)" — "Each commit names the AI assistant as co-author: a standard git line, visible in the history."
    - "Neither" — "Commits carry no line about the assistant."
    - "Both — Co-Authored-By and a link to the assistant session" — "The link points to this conversation — never in a public repository, the conversation can hold private content."
    - "Session link only"

The project's own rules win over a stored yes: when its CLAUDE.md forbids
one of these lines (a public repository that bans session links), say so
in the question and leave out the options that add it.

**Q4 — the allowlist** (only when Step 3 found no dct entry):

  question: "Let dct's tools run without asking you each time?"
  header: "Allowlist"
  options:
    - "Yes — for all my projects (recommended)" — "Adds mcp__dct__* to permissions.allow in ~/.claude/settings.json; dct's tools read and write only dct's own database."
    - "Yes — only in this project" — "Adds it to .claude/settings.local.json in this folder."
    - "No — ask me each time"

**"Change the name or version"** → the owner says what in plain words
("name My App", "version 1.0.0", "short name my-app", "folder ~/code/app");
apply it and ask the dialog again with the new values in the "Register"
preview. **"Cancel"** → nothing is written, whatever the other answers say.

### Step 6: Execute — after "Register"

1. ```
   dct project add <slug> <path> --name "<name>" --version <vX.Y.Z>
   ```
   Always pass the version the "Register" preview showed: without it `dct
   project add` stores the version it detects itself, so there is no "no
   version" choice when one is detected (change it later with `dct project
   set-version`). With no version detected or given, leave `--version` out.
   Run via Bash. Verify with `mcp__dct__list_projects` — the new slug must
   appear.
2. Q2 "Yes" / "Add" → `dct project version-sync-script <slug>` (Case 2:
   with `--kind <file>`). It writes the script from dct's packaged template
   with the block of that file switched on, prints the one line it will
   change, and refuses to overwrite an existing script.
3. Q3 → store the answer (on = the line is added, off = never added):
   ```
   dct project trailers <slug> --co-authored-by on|off --session-link on|off
   ```
4. Q4 "for all my projects" → edit `~/.claude/settings.json`, append
   `"mcp__dct__*"` to `permissions.allow` (preserve order; dedupe); "only in
   this project" → `.claude/settings.local.json` in the CWD (create the file
   if missing) with the same entry.

### Step 7: Final report

The final text of the turn:

```
✓ Bootstrap:    mcpServers.dct OK, N skills installed, M hooks installed
✓ Allowlist:    "mcp__dct__*" in ~/.claude/settings.json
✓ Project:      <slug> registered at <path>, version <v>
✓ Version sync: scripts/dct-version-sync.sh writes the version into pyproject.toml — commit it (/commit) before the first release
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
- **Show what was detected; ask only real choices.** Folder, name and
  version are shown in the "Register" preview, never asked one by one;
  "Change the name or version" covers the unusual case.
- **Single source of truth for bootstrap.** Skill calls `dct init`
  under the hood; never re-implements MCP/skill/hook installation.
- **Single source of truth for registration and the script.** Skill calls
  `dct project add` and `dct project version-sync-script`; never writes the
  DB directly and never copies or hand-edits a version-sync script.
- **Preserve existing allowlist order.** Append, don't sort; dedupe
  before write.
- **No overwrite without confirmation.** If `mcpServers.dct` already
  exists with different config, surface the conflict via AskUserQuestion
  before touching it.
