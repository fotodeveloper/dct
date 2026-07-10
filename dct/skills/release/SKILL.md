---
name: release
description: |
  Bump project version and release changelog entries.
  Use when: "release", "/release", "bump version", "nowa wersja",
  "release X.Y", "wypuść wersję", "zamknij wersję".
---

# /release — Version Bump & Changelog Release

Zero-argument workflow. Auto-detects project from CWD.

## Workflow

### Step 0: Ensure a clean working tree (auto-commit WIP first)

Run `git status --porcelain` in the project root.

**Clean (empty output)** → continue to Step 1.

**Dirty** → do NOT stop. The release commit (Step 7) must still contain ONLY the
changelog file + version-synced files, so auto-commit the working changes as
their OWN separate commit first, then continue. This preserves the reason the
gate existed — a clean release commit is a clean revert target — while removing
the manual interruption. The WIP simply lands in its own commit *below* the
release commit.

> Why a separate commit, not mixed in: mixing WIP code with a version bump
> pollutes `git log` and makes rollback risky. Committing the WIP first keeps the
> release commit changelog-only with zero friction.

Auto-commit procedure (same safety rules as `/commit` — never lose history,
never leak secrets):

1. **Only genuinely-unsafe state stops the flow.** If a merge / rebase /
   cherry-pick is in progress (`.git/MERGE_HEAD`, `.git/rebase-merge/`,
   `.git/CHERRY_PICK_HEAD`), STOP and report — an auto-commit mid-operation is
   unsafe. This is the ONLY hard stop in Step 0.
2. **Stage explicitly — never `git add -A` / `git add .`.** Stage tracked
   modifications and safe untracked files by path. EXCLUDE secrets and junk:
   anything matching `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`,
   credential/token files, or large binaries. If such a file is dirty/untracked,
   leave it unstaged and note it in the Step 9 report — do not commit it, do not
   stop. Split into multiple logical commits when the changes cleanly separate by
   file/concern (mirrors `/commit`); one commit is fine when they don't.
3. **Message** — follow the `/commit` conventions: conventional-commit subject
   `<type>(<scope>): <description> (v<current_version>)` derived from the staged
   diff, with the `Co-Authored-By` trailer. If the changes span too many concerns
   to summarize, use `chore: commit work in progress before release
   (v<current_version>)`.
4. **Commit.** If the pre-commit hook rejects, fix the underlying issue, re-stage,
   and make a NEW commit (never `--amend`, never `--no-verify`).
5. Re-run `git status --porcelain`; it MUST now be empty (modulo any
   intentionally-skipped secret/junk files from step 2). Then continue to Step 1.

### Step 1: Gather state
Call `mcp__dct__list_projects()` and match CWD to a registered project.
Call `mcp__dct__count_unreleased_changelog(project)`.
Call `mcp__dct__list_changelog(project, unreleased_only=true)`.

Present the state:
```
Project: myproj (My Project)
Current version: 1.0
Unreleased entries: 5

  [added] New Python parser support
  [added] Incremental indexing
  [fixed] Parser crash on empty files
  [fixed] Memory leak in watcher
  [changed] Refactored graph schema
```

If there are no unreleased entries, inform the user and stop.

### Step 2: Offer changelog review before bumping

After showing the unreleased entries in Step 1, ask via `AskUserQuestion`:
- question: "Ready to release, or review entries first?"
- options:
  - `"Go ahead — entries look correct"` → proceed to Step 2b
  - `"Amend one entry"` → free-text `AskUserQuestion` asking which (id) and the new text, call `mcp__dct__add_changelog` for the replacement and mark the old one via `/clog` amend flow; then re-show the list
  - `"Drop one entry"` → free-text `AskUserQuestion` for id, soft-delete via direct DB update (or flag for post-release removal); re-show the list
  - `"Cancel release"` → stop

This is the pre-stamp safety net — once Step 4 stamps, reverting requires
manual SQL. Offering an explicit review step prevents regret.

### Step 2b: Ask bump type

The bump type MUST be chosen through `AskUserQuestion` — do NOT substitute a
plain-text prompt, and do NOT infer it from the user's earlier words, even if
they seem decided. The bump is effectively irreversible once Step 4 stamps
(reverting needs manual SQL), so the structured options must show the semver
implication AND the concrete resulting version number before committing to it.
This overrides any general "a plain-text answer is fine" preference.

Compute the concrete next version from `current_version` (vX.Y.Z) for each label:
- question: `"What type of release? (current: v<current_version>)"`
- options:
  - `"patch — v<X.Y.(Z+1)> (bug fixes only)"`
  - `"minor — v<X.(Y+1).0> (new features, backward compatible)"`
  - `"major — v<(X+1).0.0> (breaking changes)"`
  - `"explicit version — I'll type one"` → follow-up free-text AskUserQuestion

### Step 3: Confirm
```
Release myproj 1.1?
  5 changelog entries will be stamped
  Version: 1.0 → 1.1
```
Wait for confirmation.

### Step 4: Stamp version in DB

Call `mcp__dct__release_changelog(project, version=<version>)`. This:
- Updates all unreleased changelog entries with the new `version`.
- Updates the project's `current_version` field.
- After this call, `count_unreleased_changelog(project)` will return 0.

### Step 5: Append the new version's section to the changelog file

The changelog file (`F`, resolved below) is an **append-only archive**. /release ADDS the just-released
version's section to the top of the existing file — it does NOT regenerate the
whole file from the DB.

> **Why append, not regenerate:** a full `export_changelog`
> renders the file from DB rows only. For a project that adopted dct mid-life,
> the DB holds only post-adoption versions, so overwriting `changelog.md`
> silently DROPS every pre-adoption section (one such project lost 18 versions
> / ~536 lines this way). An operation that only ever inserts cannot lose history.

Let `V` = the version just stamped in Step 4 and `F` = the project's changelog
file. **Resolve `F` via the project's `changelog_path`, do NOT hardcode
`changelog.md`:** `F = {project.path}/{changelog_path}`, where `changelog_path`
comes from the Step 1 `list_projects` result (default `changelog.md` when the
field is absent — e.g. a not-yet-restarted MCP server). A project that keeps its
changelog in a subdirectory (e.g. a monorepo with `packages/core/changelog.md`) is then
handled transparently. Authoritative cross-check, always live:
`dct project changelog-path <slug>` prints the resolved absolute path on its
`→` line.

**Case A — F does not exist (bootstrap / from-scratch project):**
Call `mcp__dct__export_changelog(project, include_unreleased=false)` and write
the full result to F. For a from-scratch project the full render equals the
incremental one, so this is safe. Then continue to Step 6.

**Case B — F already exists (the normal path):**

1. Render ONLY the new section:
   `section = mcp__dct__export_changelog(project, version=V)`.
   - If `isinstance(section, dict)` → it is an error: do NOT touch F, report it,
     stop (the DB was already stamped in Step 4, so either fix the export error
     and re-run Step 5, or manually revert the stamped entries).
   - If `section == ""` → V has no entries (unexpected after Step 4): STOP and
     report rather than writing an empty section.
2. Read F. Count its existing version-section markers — **lines that start with** `## [` (`grep -c '^## \['`) — call it `N`. Anchor to line start; an entry body may contain `## [` mid-text and MUST NOT be counted.
3. **Idempotency guard:** if a **line starting with** `## [V]` already appears in F (anchored — `grep -q '^## \[V\]'`), the section is
   already archived (e.g. a re-run after a partial release). Do NOT write —
   report "changelog.md already contains [V]" and continue to Step 6.
4. Insert `section` directly **below the `# Changelog` header** (keep exactly
   one blank line after the header) so it becomes the TOP version section.
   Every existing section MUST stay byte-for-byte unchanged below it.
   - Preferred mechanism: an Edit whose `old_string` is the current top
     `## [...] - ...` line and whose `new_string` is `section` + that same line
     (i.e. prepend the new section in front of the previous top version).
   - If F has only the `# Changelog` header and no version sections yet,
     append `section` after the header instead.
5. **Safety check (mandatory):** re-read F and re-count **lines starting with** `## [` (`grep -c '^## \['`). The new
   count MUST be exactly `N + 1` — one section added, zero lost. If it is not,
   STOP, restore F to its pre-write contents, and report. NEVER commit a
   changelog that lost history.

The DB stays the source of truth for the NEXT release's unreleased entries;
the FILE is the source of truth for shipped history.

### Step 6: Sync version-bearing files

If `{project.path}/scripts/dct-version-sync.sh` exists, run it:
```bash
bash scripts/dct-version-sync.sh <version>
```

The sync script is **project-specific** — each project uncomments the
relevant block in the template. Canonical patterns by project kind:

| Project kind | Files to bump | sed/jq snippet |
|---|---|---|
| Python package | `pyproject.toml` `[project].version` | `sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml` |
| Python package (fallback) | `<pkg>/__init__.py` `__version__` | `sed -i '' "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" dct/__init__.py` |
| Rust | `Cargo.toml` `[package].version` | `sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" Cargo.toml` |
| Node (npm/yarn) | `package.json` `.version` | `jq --arg v "$VERSION" '.version = $v' package.json > tmp && mv tmp package.json` |
| Tauri | `src-tauri/tauri.conf.json` `.version` | `jq --arg v "$VERSION" '.version = $v' src-tauri/tauri.conf.json > tmp && mv tmp src-tauri/tauri.conf.json` |
| Claude Code plugin | `plugin.json` `.version` | `jq --arg v "$VERSION" '.version = $v' plugin.json > tmp && mv tmp plugin.json` |
| Swift (Info.plist) | `CFBundleShortVersionString` | `/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" Info.plist` |

For **monorepos** with independently-versioned sub-packages, wrap each
block in `if has_changes "<path>"; then ... fi` so only sub-packages that
actually changed get bumped. `has_changes` is provided by the template.

For **dct itself** (Python package, single-version), the sync script is
uncommented to always bump `pyproject.toml` + `dct/__init__.py` fallback.

If the sync script doesn't exist, skip this step — DB is the source of
truth. Inform the user so they can add one if they have build files that
need sync.

### Step 7: Commit the release

Stage ONLY the artifacts touched by Steps 5 and 6:
- `changelog.md`
- Any files modified by `scripts/dct-version-sync.sh` (check `git status --porcelain` after Step 6)

Then commit with the **literal** release message format:
```bash
git commit -m "release: v<version>"
```

No type, no scope, no version suffix — just `release: v1.1`. The dct global hook
recognizes this prefix and bypasses the changelog-presence check (which would otherwise
fail because all entries were just stamped).

### Step 8: Tag the release (mandatory)

Tag HEAD with the `v<version>` prefix:
```bash
git tag v<version>
```

Rationale: the tag is the canonical marker of "this commit = this release"
for git log, GitHub, and future `git describe` output. Conventional `v`
prefix matches npm, PyPI, Cargo, and GitHub release UI expectations.

**Do NOT `git push --tags`** automatically — pushing to remote is a
separate, user-confirmed action. Inform the user the tag is local; if
they want it on the remote, they run `git push origin v<version>` or
`git push --tags` themselves.

If a tag `v<version>` already exists (rare — usually indicates a failed
earlier release), ask the user via `AskUserQuestion`:
  - "Tag v<version> exists. Move it to the new commit, or fail?"
  - `"Move tag to new HEAD (--force)"`
  - `"Leave tag alone, don't tag this commit"`
  - `"Abort — the existing tag is correct, something is wrong with this release"`

### Step 9: Report
```
✓ myproj released as 1.1
  5 entries stamped
  changelog.md: [1.1] section prepended (older history preserved)
  Version-synced files: pyproject.toml, myproj/__init__.py
  Commit: release: v1.1 (abc1234)
  Tag: v1.1 (local only — run `git push origin v1.1` to push)
```

## Rules
- NEVER release without user confirmation.
- NEVER skip the bump type question, and NEVER substitute a plain-text prompt
  for it — the bump type MUST be an AskUserQuestion (Step 2b). The pre-stamp
  structured choice is the last reversible point.
- If `current_version` is not set, ask for explicit version instead of bump type.
- `changelog.md` is an **append-only archive**: /release only PREPENDS the new
  version's section (Step 5 Case B), it does NOT regenerate the whole file.
  Full-DB export is bootstrap-only (Case A, when no file exists yet). Rationale:
  regenerating from the DB drops pre-adoption history.
- Editing an already-released entry in the DB will NOT re-render the file —
  shipped history lives in `changelog.md`; correct it there directly.
- The release commit's `changelog.md` diff MUST be purely additive (one new
  `## [vX]` section on top). If `git diff` shows deletions of older sections,
  the append-only flow failed — STOP and investigate before committing.
- Release commit message MUST be exactly `release: vX.Y.Z` (no scope, no description).
  The hook's release bypass depends on this prefix.
- The release commit contains ONLY changelog.md + version-synced files — no app
  code. Step 0 guarantees this by auto-committing any WIP as a SEPARATE commit
  first, so the release commit stays a clean revert target.
- Tag is **mandatory** — every release commit gets a `v<version>` tag (Step 8).
  A release without a tag is not a release — it's a commit that happens to bump
  `pyproject.toml`. Tags are the git-native marker for `git describe`,
  `git log --decorate`, GitHub's release UI, and `pip install <pkg>==<version>`.
- Pushing the tag to remote is NOT automatic — inform the user the tag is local
  and suggest `git push origin v<version>` (or `git push --tags`) as a separate step.
