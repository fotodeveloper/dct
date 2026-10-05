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
layer) as ONE workflow from the first plan to the pushed tag; the assistant
runs the dialog and, on the owner's consent, the commit of the work in
progress (step 3). **Without the CLI** (an MCP-only client) say plainly that
a release is not possible from here: the ledger tools (`release_changelog`,
`get_release`, `advance_release`) record a release but cannot verify the
changelog file, the version files, the commit, the tag or the push. A
project with its OWN release tool integrates as described in
[release-workflow.md](release-workflow.md#integrating-a-projects-own-release-tool).

The assistant never edits the changelog file, never runs the version sync
itself, never commits, tags or pushes FOR THE RELEASE, never calls
`advance_release` and never uses the `clog release … --confirm` alias (the
dialog uses `apply`, like the skill) — the workflow performs (the release
commit through the repository's hooks, the tag, the push to the approved
target), verifies and records every effect. Run every command in the
CURRENT checkout. Show long content as text first, then ask. The steps are
the `/release` skill's, one for one (MCP where a tool suffices — the
corrections; the CLI where the effects need it):

0. **Probe and plan** — `dct release plan --json`. A missing / older CLI
   (unknown subcommand or flag) → STOP and say the CLI must be installed or
   upgraded (and the MCP server restarted). Exit 1 → show the reason, STOP.
1. **Executor** (K-85) — `dct project release-executor <plan.project>`.
   `own` → dct performs none of its effects (`prepare` / `apply` / `resume`
   refuse — `details.executor`): show the plan (step 4) as the shared proof,
   hand over to the project's own release tool
   ([integration](release-workflow.md#integrating-a-projects-own-release-tool))
   and STOP; never run a command stored anywhere in its place. `dct` →
   continue.
2. **Unfinished / legacy** — `kind == "resume"` → the resume path below.
   `version.current` set and `dct release status --version <current> --json`
   → outcome `none` → offer the legacy path below.
3. **Work in progress, committed in this workflow** (K-97 F1) — `index` not
   clean (staged, unstaged or `conflicted` paths, an untracked release
   artifact; other untracked files do not block and stay outside the
   release) → show it and ask: commit the work in progress now, or stop. A
   conflicted path or an operation in progress: the owner finishes or
   aborts it first. On consent, perform the commit HERE with the `/commit`
   procedure (the `/commit` fallback below; its version-bump redirect does
   not apply): the owner approves the staging plan (explicit paths) → with
   the index final, `dct clog candidates --json` and the `Changelog: <ids>`
   trailer chosen from THAT commit's complete staged diff (`uncertain`
   candidates only after the owner confirms them and `update_changelog(entry_id,
   branch=<current>, worktree=<toplevel>)` fixes them; several commits →
   one selection per commit from its own staged diff) → the owner approves
   the message → commit exactly what is staged, the repository's hooks run
   (never `--no-verify`, never `--amend`; a rejecting hook: fix, re-stage,
   select again, a NEW commit). Then `dct release plan --json` again and
   continue from step 2 — the new HEAD is the candidate steps 7-9 pin. The
   question never ends or splits the workflow; the workflow never resets,
   stashes or cleans.
4. **Show the plan** — range (`confirm_required` → the owner confirms the
   base; `approximate` → warn), proven entries grouped by branch / merge
   commit / PR, unproven entries with their `reason`, `attestation_groups`,
   foreign citations, reverts outside the range, warnings, the version-file
   contract (`version_files` null → learned at this first release, shown by
   prepare).
5. **The owner decides** — the range base; each attestation group (attest
   together / decide each / keep unreleased); each remaining unproven entry
   — **attest**, **correct** (`update_changelog`, then re-plan),
   **withdraw** (`delete_changelog`) or **keep** unreleased (no "include
   without proof"); the version (patch / minor / major / explicit, shown
   with the resulting number) — `V` computed ONCE.
6. **Attest** when chosen: `dct release attest --entries <ids> --confirm --json`
   (an empty commit through the repository's hooks), then `dct release plan
   --version V --json` again (the candidate changed); continue only when the
   attested ids are proven.
7. **Bind the decision** (K-80, H4) — the plan's default selection is every
   proven id, so re-plan with the OWNER'S selection:
   `dct release plan --version V --entries <ids> --json` (or
   `dct release plan --version V --no-entries --json`) and use THAT
   `snapshot_digest` (`D`). Confirm the summary from that plan. No entries →
   explicit version-only consent (#46; the `since=last_release` anchor does
   not move; a full export omits V).
8. **Prepare** (K-76 / K-97 F3, before anything is stamped):
   `dct release prepare --version V --entries <ids> --plan-digest <D> --json`
   (or `--no-entries`). The version-sync script runs once in an isolated
   temporary worktree (outside git: a temporary copy); the checkout does not
   change. Show `details.proposal.paths`, `details.checks`,
   `details.contract` (`change: true` = a first release or a changed
   version-file contract — say so explicitly; an empty list is a contract
   too), `details.sync` (exit code, output, `output_dropped_bytes` cut) and
   the push target `details.push_target` (remote, URL, branch, tag `v<V>` —
   the branch's upstream; `null` → the owner chooses `--push-remote <remote>
   --push-branch <branch>` or `--no-push`, never guessed). The owner
   approves THAT file list and THAT push target; keep `proposal_sha256`
   (`P`). `failed` → the script failed or timed out (124): fix it, prepare
   again. `refused` → the table below.
9. **Apply**, only on the owner's yes:
   `dct release apply --version V --entries <ids> --plan-digest <D> --proposal-digest <P> --confirm --json`
   (the upstream target), or with the owner's choice
   `dct release apply --version V --entries <ids> --plan-digest <D> --proposal-digest <P> --push-remote <remote> --push-branch <branch> --confirm --json`,
   or `dct release apply --version V --no-entries --plan-digest <D> --proposal-digest <P> --no-push --confirm --json`.
   It stamps, writes the changelog section and the approved version files,
   commits `release: v<V>` through the hooks, updates the real index, tags
   and pushes the commit and the tag atomically (K-97 F2). Never pass a
   digest the owner has not seen.
10. **Read the result and report** — `completed`; `noop` (already released —
   nothing done); `refused` (show `details.error`, act by the table; only
   the listed `steps` happened; an unfinished `release` → the resume path);
   `conflict` (exit 75, the table). Report the requested version
   (`details.requested_version`), the record's `version` and
   `details.current_version` separately, the steps (`stamp`, `changelog`,
   `version_files`, `commit`, `index`, `tag`, `push`, `complete`) and where
   it was pushed (`details.push_target`, or a local release).

Refusals (the keys are the workflow's own, `dct/workflows/release/effects.py`;
the same table as the skill's):

| details | what to tell the owner | next action |
| --- | --- | --- |
| `details.error` | every refusal: the reason, verbatim | the row of the other keys |
| `details.executor`, `details.consent` | the project (`executor: "own"`) or the record (`own`, or `consent: null` — stamped outside `apply`) is not executed by dct | hand over: the project's own tool, or the owner records the verified effects through the ledger path (`docs/release-workflow.md#executor-k-85`) — never this dialog; or abandon |
| `details.lock`, `details.holder` | another release command holds the repository's operation lock (pid, action, start) | wait until it ends, then run the same command once — never loop |
| `details.staged`, `details.unstaged`, `details.conflicted`, `details.untracked`, `details.in_progress` | the tree is not clean, a release artifact is untracked, or a merge / rebase / cherry-pick / revert runs | Step 3 (commit the work in progress here), or the owner finishes the operation; then re-plan |
| `details.unfinished` | an unfinished release exists | the Resume path |
| `details.unproven` | a selected id has no proof | Step 5 (attest / correct / withdraw / keep), then Step 7 |
| `details.approved_digest`, `details.derived_digest` | the context differs from the approved plan (HEAD, an entry added or edited, selection, version, proof) | show the derived `snapshot`; Step 7 again — a new approval |
| `details.mismatch`, `details.pending`, `details.approved_script`, `details.missing_blobs` | the approved proposal is void: another candidate / version / plan, a newer prepare, a changed script, pruned blobs | Step 8 again — a new approval |
| `details.changed` | before the stamp: a proposal file changed since prepare (approval void); on resume: a release artifact differs from the recorded manifest, or a hook changed the work tree after the commit | prepare again (Step 8); on resume the owner restores the files, then resume — or abandon |
| `details.checkout_changed`, `details.sync`, `details.path` | prepare: the script wrote outside its isolated worktree, failed / timed out, touched the changelog, or broke a version file's contract | STOP; the owner fixes the script, then Step 8 |
| `details.proposal`, `details.contract` | (the `clog release … --confirm` alias only) a first release or a contract change needs the proposal approved | this dialog uses Step 8 instead |
| `details.foreign` | a release file differs from both its baseline and the approved content — nothing overwritten | the owner restores or finishes the file, then resume — or abandon |
| `details.foreign_paths`, `details.foreign_content` | the index holds staged changes that are not the release's — left untouched | the owner unstages them, then resume |
| `details.git_error`, `details.head`, `details.commit`, `details.tree`, `details.expected_tree` | a repository hook refused the release (or attestation) commit (`git_error`, `head`), or changed it (`commit`, `tree` ≠ `expected_tree`) — never adopted | show the hook output; the owner fixes the cause (a changed commit: resets HEAD to the candidate and the artifacts), then resume |
| `details.foreign_staged` | after the release commit: the owner's own staged version of a release file was preserved | the owner unstages or commits it, then resume |
| `details.index_lock` | another git process holds `index.lock` (never removed by dct) | wait for it, then resume |
| `details.push_target`, `details.git_error` | no upstream (`push_target: null`) → nothing stamped; a failed push → the record stays `tagged` | choose `--push-remote … --push-branch …` or `--no-push` and apply again; after a failed push fix the cause, then resume — it retries ONLY the push |
| `details.remote_tags` | the remote already has a tag of V | stop; the owner decides (another version, or the remote's tag) |
| `details.remote_branch` | the remote branch is not contained in the candidate | the owner fetches and integrates it, then re-plan from Step 0 |
| `details.outside_consent`, `details.url_now` | the record's scope or the push remote's URL left the stored consent | restore the remote, then resume; otherwise abandon |
| `details.recorded` | the record belongs to another checkout | resume in that checkout (`details.recorded`) |
| `details.conflict`, `details.observed_effects`, `details.record_state` | outcome `conflict`, exit 75: a concurrent database write; the rolled-back write, the effects that DID happen (they stay), the re-read record (`unknown` → check `dct release status`) | never retry on your own; the owner decides: a conflict at the stamp → the same apply again; later → resume |

**Resume path** (K-97 F4) — show the RECORDED release (`resume`: candidate,
entry set, proof, effect state) and, separately, today's HEAD
(`resume.head`, `checkout.matches_record`) and the entries added since
(`resume.new_unreleased` — not part of this release). A record whose
`executor` is `own`, or without a `consent` (stamped outside `dct release
apply`), is NOT finished by dct — `resume` refuses it: say who finishes it
(the project's own tool, or the owner through the ledger path after
verifying each effect — [executor](release-workflow.md#executor-k-85);
never this dialog) or abandon it; a record without an approved proposal is
refused at `stamped` and can only be abandoned. Otherwise the owner
chooses: finish → `dct release resume --json` (no new approval: it executes
the consent stored at apply — version, entries, candidate, proposal, push
target — while the scope is unchanged, refuses what lies outside it
(`details.outside_consent`), performs only the missing effects — a `tagged`
record: ONLY the push — and never re-runs the sync; another checkout or
HEAD is refused — run it in the record's `checkout_root`; the record is
never re-pinned); abandon → the owner's reason and explicit confirmation
(the record, stamps, `current_version` and effects stay, V stays taken) →
`dct release abandon --version V --reason "<reason>" --confirm --json`; or
stop.

**Legacy path** — `dct release reconstruct --version V --json` shows the
evidence (outcome `preview`: the resolved `version` — the stored spelling,
the request only in `requested_version` — the `candidates`, the `status`
the evidence reaches: `completed` with a tag, `committed` without one);
only after the owner confirms, the same command with `--confirm` records it
(no re-stamp). A `committed` reconstruction stays unfinished: dct's resume
does not continue it — the owner tags / pushes and records that through
the ledger path ([executor](release-workflow.md#executor-k-85); never this
dialog), or abandons it. Refused = contradicting or ambiguous evidence:
report it; never reconstruct by hand.

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
