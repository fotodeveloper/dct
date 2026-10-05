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

`list_projects`, `count_unreleased_changelog`, `list_changelog` and
`update_changelog` are deferred — load them with ONE ToolSearch call before the
preflight:

```
ToolSearch("select:mcp__dct__list_projects,mcp__dct__count_unreleased_changelog,mcp__dct__list_changelog,mcp__dct__update_changelog")
```

The trailer format and the range / proof rules used below are defined ONCE in
`docs/assistant-fallbacks.md` ("Changelog proof: trailers and range") —
restated here briefly; on divergence that doc wins.

## Workflow

### Step 1: Preflight (ONE parallel message)

Run all probes in a single parallel message:
- `git status`
- `git diff --cached --stat`
- `git diff --stat`
- `git log --oneline -5`
- `git branch --show-current` (empty = detached HEAD) and `git rev-parse --show-toplevel`
- `mcp__dct__list_projects()` — match longest path prefix → `slug`, `current_version` (null if not a dct project)
- `mcp__dct__count_unreleased_changelog(project, branch=<current>, include_unbranched=true)`
  — changelog count for this branch (entries with no branch count too);
  detached HEAD → omit `branch` (project-wide count)

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

If a project was detected and `count == 0` (per branch, see Step 1) AND staged
files contain non-exempt code, ask via `AskUserQuestion`: "No unreleased changelog entry. Add one now?"
→ "Yes, via /clog" (invoke `/clog` or `add_changelog` inline) / "Skip (warn: may
hit the hook)" / "Cancel".

**Exempt patterns** (no changelog required if ALL staged files match). They
decide only whether an entry is REQUIRED — attaching an existing matching entry
(Step 3b) is always allowed, exempt paths or not:
- `.claude/`, `.claude-plugin/`, `.github/`, `.cursor/`, `.vscode/`, `.gstack/`
- `docs/`, `scripts/`, `tests/`, `dct/skills/`, `test_*.py`, `conftest.py`
- `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `README.md`, `LICENSE*`
- `changelog.md`, `CHANGELOG*`, `.gitignore`
- Config files: `pyproject.toml`, `Cargo.toml`, `package.json`, `biome.json`, `tsconfig*.json`

**Blacklist — NEVER stage:** `.env`, `.env.*`, `credentials*`, `secrets*`,
`*.pem`, `*.key`, binaries, generated artifacts (`dist/`, `build/`, `target/`, `*.pyc`).

### Step 3b: Select the `Changelog:` trailer

The trailer is the proof `/release` reads later: an entry no commit names is
released only after an explicit attestation.

1. **Candidates** = `mcp__dct__list_changelog(project, unreleased_only=true,
   branch=<current>)` rows whose `worktree` equals the current toplevel or is
   null, MINUS ids already present in trailers of the range `R` (target SHA =
   `HEAD`):
   - `R` = `v$CUR..HEAD` when `git rev-parse -q --verify "refs/tags/v$CUR"`
     succeeds and `git merge-base --is-ancestor "v$CUR" HEAD` holds
     (`CUR` = `current_version`); otherwise `--since=<oldest unreleased
     entry's created_at minus 1 day> HEAD`, with a warning that the boundary
     is approximate.
   - Ids in `R` = union of
     `git log --format='%(trailers:key=Changelog,valueonly,separator=%x2C)' R`
     and the lines of `git log --format=%B R` that match
     `^Changelog: [0-9]+(, [0-9]+)*$` exactly.
2. **Match against the FINAL staged diff** (`git diff --cached`, after the
   staging plan is settled): keep the candidates whose entry text /
   `source_file` describe what is being committed. Ask via `AskUserQuestion`
   when unsure. NEVER attach an entry whose `branch` is null automatically.
3. **No candidates, but unreleased entries of THIS worktree carry another
   branch** (e.g. recorded before a branch rename or switch) → offer
   `mcp__dct__update_changelog(entry_id, branch=<current>)`, then re-select.
4. Detached HEAD → no automatic selection; ask which entries (if any) to attach.
5. Format: ONE line `Changelog: <id>, <id>` — ids ascending, comma + space —
   in the final trailer block, before `Co-Authored-By:`. No selected ids → no
   `Changelog:` line.

### Step 4: Stage + commit (ONE command, explicit paths)

Show the staging plan, get approval, then stage and commit in a SINGLE command
with explicit paths — never `git add -A`, `git add .`, or globs:

```bash
git add dct/core/items.py tests/test_items.py && git commit -m "$(cat <<'EOF'
<type>(<scope>): <description> (v<current_version>)

Optional body paragraph explaining WHY (not what).

Changelog: 12, 15
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- `type` ∈ {feat, fix, refactor, chore, docs, test, perf, style, revert};
  `scope` = component (`core`, `cli`, `server`, …);
  `description` = imperative, lowercase, no trailing period, ≤ 72 chars.
- `(v<version>)` suffix — OMIT if `current_version` is null or this IS the release commit.
- Release commits use the literal `release: vX.Y.Z` format instead, and carry
  NO `Changelog:` trailer.
- The `Changelog:` line is the Step 3b selection; omit it when nothing was selected.

### Step 5: Hook-reject recovery

If a hook rejects the commit:
1. Read the error — it says exactly what's missing.
2. Fix the underlying issue (usually: add a changelog entry via `/clog`).
3. Re-stage new files from the fix.
4. Recompute the Step 3b trailer selection against the new staged diff (a new
   `/clog` entry is now a candidate).
5. Create a **NEW** commit — never `--amend` (the rejected commit never happened).
6. Never bypass with `--no-verify`, `--no-gpg-sign`, or similar.

### Step 6: Verify (ONE command)

```bash
git log --oneline -3 && git status
```

Report: `✓ Committed abc1234 — feat(core): add checkpoint CRUD (v0.1.0) · Changelog: 12, 15`
(or `· no Changelog trailer`).

## Rules (hard)

- NEVER commit without user approval of the staging plan AND message draft.
- NEVER stage `.env`, credentials, binaries, or anything matching the blacklist.
- NEVER use `git add -A`, `git add .`, or broad globs.
- NEVER use `--amend` unless the user explicitly asks (to edit a successful commit).
- NEVER use `--no-verify`. Hooks exist for reasons — if blocked, fix and re-commit.
- NEVER force-push to main/master branches.
- ALWAYS include `Co-Authored-By` trailer with Claude's model identifier.
- Release commits use literal `release: vX.Y.Z` format — no scope, no description,
  no `Changelog:` trailer.
- NEVER attach a changelog entry with a null `branch` without asking.
