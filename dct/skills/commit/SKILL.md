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

The trailer format and the range / proof rules are implemented ONCE by the
release workflow (`dct clog candidates --json`, `dct release plan --json`;
schemas in `docs/release-workflow.md`) and described in
`docs/assistant-fallbacks.md` ("Changelog proof: trailers and range") — on
divergence the code and that doc win.

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
  — changelog count for this branch (uncertain entries — no branch, or
  server-detected — count too); detached HEAD → omit `branch`
  (project-wide count — a gate only, never a candidate list)

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

If a project was detected and `count == 0` (per branch, see Step 1) AND the
files to commit contain non-exempt code, ask via `AskUserQuestion`: "No unreleased changelog entry. Add one now?"
→ "Yes, via /clog" (invoke `/clog` or `add_changelog` inline) / "Skip (warn: may
hit the hook)" / "Cancel".

The count is ONLY this gate (are there entries at all?) — never proof that an
entry belongs to this commit. Which entries the commit carries is decided in
Step 5, from the final staged diff.

**Exempt patterns** (no changelog required if ALL staged files match). They
decide only whether an entry is REQUIRED — attaching an existing matching entry
(Step 5) is always allowed, exempt paths or not:
- `.claude/`, `.claude-plugin/`, `.github/`, `.cursor/`, `.vscode/`, `.gstack/`
- `docs/`, `scripts/`, `tests/`, `dct/skills/`, `test_*.py`, `conftest.py`
- `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `README.md`, `LICENSE*`
- `changelog.md`, `CHANGELOG*`, `.gitignore`
- Config files: `pyproject.toml`, `Cargo.toml`, `package.json`, `biome.json`, `tsconfig*.json`

**Blacklist — NEVER stage:** `.env`, `.env.*`, `credentials*`, `secrets*`,
`*.pem`, `*.key`, binaries, generated artifacts (`dist/`, `build/`, `target/`, `*.pyc`).

### Step 4: Stage (explicit paths) — BEFORE selecting the trailer

The order is fixed: **stage (Step 4) → select (Step 5) → commit (Step 6)**.
The trailer attests what the commit CONTAINS, so the index must hold exactly
the commit's content before any entry is chosen, and nothing is staged after
the choice.

Show the staging plan, get approval, then stage path by path — never
`git add -A`, `git add .`, or globs:

```bash
git add -- dct/core/items.py tests/test_items.py
```

- A path that is ALREADY partially staged (it shows in both
  `git diff --cached` and `git diff`) is a deliberate choice: leave it as it
  is — never re-add the whole file unless the user asks to include its
  unstaged hunks too.
- When the user staged everything themselves, there is nothing to add — the
  index is the plan.

### Step 5: Select the `Changelog:` trailer from the FINAL staged diff

The trailer is the proof the release workflow reads later: an entry no
commit names is released only after an explicit attestation. The rules are
computed by the workflow, not by this skill (`dct/workflows/release/`,
schema `docs/release-workflow.md`):

1. **Candidates** — Step 4 is done, the index is final. Run
   `dct clog candidates --json` in the current checkout. It returns
   `confirmed` (client-recorded entries of THIS branch and checkout),
   `uncertain` (server-detected or without provenance, whatever branch they
   store; on a detached HEAD every entry of this checkout), `other_branch`,
   `already_proven` (ids a commit of the range already names — never cited
   again) and `staged_files`. Show its `warnings` (detached HEAD, approximate
   range). CLI missing or too old → select by hand with the `/commit`
   fallback of `docs/assistant-fallbacks.md` (same classification).
2. **Match against the complete `git diff --cached`** (and `staged_files`):
   keep the `confirmed` candidates whose entry text / `source_file` describe
   what is staged. Ask via `AskUserQuestion` when unsure.
3. **`uncertain` candidates are matched too, but never attached
   automatically** (K-53). When one matches the staged diff, ask; only on
   "yes" confirm it against the ACTUAL checkout first —
   `mcp__dct__update_changelog(entry_id, branch=<current>, worktree=<toplevel>)`
   (both fields) — then attach it. A candidate that describes another change
   is left alone.
4. **Detached HEAD** (`checkout.detached`, K-66): a soft presence check only.
   There are no confirmed candidates; every entry of this checkout is
   `uncertain` (`detached_head`) — ask which (if any) the commit carries.
   Confirm a server-detected one with
   `mcp__dct__update_changelog(entry_id, worktree=<toplevel>)` (no branch
   exists; its unconfirmed branch is cleared). Never fall back to an
   unfiltered list of every branch.
5. **No match, but `other_branch` entries describe the staged change**
   (recorded before a branch rename or switch) → offer
   `mcp__dct__update_changelog(entry_id, branch=<current>)`, then re-select.
6. Format: ONE line `Changelog: <id>, <id>` — ids ascending, comma + space —
   in the final trailer block, before `Co-Authored-By:`. No selected ids → no
   `Changelog:` line.
7. **The strict format is reserved** (K-66): a column-0 line
   `Changelog: <ids>` ANYWHERE in the message counts as proof. When a body
   shows the format as an example, quote or indent it (`> Changelog: 12`) so
   it proves nothing.

### Step 6: Commit — WITHOUT another `git add`

Show the message draft, get approval, then commit exactly what Step 4 staged:

```bash
git commit -m "$(cat <<'EOF'
<type>(<scope>): <description> (v<current_version>)

Optional body paragraph explaining WHY (not what).

Changelog: 12, 15
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- No `git add` here and no `git commit -a` / `git commit <paths>` — any of
  them would change the content after the trailer was chosen.
- `type` ∈ {feat, fix, refactor, chore, docs, test, perf, style, revert};
  `scope` = component (`core`, `cli`, `server`, …);
  `description` = imperative, lowercase, no trailing period, ≤ 72 chars.
- `(v<version>)` suffix — OMIT if `current_version` is null or this IS the release commit.
- Release commits use the literal `release: vX.Y.Z` format instead, and carry
  NO `Changelog:` trailer.
- **Reverts** (K-68) keep git's standard message so the release workflow
  sees the revert: `git revert --no-commit <sha>` stages it (Step 4), then
  the subject is exactly `Revert "<original subject>"` and the body holds the
  column-0 line `This reverts commit <full sha>.` (git's own wording; no
  conventional type, no version suffix). The entries the reverted commit
  proved lose that proof — never cite them in the revert's trailer.
- The `Changelog:` line is the Step 5 selection; omit it when nothing was selected.

### Step 7: Hook-reject recovery

If a hook rejects the commit:
1. Read the error — it says exactly what's missing.
2. Fix the underlying issue (usually: add a changelog entry via `/clog`).
3. Stage the files of the fix explicitly (Step 4 rules).
4. ANY change to the index — re-staging, a new file — makes the selection
   stale: redo Step 5 against the new `git diff --cached` (a new `/clog`
   entry is now a candidate).
5. Create a **NEW** commit (Step 6) — never `--amend` (the rejected commit never happened).
6. Never bypass with `--no-verify`, `--no-gpg-sign`, or similar.

### Step 8: Verify (ONE command)

```bash
git log --oneline -3 && git status
```

Report: `✓ Committed abc1234 — feat(core): add checkpoint CRUD (v0.1.0) · Changelog: 12, 15`
(or `· no Changelog trailer`).

## Rules (hard)

- NEVER commit without user approval of the staging plan AND message draft.
- NEVER select the `Changelog:` trailer before the staging is final, and NEVER
  stage anything after selecting it (Step 4 → Step 5 → Step 6).
- NEVER replace a deliberate partial staging with the whole file.
- NEVER stage `.env`, credentials, binaries, or anything matching the blacklist.
- NEVER use `git add -A`, `git add .`, or broad globs.
- NEVER use `--amend` unless the user explicitly asks (to edit a successful commit).
- NEVER use `--no-verify`. Hooks exist for reasons — if blocked, fix and re-commit.
- NEVER force-push to main/master branches.
- ALWAYS include `Co-Authored-By` trailer with Claude's model identifier.
- Release commits use literal `release: vX.Y.Z` format — no scope, no description,
  no `Changelog:` trailer.
- NEVER attach an `uncertain` candidate (null `branch`, `branch_source='server'`,
  detached HEAD) without asking and confirming it via `update_changelog`.
- Reverts keep git's standard `Revert "…"` / `This reverts commit <sha>.`
  message; examples of the trailer format in a body are quoted.
