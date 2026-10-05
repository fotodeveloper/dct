# Assistant fallback workflows

Skills in `dct/skills/` are **the preferred** way to drive dct from Claude Code.
But skill names can be renamed, disabled per-session, or missing in future
versions. Do not couple logic to skill existence — the workflows below
describe what each skill does so an assistant can perform the steps manually
via MCP tools when the named skill is unavailable.

**Write ops return a minimal ack**, not the full record — the created/updated
`id` plus a few post-state scalars. **Remember the `id`** returned by
`create_item` / `add_checkpoint` / `create_handoff` (etc.); you need it for
every follow-up call. Fetch full context deliberately: `get_item(id)` (fields,
notes, checkpoints, progress) or `get_handoff(id)` (untruncated prompt +
context_summary). `list_handoffs` returns 200-char prompt previews — use
`get_handoff(id)` for the full text.

## `/pickup` fallback

The skill is TWO-PHASE — a read, a dialog, then (only if the user says so)
the consume. Without the skill:

1. `pickup_work(project="", consume="false")` — a PURE READ, no `consumed_by`
   needed and nothing written. `handoff.latest` carries the newest unconsumed
   handoff with its FULL prompt; `handoff.others` are 200-char previews;
   `handoff.pending_count` says how many wait.
2. Show the prompt and let the USER decide: consume and follow / follow but
   keep it unconsumed (audit) / start fresh — and, with several handoffs,
   which one.
3. On "consume":
   `pickup_work(project="", consume="true", handoff_id=N, include="none", consumed_by="<agent> @ <slug> <iso_utc>Z")`
   — `include="none"` skips the snapshot re-read (step 1 already has it).
4. Read the next step from `sprint.checkpoints` (first `pending` row of the
   chosen item); honor `gates` and `decisions` before acting.
5. CLI equivalent: `dct pickup [--json]` (its `--consume` default is `never`),
   `dct pickup --consume always --handoff-id N` for the write.

Never auto-pick among several handoffs; never call `consume_handoff` here —
`pickup_work` owns the consume. `consume=""` (auto) and `--consume always`
belong to AUTONOMOUS flows (CI, unattended agents) where nobody can be asked.
If a parallel session wins the race, a server-selected consume comes back
`consumed: false` (proceed with the fresh snapshot); an explicit `handoff_id`
errors "not found or already consumed" — report it, don't retry in a loop.

## `/handoff` fallback

The skill is preflight + dialogs + one atomic call. Without the skill:

1. `get_context_snapshot(project="")` — sprint/gates/decisions feed the draft.
2. Confirm the membership change + prompt text with the user. Sprints are
   MULTI-SESSION (ADR #57): the default is "sprint untouched", the normal edge
   is ADDITIVE (add discovered items / cut scope), and a full replace is
   conscious replanning.
3. `handoff_work(prompt=..., created_by="<agent> @ <slug> <iso_utc>Z", project="",
   item_ids="__keep__" (default) | "22,21,6" (replace), add_item_ids="22,6",
   remove_item_ids="7", sprint=<id-or-code when several active>, scope=...)` —
   one transaction; a failed sprint write leaves no handoff. Replace and the
   additive pair are mutually exclusive. **Never `item_ids=""`** — an empty
   replace errors; emptying a sprint is `clear_sprint`'s job. Unknown item ids
   abort everything (strict), removing a TERMINAL member errors (resolved work
   is the sprint's history), and an ambiguous / membership-less `sprint`
   selector errors too.
4. "No prompt" case: `handoff_work` requires a prompt — with ≤ 1 active sprint
   fall back to `set_sprint(project, item_ids=...)`; with ≥ 2 active sprints
   `set_sprint` errors (current-sprint writes refuse to guess), so target the
   chosen sprint via `assign_item_to_sprint(item_id, sprint_id)` /
   `remove_item_from_sprint(item_id, sprint_id)`.
5. CLI equivalent: `dct handoff create --prompt-file - --add-items 22,6 --scope g1`
   (`--remove-items` cuts scope, `--items` replaces; omit all three to keep the
   sprint).

## Changelog proof: trailers and range (shared by `/commit` and `/release`)

A changelog entry is **proven** for a release when a commit in the release
range names its id in a `Changelog:` trailer. `/commit` writes the trailers,
`/release` reads them; both use exactly these rules. They are implemented
ONCE in code — `dct/workflows/release/proof.py` (the rules) and
`dct/workflows/release/plan.py` (range, reverts, squash, provenance) — and
computed read-only by two commands (schemas:
[release-workflow.md](release-workflow.md)):

- `dct release plan [-P slug] [--version V | --patch|--minor|--major] --json`
  — the release plan of the current checkout: range, proof and provenance
  per entry, unproven entries with a reason, foreign citations, reverts,
  squash proposals, index state, version-file contract. No side effects.
- `dct clog candidates [-P slug] --json` — `/commit`'s candidates:
  `confirmed` vs `uncertain`, minus the ids the range already proves, plus
  the staged file list.

The prose below describes what they compute; when a client cannot run the
CLI, apply it by hand.

**Trailer format (written by `/commit`).** One line `Changelog: <id>, <id>` —
ids ascending, separated by comma + space — in the final trailer block of the
message, before `Co-Authored-By:`. Release commits (`release: vX.Y.Z`) carry
no `Changelog:` trailer.

**Range `R` for a target commit `SHA` (K-71).** The base is the nearest
release tag that is an ancestor of the target: among the tags reachable
from `SHA` whose name follows the version contract (`v1.5.0`, `1.5`, …),
the one with the HIGHEST version (ties: the exact `v<current_version>`,
then a `v`-prefixed name). `R = <base>..$SHA`. When the base is not the tag
of the project's `current_version` (e.g. a maintenance branch cut from an
older release), the plan flags `confirm_required` — show the base and let
the owner confirm it. Without any ancestor release tag:
`R = --since=<oldest unreleased entry's created_at minus 1 day> $SHA`, and
WARN that the boundary is approximate. Backports of a LOWER version stay
refused by the never-rewind rule.

**Reading proof for `R`** — per commit, the union of:
1. git's trailer parser:
   `git log --format='%(trailers:key=Changelog,valueonly,separator=%x2C)' R`;
2. a strict full-body scan: `git log --format=%B R`, keeping ONLY lines that
   match `^Changelog: [0-9]+(, [0-9]+)*$` exactly, in column 0. This covers
   squash merges that keep the original commit messages inside the body;
   quoted, bulleted or indented prose (`> Changelog: 12`, `- Changelog: 12`,
   `    Changelog: 12`) never counts — and a column-0 line inside an example
   DOES count, so quote examples.
A cited id that is not an unreleased entry of the project (unknown, another
project's, already released, deleted) is a foreign citation: reported,
never stamped. An entry cited only BELOW the base tag is `below_range`.

**Reverts (K-68).** A commit whose subject starts `Revert "` (or `Reapply "`,
git's name for a revert of a revert) and that carries git's standard line
`This reverts commit <sha>.` reverts `<sha>`: when `<sha>` is in `R`, the
ids it proves lose that proof (reason `reverted`, with the revert SHA); a
revert of a merge also removes the proof of the commits the merge brought
in. A revert of the revert restores the proof (chains alternate). A revert
whose target is outside `R` is reported, never guessed. `/commit` writes
reverts with git's standard message (`git revert`) so this detection works.

**Squash merges lose trailers (K-69).** A squash configured as "PR title and
description" drops the commit bodies, and a LOCAL `git merge --squash` indents
every original message by four spaces in the squash commit's body — neither
read above sees an indented `    Changelog: 12`. Those entries end up WITHOUT
proof. The plan detects squash-looking commits (subject ending `(#N)`,
git's "Squashed commit of the following:" message, indented `Changelog:`
lines) and groups the unproven entries linked to them — by an indented id,
by the entry's client-confirmed branch named in the squash message, or by
that branch's own proving commits being absent from the candidate's
ancestry — into ONE proposed collective attestation per source branch
(`attestation_groups`). A proposal only: they need an explicit attestation
(below), never an automatic assignment from the squashed history.
Recommended merge modes keep the proof: a merge commit, a rebase merge, or
a hosted squash that keeps the commit messages.

**Provenance (K-70).** For every proven entry the plan reports its branch
metadata, the proving commit(s), the merge commit on the candidate's
first-parent line that brought it in, and the PR number from a
`Merge pull request #N …` or `… (#N)` subject — the `proof` map the release
record stores.

**Detached HEAD.** `git branch --show-current` prints nothing: there is no
branch to filter on, and an empty `branch` filter means "every entry of every
branch". Never fall into that unfiltered list by accident — narrow the
candidates by `worktree=<toplevel>` instead, select nothing automatically, and
ask the owner which entries (if any) the commit carries.

**Server-detected entries are unconfirmed.** A row with
`branch_source='server'` names the MCP server's own checkout (the session
start directory), not where the agent worked. It is never attached as found:
confirm or correct it first with `update_changelog(entry_id, branch=<current>,
worktree=<toplevel>)` against the actual checkout (both fields — that makes it
`branch_source='client'`). The commit hook's count is only a gate (are there
entries at all?), never proof that an entry belongs to the commit — the
trailer set is always chosen from the final staged diff.

**Attestation commit (created only by `/release`).** The only way to add proof
after the fact: once the owner confirms the entries' changes are in the
candidate, an empty commit on the candidate branch whose last paragraph is the
trailer block (`Changelog:` first, then the usual `Co-Authored-By:`):

```bash
git commit --allow-empty -m "$(cat <<'MSG'
chore: attest changelog entries

Changelog: 12, 15
MSG
)"
```

## `/release` fallback

A release is a sequence of effects — DB stamp → changelog file + version
files → release commit → tag — and the server keeps a durable **release
record** of it (`get_release` / `advance_release`): the version `V`, the
exact `entry_ids` (also `[]`), the pinned `candidate_sha`, the
`checkout_root`, the creation date and a forward-only status
`stamped → files_written → committed → tagged → completed`. Every step below
that finishes an effect advances the record; an interrupted release is
RESUMED from it, never restarted with a new bump.

**Project root and changelog file (shared by both paths).** `checkout` =
`git rev-parse --show-toplevel` of the CURRENT checkout. `root` = the project
root in THIS checkout = `checkout` + the project's `path` relative to the
toplevel of the checkout that holds `path` (`git -C <path> rev-parse
--show-toplevel`; equal to `checkout` for a single-project repo, a
subdirectory of it for a monorepo project). Before using it confirm that
`git -C <checkout> rev-parse --git-common-dir` and `git -C <path> rev-parse
--git-common-dir` resolve to the same directory — otherwise this is not a
checkout of the project's repository: STOP. The changelog file is
`F = {root}/{changelog_path}` (`changelog_path` from `list_projects`, default
`changelog.md`) and the version-sync script is
`{root}/scripts/dct-version-sync.sh`, run with `root` as its working
directory. (`dct project changelog-path <slug>` resolves against the
registered path — the main checkout — so it is NOT the destination when the
release runs in a linked worktree.)

**Changelog-file contract (bootstrap, existing file, resume).** After the
file step the section `## [V]` appears in `F` EXACTLY once, every earlier
section is byte-for-byte unchanged, and a resumed release keeps the date of
the original release (the record's `created_at` date; an exported section
already carries the stamp date). The section is
`export_changelog(project, version=V)`, or — for a version-only release,
where that export is `""` — `## [V] - <date>` followed by the line
`- Version-only release — no changelog entries.`:
- `F` missing (bootstrap): write `export_changelog(project,
  include_unreleased=false)`; the full export omits version-only releases, so
  when `V` is version-only insert its section below the `# Changelog` header
  (as the top section). A bootstrap may recreate many sections at once —
  check only that `## [V]` occurs exactly once.
- `F` exists: count the lines starting with `## [` (`N`). If a line starting
  with `## [V]` is already there, do not write (resume: verify it instead).
  Otherwise PREPEND the section directly below the `# Changelog` header —
  NEVER regenerate the file (a full-DB export drops pre-adoption history) —
  and re-count: exactly `N + 1`, else restore `F` and STOP.
- `## [V]` more than once, or a status at/after `files_written` with the
  section missing → a contradiction: STOP.

**Normal path:**

1. **Capability probe.** Load the schemas (`list_projects`,
   `count_unreleased_changelog`, `list_changelog`, `release_changelog`,
   `get_release`, `advance_release`, `export_changelog`, `update_changelog`,
   `delete_changelog`). The loaded `release_changelog` schema MUST list
   `entry_ids`, `candidate_sha` and `checkout_root`, and `get_release` /
   `advance_release` MUST exist; otherwise STOP: "The dct MCP server is older
   than this skill — restart it (or upgrade dct) before releasing." (an old
   server silently ignores unknown arguments and would stamp every entry).
2. Detect current project: match CWD against `mcp__dct__list_projects()` results, take longest path prefix; resolve `checkout`, `root` and `F` as above.
3. **Unfinished release first — BEFORE any WIP commit and BEFORE any bump.**
   `get_release(project)` (no version = the latest record that is not
   `completed`). A record → follow the **resume path** below instead of this
   one. No record → the **legacy check**: with `CUR` = `current_version` set,
   if the tag `v$CUR` is missing or `F` exists without a `## [CUR]` line, the
   state is ambiguous (a release from before records existed, or one that
   never happened). Do NOT guess: reconstruct what is known (entries carrying
   `CUR` in `list_changelog(project, unreleased_only=false)`, a commit with
   subject `release: v$CUR`, the tag, the section) and ask the owner how to
   proceed — finish the missing effects by hand (each one confirmed), treat
   it as complete, or stop. There is no tool that creates a record for an
   old release.
4. **Unsafe git state stops the flow:** a merge / rebase / cherry-pick in
   progress — resolve each marker with `git rev-parse --git-path MERGE_HEAD`
   / `rebase-merge` / `rebase-apply` / `CHERRY_PICK_HEAD` (worktree-safe) and
   test whether the path exists. A dirty tree is NOT a stop: commit the WIP
   first as its own commit(s), each following the `/commit` order — stage the
   chosen paths (keep deliberate partial staging), THEN select the trailer
   against the complete `git diff --cached`, THEN `git commit` without
   another `git add`.
5. Pin the candidate `SHA=$(git rev-parse HEAD)`, compute `R`, read the proof
   (rules above), and split `list_changelog(project, unreleased_only=true)`
   into entries **with proof in range** and **without proof**.
6. For EACH entry without proof (entries with a null branch included) the
   owner chooses: (i) **attest** — confirm the change is in the candidate,
   then create the attestation commit; (ii) **correct** it
   (`update_changelog`); (iii) **withdraw** it (`delete_changelog`); (iv)
   **leave it unreleased**. There is no "include without proof" option.
7. After every commit the flow itself creates (WIP, attestation): re-pin `SHA`
   and re-scan `R`.
8. Ask via AskUserQuestion: `patch` | `minor` | `major` | explicit version.
   Compute `V` ONCE from that choice; every retry reuses the same `V`.
9. Confirm the proven set. When it is empty, ask explicitly whether to release
   the version only (no entries) — a version-only release needs that explicit
   consent; zero unreleased entries never block it.
10. `mcp__dct__release_changelog(project, version=V, entry_ids=[<proven ids>], candidate_sha=SHA, checkout_root=checkout)`
    — ALWAYS an explicit `version` (never `bump_type`) and ALWAYS an explicit
    `entry_ids` list (`[]` for a version-only release; an omitted `entry_ids`
    stamps every unreleased entry). Check the answer: `mode`, `entry_ids` and
    `released` (plus `already_released`) match the selection, `release` is a
    record with exactly those `entry_ids`, and `current_version` equals `V`.
    Report the requested `V` and the returned `current_version` separately.
    `current_version` ≠ `V` (a historical re-run) → STOP: never run
    version-sync or create a tag for it. `release` null (a legacy no-op) →
    STOP and treat it as the legacy check in step 3. STOP on any mismatch or
    `{"error": ...}`.
11. Changelog file: write the section per the contract above (`""` from the
    single-version export is expected only for a version-only release; an
    `{"error": ...}` → STOP — the DB is stamped, fix the export and resume).
12. If `{root}/scripts/dct-version-sync.sh` exists, run
    `bash scripts/dct-version-sync.sh V` with `root` as the working directory.
    A non-zero exit STOPS the release (resume after fixing the script). This
    bumps `pyproject.toml`/`Cargo.toml`/`package.json`/`tauri.conf.json`/etc.
    per the project-specific template. Then
    `advance_release(V, "files_written", project)`.
13. Stage the changelog file `F` + any files modified by the sync script (explicit paths); commit with literal message `release: vX.Y.Z` — no scope, no description, no suffix, no `Changelog:` trailer. Then
    `advance_release(V, "committed", project, release_commit_sha=$(git rev-parse HEAD))`
    — the record now pins the release commit.
14. **Tag** the release commit: `git tag vX.Y.Z`; then
    `advance_release(V, "tagged", project)`. A tag `vX.Y.Z` that already
    points elsewhere is a contradiction: STOP and report — move a tag only on
    the owner's explicit instruction.
15. Tell the user the tag is **local only** — pushing to remote is a separate
    step they must confirm (`git push origin vX.Y.Z`). Finish with
    `advance_release(V, "completed", project)`.

**Resume path** (a record from `get_release`, status neither `completed`
nor `abandoned`; the owner may instead abandon it —
`abandon_release(V, reason, confirm=true, project)`, which keeps the record,
the stamps and `current_version`): show
the record (`version`, `status`, `entry_ids`, `candidate_sha`,
`checkout_root`, `created_at`) and get the owner's consent to finish it.
Then verify each effect against reality, in order, SKIP an effect that
exists and matches, perform a missing one, STOP on any contradiction, and
advance the record after each verified or finished effect:
1. Checkout: `checkout` must equal the record's `checkout_root` (when set),
   and `current_version` must equal the record's `version` — a release older
   than the current version is never resumed here (no version-sync, no tag
   for a historical version): STOP and report both versions.
2. DB stamp: the live entries carrying `V` (`list_changelog(project,
   unreleased_only=false)`) are EXACTLY the record's `entry_ids` (none for a
   version-only release). Nothing to re-stamp: the stamp and the record are
   written together.
3. Files (status `stamped`): HEAD must still be the record's `candidate_sha`
   (later commits → STOP); write or verify the changelog section per the
   contract (date preserved), run the version-sync script as in step 12
   (a failure stops), then `advance_release(V, "files_written", project)`.
   Status already `files_written` or later: the section must be present
   exactly once (else STOP).
4. Release commit (status below `committed`): HEAD = `candidate_sha` → make
   the release commit as in step 13; HEAD already a commit with subject
   exactly `release: vV` whose parent is `candidate_sha` → it exists, skip;
   anything else → STOP. Then
   `advance_release(V, "committed", project, release_commit_sha=<release commit SHA>)`.
   Status `committed` or later: the record's `release_commit_sha` must be a commit
   with subject `release: vV` that is HEAD or an ancestor of HEAD (else STOP).
5. Tag: missing → `git tag vV <release commit>`; present and pointing at the
   release commit → skip; pointing elsewhere → STOP. Then
   `advance_release(V, "tagged", project)`.
6. Step 15 (push question), then `advance_release(V, "completed", project)`.
   A resume may finish these steps even though `released` would now be 0 —
   nothing is re-stamped, only the missing effects are completed.

## `/commit` fallback

The order is fixed: **stage → select → commit**. The `Changelog:` trailer
attests what the commit CONTAINS, so it is chosen only after the index holds
exactly what will be committed, and nothing is staged after the choice.

1. Pre-flight: run in parallel — `git status`, `git diff --cached --stat`, `git diff --stat`, `git log --oneline -5`, `git branch --show-current`, `git rev-parse --show-toplevel`.
2. Detect project via CWD.
3. `mcp__dct__count_unreleased_changelog(project, branch=<current>, include_unbranched=true)` (detached HEAD → project-wide count, no `branch` — a gate only, never a candidate list) — if 0 and non-exempt files staged, prompt user to run `/clog` or add entry inline via `mcp__dct__add_changelog`. Exempt paths decide only whether an entry is REQUIRED; attaching an existing matching entry is always allowed. The count is only this gate — never proof that an entry belongs to the commit.
4. Determine commit type + scope + description. Read `current_version` from project.
5. **Stage first.** Stage the chosen files explicitly, path by path:
   `git add -- <path> <path>`. Never `git add -A` / `git add .`. Never stage `.env`, credentials, binaries. A path that is ALREADY partially staged (it shows in both `git diff --cached` and `git diff`) is a deliberate choice — leave it as it is; never re-add the whole file unless the owner asks for its unstaged hunks too.
6. **Then select the trailer** against the complete staged diff (`git diff --cached`) — the index is now final. Candidates = `mcp__dct__list_changelog(project, unreleased_only=true, branch=<current>, include_unbranched=true)` rows whose `worktree` equals the current toplevel or is null, minus the ids already proven in `R` (target `SHA` = `HEAD`; rules above). Keep the candidates whose entry text / `source_file` describe what is staged; ask when unsure. An uncertain row — `branch` null or `branch_source='server'` — is never attached automatically: when it matches, ask, and confirm it first with `update_changelog(entry_id, branch=<current>, worktree=<toplevel>)`. Detached HEAD → `list_changelog(project, unreleased_only=true, worktree=<toplevel>)`, no automatic selection, ask (never an unfiltered list). No candidates, but unreleased entries of THIS worktree carry another branch → offer `update_changelog(entry_id, branch=<current>)`.
7. **Then commit — WITHOUT another `git add`.** HEREDOC: `git commit -m "<type>(<scope>): <description> (v<version>) …"`; the final trailer block is `Changelog: <ids>` (when any were selected) followed by `Co-Authored-By:`.
8. Verify: `git log --oneline -3`, report the commit hash and the attached changelog ids.
9. On hook rejection: fix the reported issue. ANY change to the index afterwards (re-staging, a new file from the fix) means the selection is stale: re-run step 6 against the new `git diff --cached`, then create a NEW commit (never `--amend`, never `--no-verify`).

## `/track` (create item) fallback

1. Determine project (CWD detect or ask).
2. Gather: item_type, title, `short_title` (the label: a 2-4 word English noun phrase naming what the item is about, ≤ 50 chars, no item number, no project name — e.g. "Windows hook paths"), priority, description, component, tags (optional).
3. Present draft to user (the label shown as "Label"), confirm.
4. `mcp__dct__create_item(item_type, title, project, short_title, ...)` — **REMEMBER the returned `id`**. The ack must echo `short_title` (null when none was stored); a MISSING key means the MCP server predates the field — report an unsupported (outdated) server contract and tell the user to restart/upgrade it. Do NOT retry the create (the item already exists) and do not claim the label was saved.
5. For feature/improvement items: break work into 3-7 checkpoints via `mcp__dct__add_checkpoint(item_id, text)` — each one concrete and actionable. **REMEMBER each returned `id`**.
6. Report to user: `#<id> <type>/<priority> [<short_title>] — <title>` plus checkpoint count.

## `/clog` (changelog entry) fallback

1. Determine project (CWD detect or ask).
2. Gather: category (added/changed/fixed/removed), entry text, component, source_file, source_lines, related_item_id (the last four optional).
3. In the agent's CURRENT working directory read `git branch --show-current` (empty = detached → omit `branch`) and `git rev-parse --show-toplevel` → `branch` / `worktree`. Pass them explicitly: the MCP server's directory is the session start directory, which may be another checkout.
4. Present draft, confirm.
5. `mcp__dct__add_changelog(category, entry, project, component, source_file, source_lines, related_item_id, branch, worktree)`.
6. If the ack carries `branch_source='server'` (or a `warning`), the entry is NOT confirmed: correct it with `update_changelog(id, branch=<current>, worktree=<toplevel>)`.
7. Report: `✓ clog #<id> [<category>] — <entry>`.

## `/track-resolve` (resolve/close item) fallback

1. Parse ID(s) from user input. Accept: `#42`, `42`, `ITEM-42`, `0042` — normalize via regex `^(#|ITEM-|item-)?0*(\d+)$`.
2. For each id: `mcp__dct__get_item(id)` — show current state, any unfinished checkpoints.
3. Determine resolution status: `resolved` (fixed/implemented) or `wont_fix` (scope decision).
4. Build resolution note: "Fixed in commit <hash>" if recent commit, or "Decision: <reason>" from context, else ask user.
5. If item has pending checkpoints, ask whether to `reject` them (preserved in ledger) or proceed anyway.
6. `mcp__dct__resolve_item(id, status, resolution)` — set terminal status.
7. Report: `✓ #<id> → <status>`.

These prose descriptions are the authoritative behavior. Skill files in
`dct/skills/` are shortcuts. If they diverge — fix the skill, not this doc.
