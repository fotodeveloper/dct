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
the release workflow reads them. The rules are implemented ONCE in code —
`dct/workflows/release/proof.py` (trailers, reverts, squash signals) and
`dct/workflows/release/plan.py` (range, provenance, candidates) — and
computed read-only by two commands whose JSON schemas, range rule (K-71),
revert rule (K-68), squash heuristic and its limits (K-69) and provenance
(K-70) are documented in [release-workflow.md](release-workflow.md):

- `dct release plan [-P slug] [--version V | --patch|--minor|--major]
  [--entries <ids> | --no-entries] --json` — the release plan of the
  current checkout; its `snapshot` / `snapshot_digest` is what the owner
  approves (K-80). No side effects.
- `dct clog candidates [-P slug] --json` — `/commit`'s candidates:
  `confirmed` vs `uncertain`, minus the ids the range already proves, plus
  the staged file list.

The essentials an assistant must know to write and read proof (the code is
authoritative):

- **Trailer format** (written by `/commit`): one line `Changelog: <id>, <id>`
  — ids ascending, comma + space — in the final trailer block, before
  `Co-Authored-By:`. Release commits (`release: vX.Y.Z`) carry none.
- **What counts** (per commit of the range): git's trailer parser
  (`%(trailers:key=Changelog,valueonly,separator=%x2C)`) plus every body line
  matching `^Changelog: [0-9]+(, [0-9]+)*$` in column 0. The strict format is
  RESERVED (K-66): a column-0 line in an example counts too, so quote or
  indent examples; quoted, bulleted and indented lines never count.
- **Range**: from the nearest release tag that is an ancestor of the
  candidate; the plan flags `confirm_required` when that is not the tag of
  `current_version` (show it, the owner confirms) and `approximate` when no
  ancestor tag exists (warn).
- **Reverts**: `/commit` writes reverts with git's standard message
  (`Revert "<subject>"` + `This reverts commit <sha>.`) — that is what the
  workflow detects; the reverted commit's ids lose their proof.
- **Squash merges lose trailers**: a title-only squash drops the bodies, a
  local `git merge --squash` indents them. The plan proposes ONE collective
  attestation per source branch (`attestation_groups`) — a proposal the
  owner confirms, never an automatic assignment. Recommended merge modes:
  [release-workflow.md](release-workflow.md#merge-policy).
- **Uncertain entries** (`branch_source='server'` — the MCP server's own
  checkout — or no provenance) and every entry on a **detached HEAD** are
  never attached as found: match them against the staged diff, ask, and
  confirm them with `update_changelog(entry_id, branch=<current>,
  worktree=<toplevel>)` (detached: `worktree` only) first. Never fall into an
  unfiltered list of every branch. The commit hook's count is only a gate
  (are there entries at all?), never proof that an entry belongs to the
  commit — the trailer set is always chosen from the final staged diff.
- **Attestation** — the only way to add proof after the fact — is the empty
  commit `chore: attest changelog entries` created by `dct release attest
  --entries <ids> --confirm` once the owner confirms the changes are in the
  candidate.

## `/release` fallback

The release runs in the local **dct CLI** (`dct release …`, the effects
layer); the assistant only runs the dialog. **Without the CLI** (an
MCP-only client) say plainly that a release is not possible from here: the
ledger tools (`release_changelog`, `get_release`, `advance_release`) record a
release but cannot verify the changelog file, the version files, the commit
or the tag. A project with its OWN release tool integrates as described in
[release-workflow.md](release-workflow.md#integrating-a-projects-own-release-tool).

The assistant never edits the changelog file, never runs version sync, never
commits, tags or pushes for the release and never calls `advance_release` —
the workflow performs, verifies and records every effect. Run every command
in the CURRENT checkout. Show long content as text first, then ask.

1. `dct release plan --json`. A missing / older CLI (unknown subcommand or
   flag) → STOP and say the CLI must be installed or upgraded. Exit 1 → show
   the reason, STOP.
2. `kind == "resume"` → the resume path below. `version.current` set and
   `dct release status --version <current> --json` → outcome `none` → offer
   the legacy path below.
3. `index` not clean → the owner commits the work in progress first with the
   `/commit` flow (its own commit), then re-plan. The workflow never resets,
   stashes or cleans.
4. Show the plan: range (`confirm_required` → the owner confirms the base;
   `approximate` → warn), proven entries grouped by branch / merge commit /
   PR, unproven entries with their `reason`, `attestation_groups`, foreign
   citations, reverts outside the range, warnings, the version-file
   contract.
5. The owner decides: the range base; each attestation group (attest
   together / decide each / keep unreleased); each remaining unproven entry —
   **attest**, **correct** (`update_changelog`, then re-plan), **withdraw**
   (`delete_changelog`) or **keep** unreleased (no "include without proof");
   the version (patch / minor / major / explicit, shown with the resulting
   number) — `V` computed ONCE.
6. Attest when chosen: `dct release attest --entries <ids> --confirm --json`,
   then `dct release plan --version V --json` again (the candidate changed).
7. Bind the decision: `dct release plan --version V --entries <ids> --json`
   (or `--no-entries`) — the snapshot of exactly this candidate, version
   and selection. Confirm the summary from THAT plan. No entries → explicit
   version-only consent (the `since=last_release` anchor does not move; a
   full export omits V). Then
   `dct release apply --version V --entries <ids> --plan-digest <snapshot_digest> --confirm --json`
   (or `--no-entries`). A refusal "differs from the approved plan" (HEAD,
   entries, selection, version or proof changed) → show it, plan again,
   ask again — never retry with a new digest unasked.
8. First release (`version_files.version_files` null): apply stops after the
   sync with `details.proposed_version_files`; show the list; only on the
   owner's yes `dct release resume --confirm-version-files --json`.
9. Read the result: `completed`; `noop` (already released — nothing done);
   `refused` (show `details`, only the listed `steps` happened; an
   unfinished `release` → resume path); `failed` (the version-sync script —
   fix it, then resume). Report the requested version
   (`details.requested_version`), the record's `version` and
   `details.current_version` separately. The tag is local; pushing is the
   owner's action.

**Resume path** — show the RECORDED release (`resume`: candidate, entry
set, proof, effect state) and, separately, today's HEAD
(`resume.head`, `checkout.matches_record`) and the entries added since
(`resume.new_unreleased` — not part of this release), then the owner
chooses: finish → `dct release resume --json` (another checkout or
HEAD is refused — run it in the record's `checkout_root`; the record is never
re-pinned); abandon → the owner's reason and explicit confirmation (the
record, stamps, `current_version` and effects stay, V stays taken) →
`dct release abandon --version V --reason "<reason>" --confirm --json`; or
stop.

**Legacy path** — `dct release reconstruct --version V --json` shows the
evidence (outcome `preview`); only after the owner confirms, the same
command with `--confirm` records it (no re-stamp). Refused = ambiguous
evidence: report it; never reconstruct by hand.

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
6. **Then select the trailer** against the complete staged diff (`git diff --cached`) — the index is now final. `dct clog candidates --json`: keep the `confirmed` candidates whose entry text / `source_file` describe what is staged (ask when unsure). `uncertain` candidates are matched too but attached only after the owner confirms them and `update_changelog(entry_id, branch=<current>, worktree=<toplevel>)` fixes both fields against the actual checkout — never automatically. Detached HEAD → no confirmed list, every entry of this checkout is uncertain: ask (confirm with `worktree` only). No match, but `other_branch` entries describe the change → offer `update_changelog(entry_id, branch=<current>)`. Ids in `already_proven` are never cited again. Without the CLI: `list_changelog(project, unreleased_only=true)` and the same classification by hand (client provenance of this branch + toplevel = confirmed; `branch_source` not `client` = uncertain).
7. **Then commit — WITHOUT another `git add`.** HEREDOC: `git commit -m "<type>(<scope>): <description> (v<version>) …"`; the final trailer block is `Changelog: <ids>` (when any were selected) followed by `Co-Authored-By:`. A trailer-format example inside the body is quoted (`> Changelog: 12`). A revert keeps git's standard message: `git revert --no-commit <sha>` stages it, the subject is `Revert "<original subject>"` and the body has `This reverts commit <full sha>.`.
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
