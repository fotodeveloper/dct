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
prepare / apply / resume / abandon / reconstruct / status`, all with `--json`
(schemas, order of effects, refusals: `docs/release-workflow.md`; code:
`dct/workflows/release/`). This skill runs the DIALOG from the first plan to
the pushed tag in ONE workflow: it shows what the workflow reports, asks the
owner, passes the owner's decisions back as flags — and, when the owner
agrees, commits the work in progress itself (Step 3) without leaving this
workflow. The authoritative step list is the `/release` fallback of
`docs/assistant-fallbacks.md`; this skill follows it step for step.

The workflow performs and verifies every release effect and records the
progress in the release record itself. This skill therefore:
- never edits the changelog file, never runs a version-sync script;
- never stages, commits or tags FOR THE RELEASE (the attestation commit, the
  release commit and the tag are made by `dct release attest` / `apply` /
  `resume`); the only commit it makes is the owner's work in progress
  (Step 3), with the `/commit` procedure;
- never calls `advance_release` and never stamps through `release_changelog`
  — the ledger is written by the workflow;
- does not run a push of its own: the push of the release commit and the tag
  is a step of `apply` / `resume`, to the push target the owner approved
  (Step 8);
- never resets, stashes or cleans the owner's work, never uses the
  `clog release … --confirm` alias (it uses `apply`, like the fallback).

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
- `schema_version` lower than 3 (the plan has no `executor` key — an older
  CLI) → STOP with the same message.
- Exit 1 (the checkout could not be verified, project not found) → show the
  message and STOP. Never retry from another directory.

Load the two MCP tools the dialog may need for corrections with ONE call:

```
ToolSearch("select:mcp__dct__update_changelog,mcp__dct__delete_changelog")
```

### Step 1: Executor (K-85)

Run `dct project release-executor <plan.project>` (read-only without a
value).

- `own` → the project's OWN release tool performs the effects: dct's
  `prepare` / `apply` / `resume` refuse it before any write
  (`details.executor`). Show the plan (Step 4) as the shared proof, say that
  this project releases through its own integration
  (`docs/release-workflow.md#integrating-a-projects-own-release-tool`) and
  STOP. Never run a command stored anywhere in its place.
- `dct` → continue.

### Step 2: Unfinished release, legacy release

- `kind == "resume"` (`unfinished_release` is a record) → an earlier release
  did not finish. Follow the **Resume path** below — never start a new
  version on top of it.
- `version.current` set → `dct release status --version <current> --json`.
  Outcome `none` means the current version was released before release
  records existed. Offer the **Legacy path** below (recommended: it records
  the old release from evidence, without re-stamping), or continuing without
  it, or stopping.

### Step 3: Work in progress — committed in this workflow (K-97 F1)

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
workflow, following the `/commit` procedure (the `commit` skill, Steps 1–8;
its version-bump redirect to `/release` does not apply — you are in it):

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
4. Then re-plan: `dct release plan --json` again and continue from Step 2 —
   the new HEAD is the candidate the later steps pin (Step 7 binds it, Step 8
   and Step 9 refuse any other).

The question never ends or splits the release: after the commit the dialog
goes on; `"Stop"` ends it with nothing released.

### Step 4: Show the plan (plain text)

Print, from the plan:

```
Project: proj-a — current 1.0.0
Candidate: abc1234 (feat/x) — range v1.0.0..abc1234 (12 commits)

Proven (4), by branch / merge:
  feat/x — merge def5678 (PR #41)
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

### Step 5: The owner's decisions (AskUserQuestion)

Ask in as few calls as the question limit allows, in this order:

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
run). Outcome `attested` → the candidate changed: run
`dct release plan --version V --json` again, show what changed and continue
with Step 7 only when the attested ids are now proven. Outcome `refused` →
show `details.error` and return to Step 5.

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

### Step 8: Prepare — the version files and the push target (K-76, K-97 F3)

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
- the push target `details.push_target` — remote, URL, branch, tag `v<V>`
  (the branch's upstream). `null` (no upstream, see `warnings`) → the owner
  chooses a remote and a branch (`--push-remote <remote> --push-branch
  <branch>`) or a local release (`--no-push`); never guess one.

Then ask: `"Release with these files, pushed to <remote>/<branch>"` /
`"Other push target"` / `"Local release — no push"` / `"Not these files —
stop"`. Keep `details.proposal_sha256` (`P`). Outcome `failed` → the script
failed or timed out (exit code 124; `details.sync`): nothing stamped; the
owner fixes the script, then prepare again. `refused` → the refusal table
below.

### Step 9: Apply — only on the owner's yes

```bash
dct release apply --version 1.1.0 --entries 12,13,15,16,17 --plan-digest <D> --proposal-digest <P> --confirm --json
dct release apply --version 1.1.0 --entries 12,13,15,16,17 --plan-digest <D> --proposal-digest <P> --push-remote origin --push-branch main --confirm --json   # another push target
dct release apply --version 1.1.0 --no-entries --plan-digest <D> --proposal-digest <P> --no-push --confirm --json   # version-only, local
```

The first form pushes to the upstream shown in Step 8; pass the push flags
the owner chose, nothing else. The workflow stamps, writes the changelog
section and the approved version files, makes the `release: v<V>` commit
through the repository's hooks, updates the real index, tags `v<V>` and
pushes the commit and the tag atomically (K-97 F2) — each verified and
recorded. Never pass a digest the owner has not seen.

### Step 10: Read the result and report

- Exit 0, outcome `completed` → report (below).
- Outcome `noop` → the version is already released (a recorded, historical
  or legacy release) — nothing was done. A warning naming `dct release
  reconstruct` → offer the **Legacy path**.
- Exit 1, outcome `refused` → show `details.error`, then act by the table
  below. Only the listed `steps` happened. A `release` that is unfinished →
  the **Resume path**.
- Outcome `conflict` (exit 75) → the conflict row of the table.

Refusals (the keys are the workflow's own, `dct/workflows/release/effects.py`):

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

Report the requested version (`details.requested_version`), the record's
version (`version` — the authoritative spelling of every artifact) and the
project's `details.current_version` SEPARATELY (K-55 / K-31), and the push:

```
✓ proj-a released: requested 1.1.0 · record 1.1.0 · current_version 1.1.0
  5 entries stamped (#12, #13, #15, #16, #17); attested: #16, #17; left unreleased: #18
  Steps: stamp done · changelog done · version_files done · commit done · index done · tag done · push done · complete done
  Pushed: <details.push_target.remote> <branch> + v1.1.0   (or: local release, push skipped)
```

When `.github/workflows/publish.yml` exists in the repository, say that the
pushed tag publishes the package.

## Resume path (K-97 F4)

Show the RECORDED release from the plan's `resume` (`version`, `status`,
`candidate_sha`, `entry_ids`, `proof`, `effects`, `release_commit_sha`) —
the resume continues exactly that, it is never recomputed on today's HEAD —
and, SEPARATELY, today's `resume.head` (`checkout.matches_record`) and the
entries added since (`resume.new_unreleased`: not part of this release).

A record whose `executor` is `own`, or without a `consent` (stamped outside
`dct release apply` — MCP `release_changelog`, an own tool, a partial
reconstruction), is NOT finished by dct: resume refuses it. Say who
finishes it — the project's own tool, or the owner through the ledger path
after verifying each effect (`docs/release-workflow.md#executor-k-85`; never
this dialog) — offer the takeover when its `executor` is `dct` and its
release commit exists (the last row of the Legacy path table below: dct
records the consent from the verified artifacts, then `resume` performs
only the missing effects) — or offer to abandon it.

Otherwise ask: `"Finish release v<version>"` / `"Abandon release
v<version>"` / `"Stop"`.

- **Finish** → `dct release resume --json`. No new approval: it executes the
  consent stored at apply (version, entries, candidate, proposal, push
  target) while the scope is unchanged, and refuses what lies outside it
  (`details.outside_consent`). It checks the record against THIS checkout
  (another worktree or HEAD → refused; the record is never re-pinned — run
  it in the record's `checkout_root`), verifies each effect, skips what
  exists and matches, performs what is missing (a `tagged` record: ONLY the
  push) and refuses on a contradiction without overwriting. The version sync
  is never re-run. Read the result as in Step 10.
- **Abandon** → ask for the owner's reason (free text), and confirm that the
  record, the stamps, `current_version` and every effect stay and V stays
  taken (the work is released later under a NEW version). Then:

```bash
dct release abandon --version 1.1.0 --reason "<the owner's reason>" --confirm --json
```

## Legacy path (a release without a record)

```bash
dct release reconstruct --version 1.0.0 --json
```

Outcome `preview`: show `details.evidence` — its resolved `version` (the
stored spelling) and the request `requested_version` SEPARATELY, the
`candidates` (each verified or not, with its reason), the `status` the
evidence reaches (`completed` with a tag, `committed` without one), entry
ids, release commit, tag, changelog section, version files — and ask
`"Record this release"` / `"Stop"`. Only on yes run the same command with
`--confirm`. Also show `evidence.verification` (the scope that was
verified; `limited: true` = only the database's stamp, and in git the
commit, prove the release — say so) and `details.executor` (the executor
ASSIGNED to the record now — a responsibility, not who made the release).
Refused → show the reason; the owner decides — never reconstruct by hand,
never write the record another way. The CLI steps for each refusal and for
finishing (`details.finish`):

| situation | CLI step |
| --- | --- |
| several verified candidates, no tag (`details.candidates`) | the owner picks one: `dct release reconstruct --version V --release-commit <sha> [--confirm] --json` |
| the release commit changed older changelog history (`details.history`: the diff) | show the diff; nothing is recorded and there is no flag that waives it |
| outside git, the contract is unknown (`details.contract.state: "unknown"`) | `dct release version-files --json` (show), then with the owner's answer `dct release version-files --set <a,b> --confirm --json` or `dct release version-files --none --confirm --json`; then reconstruct again |
| the project's executor is `own` (`details.executor: "own"`) | its tool supplies the evidence: `dct release reconstruct --version V --evidence <file.json> [--confirm] --json` — never composed by this dialog |
| the record ends at `committed` (`details.finish`) | `dct release takeover --version V (--push-remote R [--push-branch B] [--push-url U] \| --no-push) --json` (preview: `details.consent`, `details.missing_effects`, `details.takeover_digest`); after the owner approves exactly that: the same command with `--takeover-digest <digest> --confirm --json`; then `dct release resume --json`. Or the ledger path of whoever performs the missing effects (`docs/release-workflow.md#executor-k-85`; never this dialog) |

## Rules

- NEVER release without the owner's confirmation; the owner decides the
  work-in-progress commit, every attestation, correction, withdrawal, the
  version, version-only, the version files and the push target.
- ALWAYS run `dct release plan --json` first and read the executor; an
  unfinished record is resumed or abandoned, never replaced by a new version.
- ONE workflow: work in progress is committed inside it (Step 3) on the
  owner's consent — never send the owner away to commit it.
- NEVER stamp an entry without proof: attest (Step 6), correct, withdraw or
  keep it. Collective attestation is a proposal the owner confirms.
- ALWAYS pass `--version V` (computed once), `--entries` or, with explicit
  consent, `--no-entries`, the `snapshot_digest` of the plan bound to THAT
  selection, and `--proposal-digest` only after the owner approved the
  prepared files and the push target.
- NEVER retry a conflict or a lock refusal on your own.
- `dct release abandon` only with the owner's reason and explicit
  confirmation.
- The skill never edits the changelog file, never runs the version sync
  itself, never commits, tags or pushes FOR THE RELEASE and never calls
  `advance_release` — the workflow does the effects and records them.
