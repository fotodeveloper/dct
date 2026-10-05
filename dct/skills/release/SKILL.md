---
name: release
description: >-
  [dct] Bump project version and release changelog entries. Use when:
  "release", "bump version", "ship it" — in any language.
---

# /release — Version Bump & Changelog Release

Zero-argument workflow. Auto-detects project from CWD.

## Deferred-tool load (do this first)

`list_projects`, `count_unreleased_changelog`, `list_changelog`,
`release_changelog`, `export_changelog`, `update_changelog` and
`delete_changelog` are deferred — load them all with ONE ToolSearch call
before Step 0:

```
ToolSearch("select:mcp__dct__list_projects,mcp__dct__count_unreleased_changelog,mcp__dct__list_changelog,mcp__dct__release_changelog,mcp__dct__export_changelog,mcp__dct__update_changelog,mcp__dct__delete_changelog")
```

**Capability probe (mandatory, before anything else):** the loaded schema of
`release_changelog` MUST list an `entry_ids` parameter. If it does not, STOP:
"The dct MCP server is older than this skill — restart it (or upgrade dct)
before releasing." An old server silently ignores unknown arguments and would
stamp EVERY unreleased entry.

The trailer format and the range / proof rules used below are defined ONCE in
`docs/assistant-fallbacks.md` ("Changelog proof: trailers and range") —
restated here briefly; on divergence that doc wins.

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
   cherry-pick is in progress, STOP and report — an auto-commit mid-operation
   is unsafe. This is the ONLY hard stop in Step 0. Probe with
   `git rev-parse --git-path <marker>` for each of `MERGE_HEAD`,
   `rebase-merge`, `rebase-apply`, `CHERRY_PICK_HEAD` and test whether the
   printed path exists — never a literal `.git/...` path (in a linked worktree
   `.git` is a file, not a directory).
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
   (v<current_version>)`. Each WIP commit carries the `Changelog:` trailer
   selected exactly as `/commit` Step 3b does, against THAT commit's staged
   diff.
4. **Commit.** If the pre-commit hook rejects, fix the underlying issue, re-stage,
   and make a NEW commit (never `--amend`, never `--no-verify`).
5. Re-run `git status --porcelain`; it MUST now be empty (modulo any
   intentionally-skipped secret/junk files from step 2). Then continue to Step 1
   (which pins the candidate SHA AFTER these commits).

### Step 1: Gather state, pin the candidate, read the proof

After Step 0 leaves the tree clean, gather all state in a single parallel message:
- `mcp__dct__list_projects()` — match CWD to a registered project → `slug`, `current_version`, `changelog_path`, `path`
- `mcp__dct__list_changelog(project, unreleased_only=true)` — ALL unreleased entries (every branch)
- `git status --porcelain` — confirm the tree is still clean going into the release
- `git rev-parse HEAD` → pin the candidate `SHA`; `git branch --show-current`; `git rev-parse --show-toplevel`

Then compute the range `R` and read the proof:
- `CUR` = `current_version`. If `git rev-parse -q --verify "refs/tags/v$CUR"`
  succeeds AND `git merge-base --is-ancestor "v$CUR" "$SHA"` holds →
  `R = v$CUR..$SHA`. Otherwise `R = --since=<oldest unreleased entry's
  created_at minus 1 day> $SHA` — and WARN the user that the boundary is
  approximate.
- Proven ids = union of
  `git log --format='%(trailers:key=Changelog,valueonly,separator=%x2C)' R`
  and the lines of `git log --format=%B R` matching
  `^Changelog: [0-9]+(, [0-9]+)*$` exactly (squash merges that keep the
  original messages; quoted `> Changelog: 12` or bulleted `- Changelog: 12`
  prose never counts). A squash configured as "PR title and description"
  drops the commit bodies — those entries show up without proof and need an
  attestation.
- Split the unreleased entries into **with proof in range** and **without
  proof**. An entry whose trailer lies below the previous tag is "without
  proof in range".

Present the state:
```
Project: proj-a (My Project)
Current version: 1.0
Candidate: abc1234 (feat/x) — range v1.0..abc1234
Unreleased entries: 5

  With proof in range (3):
    #12 [added] New Python parser support          (feat/x)
    #13 [added] Incremental indexing               (feat/x)
    #15 [fixed] Parser crash on empty files        (feat/x)
  Without proof (2):
    #16 [fixed] Memory leak in watcher             (feat/x)
    #17 [changed] Refactored graph schema          (no branch)
```

If there are no unreleased entries at all, go straight to Step 2 Question 2
and the version-only branch of Step 3 (or stop if the user does not want a
version-only release).

### Step 2: Decide entries without proof + bump type (AskUserQuestion)

This is the pre-stamp safety net — once Step 4 stamps, reverting requires
manual SQL. Ask the decisions in as few `AskUserQuestion` calls as the
question limit allows (the bump question rides in the first call).

**Question 1 — one per entry WITHOUT proof** (entries with a null branch
included — they get a decision too, never an assumption):
- question: `"#<id> [<category>] <entry> — no Changelog trailer in range. What now?"`
- options:
  - `"Attest — the change is in the candidate"` → the owner confirms the
    change is in `SHA`; collect every attested id, then create ONE empty
    attestation commit on the candidate branch (the ONLY way to add proof
    after the fact):
    ```bash
    git commit --allow-empty -m "$(cat <<'EOF'
    chore: attest changelog entries

    Changelog: 16, 17
    Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
    EOF
    )"
    ```
  - `"Correct it"` → follow-up free-text question for the fix;
    `mcp__dct__update_changelog(entry_id, entry=..., branch=..., ...)`
    (replaces the old Amend flow); re-scan and ask again for this entry.
  - `"Withdraw it"` → `mcp__dct__delete_changelog(entry_id)` (soft delete;
    replaces the old Drop flow).
  - `"Leave unreleased"` → the entry stays for a later release (typical for
    another branch's work that is not in the candidate).

There is NO "include without proof" option. A free-text "cancel" answer stops
the release. A proven entry's text may still be fixed with `update_changelog`
before Step 4 if the user asks.

**After every commit this skill creates (WIP in Step 0, attestation here):**
re-pin `SHA=$(git rev-parse HEAD)` and re-run the Step 1 scan before moving on.

**Question 2 — bump type.** The bump type MUST be an `AskUserQuestion` option —
do NOT substitute a plain-text prompt, and do NOT infer it from the user's
earlier words. The bump is effectively irreversible once Step 4 stamps, so the
options MUST show the semver implication AND the concrete resulting version
number. Compute the next version from `current_version` (vX.Y.Z):
- question: `"What type of release? (current: v<current_version>)"`
- options:
  - `"patch — v<X.Y.(Z+1)> (bug fixes only)"`
  - `"minor — v<X.(Y+1).0> (new features, backward compatible)"`
  - `"major — v<(X+1).0.0> (breaking changes)"`
  - `"explicit version — I'll type one"` → follow-up free-text AskUserQuestion

Compute the release version `V` ONCE from this answer. Every later step — and
every retry — uses that same explicit `V`; never re-derive it from a bump.

Resolve all Question 1 decisions (and their re-scans) before Step 3.

### Step 3: Confirm the proven set
```
Release proj-a 1.1?
  Candidate: def5678 (feat/x)
  Stamped (proven): #12, #13, #15, #16, #17
  Left unreleased: —
  Version: 1.0 → 1.1
```
Wait for confirmation.

**Empty proven set** → ask explicitly: "No proven entries. Release version V
only (no changelog entries)?" If yes, Step 4 passes `entry_ids=[]` and Step 5
writes a version-only section. Tell the user the side effects: the
`since=last_release` anchor does not move (no entry gets `released_at`), and a
full `export_changelog` omits V. If no, stop.

### Step 4: Stamp version in DB

Call `mcp__dct__release_changelog(project, version=V, entry_ids=[<proven ids>])`
— ALWAYS an explicit `version` (never `bump_type`) and ALWAYS an explicit
`entry_ids` list (`[]` for a version-only release). This:
- Stamps exactly those entries with `V` (STRICT — any invalid id fails the
  whole call and nothing is written).
- Updates the project's `current_version` field to `V`.
- Is a no-op when repeated with the same arguments — a retry after an
  interrupted run reuses the same `V` and ids.

Check the answer before continuing: `mode` is `"selected"` (or
`"version_only"` for `[]`), `entry_ids` equals the selection, and `released`
(plus `already_released` on a retry) accounts for every selected id. STOP and
report on any mismatch or `{"error": ...}`. Entries left unreleased keep
counting in `count_unreleased_changelog` — that is expected.

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
`changelog.md`:** `F = {root}/{changelog_path}`, where `root` = the CURRENT
checkout's toplevel (`git rev-parse --show-toplevel`) when it lies inside
`project.path` (e.g. a linked worktree of the project), else `project.path`;
`changelog_path` comes from the Step 1 `list_projects` result (default `changelog.md` when the
field is absent — e.g. a not-yet-restarted MCP server). A project that keeps its
changelog in a subdirectory (e.g. a monorepo with `packages/core/changelog.md`) is then
handled transparently. Authoritative cross-check, always live:
`dct project changelog-path <slug>` prints the resolved absolute path on its
`→` line.

**Case A — F does not exist (bootstrap / from-scratch project):**
Call `mcp__dct__export_changelog(project, include_unreleased=false)` and write
the full result to F. For a from-scratch project the full render equals the
incremental one, so this is safe. A version-only release is omitted by the
full export — insert its section (Case B step 1) below the header. Then
continue to Step 6.

**Case B — F already exists (the normal path):**

1. In ONE parallel message, render the new section AND read F:
   `section = mcp__dct__export_changelog(project, version=V)` and `Read(F)`.
   - If `isinstance(section, dict)` → it is an error: do NOT touch F, report it,
     stop (the DB was already stamped in Step 4, so either fix the export error
     and re-run Step 5, or manually revert the stamped entries).
   - If `section == ""` → V has no entries. For a version-only release
     (Step 3, `entry_ids=[]`) this is expected: use
     `section = "## [V] - YYYY-MM-DD\n\n- Version-only release — no changelog entries.\n"`
     (today's date). Otherwise it is unexpected after Step 4: STOP and report
     rather than writing an empty section.
2. From the F contents, count its existing version-section markers — **lines that start with** `## [` (`grep -c '^## \['`) — call it `N`. Anchor to line start; an entry body may contain `## [` mid-text and MUST NOT be counted.
3. **Idempotency guard:** if a **line starting with** `## [V]` already appears in F (anchored — `grep -q '^## \[V\]'`), the section is
   already archived (e.g. a re-run after a partial release). Do NOT write —
   report "the changelog already contains [V]" and continue to Step 6.
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

If `{root}/scripts/dct-version-sync.sh` exists (`root` as in Step 5), run it
from `root`:
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

### Step 7: Commit + tag the release (ONE command)

Stage ONLY the artifacts touched by Steps 5 and 6 (the changelog file `F` and any files
modified by `scripts/dct-version-sync.sh` — check `git status --porcelain` after
Step 6), then commit and tag in a SINGLE command with explicit paths:

```bash
git add <F> <version-synced-files> && git commit -m "release: v<version>" && git tag v<version>
```

- The commit message is the **literal** `release: v<version>` — no type, no
  scope, no version suffix, no `Changelog:` trailer. The dct global hook
  recognizes this prefix and bypasses the changelog-presence check.
- The tag is **mandatory** — the `v<version>` prefix is the canonical
  "this commit = this release" marker for `git log`, `git describe`, GitHub, and
  matches npm/PyPI/Cargo release-UI expectations.

If a tag `v<version>` already exists (rare — usually a failed earlier release),
`git tag` fails; ask via `AskUserQuestion`:
  - "Tag v<version> exists. Move it to the new commit, or fail?"
  - `"Move tag to new HEAD (--force)"` → `git tag -f v<version>`
  - `"Leave tag alone, don't tag this commit"`
  - `"Abort — the existing tag is correct, something is wrong with this release"`

### Step 8: Push the tag? (AskUserQuestion)

Pushing is a separate, user-confirmed action — never push automatically. Ask via
`AskUserQuestion`:
- question: "Push tag v<version> now?"
- options:
  - `"Push now"` → run `git push origin v<version>`. **If `.github/workflows/publish.yml`
    exists in the repo, the option description MUST state that pushing publishes
    the package to PyPI after the test job passes** (the tag push triggers the
    release workflow). Check for the file before rendering the options.
  - `"Keep local"` → do not push; the Step 9 report reminds the user how to push
    later (`git push origin v<version>`).

### Step 9: Report
```
✓ proj-a released as 1.1
  5 entries stamped (#12, #13, #15, #16, #17); attested: #16, #17; left unreleased: —
  changelog.md: [1.1] section prepended (older history preserved)
  Version-synced files: pyproject.toml, proj_a/__init__.py
  Commit: release: v1.1 (abc1234)
  Tag: v1.1 (pushed to origin) | v1.1 (local only — run `git push origin v1.1` to push)
```

The Tag line reflects the Step 8 choice: "pushed to origin" if the user chose
"Push now", otherwise "local only" with the push-later reminder.

## Rules
- NEVER release without user confirmation.
- NEVER skip the bump type question, and NEVER substitute a plain-text prompt
  for it — the bump type MUST be an AskUserQuestion (Step 2, Question 2). The
  pre-stamp structured choice is the last reversible point.
- NEVER call `release_changelog` without `entry_ids` or without an explicit
  `version`, and NEVER skip the capability probe.
- NEVER stamp an entry without proof in range — attest, correct, withdraw or
  leave it (Step 2). Only this skill creates attestation commits.
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
- Tag is **mandatory** — every release commit gets a `v<version>` tag (Step 7).
  A release without a tag is not a release — it's a commit that happens to bump
  `pyproject.toml`. Tags are the git-native marker for `git describe`,
  `git log --decorate`, GitHub's release UI, and `pip install <pkg>==<version>`.
- Pushing the tag to remote is NOT automatic — it is a user-confirmed
  `AskUserQuestion` (Step 8). When `.github/workflows/publish.yml` exists,
  "Push now" publishes to PyPI after the test job passes; say so in the option.
