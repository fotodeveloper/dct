---
name: release
description: >-
  [dct] Release a version — review the proof plan with the owner, settle
  entries without proof, then let the local `dct release` workflow stamp,
  write the changelog and version files, commit and tag, each verified. Use
  when: "release", "bump version", "ship it" — in any language.
---

# /release — the dialog over the `dct release` workflow

The release is EXECUTED by the local workflow — `dct release plan / attest /
apply / resume / abandon / reconstruct / status`, all with `--json` (schemas,
order of effects, refusals: `docs/release-workflow.md`; code:
`dct/workflows/release/`). This skill only runs the DIALOG: it shows what the
workflow reports, asks the owner, and passes the owner's decisions back as
flags.

The workflow performs and verifies every effect and records the progress in
the release record itself. This skill therefore:
- never edits the changelog file, never runs a version-sync script;
- never stages, commits or tags for the release (the attestation and release
  commits and the tag are made by `dct release attest` / `apply` / `resume`);
- never calls `advance_release` and never stamps through `release_changelog`
  itself — the ledger is written by the workflow;
- never pushes, never resets, stashes or cleans the owner's work.

Run every command in the CURRENT checkout (your shell's working directory),
never in the MCP server's directory. Add `-P <slug>` only when the project is
not detected from the CWD.

Present long content (the plan, the evidence) as plain message text FIRST;
the AskUserQuestion that follows only references it (two-turn pattern).

## Workflow

### Step 0: Capability probe and the plan

Run `dct release plan --json`.

- Command missing, or exit 2 with `invalid choice` / an unknown argument →
  STOP: "The local dct CLI is missing or older than this skill — install or
  upgrade dct; the release workflow runs in the CLI." Do NOT fall back to the
  MCP tools: the ledger tools alone cannot verify the changelog file, the
  version files, the commit or the tag.
- Exit 1 (the checkout could not be verified, project not found) → show the
  message and STOP. Never retry from another directory.

Load the two MCP tools the dialog may need for corrections with ONE call:

```
ToolSearch("select:mcp__dct__update_changelog,mcp__dct__delete_changelog")
```

### Step 1: Unfinished release, legacy release

- `kind == "resume"` (`unfinished_release` is a record) → an earlier release
  did not finish. Follow the **Resume path** below — never start a new
  version on top of it.
- `version.current` set → `dct release status --version <current> --json`.
  Outcome `none` means the current version was released before release
  records existed. Offer the **Legacy path** below (recommended: it records
  the old release from evidence, without re-stamping), or continuing without
  it, or stopping.

### Step 2: A clean tree

`dct release apply` refuses unless the index and the work tree are clean
(`index.clean`, no `conflicted` path, no untracked release artifact) and no
merge / rebase / cherry-pick / revert is in progress — and it never resets,
stashes or cleans. When the plan's `index` is not clean, show it and ask:
`"Commit the work in progress with /commit first"` (the `/commit` dialog:
stage → select the trailer → commit, as its own commit below the release) /
`"Stop"`. After `/commit`, run `dct release plan --json` again.

### Step 3: Show the plan (plain text)

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
  `merge_commit` / `pr` (K-70); each shows its proving `commits`. The public changelog never
  carries branch names — this grouping is for the review only.
- Unproven entries (`entries.unproven`) show their `reason` (`no_trailer`, `reverted` + the
  reverting commit, `below_range`, `squash_suspected` + the group, `no_git`).
- `range.confirm_required` → say: "The proof range starts at `<base_tag>`,
  not at the current version's tag `<expected_tag.name>`" — the owner
  confirms it in Step 4. `range.approximate` → WARN: "No ancestor release
  tag — the range is approximate (`--since=<since>`)". No range (a project
  outside git) → say that its entries have no commit proof and are released
  as the owner selects them.
- `foreign_citations`, `reverts` with status `outside_range` or `ambiguous`,
  and every `warnings` line: shown as they are.
- `version_files.version_files` null → "Version files: to be learned at this
  first release" (Step 6 — `dct release prepare` shows them for approval).

### Step 4: The owner's decisions (AskUserQuestion)

Ask in as few calls as the question limit allows, in this order:

1. **Range** (only when `confirm_required`): `"Use <base_tag> as the base"` /
   `"Stop"`.
2. **Collective attestation** — one question per `attestation_groups` item
   (K-69): `"Attest #16, #17 together — their changes are in the
   candidate"` / `"Decide each entry"` / `"Leave them unreleased"`. A
   proposal only; nothing is attested without the owner's yes.
3. **Each remaining entry without proof** (null branch included):
   - `"Attest — the change is in the candidate"` → collected for Step 5;
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

### Step 5: Attest (only when the owner chose it)

```bash
dct release attest --entries 16,17 --confirm --json
```

The workflow creates the empty commit `chore: attest changelog entries`
whose final paragraph is `Changelog: <ids>` (only ids the plan lists as
unproven; nothing staged; no release unfinished). Outcome `attested` → the
candidate changed: run `dct release plan --version V --json` again, show what
changed and continue with Step 6 only when the attested ids are now proven.
Outcome `refused` → show `details.error` and return to Step 4.

### Step 6: Confirm, then apply

First bind the decision — the plan of exactly this version and selection
(K-80; the approval is of THIS snapshot):

```bash
dct release plan --version 1.1.0 --entries 12,13,15,16,17 --json
dct release plan --version 1.1.0 --no-entries --json               # version-only
```

Build the summary from THAT plan's `snapshot` (candidate, selected ids,
proof) and keep its `snapshot_digest`. Show the summary and wait for the
owner's yes:

```
Release proj-a 1.1.0?
  Candidate: def5678 (feat/x)
  Stamped (proven): #12, #13, #15, #16, #17
  Left unreleased: #18
  Version: 1.0.0 → 1.1.0
```

- **No entries selected** → ask explicitly: "No proven entries. Release
  version V only (no changelog entries)?" A version-only release is a real
  release and needs this consent (`--no-entries` states the intent; it is not
  the consent). Say the side effects: the `since=last_release` anchor does not
  move, and a full changelog export omits V. No → stop.
Prepare the version files BEFORE anything is stamped (K-76) — the
version-sync script runs once in an isolated temporary worktree; nothing in
the checkout changes:

```bash
dct release prepare --version 1.1.0 --entries 12,13,15,16,17 --plan-digest <snapshot_digest> --json
dct release prepare --version 1.1.0 --no-entries --plan-digest <snapshot_digest> --json   # version-only
```

Show `details.proposal.paths` (one per line), `details.checks` and, when
`details.contract.change` is true (a first release, or the set differs
from the confirmed contract — an empty list is a contract too), say so;
ask `"These are the version files"` / `"Not these — stop"`. Outcome
`failed` → the script failed or timed out (`details.sync`): the owner fixes
it, prepare again. Only on the owner's yes:

```bash
dct release apply --version 1.1.0 --entries 12,13,15,16,17 --plan-digest <snapshot_digest> --proposal-digest <proposal_sha256> --confirm --json
dct release apply --version 1.1.0 --no-entries --plan-digest <snapshot_digest> --proposal-digest <proposal_sha256> --confirm --json   # version-only
```

A refusal saying the context "differs from the approved plan" (HEAD moved,
an entry was added or edited, the selection or version differs) or that
voids the proposal (another candidate / version / plan, a changed script or
baseline, a newer prepare) → show `details`, re-run plan / prepare and ask
again; never pass a digest the owner has not seen.

### Step 7: Read the result and report

- Exit 0, outcome `completed` → report (below).
- Outcome `noop` → the version is already released (a recorded, historical
  or legacy release) — nothing was done. A warning naming `dct release
  reconstruct` → offer the **Legacy path**.
- Exit 1, outcome `refused` → show `details.error` and the refusal's data
  (`staged`, `unproven`, `foreign_paths`, `foreign_content`, `changed`,
  `contract`, …). Only the listed `steps` happened. A `release` that is
  unfinished → the **Resume path**.
- Outcome `failed` (prepare) → the version-sync script failed or timed out
  (its exit code is the command's, 124 on timeout): nothing was stamped; the
  owner fixes the script, then prepare again.

Report the requested version (`details.requested_version`), the record's
version (`version` — the authoritative spelling of every artifact) and the
project's `details.current_version` SEPARATELY (K-55 / K-31):

```
✓ proj-a released: requested 1.1.0 · record 1.1.0 · current_version 1.1.0
  5 entries stamped (#12, #13, #15, #16, #17); attested: #16, #17; left unreleased: #18
  Steps: stamp done · changelog done · version_files done · commit done · tag done · complete done
  Tag v1.1.0: local only
```

Pushing is the owner's action — this skill never pushes; name the command
for the owner (`git push origin <branch> v<V>`). When
`.github/workflows/publish.yml` exists in the repository, say that pushing
the tag publishes the package.

## Resume path

Show the RECORDED release from the plan's `resume` (`version`, `status`,
`candidate_sha`, `entry_ids`, `proof`, `effects`, `release_commit_sha`) —
the resume continues exactly that, it is never recomputed on today's HEAD —
and, SEPARATELY, today's `resume.head` (`checkout.matches_record`) and the
entries added since (`resume.new_unreleased`: not part of this release),
then ask: `"Finish release v<version>"` /
`"Abandon release v<version>"` / `"Stop"`.

- **Finish** → `dct release resume --json`. The workflow checks the record
  against THIS checkout (another worktree or HEAD → refused; the record is
  never re-pinned — run it in the record's `checkout_root`), verifies each
  effect, skips what exists and matches, performs what is missing and refuses
  on a contradiction without overwriting (`foreign`: a file differs from
  both its baseline and the approved content — the owner restores or
  finishes it). The version sync is never re-run. Read the result as in
  Step 7.
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

Outcome `preview`: show `details.evidence` (entry ids, candidate, release
commit, tag, changelog section, version files) and ask `"Record this
release"` / `"Stop"`. Only on yes run the same command with `--confirm`.
Refused → the evidence is ambiguous: show the reason; the owner decides —
never reconstruct by hand, never write the record another way.

## Rules

- NEVER release without the owner's confirmation; the owner decides every
  attestation, correction, withdrawal, the version and version-only.
- ALWAYS run `dct release plan --json` first; an unfinished record is resumed
  or abandoned, never replaced by a new version.
- NEVER stamp an entry without proof: attest (Step 5), correct, withdraw or
  keep it. Collective attestation is a proposal the owner confirms.
- ALWAYS pass `--version V` (computed once) and `--entries` or, with explicit
  consent, `--no-entries`.
- `--proposal-digest` only after the owner confirmed the prepared file list.
- `dct release abandon` only with the owner's reason and explicit
  confirmation.
- The skill never edits the changelog file, never runs the version sync
  itself, never commits, tags or pushes for the release and never calls
  `advance_release` — the workflow does the effects and records them.
