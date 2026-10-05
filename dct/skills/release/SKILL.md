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
`release_changelog`, `get_release`, `advance_release`, `export_changelog`,
`update_changelog` and `delete_changelog` are deferred — load them all with
ONE ToolSearch call before Step 0:

```
ToolSearch("select:mcp__dct__list_projects,mcp__dct__count_unreleased_changelog,mcp__dct__list_changelog,mcp__dct__release_changelog,mcp__dct__get_release,mcp__dct__advance_release,mcp__dct__export_changelog,mcp__dct__update_changelog,mcp__dct__delete_changelog")
```

**Capability probe (mandatory, before anything else):** the loaded schema of
`release_changelog` MUST list an `entry_ids` parameter (and `candidate_sha` /
`checkout_root`), and `get_release` / `advance_release` MUST load. If not,
STOP: "The dct MCP server is older than this skill — restart it (or upgrade
dct) before releasing." An old server silently ignores unknown arguments and
would stamp EVERY unreleased entry.

The trailer format, the range / proof rules and the release-record contract
used below are defined ONCE in `docs/assistant-fallbacks.md` ("Changelog
proof: trailers and range", "`/release` fallback") — restated here briefly;
on divergence that doc wins.

## The release record

Every stamp writes a durable record (`get_release` reads it): version `V`, the
exact `entry_ids` (also `[]`), the pinned `candidate_sha`, the
`checkout_root`, `created_at` and a forward-only `status`
`stamped → files_written → committed → tagged → completed`. Each step that
finishes an effect calls `advance_release` right after verifying it. An
interrupted release is RESUMED from its record (see "Resume path") — never
restarted with a new bump.

## Project root, changelog file, version-sync script

- `checkout` = `git rev-parse --show-toplevel` of the CURRENT checkout.
- Same repository: `git -C <checkout> rev-parse --git-common-dir` and
  `git -C <project.path> rev-parse --git-common-dir` must resolve to the same
  directory (main checkout or any linked worktree). Otherwise this is not a
  checkout of the project — STOP.
- `root` (the project root in THIS checkout) = `checkout` + the project's
  `path` relative to `git -C <project.path> rev-parse --show-toplevel` — equal
  to `checkout` for a single-project repo, a subdirectory of it for a project
  registered inside a monorepo.
- `F` = `{root}/{changelog_path}` (`changelog_path` from `list_projects`,
  default `changelog.md` when the field is absent). Do NOT hardcode
  `changelog.md`. `dct project changelog-path <slug>` resolves against the
  registered path (the main checkout), so it is NOT the destination when the
  release runs in a linked worktree.
- Version-sync script = `{root}/scripts/dct-version-sync.sh`, always run with
  `root` as the working directory.

## Workflow

### Step 0: Identify the project and detect an unfinished release

This step runs FIRST — before the WIP auto-commit (Step 1) and before any
bump is chosen (Step 3). In ONE parallel message:
- `mcp__dct__list_projects()` — match CWD to a registered project → `slug`, `current_version`, `changelog_path`, `path`
- `git rev-parse --show-toplevel`, `git rev-parse --git-common-dir`, `git branch --show-current`

Resolve `checkout`, `root` and `F` (section above), then call
`mcp__dct__get_release(project)` (no version = the latest record that is not
`completed`):

- **A record** → an earlier release did not finish. Show it (`version`,
  `status`, `entry_ids`, `candidate_sha`, `checkout_root`, `created_at`) and
  ask via `AskUserQuestion`: `"Finish release v<version> (resume)"` /
  `"Stop"`. On resume follow **Resume path** below and do NOT continue with
  Step 1 — no WIP commit, no new bump.
- **No record** → the legacy check. With `CUR` = `current_version` set: if
  `git rev-parse -q --verify "refs/tags/v$CUR"` fails, or `F` exists without a
  line starting with `## [CUR]`, the state is AMBIGUOUS — an unfinished
  release from before records existed, or a release made without a tag or a
  file. Never guess. Reconstruct what is known — entries carrying `CUR`
  (`list_changelog(project, unreleased_only=false)`), a commit with subject
  `release: v$CUR` (`git log --format='%H %s' --grep='^release: v'`), the tag,
  the section — print it, and ask the owner: finish the missing effects by
  hand (each one confirmed before it is done), treat `CUR` as complete and
  continue, or stop. There is no tool that creates a record for an old
  release; say so.
- Otherwise (no record, tag and section present — or no `current_version`)
  → continue with Step 1.

### Step 1: Ensure a clean working tree (auto-commit WIP first)

Run `git status --porcelain` in the checkout.

**Clean (empty output)** → continue to Step 2.

**Dirty** → do NOT stop. The release commit (Step 8) must still contain ONLY the
changelog file + version-synced files, so auto-commit the working changes as
their OWN separate commit first, then continue. This preserves the reason the
gate existed — a clean release commit is a clean revert target — while removing
the manual interruption. The WIP simply lands in its own commit *below* the
release commit.

> Why a separate commit, not mixed in: mixing WIP code with a version bump
> pollutes `git log` and makes rollback risky. Committing the WIP first keeps the
> release commit changelog-only with zero friction.

Auto-commit procedure (same safety rules and the same ORDER as `/commit`:
stage → select the trailer → commit; never lose history, never leak secrets):

1. **Only genuinely-unsafe state stops the flow.** If a merge / rebase /
   cherry-pick is in progress, STOP and report — an auto-commit mid-operation
   is unsafe. Probe with
   `git rev-parse --git-path <marker>` for each of `MERGE_HEAD`,
   `rebase-merge`, `rebase-apply`, `CHERRY_PICK_HEAD` and test whether the
   printed path exists — never a literal `.git/...` path (in a linked worktree
   `.git` is a file, not a directory).
2. **Stage first, explicitly — never `git add -A` / `git add .`.** Stage
   tracked modifications and safe untracked files by path
   (`git add -- <path> …`). A path that is already partially staged (it shows
   in both `git diff --cached` and `git diff`) is a deliberate choice: keep
   that staging and commit it first — never re-add the whole file on top of
   it. EXCLUDE secrets and junk: anything matching `.env*`, `*.pem`, `*.key`,
   `id_rsa*`, `*.p12`, credential/token files, or large binaries. If such a
   file is dirty/untracked, leave it unstaged and note it in the Step 11
   report — do not commit it, do not stop. Split into multiple logical
   commits when the changes cleanly separate by file/concern (mirrors
   `/commit`); one commit is fine when they don't.
3. **Then select the trailer** for THAT commit exactly as `/commit` Step 5
   does, against the complete `git diff --cached` of the now-final index
   (uncertain rows — null branch or `branch_source='server'` — only after
   asking and confirming them with `update_changelog`).
4. **Message** — follow the `/commit` conventions: conventional-commit subject
   `<type>(<scope>): <description> (v<current_version>)` derived from the staged
   diff, with the `Changelog:` trailer from step 3 and the `Co-Authored-By`
   trailer. If the changes span too many concerns to summarize, use
   `chore: commit work in progress before release (v<current_version>)`.
5. **Commit WITHOUT another `git add`** (`git commit -m …` — no `-a`, no
   paths). If the pre-commit hook rejects, fix the underlying issue, re-stage,
   redo step 3 against the new staged diff (any index change makes the
   selection stale) and make a NEW commit (never `--amend`, never
   `--no-verify`).
6. Re-run `git status --porcelain`; it MUST now be empty (modulo any
   intentionally-skipped secret/junk files from step 2). Then continue to Step 2
   (which pins the candidate SHA AFTER these commits).

### Step 2: Gather state, pin the candidate, read the proof

After Step 1 leaves the tree clean, gather all state in a single parallel message:
- `mcp__dct__list_changelog(project, unreleased_only=true)` — ALL unreleased entries (every branch)
- `git status --porcelain` — confirm the tree is still clean going into the release
- `git rev-parse HEAD` → pin the candidate `SHA`; `git branch --show-current`

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
  prose never counts).
- Squashes lose trailers: a squash configured as "PR title and description"
  drops the commit bodies, and a local `git merge --squash` indents the
  original messages (an indented `    Changelog: 12` is not proof). Those
  entries show up without proof and need an explicit attestation — never
  assign them from the squashed history automatically.
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

If there are no unreleased entries at all, go straight to Step 3 Question 2
and the version-only branch of Step 4 — zero unreleased entries never block an
explicit version-only release (or stop if the user does not want one).

### Step 3: Decide entries without proof + bump type (AskUserQuestion)

This is the pre-stamp safety net — once Step 5 stamps, reverting requires
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
before Step 5 if the user asks.

**After every commit this skill creates (WIP in Step 1, attestation here):**
re-pin `SHA=$(git rev-parse HEAD)` and re-run the Step 2 scan before moving on.

**Question 2 — bump type.** The bump type MUST be an `AskUserQuestion` option —
do NOT substitute a plain-text prompt, and do NOT infer it from the user's
earlier words. The bump is effectively irreversible once Step 5 stamps, so the
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

Resolve all Question 1 decisions (and their re-scans) before Step 4.

### Step 4: Confirm the proven set
```
Release proj-a 1.1?
  Candidate: def5678 (feat/x)
  Stamped (proven): #12, #13, #15, #16, #17
  Left unreleased: —
  Version: 1.0 → 1.1
```
Wait for confirmation.

**Empty proven set** → ask explicitly: "No proven entries. Release version V
only (no changelog entries)?" A version-only release is a real release and
needs this explicit consent. If yes, Step 5 passes `entry_ids=[]` and Step 6
writes a version-only section. Tell the user the side effects: the
`since=last_release` anchor does not move (no entry gets `released_at`), and a
full `export_changelog` omits V. If no, stop.

### Step 5: Stamp version in DB (writes the release record)

Call
`mcp__dct__release_changelog(project, version=V, entry_ids=[<proven ids>], candidate_sha=SHA, checkout_root=checkout)`
— ALWAYS an explicit `version` (never `bump_type`) and ALWAYS an explicit
`entry_ids` list (`[]` for a version-only release; an omitted `entry_ids`
would stamp every unreleased entry). In one transaction this:
- Stamps exactly those entries with `V` (STRICT — any invalid id fails the
  whole call and nothing is written).
- Updates the project's `current_version` field to `V`.
- Writes the release record (status `stamped`) with that exact set, the
  pinned `candidate_sha` and the `checkout_root`.
- Is a no-op returning the existing record when repeated with the same
  set; a different set for a recorded `V` is refused.

Check the answer before continuing — STOP and report on any mismatch or
`{"error": ...}`:
- `mode` is `"selected"` (or `"version_only"` for `[]`), `entry_ids` equals
  the selection, and `released` (plus `already_released`) accounts for every
  selected id;
- `release` is a record whose `entry_ids` equal the selection;
- report the requested `V` and the returned `current_version` SEPARATELY.
  `current_version` ≠ `V` means a historical no-op (an older recorded
  release): STOP — never run version-sync or create a tag for it.
  `release` null means a legacy no-op without a record: STOP and handle it
  like the Step 0 legacy check.

Entries left unreleased keep counting in `count_unreleased_changelog` — that
is expected.

### Step 6: Write the new version's section into the changelog file

`F` (section "Project root, changelog file, version-sync script") is an
**append-only archive**. /release ADDS the just-released version's section to
the top of the existing file — it does NOT regenerate the whole file from the
DB.

> **Why append, not regenerate:** a full `export_changelog`
> renders the file from DB rows only. For a project that adopted dct mid-life,
> the DB holds only post-adoption versions, so overwriting `changelog.md`
> silently DROPS every pre-adoption section (one such project lost 18 versions
> / ~536 lines this way). An operation that only ever inserts cannot lose history.

**One contract for bootstrap, an existing file and a resume:** afterwards the
section `## [V]` appears in `F` EXACTLY once, every earlier section is
unchanged, and the release date is the ORIGINAL one (an exported section
carries the stamp date; a version-only section uses the record's
`created_at` date — never "today" on a resume).

**The section.** `section = mcp__dct__export_changelog(project, version=V)`.
- `isinstance(section, dict)` → an error: do NOT touch F, report it, stop (the
  DB is stamped and recorded — fix the export error and resume).
- `section == ""` → V has no entries. For a version-only release this is
  expected: use
  `section = "## [V] - YYYY-MM-DD\n\n- Version-only release — no changelog entries.\n"`
  with the record's `created_at` date. Otherwise it is unexpected after
  Step 5: STOP and report rather than writing an empty section.

**Case A — F does not exist (bootstrap / from-scratch project):**
Call `mcp__dct__export_changelog(project, include_unreleased=false)` and write
the full result to F. The full export omits version-only releases: when V is
version-only, insert its `section` directly below the `# Changelog` header.
A bootstrap may recreate many history sections at once, so check only that a
line starting with `## [V]` occurs exactly once (`grep -c '^## \[V\]'` = 1).

**Case B — F already exists (the normal path):**

1. In ONE parallel message, render the section (above) AND `Read(F)`.
2. Count F's existing version-section markers — **lines that start with** `## [` (`grep -c '^## \['`) — call it `N`. Anchor to line start; an entry body may contain `## [` mid-text and MUST NOT be counted.
3. **Idempotency guard:** if a **line starting with** `## [V]` already
   appears in F (anchored — `grep -c '^## \[V\]'`): exactly once → the
   section is already archived (a resume) — do NOT write, continue to Step 7;
   more than once → a contradiction: STOP and report.
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

### Step 7: Sync version-bearing files, then record `files_written`

Only when the Step 5 `current_version` equals `V` (never for a historical
version). If `{root}/scripts/dct-version-sync.sh` exists, run it with `root`
as the working directory:
```bash
cd <root> && bash scripts/dct-version-sync.sh <version>
```
A non-zero exit STOPS the release — report it; the record stays `stamped`, and
the next `/release` resumes after the script is fixed.

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

If the sync script doesn't exist, skip the sync — DB is the source of
truth. Inform the user so they can add one if they have build files that
need sync.

Then record the step: `mcp__dct__advance_release(version=V, status="files_written", project)`.

### Step 8: Release commit, then record `committed`

Stage ONLY the artifacts touched by Steps 6 and 7 (the changelog file `F` and
any files modified by the sync script — check `git status --porcelain`), with
explicit paths, then commit:

```bash
git add -- <F> <version-synced-files> && git commit -m "release: v<version>"
```

- The commit message is the **literal** `release: v<version>` — no type, no
  scope, no version suffix, no `Changelog:` trailer. The dct global hook
  recognizes this prefix and bypasses the changelog-presence check.

Then pin the release commit in the record:
`mcp__dct__advance_release(version=V, status="committed", project, candidate_sha=$(git rev-parse HEAD))`.

### Step 9: Tag the release commit, then record `tagged`

```bash
git tag v<version>
```

The tag is **mandatory** — the `v<version>` prefix is the canonical "this
commit = this release" marker for `git log`, `git describe`, GitHub, and
matches npm/PyPI/Cargo release-UI expectations.

If a tag `v<version>` already exists: pointing at the release commit
(`git rev-parse "v<version>^{commit}"` = the Step 8 SHA) → it matches, skip;
pointing elsewhere → a contradiction: STOP and report. Move a tag
(`git tag -f`) only on the owner's explicit instruction.

Then `mcp__dct__advance_release(version=V, status="tagged", project)`.

### Step 10: Push the tag? (AskUserQuestion), then record `completed`

Pushing is a separate, user-confirmed action — never push automatically. Ask via
`AskUserQuestion`:
- question: "Push tag v<version> now?"
- options:
  - `"Push now"` → run `git push origin v<version>`. **If `.github/workflows/publish.yml`
    exists in the repo, the option description MUST state that pushing publishes
    the package to PyPI after the test job passes** (the tag push triggers the
    release workflow). Check for the file before rendering the options.
  - `"Keep local"` → do not push; the Step 11 report reminds the user how to push
    later (`git push origin v<version>`).

Then `mcp__dct__advance_release(version=V, status="completed", project)`.

### Step 11: Report
```
✓ proj-a released as 1.1 (requested 1.1 · current_version 1.1)
  5 entries stamped (#12, #13, #15, #16, #17); attested: #16, #17; left unreleased: —
  changelog.md: [1.1] section prepended (older history preserved)
  Version-synced files: pyproject.toml, proj_a/__init__.py
  Commit: release: v1.1 (abc1234)
  Tag: v1.1 (pushed to origin) | v1.1 (local only — run `git push origin v1.1` to push)
  Release record: completed
```

The Tag line reflects the Step 10 choice: "pushed to origin" if the user chose
"Push now", otherwise "local only" with the push-later reminder.

## Resume path (an unfinished record from Step 0)

Finish the recorded release `V` — never a new one. Walk the effects in order:
verify each against reality, SKIP an effect that exists and matches, perform
a missing one, STOP and report on ANY contradiction, and call
`advance_release` after each verified or finished effect. A resume may finish
the missing steps even though nothing is left to stamp (`released` = 0):
the stamp and the record were written together.

1. **Checkout and version.** Run the Step 1 unsafe-state probe (merge /
   rebase / cherry-pick → STOP). `checkout` must equal the record's
   `checkout_root` (when set) — else STOP: the resume must run in that
   checkout. `current_version` must equal `V` — an older version than the
   current one is never resumed here (no version-sync, no tag for a
   historical version): STOP and report the requested `V` and
   `current_version` separately.
2. **DB stamp.** The live entries carrying `V`
   (`list_changelog(project, unreleased_only=false)`) must be EXACTLY the
   record's `entry_ids` (none for a version-only release). Otherwise STOP.
3. **Files** — status `stamped`: HEAD must still be the record's
   `candidate_sha` (later commits on top → STOP). Write or verify the section
   per Step 6 (exactly once, original date), run the version sync per Step 7
   (a failing script stops), then
   `advance_release(version=V, status="files_written", project)`. Status
   `files_written` or later: the section must be present exactly once, else
   STOP.
4. **Release commit** — status below `committed`: HEAD = `candidate_sha` →
   make it per Step 8; HEAD already a commit whose subject is exactly
   `release: v<V>` and whose parent is `candidate_sha` → it exists, skip;
   anything else → STOP. Then
   `advance_release(version=V, status="committed", project, candidate_sha=<release commit SHA>)`.
   Status `committed` or later: the record's `candidate_sha` must be a commit
   with subject `release: v<V>` that is HEAD or an ancestor of HEAD, else
   STOP.
5. **Tag** — missing → `git tag v<V> <release commit>`; present and pointing
   at the release commit → skip; pointing elsewhere → STOP. Then
   `advance_release(version=V, status="tagged", project)`.
6. **Push + complete** — Step 10, then Step 11 (say it was a resume and
   which effects were skipped as already present).

## Rules
- NEVER release without user confirmation.
- ALWAYS check `get_release(project)` in Step 0 — before the WIP auto-commit
  and before choosing a bump. An unfinished record is resumed, never
  replaced by a new version.
- NEVER skip the bump type question, and NEVER substitute a plain-text prompt
  for it — the bump type MUST be an AskUserQuestion (Step 3, Question 2). The
  pre-stamp structured choice is the last reversible point.
- NEVER call `release_changelog` without `entry_ids` or without an explicit
  `version`, and NEVER skip the capability probe.
- NEVER run version-sync or create a tag for a version that is not the
  current one (`current_version` ≠ `V`); report both values.
- A version-only release (`entry_ids=[]`) only with the owner's explicit
  consent (Step 4); a failing version-sync script stops the release.
- NEVER stamp an entry without proof in range — attest, correct, withdraw or
  leave it (Step 3). Only this skill creates attestation commits.
- If `current_version` is not set, ask for explicit version instead of bump type.
- `changelog.md` is an **append-only archive**: /release only PREPENDS the new
  version's section (Step 6 Case B), it does NOT regenerate the whole file.
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
  code. Step 1 guarantees this by auto-committing any WIP as a SEPARATE commit
  first, so the release commit stays a clean revert target.
- Tag is **mandatory** — every release commit gets a `v<version>` tag (Step 9).
  A release without a tag is not a release — it's a commit that happens to bump
  `pyproject.toml`. Tags are the git-native marker for `git describe`,
  `git log --decorate`, GitHub's release UI, and `pip install <pkg>==<version>`.
- Pushing the tag to remote is NOT automatic — it is a user-confirmed
  `AskUserQuestion` (Step 10). When `.github/workflows/publish.yml` exists,
  "Push now" publishes to PyPI after the test job passes; say so in the option.
