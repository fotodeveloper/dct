# Release workflow — proof layer and effects layer

The release workflow is layered (K-72):

1. **Proof layer** (this document) — `dct release plan --json` and
   `dct clog candidates --json`. Read-only: no database write, no ref, index
   or work-tree change (every git read runs with `--no-optional-locks`).
   Usable by every project, also one whose own release tool performs the
   effects.
2. **Ledger layer** — core / MCP: `release_changelog`, `get_release`,
   `advance_release` (`dct/core/releases.py`).
3. **Effects layer** — `dct release attest / prepare / apply / resume /
   abandon / reconstruct` (changelog file, version files, release commit, tag,
   push), the
   default behind `/release` ([below](#the-effects-layer)). A project with its
   own release tool replaces only this layer: it verifies its own effects and
   reports progress through `advance_release`. Which of the two performs a
   release is EXPLICIT — the project's `release_executor` (`dct` | `own`),
   copied onto each record at its stamp ([Executor](#executor-k-85)).

Implementation: `dct/workflows/release/proof.py` (pure rules over commit
messages), `dct/workflows/release/plan.py` (git + database reads) and
`dct/workflows/release/effects.py` (the effects layer). The
rules in prose: [assistant-fallbacks.md](assistant-fallbacks.md#changelog-proof-trailers-and-range-shared-by-commit-and-release).

Both documents carry `schema_version` (currently `2` — version 2 added
`resume`, `snapshot`, `snapshot_digest`, the graph-nearest range base, the
merge-revert mainline and the per-recognition provenance; a `(#N)` subject
suffix is no longer a `pr`); it moves when the meaning of a key changes. The top-level keys below are pinned by
`tests/test_release_plan.py::test_json_documents_keep_their_top_level_keys`.
Datetimes are ISO 8601 strings (UTC when stored naive). Paths in `index`
are relative to the repository root.

## `dct release plan [-P slug] [--version V | --patch | --minor | --major] [--entries … | --no-entries] [--json]`

The checkout is verified STRICTLY (K-52): the current directory must be a
checkout (main or linked worktree) of the project's repository with a first
commit; anything not confirmed exits 1 — never a fallback to the main
checkout. A project whose registered path is CONFIRMED outside git gets a
plan without a range (its entries: reason `no_git`); a project without a
registered path gets reason `no_path` — the absence of a repository is not
confirmed (K-95 C1), and `apply` refuses it.

`--entries` / `--no-entries` choose the selection the **snapshot** binds
(default: every proven id — plus, outside git, every entry). The snapshot is
what the owner approves: `dct release prepare` and `apply` require its `snapshot_digest`
(below).

| key | content |
| --- | --- |
| `schema_version` | `1` |
| `project` | slug |
| `kind` | `"release"`, or `"resume"` when an unfinished release record exists (then `resume` is set and nothing is computed on today's HEAD) |
| `version` | `{current, target, bump}` — `target` from `--version` (normalized) or the bump; for a resume without a version, the record's version |
| `checkout` | `{git: true, repo_root, project_root, candidate_sha, head (full HEAD), branch (null on a detached HEAD), detached, matches_record?}` or `{git: false, reason: "no_path" \| "not_a_repository", project_root}`; `candidate_sha` = HEAD for a release, the RECORDED candidate for a resume (K-95 C5); `matches_record` (resume only) = `{candidate_sha, checkout_root}` booleans against the record |
| `unfinished_release` | the unfinished release record (`core.releases.RECORD_FIELDS`) or `null` |
| `range` | see below; `null` without git |
| `entries` | `{proven: [...], unproven: [...]}` — every unreleased entry of the project, by id |
| `foreign_citations` | `[{id, reason: "unknown" \| "other_project" \| "released" \| "deleted", version, commits}]` — cited in the range, never stamped |
| `reverts` | `[{sha, target, target_sha, status: "in_range" \| "outside_range" \| "ambiguous" \| "unconfirmed", mainline, reason, effective, affected}]` — see [Reverts](#reverts-k-68-k-82) |
| `squash_commits` | `[{sha, subject, signals, pr, lost_ids, proves}]` — `signals` ⊆ `pr_suffix`, `local_squash`, `indented_trailers` |
| `attestation_groups` | `[{branch, entry_ids, squash_commits, signals, proposal}]` — ONE proposed collective attestation per source branch; `signals` ⊆ `indented_trailer`, `branch_named`, `branch_commits_absent` |
| `proof` | the proof map the release record stores: `{"<id>": {commits, merge_commit, pr, source, ambiguous, associations}}` (`core.releases.validate_proof`; provenance below) |
| `index` | `{clean, staged, unstaged, untracked, conflicted, release_artifacts: [{path, role: "changelog" \| "version_file", staged, unstaged, untracked, conflicted}]}` — `clean` ignores untracked files; `staged` = the index differs from HEAD; `null` without git |
| `version_files` | the project's version-file contract `{version_files (null = to be learned at the first release), confirmed_at, history}` |
| `warnings` | human-readable strings (range to confirm, approximate range, revert outside the range or unconfirmed, unfinished release, HEAD not the recorded candidate, selection holding unproven ids, no git) |
| `resume` | `null`, or for a resume plan (K-95 C5) the RECORDED release: `{version, status, candidate_sha, checkout_root, release_mode, entry_ids, proof, release_commit_sha, manifest, effects: {stamped, files_written, committed, tagged, pushed, completed} (booleans by status), head (today's HEAD, separately), head_is_candidate, new_unreleased (entries added since — NOT part of the record)}`. A resume plan has `range: null`, empty `entries` and `proof`, `snapshot: null` — an approved release is never recomputed on today's HEAD |
| `snapshot` | the IMMUTABLE approved context (K-80): `{project, version, current_version, git, repo_root, checkout_root, project_root, candidate_sha (full), range: {base_tag, base_sha, approximate, since, revs}, selected_ids, proof (the map of the selected ids), entries: {"<id>": state digest}}` — `entries` covers EVERY unreleased entry the plan saw (`core.changelog.entry_state_digest` over id, category, entry, component, version); `null` for a resume plan and for a project without a path |
| `snapshot_digest` | SHA-256 (hex) of the snapshot's canonical JSON (sorted keys, compact separators, UTF-8) — what `dct release apply --plan-digest` takes |

**`range`**: `{candidate_sha, base_tag, base_sha, base_version,
base_distance, alternatives, expected_tag, confirm_required, approximate,
since, revs, display, below_scan, commit_count}`.

- `base_tag` (K-83): the NEAREST release tag that is an ancestor of the
  candidate — the minimum number of PARENT EDGES from the candidate to the
  tagged commit (`base_distance`; a breadth-first search over `git rev-list
  --parents`, never `rev-list --count TAG..HEAD`, which counts commits, and
  never the highest version). Annotated tags count at their commit. Ties at
  that distance — one deterministic choice: the exact `v<current_version>`,
  then a name of the current version in another spelling, then a
  `v`-prefixed name, then the lowest name; every other tag at the same
  distance is listed in `alternatives` (`[{name, sha, version,
  distance}]`). Versions are only compared (the never-rewind order), never
  used to rank the base. Tags whose name is not a version are ignored.
- `expected_tag`: `{name: "v<current_version>", exists, ancestor}` (null
  without a current version). `confirm_required` is true when the base is
  not the current version's tag, or when another tag on a DIFFERENT commit
  is equally near — show it (and the alternatives) and confirm it.
- `below_scan` (K-95 C3): `{since, until (= base_sha), bounded: true}` —
  the window scanned below the base for the `below_range` diagnosis
  (`--since` = the oldest unreleased entry minus one day); "not found in
  the scanned window" never means "not in history". `null` without a base
  or without unreleased entries.
- No ancestor release tag: `approximate: true`, `since` = the oldest
  unreleased entry's `created_at` minus one day, `revs = ["--since=…",
  candidate]`; without unreleased entries the range is empty (`revs: null`).

**Entry objects** (both lists): `{id, category, entry, component, source_file,
branch, worktree, branch_source, uncertain, created_at}` — `uncertain` is
true when `branch_source` is not `client` (K-53: server-detected or no
provenance; still classified by proof). Plus:

- proven: `commits` (proving SHAs, oldest first) and the provenance
  (K-70, K-89): ONE recognition per proving commit — a proving commit that
  is itself a real merge names ITSELF (`source: "own_merge"`), else the
  first-parent merge that brought it in (`"introducing_merge"`);
  `merge_commit` + `pr` come from that same recognition (`pr` = the merge's
  `Merge pull request #N` subject, else null — never borrowed from another
  commit). `associations` lists every recognition `{merge_commit, pr,
  source, certain}`, including a `… (#N)` subject suffix as `source:
  "subject_suffix", certain: false` (it may be an issue number — never the
  entry's `pr`, K-95 C4). Several DIFFERENT merge associations →
  `ambiguous: true`, `merge_commit` / `pr` / `source` null — never one
  picked;
- unproven: `reason` ∈ `no_trailer` (no proof in the range nor in the
  bounded below scan), `reverted` (+ `commits`, `reverted_by`),
  `revert_unconfirmed` (+ `commits`, `reverted_by`: an unconfirmed revert
  may touch its proving commits — confirm or attest, K-82), `below_range`
  (+ `commits`: cited only below the base, inside `range.below_scan`),
  `squash_suspected` (+ `group`: index into `attestation_groups`), `no_git`
  (a path CONFIRMED outside git), `no_path` (no registered path).

<a id="reverts-k-68-k-82"></a>**Reverts** (K-68, K-82). A standard revert
of a range commit neutralizes its proof (`status: "in_range"`); a revert of
a revert restores it, and so on. A revert of a MERGE M takes its mainline
from git's message (`This reverts commit <M>, reversing changes made to
<P>.`): P must match exactly ONE real parent of M (`mainline`), and the
neutralized commits are M plus `rev-list M ^P` — what that revert removed
(`-m 1` and `-m 2` differ), never a fixed parent 1. The revert must be FULL:
its own change equals the inverse of M → P (or, for an ordinary commit T,
of T → its parent), compared without line positions. Otherwise the report
is `status: "unconfirmed"` with `reason` ∈ `no_mainline` (missing or edited
message), `mainline_not_a_parent`, `mainline_ambiguous`,
`mainline_on_non_merge`, `partial` (a conflict resolved differently, an
edited revert): nothing is neutralized automatically and every entry the
revert may touch (without a usable mainline: what M brought in relative to
ANY parent) gets `revert_unconfirmed` — never a guess. A plain first-parent
merge without a revert is unchanged.

**Squash heuristic and its limits.** A non-merge commit of the range looks
like a squash when its subject ends `(#N)`, its message starts with git's
`Squashed commit of the following:`, or it carries indented `Changelog:`
lines. An unproven entry is linked to such a commit by (strongest first) its
id on an indented line, its client-confirmed branch named in the message, or
that branch's local / remote-tracking ref still holding commits that prove
it but are absent from the candidate's ancestry (linked to every squash
commit of the range). Limits: an ordinary commit whose subject ends with an
issue reference `(#12)` looks like a squash; an entry whose branch ref was
deleted and is not named anywhere gets no proposal (it stays `no_trailer`);
entries with server / null provenance link only through an indented id.
GitLab-style merge requests (`See merge request …!N`) are not parsed for a
PR number. The groups are proposals — the owner confirms an attestation;
a squash's `pr` in `squash_commits` is the heuristic suffix number of that
proposal only, never proof and never a public PR reference (K-95 C4).

## Merge policy

Proof travels in commit MESSAGES, so the way a branch reaches the release
line decides whether its entries arrive proven (K-69). Recommended, in any
order:

- **merge commit** — every original commit, trailer included, stays in the
  candidate's ancestry;
- **rebase merge** (fast-forward or rebase-and-merge) — the commits are
  rewritten, their messages and trailers kept;
- **hosted squash that keeps the commit messages** — the squash body
  concatenates the original messages, each `Changelog:` line still in
  column 0 (the strict body scan reads it).

Proof is lost by:

- **title-only squash** ("PR title" / "PR title and description" as the
  squash message) — the commit bodies, and with them every trailer, are
  dropped; nothing is left to read;
- **local `git merge --squash`** — git writes `Squashed commit of the
  following:` and INDENTS each original message by four spaces; an indented
  `    Changelog: 12` is deliberately not proof (indented and quoted lines
  never count, K-66).

Such entries end up unproven. The plan recognises squash-looking commits and
proposes ONE collective attestation per source branch (`attestation_groups`,
heuristic and its limits: "Squash heuristic and its limits" above);
the owner confirms it and `dct release attest` writes the proof — never an
automatic assignment. Projects that must squash with title only can attest
each branch's entries right after the merge, while the evidence is fresh.

## `dct clog candidates [-P slug] [--json]`

Best-effort location (read-only; for `/commit`, K-53).

| key | content |
| --- | --- |
| `schema_version` | `1` |
| `project` | slug |
| `checkout` | `{git: true, toplevel, branch, head, detached}` or `{git: false}` |
| `range` | the `range` object of the plan for target HEAD, or null |
| `confirmed` | client-provenance entries of the current branch whose worktree is this toplevel |
| `uncertain` | entries with server-detected or no provenance — whatever branch / worktree they store — and, on a detached HEAD, every client entry of this checkout; a client entry of the current branch whose worktree is NOT recorded (K-95 C2: a candidate to compare with the diff, never confirmed); each with `uncertain: true` and `uncertain_reason` ∈ `server_detected`, `no_provenance`, `detached_head`, `no_worktree`. NEVER attached automatically: match against `staged_files`, confirm (`update_changelog` branch + worktree) first |
| `other_branch` | client entries of this worktree recorded on another branch (offer to correct the branch) |
| `already_proven` | ids a commit of the range already proves — removed from every list |
| `staged_files` | `git diff --cached --name-only` (repo-root-relative) |
| `warnings` | detached HEAD, not a checkout, unreadable range |

Client entries of other checkouts are not listed.

## The effects layer

`dct/workflows/release/effects.py` — ONE implementation for the CLI and the
skills of both clients (they only run the dialog). Every effect is VERIFIED
against reality before the ledger records its progress; a status is never
taken as proof. The release commit and its tag are pushed to the approved
push target (K-97 F2). The MCP server never runs this code.

**Operation lock** (K-96 D7, `dct/workflows/release/oplock.py`): every
MUTATING command — `prepare`, `apply`, `resume`, `attest`, `abandon`,
`reconstruct --confirm`, the `clog release` alias and the old ledger
commands `clog release-advance` / `clog release-abandon` (K-96 D10) — holds ONE lock per
repository from before its pre-checks / stamp until it returns:
`<git common dir>/dct-release.lock` (shared by the main checkout, every
linked worktree and every dct project inside that repository), for a project
CONFIRMED outside git `<data dir>/release-locks/<sha256 of its realpath>.lock`
(`dct.paths.release_lock_dir`). An advisory `flock` (Windows: `msvcrt`
byte lock), released by the kernel when the holder dies; the file is never
deleted. A second mutating command refuses AT ONCE (no waiting) with the
holder's pid / action / start time (`details.lock`, `details.holder`). It
is not git's `index.lock`: editors and other git processes are never
blocked; no SQL transaction is held around the effects. Read-only commands
(`plan`, `status`, the `reconstruct` preview) take no lock.

### Commands

The ONE path of a release: `plan` (read-only, approve its
`snapshot_digest`) → `prepare` (the isolated version sync — approve its
`proposal_sha256`, which shows the file list / contract BEFORE the stamp,
K-97 F3) → `apply` → `resume` after an interruption.

| command | effect |
| --- | --- |
| `dct release attest --entries 16,17 --confirm` | the EMPTY attestation commit on the current branch (or detached HEAD): subject `chore: attest changelog entries`, final paragraph `Changelog: <ids ascending>`. Only ids the plan lists as unproven; refused when anything is staged, an operation (merge, rebase, cherry-pick, revert) is in progress, or a release is unfinished. Created by `git commit --allow-empty` (the repository's hooks run, K-96 — see [Commits and hooks](#commits-and-hooks-k-96-k-79)) from a temporary index holding HEAD's tree; re-validated: parent = the old HEAD, tree unchanged, subject, the trailer proves the ids — a hook that changed any of it is refused with the observed commit (HEAD moved to it), never accepted. The candidate changes; no record exists yet. |
| `dct release prepare [--version V \| --patch \| --minor \| --major] (--entries … \| --no-entries) --plan-digest D [--json]` | the PREPARE stage (K-76, [below](#prepare-dct-release-prepare-k-76)): the version-sync script runs ONCE, isolated, and its result becomes the project's PENDING version-file proposal; outcome `prepared` with `proposal_sha256`. Nothing is stamped. |
| `dct release apply [--version V \| --patch \| --minor \| --major] (--entries … \| --no-entries) --plan-digest D --proposal-digest P [--push-remote R [--push-branch B] \| --no-push] --confirm` | one release from the APPROVED plan (K-80) and the APPROVED proposal (K-76): `D` = the `snapshot_digest` of `dct release plan`, `P` = the `proposal_sha256` of `dct release prepare`, both with the SAME version and selection — steps 1-7 below. The push target (step 7) is approved with it. A re-run of a recorded / historical release needs no `P` (no-op). |
| `dct release resume` | finishes the unfinished record (table below). No `--confirm` (K-97 F4): it executes the CONSENT stored at apply — version, entries, candidate, proposal, push target — and refuses whatever lies outside it (then: a new apply, or abandon) |
| `dct release abandon --version V --reason "…" --confirm` | core `abandon_release`: the record ends `abandoned`; stamps, `current_version` and every effect stay; V stays taken |
| `dct release reconstruct --version V [--confirm]` | a legacy release WITHOUT a record: evidence is gathered read-only and shown; `--confirm` records it (core `reconstruct_release`, no re-stamp) — [Reconstruct](#reconstruct-dct-release-reconstruct-k-60-k-86-k-96-d8) |
| `dct release status [--version V]` | the record (read-only) |
| `dct clog release-advance V STATUS [--release-commit-sha S] [--json]` | the LEDGER path (K-96 D10, `effects.record_progress`): core `advance_release` as the actor `ledger` under the operation lock, for a release dct does NOT execute (an `own` record, or one stamped outside `apply`); a release dct executes is refused (`resume` records its progress). Outcome `advanced` / `noop`, details `{updated, previous_status}` |
| `dct clog release-abandon V --reason "…" --confirm [--json]` | alias of `dct release abandon` (the same function, the same lock) |
| `dct clog release [V] [--patch …] [--entries … \| --no-entries] [--proposal-digest P] [--push-remote R [--push-branch B] \| --no-push] --confirm [--json]` | alias of `prepare` + `apply` (the same functions, no rules of its own); `--confirm` is REQUIRED as for apply (K-96 D1 — it commits, tags and pushes; running it is not the confirmation). No `--plan-digest`: the plan is derived inside the call for the candidate it pins, with the same stamp binding. Without `--entries` it releases every unreleased entry — all must be proven. Without `--proposal-digest` it prepares inside the call and proceeds only when the proposal's files EQUAL the confirmed contract; a first release (also an empty contract, D4) or a contract change is refused with the proposal shown (it stays pending) — re-run with `--proposal-digest P`. Refused instead of guessed: unproven entries, a proof range that needs confirmation (`range.confirm_required`). |

### Executor (K-85)

Who performs a release's effects is explicit, never guessed from a script
name: the project setting `release_executor` — `dct` (default for every
project; the effects layer) or `own` (the project's own release tool; mark
a project with an existing release process BEFORE it uses this workflow:
`dct project release-executor <slug> own`). The setting is copied onto each
NEW record at its stamp (`releases.executor`, immutable) — the RECORD decides
from then on; a later change of the project setting never takes over an
unfinished release (in either direction). dct never runs a command stored in
the database.

| surface | executor `dct` | executor `own` |
| --- | --- | --- |
| `release plan --json`, `clog candidates` (proof layer) | yes | yes — the own tool consumes the same plan, snapshot and proof map |
| `release attest` (the attestation commit) | yes | yes — a PROOF-layer commit (no ledger write, no release effect); its trailer format is the contract, so an own tool may also write it itself |
| `release prepare` / `apply` / the `clog release` alias | yes | REFUSED before any write (`details.executor: "own"`, pointing here) |
| ledger: MCP `release_changelog`, `get_release`, `advance_release`, `abandon_release` | stamp: use `apply` (an MCP stamp has no consent — below) | yes — the own tool's path |
| `release resume` | only a record with executor `dct` AND a recorded consent | REFUSED (`details.executor`) |
| `release abandon`, `clog release-abandon` | yes | yes (ledger only) |
| `release reconstruct` | yes | yes — it records VERIFIED history and performs no effect; the record gets the project's executor at reconstruction |

**Who records progress** (`core.releases.progress_owner`,
`advance_release(actor=…)`): a record with executor `dct` (null on older
records counts as `dct`) AND a consent was stamped by `dct release apply` —
only the effects layer advances it (`resume`, after verifying each effect);
MCP `advance_release` and `clog release-advance` (actor `ledger`) are
refused. Every other record — executor `own`, or stamped outside the effects
layer (MCP `release_changelog`, older records, a partial reconstruction): no
approved proposal, no approved push target — is advanced ONLY through the
ledger path by whoever performed and verified its effects; `dct release
resume` refuses it at every status (it never finishes such a record without
a push, L9) — finish it yourself or abandon it.

`--confirm-version-files` was removed (K-76): it approved whatever result
happened to be present after the stamp. It survives as a hidden flag of
`apply`, `resume` and `clog release` that REFUSES (exit 1) and names the
prepare procedure.

### Prepare (`dct release prepare`, K-76)

An explicit EFFECTS stage before the stamp — not part of the read-only plan:

1. **The same pre-checks as apply** — strict checkout, no unfinished
   record, clean index and work tree, the plan re-derived for the pinned
   candidate must equal `--plan-digest`, the selection proven. A version
   that already has a record, or is lower than `current_version`, has
   nothing to prepare (refused).
2. **The script of the CANDIDATE** — `scripts/dct-version-sync.sh` (else
   `scripts/version-sync.sh`) under the project root, as the candidate
   commit holds it (an untracked script is refused: the release runs only
   what its candidate holds). No script → an EMPTY proposal (approved the
   same way, D4).
3. **The isolated run** — a temporary linked worktree DETACHED at the
   candidate, under the system temp directory (outside the checkout; the
   repository's hooks are disabled for this internal checkout via
   `core.hooksPath`), `bash <script> <V>` with cwd = the project root
   inside it, its own process group, stdin closed, GIT_* location
   variables dropped. Timeout `effects.SYNC_TIMEOUT_SECONDS` (300 s): the
   whole process group is stopped (SIGTERM, SIGKILL after
   `SYNC_KILL_GRACE_SECONDS`) before the command returns — also anything
   the script left running after a normal exit. Captured stdout + stderr:
   the first `SYNC_OUTPUT_MAX_BYTES` (64 KiB), the rest counted
   (`output_dropped_bytes`). A non-zero exit → outcome `failed` with the
   script's exit code (124 on timeout); nothing stamped, no proposal. The
   worktree is removed (`git worktree remove --force` + `git worktree
   prune`) in every case. A script that needs untracked or ignored files
   of the real checkout (a local venv, …) fails there — reported as such,
   never retried in place.
4. **The checkout is verified unchanged** — HEAD, every ref, the index,
   the status and the bytes of every untracked file; a script that wrote
   outside its worktree is refused with what changed (`checkout_changed`) —
   dct restores nothing.
5. **The proposal** — the changes are staged in the TEMPORARY worktree's
   own index (`git add -A`; ignored files stay out; blobs land in the
   repository's object database) and compared with the candidate:
   exact paths, `base_blobs` (the candidate's blob, null = absent),
   `expected_blobs`, the script's identity + sha256, `candidate_sha`,
   `version`, `plan_digest`, `release_mode`, and the changelog BASELINE
   (`changelog`: path, base blob, base sha256). Refused: a script that
   modified the changelog file (dct writes it — K-77 / D2, checked on the
   real result), deleted / retyped a file or changed only a mode, or a
   version file breaking the **version-file contract** (K-96 D9): a file
   dct has a reader for (`core.version.VERSION_FIELD_READERS`, by name:
   `VERSION`, `pyproject.toml` `[project].version`, `package.json`
   `.version`, `Cargo.toml` `[package].version`, `tauri.conf.json`) must
   have its version FIELD equal to V (`same_version`) — a file holding V
   elsewhere but another version field is refused; any other file may
   change only lines holding V as a whole version token (at least one, no
   line only removed). The proposal is stored as the project's PENDING
   proposal (`projects.release_proposal`, `core.releases.set_pending_proposal`
   — a new prepare replaces it; pending is not approval).
6. **Outside git** (a project CONFIRMED outside git — D5, an explicit
   `without_git` proposal, never a Git proof): the same on a temporary COPY
   of the project directory (bounded: `COPY_MAX_FILES` 20000 files /
   `COPY_MAX_BYTES` 256 MiB, special files refused); every file of the
   directory must be byte-identical afterwards; blob ids are SHA-256 of the
   content, kept in dct's release blob store
   (`<data dir>/release-blobs`, `dct.paths.release_store_dir`) — the
   approved version files AND the changelog baseline (content + sha256, or
   "absent"), so a resume can rebuild and verify F (K-78).

Result `details`: `{proposal, proposal_sha256, plan_digest, entry_ids,
contract: {confirmed, proposed, change}, sync: null | {exit_code,
timed_out, output, output_bytes, output_dropped_bytes, stopped_processes,
duration_seconds}, checks: [{path, check: "version_field", value} | {path,
check: "lines_with_version", changed_lines}]}`. `contract.change` is true
for a first release (`confirmed` null) and for a changed set — approving
`P` approves that contract too; it is recorded with the stamp.

### Order of effects (`apply`)

1. **Pre-checks** — strict checkout (K-52; a project without a registered
   path is refused, K-95 C1); its HEAD is read ONCE — the candidate; no
   unfinished record (else `resume`); index and work tree clean: any
   staged, unstaged or conflicted path, a release artifact that is
   untracked, or an operation in progress refuses — dct never resets,
   stashes or cleans (K-50). **The approved plan** (K-80): the plan is
   re-derived FOR THE PINNED CANDIDATE (never a HEAD read later) with the
   same version and selection; its `snapshot_digest` must equal
   `--plan-digest` — a different HEAD, range base, entry set or entry
   state, selection, version or proof refuses before any database write
   (details: `approved_digest`, `derived_digest`, the derived `snapshot`).
   The requested ids must be proven by the plan (an id the plan reports as
   unproven is never stamped; attest it first). Entries of a project
   confirmed outside git have no commit proof — they are released as the
   owner selects them (K-63). Version-only only with `--no-entries`.
   **The approved proposal** (K-76): `--proposal-digest` must name the
   project's PENDING proposal (a newer prepare voids an older digest),
   prepared for this candidate, version, plan digest and mode (`mismatch`);
   the script must be unchanged (identity + sha256); every proposal path
   must still hold its base content (outside git also F its baseline —
   `changed`); every expected blob must still be available (`git cat-file
   -e`; a blob pruned by `git gc` → prepare again). **The push target**
   (K-97 F2, `effects.push_target`): `--push-remote R [--push-branch B]`,
   else the current branch's UPSTREAM (`branch.<b>.remote` +
   `branch.<b>.merge`); no upstream (also a detached HEAD or a local `.`
   upstream) → refused with the explicit choice (`--push-remote …` or
   `--no-push`), never guessed; `--push-remote` without `--push-branch`
   needs the upstream to be on that remote. `--no-push` = an explicit local
   release (`push_target: null`); a project outside git has none. The
   remote is READ before the stamp (`git ls-remote`, network): a tag of V
   there in any spelling, or a remote branch the candidate does not contain
   (the push could never be a fast-forward), refuses — nothing stamped.
   Right before the stamp HEAD must still be the candidate (else refused,
   nothing stamped). All of this runs under the [operation lock](#the-effects-layer),
   taken before the first pre-check.
2. **Stamp** — `release_entries(V, entry_ids, proof=<snapshot proof map>,
   candidate_sha=<the pinned candidate>, checkout_root=<repo root>,
   stampable_ids=<the selected ids the plan proves>,
   expected_entries=<snapshot.entries>, consent=<the approval>,
   manifest={untracked_baseline}, consume_proposal=True)`. K-81, inside the
   stamp transaction and before any write: only `stampable_ids` get a NEW
   stamp (an id the plan does not list — unknown, added later — is
   refused; the one exception is a live entry of the project already
   carrying the same V, the legacy set, which is never stamped again), and
   the live unreleased entries must be exactly the snapshot's, in the same
   state — an entry added, edited or removed since the plan refuses ("run
   the plan again"). K-76 / D3 / D4, in the SAME transaction: the pending
   proposal must still be the approved one; it becomes the record's
   `proposal`, `consent` = `{version, entry_ids, candidate_sha,
   proposal_sha256, render, push_target: {remote, url (its push URL at
   approval), branch, tag: "v<V>"} | null}`, the project's version-file contract becomes the proposal's
   paths (history kept — a first contract, an empty one or a change), the
   pending proposal is cleared and the untracked files present before the
   release are recorded as `manifest.untracked_baseline` — never consent to
   include them. Any refusal writes NOTHING (entries, `current_version`,
   record, contract and the pending proposal stay). A HEAD move after the
   stamp never re-pins the record: the effects run on the recorded
   candidate only and refuse elsewhere. No SQL transaction spans the git
   reads. From here the record's `version` is the spelling of every
   artifact (K-55); the requested V is only reported. A recorded /
   historical / legacy release returns outcome `noop` with no side effect.
3. **Changelog file F** (`<project root>/<changelog_path>`, only when the
   project manages its changelog — `manages_changelog`, the plan's
   artifact rule, D2). The expected content is deterministic: F's BASELINE
   (the candidate commit's F; outside git the copy persisted at prepare —
   K-78, the current file is never its own proof) + the V section rendered
   from the database export of the record's version and headed `##
   [<record version>] - <record created_at date, UTC>` (also on a resume)
   — the ONE renderer of the export (`export_changelog_md`), so the
   record's persisted `render` decides the PR references (K-90, below); a
   version-only release gets `- Version-only release — no changelog
   entries.`. Existing F: the section is inserted above the first `## [`
   line, every earlier byte stays, one section more, V exactly once; F
   already holding the IDENTICAL V section → unchanged; any other V
   section → refused. Bootstrap (no F in the baseline): the full export
   without Unreleased, with the section of V exactly once.
4. **Version files** — the script is NEVER run after the stamp: the
   APPROVED expected blobs are written (`git cat-file --filters`; outside
   git from the blob store), each with `_write_atomic`. **Content-verified
   retry** (K-75): BEFORE anything is written every artifact (F included)
   is classified — its current content is the baseline (→ written), the
   approved content (→ kept) or anything else (→ refused: nothing written,
   the index untouched, `foreign` names the paths; never restored
   automatically — restore or finish the file yourself, or abandon).
   **One consistent read** (K-77): afterwards the FULL artifact set is
   read ONCE and every hash, every blob id and the expected tree are
   computed from that one read; F must equal its expected content and
   every version file its approved blob — a mismatch refuses and records
   nothing. Then the manifest is recorded with `files_written`:
   `{changelog: {path, sha256, section_date}, version_files: [{path,
   sha256}], tree, untracked_baseline}` (paths repo-root-relative; for a
   project without git relative to the project root and without `tree`).
5. **Release commit** (K-96, K-79 — [Commits and hooks](#commits-and-hooks-k-96-k-79))
   — the EXPECTED tree = the candidate's tree with exactly the manifest
   blobs (`hash-object -w` of the verified work-tree files + a temporary
   index); it must equal `manifest.tree`. The real index is checked
   READ-ONLY (`git diff-index --cached`, `ls-files -s`; never `write-tree`
   or `git add` on it): paths staged against the candidate must be manifest
   paths whose staged blob is already the expected one — a foreign path or
   foreign CONTENT inside a release file (e.g. a staged hunk in
   `pyproject.toml`) refuses and leaves the staging untouched; an existing
   `index.lock` (another git process) refuses before anything is created.
   Then `git commit --allow-empty -m "release: v<V>"` from a TEMPORARY
   index holding exactly the expected tree — the repository's hooks run on
   exactly what is committed. HEAD moves by git commit's own ref
   transaction (compare-and-swap against the candidate it read). Afterwards
   the commit is RE-VALIDATED: single parent = the candidate, tree =
   `manifest.tree`, subject `release: v<V>` (a trailer a hook appended is
   fine), the work-tree artifacts still the manifest's. Then the user's
   REAL index follows the commit under git's index.lock protocol (below),
   and `committed` is recorded with `release_commit_sha`.
6. **Tag** — `v<V>`, lightweight (like the repository's existing tags), at
   the verified release commit (tree and parent re-checked). A tag of the
   same version (any spelling) at another commit refuses; one at the
   release commit is accepted (no second tag for another spelling).
   `tagged`.
7. **Push** (K-97 F2) — to the CONSENTED push target only (never
   re-derived from today's upstream, K-97 F4): its remote must still have
   the approved push URL (else refused: outside the consent). `git
   ls-remote` first: a tag of V on the remote at another commit refuses;
   both already there (the branch at the release commit or a descendant
   this repository knows, the tag exactly at it) → `verified`, not pushed
   again. Else `git push --atomic <remote> <commit>:refs/heads/<branch>
   <commit>:refs/tags/v<V>` (the local `pre-push` and the remote's hooks
   run; `GIT_TERMINAL_PROMPT=0`, the caller's SSH / credential variables
   kept) and `git ls-remote` again must show both; then `pushed`, then
   `completed`. A failed push (rejected, unreachable, a refusing hook)
   leaves the record `tagged` with the git error (`details.git_error`) —
   `resume` retries ONLY the push: never a new bump, commit or tag. Without
   a push target (`--no-push`, outside git): step `push` is `skipped` and
   the record goes from `tagged` to `completed`.

### Commits and hooks (K-96, K-79)

Both commits the effects layer creates — the release commit and the
attestation commit — go through `git commit` (`effects._hook_commit`),
never `commit-tree` and never `--no-verify`: pre-commit,
prepare-commit-msg, commit-msg and post-commit run as the repository
configures them (`core.hooksPath` included), `commit.gpgSign` and the like
apply as git applies them. The commit is built from a TEMPORARY index
(`GIT_INDEX_FILE`, which git exports to the hooks) holding exactly the
approved tree, so a hook checks what is committed and the user's index is
not touched while it runs. Timeout `effects.HOOK_GIT_TIMEOUT_SECONDS`
(900 s), stdin closed.

- A hook that REFUSES (or a lost HEAD race, a signing failure): `git
  commit` fails, no commit is created by dct, HEAD is reported (unchanged,
  or where another process moved it); the record is unchanged
  (`files_written`), nothing tagged or pushed. Fix the cause and `resume`.
- A hook that CHANGES the content (a formatter rewriting a file, restaged
  files, an edited subject): the commit exists and HEAD moved to it, but
  it is not the approved commit — refused with the observed effects
  (`details.commit`, `head`, `tree`, `parents`, `subject`,
  `expected_tree`), NEVER adopted; dct restores nothing. Reset HEAD to the
  candidate and the artifacts yourself (or abandon), then `resume`. A hook
  that only rewrote the WORK TREE after an approved commit: refused until
  the files hold the commit's content again (then `resume` adopts it).

**The real index after the commit** (`effects._sync_real_index`, K-79):
git's own lock protocol — `<git dir>/index.lock` created with `O_CREAT |
O_EXCL` (an existing lock is another git process: refused, never deleted,
never written); UNDER the lock the whole relevant state is re-checked —
HEAD must still be the release commit, and every artifact's index entry
must be the candidate's (→ updated to the commit's) or already the
commit's; anything else is the user's own staging → refused and PRESERVED,
nothing written. Every other entry (foreign staging elsewhere) is copied
unchanged. The new index is prepared on a COPY (a temporary
`GIT_INDEX_FILE`), written into the lock and renamed into place — no `git
add` / `git commit` ever runs on the real index while dct holds its lock.
A refusal here comes AFTER the commit: the release commit exists (HEAD),
`committed` is not recorded; resume adopts the commit once the cause is
gone (an unstaged / committed foreign blob, a released `index.lock`).

The concurrency windows are covered by tests on real repositories
(`tests/test_release_git_effects.py`): a stage of a DIFFERENT blob for a
release path after the index check (`before_commit` barrier) and during
the commit (a hook — another process — staging into the real index): the
user's blob survives, the commit is still exactly the approved tree; HEAD
moved during the commit: git's compare-and-swap fails, nothing committed;
a foreign `index.lock` before the commit and before the index update:
refused, the lock untouched.

### Resume and interruptions

`resume` (under the operation lock) first checks the RECORD's executor
([Executor](#executor-k-85): `dct` with a recorded consent, else refused
before anything), then verifies the record's FULL entry
set (K-94 B3, `core.releases.verify_release_entry_set`: exactly the live
entries carrying its version — a mismatch refuses before any effect and is
never repaired by widening the record), that its consent names its
proposal, and that the record's version, entries and candidate are the
consented ones (K-97 F4: resume executes the STORED consent and asks for no
new approval; anything outside it refuses — a new apply, or abandon); then the
record against the checkout (K-51): the record's `checkout_root` must be
THIS checkout (another worktree → refused, the record is never re-pinned), a
git record needs its candidate and checkout, no operation may be in
progress. Then, by status:

| status | HEAD must be | what resume does |
| --- | --- | --- |
| `stamped` | the candidate | index: nothing staged or conflicted; unstaged only F and the proposal's files. A record without an approved proposal (stamped outside the effects layer, or before K-76) is refused — the sync is never run after the stamp; abandon it. Otherwise steps 3-4: each artifact classified (baseline → written, approved → kept, else refused with nothing written), the approved blobs written, the full set verified from one read, the manifest recorded |
| `files_written` | the candidate — or the release commit itself (its only parent is the candidate, its tree is `manifest.tree`, its subject `release: v<V>`), which is adopted (then only the real-index update and `committed`) | every manifest file must still hold its recorded hash (a reverted version file → refused, not overwritten); then step 5 (the commit through the hooks) |
| `committed` | the release commit or a descendant | the release commit is verified (tree = `manifest.tree`, parent = candidate); step 6 |
| `tagged` | the release commit or a descendant | the tag must point at the release commit; step 7 — ONLY the push, to the consented target (already on the remote → verified); without a push target `completed` |
| `pushed` | the release commit or a descendant | the tag as for `tagged`; the remote must still show the branch and the tag (`git ls-remote`, nothing pushed again); `completed` |
| `completed` / `abandoned` | — | no unfinished record: outcome `noop`, no side effect |

Interruption points (`effects.INTERRUPTION_POINTS`, the `on_point` hook of
`apply_release` / `resume_release`) — a crash at each one is resumed by
`resume` exactly once (one section, one release commit, one tag; the sync
never re-run):

| point | state left behind |
| --- | --- |
| `before_stamp` | nothing (no record; the pending proposal stays — run `apply` again) |
| `after_stamp` | record `stamped` (+ proposal, consent, `untracked_baseline`, the learned contract — one transaction) |
| `before_changelog` | every artifact classified; nothing written; `stamped` |
| `after_changelog` | F written; no version file written; `stamped` |
| `before_version_files` | as `after_changelog` |
| `after_version_file` | ONE more version file written (fires after each write); `stamped` |
| `after_version_files` | every artifact written; not verified, not `files_written` |
| `after_files_written` | manifest recorded; no commit |
| `before_commit` | the real index checked (read-only, untouched), the temporary index built; `git commit` not run |
| `after_commit` | release commit created through the hooks, HEAD moved, verified; the real index not updated; not `committed` (adopted on resume) |
| `after_index` | the real index follows the release commit; not `committed` (adopted on resume) |
| `after_committed` | no tag |
| `after_tag` | tag created; not `tagged` |
| `after_tagged` | not pushed |
| `before_push` | the remote read (no tag of V elsewhere); `git push` not run; `tagged` |
| `after_push` | pushed and verified on the remote; not `pushed` (verified, not pushed again, on resume) |
| `after_pushed` | not `completed` |

A user edit between runs inside an artifact (a version file or F) is
neither the baseline nor the approved content: the resume refuses and
leaves the files and the index exactly as they are.

### Reconstruct (`dct release reconstruct`, K-60, K-86, K-96 D8)

Evidence, read-only (`effects.gather_evidence`), in this order:

1. **The version spelling first** (K-86): the AUTHORITATIVE spelling comes
   from the SAME shared legacy resolver core uses
   (`changelog.version_spelling` → `resolve_version_spelling`: the live
   record's spelling, else the first-stamped entry's — earliest
   `released_at`, null last, ties → the lowest id — else `current_version`).
   The requested V is only reported (`requested_version`); the equivalence
   map is shown (`spellings`); every comparison below is `same_version`, and
   the files, the section, the tag and the record all refer to that ONE
   resolved version (a request for 1.1.0 of a release stored as 1.1 records
   1.1 — nothing re-stamped, nothing rewritten). The final spelling must fit
   the schema (`ensure_version_fits`, K-87) — also in the preview. No entry
   carries V and it is not `current_version` → refused.
2. **Candidates** (D8): every commit titled `release: v<V>` (any equivalent
   spelling, any local ref) and the commit the tags of V point at is a
   CANDIDATE, verified on its own evidence: exactly ONE parent (the
   candidate), the paths it changed besides F equal to the version-file
   contract (when one exists), F at that commit holding the V section
   EXACTLY once (any equivalent label; two sections such as `[1.1]` and
   `[1.1.0]` are a contradiction) with the database's entries (its date
   kept as `section_date`), and each changed version file passing the D9
   contract against the parent (J8 — never a substring search): a file dct
   has a reader for must have its version FIELD equal to V (`same_version`),
   any other file may change only lines holding V as a whole version token
   (any equivalent spelling). `candidates`: `[{commit, verified, reason}]`.
3. **The stage the evidence reaches**: the tags of V must all point at ONE
   commit (else refused — a contradiction). With a tag: that commit must be
   a verified candidate → status `completed` (other candidates, e.g. a
   backport on another branch, are listed, not used). Without a tag:
   exactly one verified candidate → status `committed` — the record ends
   where the evidence ends; dct's `resume` does not continue it (no
   consent: nobody approved a push target) — tag / push it yourself and
   record that with `dct clog release-advance`, or abandon it. No verified
   candidate, or several without a tag → refused. Without a release commit
   nothing is reconstructed (a stamp without its candidate cannot be
   verified).
4. **Without git**: F (the section, as above) and the contract's files as
   found (version field, or a line holding V) → `completed`.

The record: mode `reconstructed`, the resolved version, the status of step
3, the manifest `{tree, changelog, version_files}`, no consent, proposal or
render, the project's executor at reconstruction. Any refusal writes nothing.

### JSON (`--json`, every subcommand)

One shape, `effects.RESULT_KEYS` (pinned by
`tests/test_release_effects.py::test_result_keys_are_the_documented_schema`):

| key | content |
| --- | --- |
| `schema_version` | `1` |
| `action` | `prepare`, `apply`, `resume`, `attest`, `abandon`, `reconstruct`, `status`, `advance` (`clog release-advance`) |
| `project` | slug |
| `outcome` | `completed`, `noop`, `attested`, `abandoned`, `preview`, `reconstructed`, `prepared`, `advanced`, `found` / `none` (status), `refused`, `failed` (the version-sync script in prepare: non-zero exit or timeout), `conflict` (a lost serialization race — [below](#conflicts-k-88)) |
| `version` | the record's version (authoritative spelling) or null |
| `release` | the record (`core.releases.RECORD_FIELDS`) or null |
| `steps` | `[{step, result, …}]` — step ∈ `sync`, `proposal` (prepare, and the `clog release` alias), `stamp`, `changelog`, `version_files`, `files`, `commit`, `index` (the real index after the commit), `tag`, `push` (`remote`, `branch`, `tag`; `skipped` with `reason` without a push target), `complete`, `attest`; result ∈ `done`, `verified`, `skipped`, `adopted`, `approved`, `conflict` (the last step of a conflict response) |
| `warnings` | human-readable strings |
| `details` | prepare: see [Prepare](#prepare-dct-release-prepare-k-76), plus `push_target` (what apply would push to by default — the upstream — or null, read-only); apply: `{requested_version, previous_version, bump, entry_ids, mode, released, current_version, push_target}`; attest: `{previous_head, head, entry_ids}`; abandon: `{updated}`; reconstruct: `{evidence: {version (authoritative), requested_version, spellings, entry_ids, candidate_sha, release_commit_sha, tag, checkout_root, manifest, status, without_git, candidates}}` (`status` = `completed` \| `committed`; `candidates` (git) = `[{commit, verified, reason}]`); advance: `{updated, previous_status}`; conflict: [Conflicts](#conflicts-k-88); refused / failed: the reason (`error`) plus the refusal's data (e.g. `staged`, `unproven`, `proposal` / `proposal_sha256` / `contract` (the alias), `mismatch`, `checkout_changed`, `foreign`, `foreign_paths`, `foreign_content`, `foreign_staged`, `changed`, `path`, `sync`, `exit_code`; the git effects: `git_error`, `commit`, `head`, `tree`, `parents`, `subject`, `expected_tree`, `index_lock`, `push_target`, `remote_tags`, `remote_branch`, `outside_consent`; the operation lock: `lock`, `holder`; the executor: `executor`, `consent`) |

Exit codes: 0 success (also `noop`), 1 refused, the script's own code when
the version sync fails in prepare (124 on timeout), 75 for a conflict
(`effects.CONFLICT_EXIT_CODE`, sysexits EX_TEMPFAIL), 2 for a usage error.

### Conflicts (K-88)

A database write of a release command can lose a serialization race on
PostgreSQL (SQLSTATE 40001: another transaction changed the data after this
one's snapshot). It is recognised ONLY by the shared detector
`db.is_serialization_failure` (the domain `SerializationConflict` and a
driver error carrying SQLSTATE 40001 — type and SQLSTATE, never message
text; the same one pickup uses, K-74). The effects layer — where the step is
known — turns it into `effects.ReleaseConflict`; the CLI prints the stable
result with outcome `conflict` and exits 75. Never a traceback, and never
retried automatically: not the write, not the sync, the commit, the tag or
the push.

The rollback covers only the FAILED transaction: files written, a commit, a
tag or a push performed before it STAY and are reported — never "nothing
written".

| key | content |
| --- | --- |
| `steps` | the run's own step log up to the conflict, then `{step, result: "conflict"}` |
| `details.conflict` | `{step, status}` — the write that was rolled back: `stamp`, `files` / `commit` / `tag` / `push` / `complete` (the progress write of `status` `files_written` … `completed`), `proposal` (prepare's pending proposal), `abandon`, `reconstruct`, `advance`; `status` null when the write is not a status move. A race that escaped every step-aware write (a read) names the command (`apply`, `resume`, …) |
| `details.observed_effects` | the effect steps (`stamp`, `changelog`, `version_files`, `commit`, `index`, `tag`, `push`, `attest`) of the step log that are not `skipped`, each with its result (`done` = performed by this run, `verified` / `adopted` = found in place); `[]` = none before the conflict; `null` = UNKNOWN (the command-level boundary does not know the run's steps) |
| `details.record_state` | `read` (`release` = the record as RE-READ after the rollback), `absent` (no record — e.g. a conflict at the stamp of a new version), `unknown` (the re-read failed too — `details.record_error`; the state is never guessed) |
| `details.error` | the explanation: what was rolled back, what stays, what to do |

What next: a conflict at the stamp left nothing of that stamp (the
approved proposal is still pending) — run the same `apply` again; any
later conflict left the record at the status it shows — `dct release
resume` continues from there (an effect already in place is verified, not
repeated).

## The release ledger (record fields)

`dct/core/releases.py` — one record per released version
(`core.releases.RECORD_FIELDS`; MCP `get_release`, `dct release status
--json`). The ledger stores and cross-checks what the workflow verified; it
never reads files or git. Every field is validated before any read or write
and a refusal writes nothing.

| field | content | mutability |
| --- | --- | --- |
| `id`, `created_at`, `updated_at` | row identity and times | — |
| `version` | the AUTHORITATIVE spelling (normalized) — every artifact uses it | fixed |
| `mode` | stamp mode: `all`, `selected`, `version_only`, `reconstructed` | fixed |
| `entry_ids` | the exact entry set (`[]` = version-only); must always equal EXACTLY the live entries carrying the version (K-94 B3 — `verify_release_entry_set` is the pre-effect gate of a resume / retry; a mismatch refuses, the record is never extended or shrunk) | fixed |
| `candidate_sha` | the commit the release was cut from, or null | immutable (K-61) |
| `checkout_root` | absolute path of the release checkout, or null | fixed |
| `release_mode` | the VERIFIED release mode (K-94 B1): `git` or `without_git` (a project CONFIRMED outside git); null = recorded before the field existed (unverified) | write-once |
| `executor` | who performs the effects (K-85): `dct` (the effects layer) or `own` (the project's own tool) — copied from the project's `release_executor` at stamp time (and at reconstruction); null on records older than the field (counts as `dct`). With `consent` it decides who records progress ([Executor](#executor-k-85)) | immutable — a later project-setting change never takes over an unfinished release |
| `status` | the state machine below | forward only |
| `release_commit_sha` | the verified release commit | set once, from `committed` on |
| `manifest` | JSON object of the verified artifacts (e.g. `changelog`, `version_files`, `tree`, `untracked_baseline`) | write-once per key; the MERGED manifest ≤ 64 KiB |
| `proof` | `{"<entry id>": {commits, merge_commit, pr}}` (K-70) | written with the stamp; a re-run passing a different map is refused (K-94 B2) |
| `consent` | what the owner approved (K-97 F4): `{version, entry_ids, candidate_sha, proposal_sha256, render, push_target: {remote, url, branch, tag} \| null}` (`null` = no push: `--no-push`, outside git, or a record from before the push) — what a resume EXECUTES: its version, entries and candidate must be the record's, its push target is the only one pushed to (its remote must still have that push URL) | write-once |
| `proposal` | the approved version-file proposal (K-76): `{paths, base_blobs: {path: blob \| null}, expected_blobs: {path: blob}, script: {identity, sha256} \| null, candidate_sha, version, plan_digest, release_mode, changelog: {path, base_blob, base_sha256} \| null}` (the last four: the release context it is bound to; blob ids are git blob ids, or SHA-256 of the content in dct's release blob store for `without_git`); `consent.proposal_sha256` = `proposal_digest(proposal)` (SHA-256 of its canonical JSON). Written by the stamp that consumed the project's PENDING proposal | write-once |
| `render` | the changelog render settings of THIS release (K-90): `{"pr_refs": bool}` — defaults to the project's `changelog_pr_refs` at stamp time, so a later preference change cannot alter the expected hashes | write-once |
| `completed_at`, `abandoned_at`, `abandon_reason` | terminal stamps; the FIRST abandon reason stays — the same reason again is a no-op, a different one is refused (K-94 B5) | set once |

**State machine** (`core.releases.TRANSITIONS`): `stamped → files_written →
committed → tagged → pushed → completed`, forward jumps allowed (the
workflow verified the intermediate effects), `abandoned` (terminal, only via
`abandon_release`) from every unfinished status. `pushed` (K-97) = the
release commit and its tag are on the push target; a failed push leaves the
record `tagged` and is resumed from there — never a new bump, commit or
tag. K-94 B1: a NULL `candidate_sha` never implies "no git". Only a
`without_git` record completes without a release commit — and only with the
manifest of its verified artifacts; it never reaches `committed`, `tagged`
or `pushed` and carries no candidate or release commit. Every other record
(also `release_mode` null) needs `release_commit_sha` from `committed` on.

**Write-once rule** (`check_unchanged`): a field not yet recorded may be
written once (`advance_release`); the stored value again is a confirmation;
a DIFFERENT value is refused ("immutable"). A re-run of `release_changelog`
that returns an existing record never fills a field either: a given
`proof` must equal the stored map (also refused when the record has none);
a given `release_mode` / `consent` / `proposal` / `render` must equal the
stored value — a record that predates the field (null) has nothing to
contradict and stays as it is.

**Size caps** (refused, never truncated): `manifest` 64 KiB, `proof` 256
KiB, `consent` and `proposal` 64 KiB — BYTES of the canonical JSON (sorted
keys, compact separators, UTF-8; `core.releases.json_size`), the manifest
measured after the merge; `abandon_reason` 2000 characters.

**Legacy spellings** (K-86 / K-93 A3): `changelog.resolve_version_spelling`
is the ONE resolver of a version stored under several equivalent spellings
(1.1 / 1.1.0 / v1.1). Rule (`changelog.spelling_rule`, shared by the export
and the reconstruction): the live record's spelling; else the stored
spelling of the FIRST-STAMPED entry — earliest `released_at` (null = unknown
time, after every known time), ties → the lowest entry id; else
`current_version`. It also returns the equivalence map (`spellings`: stored
spelling → entry ids, `equivalent`, `several`) — nothing is rewritten, and a
caller comparing artifacts (file, section, tag) against it refuses a real
contradiction instead of picking one. The reconstruction stores the
resolved spelling and checks that FINAL value against the schema's width
(K-87, `ensure_version_fits`).

**Project settings**: `release_executor` (`dct` default \| `own`; `dct
project release-executor <slug> own|dct`) and `changelog_pr_refs` (default
off; `dct project changelog-pr-refs <slug> on|off`). **Pending proposal**:
`projects.release_proposal` — the canonical JSON of the latest `dct release
prepare` (`core.releases.set_pending_proposal` / `get_pending_proposal`),
replaced by a new prepare, consumed and cleared by the stamp that names its
digest (`release_entries(consume_proposal=True)`).

**PR references in the changelog** (K-90, opt-in): with `changelog_pr_refs`
on at stamp time the record's `render` is `{"pr_refs": true}` and every
entry of THAT release whose stored proof has an unambiguous `pr`
(`core.releases.proof_pr`: set and not `ambiguous`) renders as `- <entry>
[(<component>)] (#N)` — N is the pull request linked to the entry in this
repository, never a dct item or changelog id, never a heuristic subject
number (the plan never stores one as `pr`). ONE renderer
(`dct.export.export_changelog_md`) serves the full export and the single
section the effects layer inserts. The setting is persisted per release:
changing it later (between apply and resume, or after) changes neither the
section nor the expected hashes, and releases without a record or stamped
with it off are never rewritten. Branch names never appear in the public
changelog.

**Read-only commands** (K-84): `release plan`, `release status` / `clog
release-status`, `clog candidates` and the `reconstruct` preview open the
database read-only (`core.engine.readonly_engine`: SQLite `mode=ro`, no
journal-mode change, no file or directory created; PostgreSQL READ ONLY
transactions) and refuse a missing database or an older schema with a
message naming `dct db upgrade` — the explicit bootstrap every write command
otherwise runs implicitly.

## Layers and a project's own release tool

The three layers (K-72) and who may use them:

| layer | surface | who uses it |
| --- | --- | --- |
| 1 proof | `dct release plan --json`, `dct clog candidates --json` | every project — read-only, no side effects |
| 2 ledger | MCP / core `release_changelog`, `get_release`, `advance_release`, `abandon_release` | the effects layer, or a project's own release tool |
| 3 effects | `dct release attest / prepare / apply / resume / abandon / reconstruct` | the default behind `/release` for projects without their own tool |

The skills of both clients only run the dialog over layers 1 and 3; the MCP
process never verifies files (it runs in its own directory, not in the
release checkout).

### Integrating a project's own release tool

A project whose own tool writes the changelog, bumps the version, commits,
tags or publishes REPLACES ONLY layer 3. It keeps the proof and the ledger:

0. **Mark it** — `dct project release-executor <slug> own` before the first
   release through this workflow ([Executor](#executor-k-85)): dct's
   `prepare` / `apply` / `resume` then refuse to perform effects for it,
   and its records carry executor `own`.
1. **Proof** — run `dct release plan --version V --entries <ids> --json`
   in the release checkout. Release only ids from `entries.proven` (or ids
   attested first with `dct release attest`); take `candidate_sha`,
   `repo_root` and the proof map from the `snapshot` (the approved context,
   K-80) — and re-run the plan right before the stamp: a different
   `snapshot_digest` means the context changed (new HEAD, new or edited
   entries), so plan and approve again.
2. **Exact stamp** — `release_changelog(project, version=V, entry_ids=[…],
   candidate_sha=<checkout.candidate_sha>, checkout_root=<checkout.repo_root>,
   proof={<id>: proof[<id>], …})` with an explicit version and an explicit
   `entry_ids` list (`[]` = version-only). It writes the release record
   (`stamped`); a repeat with the same set is a no-op, a different set for
   a recorded V is refused.
3. **Own effects, verified by the tool** — the tool performs its effects and
   checks each against reality itself (file content, the commit's tree and
   parent, the tag's target). A status is never proof: report only what was
   verified.
4. **Progress** — `advance_release(version=V, status=…)` after each verified
   effect, in order `files_written` (optionally with `manifest`) →
   `committed` (with `release_commit_sha`; the candidate stays immutable) →
   `tagged` → `pushed` (after the verified push) → `completed`. An interrupted release is resumed from
   `get_release(project)`, never re-stamped under a new version;
   `abandon_release` ends it explicitly with a reason.

A record the project's own tool stamped is finished by that tool: `dct
release resume` refuses it (its executor is `own`, or — stamped through MCP
without the setting — it carries no consent) and never adopts a foreign
tool's effects. Its tests: `tests/test_release_executor.py` (a fake own tool
run as a separate process: shared plan → ledger stamp → its own effects →
`advance_release`; dct's `apply` / `resume` refuse and its effects survive).

