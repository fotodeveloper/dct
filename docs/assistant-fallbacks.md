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

The assistant never edits the changelog file FOR THE RELEASE (rewriting a
non-Markdown changelog to Markdown, when the owner chooses it in step 5, is
work in progress committed in step 3 before the plan), never runs the
version sync itself, never commits, tags or pushes FOR THE RELEASE, never calls
`advance_release` and does not use the `clog release` alias (the dialog uses
`apply`, like the skill) — the workflow performs (the release commit through
the repository's hooks, the tag, the push to the approved target), verifies
and records every effect. An entry added or corrected during the dialog
never takes a version: the stamp of `apply` is the only writer of versions.
Run every command in the CURRENT checkout. Show long content as text first,
then ask. The steps are the `/release` skill's, one for one (MCP where a
tool suffices — the corrections; the CLI where the effects need it):

0. **Probe and plan** — `dct release plan --json`. A missing / older CLI
   (unknown subcommand or flag, or a plan whose `schema_version` lower than 4
   shows it has no `executor` or no `changelog_file` key) → STOP and say the CLI must be installed
   or upgraded (and the MCP server restarted). Exit 1 → show the reason, STOP.
1. **Route — from this ONE plan result** (K-111, K-119). The plan carries
   `kind`, `unfinished_release` and `executor`: `executor.project` (the
   project's release-executor setting TODAY) and `executor.record` (`null`,
   or for the unfinished record its `version`, `status`, stored `executor`,
   `progress_owner` and `consent`), read in one transaction. Decide from this
   ONE result — no other command is run to find out who releases (a second
   read at a later moment can disagree with the plan). FIRST: is there an
   unfinished release (`kind == "resume"`)? Then it is routed by what the
   RECORD stores, whatever the setting says today (in both directions).
   `executor.project` applies ONLY to a NEW release. Never ask the owner
   again merely because the setting for future releases changed.

   | the plan shows (the FIRST matching row routes) | who finishes it | path |
   | --- | --- | --- |
   | `kind == "resume"` and `executor.record.progress_owner == "dct"` | dct, on the consent STORED on the record | **Resume path** → `dct release resume` at once when the owner asked to continue (no second question) |
   | `kind == "resume"` and `executor.record.executor == "own"` | the project's own release tool | **Resume path** → hand over: its tool finishes it through the ledger path; dct runs nothing and takes nothing over |
   | `kind == "resume"` and `executor.record.consent == false` | nobody automatically — a `dct` record stamped outside `apply` | **Resume path** → offer `dct release takeover` (a NEW consent the owner approves) or the ledger path |
   | `kind == "release"` and `executor.project == "own"` | the project's own release tool | show the plan (Step 4) as the shared proof, hand over and STOP |
   | `kind == "release"` and `executor.project == "dct"` | dct | Step 2 |

   A new release of an `own` project: dct performs none of its effects
   (`prepare` / `apply` / `resume` refuse — `details.executor`): show the
   plan (step 4) as the shared proof, hand over to the project's own release
   tool ([integration](release-workflow.md#integrating-a-projects-own-release-tool):
   it stamps through the ledger with `expected_executor="own"` and reports
   each verified effect, the tag with its `tag_proof`) and STOP; never run a
   command stored anywhere in its place. Every later plan of the dialog is
   routed by the same table.
2. **Legacy release** — only for a new release: `version.current` set and
   `dct release status --version <current> --json` → outcome `none` → offer
   the legacy path below.
3. **Work in progress, committed in this workflow** (K-97 F1, K-120) —
   `index` not clean (staged, unstaged or `conflicted` paths, an untracked
   release artifact; other untracked files do not block and stay outside the
   release) → show it and ask: commit the work in progress now, or stop. A
   conflicted path or an operation in progress: the owner finishes or
   aborts it first. On consent, perform the commit HERE with the `/commit`
   procedure (the `/commit` fallback below — the ONE source of the commit
   commands; its version-bump redirect does not apply). That pointer is an
   instruction for the assistant, not a request to the owner: after the
   consent the assistant commits the work in progress itself, plans again,
   finishes the release and lets the workflow perform the approved push — the
   owner is not told to commit and come back. The procedure: the owner
   approves the staging plan (explicit paths) → with
   the index final, `dct clog candidates --json` and the `Changelog: <ids>`
   trailer chosen from THAT commit's complete staged diff (`uncertain`
   candidates only after the owner confirms them and `update_changelog(entry_id,
   branch=<current>, worktree=<toplevel>)` fixes them; several commits →
   one selection per commit from its own staged diff) → the owner approves
   the message → commit exactly what is staged, the repository's hooks run
   (never `--no-verify`, never `--amend`; a rejecting hook: fix, re-stage,
   select again, a NEW commit). Then `dct release plan --json` again and
   continue from step 1 — the new HEAD is the candidate steps 7-9 pin. The
   question never ends or splits the workflow; the workflow never resets,
   stashes or cleans.
4. **Show the plan** — range (`confirm_required` → the owner confirms the
   base; `approximate` → warn), proven entries grouped by branch / merge
   commit / PR — a PR number WITH its origin (`pr_source`, K-116: `git` =
   recognised in the repository by the plan; `client` = declared by a
   client, an attestation — never presented as git's recognition) —
   unproven entries with their `reason`, `attestation_groups`,
   foreign citations, reverts outside the range, warnings, the version-file
   contract (`version_files` null → learned at this first release, shown by
   prepare), the changelog file (`changelog_file`: `path` and `source` —
   `detected` / `stored` / `default` —, the other `candidates`; `managed`
   false → none).
5. **The owner decides** — FIRST a `changelog_file.problem` (#876; prepare
   and apply refuse it before the stamp), from its `found` and `ways`:
   `ambiguous` → the owner picks one of the offered paths (`dct project
   changelog-path <slug> <path>`; a link is offered as the path through its
   target — one without an admissible target is not a choice: its
   replace-link way and the off way are offered, K-27); `case_variant` → the tracked spelling
   (the same command); `not_markdown` → one of its ways: convert the
   existing changelog to Markdown (the assistant rewrites its content in
   the Keep a Changelog layout AND renames it to a Markdown extension, e.g.
   `git mv HISTORY.rst HISTORY.md` — git mv alone does not convert the
   format —, the owner approves the diff, step 3 commits it — outside git:
   renamed with `mv`, nothing to commit —, then `dct
   project changelog-path <slug> HISTORY.md` chooses it), an explicit
   Markdown file (`dct project changelog-path <slug> CHANGELOG.md` — any
   Markdown name, `changelog.md` too), or turn dct's changelog file off
   (`dct project changelog <slug> off`); `symlink` (the file or a
   directory of its path — `component`) → the path through the link's
   target as the explicit path (its `changelog-path` command, offered only
   when admissible), the link replaced (step 3 commits it; outside git
   nothing to commit), or the changelog file off — with `baseline:
   "work_tree"` the link is only on disk: moved away; `not_a_directory` /
   `not_a_file` / `outside_project` → another path, detection (`--detect`,
   offered only when the detected file also passes the work-tree check) or
   the changelog file off — `not_a_file` with `baseline: "work_tree"` is a
   TRACKED directory whose name differs only in case that a
   case-insensitive file system opens at the path: never moved away (K-28); `not_baseline` → by its
   `baseline`: `absent` / `tracked` → the owner moves the file away or
   commits it (step 3); `unfiltered` → nothing to commit (the content is
   the candidate's, but not in the form a checkout through its filters
   writes): the file is checked out again through its filters (`git rm
   --cached -q -- <path>`, then `git checkout HEAD -- <path>`); then
   re-plan from step 0. Then the range base; each attestation group (attest
   together / decide each / keep unreleased); each remaining unproven entry
   — **attest**, **correct** (`update_changelog`, then re-plan),
   **withdraw** (`delete_changelog`) or **keep** unreleased (no "include
   without proof"); the version (patch / minor / major / explicit, shown
   with the resulting number) — `V` computed ONCE.
6. **Attest** when chosen: `dct release attest --entries <ids> --confirm --json`
   (an empty commit through the repository's hooks; it must prove EXACTLY the
   approved ids before it is published), then `dct release plan --version V --json`
   again (the candidate changed); continue only when the attested ids are
   proven.
7. **Bind the decision** (K-80, H4) — the plan's default selection is every
   proven id, so re-plan with the OWNER'S selection:
   `dct release plan --version V --entries <ids> --json` (or
   `dct release plan --version V --no-entries --json`) and use THAT
   `snapshot_digest` (`D`). Confirm the summary from that plan. No entries →
   explicit version-only consent (#46; the `since=last_release` anchor does
   not move; a full export omits V).
8. **Prepare** (K-76 / K-97 F3 / K-104, before anything is stamped):
   `dct release prepare --version V --entries <ids> --plan-digest <D> --json`
   (or `dct release prepare --version V --no-entries --plan-digest <D> --json`).
   The version-sync script runs once in an isolated
   temporary worktree (outside git: a temporary copy); the checkout does not
   change. Show `details.proposal.paths`, `details.checks`,
   `details.contract` (`change: true` = a first release or a changed
   version-file contract — say so explicitly; an empty list is a contract
   too), `details.sync` (exit code, output, `output_dropped_bytes` cut) and
   the push target `details.push_target` (remote, branch, tag `v<V>` — the
   branch's upstream — and `details.push_target.url`, the ONE effective push
   URL: the remote's push address, never its fetch URL; `null`, with the
   reason in `warnings` → the owner decides, never guessed: no upstream →
   the owner names the remote and the branch, or `--no-push`; a remote
   with SEVERAL push URLs, listed in `warnings` → the owner picks ONE,
   there is no mirror mode; a URL git would read as a remote NAME or
   rewrite → the owner fixes the remote's push URL (the table below), then
   prepare again). The owner
   approves THAT file list and THAT push target; keep `proposal_sha256`
   (`P`) and the APPROVED push target — its three values `remote`, `branch`
   and `url`, exactly as shown: apply takes all three and never selects a
   target itself (the upstream may change after the approval). Another
   push target → the owner names remote and branch; show that remote's
   push URLs (`git remote get-url --push --all <remote>`, read-only) and
   let the owner approve ONE — those three are then the approved target. `failed` → the script failed or timed out (124; `details.sync`
   and the environment contract it ran under, `details.environment`): fix
   it, prepare again. `refused` → the table below.
9. **Apply**, only on the owner's yes:
   `dct release apply --version V --entries <ids> --plan-digest <D> --proposal-digest <P> --push-remote <remote> --push-branch <branch> --push-url <url> --confirm --json`
   (the approved push target: `details.push_target.{remote,branch,url}` of
   step 8, verbatim),
   or `dct release apply --version V --no-entries --plan-digest <D> --proposal-digest <P> --no-push --confirm --json`.
   A release that pushes ALWAYS carries the three push flags — there is no
   form without them: an apply that lacks one is refused before the stamp,
   and `--no-push` only on the owner's explicit choice of a local release.
   It stamps, writes the changelog section and the approved version files,
   commits `release: v<V>` through the hooks (on the pinned candidate,
   published by compare-and-swap — K-103; needs git 2.36 or newer), updates
   the real index, tags and pushes the commit and the tag atomically to the
   approved URL (K-97 F2, K-104) — exactly those two refs: no other tag,
   and never a submodule (a commit a gitlink names stays the owner's to
   publish). Never pass a digest the owner has not
   seen.
10. **Read the result and report** — `completed`; `noop` (already released —
   nothing done); `refused` (show `details.error`, act by the table; only
   the listed `steps` happened; an unfinished `release` → the resume path:
   the same operation continues there on the stored consent once the cause
   is gone); `conflict` (exit 75, the table). Show every `warnings` line,
   also of a successful result (a post-commit hook that failed after the
   commit was published — the release stands; a pin or stored blob that
   could not be cleaned). Report the requested version
   (`details.requested_version`), the record's `version` and
   `details.current_version` separately, the steps (`stamp`, `changelog`,
   `version_files`, `commit`, `index`, `tag`, `push`, `complete`) and where
   it was pushed (`details.push_target` and the `url` of the `push` step, or
   a local release).

Refusals (the keys are the workflow's own, `dct/workflows/release/effects.py`;
the same table as the skill's):

| details | what to tell the owner | next action |
| --- | --- | --- |
| `details.error` | every refusal: the reason, verbatim | the row of the other keys |
| `details.executor`, `details.consent` | the project (`executor: "own"`) or the record (`own`, or `consent: null` — stamped outside `apply`) is not executed by dct | a new release: hand over to the project's own tool (Step 1); a record: the Resume path — the own tool's ledger path, or for a `dct` record the takeover (a NEW consent); this dialog runs nothing in its place |
| `details.lock`, `details.holder` | another release command holds the repository's operation lock (pid, action, start) | wait until it ends, then run the same command once — never loop |
| `details.lock`, `details.reason` | `lock: null` — the lock's identity cannot be determined: the project has no registered path (`reason: "no_path"`) or its path cannot be verified (`"unverifiable_path"`); dct never locks on a substitute key | this is not a busy lock: the owner registers or corrects the path (`dct project rename <slug> --path <dir>`), or the command is run where the registered path exists; then the same command once |
| `details.process_groups`, `details.process_group` | a dct that was killed left processes running (`process_groups`; `[]` = it cannot be verified), or a version-sync script could not be stopped (`process_group`) — the repository may still be written to | never loop and never clear it yourself: show the groups and the hint of `details.error`; the owner decides (ends those processes and clears the lock file as the message says); only then the same command once |
| `details.git_version`, `details.required_git_version` | git is older than the workflow needs (the hooks are run explicitly) — refused before any effect, never a fallback that skips hooks | the owner upgrades git; then the same command |
| `details.staged`, `details.unstaged`, `details.conflicted`, `details.untracked`, `details.in_progress` | the tree is not clean, a release artifact is untracked, or a merge / rebase / cherry-pick / revert runs | Step 3 (commit the work in progress here), or the owner finishes the operation; then re-plan |
| `details.unfinished` | an unfinished release exists | the Resume path |
| `details.unproven` | a selected id has no proof | Step 5 (attest / correct / withdraw / keep), then Step 7 |
| `details.approved_ids`, `details.proven_ids` | attest: a commit hook changed the id set — the commit would prove `proven_ids`, the owner approved `approved_ids` (equality is required); nothing was published | the owner fixes the hook; then Step 6 again |
| `details.approved_digest`, `details.derived_digest` | the context differs from the approved plan (HEAD, an entry added or edited, selection, version, proof) — or, for a takeover, from the approved preview | show the derived `snapshot`; Step 7 again — a new approval (a takeover: preview again) |
| `details.proposal_digest`, `details.plan_digest` | a new release was started without an approved version-file proposal (`proposal_digest: null`) — equal paths or a checked version field are no consent to the content | Step 8 (prepare for the plan `plan_digest`), the owner approves the files, then Step 9 with that digest |
| `details.mismatch`, `details.pending`, `details.approved_script`, `details.missing_blobs` | the approved proposal is void: another candidate / version / plan, a newer prepare, a changed script, pruned blobs | Step 8 again — a new approval |
| `details.changed` | before the stamp: a proposal file changed since prepare (approval void); on resume: a release artifact differs from the recorded manifest, or a post-commit hook changed the work tree after the commit | prepare again (Step 8); on resume the owner restores the files, then resume |
| `details.checkout_changed`, `details.sync`, `details.environment`, `details.path` | prepare: the script wrote outside its isolated worktree, failed / timed out (`sync`, with its `environment` contract: it ran isolated, with only the candidate's files — never in the checkout), touched the changelog, or broke a version file's contract | STOP; the owner fixes the script (it must provide what it needs itself, committed), then Step 8 |
| `details.changelog_file`, `details.approved` | the changelog file is not settled (`changelog_file.problem`: `ambiguous`, `case_variant`, `not_markdown`, `symlink`, `not_a_directory`, `not_a_file`, `outside_project`, `not_baseline`, or `content` — K-9, prepare / apply only: composing the release's section into the candidate's file refuses, e.g. not UTF-8, a different pre-written V section or git filters that rewrite the text (`filtered`, K-20), `changelog_file.detail` — refused BEFORE the stamp, nothing released) — or, on resume, `approved` (the file the approved proposal binds) is not the file resolved in the candidate now (a changelog setting changed since the approval, or a record stamped before #876: neither file is written) | before the stamp: step 5 (its `ways`; `content` → the owner fixes the file — re-encodes it / removes the pre-written section, committed as work in progress when the project is in git (outside git there is nothing to commit) — or turns the changelog file off), then re-plan from step 0; on resume FIRST set a changelog setting consistent with the approved path (which setting the approval was made under is not recorded) — `details.restore`, the `dct project changelog …` / `dct project changelog-path …` commands the refusal names, derived from the stored proposal — on the owner's yes, then `dct release resume`; only when `restore` is empty (a record stamped before #876) or the owner declines, the last resort: `dct release abandon` (the owner's reason) ends the record, its stamps and `current_version` stay |
| `details.tags` | K-8: a tag of V (any spelling) already exists in this repository — the release tags its own release commit, which does not exist yet; refused BEFORE the stamp, nothing released | show the tags; the owner removes a leftover tag themselves (the refusal names the command; the dialog never runs it) or chooses another version; re-plan from step 0 |
| `details.foreign` | a release file differs from both its baseline and the approved content — nothing overwritten | an edited file: the owner restores or finishes it, then resume; a changelog file whose baseline is absent (the message says so — an ignored / untracked file, a case-variant, or an edit of the file the release wrote): the owner moves it away, never "restores" it, then resume |
| `details.foreign_paths`, `details.foreign_content` | the index holds staged changes that are not the release's — left untouched | the owner unstages them, then resume |
| `details.git_error`, `details.hook`, `details.head`, `details.expected_parent`, `details.tree`, `details.expected_tree`, `details.changed`, `details.subject`, `details.unpublished_commit`, `details.pinned_ref`, `details.head_ref`, `details.signed` | the release (or attestation) commit was stopped and nothing was published — no ref moved: a hook refused it (`hook`, `git_error`), a hook changed the approved content (`changed`, `tree` ≠ `expected_tree`) or the message outside its contract (`subject`; `unpublished_commit` = an unreferenced object git removes), signing failed (`signed`), or HEAD moved / the branch was switched meanwhile (`head` ≠ `expected_parent`, `pinned_ref` ≠ `head_ref`) — or, refused by apply BEFORE the stamp (`git_error` alone, nothing released), git cannot name the commit identity (K-8 audit) | show the hook output; the owner fixes the hook or puts the files back as approved (after a branch switch: switches back), then resume on the same candidate (attest: Step 6 again) — no branch has to be moved back; the identity: the owner sets `user.name` / `user.email`, then apply again |
| `details.commit`, `details.parents`, `details.tree`, `details.expected_tree`, `details.candidate` | on resume HEAD is neither the candidate nor the release commit — a commit made by hand, or the leftover of an older run (`commit`, its `parents`, its `tree` against the approved `expected_tree`, the `candidate`); reported exactly: dct touched no ref, no staging and no file, and it never resets | show it. The hint of `details.error` is conditional: IF that commit is the owner's to discard and nothing is based on it, the owner moves the branch back to the candidate, then resume. The owner decides — never this dialog |
| `details.foreign_staged` | after the release commit: the owner's own staged version of a release file was preserved | the owner unstages or commits it, then resume |
| `details.index_lock` | before the stamp (apply, K-25): another git process holds `index.lock` (never removed by dct) — refused, nothing stamped or recorded, so `dct release resume` only warns "nothing to resume" | wait for it, then run the same `dct release apply` again (the same approvals) |
| `details.index_lock` | after the stamp (the commit step of apply or resume): another git process holds `index.lock` (never removed by dct) — the record stays as it is | wait for it, then `dct release resume` (it continues the recorded release) |
| `details.push_target`, `details.push_urls` | before the stamp, nothing released: the apply did not name the COMPLETE approved push target (`push_target: null` — a missing `--push-remote` / `--push-branch` / `--push-url`; apply never selects the upstream), the named URL is not a push address of that remote (`push_urls`: the remote changed since the approval), or — prepare / takeover — no upstream, or the remote has several push URLs (`push_urls`) — a release goes to ONE approved address (no mirror mode) | pass the three values the owner approved in Step 8; when the target itself changed or is unsettled the owner chooses again — remote, branch and ONE of the listed addresses (`--push-remote … --push-branch … --push-url <url>`), or `--no-push`; then apply again. On resume `push_urls` without the approved URL = the remote no longer pushes to the approved address (outside the consent): the owner restores the remote, then resume |
| `details.url`, `details.rewrite_rule`, `details.remote_name` | git would not send the approved address `url` to the repository it names: an `insteadOf` / `pushInsteadOf` rule (`rewrite_rule`) would rewrite it on git's command line, or the string is also the name of a git remote (`remote_name` — any config scope, also global; a slash does not make it a path) and git would use that remote's own addresses — dct pushes only to the exact approved URL | the owner removes that rule (or points the remote at the final address); for a remote name BEFORE the stamp: writes the remote's push URL explicitly (`./name`, an absolute path or a real URL) or renames the colliding remote, then Step 8 again; AFTER the stamp the consent keeps the approved string, so only renaming or removing the colliding remote works, then resume (or abandon) |
| `details.git_error`, `details.push_target` | a failed push → the record stays `tagged` (the commit and the tag exist locally) | fix the cause, then resume — it retries ONLY the push, to the approved URL |
| `details.remote_tags` | the push destination already has a tag of V | stop; the owner decides (another version, or the remote's tag) |
| `details.remote_branch` | the remote branch is not contained in the candidate | the owner fetches and integrates it, then re-plan from Step 0 |
| `details.outside_consent` | the record's version, entries or candidate left the stored consent — resume does not cover it | show what differs; the owner restores the recorded scope, then resume. The dialog never widens a consent (Resume path) |
| `details.recorded` | the record belongs to another checkout | resume in that checkout (`details.recorded`) |
| `details.conflict`, `details.observed_effects`, `details.record_state` | outcome `conflict`, exit 75: a concurrent database write; the rolled-back write, the effects that DID happen (they stay), the re-read record (`unknown` → check `dct release status`) | never retry on your own; the owner decides: a conflict at the stamp → the same apply again; later → resume |

**Resume path** (K-97 F4, K-111, K-112) — step 1 routes here. Show the
RECORDED release (`resume`: candidate, entry set, proof — a PR number with
its origin, `pr_source` — effect state) and, separately, today's HEAD
(`resume.head`, `checkout.matches_record`), the entries added since
(`resume.new_unreleased` — not part of this release) and who finishes it
(`executor.record`).

*A release dct executes* (`progress_owner` is `dct`): the owner's consent of
`apply` (or of a takeover) is stored on the record — version, entries,
candidate, proposal, push target — and is not asked for a second time. The
owner asked to continue it and its scope is unchanged → show the stored
state and run `dct release resume --json` at once, on the stored consent: no
new approval, no menu, no second question (the command takes no confirmation
flag). It performs only the missing effects — a `tagged` record: ONLY the
push — and never re-runs the sync; another checkout or HEAD is refused (run
it in the record's `checkout_root`; the record is never re-pinned); the
release commit, the tag and the push are finished by this one command,
inside the same workflow. The request was something else (a new version, a
bare "release" in another session) → a record in the database never starts
anything by itself: show the state, say it is finished first, and resume
when the owner says so. An earlier explicit "stop", or a withdrawn consent,
stays binding until the owner says to continue. A refusal or a conflict is
reported, not retried — never loop; run it again only when the owner says
the cause is gone. The scope changed (`details.outside_consent`) → the
stored consent does not cover it: show what differs; a new or wider consent
is asked for only then — for that concrete change of scope — and the
assistant never widens one by itself (the owner restores the recorded scope,
then resume, or decides how the release ends). Abandon only on the owner's
explicit request or decision, with the owner's reason — never a routine
option (the record, stamps, `current_version` and effects stay, V stays
taken) →
`dct release abandon --version V --reason "<reason>" --confirm --json` —
the only way to abandon a release dct executes: the MCP tool
`abandon_release` refuses such a record (it holds no operation lock,
K-119 M6).

*A record nobody finishes automatically* (`progress_owner` is `ledger`):
`resume` refuses it (`details.executor`, `details.consent`) — do not run it.
The record's `executor` is `own` → its own tool finishes it through the
ledger path (its stamp carried `expected_executor="own"`; it reports each
verified effect, the tag with its `tag_proof`, a push with its `push_proof`
— [integration](release-workflow.md#integrating-a-projects-own-release-tool));
hand over. An `own` record stays with its tool: dct never offers the
takeover for it, whatever the setting says today. The record's `executor`
is `dct` (or null) and it has no consent (stamped outside `dct release
apply`: the MCP stamp, an older record, a reconstruction that ended at
`committed`) → two ways, the owner chooses. (1) The takeover: dct records
the missing consent from the VERIFIED artifacts — a NEW consent that needs
the owner's explicit approval ("no second question" covers only the
unchanged resume above), with an explicit push decision. Preview:
`dct release takeover --version V --push-remote <remote> --push-branch <branch> --json`
(ONE of several push URLs: add `--push-url <url>`), or
`dct release takeover --version V --no-push --json`
→ outcome `preview`, nothing written: show `details.consent` (version,
entries, candidate, release commit, tree, artifacts digest, the push target
or no push), `details.stage`, `details.missing_effects`, and keep
`details.takeover_digest` (`T`); only after the owner approves exactly that,
with the same push flags:
`dct release takeover --version V --push-remote <remote> --push-branch <branch> --takeover-digest <T> --confirm --json`
→ outcome `taken_over` (no effect performed); then `dct release resume --json`
performs only the missing effects. Refused (no release commit verifies,
`details.contradictions`, a digest that no longer matches → preview again)
→ report it; the ledger path stays. (2) The ledger path: whoever performs
the missing effects verifies them and records the progress
([executor](release-workflow.md#executor-k-85)) — never this dialog.

**Legacy path** — `dct release reconstruct --version V --json` shows the
evidence (outcome `preview`, nothing written): the resolved `version` — the
stored spelling, the request only in `requested_version` — the `candidates`
(verified or not, with the reason and the `history` state), the `status` the
evidence reaches (`completed` with a tag, `committed` without one),
`evidence.verification` (the scope that WAS verified; `limited: true` = no
artifact content was verified, only the database's stamp and, in git, the
commit prove the release — say so), `evidence.contract` (`state: "unknown"`
is not a `"confirmed"` empty list), and beside it `details.executor` (the
executor ASSIGNED to the record now — a responsibility, `provenance: false`,
not who made the release) and `details.finish` (the ways to finish a release
left at `committed`). Only after the owner confirms,
`dct release reconstruct --version V --confirm --json` records it (outcome
`reconstructed`, no re-stamp). Refused = contradicting or ambiguous
evidence: report it; never reconstruct by hand. The step for each refusal
and for finishing:

| situation | step |
| --- | --- |
| several verified candidates, no tag (`details.candidates`) | show them all; dct picks none — the owner picks one: `dct release reconstruct --version V --release-commit <sha> --json`, then the same with `--confirm` |
| the release commit changed older changelog history (`details.history`) | show the diff (`state`, `diff`, `diff_lines`); nothing is recorded and no flag or waiver exists — also for a typo fix; the release stays without a record |
| outside git, the contract is unknown (`details.contract`, `state: "unknown"`) | `dct release version-files --json` (outcome `none` = UNKNOWN, `found` = confirmed); ask the owner which files hold the version, or none; with THAT answer `dct release version-files --set <a,b> --confirm --json` or `dct release version-files --none --confirm --json` (outcome `confirmed`); then reconstruct again |
| the project's executor is `own` (`details.executor: "own"`) | its tool supplies the evidence file: `dct release reconstruct --version V --evidence <file.json> --json`, then the same with `--confirm` — the file comes from the own tool, never composed by the assistant |
| the record ends at `committed` (`details.finish`) | way `takeover` → the takeover of the resume path above (preview, the owner approves that digest, resume); way `ledger` → whoever performs the missing tag and push records them ([executor](release-workflow.md#executor-k-85); the only way for an `own` record) |

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
