---
name: commit
description: |
  Create a git commit following dct changelog-first workflow and conventional commit format.
  Use when: "commit", "commit this", "zapisz", "save this", "commitnij", or when the
  user has finished implementing something and the natural next step is to commit.
---

# /commit — Git Commit (dct-aware)

Universal commit workflow. Works in any dct-registered project — auto-detects from CWD.
Mirrors the global hook's exemption rules so the first attempt passes.

## Workflow

### Step 1: Pre-flight parallel probe

Run these 4 Bash commands **in parallel** (one message, four tool calls):

```
git status
git diff --cached --stat
git diff --stat
git log --oneline -5
```

If nothing changed AND nothing staged → inform user "Clean tree, nothing to commit" and stop.
If only untracked files with no staged or modified files → ask user whether to track them.

### Step 2: Detect project and fetch current_version

Call `mcp__dct__list_projects()` and match longest project path prefix against CWD.

- If CWD is inside a registered project → remember `slug`, `current_version`.
- If not a dct project → `current_version` is null, continue without version suffix.

### Step 2b: Version-bump detection — redirect to /release

If staged changes look like a version bump (matching any of the conditions
below), STOP and suggest `/release` to the user instead of committing here:

- `changelog.md` / `CHANGELOG.md` is staged AND contains a new `## [X.Y.Z]` heading
- Any of `pyproject.toml`, `Cargo.toml`, `package.json`, `tauri.conf.json`, `plugin.json` has a staged `version = "..."` change
- `mcp__dct__count_unreleased_changelog(project)` returned a large batch (≥10) that looks release-ready

Present via `AskUserQuestion`:
- question: "Staged changes look like a version bump. Prefer /release?"
- options: `"Run /release instead"` / `"No — regular commit"` / `"Cancel"`.

Release commits use the literal `release: vX.Y.Z` format (no scope, no version
suffix on subject) — different from regular /commit messages.

### Step 3: Changelog guard

If project detected:
- Call `mcp__dct__count_unreleased_changelog(project)`.
- If count == 0 AND staged files contain non-exempt code (see Exempt patterns below):
  - Prompt user via AskUserQuestion: "No unreleased changelog entry. Add one now? [Yes, via /clog] [Skip, changelog not needed for this commit] [Cancel]"
  - On "Yes" → invoke `/clog` or call `mcp__dct__add_changelog` inline.
  - On "Skip" → proceed but warn this may hit the commit hook.
  - On "Cancel" → stop.

**Exempt patterns** (no changelog required if ALL staged files match):
- `.claude/`, `.claude-plugin/`, `.github/`, `.cursor/`, `.vscode/`, `.gstack/`
- `docs/`, `scripts/`, `tests/`, `skills/`, `test_*.py`, `conftest.py`
- `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `README.md`, `LICENSE*`
- `changelog.md`, `CHANGELOG*`, `.gitignore`
- Config files: `pyproject.toml`, `Cargo.toml`, `package.json`, `biome.json`, `tsconfig*.json`

### Step 4: Stage files explicitly

Show user the proposed staging plan:
```
Files to stage:
  dct/core/items.py
  tests/test_items.py
```

Stage each path **by name** — one per `git add <path>` call, or multiple paths in one call
listed explicitly. NEVER:
- `git add -A`
- `git add .`
- `git add *`

NEVER stage (blacklist):
- `.env`, `.env.*` (any env files)
- `credentials*`, `secrets*`, `*.pem`, `*.key`
- Binaries, generated artifacts (`dist/`, `build/`, `target/`, `*.pyc`)

### Step 5: Draft and commit

Build the commit message with conventional format:

```
<type>(<scope>): <description> (v<current_version>)
```

- `type` ∈ {feat, fix, refactor, chore, docs, test, perf, style, revert}
- `scope` = component name (match project's component taxonomy: `core`, `cli`, `server`, etc.)
- `description` = imperative, lowercase, no trailing period, ≤ 72 chars
- `(v<version>)` suffix — **OMIT if `current_version` is null** or if this IS the release commit.

**Release commits** have a different format (set by `/release`):
```
release: vX.Y.Z
```

Execute via HEREDOC to preserve formatting:

```bash
git commit -m "$(cat <<'EOF'
<type>(<scope>): <description> (v<version>)

Optional body paragraph explaining WHY (not what).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 6: Hook-reject recovery

If the dct hook (or any project hook) rejects the commit:

1. Read the hook's error message carefully — it tells you exactly what's missing.
2. Fix the underlying issue (usually: add changelog entry via `/clog`).
3. Re-stage any new files from the fix.
4. Create a **NEW** commit — do NOT `git commit --amend`. Amend modifies the PREVIOUS commit, which may not exist (rejected commits don't happen).
5. Never bypass with `--no-verify`, `--no-gpg-sign`, or similar escape hatches.

### Step 7: Verify

After successful commit:
```bash
git log --oneline -3
git status
```

Report to user:
```
✓ Committed abc1234 — feat(core): add checkpoint CRUD (v0.1.0)
```

## Rules (hard)

- NEVER commit without user approval of the staging plan AND message draft.
- NEVER stage `.env`, credentials, binaries, or anything matching the blacklist.
- NEVER use `git add -A`, `git add .`, or broad globs.
- NEVER use `--amend` unless the user explicitly asks (to edit a successful commit).
- NEVER use `--no-verify`. Hooks exist for reasons — if blocked, fix and re-commit.
- NEVER force-push to main/master branches.
- ALWAYS include `Co-Authored-By` trailer with Claude's model identifier.
- Release commits use literal `release: vX.Y.Z` format — no scope, no description.
