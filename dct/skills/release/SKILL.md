---
name: release
description: >-
  [dct] Release a version — review the proof plan with the owner, commit
  work in progress on consent, settle entries without proof, approve the
  prepared version files and the push target, then let the local
  `dct release` workflow stamp, write the changelog and version files,
  commit, tag and push, each verified. Use when: "release", "bump version",
  "ship it" — in any language.
---

# /release — ONE workflow over `dct release`

The release is EXECUTED by the local workflow — `dct release plan / attest /
prepare / apply / resume / abandon / reconstruct / takeover / version-files /
status`, all with `--json` (schemas, order of effects, refusals:
`docs/release-workflow.md`; code: `dct/workflows/release/`). This skill runs
the DIALOG from the first plan to the pushed tag in ONE workflow: it shows
what the workflow reports, asks the owner, passes the owner's decisions back
as flags — and, when the owner agrees, commits the work in progress itself
(Step 3) without leaving this workflow. The authoritative step list is the
`/release` fallback of `docs/assistant-fallbacks.md`; this skill follows it
step for step.

The workflow performs and verifies every release effect and records the
progress in the release record itself. This skill therefore:
- never edits the changelog file FOR THE RELEASE (the workflow writes the
  section), never runs a version-sync script — rewriting a non-Markdown
  changelog to Markdown (Step 5, the owner's choice) is work in progress,
  committed in Step 3 before the plan;
- never stages, commits or tags FOR THE RELEASE (the attestation commit, the
  release commit and the tag are made by `dct release attest` / `apply` /
  `resume`); the only commit it makes is the owner's work in progress
  (Step 3), with the `/commit` procedure;
- never calls `advance_release` and never stamps through `release_changelog`
  — the ledger is written by the workflow; an entry added or corrected during
  the dialog never takes a version (the stamp of `apply` is the only writer
  of versions);
- does not run a push of its own: the push of the release commit and the tag
  is a step of `apply` / `resume`, to the push target the owner approved
  (Step 8);
- never resets, stashes or cleans the owner's work, and does not use the
  `clog release` alias (it uses `apply`, like the fallback).

Run every command in the CURRENT checkout (your shell's working directory),
never in the MCP server's directory. Add `-P <slug>` only when the project is
not detected from the CWD.

Present long content (the plan, the proposal, the evidence) as plain message
text FIRST; the AskUserQuestion that follows only references it (two-turn
pattern).

## Workflow

### Step 0: Capability probe and the plan

Run `dct release plan --json`.

- Command missing, or exit 2 with `invalid choice` / an unknown argument →
  STOP: "The local dct CLI is missing or older than this skill — install or
  upgrade dct (and restart the MCP server); the release workflow runs in the
  CLI." Do NOT fall back to the MCP tools: the ledger tools alone cannot
  verify the changelog file, the version files, the commit, the tag or the
  push.
- `schema_version` lower than 5 (the plan has no `executor`,
  `changelog_file`, `version.suggested` or `pending_consent` key — an
  older CLI) → STOP with the same message.
- Exit 1 (the checkout could not be verified, project not found) → show the
  message and STOP. Never retry from another directory.

Load the two MCP tools the dialog may need for corrections with ONE call:

```
ToolSearch("select:mcp__dct__update_changelog,mcp__dct__delete_changelog")
```

### Step 1: Route — from this ONE plan result (K-111, K-119)

The plan carries everything the routing needs, read in one transaction:
`kind`, `unfinished_release` and `executor` — `executor.project` (the
project's release-executor setting TODAY) and `executor.record` (`null`, or
for the unfinished record its `version`, `status`, stored `executor`,
`progress_owner` and `consent`). Decide from this ONE result. Run no other
command to find out who releases: a second read at a later moment can
disagree with the plan.

FIRST: is there an unfinished release (`kind == "resume"` —
`unfinished_release` is the record)? Then it is routed by what the RECORD
stores, whatever the setting says today — a release dct stamped stays dct's
after the project switched to its own tool, and the other way round.
`executor.project` applies ONLY to a NEW release. Never ask the owner again
merely because the setting for future releases changed; one line saying so
is enough.

| the plan shows (the FIRST matching row routes) | who finishes it | path |
| --- | --- | --- |
| `kind == "resume"` and `executor.record.progress_owner == "dct"` | dct, on the consent STORED on the record | **Resume path** → `dct release resume` at once when the owner asked to continue (no second question) |
| `kind == "resume"` and `executor.record.executor == "own"` | the project's own release tool | **Resume path** → hand over: its tool finishes it through the ledger path; dct runs nothing and takes nothing over |
| `kind == "resume"` and `executor.record.consent == false` | nobody automatically — a `dct` record stamped outside `apply` | **Resume path** → offer `dct release takeover` (a NEW consent the owner approves) or the ledger path |
| `kind == "release"` and `executor.project == "own"` | the project's own release tool | show the plan (Step 4) as the shared proof, hand over and STOP |
| `kind == "release"` and `executor.project == "dct"` | dct | Step 2 |

- **A new release of an `own` project** — the project's OWN release tool
  performs the effects: dct's `prepare` / `apply` / `resume` refuse it
  before any write (`details.executor`). Show the plan (Step 4) as the
  shared proof, say that this project releases through its own integration
  (`docs/release-workflow.md#integrating-a-projects-own-release-tool`: its
  tool stamps through the ledger with `expected_executor="own"` and reports
  each verified effect, the tag with its `tag_proof`) and STOP. Never run a
  command stored anywhere in its place. A release its tool made before
  release records existed is recorded by the **Legacy path** (the
  `--evidence` row), when the owner asks for it.
- Every later plan of this dialog (after Step 3, Step 6) is routed by the
  same table, from THAT result.

### Step 2: Legacy release

Only for a new release (`kind == "release"`): `version.current` set →
`dct release status --version <current> --json`. Outcome `none` means the
current version was released before release records existed. Offer the
**Legacy path** below (recommended: it records the old release from
evidence, without re-stamping), or continuing without it, or stopping.
Outcome `found` → continue.

### Step 3: Work in progress — committed in this workflow (K-97 F1, K-120)

`prepare` / `apply` refuse unless the index and the work tree are clean
(`index.clean`, no `conflicted` path, no untracked release artifact) and no
merge / rebase / cherry-pick / revert is in progress — dct never resets,
stashes or cleans. Untracked files that are not release artifacts do not
block: they stay outside the release (recorded as its untracked baseline).

When the plan's `index` is not clean, show the staged, unstaged and
conflicted paths and ask: `"Commit the work in progress now"` /
`"Stop"`. A conflicted path or an operation in progress cannot be
committed as it is — the owner finishes or aborts it first, then the
workflow continues here.

On `"Commit the work in progress now"` perform the commit HERE, in this same
workflow, following the `/commit` procedure (the `commit` skill, Steps 1–8 —
the ONE source of the commit commands; its version-bump redirect to
`/release` does not apply: you are in it). That pointer is an instruction
for YOU, not a request to the owner: after the owner's consent you commit
the work in progress yourself, plan again, finish the release and let the
workflow perform the approved push. Do not tell the owner to commit and come
back.

1. Show the staging plan (explicit paths, never everything at once; a
   partially staged path stays as it is; never secrets or build output),
   get the owner's approval, stage it.
2. With the index final, `dct clog candidates --json` and choose the
   `Changelog: <ids>` trailer from THAT commit's complete staged diff —
   `confirmed` candidates that describe it; an `uncertain` one only after
   the owner confirms it and `mcp__dct__update_changelog(entry_id,
   branch=<current>, worktree=<toplevel>)` fixes its provenance (K-17 /
   K-23 / K-53). Several commits → each one gets its own selection from its
   own final staged diff.
3. Show the message draft (conventional format, `(v<current_version>)`
   suffix, the trailer before `Co-Authored-By:`), get approval, commit
   exactly what is staged — the repository's hooks run; never `--no-verify`,
   never `--amend`. A rejecting hook: fix the cause, re-stage, select the
   trailer again from the new staged diff, create a NEW commit.
4. Then re-plan: `dct release plan --json` again and continue from Step 1 —
   the new HEAD is the candidate the later steps pin (Step 7 binds it, Step 8
   and Step 9 refuse any other).

The question never ends or splits the release: after the commit the dialog
goes on to the pushed tag; `"Stop"` ends it with nothing released.

### Step 4: Show the plan (plain text)

Print, from the plan:

```
Project: proj-a — current 1.0.0
Candidate: abc1234 (feat/x) — range v1.0.0..abc1234 (12 commits)

Proven (4), by branch / merge:
  feat/x — merge def5678 (PR #41, recognised in git)
    #12 [added] New parser support            proved by 1a2b3c4
    #13 [added] Incremental indexing          proved by 5d6e7f8
  (no branch)
    #15 [fixed] Parser crash on empty files   proved by 9a8b7c6
Without proof (3):
  #16 [fixed] Memory leak in watcher          squash_suspected (group 1)
  #17 [changed] Graph schema                  squash_suspected (group 1)
  #18 [fixed] Retry on timeout                reverted by 0f1e2d3
Collective attestation proposed:
  group 1 — feat/y: #16, #17 (squash 4c5d6e7 "Add watcher (#42)"; signals: branch_named)
Version files: pyproject.toml, proj_a/__init__.py (contract)
```

- Proven entries (`entries.proven`) are grouped by `branch`, then by
  `merge_commit` / `pr` (K-70); each shows its proving `commits`. The public
  changelog never carries branch names — this grouping is for the review
  only.
- A PR number is shown WITH its origin (`pr_source`, K-116): `git` = the
  plan recognised it in the repository (the entry's own merge); `client` =
  declared by a client, an attestation nobody looked up — say "declared",
  never present it as git's recognition. A `(#N)` subject suffix (the squash
  line above) is a heuristic, not a PR of the entry.
- Unproven entries (`entries.unproven`) show their `reason` (`no_trailer`,
  `reverted` + the reverting commit, `revert_unconfirmed`, `below_range`,
  `squash_suspected` + the group, `no_git`, `no_path`).
- `range.confirm_required` → say: "The proof range starts at `<base_tag>`,
  not at the current version's tag `<expected_tag.name>`" — the owner
  confirms it in Step 5. `range.approximate` → WARN: "No ancestor release
  tag — the range is approximate (`--since=<since>`)". No range (a project
  outside git) → say that its entries have no commit proof and are released
  as the owner selects them.
- `foreign_citations`, `reverts` with status `outside_range` or `ambiguous`,
  and every `warnings` line: shown as they are.
- `version_files.version_files` null → "Version files: learned at this first
  release — `dct release prepare` shows them for approval (Step 8)".
- `changelog_file` — the changelog file the release writes (#876): show
  `path` and `source` (`detected` = found among the tracked files, e.g.
  `CHANGELOG.md`; `stored` = the project's explicit setting; `default` =
  `changelog.md`, existing or new) and the other `candidates`. `managed`
  false → "No changelog file (dct's changelog file is off)". A `problem`
  is settled in Step 5 before anything else — prepare and apply refuse it
  before the stamp.

### Step 5: The owner's decisions (AskUserQuestion)

Ask in as few calls as the question limit allows, in this order:

0. **The changelog file** (only when `changelog_file.problem` is set) —
   show `changelog_file.found` and its `ways` (each `what` and `command`):
   - `ambiguous` (several Markdown changelogs) → one option per offered
     `changelog-path` way (`"Use CHANGES.md"`; a link among them is offered
     as the path through its target) — run its `dct project
     changelog-path <slug> <path>`; a link candidate without an admissible
     target is not offered as a choice: its `"Replace the link"` way and
     `"Turn dct's changelog file off"` are (K-27);
   - `case_variant` (the stored path differs from a tracked file only in
     case) → `"Use <found>"` (its `changelog-path` command) / `"Stop"`;
   - `not_markdown` (the only changelog is not Markdown, e.g.
     `HISTORY.rst`, or the stored path has no Markdown extension) → its
     ways: `"Convert it to Markdown"` (you rewrite its content in the Keep a
     Changelog layout AND rename it to a Markdown extension, e.g. `git mv
     HISTORY.rst HISTORY.md` — git mv alone does not convert the format —,
     show the diff, it is committed as work in progress — Step 3 —, then
     `dct project changelog-path <slug> HISTORY.md` chooses it; outside
     git: renamed with `mv`, nothing to commit),
     `"Explicit Markdown file"` (`dct project changelog-path <slug>
     CHANGELOG.md` — any Markdown name, `changelog.md` too; the old file
     stays as it is), `"Turn dct's changelog file off"` (`dct project
     changelog <slug> off`);
   - `symlink` (the changelog file or a directory of its path —
     `component` — is a symbolic link; dct never writes through one) →
     `"Use <target>"` (its `changelog-path` command, offered only when the
     path through the target is admissible) / `"Replace the link"`
     (committed as work in progress; outside git nothing to commit) /
     `"Turn dct's changelog file off"`; with `baseline: "work_tree"` the
     link exists only on disk (ignored / untracked) → the owner moves it
     away;
   - `not_a_directory` / `not_a_file` / `outside_project` (a file or
     submodule where a directory is expected, a directory or submodule
     where the file is expected, a stored path leaving the project) → its
     ways: another path (`dct project changelog-path <slug> <path>`),
     detection (`--detect`, offered only when the detected file also
     passes the work-tree check) or the changelog file off; `not_a_file`
     with `baseline: "work_tree"` = a TRACKED directory whose name differs
     only in case, opened at the path by a case-insensitive file system —
     never moved away: another path or off (K-28);
   - `not_baseline` → follow its `ways`, by `baseline`: `absent` / `tracked`
     (a file at the path that is not the candidate's content — ignored,
     untracked, uncommitted, or a case-variant on a case-insensitive file
     system) → the owner moves it away or commits it (Step 3);
     `unfiltered` (the content IS the candidate's, but not in the form a
     checkout through its filters writes — eol / filter attributes changed
     after the checkout) → there is nothing to commit: the file is checked
     out again through its filters (`git rm --cached -q -- <path>`, then
     `git checkout HEAD -- <path>`).
   Then re-plan from Step 0 — a settled problem is a new plan.
1. **Range** (only when `confirm_required`): `"Use <base_tag> as the base"` /
   `"Stop"`.
2. **Collective attestation** — one question per `attestation_groups` item
   (K-69): `"Attest #16, #17 together — their changes are in the
   candidate"` / `"Decide each entry"` / `"Leave them unreleased"`. A
   proposal only; nothing is attested without the owner's yes.
3. **Each remaining entry without proof** (null branch included):
   - `"Attest — the change is in the candidate"` → collected for Step 6;
   - `"Correct it"` → a follow-up for the fix, then
     `mcp__dct__update_changelog(entry_id, …)`; re-plan afterwards;
   - `"Withdraw it"` → `mcp__dct__delete_changelog(entry_id)` (soft delete);
   - `"Keep it unreleased"` → it waits for a later release.
   There is NO "include without proof" option. For a `reverted` entry say
   that its change was reverted — attest only when the owner confirms the
   change is back in the candidate.
4. **Version** — an AskUserQuestion, never a plain-text prompt and never
   inferred from earlier words. Options with the concrete result computed
   from `version.current`: `"patch — v<X.Y.Z+1> (bug fixes only)"`,
   `"minor — v<X.Y+1.0> (new, backward compatible)"`,
   `"major — v<X+1.0.0> (breaking)"`, `"explicit version — I'll type one"`.
   No current version → explicit version only. Compute `V` ONCE; every later
   command and every retry passes that same `--version V`.

### Step 6: Attest (only when the owner chose it)

```bash
dct release attest --entries 16,17 --confirm --json
```

The workflow creates the empty commit `chore: attest changelog entries`
whose final paragraph is `Changelog: <ids>` (only ids the plan lists as
unproven; nothing staged; no release unfinished; the repository's hooks
run; the commit must prove EXACTLY the approved ids before it is published).
Outcome `attested` → the candidate changed: run
`dct release plan --version V --json` again, show what changed and continue
with Step 7 only when the attested ids are now proven. Outcome `refused` →
show `details.error` and return to Step 5 (a hook that changed the id set:
the `details.approved_ids` row of the table).

### Step 7: Bind the decision (K-80, H4)

The plan's default selection is every proven id — so bind the OWNER'S
selection with a new plan of exactly this version and these entries:

```bash
dct release plan --version 1.1.0 --entries 12,13,15,16,17 --json
dct release plan --version 1.1.0 --no-entries --json               # version-only
```

Build the summary from THAT plan's `snapshot` (candidate, selected ids,
proof) and keep its `snapshot_digest` (`D`) — never the digest of an earlier
plan. Show the summary and wait for the owner's yes:

```
Release proj-a 1.1.0?
  Candidate: def5678 (feat/x)
  Stamped (proven): #12, #13, #15, #16, #17
  Left unreleased: #18
  Version: 1.0.0 → 1.1.0
```

- **No entries selected** → ask explicitly: "No proven entries. Release
  version V only (no changelog entries)?" A version-only release (#46) is a
  real release and needs this consent (`--no-entries` states the intent; it
  is not the consent). Say the side effects: the `since=last_release`
  anchor does not move, and a full changelog export omits V. No → stop.

### Step 8: Prepare — the version files and the push target (K-76, K-97 F3, K-104)

Before anything is stamped: the version-sync script runs ONCE in an
isolated temporary worktree (outside git: a temporary copy); nothing in the
checkout changes:

```bash
dct release prepare --version 1.1.0 --entries 12,13,15,16,17 --plan-digest <D> --json
dct release prepare --version 1.1.0 --no-entries --plan-digest <D> --json   # version-only
```

Outcome `prepared` → show, as plain text:

- `details.proposal.paths` (one per line — the exact files the release
  commit will change besides the changelog) and `details.checks` (per file:
  its version field, or the lines holding V);
- `details.contract`: when `change` is true say it explicitly — a FIRST
  release (`confirmed` null) or a different set than the confirmed
  version-file contract; an empty list is a contract too (no version files);
- `details.sync`: the script's exit code, duration and output
  (`output_dropped_bytes` > 0 → say how many bytes were cut);
- the push target `details.push_target` — remote, branch, tag `v<V>` (the
  branch's upstream) and `details.push_target.url`: the ONE effective push
  URL — the remote's push address, never its fetch URL; the release is read,
  pushed and verified exactly there. `null` (the reason is in `warnings`) →
  the owner decides, never guess: no upstream → the owner names the remote
  and the branch, or a local release (`--no-push`); a remote with SEVERAL
  push URLs (listed in `warnings`) → the owner picks ONE — there is no
  mirror mode, the other addresses are the owner's to push; a URL git would
  read as a remote NAME or rewrite → the owner fixes the remote's push URL
  (the refusal table), then prepare again.

Then ask: `"Release with these files, pushed to <remote>/<branch> (<url>)"`
/ `"Other push target"` / `"Local release — no push"` / `"Not these files —
stop"`. Keep `details.proposal_sha256` (`P`) and the APPROVED push target —
its three values `remote`, `branch` and `url`, exactly as shown: apply takes
all three and never selects a target itself (the upstream may change after
the approval). `"Other push target"` → the owner names remote and branch;
show that remote's push URLs (`git remote get-url --push --all <remote>`,
read-only) and let the owner approve ONE — those three are then the
approved target. Outcome `failed` → the script
failed or timed out (exit code 124; `details.sync`, with the environment
contract it ran under in `details.environment`): nothing stamped; the owner
fixes the script, then prepare again. `refused` → the refusal table below.

### Step 9: Apply — only on the owner's yes

```bash
dct release apply --version 1.1.0 --entries 12,13,15,16,17 --plan-digest <D> --proposal-digest <P> --push-remote origin --push-branch main --push-url <url> --confirm --json   # the approved push target: details.push_target.{remote,branch,url} of Step 8
dct release apply --version 1.1.0 --no-entries --plan-digest <D> --proposal-digest <P> --no-push --confirm --json   # version-only, local
```

A release that pushes ALWAYS carries the three push flags — the `remote`,
`branch` and `url` the owner approved in Step 8 (the shown
`details.push_target`, or the other target the owner named there), verbatim.
There is no form without them: an apply that lacks one is refused before the
stamp, and `--no-push` only on the owner's explicit choice of a local
release. The workflow stamps, writes the changelog
section and the approved version files, makes the `release: v<V>` commit
through the repository's hooks (on the pinned candidate, published by
compare-and-swap — needs git 2.36 or newer), updates the real index, tags
`v<V>` and pushes the commit and the tag atomically to the approved URL
(K-97 F2, K-104) — exactly those two refs: no other tag, and never a
submodule (a commit a gitlink names stays the owner's to publish) — each
verified and recorded. Never pass a digest the
owner has not seen.

### Step 10: Read the result and report

- Exit 0, outcome `completed` → report (below).
- Outcome `noop` → the version is already released (a recorded, historical
  or legacy release) — nothing was done. A warning naming `dct release
  reconstruct` → offer the **Legacy path**.
- Exit 1, outcome `refused` → show `details.error`, then act by the table
  below. Only the listed `steps` happened. A `release` that is unfinished →
  the **Resume path**: the same operation continues there on the stored
  consent once the cause is gone.
- Outcome `conflict` (exit 75) → the conflict row of the table.
- Show every `warnings` line of a result, also of a successful one — a
  post-commit hook that failed after the commit was published (the release
  stands; nothing is repeated), a pin or stored blob that could not be
  cleaned.

Refusals (the keys are the workflow's own, `dct/workflows/release/effects.py`):

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
| `details.changelog_file`, `details.approved` | the changelog file is not settled (`changelog_file.problem`: `ambiguous`, `case_variant`, `not_markdown`, `symlink`, `not_a_directory`, `not_a_file`, `outside_project`, `not_baseline`, or `content` — K-9, prepare / apply only: composing the release's section into the candidate's file refuses, e.g. not UTF-8, a different pre-written V section or git filters that rewrite the text (`filtered`, K-20), `changelog_file.detail` — refused BEFORE the stamp, nothing released) — or, on resume, `approved` (the file the approved proposal binds) is not the file resolved in the candidate now (a changelog setting changed since the approval, or a record stamped before #876: neither file is written) | before the stamp: Step 5, item 0 (its `ways`; `content` → the owner fixes the file — re-encodes it / removes the pre-written section, committed as work in progress when the project is in git (outside git there is nothing to commit) — or turns the changelog file off), then re-plan from Step 0; on resume FIRST set a changelog setting consistent with the approved path (which setting the approval was made under is not recorded) — `details.restore`, the `dct project changelog …` / `dct project changelog-path …` commands the refusal names, derived from the stored proposal — on the owner's yes, then `dct release resume`; only when `restore` is empty (a record stamped before #876) or the owner declines, the last resort: `dct release abandon` (the owner's reason) ends the record, its stamps and `current_version` stay |
| `details.tags` | K-8: a tag of V (any spelling) already exists in this repository — the release tags its own release commit, which does not exist yet; refused BEFORE the stamp, nothing released | show the tags; the owner removes a leftover tag themselves (the refusal names the command; the dialog never runs it) or chooses another version; re-plan from Step 0 |
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

Report the requested version (`details.requested_version`), the record's
version (`version` — the authoritative spelling of every artifact) and the
project's `details.current_version` SEPARATELY (K-55 / K-31), and the push —
remote, branch, tag and the `url` of the `push` step:

```
✓ proj-a released: requested 1.1.0 · record 1.1.0 · current_version 1.1.0
  5 entries stamped (#12, #13, #15, #16, #17); attested: #16, #17; left unreleased: #18
  Steps: stamp done · changelog done · version_files done · commit done · index done · tag done · push done · complete done
  Pushed: <remote> <branch> + v1.1.0 → <url>   (or: local release, push skipped)
```

When `.github/workflows/publish.yml` exists in the repository, say that the
pushed tag publishes the package.

## Resume path (K-97 F4, K-111, K-112)

Step 1 routes here (`kind == "resume"`). Show the RECORDED release from the
plan's `resume` (`version`, `status`, `candidate_sha`, `entry_ids`, `proof`,
`effects`, `release_commit_sha`) — a resume continues exactly that, it is
never recomputed on today's HEAD — and, SEPARATELY, today's `resume.head`
(`checkout.matches_record`), the entries added since
(`resume.new_unreleased`: not part of this release) and who finishes it
(`executor.record`). A PR number in the recorded `proof` is shown with its
origin (`pr_source`: recognised in git, or declared by a client).

### A release dct executes (`progress_owner` is `dct`)

The owner approved this release at `apply` (or at a takeover), and that
consent is stored on the record: version, entries, candidate, proposal, push
target. It is not asked for a second time.

- **The owner asked to continue it** ("resume", "finish the release", "go
  on" after a refusal whose cause is gone) and its scope is unchanged → show
  the stored state and run, at once and on the stored consent:

  ```bash
  dct release resume --json
  ```

  No new approval, no menu, no second question — the command takes no
  confirmation flag because there is nothing left to approve. It checks the
  record against THIS checkout (another worktree or HEAD → refused; the
  record is never re-pinned — run it in the record's `checkout_root`),
  verifies each effect, skips what exists and matches, performs what is
  missing (a `tagged` record: ONLY the push) and refuses on a contradiction
  without overwriting. The version sync is never re-run. The release commit,
  the tag and the push are finished by this one command, inside the same
  workflow. Read the result as in Step 10.
- **The request was something else** (a new version, a bare "release" in a
  session that did not start this one): a record in the database never
  starts anything by itself. Show the stored state, say that this release
  is finished first (a new version cannot be stamped over it), and run the
  resume when the owner says to finish it.
- **An earlier explicit "stop"**, or a withdrawn consent, stays binding in
  this conversation: do not resume until the owner says to continue.
- **A refusal or a conflict is reported, not retried**: one run per request
  — never loop; run it again only when the owner says the cause is gone
  (the table of Step 10).
- **The scope changed** (`details.outside_consent`: the record's version,
  entries or candidate differ from the consent) → the stored consent does
  not cover it. Show exactly what differs. A new or wider consent is asked
  for only then — for that concrete change of scope, never as a routine
  question — and the dialog never widens a consent by itself: the owner
  restores the recorded scope (then resume), or decides how the release
  ends.
- **Abandon** only on the owner's explicit request or decision, with the
  owner's reason — it is not a routine option and never the dialog's
  choice. Confirm that the record, the stamps, `current_version` and every
  effect stay and V stays taken (the work is released later under a NEW
  version). Then:

  ```bash
  dct release abandon --version 1.1.0 --reason "<the owner's reason>" --confirm --json
  ```

  This local command is the only way to abandon a release dct executes: the
  MCP tool `abandon_release` refuses such a record (it holds no operation
  lock, so an effect could run past it — K-119 M6).

### A record nobody finishes automatically (`progress_owner` is `ledger`)

`resume` refuses it at every status (`details.executor`, `details.consent`)
— do not run it.

- **The record's `executor` is `own`** → the project's own tool finishes it
  through the ledger path: its stamp carried `expected_executor="own"`, and
  it reports each effect it verified (the tag with its `tag_proof`, a push
  with its `push_proof`) —
  `docs/release-workflow.md#integrating-a-projects-own-release-tool`. Say so
  and hand over; this dialog performs none of it. An `own` record stays with
  its tool: dct never offers the takeover for it, whatever the project's
  setting says today.
- **The record's `executor` is `dct` (or null) and it has no consent** — it
  was stamped outside `dct release apply` (the MCP stamp, an older record, a
  reconstruction that ended at `committed`). Two ways to finish it; the
  owner chooses:

  1. **The takeover** — dct records the missing consent from the VERIFIED
     artifacts (no re-stamp, no new bump). This is a NEW consent and needs
     the owner's explicit approval — "no second question" covers only the
     unchanged resume above. The push decision is explicit (ask: the remote
     and branch, ONE address with `--push-url <url>` when the remote has
     several, or no push). Preview:

     ```bash
     dct release takeover --version 1.1.0 --push-remote origin --push-branch main --json
     dct release takeover --version 1.1.0 --no-push --json                # a local release
     ```

     Outcome `preview` (nothing written): show `details.consent` — version,
     entries, candidate, release commit, tree, the artifacts digest
     (`takeover.artifacts_sha256`), the push target or "no push" —
     `details.stage`, `details.missing_effects` (what resume will still do)
     and keep `details.takeover_digest` (`T`). Ask: `"Hand release
     v<version> to dct with exactly this consent"` / `"Not this — stop"`.
     Only on yes, with the SAME push flags:

     ```bash
     dct release takeover --version 1.1.0 --push-remote origin --push-branch main --takeover-digest <T> --confirm --json
     ```

     Outcome `taken_over` — no effect was performed. Then, in the same
     workflow, `dct release resume --json` performs only the missing
     effects. Refused (no release commit verifies — a `stamped` record with
     nothing made; `details.contradictions`; a digest that no longer
     matches → preview again) → show it; the ledger path stays.
  2. **The ledger path** — whoever performs the missing effects verifies
     them and records the progress (`docs/release-workflow.md#executor-k-85`).
     Never this dialog.

## Legacy path (a release without a record)

```bash
dct release reconstruct --version 1.0.0 --json
```

Outcome `preview` (nothing written): show `details.evidence` —

- its resolved `version` (the stored spelling) and the request
  `requested_version` SEPARATELY;
- the `candidates` (each verified or not, with its reason and its `history`
  state), the `status` the evidence reaches (`completed` with a tag,
  `committed` without one), entry ids, release commit, tag, changelog
  section, version files;
- `evidence.verification` — the scope that WAS verified. `limited: true` =
  no artifact content was verified: only the database's stamp, and in git
  the commit, prove the release — say so plainly;
- `evidence.contract` — the version-file contract: `state: "unknown"`
  (nothing was ever confirmed) is not the same as a `"confirmed"` empty
  list;

and, beside the evidence, `details.executor` — the executor ASSIGNED to the
record now: a responsibility for its further handling (`provenance: false`),
not who made the release — and `details.finish`: the ways to finish a
release the evidence leaves at `committed`. Ask `"Record this release"` /
`"Stop"`. Only on yes:

```bash
dct release reconstruct --version 1.0.0 --confirm --json
```

Outcome `reconstructed`. Refused → show the reason; the owner decides —
never reconstruct by hand, never write the record another way. The step for
each refusal and for finishing:

| situation | step |
| --- | --- |
| several verified candidates, no tag (`details.candidates`) | show them all; dct picks none — the owner picks one: `dct release reconstruct --version V --release-commit <sha> --json`, then the same with `--confirm` |
| the release commit changed older changelog history (`details.history`) | show the diff (`state`, `diff`, `diff_lines`); nothing is recorded and no flag or waiver exists — also for a typo fix; the release stays without a record |
| outside git, the contract is unknown (`details.contract`, `state: "unknown"`) | `dct release version-files --json` (outcome `none` = UNKNOWN, `found` = confirmed); ask the owner which files hold the version, or none; with THAT answer `dct release version-files --set <a,b> --confirm --json` or `dct release version-files --none --confirm --json` (outcome `confirmed`); then reconstruct again |
| the project's executor is `own` (`details.executor: "own"`) | its tool supplies the evidence file: `dct release reconstruct --version V --evidence <file.json> --json`, then the same with `--confirm` — the file comes from the own tool, never composed by this dialog |
| the record ends at `committed` (`details.finish`) | way `takeover` → the takeover of the **Resume path** above (preview, the owner approves that digest, resume); way `ledger` → whoever performs the missing tag and push records them (`docs/release-workflow.md#executor-k-85`; the only way for an `own` record) |

## Rules

- NEVER release without the owner's confirmation; the owner decides the
  work-in-progress commit, every attestation, correction, withdrawal, the
  version, version-only, the version files and the push target.
- ALWAYS run `dct release plan --json` first and route from that ONE result
  (Step 1): an unfinished record by what it stores, a new release by the
  project's setting. An unfinished record is resumed or — on the owner's
  explicit decision — abandoned, never replaced by a new version.
- An unchanged resume needs no second question; a takeover and a changed
  scope need a new, explicit consent; a record alone starts nothing.
- ONE workflow: work in progress is committed inside it (Step 3) on the
  owner's consent, by you — the owner is not sent away for it — and the
  release goes on to the approved push.
- NEVER stamp an entry without proof: attest (Step 6), correct, withdraw or
  keep it. Collective attestation is a proposal the owner confirms.
- ALWAYS pass `--version V` (computed once), `--entries` or, with explicit
  consent, `--no-entries`, the `snapshot_digest` of the plan bound to THAT
  selection, and `--proposal-digest` only after the owner approved the
  prepared files and the push target.
- NEVER retry a conflict or a refusal on your own, and never loop on a lock.
- `dct release abandon` only with the owner's reason and explicit
  confirmation.
- The skill never edits the changelog file FOR THE RELEASE (a rewrite to
  Markdown is the owner's work in progress, Step 3), never runs the
  version sync itself, never commits, tags or pushes FOR THE RELEASE and never calls
  `advance_release` — the workflow does the effects and records them.
