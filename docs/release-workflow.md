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

Both documents carry `schema_version` (currently `3`; ONE constant,
`plan.SCHEMA_VERSION`, for the plan and the candidates) — it moves when the
meaning of a key changes:

- version 2 added `resume`, `snapshot`, `snapshot_digest`, the graph-nearest
  range base, the merge-revert mainline and the per-recognition provenance;
  a `(#N)` subject suffix is no longer a `pr`;
- version 3 added `executor` (the project's executor and, for an unfinished
  record, the record's stored executor, progress owner and consent — K-119),
  `pr_source` on every proof object (K-116 H7), the revert comparison that
  keeps blob identity (K-105) and the ancestry cap that refuses (K-116 H8).

A client that needs a key checks `schema_version` first (a plan with
`schema_version` lower than 3 has no `executor`). The effects layer's result
document has its OWN `effects.SCHEMA_VERSION` ([JSON](#json---json-every-subcommand)).
The top-level keys and the `schema_version` cell of each table below are
pinned against the module that emits the document by
`tests/test_release_plan.py::test_json_documents_keep_their_top_level_keys`
and `tests/test_release_round4_proof.py::test_documented_schema_table_matches_its_module`.
Datetimes are ISO 8601 strings (UTC when stored naive). Paths in `index`
are relative to the repository root.

## `dct release plan [-P slug] [--version V | --patch | --minor | --major] [--entries … | --no-entries] [--json]`

The checkout is verified STRICTLY (K-52): the current directory must be a
checkout (main or linked worktree) of the project's repository with a first
commit; anything not confirmed exits 1 — never a fallback to the main
checkout. A project whose registered path is CONFIRMED outside git gets a
plan without a range (its entries: reason `no_git`); a project without a
registered path gets reason `no_path` — the absence of a repository is not
confirmed (K-95 C1, K-116 H1: a missing path is never a confirmed `no_git`),
its plan has no snapshot, and `prepare`, `apply` and the `clog release`
alias refuse it.

`--entries` / `--no-entries` choose the selection the **snapshot** binds
(default: every proven id — plus, outside git, every entry). The snapshot is
what the owner approves: `dct release prepare` and `apply` require its `snapshot_digest`
(below).

| key | content |
| --- | --- |
| `schema_version` | `3` |
| `project` | slug |
| `kind` | `"release"`, or `"resume"` when an unfinished release record exists (then `resume` is set and nothing is computed on today's HEAD) |
| `version` | `{current, target, bump}` — `target` from `--version` (normalized) or the bump; for a resume without a version, the record's version |
| `checkout` | `{git: true, repo_root, project_root, candidate_sha, head (full HEAD), branch (null on a detached HEAD), detached, matches_record?}` or `{git: false, reason: "no_path" \| "not_a_repository", project_root}`; `candidate_sha` = HEAD for a release, the RECORDED candidate for a resume (K-95 C5); `matches_record` (resume only) = `{candidate_sha, checkout_root}` booleans against the record |
| `executor` | `{project, record}` (K-119, `core.releases.release_routing` — read together with `unfinished_release`, so a dialog routes from this ONE result): `project` = the project's CURRENT `release_executor` (`"dct"` \| `"own"`) — it decides only a NEW release; `record` = `null`, or for the unfinished record `{version, status, executor (the value STORED on the record, immutable; null on a record older than the field — counts as `dct`), progress_owner (`"dct"` = stamped by `dct release apply`: only `dct release resume` / `abandon` continue it \| `"ledger"` = its effects are performed and reported by whoever stamped it — an `own` record, or one stamped outside `apply`), consent (bool — the record carries the owner's consent)}`. A resume is routed by `record`, never by `project` ([Executor](#executor-k-85)) |
| `unfinished_release` | the unfinished release record (`core.releases.RECORD_FIELDS`) or `null` |
| `range` | see below; `null` without git |
| `entries` | `{proven: [...], unproven: [...]}` — every unreleased entry of the project, by id |
| `foreign_citations` | `[{id, reason: "unknown" \| "other_project" \| "released" \| "deleted", version, commits}]` — cited in the range, never stamped |
| `reverts` | `[{sha, target, target_sha, status: "in_range" \| "outside_range" \| "ambiguous" \| "unconfirmed", mainline, reason, effective, affected}]` — see [Reverts](#reverts-k-68-k-82) |
| `squash_commits` | `[{sha, subject, signals, pr, lost_ids, proves}]` — `signals` ⊆ `pr_suffix`, `local_squash`, `indented_trailers` |
| `attestation_groups` | `[{branch, entry_ids, squash_commits, signals, proposal}]` — ONE proposed collective attestation per source branch; `signals` ⊆ `indented_trailer`, `branch_named`, `branch_commits_absent` |
| `proof` | the proof map the release record stores: `{"<id>": {commits, merge_commit, pr, pr_source, source, ambiguous, associations}}` (`core.releases.validate_proof`; provenance below). More than `core.releases.ASSOCIATIONS_MAX` (50) associations of one entry REFUSE the plan (exit 1) — the list is never cut (K-116 H8) |
| `index` | `{clean, staged, unstaged, untracked, conflicted, release_artifacts: [{path, role: "changelog" \| "version_file", staged, unstaged, untracked, conflicted}]}` — `clean` ignores untracked files; `staged` = the index differs from HEAD; `null` without git |
| `version_files` | the project's version-file contract `{version_files (null = to be learned at the first release), confirmed_at, history}` |
| `warnings` | human-readable strings (range to confirm, approximate range, revert outside the range or unconfirmed, unfinished release, HEAD not the recorded candidate, selection holding unproven ids, no git) |
| `resume` | `null`, or for a resume plan (K-95 C5) the RECORDED release: `{version, status, candidate_sha, checkout_root, release_mode, entry_ids, proof, release_commit_sha, manifest, effects: {stamped, files_written, committed, tagged, pushed, completed} (booleans by status), head (today's HEAD, separately), head_is_candidate, new_unreleased (entries added since — NOT part of the record)}`. A resume plan has `range: null`, empty `entries` and `proof`, `snapshot: null` — an approved release is never recomputed on today's HEAD |
| `snapshot` | the IMMUTABLE approved context (K-80): `{project, version, current_version, git, repo_root, checkout_root, project_root, candidate_sha (full), range: {base_tag, base_sha, approximate, since, revs}, selected_ids, proof (the map of the selected ids), entries: {"<id>": state digest}}` — `entries` covers EVERY unreleased entry the plan saw (`core.changelog.entry_state_digest` over id, category, entry, component, version), so ANY change of the unreleased set after the plan — also a new entry outside the selection — is another snapshot and needs a new plan (K-116 H3); `null` for a resume plan and for a project without a path |
| `snapshot_digest` | SHA-256 (hex) of the snapshot's canonical JSON (sorted keys, compact separators, UTF-8) — what `dct release apply --plan-digest` takes |

**`range`**: `{candidate_sha, base_tag, base_sha, base_version,
base_distance, alternatives, expected_tag, confirm_required, approximate,
since, revs, display, below_scan, commit_count}`.

- `base_tag` (K-83): the NEAREST release tag that is an ancestor of the
  candidate — the minimum number of PARENT EDGES from the candidate to the
  tagged commit (`base_distance`; a breadth-first search over `git rev-list
  --parents`, never `rev-list --count TAG..HEAD`, which counts commits, and
  never the highest version). Annotated tags count at their commit. Ties at
  that distance (K-116 H5) — one deterministic choice: the exact
  `v<current_version>`, then a name of the current version in another
  spelling, then a `v`-prefixed name, then the lowest name; the choice never
  hides the tie: every other tag at the same distance is listed in
  `alternatives` (`[{name, sha, version, distance}]`), named in a warning
  and printed by the text plan (`equally near: …`). Versions are only
  compared (the never-rewind order), never used to rank the base. Tags
  whose name is not a version are ignored.
- **The ancestry read is bounded, never cut** (K-116 H8,
  `plan.read_ancestry`): the graph is STREAMED from git (one line per
  commit, `gitinfo.git_read_lines` — the process layer; its timeout is
  `gitinfo.GIT_READ_TIMEOUT_SECONDS`, 60 s) and read in full or not at all.
  A history of more than `plan.ANCESTRY_MAX_COMMITS` (1 000 000) commits, a
  failed read or a timeout REFUSES the plan (exit 1, `GitReadError`) —
  distances computed on a prefix of the graph could name the wrong base. A
  repository without an ancestor release tag is not read at all.
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
  commit). `pr_source` (K-116 H7) is the ORIGIN of `pr`: the plan emits
  `"git"` (recognised in the repository) with a `pr`, else null — never
  `"client"`, which only a caller's declaration gets
  ([origin of a PR number](#origin-of-a-pr-number-k-116-h7)).
  `associations` lists every recognition `{merge_commit, pr,
  source, certain}`, including a `… (#N)` subject suffix as `source:
  "subject_suffix", certain: false` (it may be an issue number — never the
  entry's `pr`, never certain, K-95 C4). Several DIFFERENT merge
  associations → `ambiguous: true`, `merge_commit` / `pr` / `pr_source` /
  `source` null — never one picked;
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
of T → its parent) — for EVERY revert, an ordinary one included (K-116 H6).
Otherwise the report
is `status: "unconfirmed"` with `reason` ∈ `no_mainline` (missing or edited
message), `mainline_not_a_parent`, `mainline_ambiguous`,
`mainline_on_non_merge`, `partial` (a conflict resolved differently, an
edited revert, or a revert of a ROOT commit — nothing to compare with):
nothing is neutralized automatically and every entry the
revert may touch (without a usable mainline: what M brought in relative to
ANY parent) gets `revert_unconfirmed` — no automatic proof, never a guess;
the owner confirms it (attest) or corrects the history. A plain first-parent
merge without a revert is unchanged.

**What "the same change" means** (K-105, `plan.change_between` — the ONE
representation, used for every revert by the plan and by `clog
candidates`): per changed path (`git diff-tree -p -U0 --no-renames
--full-index`; a rename is a deletion plus an addition) git's header lines —
the path, `new file mode` / `deleted file mode` (the file TYPE is in the
mode: regular, executable, symlink, gitlink) and `old mode` / `new mode` —
plus the identity of the content:

- a path WITH a textual patch: its removed and added lines. Positions (`@@`)
  and the `index` line are dropped, so a clean revert still equals the
  inverse of what it reverts when later commits moved the lines or touched
  the file elsewhere (the blob ids legitimately differ then);
- a path WITHOUT a textual patch (binary content, an empty file — whatever
  git prints no lines for): the `index <old>..<new>` line with the FULL
  input and output blob ids. Two `Binary files … differ` lines are never
  the same change by themselves.

So B0 → B1, its full revert, then a revert of that revert resolved to B2
leaves the entry WITHOUT automatic proof (the last revert is `partial`: the
entry's change did not come back) until a commit brings B1 back; a correct
full binary revert — also of an added, deleted or renamed binary file, or of
a mode-only change — is still confirmed.

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
| `schema_version` | `3` |
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
(`plan`, `status`, the `reconstruct` preview) take no lock. The lock is
also the scope of the supervised operation — next section.

### Process supervision and the lock's ownership (K-108, K-113, K-118 L10)

ONE runner (`dct/core/procs.py`: `run`, `stream_lines`) starts every
external process of the release workflow: the version-sync script of
`prepare` and every git command of the proof and the effects layer
(`dct.core.gitinfo`: `git_read`, `git_read_lines`, `git_read_blob`,
`git_cat_blob`, `git_write`, the strict checkout verification) — so also
the repository's hooks, filters and the push transport, which git starts.

**The process group.** Each process runs in its own session, as the leader
of its own process group, with stdin closed and no controlling terminal:
its children and grandchildren are in that group, the terminal's Ctrl-C
reaches only dct, and nothing can prompt (a hook, a credential helper or an
ssh key that needs a passphrase FAILS instead of waiting — use an agent, a
credential helper or askpass). The runner owns the end of the group:

- after a normal exit, whatever the command left running in its group is
  stopped before the call returns;
- on a timeout, an interruption of dct or any error in dct, the whole group
  is stopped FIRST — SIGTERM, `procs.TERM_GRACE_SECONDS` (3 s), then
  SIGKILL — and the runner waits until the group is gone (the leader
  reaped, no member left). Only then is the temporary worktree removed and
  the operation lock released: no process of a release writes into a
  worktree that is being removed, or after the lock is free;
- a group that cannot be confirmed gone (`procs.KILL_CONFIRM_SECONDS`, 5 s
  after SIGKILL — a member no signal of this user reaches) is REPORTED
  (`details.process_group` for the sync script, the git error otherwise):
  the isolated worktree / copy is then left in place, and the group stays
  in the lock's record (below).

**Signals.** While a mutating command holds the operation lock, SIGINT
(Ctrl-C), SIGTERM and SIGHUP on the main thread are turned into exceptions
(KeyboardInterrupt / `procs.Interrupted`), so the command unwinds: the
group is reaped, temporary resources removed, the lock released. The
handlers are temporary — restored when the command ends, where a received
SIGTERM / SIGHUP is delivered again to the original disposition (dct still
dies from the signal, after the cleanup). A signal the caller ignores
(`nohup`) stays ignored. While the runner starts or stops a process a
signal is deferred until the group is reaped. Off the main thread Python
delivers no signals, so nothing is installed there (an embedding
application keeps its own handlers); timeouts and errors are supervised on
every thread. SIGKILL of dct cannot be handled by any handler.

**Who owns the protection** (the lock file's holder record). The record is
`{pid, action, host, started_at, spawning, groups}`: `groups` lists the
process groups the runner started and has not yet CONFIRMED gone,
`spawning` is true between "about to start a process" and "its group is
recorded"; each update is one fixed-size write. A clean release empties
the file. When dct is killed (SIGKILL, a crash, a power loss) the kernel
drops the flock while a group may run on — the record stays, and the NEXT
mutating command, after taking the flock and before writing its own
record, refuses (`details.process_groups`, `details.holder`,
`details.lock`; the record is left untouched) while a recorded group still
has a live process. It also refuses when it cannot tell: `spawning` was
true (dct died between starting a process and recording its group), the
record is of another host, or it cannot be read — the message then says
how to clear it (check for leftover processes, then empty the lock file:
`: > <lock>`). A record whose groups are all gone is replaced silently.
The lock's file descriptor is deliberately NOT inherited by the processes:
an inherited descriptor proves nothing (the holder's own unlock releases
the shared lock, a descendant may close it) and would hand the lock to
every daemon a hook or a transport leaves behind.

Limits, stated plainly: a process that LEAVES its group (`setsid`, a
daemon: gpg-agent, an ssh ControlMaster, git's background gc) is neither
stopped nor waited for nor recorded; a recorded group id that the system
reused for an unrelated process after a hard death refuses until that
process ends or the owner empties the lock file; without process groups
(Windows) only the started process itself is stopped and nothing is
recorded.

**Output** is drained continuously from stdout AND stderr at once (a
process is never blocked on a full pipe, however much it prints) into
bounded memory — never into a temporary file:

| what is printed | call sites | how it is kept |
| --- | --- | --- |
| DIAGNOSTIC text | the sync script (stdout + stderr as one stream); stderr of every git command; stdout of `git commit` and `git push` (hook and transport chatter) | sync: the first `effects.SYNC_OUTPUT_MAX_BYTES` (64 KiB); git: the first and the last `gitinfo.GIT_DIAGNOSTIC_MAX_BYTES / 2` (32 KiB each) per stream. The rest is counted and dropped (`output_dropped_bytes`; `[… N bytes of output dropped …]` in a git error); a character split by the cut is dropped whole, never shown as U+FFFD |
| DATA the workflow acts on — also the proof layer's input and the push verification | stdout of every other git command: `rev-parse`, `log`, `diff`, `diff-tree`, `ls-tree`, `ls-files`, `status`, `for-each-ref`, `cat-file` (blobs), `hash-object`, `write-tree`, `config`, `remote get-url`, `ls-remote` | read WHOLE up to `gitinfo.GIT_OUTPUT_MAX_BYTES` (64 MiB); past it the command is an ERROR (the plan / the step is refused) — never a cut result presented as complete |
| DATA that grows with the history | `rev-list --parents` of the plan (`gitinfo.git_read_lines`) | streamed line by line, any size (the plan's own cap: `ANCESTRY_MAX_COMMITS`); complete only after git exited 0; one line over `gitinfo.GIT_LINE_MAX_BYTES` (1 MiB) is an error |

What an interruption or a timeout leaves behind:

| event | state |
| --- | --- |
| Ctrl-C / SIGTERM / SIGHUP during `prepare` (the sync script running) | the script and everything it started are gone; the temporary worktree is removed and pruned; no proposal stored, nothing stamped; the lock is free |
| the sync script exceeds `SYNC_TIMEOUT_SECONDS` | the same, reported as `failed` with exit code 124 |
| Ctrl-C / SIGTERM / SIGHUP or `HOOK_GIT_TIMEOUT_SECONDS` during `git commit` (a hook running) | git, the hook and its children are gone; no commit; the record stays `files_written` — `resume` |
| the same during `git push` | git, the pre-push hook and the transport are gone; the record stays `tagged`; whether the remote received anything is re-read by `resume`, which retries only the push (already there → verified) |
| dct killed with SIGKILL (or a crash) during any of these | no cleanup ran: a temporary worktree of `prepare` stays registered (`git worktree list`; remove it with `git worktree remove --force <path>` once nothing runs in it), the process group may still run — the next mutating command refuses while it does (above) |


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
| `dct release abandon --version V --reason "…" --confirm` | core `abandon_release` as the actor `dct`, under the operation lock: the record ends `abandoned`; stamps, `current_version` and every effect stay; V stays taken. The ONLY path that abandons a release dct executes (K-119 M6): MCP `abandon_release` holds no lock and refuses such a record, so no effect runs past an abandon — afterwards `resume` finds no unfinished record |
| `dct release reconstruct --version V [--confirm]` | a legacy release WITHOUT a record: evidence is gathered read-only and shown; `--confirm` records it (core `reconstruct_release`, no re-stamp) — [Reconstruct](#reconstruct-dct-release-reconstruct-k-60-k-86-k-96-d8) |
| `dct release status [--version V]` | the record (read-only) |
| `dct clog release-advance V STATUS [--release-commit-sha S] [--tag-proof JSON] [--push-proof JSON] [--json]` | the LEDGER path (K-96 D10, `effects.record_progress`): core `advance_release` as the actor `ledger` under the operation lock, for a release dct does NOT execute (an `own` record, or one stamped outside `apply`); a release dct executes is refused (`resume` records its progress). `--tag-proof` / `--push-proof` carry what the caller verified ([proofs](#proof-of-the-tag-and-of-the-push-k-115)) — a record with git reaches `tagged` / `pushed` / `completed` only with them. Outcome `advanced` / `noop`, details `{updated, previous_status}` |
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
the database. `dct release plan --json` reports both in ONE result
(`executor`: the project's setting, and for an unfinished record its stored
executor, progress owner and consent — K-119), so a dialog routes a new
release by `executor.project` and a resume by `executor.record`.

**The stamp binds the expected executor** (K-106): the approval is for a
release performed by ONE executor. `release_entries(expected_executor=…)`
(MCP `release_changelog(expected_executor=…)`; `dct release apply` records
`executor: "dct"` in the consent, which binds the same way) compares it with
the project's CURRENT setting INSIDE the stamp transaction, and the setter
(`dct project release-executor`, `core.projects.set_release_executor`) takes
the same project lock — so a change either precedes that comparison (the
stamp refuses BEFORE any write: no entry stamped, no record, no version-file
contract, the pending proposal stays) or waits until the stamp committed
(the record keeps the executor the stamp saw). As a second barrier the
fresh `apply` re-reads the record after the stamp and verifies its executor
and its scope before its first effect, exactly like a resume. A re-run for
a version that already has a record is compared with that RECORD's executor.

| surface | executor `dct` | executor `own` |
| --- | --- | --- |
| `release plan --json`, `clog candidates` (proof layer) | yes | yes — the own tool consumes the same plan, snapshot and proof map |
| `release attest` (the attestation commit) | yes | yes — a PROOF-layer commit (no ledger write, no release effect); its trailer format is the contract, so an own tool may also write it itself |
| `release prepare` / `apply` / the `clog release` alias | yes | REFUSED before any write (`details.executor: "own"`, pointing here) |
| ledger: MCP `release_changelog`, `get_release`, `advance_release`, `abandon_release` | stamp: use `apply` (an MCP stamp has no consent — below) | yes — the own tool's path |
| `release resume` | only a record with executor `dct` AND a recorded consent | REFUSED (`details.executor`) |
| `release abandon`, `clog release-abandon` (local, under the operation lock) | yes — the only way to abandon a record dct executes | yes (ledger only) |
| MCP `abandon_release` (no lock — it never guesses the checkout) | only a record WITHOUT a consent (stamped outside `apply`); a record dct executes is REFUSED and directed to `dct release abandon` (K-119 M6) | yes |
| `release reconstruct` | yes | yes — it records VERIFIED history and performs no effect; the record gets the project's executor at reconstruction |

**Who records progress** (`core.releases.progress_owner`,
`advance_release(actor=…)` / `abandon_release(actor=…)` — the actor is
ALWAYS checked, K-119 M5: the default is `ledger`, and no value, `None`
included, skips the check): a record with executor `dct` (null on older
records counts as `dct`) AND a consent was stamped by `dct release apply` —
only the effects layer advances it (`resume`, after verifying each effect)
and only the local `dct release abandon` ends it;
MCP `advance_release` / `abandon_release` and `clog release-advance` (actor
`ledger`) are refused. Every other record — executor `own`, or stamped outside the effects
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
   inside it, its own process group, stdin closed, no terminal, GIT_*
   location variables dropped — through the supervised runner
   ([Process supervision](#process-supervision-and-the-locks-ownership-k-108-k-113-k-118-l10)).
   Timeout `effects.SYNC_TIMEOUT_SECONDS` (300 s): the whole process group
   is stopped (SIGTERM, SIGKILL after `procs.TERM_GRACE_SECONDS`) and
   reaped before the command returns — also on Ctrl-C / SIGTERM / SIGHUP,
   and anything the script left running after a normal exit. stdout +
   stderr are drained as one stream into memory (never a file): the first
   `SYNC_OUTPUT_MAX_BYTES` (64 KiB) kept, the rest counted
   (`output_dropped_bytes`). A non-zero exit → outcome `failed` with the
   script's exit code (124 on timeout); nothing stamped, no proposal. The
   worktree is removed (`git worktree remove --force` + `git worktree
   prune`) in every case but one: a process group that could not be
   confirmed gone keeps it (nothing is removed under a process that may
   still write there). A script that needs untracked or ignored files
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
   release commit is accepted (no second tag for another spelling — also
   an annotated one the owner created). `tagged` is recorded WITH the tag's
   verified identity (`tag_proof`, K-115 G4).
7. **Push** (K-97 F2) — to the CONSENTED push target only (never
   re-derived from today's upstream, K-97 F4): its remote must still have
   the approved push URL (else refused: outside the consent). `git
   ls-remote` first: a tag of V on the remote at another commit refuses;
   both already there (the branch at the release commit or a descendant
   this repository knows, the tag exactly at it) → `verified`, not pushed
   again. Else `git push --atomic <remote> <commit>:refs/heads/<branch>
   <commit>:refs/tags/v<V>` (the local `pre-push` and the remote's hooks
   run; `GIT_TERMINAL_PROMPT=0`, the caller's SSH / credential variables
   kept) and `git ls-remote` again must show both; then `pushed` — recorded
   WITH what the remote showed (`push_proof`: the consented target, the
   commit its tag resolved to, its branch tip; K-115 G4) — then
   `completed`. A failed push (rejected, unreachable, a refusing hook)
   leaves the record `tagged` with the git error (`details.git_error`) —
   `resume` retries ONLY the push: never a new bump, commit or tag. Without
   a push target (`--no-push`, outside git): step `push` is `skipped` and
   the record goes from `tagged` to `completed` — the consent's
   `push_target: null` is the explicit local release; no push proof exists
   or is needed.

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
(900 s), stdin closed, no terminal. git runs under the supervised runner
([Process supervision](#process-supervision-and-the-locks-ownership-k-108-k-113-k-118-l10)):
a hook that exceeds the timeout — or is running when dct is interrupted —
is stopped together with everything it started (K-118 L10), and the same
holds for `git push`, its pre-push hook and its transport. Hook output is
kept bounded (its start and its end) in the refusal's `git_error`.

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
never repaired by widening the record; a ledger with several live records
of the version refuses too, K-115 G6), that its consent names its
proposal, and that the record's version, entries and candidate are the
consented ones (K-97 F4: resume executes the STORED consent and asks for no
new approval; anything outside it refuses — a new apply, or abandon), and
that record, consent, proposal, render and proofs describe ONE release
(K-115 G5/G9, `check_record_consistency`). A FRESH `apply` runs the same
three checks on the record it just stamped, re-read from the database,
before its first effect (K-102 / K-106); then the
record against the checkout (K-51): the record's `checkout_root` must be
THIS checkout (another worktree → refused, the record is never re-pinned), a
git record needs its candidate and checkout, no operation may be in
progress. Then, by status:

| status | HEAD must be | what resume does |
| --- | --- | --- |
| `stamped` | the candidate | index: nothing staged or conflicted; unstaged only F and the proposal's files. A record without an approved proposal (stamped outside the effects layer, or before K-76) is refused — the sync is never run after the stamp; abandon it. Otherwise steps 3-4: each artifact classified (baseline → written, approved → kept, else refused with nothing written), the approved blobs written, the full set verified from one read, the manifest recorded |
| `files_written` | the candidate — or the release commit itself (its only parent is the candidate, its tree is `manifest.tree`, its subject `release: v<V>`), which is adopted (then only the real-index update and `committed`) | every manifest file must still hold its recorded hash (a reverted version file → refused, not overwritten); then step 5 (the commit through the hooks) |
| `committed` | the release commit or a descendant | the release commit is verified (tree = `manifest.tree`, parent = candidate); step 6 |
| `tagged` | the release commit or a descendant | a tag of V must point at the release commit and the recorded `tag_proof` must still be what the repository shows (a moved / deleted tag → refused); a record older than the field gets the identity verified NOW with its next progress write; step 7 — ONLY the push, to the consented target (already on the remote → verified); without a push target `completed` |
| `pushed` | the release commit or a descendant | the tag as for `tagged`; the remote must still show the branch and the tag (`git ls-remote`, nothing pushed again; a record without a `push_proof` gets what was read now); `completed` |
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
   a verified candidate → status `completed`, recorded WITH the tag's
   identity as verified now (`evidence.tag_proof` → the record's
   `tag_proof`, K-115 G4; other candidates, e.g. a
   backport on another branch, are listed, not used). Without a tag:
   exactly one verified candidate → status `committed` — the record ends
   where the evidence ends; dct's `resume` does not continue it (no
   consent: nobody approved a push target) — tag / push it yourself and
   record that with `dct clog release-advance … --tag-proof '{…}'`, or
   abandon it. No verified
   candidate, or several without a tag → refused. Without a release commit
   nothing is reconstructed (a stamp without its candidate cannot be
   verified).
4. **Without git**: F (the section, as above) and the contract's files as
   found (version field, or a line holding V) → `completed`.

The record: mode `reconstructed`, the resolved version, the status of step
3, the manifest `{tree, changelog, version_files}`, the `tag_proof` of a
`completed` git release, no consent, proposal, render or push proof (nobody
approved or verified a push through dct — no confirmation is invented for
old history), the project's executor at reconstruction. That executor (G8)
is an explicitly ASSIGNED responsibility for the record's further handling
— who may record progress on it or abandon it — NOT provenance: it does not
say who made the historical release. Any refusal writes nothing — also
when the ledger already holds a record of the version, or several (K-115
G6).

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
| `details` | prepare: see [Prepare](#prepare-dct-release-prepare-k-76), plus `push_target` (what apply would push to by default — the upstream — or null, read-only); apply: `{requested_version, previous_version, bump, entry_ids, mode, released, current_version, push_target}`; attest: `{previous_head, head, entry_ids}`; abandon: `{updated}`; reconstruct: `{evidence: {version (authoritative), requested_version, spellings, entry_ids, candidate_sha, release_commit_sha, tag, tag_proof ({name, object, commit, annotated} \| null), checkout_root, manifest, status, without_git, candidates}}` (`status` = `completed` \| `committed`; `candidates` (git) = `[{commit, verified, reason}]`); advance: `{updated, previous_status}`; conflict: [Conflicts](#conflicts-k-88); refused / failed: the reason (`error`) plus the refusal's data (e.g. `staged`, `unproven`, `proposal` / `proposal_sha256` / `contract` (the alias), `mismatch`, `checkout_changed`, `foreign`, `foreign_paths`, `foreign_content`, `foreign_staged`, `changed`, `path`, `sync`, `exit_code`; the git effects: `git_error`, `commit`, `head`, `tree`, `parents`, `subject`, `expected_tree`, `index_lock`, `push_target`, `remote_tags`, `remote_branch`, `outside_consent`; the operation lock: `lock`, `holder`, `process_groups` (groups a killed dct left running — or `[]` when that cannot be verified), `process_group` (a sync script's group that could not be stopped); the executor: `executor`, `consent`) |

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
| `entry_ids` | the exact entry set (`[]` = version-only); must always equal EXACTLY the live entries carrying the version (K-94 B3 — `verify_release_entry_set` is the pre-effect gate of a resume / retry AND of a fresh apply; `advance_release` checks it again in its own transaction on EVERY call, also a repeat of the current status, K-102; a mismatch refuses, the record is never extended or shrunk) | fixed |
| `candidate_sha` | the commit the release was cut from, or null | immutable (K-61) |
| `checkout_root` | absolute path of the release checkout, or null | fixed |
| `release_mode` | the VERIFIED release mode (K-94 B1): `git` or `without_git` (a project CONFIRMED outside git); null = recorded before the field existed (unverified) | write-once |
| `executor` | who performs the effects (K-85): `dct` (the effects layer) or `own` (the project's own tool) — copied from the project's `release_executor` at stamp time, after the stamp compared it with the executor its caller expects (K-106), and at reconstruction (there: an assigned responsibility, not provenance); null on records older than the field (counts as `dct`). With `consent` it decides who records progress and who may abandon ([Executor](#executor-k-85)) | immutable — a later project-setting change never takes over an unfinished release |
| `status` | the state machine below | forward only |
| `release_commit_sha` | the verified release commit | set once, from `committed` on |
| `manifest` | JSON object of the verified artifacts (e.g. `changelog`, `version_files`, `tree`, `untracked_baseline`) | write-once per key; the MERGED manifest ≤ 64 KiB |
| `proof` | `{"<entry id>": {commits, merge_commit, pr[, pr_source, source, ambiguous, associations]}}` (K-70, K-89); `pr_source` = `git` \| `client`, stored with every `pr` ([origin](#origin-of-a-pr-number-k-116-h7)) | written with the stamp; a re-run passing a different map (the origin included) is refused (K-94 B2) |
| `consent` | what the owner approved (K-97 F4): `{version, entry_ids, candidate_sha, proposal_sha256, render, push_target: {remote, url, branch, tag} \| null[, executor: "dct" \| "own"]}` (`push_target: null` = the EXPLICIT local release: `--no-push`, or outside git; `executor` (K-106) = who the owner approved to perform it — present on consents written since the field) — what a resume EXECUTES: its version, entries and candidate must be the record's, its push target is the only one pushed to (its remote must still have that push URL) | write-once |
| `proposal` | the approved version-file proposal (K-76): `{paths, base_blobs: {path: blob \| null}, expected_blobs: {path: blob}, script: {identity, sha256} \| null, candidate_sha, version, plan_digest, release_mode, changelog: {path, base_blob, base_sha256} \| null}` (the last four: the release context it is bound to; blob ids are git blob ids, or SHA-256 of the content in dct's release blob store for `without_git`); `consent.proposal_sha256` = `proposal_digest(proposal)` (SHA-256 of its canonical JSON). Written by the stamp that consumed the project's PENDING proposal | write-once |
| `render` | the changelog render settings of THIS release (K-90): `{"pr_refs": bool}` — defaults to the project's `changelog_pr_refs` at stamp time, so a later preference change cannot alter the expected hashes | write-once |
| `tag_proof` | the VERIFIED identity of the release tag (K-115 G4): `{name, object (what refs/tags/<name> resolves to — the tag object of an annotated tag), commit (the commit it targets = `release_commit_sha`), annotated, recorded_at}`; null = not proven (no tag yet, a `without_git` release, or a record from before the field) | write-once, from `tagged` on |
| `push_proof` | the VERIFIED push (K-115 G4): `{target: {remote, url, branch, tag}, tag_commit (= `release_commit_sha`), branch_tip, recorded_at}`; with a consent `target` = `consent.push_target` exactly; null = no push verified (a local release, `without_git`, or a record from before the field) | write-once, from `pushed` on |
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
(also `release_mode` null) needs `release_commit_sha` from `committed` on,
its `tag_proof` from `tagged` on and — for `pushed`, and for `completed`
when its consent approved a push — its `push_proof` (K-115 G4,
[below](#proof-of-the-tag-and-of-the-push-k-115)): a status alone is never
proof.

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

<a id="origin-of-a-pr-number-k-116-h7"></a>**Origin of a PR number**
(K-116 H7). Every stored `pr` carries `pr_source` — set by the WRITER, never
taken from the map (`core.releases.validate_proof(proof, pr_source)`):

- `git` — recognised in the repository by the release plan (the `Merge pull
  request #N` subject of the entry's own / introducing merge) and stamped
  by `dct release apply` from the approved snapshot. Such an object must
  carry that recognition (`source` + `merge_commit`), so a heuristic number
  can never be stored as recognised.
- `client` — DECLARED by the caller: a client attestation. Every `pr` that
  arrives through MCP `release_changelog` (or a direct core call) is stored
  this way, whatever `pr_source` the supplied map states — also a map
  copied from `dct release plan`: the server read no repository and asks no
  hosting service. No lookup is required for it; it is simply never
  presented as git's recognition.

The plan's heuristics — a `(#N)` subject suffix (`associations[].source:
"subject_suffix"`, never certain) and `squash_commits[].pr` — are never a
`pr`, hence have no `pr_source` and are never rendered. The origin is shown
wherever the number is: in the record (`get_release`, `release status
--json`) and in `dct release status` (`#12: PR #7 — client attestation` /
`recognised in git (the entry's merge)`; `origin not recorded` for a map
stored before the field).

**PR references in the changelog** (K-90, opt-in): with `changelog_pr_refs`
on at stamp time the record's `render` is `{"pr_refs": true}` and every
entry of THAT release whose stored proof has an unambiguous `pr`
(`core.releases.proof_pr`: set and not `ambiguous`; either origin) renders as `- <entry>
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
otherwise runs implicitly. Caveat (G2): on a SQLite database that is
ALREADY in WAL mode, SQLite itself may create the `-wal` / `-shm` sidecar
files next to the database for any reader, also a read-only one; the
database file's bytes and mtime, its schema and its data stay unchanged
(that is what the tests compare). The SQLite file path is resolved exactly
as the normal engine resolves it for the same URL (`dct.paths.sqlite_url` /
`sqlite_database_path`, K-99 — SQLAlchemy 2.1 percent-decodes the database
part of a URL, 2.0 does not).

### Proof of the tag and of the push (K-115)

A status is never proof — `tagged` used to be recordable by anyone who
called `advance_release`, and a git record could jump to `completed` with no
tag at all. Now the ledger stores WHAT was verified and enforces it
(`core.releases.check_proof_requirements`, on every move into the status,
for every executor, and at reconstruction):

| record | `tagged` | `pushed` | `completed` |
| --- | --- | --- | --- |
| with git (`release_mode` `git`, or null = unverified) and a consent that approved a push (`push_target` set) | `tag_proof` | `tag_proof` + `push_proof` for exactly that target | `tag_proof` + `push_proof` |
| with git, consent with `push_target: null` (the explicit local release) | `tag_proof` | refused (no push was approved) | `tag_proof` |
| with git, no consent (an own tool's record, MCP stamp, reconstruction — its push scope is not recorded) | `tag_proof` | `tag_proof` + `push_proof` (the tool's own target) | `tag_proof` |
| `without_git` | never | never | the manifest of its verified artifacts; a tag / push proof is REFUSED |

- The LOCAL executor verifies the repository: dct's effects layer reads the
  tag (`refs/tags/<name>`: the object it resolves to and the commit it
  peels to) and the remote (`git ls-remote` of the consented target) and
  passes what it saw; an own tool reports the same through
  `advance_release(tag_proof=…, push_proof=…)` / `dct clog release-advance
  --tag-proof … --push-proof …`. The LEDGER only cross-checks: the tag's
  `commit` and the push's `tag_commit` must be the record's
  `release_commit_sha`; a lightweight tag has `object == commit`, an
  annotated one a different `object`; a push `target` must equal the
  consent's; for a release dct executes the tag `name` must be a spelling
  of the version (an own tool's tag name is its own format). Report FULL
  commit ids.
- A proof describes the state AT VERIFICATION TIME (`recorded_at`, stamped
  by the ledger) — not a guarantee that nobody deletes or moves the tag
  later. On resume dct compares the recorded tag with the repository again
  and refuses a moved or deleted one.
- A missing field is never treated as proof (G3): a record that reached
  `tagged` / `pushed` before the fields existed needs the proof for its
  next move (dct's `resume` re-verifies and supplies it); a record that is
  already `completed` stays as it is — nothing is added to old history
  after the fact, and its null proof simply means "not proven".

**One consistent release** (K-115 G5/G9, `core.releases.check_record_consistency`
— before every ledger write, also a repeat, and before any effect): each
JSON field being valid alone is not enough. The consent must name the
record's version (version contract), EXACTLY its `entry_ids`, its
`candidate_sha`, its `render` (when both are recorded), its `executor`
(when the consent names one) and — by `proposal_sha256` — the RECORDED
proposal (a digest without a proposal, or a proposal beside a consent that
names none, is a contradiction); the proposal's `version`, `candidate_sha`
and `release_mode` (each when bound) must be the record's; the proofs must
match the release commit and the consented push target. A contradiction
that was written outside the API blocks every further progress write —
nothing is repaired by widening either side.

**An ambiguous ledger refuses writes** (K-115 G6): one live record per
(project, version) is enforced under the project lock and by a unique
index on the stored text — data from before the index, or raw SQL, can
still leave SEVERAL live records of one semantic version (`1.1` and
`1.1.0`; the bootstrap only warns when the index cannot be created). Then
every WRITE of that version — `advance_release`, `abandon_release`, the
stamp and its re-run, `reconstruct_release`, the history import, the
pre-effect gate of apply / resume — refuses and names the duplicates
instead of picking the first record. Diagnostic reads keep working:
`get_release` / `dct release status` return the oldest record plus
`duplicates` (`[{id, version, status}]` of all of them; the key is absent
on a healthy ledger). Repair is manual: soft-delete the wrong row(s)
(`releases.deleted_at`).

**The stamp is the only writer of versions** (K-102): public core
`changelog.add_entry` refuses every explicit `version` (the parameter
remains only for positional compatibility) and runs under the same project
lock as the stamp; old released history and test fixtures use the separate
`changelog.import_released_entry`, which refuses a version that has a
release record (any spelling) or that is higher than the current version.
An entry that nevertheless carries the version of a recorded release (raw
SQL) makes the record inconsistent: every progress write and every effect
is refused until it is resolved — the record and its consent are never
widened to include it.

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
   proof={<id>: proof[<id>], …}, expected_executor="own")` with an explicit
   version and an explicit `entry_ids` list (`[]` = version-only);
   `expected_executor` (K-106) makes the stamp refuse when the project's
   setting is no longer `own`. A `pr` in the map is stored as the tool's
   declaration (`pr_source: "client"`, also when copied from the plan —
   [origin](#origin-of-a-pr-number-k-116-h7)). It writes the release record
   (`stamped`); a repeat with the same set is a no-op, a different set for
   a recorded V is refused.
3. **Own effects, verified by the tool** — the tool performs its effects and
   checks each against reality itself (file content, the commit's tree and
   parent, the tag's target). A status is never proof: report only what was
   verified.
4. **Progress** — `advance_release(version=V, status=…)` after each verified
   effect, in order `files_written` (optionally with `manifest`) →
   `committed` (with `release_commit_sha`; the candidate stays immutable) →
   `tagged` (with `tag_proof` — the tag as the tool verified it: `{name,
   object, commit, annotated}`, the commit being the release commit; the
   tag NAME is the tool's own format) → `pushed` (after the verified push,
   with `push_proof`) → `completed` ([proofs](#proof-of-the-tag-and-of-the-push-k-115):
   a tool that does not push completes from `tagged`). An interrupted release is resumed from
   `get_release(project)`, never re-stamped under a new version;
   `abandon_release` ends it explicitly with a reason.

A record the project's own tool stamped is finished by that tool: `dct
release resume` refuses it (its executor is `own`, or — stamped through MCP
without the setting — it carries no consent) and never adopts a foreign
tool's effects. Its tests: `tests/test_release_executor.py` (a fake own tool
run as a separate process: shared plan → ledger stamp → its own effects →
`advance_release`; dct's `apply` / `resume` refuse and its effects survive).

