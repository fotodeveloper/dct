---
name: commit
description: >-
  [dct] Create a git commit via changelog-first workflow, conventional format
  with (vX.Y.Z) suffix. Use when: "commit", "save this", or finished work is
  ready to save — in any language.
---

# /commit — Git Commit (dct-aware)

Universal commit workflow. Auto-detects the project from CWD and mirrors the
global hook's exemption rules so the first attempt passes.

## Deferred-tool load (do this first)

`list_projects` and `count_unreleased_changelog` are deferred — load both with
ONE ToolSearch call before the preflight:

```
ToolSearch("select:mcp__dct__list_projects,mcp__dct__count_unreleased_changelog")
```

## Workflow

### Step 1: Preflight (ONE parallel message)

Run all probes in a single parallel message:
- `git status`
- `git diff --cached --stat`
- `git diff --stat`
- `git log --oneline -5`
- `mcp__dct__list_projects()` — match longest path prefix → `slug`, `current_version` (null if not a dct project)
- `mcp__dct__count_unreleased_changelog(project)` — changelog count

If nothing changed AND nothing staged → "Clean tree, nothing to commit", stop.
If only untracked files → ask whether to track them.

### Step 2: Version-bump redirect to /release

If staged changes look like a version bump, STOP and suggest `/release`:
- `changelog.md` / `CHANGELOG.md` staged AND contains a new `## [X.Y.Z]` heading
- A staged `version = "..."` change in `pyproject.toml`, `Cargo.toml`, `package.json`, `tauri.conf.json`, `plugin.json`
- `count_unreleased_changelog` returned a release-ready batch (≥10)

Ask via `AskUserQuestion`: "Staged changes look like a version bump. Prefer /release?"
→ `"Run /release instead"` / `"No — regular commit"` / `"Cancel"`. Release commits
use the literal `release: vX.Y.Z` format (no scope, no version suffix).

### Step 3: Changelog guard

If a project was detected and `count == 0` AND staged files contain non-exempt
code, ask via `AskUserQuestion`: "No unreleased changelog entry. Add one now?"
→ "Yes, via /clog" (invoke `/clog` or `add_changelog` inline) / "Skip (warn: may
hit the hook)" / "Cancel".

**Exempt patterns** (no changelog required if ALL staged files match):
- `.claude/`, `.claude-plugin/`, `.github/`, `.cursor/`, `.vscode/`, `.gstack/`
- `docs/`, `scripts/`, `tests/`, `dct/skills/`, `test_*.py`, `conftest.py`
- `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `README.md`, `LICENSE*`
- `changelog.md`, `CHANGELOG*`, `.gitignore`
- Config files: `pyproject.toml`, `Cargo.toml`, `package.json`, `biome.json`, `tsconfig*.json`

**Blacklist — NEVER stage:** `.env`, `.env.*`, `credentials*`, `secrets*`,
`*.pem`, `*.key`, binaries, generated artifacts (`dist/`, `build/`, `target/`, `*.pyc`).

### Step 4: Stage + commit (ONE command, explicit paths)

Show the staging plan, get approval, then stage and commit in a SINGLE command
with explicit paths — never `git add -A`, `git add .`, or globs:

```bash
git add dct/core/items.py tests/test_items.py && git commit -m "$(cat <<'EOF'
<type>(<scope>): <description> (v<current_version>)

Optional body paragraph explaining WHY (not what).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- `type` ∈ {feat, fix, refactor, chore, docs, test, perf, style, revert};
  `scope` = component (`core`, `cli`, `server`, …);
  `description` = imperative, lowercase, no trailing period, ≤ 72 chars.
- `(v<version>)` suffix — OMIT if `current_version` is null or this IS the release commit.
- Release commits use the literal `release: vX.Y.Z` format instead.

### Step 5: Hook-reject recovery

If a hook rejects the commit:
1. Read the error — it says exactly what's missing.
2. Fix the underlying issue (usually: add a changelog entry via `/clog`).
3. Re-stage new files from the fix.
4. Create a **NEW** commit — never `--amend` (the rejected commit never happened).
5. Never bypass with `--no-verify`, `--no-gpg-sign`, or similar.

### Step 6: Verify (ONE command)

```bash
git log --oneline -3 && git status
```

Report: `✓ Committed abc1234 — feat(core): add checkpoint CRUD (v0.1.0)`

## Rules (hard)

- NEVER commit without user approval of the staging plan AND message draft.
- NEVER stage `.env`, credentials, binaries, or anything matching the blacklist.
- NEVER use `git add -A`, `git add .`, or broad globs.
- NEVER use `--amend` unless the user explicitly asks (to edit a successful commit).
- NEVER use `--no-verify`. Hooks exist for reasons — if blocked, fix and re-commit.
- NEVER force-push to main/master branches.
- ALWAYS include `Co-Authored-By` trailer with Claude's model identifier.
- Release commits use literal `release: vX.Y.Z` format — no scope, no description.
