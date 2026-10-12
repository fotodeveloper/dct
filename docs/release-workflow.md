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
   abandon / reconstruct / takeover / version-files` (changelog file, version
   files, release commit, tag, push), the
   default behind `/release` ([below](#the-effects-layer)). A project with its
   own release tool replaces only this layer: it verifies its own effects and
   reports progress through `advance_release`. Which of the two performs a
   release is EXPLICIT — the project's `release_executor` (`dct` | `own`),
   copied onto each record at its stamp ([Executor](#executor-k-85)).

Implementation: `dct/workflows/release/proof.py` (pure rules over commit
messages), `dct/workflows/release/plan.py` (git + database reads) and
`dct/workflows/release/effects.py` (the effects layer). The
rules in prose: [assistant-fallbacks.md](assistant-fallbacks.md#changelog-proof-trailers-and-range-shared-by-commit-and-release).

Both documents carry `schema_version` (currently `5`; ONE constant,
`plan.SCHEMA_VERSION`, for the plan and the candidates) — it moves when the
meaning of a key changes:

- version 2 added `resume`, `snapshot`, `snapshot_digest`, the graph-nearest
  range base, the merge-revert mainline and the per-recognition provenance;
  a `(#N)` subject suffix is no longer a `pr`;
- version 3 added `executor` (the project's executor and, for an unfinished
  record, the record's stored executor, progress owner and consent — K-119),
  `pr_source` on every proof object (K-116 H7), the revert comparison that
  keeps blob identity (K-105) and the ancestry cap that refuses (K-116 H8);
- version 4 added `changelog_file` — the changelog file a release writes,
  DETECTED among the candidate's tracked files instead of an assumed
  `changelog.md`, with the problem prepare and apply refuse before the
  stamp (#876, [The changelog file](#the-changelog-file-876));
- version 5 added `version.suggested` — the version dct proposes for the
  snapshot's selection, kept OUTSIDE the snapshot so the plan digest does
  not change ([The version suggestion](#the-version-suggestion)) — and the
  top-level key `pending_consent` (the preview a dialog may finish —
  [Apply --consent](#apply---consent-the-one-dialog-release)); the
  candidates gained `secret_like` (the changed files whose path looks like
  a secret).

A client that needs a key checks `schema_version` first (a plan with
`schema_version` lower than 3 has no `executor`, lower than 4 no
`changelog_file`, lower than 5 no `version.suggested`, no
`pending_consent` and no candidates `secret_like`). The effects layer's result
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
| `schema_version` | `5` |
| `project` | slug |
| `kind` | `"release"`, or `"resume"` when an unfinished release record exists (then `resume` is set and nothing is computed on today's HEAD) |
| `version` | `{current, target, bump, suggested}` — `target` from `--version` (normalized) or the bump; for a resume without a version, the record's version. `suggested` = the version dct proposes for the snapshot's selection, whatever `--version` / the bump flag asked (`{bump, target, reason, safe, taken, alternatives}`, [The version suggestion](#the-version-suggestion)); it lives OUTSIDE the `snapshot`, so `snapshot_digest` does not depend on it. `null` for a resume plan, a project without a path and a project whose executor is `own` (its own release tool picks the version) |
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
| `changelog_file` | the changelog file the release writes (#876 — `core.changelog_file.resolve_changelog` at the candidate; for a resume the RECORDED candidate): `{managed, configured (the stored `changelog_path`), explicit (`projects.changelog_path_explicit` — the owner chose the path; false = detect), path (base-relative; null when not managed or on a problem), source: "stored" \| "detected" \| "default" \| null, format: "markdown" \| "other" \| null, candidates: [{path, format}], problem: null \| "ambiguous" \| "case_variant" \| "not_markdown" \| "symlink" \| "not_a_directory" \| "not_a_file" \| "outside_project" \| "not_baseline", found: [path], ways: [{way, command, what}]}` — `symlink` / `not_a_directory` / `not_a_file` also carry `component: "file" \| "directory"` (K-19); `not_baseline` (a new release in git — with uncommitted work checked at the future tree of the work-in-progress commit, as the preview checks it: [The changelog file](#the-changelog-file-876)) also carries `baseline: "absent" \| "tracked" \| "unfiltered"`, and a directory of the path that is a link or no directory ON DISK carries `baseline: "work_tree"`, as does a TRACKED directory / link whose name differs only in case that a case-insensitive file system opens at the file's path (`not_a_file` / `symlink`, component `file`, K-28). The problem `content` (K-9) appears only in a refusal of prepare / apply, never in the plan (the plan composes no section). A problem adds a warning; `prepare` and `apply` refuse it before the stamp ([The changelog file](#the-changelog-file-876)). `null` for a project without a path. Read-only: nothing is stored |
| `warnings` | human-readable strings (range to confirm, approximate range, revert outside the range or unconfirmed, unfinished release, HEAD not the recorded candidate, selection holding unproven ids, no git, the changelog file's problem or the other changelog-like files beside it) |
| `resume` | `null`, or for a resume plan (K-95 C5) the RECORDED release: `{version, status, candidate_sha, checkout_root, release_mode, entry_ids, proof, release_commit_sha, manifest, effects: {stamped, files_written, committed, tagged, pushed, completed} (booleans by status), head (today's HEAD, separately), head_is_candidate, new_unreleased (entries added since — NOT part of the record), blocked}`. `resume.blocked` (K-34): `null` when `dct release resume` can continue, else `{approved, now, restore, resume, abandon, message}` — computed by the SAME read-only predicate resume refuses with (`plan.resume_block`; see [The changelog file](#the-changelog-file-876)), also named in `warnings`; a dialog never offers finishing the release while it is set. A resume plan has `range: null`, empty `entries` and `proof`, `snapshot: null` — an approved release is never recomputed on today's HEAD |
| `snapshot` | the IMMUTABLE approved context (K-80): `{project, version, current_version, git, repo_root, checkout_root, project_root, candidate_sha (full), range: {base_tag, base_sha, approximate, since, revs}, selected_ids, proof (the map of the selected ids), entries: {"<id>": state digest}}` — `entries` covers EVERY unreleased entry the plan saw (`core.changelog.entry_state_digest` over id, category, entry, component, version), so ANY change of the unreleased set after the plan — also a new entry outside the selection — is another snapshot and needs a new plan (K-116 H3); `null` for a resume plan and for a project without a path |
| `snapshot_digest` | SHA-256 (hex) of the snapshot's canonical JSON (sorted keys, compact separators, UTF-8) — what `dct release apply --plan-digest` takes |
| `pending_consent` | the project's preview slot as a dialog routes it ([Apply --consent](#apply---consent-the-one-dialog-release)): `null` without a slot (and in the effects layer's own pinned plans), else `{preview_id, created_at, base_head, checkout_root (the checkout the preview was made in), variants: [{name, version}] (the preview's order — no digests), progress: {work_commit, attest_commit}, applied (null, or once a commit was recorded under the owner's Release the variant it was made under — `{name, version, entries, attest}`, no digest — so the Finish question says what it releases), adoptable (bool — `plan.consent_adoption`, the ONE predicate `effects.apply_consent` uses before its commits, after them and before the stamp: the ref HEAD names is the preview's — a detached HEAD included, a branch switched on the same commit is another context —, HEAD is the recorded progress commit or a commit apply would adopt, and the chain names every recorded commit), consent_digest, blocked}` — `consent_digest` is non-null ONLY when `progress` is (the owner said Release and a commit was made under that consent): the slot's `progress.consent` — the C the commits were made under, also when both variants share them — provided adoptable is true for that C. `blocked` is null when adoptable or when no commit was recorded; else why the release approved earlier cannot be finished HERE — `{why: "other_checkout" \| "branch_switched" \| "head_moved" \| "progress_lost" (apply's own `base_moved.why`), head, head_ref (null when detached), expected_ref (the preview's; null when it was detached), next_step}` — `next_step` is one plain sentence that works (switch back with `git switch <branch>` — for a preview made on a detached HEAD `git switch --detach <the verified progress commit: the recorded A, else W>` (`plan.switch_back_command`, the same command apply's refusal names; never the base H0, which drops the commits made from HEAD's chain), release it in the other checkout, discard the preview and preview again, preview again), never "finish": a dialog never offers a Finish that apply refuses. A recorded commit that no longer resolves (reset away and pruned by `git gc`) is not adoptable: `consent_digest` null, `blocked.why` `progress_lost` and a warning naming it — the plan never raises for it. A preview without progress is a draft: the next preview replaces it, `--discard-preview` removes it |

<a id="the-version-suggestion"></a>**The version suggestion** (`version.suggested`,
`plan.suggest_version`). Computed from the categories of the entries the
snapshot SELECTS (an entry outside the selection never moves the bump), the
project's `current_version` and — only without one — the version of the
range's base tag (`range.base_version`). The result is `{bump, target,
reason, safe, taken, alternatives}`:

| condition | `bump` / `target` | `reason` | `safe` |
| --- | --- | --- | --- |
| no current version, no ancestor release tag | `null` / `0.1.0` | `first_version` | true — shown, not asked |
| no current version, an ancestor release tag vX | the category rule applied to X | `after_tag` | false |
| a current version outside the version contract (`X.Y` / `X.Y.Z`) | `null` / `null` | `current_unparseable` | false — the owner types a version |
| nothing selected (version only) | patch | `version_only` | true |
| only `fixed` | patch | `fixes_only` | true |
| `added` or `changed`, no `removed` | minor | `features` | true |
| `removed`, current ≥ 1.0 | major | `breaking` | false — the minor is among the alternatives unless it is taken |
| `removed`, current 0.x | minor | `breaking_0x` | true — never 1.0.0 |

- **Taken versions.** A version is taken when a local tag names it in any
  spelling (`gitinfo.version_tags`: `v0.2.0`, `0.2.0`, `v0.2`, `V0.2.0`,
  lightweight or annotated, on any commit) or a release record of it exists
  in any status, `abandoned` included, or live released changelog entries
  carry it (imported history without a record or a tag). A taken target moves to the next
  patch above it — up to 100 steps (`plan.SUGGEST_MAX_STEPS`), then
  `target` is `null` — every skipped version is listed in `taken`, `reason`
  becomes `tag_taken` and `safe` false; `bump` stays the category rule's.
  A taken `0.1.0` of a first release gives `0.1.1` with `tag_taken`.
- **`alternatives`** (`[{bump, target}]`, ascending): the other bumps of
  the base — for ≥ 1.0 among patch / minor / major, for 0.x only patch and
  minor — taken ones left out; `[]` for `first_version` and
  `current_unparseable`.
- **Never 0.x → 1.0.0.** For a 0.x base neither the target nor an
  alternative has a major version of 1 or more: 1.0.0 is only ever a
  version the owner types.
- `safe: false` means the owner should see why (the reason) before the
  version is used; `version.target` (from `--version` or a bump flag) is
  never replaced by the suggestion.

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
  is equally near — show it (and the alternatives) and confirm it. K-36:
  the PLAN is the one place that asks (its warning ends "— confirm it";
  the one-dialog release shows the range on its one screen — answering
  Release confirms it — and never asks again); the approved
  `snapshot_digest` binds the base, so `prepare` and `apply` replace exactly
  that warning (`plan.range_notice`) with information — "the proof range
  starts at vX, not at vY — the base the approved plan digest binds". The
  strict alias (no plan digest) still refuses such a range.
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
| `schema_version` | `5` |
| `project` | slug |
| `checkout` | `{git: true, toplevel, branch, head, detached}` or `{git: false}` |
| `range` | the `range` object of the plan for target HEAD, or null |
| `confirmed` | client-provenance entries of the current branch whose worktree is this toplevel |
| `uncertain` | entries with server-detected or no provenance — whatever branch / worktree they store — and, on a detached HEAD, every client entry of this checkout; a client entry of the current branch whose worktree is NOT recorded (K-95 C2: a candidate to compare with the diff, never confirmed); each with `uncertain: true` and `uncertain_reason` ∈ `server_detected`, `no_provenance`, `detached_head`, `no_worktree`. NEVER attached automatically: match against `staged_files`, confirm (`update_changelog` branch + worktree) first |
| `other_branch` | client entries of this worktree recorded on another branch (offer to correct the branch) |
| `already_proven` | ids a commit of the range already proves — removed from every list |
| `staged_files` | `git diff --cached --name-only` (repo-root-relative) |
| `secret_like` | the changed files (staged, unstaged or untracked — `git status`, renames by their new path) whose path looks like a secret by its file NAME — the release preview's ONE predicate (`plan.secret_like`: `.env`, `.env.*` except the templates `*.example` / `*.sample`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `credentials*.json`); a name heuristic, never a content scanner: `[{path, staged, unstage}]` sorted by path. `staged` false → never offered for staging; `staged` true → the commit stops, `unstage` is the quoted command that takes it out of the index without touching the file (`git restore --staged -- <path>`; `git rm --cached -q -- <path>` before the first commit), else null — dct never changes the index itself. `[]` outside git |
| `warnings` | detached HEAD, not a checkout, unreadable range, unreadable changed-file list |

Client entries of other checkouts are not listed.

## The effects layer

`dct/workflows/release/effects.py` — ONE implementation for the CLI and every
client skill (a skill only runs the dialog). Every effect is VERIFIED
against reality before the ledger records its progress; a status is never
taken as proof. The release commit and its tag are pushed to the approved
push target (K-97 F2). The MCP server never runs this code.

**Operation lock** (K-96 D7, `dct/workflows/release/oplock.py`): every
MUTATING command — `prepare`, `apply`, `resume`, `attest`, `abandon`,
`reconstruct --confirm`, `takeover --confirm`, `version-files --confirm`,
the `clog release` alias and the old ledger
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
(`plan`, `status`, the previews of `reconstruct`, `takeover` and
`version-files`) take no lock. The lock is
also the scope of the supervised operation — next section.

**One lock identity** (K-118 L8, `effects._lock_file`): every command above
derives the SAME key, a function of the project's REGISTERED PATH only —
with or without a current directory, by the full command or its alias: the
git common dir of the repository holding that path
(`gitinfo.release_lock_location`), or the directory itself when the path is
CONFIRMED outside git. There is no substitute key. A project WITHOUT a
registered path (`details.reason: "no_path"`), or with a path that cannot
be verified — a missing directory, a broken repository, git missing
(`"unverifiable_path"`) — is REFUSED (`details.lock: null`) before anything
is read or written: a lock derived from the slug or from the path string
would let `abandon` / `clog release-advance` run beside a release that
holds the repository's lock. Register or correct the path (`dct project
rename <slug> --path <dir>`) and run the command where it exists; a release
dct does not execute can also be recorded through the MCP ledger tools,
which take no lock and refuse a record dct executes.

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
| DIAGNOSTIC text | the sync script (stdout + stderr as one stream); stderr of every git command; stdout of `git hook run` and `git push` (hook and transport chatter) | sync: the first `effects.SYNC_OUTPUT_MAX_BYTES` (64 KiB); git: the first and the last `gitinfo.GIT_DIAGNOSTIC_MAX_BYTES / 2` (32 KiB each) per stream. The rest is counted and dropped (`output_dropped_bytes`; `[… N bytes of output dropped …]` in a git error); a character split by the cut is dropped whole, never shown as U+FFFD |
| DATA the workflow acts on — also the proof layer's input and the push verification | stdout of every other git command: `rev-parse`, `log`, `diff`, `diff-tree`, `ls-tree`, `ls-files`, `status`, `for-each-ref`, `cat-file` (blobs), `hash-object`, `write-tree`, `commit-tree`, `stripspace`, `var`, `config`, `remote get-url`, `ls-remote`; the answers of `update-ref --stdin` | read WHOLE up to `gitinfo.GIT_OUTPUT_MAX_BYTES` (64 MiB); past it the command is an ERROR (the plan / the step is refused) — never a cut result presented as complete |
| DATA that grows with the history | `rev-list --parents` of the plan (`gitinfo.git_read_lines`) | streamed line by line, any size (the plan's own cap: `ANCESTRY_MAX_COMMITS`); complete only after git exited 0; one line over `gitinfo.GIT_LINE_MAX_BYTES` (1 MiB) is an error |

What an interruption or a timeout leaves behind:

| event | state |
| --- | --- |
| Ctrl-C / SIGTERM / SIGHUP during `prepare` (the sync script running) | the script and everything it started are gone; the temporary worktree is removed and pruned; no proposal stored, nothing stamped; the lock is free |
| the sync script exceeds `SYNC_TIMEOUT_SECONDS` | the same, reported as `failed` with exit code 124 |
| Ctrl-C / SIGTERM / SIGHUP or `HOOK_GIT_TIMEOUT_SECONDS` during a commit hook (`git hook run` pre-commit / prepare-commit-msg / commit-msg) | git, the hook and its children are gone; no commit object, no ref moved; the record stays `files_written` — `resume` |
| the same during the ref transaction of the publication | git is stopped and drops its uncommitted transaction with its locks: the ref is where it was (a SIGKILL of git itself can leave a `<ref>.lock` / `HEAD.lock`, as after any killed git — remove it once no git runs); `resume` |
| the same during post-commit | the release commit IS published; the hook is not run again — `resume` adopts the commit |
| the same during `git push` | git, the pre-push hook and the transport are gone; the record stays `tagged`; whether the remote received anything is re-read by `resume`, which retries only the push (already there → verified) |
| dct killed with SIGKILL (or a crash) during any of these | no cleanup ran: a temporary worktree of `prepare` stays registered (`git worktree list`; remove it with `git worktree remove --force <path>` once nothing runs in it), the process group may still run — the next mutating command refuses while it does (above) |


### Commands

Two ways to the same release effects:

- **The one-dialog release** (what `/release` and the `/release` fallback
  run): `plan` (read-only — routing only, in this order: an unfinished
  record, the executor, `pending_consent` (a commit made under the owner's
  Release → the Finish question), then the changelog file) → `prepare --preview`
  (everything the release will do, computed BEFORE the question on
  synthetic commits: the commit of the uncommitted changes, the entries,
  the changelog section, the isolated version sync, the checks, the push
  target) → ONE approval: the owner's answer to the one screen rendered
  from that preview — "Release", or "Release with all N", with the suggested
  version listed first (a first version, `first_version`, is shown without
  the version question; another first version goes through "Change
  something") — → `apply --consent C` with the `consent_digest` of the
  variant the screen rendered → `resume` after an interruption. Another
  version, "Change something" or a refusal of R4-R7 or R9-R12 is a new
  preview and the screen again, never a second apply of the old screen
  (R8 stops with the hook's output); "Stop" is
  `prepare --discard-preview`.
- **The CLI path** (unchanged): `plan` (read-only, approve its
  `snapshot_digest`) → `prepare` (the isolated version sync — approve its
  `proposal_sha256`, which shows the file list / contract BEFORE the stamp,
  K-97 F3) → `apply` → `resume` after an interruption.

| command | effect |
| --- | --- |
| `dct release attest --entries 16,17 --confirm` | the EMPTY attestation commit on the current branch (or detached HEAD): subject `chore: attest changelog entries`, final paragraph `Changelog: <ids ascending>`. Only ids the plan lists as unproven; refused when anything is staged, an operation (merge, rebase, cherry-pick, revert) is in progress, or a release is unfinished. Created by the same mechanism as the release commit ([Commits and hooks](#commits-and-hooks-k-96-k-79-k-103): the repository's hooks run, on a temporary index holding HEAD's tree; the commit object is created on the PINNED HEAD and checked BEFORE publication): tree unchanged, subject, and the ids the proof parser reads from it — git's `Changelog` trailer plus the strict body scan — EXACTLY the approved ones (K-107: equality, not containment; `details.approved_ids` / `proven_ids`). A hook that adds, drops or duplicates an id, rewrites the subject, stages content or changes a file in the work tree (a tracked path or a conflicted path, against a reading taken before the hooks — `hook_changed_tree`) is refused with NOTHING published (no later plan can read an unapproved attestation from history); an addition that leaves the id set alone (a `Signed-off-by:`) is fine. Then the ref HEAD names moves by compare-and-swap. The candidate changes; no record exists yet. |
| `dct release prepare [--version V \| --patch \| --minor \| --major] (--entries … \| --no-entries) --plan-digest D [--json]` | the PREPARE stage (K-76, [below](#prepare-dct-release-prepare-k-76)): the version-sync script runs ONCE, isolated, and its result becomes the project's PENDING version-file proposal; outcome `prepared` with `proposal_sha256`. Nothing is stamped. |
| `dct release prepare --preview [--version V \| --patch \| --minor \| --major] [--entries … \| --no-entries] [--attest IDS] [--message-file F] [--include-untracked PATH]… [--push-remote R --push-branch B --push-url U \| --no-push] [--no-variants] [--json]` | the one-screen release ([The preview](#the-preview-dct-release-prepare---preview)): what the release WOULD be — the commit of the uncommitted changes, the entries, the changelog section, the version files, the push — computed on synthetic commits and stored in the project's preview slot with the digest `C` of each variant's consent view; outcome `preview`. Nothing committed, staged, tagged or pushed. |
| `dct release prepare --discard-preview [--json]` | clears the preview slot (the owner declined the screen), removes its pin and releases its blobs; outcome `noop` with `details.discarded` = the preview id, or null when the slot was empty |
| `dct release apply --consent C --confirm [--json]` | the one-dialog release ([Apply --consent](#apply---consent-the-one-dialog-release)): releases EXACTLY the screen of `prepare --preview` whose `consent_digest` is C — the work-in-progress commit and the attestation commit it showed are made through the repository's hooks, the content is re-derived and compared with C before and after them, then the stamp consumes the preview and steps 3-7 below follow. Takes none of `--version`, the bump flags, `--entries`, `--no-entries`, `--plan-digest`, `--proposal-digest`, the push flags (`consent_flag_conflict`). Not offered by the `clog release` alias or by MCP. |
| `dct release apply [--version V \| --patch \| --minor \| --major] (--entries … \| --no-entries) --plan-digest D --proposal-digest P (--push-remote R --push-branch B --push-url U \| --no-push) --confirm` | one release from the APPROVED plan (K-80) and the APPROVED proposal (K-76): `D` = the `snapshot_digest` of `dct release plan`, `P` = the `proposal_sha256` of `dct release prepare`, both with the SAME version and selection — steps 1-7 below. The push target (step 7) is approved with it: in git a new release names the COMPLETE target prepare showed — remote, branch and URL — or `--no-push` (K-123; outside git: no push flag). A re-run of a recorded / historical release needs no `P` (no-op). |
| `dct release resume` | finishes the unfinished record (table below). No `--confirm` (K-97 F4): it executes the CONSENT stored at apply — version, entries, candidate, proposal, push target — and refuses whatever lies outside it (then: a new apply, or abandon) |
| `dct release abandon --version V --reason "…" --confirm` | core `abandon_release` as the actor `dct`, under the operation lock (the repository of the project's registered path — refused when it cannot be determined, K-118 L8): the record ends `abandoned`; stamps, `current_version` and every effect stay; V stays taken; the pin of its proposal's blobs is removed (K-117). The ONLY path that abandons a release dct executes (K-119 M6): MCP `abandon_release` holds no lock and refuses such a record, so no effect runs past an abandon — afterwards `resume` finds no unfinished record |
| `dct release reconstruct --version V [--release-commit SHA \| --evidence FILE] [--confirm]` | a legacy release WITHOUT a record: evidence is gathered read-only and shown (the verified scope, every candidate, the stage it proves, how to finish it); `--confirm` records it (core `reconstruct_release`, no re-stamp, nothing in the repository modified). `--release-commit` chooses one of several verified candidates; `--evidence` is the adapter of a project whose executor is `own` — [Reconstruct](#reconstruct-dct-release-reconstruct-k-60-k-86-k-96-d8) |
| `dct release takeover --version V [[--push-remote R [--push-branch B]] [--push-url U] \| --no-push] [--takeover-digest D --confirm]` | dct takes over an EXISTING record without a consent (a reconstruction that ended at `committed`, a record stamped through MCP, an older record) from its verified artifacts: without `--confirm` it shows the consent it would record and its `takeover_digest`; with `--takeover-digest D --confirm` it records consent and proposal — no bump, no re-stamp, no effect, nothing recorded changed. Then `dct release resume` performs only the missing effects — [Takeover](#takeover-dct-release-takeover-k-119-m3-k-117-j4) |
| `dct release version-files [--set PATHS \| --none] [--confirm]` | shows the project's version-file contract — UNKNOWN (never confirmed) or confirmed, also the empty one; `--set a,b` / `--none` show the proposal, with `--confirm` store it (`core.set_version_files`, under the operation lock; the earlier contract goes to its history). For a project that needs a contract without an `apply` (K-110: a reconstruction outside git) |
| `dct release status [--version V]` | the record (read-only) |
| `dct clog release-advance V STATUS [--release-commit-sha S] [--tag-proof JSON] [--push-proof JSON] [--json]` | the LEDGER path (K-96 D10, `effects.record_progress`): core `advance_release` as the actor `ledger` under the operation lock, for a release dct does NOT execute (an `own` record, or one stamped outside `apply`); a release dct executes is refused (`resume` records its progress). `--tag-proof` / `--push-proof` carry what the caller verified ([proofs](#proof-of-the-tag-and-of-the-push-k-115)) — a record with git reaches `tagged` / `pushed` / `completed` only with them. Outcome `advanced` / `noop`, details `{updated, previous_status}` |
| `dct clog release-abandon V --reason "…" --confirm [--json]` | alias of `dct release abandon` (the same function, the same lock) |
| `dct clog release [V] [--patch …] [--entries … \| --no-entries] --proposal-digest P (--push-remote R --push-branch B --push-url U \| --no-push) --confirm [--json]` | alias of `apply` (the same function, no rules of its own — the same complete push target, K-123); `--confirm` is REQUIRED as for apply (K-96 D1 — it commits, tags and pushes; running it is not the confirmation). `--proposal-digest` is REQUIRED for a new release (K-117): the alias never prepares and never approves a proposal by itself — equal paths and a checked version field are no consent to the content. No `--plan-digest`: the plan is derived inside the call for the candidate it pins, with the same stamp binding, and the approved proposal must have been prepared for EXACTLY that plan (K-116 H2: `proposal.plan_digest` = the digest derived here; `details.mismatch` otherwise) — so run `dct release plan --version V … --json`, `dct release prepare … --plan-digest D`, then the alias with that `P`. Without `--entries` it releases every unreleased entry — all must be proven. Refused instead of guessed: a missing proposal digest, unproven entries, a proof range that needs confirmation (`range.confirm_required`). |

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
| `release reconstruct` | yes — by dct's own rules (`release: v<V>`, dct's changelog layout); `--evidence` REFUSED | yes — ONLY through the own adapter (`--evidence FILE`: the tool's evidence, verified in the repository); dct's default parser REFUSES (`details.executor: "own"`). Either way it records VERIFIED history and performs no effect; the record gets the project's executor at reconstruction — an assigned responsibility, shown as such |
| `release takeover` | a record with executor `dct` and NO consent whose release commit verifies | REFUSED — an `own` record stays with its tool (the ledger path) |
| `release version-files` | yes | yes |

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
a push, L9). Such a record is never left at "do it by hand or abandon"
(K-119 M3 / K-117 J4) — the refusal names both ways to finish it: the
ledger path, or — for a record with executor `dct` whose release commit
exists — the controlled [takeover](#takeover-dct-release-takeover-k-119-m3-k-117-j4),
which records the missing consent from the verified artifacts (no
re-stamp). The takeover is the ONLY way a record becomes one dct executes
after its stamp: `advance_release` refuses a first consent on a `dct`
record, for every actor (a progress write never changes who executes a
release).

`--confirm-version-files` was removed (K-76): it approved whatever result
happened to be present after the stamp. It survives as a hidden flag of
`apply`, `resume` and `clog release` that REFUSES (exit 1) and names the
prepare procedure.

### Prepare (`dct release prepare`, K-76)

An explicit EFFECTS stage before the stamp — not part of the read-only plan:

1. **The same pre-checks as apply** — strict checkout, no unfinished
   record, clean index and work tree, the plan re-derived for the pinned
   candidate must equal `--plan-digest`, the selection proven, and the
   [changelog file](#the-changelog-file-876) settled (`details.changelog_file`
   — refused before the script runs). A version
   that already has a record, or is lower than `current_version`, has
   nothing to prepare (refused). For a new release, before the script
   runs: the changelog composition on the candidate's content (problem
   `content`, K-9) and no local tag of V (`details.tags`, K-8).
2. **The script of the CANDIDATE** — `scripts/dct-version-sync.sh` (else
   `scripts/version-sync.sh`) under the project root, as the candidate
   commit holds it (an untracked script is refused: the release runs only
   what its candidate holds). The dct-named script takes precedence; a
   script named exactly `scripts/version-sync.sh` runs automatically when
   it is absent — no setting switches it on or off, so `/dct-init` says so
   when it finds one. No script → an EMPTY proposal (approved the
   same way, D4). K-37: the script changes ONLY the version files, in its
   working directory, and nothing else — prepare runs it BEFORE the owner's
   consent screen, and the isolated worktree is not a sandbox (it can still
   reach the network, the user's home, any absolute path). Never wrap a
   release / publish / deploy command (an existing `release.sh`, a
   `release` make or just target) as the version sync: it could publish
   before anyone approved the release.
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
   still write there). **The environment contract** (K-117 J2 / J3,
   `effects.sync_environment` — reported as `details.environment` with
   every failure of the script): it runs ISOLATED and only there
   (`isolated: true`, `in_place_fallback: false`); `files` — only the
   files of the candidate: no untracked or ignored file of the checkout (a
   virtualenv, `node_modules`, build output, a local tool, `.env`) and no
   submodule content; `script` — the one committed in the candidate; the
   caller's environment without GIT_* repository variables, `PATH` as dct
   sees it; stdin closed, no terminal; no repository hook. A script that
   depends on something outside that — a missing dependency — FAILS there
   with that contract in the message; dct never falls back to running it
   in the checkout. Make the script provide what it needs (install or
   build it itself, or call a tool on `PATH`) and commit that.
4. **The checkout is verified unchanged** — HEAD, every ref, the index,
   the status, the bytes of every untracked file and the bytes (with the
   executable bit) of every tracked path that already had a change — a
   file that is ` M` before and ` M` after the script but holds other
   bytes is a change too; a script that wrote
   outside its worktree is refused with what changed (`checkout_changed`
   = only the parts that differ: `head` `{before, after}`, `refs` — the
   `"<refname> <object>"` lines in only one of the two reads, so a moved
   ref appears with both objects —, `index` — the `"<mode> <blob>
   <stage>\t<path>"` entries likewise —, `status` lines, `untracked`
   paths and `tracked` paths (an already-modified file rewritten, deleted
   or retyped); outside git a list of paths; the message NAMES the changed
   files — work tree, index, untracked —, then HEAD and the refs by name,
   e.g. a tag the script created — K-37) — dct restores nothing.
5. **The proposal** — the changes are staged in the TEMPORARY worktree's
   own index (`git add -A`; ignored files stay out; blobs land in the
   repository's object database) and compared with the candidate:
   `id` (its STABLE identity, a fresh ULID per prepare — K-117),
   exact paths, `base_blobs` (the candidate's blob, null = absent),
   `expected_blobs`, the script's identity + sha256, `candidate_sha`,
   `version`, `plan_digest`, `release_mode`, and the changelog BASELINE
   (`changelog`: path — the RESOLVED file, its exact tracked spelling —,
   base blob, base sha256). Refused: a script that
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
   **Its blobs are pinned** (K-117) before it is stored: the expected blobs
   are reachable from nothing until the release commit exists, so `git gc`
   could prune them. ONE ref per proposal,
   `refs/dct/release-proposals/<project>/<proposal id>` (`effects.pin_ref`:
   `<project>` = `s-<slug>`, or `h-<hash>` for a slug that is not safe in a
   ref name — several dct projects may share a repository; the proposal's
   `id`, not the version, which a second prepare reuses, and not a content
   hash, which two proposals may share), pointing at a TREE holding
   `expected/<path>`, `base/<path>` and `changelog-base/<path>` — a tree,
   not a commit, so `git log --all` and the proof layer never see it and
   no plan changes. The ref lives until the release is `completed` or
   `abandoned`, or the pending proposal is replaced by a new prepare; only
   THAT ref is removed — a blob another proposal's ref also holds stays
   reachable. `apply` re-creates a missing pin of the approved proposal. A
   pending proposal nobody applies keeps its pin until the next prepare.
   Never pushed (the push names explicit refspecs).
6. **Outside git** (a project CONFIRMED outside git — D5, an explicit
   `without_git` proposal, never a Git proof): the same on a temporary COPY
   of the project directory (bounded: `COPY_MAX_FILES` 20000 files /
   `COPY_MAX_BYTES` 256 MiB, special files refused); every file of the
   directory must be byte-identical afterwards; blob ids are SHA-256 of the
   content, kept in dct's release blob store
   (`<data dir>/release-blobs`, `dct.paths.release_store_dir`) — the
   approved version files AND the changelog baseline (content + sha256, or
   "absent"), so a resume can rebuild and verify F (K-78). The store has
   the same lifetime rule as the pin (K-117): when a release is completed
   or abandoned, or a pending proposal is replaced, its blobs are deleted —
   except those any other LIVE proposal still names
   (`core.releases.live_proposals`: every project's pending proposal and
   every unfinished record of this database; identical files of two
   projects are ONE blob). Cleaning is best effort (a warning, never a
   failed release). Limit: a blob another project is storing at that very
   moment can be removed before its proposal is recorded — its `apply`
   then refuses with "prepare again", never with wrong content.

Result `details`: `{proposal, proposal_sha256, plan_digest, entry_ids,
contract: {confirmed, proposed, change}, sync: null | {exit_code,
timed_out, output, output_bytes, output_dropped_bytes, stopped_processes,
duration_seconds}, checks: [{path, check: "version_field", value} | {path,
check: "lines_with_version", changed_lines}], push_target}` (step `proposal`
carries `pin`, the ref that keeps the blobs). `contract.change` is true
for a first release (`confirmed` null) and for a changed set — approving
`P` approves that contract too; it is recorded with the stamp.

### The preview (`dct release prepare --preview`)

The one-dialog release: ONE call computes everything the owner approves on
one screen, and the approval is bound to that CONTENT. `prepare --preview`
is an effects-layer mode of `prepare` — unlike the read-only previews of
`takeover` and `version-files` it STORES something: the project's preview
slot (`projects.release_preview`) and a pin ref. It takes the operation
lock (action `prepare`). Exit 0, 1 on a refusal, the script's own code on a
failing sync (124 on a timeout), 75 on a database conflict (a busy
operation lock is a refusal: exit 1, `details.lock`).

**Flags.** `--plan-digest` cannot be combined with `--preview` or
`--discard-preview` (argparse refuses); the `--entries` / `--no-entries`
choice is optional only with `--preview` (default: every id proven at the
variant's candidate — outside git every unreleased entry). `--attest`,
`--message-file`, `--include-untracked`, the push flags and
`--no-variants` belong to `--preview` (refused without it).
`--discard-preview` takes no flag besides `-P` and `--json`. Paths of
`--include-untracked` are relative to the current directory, as `git
status` shows them there (or absolute); one outside the repository is
`bad_untracked` with reason `outside`.

**What it computes, in order** (`effects.preview_release`):

1. **Pre-checks** of a new release: executor `dct` (`executor`), a
   registered path, no unfinished record (`unfinished`), git ≥ 2.36;
   `--attest` with `--no-entries` (`attest_without_entries: {attest}` — an
   attestation commit marks entries as done so they are released; a
   release of no entry has no such screen); for an EXPLICIT version
   (`--version`, a bump flag): `version_invalid: {version}` when it is no
   version, nothing to prepare (`NothingToPrepare`) when it has a record
   (`version_taken: {version, status}`, the record as `release`) or is
   lower than the current version (`version_lower: {version, current}`) —
   `prepare` refuses with the same two keys —, no local tag of it
   (`tags`). The keys carry what a "pick another version" sentence needs.
2. **S0, read-only** (git): HEAD H0 and the ref HEAD names — read again
   under the lock: a HEAD that moved (or a branch switched) since the
   checkout was located refuses (`checkout_changed`) —, an operation in
   progress, `git status --porcelain=v2 -z --untracked-files=all
   --no-renames` (never an index refresh: the user's `.git/index` stays
   byte-identical). The staging scope Pₛ = every tracked changed path
   (staged, unstaged, deleted) + the untracked release artifacts (the
   changelog file and the version-file contract; `auto: true`) + every
   `--include-untracked` path. Refusals: `in_progress`, `conflicted`,
   `intent_to_add` (`git add -N`), `submodule` (a changed gitlink or a
   dirty submodule), `bad_untracked` (`[{path, reason: "tracked" \|
   "ignored" \| "missing" \| "outside"}]`), `hidden_index_flags`
   (skip-worktree / assume-unchanged on a path of Pₛ), `filtered_paths` (a path of Pₛ with
   a `filter` attribute — LFS, clean / smudge), `secret_like` (file names
   `.env`, `.env.*` except `*.example` / `*.sample`, `*.pem`, `*.key`,
   `id_rsa*`, `*.p12`, `credentials*.json` — `plan.secret_like`, the ONE
   secret-path predicate, also `dct clog candidates`' `secret_like`; a name
   heuristic, not a content scanner), `message_required: {paths,
   untracked}` (a dirty tree without `--message-file`), `nothing_to_commit`
   (`--message-file` on a clean tree), `message_file` (unreadable or not
   UTF-8).
3. **T1** — the exact future tree of the work-in-progress commit, built in
   a TEMPORARY `GIT_INDEX_FILE`: `read-tree H0`, `git add -A -- <Pₛ>`
   (literal pathspecs), `write-tree`. A partially staged path is committed
   WHOLE (its work-tree content). A clean tree: no work, T1 = tree(H0).
   T1 == tree(H0) on a dirty tree — the index differs from HEAD but the
   files are back at HEAD (staged content taken back, `git rm --cached`,
   `--chmod`) — is nothing to commit, never an empty work commit:
   `index_only: {paths}` (the paths whose index entry differs from H0's).
   This check runs BEFORE `message_required`: an index-only difference
   without `--message-file` is refused with `index_only`, never first
   asked for a message.
   A change too large for the slot (`PREVIEW_MAX_BYTES`: every path of Pₛ
   is stored with its index entry, its work-tree digest and once per
   variant in the view) is refused here, BEFORE any sync:
   `preview_too_large: {bytes (a lower bound), max, paths}` — the same key
   when the stored slot still exceeds it.
4. **The message** — `--message-file` cleaned exactly as `git commit -F`
   cleans it (`commit.cleanup`: `strip` removes `#` lines; default,
   `whitespace`, `scissors` only whitespace; `verbatim` nothing);
   `message_empty` when the subject is empty. The subject is git's own
   (`%s`: the first paragraph, its lines right-trimmed and joined with
   spaces — `proof.message_subject`), the one the real commit shows and
   apply compares. `proves` = the ids the proof parser reads from the
   synthetic commit; each must be an unreleased entry of the project (`message_foreign_ids`: `[{id, reason, version,
   commits}]`). `uncertain` = the uncertain candidates of `dct clog
   candidates` the message does not name (display only).
5. **Synthetic commits** — `commit-tree`, never referenced by a ref, never
   signed, no hook, the identity `dct preview`, dated NOW (an approximate
   `--since` range includes them): C\* = T1 on H0 with the cleaned message
   (H0 itself on a clean tree); with `--attest IDS` A\* = an empty
   attestation commit on C\* (the message of `dct release attest`). Each
   attested id must be unproven at C\* (`not_unproven`) and neither
   `reverted` nor `revert_unconfirmed` (`not_attestable: [{id, reason}]`);
   outside git `--attest`, `--message-file` and `--include-untracked` are
   refused (`no_git`).
6. **The push target and the remote** — `push_target` (the flags, else the
   upstream; without one the view's push is `{mode: "local", reason:
   "no_upstream" \| "detached" \| "several_urls" \| "url_not_literal"}`
   and a warning — `upstream_invalid` when the branch HAS an upstream
   (`branch.<b>.remote`, not `.`) that cannot be pushed to: its remote is
   missing or its branch name invalid; `--no-push` → reason `requested`),
   then ONE `git
   ls-remote` of its URL: the remote branch must be contained in H0
   (`remote_ahead`), a remote tag of an explicit V refuses
   (`remote_tag_exists`), a remote tag of a SUGGESTED V moves it like a
   local one. An unreachable remote is a warning (`remote.checked: false`,
   `remote.error`) — apply checks it again.
7. **The shown variant** at K\* (A\* with `--attest`, else C\*): the
   changelog file resolved and checked at K\* (`changelog_file` — a
   tracked, dirty changelog in Pₛ is part of the work; an included
   untracked second changelog makes the resolution ambiguous), the plan
   pinned to K\*, the selection (`unproven` for an `--entries` id the plan
   does not prove), the version suggestion for the selection
   ([The version suggestion](#the-version-suggestion); taken = local tags,
   records, released entries and the remote's tags; `version_unresolved`
   when no version results and none was given), the changelog composition
   dated today (UTC), no local tag of V, the isolated version sync at K\*
   (exactly as [Prepare](#prepare-dct-release-prepare-k-76) — `SyncFailed`,
   `checkout_changed`, the version-file contract), the consent view and C.
8. **The "with all N" variant** (unless `--no-variants`): the waiting set
   Wt = the ids unproven at K\*. Offered only in git with 1 ≤ |Wt| ≤ 4 and
   no id `reverted` / `revert_unconfirmed`; otherwise
   `all_unavailable` = `no_git` \| `not_requested` \| `none_waiting` \|
   `reverted` \| `too_many`. It attests Wt ∪ `--attest` in an empty
   commit on C\* (the work commit's message is never extended), selects
   shown ∪ Wt, has its own suggestion (for an explicit V: that V), section
   and C, and `display.delta = {version, adds: Wt, files: its checks}`.
   The sync runs again only when its V differs from the shown one.
9. **The checkout is verified unchanged** over the whole preview (HEAD,
   refs, index, status, untracked bytes, the bytes of already-modified
   tracked files — `checkout_changed`), the blobs of every variant are
   pinned, and the slot is stored.

**Every refusal stores nothing**: the earlier slot and its pin stay.

#### The consent view

`consent_view` (and `core.releases.validate_consent_view`, closed key sets,
every key required) — `C = sha256(canonical JSON)`
(`core.releases.consent_view_digest`):

```
{ "format": 1, "project", "executor": "dct", "release_mode": "git" | "without_git",
  "checkout_root": <normalized repository root> | null, "project_root",
  "version": {"current", "target"},
  "base": {"head": H0 | null, "ref": "refs/heads/<b>" | null},
  "work": null | {"tree": T1, "subject", "message_sha256", "proves": [ids],
                  "paths": {path: {"mode": "100644" | "100755" | "120000", "blob"} | null}},
  "attest": null | {"ids": [ids]},
  "candidate_tree": T1 | null,
  "range": null | {"base_tag", "base_sha", "approximate", "since",
                   "notice_code": null | "base_not_current" | "approximate"},
  "entries": {"selected": [ids], "state": {id: entry_state_digest} (EVERY unreleased entry),
              "waiting": {id: unproven reason}, "proof": α(the snapshot's proof)},
  "changelog": {"managed": bool, "path", "base_blob", "composed_sha256",
                "section_sha256", "date" (UTC), "pr_refs"},
  "version_files": {"paths", "base_blobs", "expected_blobs", "script": null | {"identity", "sha256"},
                    "contract": {"confirmed", "change"}},
  "push": null | {"mode": "push", "remote", "branch", "url", "tag"} | {"mode": "local", "reason"} }
```

The local reasons (`core.releases.LOCAL_PUSH_REASONS`): `requested`,
`no_upstream`, `upstream_invalid`, `detached`, `several_urls`,
`url_not_literal`.

α replaces every full or 12-character id of the synthetic work commit with
`"@work"` and of the synthetic attestation commit with `"@attest"` (in
`commits`, `merge_commit`, `associations`) — the real commits apply makes
have other ids but the same role. `composed_sha256` = sha256 of the
composed changelog in its work-tree form (`_composed_forms`);
`section_sha256` = sha256 of `display.section` — the screen shows exactly
the section C binds. `changelog` is ALWAYS an object: the section, its UTC
`date` and the PR-reference render (`pr_refs`) are bound whether or not
dct writes a changelog file — with the file off (`managed: false`) `path`,
`base_blob` and `composed_sha256` are null, and the release's record and
the exported changelog still carry exactly that date and render, so the
screen never shows a date the consent does not bind. A slot stored by an
older dct with `changelog: null` no longer derives equal — its apply is
`consent_mismatch` and the screen is shown again. Outside git `base_blob` / `base_blobs` /
`expected_blobs` are the sha256 values of dct's release blob store. Not in
C: the preview id and time, the pin, the index and work-tree state, the
untracked files left out, the sync's output, the checks, the proposed
contract, warnings, the remote's state, the entries' branch / worktree.

#### The preview slot and its pin

`core.releases.set_preview` stores (`validate_preview`, at most
`PREVIEW_MAX_BYTES` = 256 KiB of canonical JSON — refused, never
truncated) in ONE transaction that also clears the pending proposal:

```
{"id": ULID, "created_at", "base": {"head", "ref", "checkout_root"},
 "work": null | {"index_before": {path: {"mode", "blob", "stage"} | null},
                 "paths_state": {path: sha256 of the work-tree bytes | null},
                 "untracked_included": [path], "message"},
 "variants": {C: {"name": "shown" | "all", "consent": <view>,
                  "proposal_content": <proposal minus id / candidate_sha / plan_digest>}},
 "pin": "refs/dct/release-previews/<project>/<id>" | null,
 "progress": {"work_commit": null, "attest_commit": null, "consent": null}}
```

A preview and a `prepare` replace each other's slot (`set_pending_proposal`
clears the preview), and the resources of what was replaced are released
after the commit. The pin is a TREE (like a proposal's pin) holding, for
EACH variant under its own name, `<variant>/expected/<path>`,
`<variant>/base/<path>`, `<variant>/changelog-base/<path>` (`shown/…`,
`all/…` — two variants of other versions hold other blobs for the same
path, and neither may push the other's out) and `work` = T1, so `git gc`
keeps every variant appliable and `git log --all` never shows them; `apply
--consent` re-creates a lost pin with the same layout; outside git the blobs are in the release blob store and
`core.releases.live_proposals` lists every variant's content, so another
proposal's cleanup keeps them. `record_preview_progress` records the commits
`apply --consent` makes and, with the first of them, `consent` — the C of
the variant they were made under (one of the slot's variants), so a
progress both variants share (a work commit of a screen that offered
"Release" and "Release with all N") still names the screen the owner
approved. Each field is write-once; another preview id is refused. An
older dct ignores the column.

**Result** — outcome `preview`, `version` = the shown variant's V, steps
`preview_tree` then `sync` (one more `sync` for an "all" variant with
another V), and `details`:

```
{"preview_id",
 "base": {"head", "ref", "branch", "detached"},
 "work": null | {"paths": [{"path", "change": "added" | "modified" | "deleted" | "mode",
                            "staged": "all" | "part" | "none", "auto"}],
                 "untracked_included", "untracked_left", "message", "subject",
                 "proves", "uncertain", "tree"},
 "remote": {"checked", "url", "tags" (remote tags that moved a suggestion), "branch_ahead", "error"},
 "left_out": [{"path", "secret_like"}],
 # display only, in NO consent view (C does not change with it): every
 # untracked, not ignored file the release does not commit — also when
 # "work" is null (a clean tree with new files) — so the screen lists it as
 # left out; secret_like = the secret-path label (.env, keys — such a file is
 # never offered for --include-untracked). Nothing is ever `git add`ed.
 "variants": [{"name", "consent_digest", "consent", "version_only", "suggested",
               "display": {"entries": {"selected": [{id, category, entry, component}],
                                       "waiting": [{id, category, entry, reason, reverted_by?}]},
                           "section", "checks", "contract": {confirmed, proposed, change},
                           "sync", "range": {text, approximate, notice}, "delta", "warnings"}}],
 # range.text never shows a synthetic commit: its id is replaced by
 # "HEAD + your uncommitted changes" / "+ the attestation commit"
 "all_unavailable"}
```

`--discard-preview` (the owner said Stop) clears the slot, removes the pin
and releases the store blobs no live proposal names: outcome `noop`,
`details.discarded` = the preview id or null.

### Apply --consent (the one-dialog release)

`dct release apply --consent C --confirm [-P slug] [--json]`
(`effects.apply_consent`) releases EXACTLY the consent view C of the
project's preview slot — the screen the owner approved. The owner never
sees a digest: the dialog passes the `consent_digest` of the variant whose
content the screen rendered, and nothing else. `--consent` takes none of
the flags that bind a release on the D/P path (`--version`, `--patch` /
`--minor` / `--major`, `--entries`, `--no-entries`, `--plan-digest`,
`--proposal-digest`, `--push-remote`, `--push-branch`, `--push-url`,
`--no-push`) — the stored view binds them all; any of them is refused with
`consent_flag_conflict: [flags]` (exit 1, a JSON document). A flag counts
when it is PRESENT on the command line — `--version ""` is present, an
empty value never reads as "no flag" (the same rule for the flags
`--discard-preview` refuses and the `--preview`-only flags). The `clog
release` alias and the MCP tools do not offer it. The D/P path (`plan` →
`prepare` → `apply --plan-digest --proposal-digest`) is unchanged.

**Order** — everything under ONE operation lock (action `apply`):

1. **The slot**: a preview exists (`no_preview` — never previewed,
   discarded by Stop, replaced by a `prepare`, or consumed by the release
   it stamped) and C is one of its variants (`consent_unknown:
   {preview_id, variants: [{name, version}]}` — no digests). C binds the
   CONTENT, not the preview id: an identical newer preview has the same C,
   and the release is bound to that newer preview. Once a commit was made
   under one variant (`progress.consent`), the other variant's C is refused
   (`consent_in_progress: {preview_id, variant, version, progress}`) —
   nothing done. Then the pre-checks of
   a new release (`executor`, `unfinished`, git ≥ 2.36, an operation in
   progress), with the project's settings read again under the lock.
2. **The base and adoption** — `plan.consent_adoption`, the ONE predicate
   the plan's `pending_consent` uses too (so the plan offers "Finish"
   exactly when apply finishes): the ref HEAD names must be the preview's
   (`base_moved` with `why: "branch_switched"` — a switch, an attach or a
   detach, also on the same commit: another context; the refusal names
   the way back, `git switch <branch>` — for a detached preview `git switch
   --detach <the recorded A, else W, else H0>` (`plan.switch_back_command`),
   and dct switches nothing itself)
   and HEAD must be H0 or the commits this consent approves —
   decided by the commits themselves (`plan.adoptable_chain`; the
   recorded `progress` only says which W dct made): a work commit W′ with
   `parents == [H0]`, `tree == work.tree`, `subject == work.subject`
   (git's `%s`) and proven ids `== work.proves`; a W′ dct did NOT make
   (not the recorded `progress.work_commit` — a plain `git commit`) must
   also carry the approved message (`work.message_sha256` equals the
   SHA-256 of its message as stored or as `git stripspace` cleans it), so a
   body the screen never showed is never adopted, while a hook's line in
   the message of the W dct made (a `Signed-off-by:`) is; and an
   attestation commit A′ on W′ (or
   on H0 without work) with an empty tree delta, `ATTEST_SUBJECT` and
   proven ids `== attest.ids` exactly — an A′ dct did NOT make (not the
   recorded `progress.attest_commit`) must also carry exactly
   `attest_message(attest.ids)` (as stored or as `git stripspace` cleans
   it). Anything else: `base_moved:
   {expected_parent, head, why: "head_moved"}` (`why: "other_checkout"`
   for a preview of another checkout or release mode). Every commit the
   slot's `progress` records must be the one HEAD's chain names: one reset
   away (still there, or pruned by `git gc`) refuses with `why:
   "progress_lost"`, pointing to `--discard-preview` — never a second W or
   A under the same approval (progress is write-once). The approved blobs
   must still exist (`missing_blobs` — e.g. pruned after the preview's pin
   was lost; a lost pin is re-created when they do).
3. **The work state** (a variant with work, W not made yet — and a variant
   WITHOUT work whose attestation is still to make, with an empty Pₛ: the
   tree must still be clean): T1′ rebuilt
   from the work tree exactly as the preview built it must be T1, no
   tracked path outside Pₛ may be dirty (nor a new untracked release
   artifact), every included untracked file must exist — else
   `work_changed: {tree_expected, tree_now, paths_changed, new_dirty,
   missing_untracked}`; the user's index entry of each path of Pₛ must be
   the one it held at the preview, H0's or T1's — else `index_changed:
   {paths}`. Content equality, not stat equality: a touched file, a path
   unstaged back to H0 or staged as T1, an untracked file that appeared
   later do not refuse (the new file is left out).
4. **The pre-commit derivation (R9a)** — the view derived NOW on
   synthetic commits (C\*′ = T1 on H0, A\*′ on it — or the adopted W / A),
   with the stored version-file content (the script never runs in apply):
   the plan pinned to K\*′ with the approved version and selection, the
   entries' states and proof (α), the changelog composed now (today's UTC
   date, the render settings, F's base), the contract, the script and the
   base blobs at K\*′, the push target RE-VALIDATED (`push_target` with
   the approved remote, branch and URL — never derived again; a refusal
   makes the push differ, `details.push_error`). Not equal to C →
   `consent_mismatch: {changed: [top-level keys], diff: {key: {approved,
   now}}}` (each side narrowed to its differing sub-keys, at most 500
   characters of canonical JSON) — NOTHING made. With a commit to make,
   the remote is read once here (`remote_tags`, `remote_branch`). The
   commit identity is resolved here ALWAYS in git — also when nothing is
   left to commit (a clean shown variant, an adopted W / A): the release
   commit needs it, so an unknown identity is refused before the stamp
   ("refused before the stamp"), never after it with an unfinished record.
5. **The work-in-progress commit W** — `_hook_commit` of T1 on H0 with the
   approved message: the repository's hooks on a temporary index; a hook
   that changes the content (the temporary index, or the work tree's files
   of Pₛ — a formatter) or any other file the work-tree check watches
   (against a reading taken before the hooks: a tracked path outside Pₛ, a
   conflicted path, an untracked release artifact — not an untracked log
   file) → `hook_changed_tree: {paths}`, one that changes
   the subject or the proven ids → `message_contract: {subject,
   approved_ids, proven_ids}`, a refusing hook → `hook`, `git_error` — all
   with NOTHING published (no ref moved, the user's index untouched, dct
   restores no file). A line a hook adds that changes neither (a
   `Signed-off-by:` trailer) is fine. W is recorded in the slot's
   `progress` (`record_preview_progress`, with `consent` = C) right after
   its publication — before the user's post-commit hook runs
   (`_hook_commit`'s `on_published`), so before any user code runs again
   and before the user's real index is synced to it for Pₛ (`_sync_real_index` with base H0: an entry that is
   H0's, T1's or what the index held at the preview is updated, the
   commit's is kept, anything else — staging that appeared meanwhile — is
   `foreign_staged`, PRESERVED; W stays, a re-run syncs again). Before
   that sync the step-2 predicate runs again (`consent_adoption` with the
   commits made): a post-commit hook or another process that switched or
   detached HEAD — even on W itself — is refused (`base_moved`, `why:
   "branch_switched"`, `made` names W, nothing released), so neither A nor
   the release commit lands on another ref. Steps
   `work_commit` `{step, result: "done" | "adopted", commit}`.
6. **The attestation commit A** (a variant with `attest`): on a W made
   or adopted, the tree must first still be clean (`work_changed` with the
   path in `new_dirty` — an edit made after an interrupted run, nothing
   more committed); then the internals
   of `dct release attest` (`_attest_commit`: nothing staged, K-107 exact
   ids, and the same work-tree check as W — a hook that changes a file →
   `hook_changed_tree`, nothing published) on W or H0, recorded in
   `progress` (with `consent` when it is the first commit) right after its
   publication, before the post-commit hook; step
   `attest`. The work
   commit's message is never extended with waiting ids.
7. **The post-commit derivation (R9)** at the real candidate K (A, W or
   H0): the step-2 predicate again — the ref HEAD names the preview's,
   HEAD is K and the chain the one made (`base_moved`, `why:
   "branch_switched"` / `"head_moved"`; it runs once more right before
   the stamp); the tree must be clean
   (`_clean_tree`); the project's settings are read AGAIN (every
   derivation reads them fresh — a changelog setting changed while the
   repository's hooks ran W or A is seen, never the copy read at step 1);
   the view derived as in step 4 — with `work` / `attest`
   the approved ones because the predicates of step 2 hold for the commits
   made — must equal C (`consent_mismatch` with `made`: W / A stay, dct
   never resets them, the slot keeps `progress`); the remote is read
   (`remote_tags`, `remote_branch`).
8. **Rebind and stamp** — the stored content becomes the proposal `{…,
   id: preview_id, candidate_sha: K, plan_digest: D(K)}`, pinned like a
   prepared proposal; the consent `{version, entry_ids, candidate_sha: K,
   proposal_sha256: P, render: {pr_refs: <the view's>}, push_target,
   executor: "dct", content_sha256: C, changelog_sha256: <the view's
   composed_sha256, when dct writes the file>}`; `release_entries(...,
   consume_preview={preview_id, consent_sha256: C, proposal},
   expected_entries, expected_release_date=<the view's date — with or
   without a changelog file>, expected_changelog_settings=<the settings
   that chose the file at step 7: core.changelog_file.choice_settings
   {managed, explicit, path}>)`. Inside the stamp transaction: the slot
   must still be that preview holding C (`preview_replaced` — a `prepare`
   or another preview replaced it), the live unreleased entries the
   approved ones (`consent_mismatch`, `changed: ["entries"]`, `diff.entries
   = {approved: {state}, now: {state}}` — an entry edited, added or
   released meanwhile), the UTC date the approved one (`date_changed:
   {approved, now}` — a release across midnight), the render settings and
   the changelog file settings the approved ones (`consent_mismatch`,
   `changed: ["changelog"]` — `manages_changelog`, the explicit marker and,
   when explicit, the path: any change of them refuses, also one that
   would choose the same file); then the record, the learned contract, and
   the slot cleared — one transaction. These checks are race-safe: the
   setters of these settings (`set_manages_changelog`,
   `set_changelog_path`, `reset_changelog_path`, `set_changelog_pr_refs`)
   take the stamp's project lock (`project_transaction(write=True)`, like
   the executor setter, K-106), so a change either precedes the comparison
   (the stamp refuses `consent_mismatch`) or waits until the stamp
   committed; any other writer of the project row committed after the
   stamp's snapshot fails it on PostgreSQL (SQLSTATE 40001 — a conflict,
   nothing written, safe to re-run), and SQLite serializes every writer. A
   refusal there stamps nothing and creates no record — but the W / A
   made before it STAY and are reported (`made`, "commit made, release
   not started"): "nothing released" never means "nothing made". The
   preview's pin and the store blobs no live proposal names are released,
   and the release goes on exactly as `apply`'s steps 3-7 (`_continue`).

**What may differ between the preview and the release** (E3): the ids of
W and A (dates, identity, signature) — the view names them `@work` /
`@attest`; lines a hook adds to a message without changing its subject or
proven ids; the plan digest, P, the proposal id and the pin refs; the
sync's output and the temporary paths (it ran once, in the preview); the
untracked files left out; the entries' `branch` / `worktree`; the
remote's state, which is read again, not compared. EVERYTHING ELSE in the
consent view must be equal — the entries' text, order and states, the
proof, `pr_refs`, the changelog's base and composition, the date (UTC),
the version-file base and expected blobs, the script's identity and
sha256, the contract, the push target, the executor.

**Result** — outcome `completed`; `details` = apply's (`requested_version,
previous_version, bump, entry_ids, mode, released, current_version,
push_target`) plus `{consent_digest, variant ("shown" | "all"), made:
{work_commit, attest_commit}, candidate_sha, plan_digest,
proposal_sha256}`; `steps` gain `work_commit` and `attest`
(`effects.EFFECT_STEPS` lists `work_commit`).

**Refusals** — exit 1 (a busy operation lock too, `details.lock`; 75 for
a database conflict, outcome `conflict`); EVERY one
carries `details.made: {work_commit, attest_commit}` (made or adopted by
this run); when one is set and nothing was stamped, the message says
"commit made, release not started" — the commit stays.

| code | condition | details |
| --- | --- | --- |
| R1 | `--consent` with a binding flag | `consent_flag_conflict: [flags]` |
| R2 | the slot is empty | `no_preview` |
| R3 | C is not a variant of the slot | `consent_unknown: {preview_id, variants}` |
| — | a commit was made under the slot's other variant | `consent_in_progress: {preview_id, variant, version, progress}` |
| — | executor `own`, an unfinished record, git < 2.36, an operation in progress | `executor`, `unfinished`, `git_version`, `in_progress` |
| R4 | another branch / a detached HEAD (also a switch on the same commit — checked before the commits, after W, after A and right before the stamp); a HEAD that is neither H0 nor an adoptable W / A; another checkout; a recorded W / A reset away (or pruned) | `base_moved: {expected_parent, head, why: "branch_switched" \| "head_moved" \| "other_checkout" \| "progress_lost"}` |
| R10 | approved blobs (or T1) pruned | `missing_blobs` |
| R5 | the work tree no longer gives T1, a path outside Pₛ dirty, an included file gone; before A (without work, or on a W made / adopted): the tree is no longer clean | `work_changed: {tree_expected, tree_now, paths_changed, new_dirty, missing_untracked}` |
| R6 | an index entry of Pₛ is none of: the preview's, H0's, T1's | `index_changed: {paths}` |
| R9a | the view on synthetic commits ≠ C (nothing made) | `consent_mismatch: {changed, diff}` (+ `push_error`) |
| — | git cannot name the commit identity (refused before the stamp, also with nothing to commit) | `git_error` |
| — | the remote holds a tag of V / its branch is not contained | `remote_tags` / `remote_branch` (+ `push_target`) |
| — | a refusing hook (W or A) | `hook`, `git_error`, `head`, `expected_parent` |
| R7 | a hook changed the content (index or files) or another watched file of the work tree (W or A) | `hook_changed_tree: {paths}` (nothing published) |
| R8 | a hook changed W's subject or proven ids | `message_contract: {subject, approved_ids, proven_ids}` (nothing published) |
| — | foreign staging in the index sync after W | `foreign_staged` (+ `made`; remove it, run the same command again) |
| R9 | the view at the real K ≠ C (the project's settings read again and the changelog file chosen again from them — in git and outside it, K-44); inside the stamp transaction: an entry changed since the step-7 derivation, the render or the changelog file settings changed | `consent_mismatch` (+ `made`; `changed: ["entries"]` / `["changelog"]` from the stamp) |
| R11 | the slot replaced before the stamp | `preview_replaced: {preview_id, now}` |
| R12 | the UTC date at the stamp ≠ the approved date | `date_changed: {approved, now}` |

After R4-R7 and R9-R12 the dialog previews again and shows the new screen,
headed by what changed and naming the commits `made` already holds; it
never re-applies silently. R8 (`message_contract`) stops with the hook's
output — a new screen would meet the same commit-msg hook; the owner
commits the changes with /commit. The preview after a commit was made
drops the `--attest` ids a made A already marks (they are proven now:
`not_unproven`) and carries a newly drafted message whenever the work tree
has uncommitted changes again (`work_changed` with `new_dirty` on a made
W; a message on a clean tree is refused); after the Finish question in a
new conversation its flags come from `pending_consent.applied` (`version`,
`entries`, `attest`) — the push target is not recorded there, so the new
screen shows the default target again. A refusing hook stops the dialog
with the hook's output (never `--no-verify`). A database conflict (exit
75) or a network error BEFORE the stamp (`git_error` + `url` from reading
the push destination, without `hook` / `rewrite_rule` / `remote_name`; the
refusal carries no `release` record) gets ONE automatic re-run of the same
`apply --consent C` — adoption (step 2) makes it safe —, then a plain
sentence and stop. The same network error AFTER the stamp (the refusal
carries the `release` record, status `tagged`) is never re-run as an
apply: the release is unfinished and `dct release resume` finishes it once
the remote is reachable. A busy operation lock (exit 1, `details.lock` +
`holder`) is never re-run by the dialog: the owner says when the other
command has ended. The CLI itself retries nothing; that single re-run is
the dialog's. A version the preview refuses (`version_lower`,
`version_taken`, `version_invalid`, `remote_tag_exists`) leads back to the
version choice — the previous screen, or the version question — and a new
preview; `index_only` stops with the owner's next step (unstage, or make
the change), the dialog never unstages. Every other refusal is one plain
sentence with its next step, the `details` keys only under the message's
"Technical details" line (`/release` skill, "When a command refuses").

**Resume** — before the stamp there is no record: "resume" is running the
SAME `apply --consent C` again. It is idempotent by adoption (step 2): no
second W or A; on an adopted W the index sync runs again (the stuck index
of a run killed between W and the sync, or one refused for foreign
staging the owner removed since). `dct release plan` exposes the slot as
`pending_consent`; its `consent_digest` is non-null only once a commit was
made under that consent — `progress.consent`, written with the commit
right after its publication and before the post-commit hook, so a kill
anywhere after W's publication (in the user's post-commit hook, or before
the index sync) leads to the re-run that syncs the index, never to a fresh
preview of a stale index (the dialog then asks one question: finish the
release you approved, or Stop). A recorded commit that no longer exists
gives no digest and a plan warning, and the re-run refuses (`why:
"progress_lost"`); preview again or discard the preview. After the stamp the record holds an
ordinary consent (`candidate_sha: K`, `content_sha256: C`, and
`changelog_sha256` when dct writes the file) and `dct release resume`
finishes it like any other — it never re-derives C. The file write right
after the stamp and EVERY resume recompose F and compare it with
`changelog_sha256` before anything is written: at the first changelog file
ever (bootstrap) F is the whole released history exported from the
database, so history imported (`import_released_entry`) between the stamp
and the write would change it — and after an interruption that window
lasts until the resume, however long. Another composition refuses with
`changelog_changed: {path, approved, now}`, nothing written; restore that
history and resume, or abandon the release. A consent without
`changelog_sha256` (recorded before it, the plan / prepare path, no
changelog file) is not checked.

### The changelog file (#876)

ONE resolver — `core.changelog_file.resolve_changelog` — decides which file
F is, for the plan (`changelog_file`), prepare, apply, resume, reconstruct,
takeover and the CLI (`dct project changelog-path <slug>` shows it, `dct
project add` reports it). It reads the TRACKED files of the candidate
(`git ls-tree`, case-sensitive — never the work tree), or outside git the
project directory. Whether the stored `projects.changelog_path` is the
owner's choice is the marker `projects.changelog_path_explicit` (K-14),
never the value: `dct project changelog-path <slug> <path>` always marks
the choice explicit — also exactly `changelog.md` —, `dct project
changelog-path <slug> --detect` clears it (the path goes back to the
default `changelog.md`, used only when detection finds nothing). The
bootstrap marks every stored path other than `changelog.md` whose marker
is unset explicit — convergently, on EVERY bootstrap (K-22: it also heals
a path an older dct without the marker stores later; guarded by a read, so
a database in the right state is never written) — and leaves the
historical default NOT explicit: whether it was ever chosen is unknown, so
it means "detect", and a `--detect` reset is never re-marked.

1. `manages_changelog` off (`dct project changelog <slug> off`) → no
   changelog file; nothing below is looked at.
2. An explicit path CHOOSES the file, it waives no check (K-10): a tracked
   case-variant of it (any component) → `case_variant`, refused — never
   rewritten silently; a name without a Markdown extension (`.md` /
   `.markdown`) → `not_markdown`, whether the file exists or not; a
   stored path leaving the project root (written past the setter, e.g. by
   another client) → `outside_project`; otherwise that file, existing or
   new (`stored`), after the path check below.
3. Detection among the regular files and symlinks DIRECTLY in the project
   root (a monorepo subproject: its own directory, never the repository
   root) whose name, compared case-insensitively (NFC + casefold), is
   `CHANGELOG`, `CHANGES`, `HISTORY`, `NEWS` or `RELEASES` with `.md` /
   `.markdown` (Markdown) or no extension / `.rst` / `.txt` / `.adoc`:
   exactly one Markdown file → F in its exact tracked spelling
   (`detected`, e.g. `CHANGELOG.md` — dct's own `changelog.md` too), or
   `symlink` when that file is a link; several Markdown files — names
   differing only in case included, `changelog.md` NOT privileged (K-13) →
   `ambiguous`, never guessed (`dct project changelog-path <slug> <path>`
   chooses); only non-Markdown files → `not_markdown`: dct writes Markdown
   only, so it never writes into that file and never creates a second
   changelog beside it — the next steps are: convert the existing
   changelog (K-15: rewrite its content in the Keep a Changelog layout AND
   give it a Markdown extension, e.g. `git mv HISTORY.rst HISTORY.md` —
   `git mv` alone does not convert the format —, commit it as work in
   progress and choose it with `dct project changelog-path <slug>
   HISTORY.md`), give an explicit Markdown path for a new file (`dct
   project changelog-path <slug> CHANGELOG.md`), or turn the changelog file
   off (`dct project changelog <slug> off`); none at all → `changelog.md`,
   a new file (`default`). Outside git the conversion names no git step
   and no commit (rename the file yourself, e.g. `mv HISTORY.rst
   HISTORY.md`, K-24).

EVERY component of the resolved path is checked (K-19), in the candidate's
tree (outside git: on disk): a symbolic link anywhere on the path —
the file itself or one of its directories — → `symlink`; a regular file or
a submodule where a directory is expected → `not_a_directory`; a directory
or a submodule where the file is expected → `not_a_file` (each problem
carries `component`: `file` \| `directory`). In git the DIRECTORIES of the
path are checked on disk too (`check_work_tree`): an ignored or untracked
link or file where the candidate holds a directory or nothing →
`symlink` / `not_a_directory` with `baseline: "work_tree"` (move it away).
On a case-insensitive file system the file itself is checked the same way
(K-28): where the candidate holds no F but TRACKS a directory, link or
submodule whose name differs only in case (a tracked `CHANGELOG.md/` while
F is `changelog.md`), the disk opens that entry at F's path → `not_a_file`
(a link: `symlink`) with `component: "file"` and `baseline: "work_tree"` —
never `not_baseline` "move it away": the tracked entry is no thing to move.
The ways of `not_a_directory`, `not_a_file` and `outside_project` are
choosing another path, going back to detection when it settles a file that
also passes the work-tree check (K-28 — in git `check_work_tree` runs on
the detected path before `--detect` is offered), or turning the changelog
file off.

A symlink is never written through (K-11; its target may lie anywhere):
its next steps are the path through the link's target as the explicit path
(`dct project changelog-path <slug> <target>`, offered only when that path
is ADMISSIBLE — Markdown, inside the project root, no link, file or
submodule on it (K-19) — the link then stays as it is), replacing the link
by the regular file (a directory link: by a real directory), or turning the
changelog file off. The choices `ambiguous` offers lead only to admissible
paths the same way (K-24): a link candidate is offered as the path through
its target, never as itself; a link candidate without an admissible target
is dropped from the choices, and then its replace-link way and the off way
are offered (K-27 — `ambiguous` never comes out without a next step). At WRITE time the release guards the project
boundary again (K-19): a directory of F that became a link or no directory
after the pre-checks, or a path that resolves outside the project root
(also a monorepo subproject's), is refused before anything is written
through it — the record stays stamped; put the directory back and resume.

In git, a NEW release also compares the work tree's file at F's path with
the candidate's content AS A CHECKOUT WRITES IT
(`core.changelog_file.check_work_tree`, `git cat-file --filters`: eol /
smudge filters applied, K-11): something on disk where the candidate has
no F (ignored or untracked — or, on a case-insensitive file system, a
tracked file whose name differs only in case) or other bytes →
`not_baseline` (`baseline: "unfiltered"` when git still considers the
bytes the candidate's content but its filter attributes changed after the
checkout — there is nothing to commit: check it out again through its
filters). The plan of a new release makes this check the way the preview
will (K-51): with uncommitted work, it resolves F and compares the work
tree's file at the FUTURE TREE T1 of the work-in-progress commit — the
preview's own scope (`plan.work_scope`: every tracked changed path and
the untracked release artifacts) and tree builder (`plan.future_tree`, a
temporary index; the preview's candidate C\* has exactly this tree) —
instead of HEAD. A difference that commit settles (an uncommitted edit of
F, a conversion to Markdown, a link replaced by the file) is then no
problem in the plan and reaches the screen; one it does not settle (an
ignored file at F's path, skip-worktree, bytes outside the scope) still is.
When a path of that scope carries a `filter` attribute the preview commits
nothing (`filtered_paths`), so nothing settles the difference and the plan
compares with HEAD. The preview itself still checks F at C\* (type, format,
path, work-tree content) and composes the section. The release composes F from the candidate's BLOB and keeps two
forms of it apart (K-20): the id of the content git STORES (what its clean
/ eol filters make of the composition as a checkout writes it — `git
hash-object --path`, what the release commit's tree holds) and the
expected WORK-TREE bytes — the checkout form of that STORED content
(K-26), exactly what a fresh checkout of the release commit writes. A CRLF
work tree under `eol=crlf` gets CRLF; a CRLF history under `* text=auto`
is stored LF and, like a fresh clone, checked out LF — the whole file,
history included, so the next release's work-tree check passes. Those
work-tree bytes must be a fixed point of the filters (`hash-object --path`
of them gives the stored id again). "Unchanged" is never a byte
comparison of CRLF with LF: the stored content must be the composition or
the composition with its line endings normalised (CRLF → LF); a filter
path that rewrites the release's text otherwise (a clean filter), or one
without that fixed point, is refused BEFORE the stamp as problem `content`
(`content: "filtered"`). dct's own id computations run with
`core.safecrlf=false` (K-29): git's round-trip guard would die on a CRLF
changelog under `text=auto` where the owner's `git add` succeeds, and dct
checks the preservation of the text itself. An LF changelog without
attributes (dct's own repository) is written exactly as composed: work
tree, blob and composition are the same bytes.
Every problem is reported by the
plan (`changelog_file.problem` and a warning) and refused by prepare and by
apply's pre-checks BEFORE the stamp, with `details.changelog_file` naming
the configured path, the file(s) found (`found`) and the true next steps
(`ways`).

The CONTENT is checked before the stamp too (K-9): prepare and apply's
pre-checks of a new release run the WHOLE composition of F
(`effects.expected_changelog` — the function the write calls after the
stamp) on the candidate's content (outside git: the file on disk) for the
release about to be stamped — its selected entries rendered as if they
carried V, the record's spelling, today's date, its PR-reference setting
and proof map; nothing is stamped on trial. A file that is not UTF-8, one
that already holds a V section (any equivalent spelling) other than
exactly the section dct renders, or an insertion that fails its own
verification, or — in git — git filters that do not keep the composed
text (K-20) → problem `content` (`content`: `not_utf8` \|
`section_differs` \| `insertion` \| `filtered`, `detail`), whose ways are fixing the
file yourself (re-encode it / remove the pre-written section), in git
committing it as work in progress, and planning again (outside git the ways
name no git step and no commit — there is nothing to commit), or turning
the changelog file off. The plan does not compose (no release is about to be stamped there),
so `content` appears only in a refusal of prepare or apply. Every
changelog-file condition knowable before the stamp — the resolution with
every component of its path (in the candidate and on disk), the work-tree
file, the composition into the candidate's content and its filter path —
is therefore refused before it; the write after the stamp refuses only
what changed after those pre-checks (the file edited meanwhile, a
directory of its path swapped for a link, or — for a file already holding
the identical section — a stamp that falls on the next UTC day, whose
section date then differs). The guarantee "refused before the stamp"
reaches exactly that far (K-25): a foreign `index.lock` that EXISTS when
apply's pre-checks run is refused before the stamp too, but a condition
that only APPEARS during the operation — an `index.lock` taken by another
git process after the pre-checks, a refusing commit hook, a failing commit
signature — stops the release after the stamp (no trial commit is made and
no hook runs twice to find out earlier); `dct release resume` finishes it
once the cause is gone. Read-only commands never store the detected path; the
approved proposal binds it per release (`proposal.changelog.path`), so a
later rename is detected again instead of being pinned by a stale stored
value.

A HISTORICAL release is judged by its own evidence, never by today's path
or detection mode (K-12, K-21): a takeover uses the changelog file of the
record's VERIFIED manifest when it holds one; otherwise each candidate
release commit names its F itself — the ONE regular Markdown file inside
the project that the commit changed (the confirmed version-file contract
left out) and that holds a V section at the commit; when it changed none
(the candidate already held the section), the file the resolver settles at
its parent — with the stored path or with detection — that holds the
section. SEVERAL such files refuse, never guessed. Finding the section is
never enough: the section must still be exactly the database's entries and
the earlier history untouched (K-109). A changelog renamed or chosen since,
or a `NEWS.md` added since, never changes the verdict on an earlier
release; outside git the files on disk are the only evidence, so the
resolution there is today's.

A resume of a `stamped` record whose approved file is not the one the
candidate resolves now (K-16) writes neither file. Its refusal first
offers a setting CONSISTENT WITH THE APPROVED PATH (K-23: the proposal does
not record whether the path was detected or chosen, so the refusal never
claims to restore "the" setting of the approval) — derived from the stored
proposal and verified with the same resolver at the recorded candidate:
`dct project changelog <slug> on|off` when only the switch changed, else
the MANUAL choice of the approved file, `dct project changelog-path <slug>
<path>` (project-relative) — never a silent switch to detection;
`details.restore` —, then `dct release resume`, and abandon as the last
resort. When no setting
resolves the approved file again (a record stamped before #876 — `changelog.md`
approved as absent while the candidate tracks `CHANGELOG.md`, K-18), no
setting is offered: abandon is the only way.

The plan shows this BEFORE anyone chooses to finish (K-34): a resume plan
carries `resume.blocked` — `null`, or `{approved, now, restore, resume,
abandon, message}` — from `plan.resume_block`, the one read-only predicate
`dct release resume` itself refuses with (`approved` and `restore` equal the
refusal's `details`), and names it in `warnings`. It is stage-aware: only a
`stamped` record's APPROVED PROPOSAL binds the changelog file; past
`stamped` the verified manifest binds it, so a setting changed afterwards
blocks nothing. Resume still re-checks at its own run; the plan is a
preview of that check, never a substitute for it.

### Order of effects (`apply`)

1. **Pre-checks** — strict checkout (K-52; a project without a registered
   path is refused, K-95 C1); git itself must be at least
   `effects.MIN_GIT_VERSION` (2.36 — `git hook run`; refused before any
   effect, K-103); its HEAD is read ONCE — the candidate; no
   unfinished record (else `resume`); index and work tree clean: any
   staged, unstaged or conflicted path, a release artifact that is
   untracked, or an operation in progress refuses — dct never resets,
   stashes or cleans (K-50). **The changelog file** (#876 — [The changelog
   file](#the-changelog-file-876)): a problem of its resolution (`ambiguous`,
   `case_variant`, `not_markdown`, `symlink`, `not_a_directory`,
   `not_a_file`, `outside_project` — every component of the path, in the
   candidate and on disk, K-19), a work-tree file at its path that is
   not the candidate's content (`not_baseline`) or — for a new release,
   after the selection is known — a content the composition of F or its
   filter path refuses (`content`, K-9 / K-20) refuses with
   `details.changelog_file`. **A local tag of
   V** (K-8, a new release in git): any tag of V (any spelling) refuses —
   the release tags its OWN release commit, which does not exist yet, so
   the tag step could never adopt it (`details.tags`; delete a leftover tag
   yourself or choose another version). **The commit identity** (apply,
   git): `git var GIT_AUTHOR_IDENT` / `GIT_COMMITTER_IDENT` must resolve —
   else refused before the stamp, not at the commit step. **The approved plan** (K-80): the plan is
   re-derived FOR THE PINNED CANDIDATE (never a HEAD read later) with the
   same version and selection; its `snapshot_digest` must equal
   `--plan-digest` — a different HEAD, range base, entry set or entry
   state, selection, version or proof refuses before any database write
   (details: `approved_digest`, `derived_digest`, the derived `snapshot`).
   The requested ids must be proven by the plan (an id the plan reports as
   unproven is never stamped; attest it first). Entries of a project
   confirmed outside git have no commit proof — they are released as the
   owner selects them (K-63). Version-only only with `--no-entries`.
   **The approved proposal** (K-76, K-117): a NEW release needs
   `--proposal-digest` — also through the `clog release` alias, which
   never prepares or approves by itself; it must name the
   project's PENDING proposal (a newer prepare voids an older digest),
   prepared for this candidate, version, plan digest and mode (`mismatch`);
   the script must be unchanged (identity + sha256); every proposal path
   must still hold its base content (outside git also F its baseline —
   `changed`); every expected blob must still be available (`git cat-file
   -e`) and its pin in place (re-created if someone removed the ref; a
   pending proposal without an `id`, prepared by an older dct → prepare
   again). **The push target** — see [The push target](#the-push-target-k-97-f2-k-104):
   ONE destination `{remote, url, branch, tag}` named COMPLETELY by the
   caller (`--push-remote`, `--push-branch`, `--push-url` — a missing one
   refuses, `details.push_target: null`; K-123), or `--no-push`. Its URL
   is READ before the stamp (`git ls-remote <url>`, network): a tag of V
   there in any spelling, or a branch there the candidate does not contain
   (the push could never be a fast-forward), refuses — nothing stamped.
   Right before the stamp HEAD must still be the candidate (else refused,
   nothing stamped), and no foreign `index.lock` may exist in the checkout
   (K-25, `details.index_lock` — dct never removes it; run the same apply
   again once it is gone). A lock that appears only after this check, a
   refusing hook or a failing signature stops the release after the stamp
   (resume finishes it). All of this runs under the [operation lock](#the-effects-layer),
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
   proposal_sha256, render, push_target: {remote, url (the ONE effective
   push URL approved), branch, tag: "v<V>"} | null}`, the project's version-file contract becomes the proposal's
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
3. **Changelog file F** (the file [The changelog file](#the-changelog-file-876)
   resolves in the candidate — the path the approved proposal binds; only
   when the project manages its changelog — `manages_changelog`, D2). The expected content is deterministic: F's BASELINE
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
   automatically — restore or finish the file yourself, or abandon; a
   changelog file whose baseline is ABSENT has nothing to restore: what is
   on disk — an ignored or untracked file, a tracked case-variant on a
   case-insensitive file system, or an edit of the file the release wrote —
   is named, and the refusal asks to move it away (resume then writes it),
   never to "restore" it).
   **One consistent read** (K-77): afterwards the FULL artifact set is
   read ONCE and every hash, every blob id and the expected tree are
   computed from that one read; F must equal its expected content and
   every version file its approved blob — a mismatch refuses and records
   nothing. Then the manifest is recorded with `files_written`:
   `{changelog: {path, sha256, section_date}, version_files: [{path,
   sha256}], tree, untracked_baseline}` (paths repo-root-relative; for a
   project without git relative to the project root and without `tree`).
5. **Release commit** (K-96, K-79, K-103 — [Commits and hooks](#commits-and-hooks-k-96-k-79-k-103))
   — the EXPECTED tree = the candidate's tree with exactly the manifest
   blobs (`hash-object -w` of the verified work-tree files + a temporary
   index); it must equal `manifest.tree`. The real index is checked
   READ-ONLY (`git diff-index --cached`, `ls-files -s`; never `write-tree`
   or `git add` on it): paths staged against the candidate must be manifest
   paths whose staged blob is already the expected one — a foreign path or
   foreign CONTENT inside a release file (e.g. a staged hunk in
   `pyproject.toml`) refuses and leaves the staging untouched; an existing
   `index.lock` (another git process) refuses before anything is created.
   Then the explicit hook lifecycle: HEAD's commit (the candidate) and the
   ref HEAD names are PINNED; pre-commit, prepare-commit-msg and commit-msg
   run on a TEMPORARY index holding exactly the expected tree; the tree,
   the work-tree artifacts and the message are verified; `git commit-tree
   <tree> -p <candidate>` creates the commit as an UNPUBLISHED object,
   which is read back (single parent = the candidate, tree =
   `manifest.tree`, subject `release: v<V>` — a trailer a hook appended is
   fine); only then the pinned ref moves, by a compare-and-swap in which
   HEAD must still name it; post-commit runs afterwards. Anything that
   deviates is refused BEFORE the publication — no foreign commit is ever
   left on the branch. Then the user's REAL index follows the commit under
   git's index.lock protocol (below), and `committed` is recorded with
   `release_commit_sha`.
6. **Tag** — `v<V>`, lightweight (like the repository's existing tags), at
   the verified release commit (tree and parent re-checked). A tag of the
   same version (any spelling) at another commit refuses; one at the
   release commit is accepted (no second tag for another spelling — also
   an annotated one the owner created). `tagged` is recorded WITH the tag's
   verified identity (`tag_proof`, K-115 G4).
7. **Push** (K-97 F2, K-104) — to the CONSENTED push target only (never
   re-derived from today's upstream, K-97 F4): its URL must still be one
   of the push addresses of its remote (else refused: outside the consent,
   `details.push_urls`). `git ls-remote <url>` first: a tag of V there at
   another commit refuses; both already there (the branch at the release
   commit or a descendant this repository knows, the tag exactly at it) →
   `verified`, not pushed again. Else `git push --atomic --no-follow-tags
   --recurse-submodules=no <url> <commit>:refs/heads/<branch>
   <commit>:refs/tags/v<V>` — the approved URL itself and only the
   refspecs the destination still lacks, nothing git configuration would
   add (K-126, K-134; the local `pre-push` and the remote's hooks run; `GIT_TERMINAL_PROMPT=0`, the caller's SSH / credential
   variables kept) — and `git ls-remote <url>` again must show both; then
   `pushed` — recorded
   WITH what that address showed (`push_proof`: the consented target, the
   commit its tag resolved to, its branch tip; K-115 G4). K-35: git does
   not record a push by URL, so dct then refreshes the remote-tracking ref
   of the pushed branch itself — set to the branch tip the destination
   SHOWED in that read (the push proof's `branch_tip`; never the release
   SHA — the branch may have moved on), by ONE ref transaction
   (`gitinfo.git_update_ref_cas`: compare-and-swap, never forced past a
   concurrent change; `no-deref`, and the symbolic-ref check below
   repeated while git holds the ref's lock — a symbolic ref created
   between the check and the write is neither written through nor
   replaced: its target and the symbolic ref stay, a warning),
   and only when the remote's FETCH configuration confirms the mapping:
   the approved URL is the remote's only fetch URL (the repository the
   tracking ref describes — a push URL that differs from the fetch URL
   updates nothing), its fetch refspecs map `refs/heads/<branch>` to
   exactly one local ref and no negative refspec (`^refs/heads/<branch>`
   or a one-`*` pattern such as `^refs/heads/release/*`) excludes it,
   dct writes only a ref under `refs/remotes/` (e.g.
   `refs/remotes/origin/main` — a mirror refspec `+refs/*:refs/*` that
   maps the branch onto a local `refs/heads/<branch>` is never written)
   that is not a symbolic ref (`git update-ref` writes THROUGH a symref to
   its target — possibly the checked-out local branch — so a symbolic
   tracking ref is skipped with a warning naming its target),
   the update is a fast-forward (no ref yet, or the ref is an ancestor of
   the tip) — the tracking ref is never forced, whatever the refspec's
   `+`: a `git fetch` between the push proof and the write may already
   have moved it further — and this repository has the tip's object.
   `details.tracking_ref` = `{ref, updated, current, remote, tip,
   previous, reason}`; an unconfirmed mapping, a ref outside
   `refs/remotes/`, a symbolic ref, a non-fast-forward or a failed git read or write is a
   warning (`git fetch <remote>` settles it), never a failed or unfinished
   release. A resume of a `pushed` record refreshes it
   the same way after its control read. Then `completed`. A failed push (rejected, unreachable, a refusing hook)
   leaves the record `tagged` with the git error (`details.git_error`) —
   `resume` retries ONLY the push: never a new bump, commit or tag. Without
   a push target (`--no-push`, outside git): step `push` is `skipped` and
   the record goes from `tagged` to `completed` — the consent's
   `push_target: null` is the explicit local release; no push proof exists
   or is needed. With `completed` the pin of the proposal's blobs (outside
   git: its store blobs no other live proposal names) is removed (K-117).

### The push target (K-97 F2, K-104)

ONE effective destination, `effects.push_target` → `{remote, url, branch,
tag: "v<V>"}` — the same value in `prepare`'s `details.push_target` (what
the owner is shown and approves), in the consent, in the pre-stamp read,
in the push, in its verification, in the `push_proof` and in every resume.

- **Apply names the approved target completely (K-123)**: a NEW release in
  git that pushes needs `--push-remote R --push-branch B --push-url U` —
  the `remote`, `branch` and `url` of the `push_target` the approved
  prepare showed. `dct release apply` and the `clog release` alias never
  select the upstream themselves: between prepare and apply the remote's
  URL, the branch's upstream or the current branch (another branch at the
  same commit) can change while HEAD and both digests stay valid, and the
  default would then be a destination nobody approved. A missing flag →
  refused before the stamp (`details.push_target: null`, the missing flags
  named); a URL that is not a push address of that remote any more, or a
  remote that does not exist → refused before the stamp. `--no-push` = an
  explicit local release (`push_target: null`), only as the owner's
  choice; a project outside git has no target and takes no push flag.
  `resume` takes no push flag (the stored consent); a takeover binds its
  own target in the takeover digest. (The target is not part of the
  proposal digest; binding it there is future work — today the caller
  passes what was approved and every part is checked against the
  repository.)
- **What prepare shows (and a takeover derives)**: `--push-remote R
  [--push-branch B]` for a takeover, else the current branch's UPSTREAM
  (`branch.<b>.remote` + `branch.<b>.merge`); no upstream (also a detached
  HEAD or a local `.` upstream) → no default: prepare shows `push_target:
  null` with the reason in `warnings`, a takeover refuses with the
  explicit choice — never guessed. The owner then names the destination
  (apply: all three flags, the URL being one of `git remote get-url --push
  --all <remote>`), or `--no-push`.
- **URL**: the remote's EFFECTIVE PUSH address, from the full `git remote
  get-url --push --all <remote>` — never only its first line, and never the
  fetch URL (git resolves a remote NAME given to `ls-remote` to the fetch
  URL: dct always passes the URL). A fetch URL and a different push URL
  (https to read, ssh to write) is a valid layout: the push URL is the
  target, read and written. Exactly one push address → that one. SEVERAL →
  `--push-url U` must choose ONE of them, else the release is refused before
  the stamp with the list (`details.push_urls`): `git push <remote>` would
  send to all of them. There is NO mirror mode: `--atomic` covers the
  branch and the tag inside one repository, never several repositories, so
  a release goes to one destination and is verified there — push the other
  addresses yourself. `--push-url` must be one of the remote's push
  addresses (it cannot name an arbitrary repository).
- **Literal addressing**: the commands are `git ls-remote <url>` and `git
  push --atomic --no-follow-tags --recurse-submodules=no <url>
  <commit>:refs/heads/<branch> <commit>:refs/tags/v<V>` — the approved URL
  and explicit refspecs, so a later change of the remote's configuration
  (another `pushurl`, a push refspec, `mirror`) cannot add a recipient or a
  ref. Two things would still make git address ANOTHER repository with the
  same string; both are refused before the stamp, by a takeover and before
  every push / control read (`effects._check_literal_url`):
  - a `url.<base>.insteadOf` / `pushInsteadOf` rule whose value is a prefix
    of the approved URL — git would rewrite it on the command line
    (`details.url`, `details.rewrite_rule`). (The URL `get-url` reports
    already has the rewrites of the remote's own URL applied.)
  - K-124: the URL is also the NAME of a remote (`details.url`,
    `details.remote_name`) — git then reads that remote's `url` and pushes
    to its `pushurl`, not to the path the string names. Every name git
    resolves counts: `remote.<name>.*` in any configuration scope (system,
    global, local, worktree, includes) and the legacy `<git common
    dir>/remotes/<name>` / `branches/<name>` files. A slash is no proof of a
    path (`group/publish` is a legal remote name), and git reads ANY string
    as a remote when a remote of exactly that name is configured — so the
    whole string is compared with every configured name. Before the stamp,
    write a relative path explicitly (`./name`), use an absolute path or a
    real URL (`scheme://…`), or rename the colliding remote, then prepare
    again; an unfinished release keeps the approved string, so there only
    renaming or removing the colliding remote helps (or abandon).
- **Scope of the publication (K-126, K-134)**: the push sends the release
  commit to the approved branch and the tag `v<V>` — two explicit refs,
  nothing else. `--no-follow-tags`: `push.followTags` (repository or
  global) adds no other tag, although annotated tags reachable from the
  release commit may be missing at the destination. `--recurse-submodules=no`:
  `push.recurseSubmodules` / `submodule.recurse` never make dct push a
  submodule — the consent covers the superproject's destination, not the
  submodules' remotes. This is a LIMIT of what dct publishes, not a
  guarantee for consumers: a gitlink of the release may name a commit its
  submodule's remote does not have yet. dct neither pushes it nor checks
  it (a separate availability check is future work) — publish submodule
  commits yourself, before or after the release.
- **Resume**: uses the STORED target. The approved URL must still be among
  the remote's push addresses (a re-pointed remote is outside the consent
  → refused); addresses ADDED since receive nothing.
- Consequences of pushing by URL: the `pre-push` hook receives the URL as
  the remote's name, and git does not move `refs/remotes/<remote>/<branch>`
  — dct refreshes it after the verified push when the remote's fetch
  configuration confirms the mapping (K-35, `details.tracking_ref`, step 7
  of [Order of effects](#order-of-effects-apply)); otherwise the next `git
  fetch` does.

### Commits and hooks (K-96, K-79, K-103)

Both commits the effects layer creates — the release commit and the
attestation commit — are made by `effects._hook_commit`: the repository's
hook lifecycle run EXPLICITLY, then a real compare-and-swap. Not `git
commit`: its parent is whatever HEAD is when IT reads it, so a HEAD that
moved after dct's last check would be committed on top of — with the
approved tree, i.e. silently dropping the other commit's files from the
branch tip — and a hook that changed the content would already be on the
branch by the time dct could look. Hooks are never skipped (no
`--no-verify` equivalent).

**Required git: 2.36** (`effects.MIN_GIT_VERSION` — `git hook run`; the ref
transaction protocol of `git update-ref --stdin` is older, 2.27). Checked
before any effect by `prepare`, `apply`, the alias, `resume` and `attest`;
an older git is refused (`details.git_version`,
`details.required_git_version`) — never a fallback that would skip hooks.

The lifecycle, in `git commit`'s order:

1. **Pinned.** HEAD must be the expected parent (the candidate; for attest
   the HEAD read at the pre-check). The ref HEAD NAMES is resolved now
   (`git symbolic-ref HEAD` → `refs/heads/<b>`, or none: detached) — the
   only ref that will be published to.
2. **Hooks**, each through `git hook run --ignore-missing <name>` (so
   `core.hooksPath` and every way git finds a hook apply): `pre-commit` →
   `prepare-commit-msg <file> message` → `commit-msg <file>`. They get
   what `git commit -m` gives them: cwd = the top level of the SAME work
   tree; `GIT_INDEX_FILE` = a TEMPORARY index holding exactly the approved
   tree (a hook checks what is committed; the user's index is not touched);
   `GIT_EDITOR=:`; the author identity as `GIT_AUTHOR_NAME` /
   `GIT_AUTHOR_EMAIL` / `GIT_AUTHOR_DATE`, resolved once (`git var
   GIT_AUTHOR_IDENT`: the caller's variables, else `user.name` /
   `user.email` — an unknown identity is refused before any hook); `<file>`
   = this checkout's `COMMIT_EDITMSG`, spelled as git spells it
   (`.git/COMMIT_EDITMSG`; absolute in a linked worktree), `message` = the
   source of a message given on the command line. Timeout
   `effects.HOOK_GIT_TIMEOUT_SECONDS` (900 s) each, stdin closed, no
   terminal, under the supervised runner
   ([Process supervision](#process-supervision-and-the-locks-ownership-k-108-k-113-k-118-l10)):
   a hook that exceeds it — or is running when dct is interrupted — is
   stopped with everything it started (K-118 L10). Hook output is kept
   bounded (its start and its end) in the refusal's `git_error`.
3. **Verified — nothing exists yet.** The temporary index must still write
   the approved tree (`details.tree`, `expected_tree`, `changed`); the
   release artifacts in the work tree must still hold the manifest's
   content; the message file is cleaned as `git commit` cleans it
   (`commit.cleanup`: default / `whitespace` / `scissors` → `git
   stripspace`, `strip` → comments removed too, `verbatim` → as it is) and
   must not be empty.
4. **The commit object** — `git commit-tree <tree> -p <parent> -F
   <message>`, with the identity of step 2 and the SIGNING POLICY in force:
   `commit-tree` ignores `commit.gpgSign` (verified by experiment), so `-S`
   is passed when it is true; `user.signingKey` and `gpg.*` (program,
   format) are git's own. A failed signature creates nothing
   (`details.signed`). The object is UNPUBLISHED — no ref names it. It is
   read back with the proof layer's own reader and checked: tree, the
   single parent, and the message contract — the subject (`release: v<V>` /
   `chore: attest changelog entries`), and for attest the exact id set
   (K-107). A hook may ADD to the message within that contract (a trailer).
5. **Publication** — ONE ref transaction (`gitinfo.git_update_ref_cas`: `git
   update-ref -m "commit: <subject>" --stdin` with `start` / `update <ref>
   <new> <parent>` / `prepare` / `commit`): git locks the pinned ref and
   compares it with the parent — a real compare-and-swap. While the
   transaction is PREPARED (the ref locked; with the files backend also
   `HEAD` when it names that ref, so `git switch` / `checkout` cannot
   re-point HEAD; with reftable the whole ref database) dct verifies that
   HEAD still names the pinned ref, then commits — or aborts. A detached
   HEAD is published to `HEAD` itself (not dereferenced). The
   `reference-transaction` hook runs, as for every ref update.
6. **post-commit** — only after the publication, and after the caller
   recorded the published commit (`on_published`: `apply --consent` writes
   its slot's progress there, so a kill inside the user's hook finds the
   commit recorded). Its failure is a WARNING
   (`warnings`): the commit stays, and the hook is never run again — a
   resume ADOPTS the published commit, it does not commit or run hooks a
   second time.

What stops a commit, and what is left:

- A hook that REFUSES, an unknown identity, a failed signature: nothing
  created or published; HEAD unchanged (`details.git_error`, `hook`,
  `head`). The record stays `files_written`. Fix the cause and `resume`.
- A hook that CHANGES the approved content (a formatter rewriting a file,
  restaged files) or the message outside its contract (another subject, an
  attestation id added, dropped or duplicated): refused BEFORE the
  publication (K-118 L1–L3). No ref moved; no foreign commit is on the
  branch; at most an unreferenced object exists (`details.unpublished_commit`,
  removed by git's garbage collection). dct restores nothing — a
  formatter's edit of a work-tree file stays: make the hook leave the files
  alone (or approve content it accepts), put the files back as approved,
  and `resume` on the SAME candidate. No branch has to be reset.
- HEAD moved (another commit), a branch switch, HEAD attached or detached
  meanwhile — before the hooks, during them, or between the last check and
  the publication: the compare-and-swap (or the HEAD check inside the
  prepared transaction) refuses; NOTHING is published (`details.head`,
  `expected_parent`, `pinned_ref`, `head_ref`, `unpublished_commit`). The
  other process's commits stay the branch tip with their files, its staging
  stays in the index.
- A post-commit hook that rewrote the WORK TREE: the commit is published;
  refused until the files hold the commit's content again (then `resume`
  adopts it).

**A partial effect found from an older run** (K-118 L1–L3): when `resume`
finds HEAD at a commit that is neither the candidate nor the release commit
— for example the leftover of a run before K-103 whose `git commit` let a
hook change the content, or a commit made by hand — it reports exactly what
it found: `details.commit`, `parents`, `tree`, `expected_tree` (the
approved one), `subject`, `candidate`. It touches no ref, no staged change
and no file, and never resets: the hint is conditional — IF that commit is
yours to discard and nothing is based on it, move the branch back yourself
(`git reset --soft <candidate>` keeps the index and the files) and
`resume`; otherwise abandon the release and plan a new one.

Not done by this lifecycle (differences from `git commit`): no
`post-rewrite` (nothing is amended), no automatic `git gc --auto` /
maintenance after the commit, no commit template or editor.

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

The concurrency windows are covered by tests on real repositories with
real hooks (`tests/test_release_git_effects.py`,
`tests/test_release_round4_git.py`): a stage of a DIFFERENT blob for a
release path after the index check (`before_commit` barrier) and during
the commit (a hook — another process — staging into the real index): the
user's blob survives, the commit is still exactly the approved tree; a
commit made by another process before the hooks (`before_commit`), during
them (a hook) and right before the publication (`before_publish`): it
stays the branch tip, nothing is published; a branch switch and an
attached / detached HEAD at `before_publish`: nothing is published on
either branch; a `git switch` attempted inside the prepared transaction
(`publishing`): it fails, the release commit lands on the pinned branch; a
foreign `index.lock` before the commit and before the index update:
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
| `stamped` | the candidate | index: nothing staged or conflicted; unstaged only F and the proposal's files. A record without an approved proposal (stamped outside the effects layer, or before K-76) is refused — the sync is never run after the stamp; abandon it. The changelog file is resolved again at the RECORDED candidate ([The changelog file](#the-changelog-file-876)); when it is not the file the approved proposal binds (a record stamped before #876 — `changelog.md` approved as absent while the candidate tracks `CHANGELOG.md` —, or a changed changelog path) resume refuses with `details.changelog_file`, `approved` and `restore`, writing neither file: it executes only the approved proposal. K-16: it first offers the `dct project …` setting that resolves the approved file again (`restore`, derived from the stored proposal and verified with the resolver — then resume), abandon last; `restore` is empty when no setting does (a record stamped before #876, K-18) — then `dct release abandon` ends the record (its stamps and `current_version` stay, V stays taken). Otherwise steps 3-4: each artifact classified (baseline → written, approved → kept, else refused with nothing written), the approved blobs written, the full set verified from one read, the manifest recorded |
| `files_written` | (F is the file `manifest.changelog` records, from here on) the candidate — or the release commit itself (its only parent is the candidate, its tree is `manifest.tree`, its subject `release: v<V>`), which is adopted (then only the real-index update and `committed`) | every manifest file must still hold its recorded hash (a reverted version file → refused, not overwritten); then step 5 (the hook lifecycle, the commit on the pinned candidate, the compare-and-swap). An adopted commit runs no hook again. Any OTHER commit at HEAD → refused with its commit / parents / tree / subject and a conditional hint, nothing reset ([above](#commits-and-hooks-k-96-k-79-k-103)) |
| `committed` | the release commit or a descendant | the release commit is verified (tree = `manifest.tree`, parent = candidate); step 6 |
| `tagged` | the release commit or a descendant | a tag of V must point at the release commit and the recorded `tag_proof` must still be what the repository shows (a moved / deleted tag → refused); a record older than the field gets the identity verified NOW with its next progress write; step 7 — ONLY the push, to the consented target's URL (already there → verified; the URL must still be a push address of its remote); without a push target `completed` |
| `pushed` | the release commit or a descendant | the tag as for `tagged`; the consented URL must still show the branch and the tag (`git ls-remote <url>`, nothing pushed again; a record without a `push_proof` gets what was read now); `completed` |
| `completed` / `abandoned` | — | no unfinished record: outcome `noop`, no side effect |

Interruption points (`effects.INTERRUPTION_POINTS`, the `on_point` hook of
`apply_release` / `apply_consent` / `resume_release`) — a crash at each one
is resumed by `resume` (before the stamp of `apply --consent`: by the same
`apply --consent` again) exactly once (one section, one release commit, one tag; the sync
never re-run):

| point | state left behind |
| --- | --- |
| `before_work_commit` | `apply --consent` only: every pre-commit check passed; no hook has run, nothing made (run the same `apply --consent` again) |
| `after_work_commit` | `apply --consent`: W published and recorded in the slot's `progress`; the real index NOT synced (the re-run adopts W and syncs the index) |
| `after_work_index` | `apply --consent`: the real index entries of Pₛ are W's (adopted on the re-run) |
| `after_attest_commit` | `apply --consent`: A published and recorded in `progress` (adopted on the re-run) |
| `before_stamp` | nothing stamped (no record; the pending proposal — or the preview — stays: run `apply` again; `apply --consent`: W / A may exist and are adopted) |
| `after_stamp` | record `stamped` (+ proposal, consent, `untracked_baseline`, the learned contract — one transaction) |
| `before_changelog` | every artifact classified; nothing written; `stamped` |
| `after_changelog` | F written; no version file written; `stamped` |
| `before_version_files` | as `after_changelog` |
| `after_version_file` | ONE more version file written (fires after each write); `stamped` |
| `after_version_files` | every artifact written; not verified, not `files_written` |
| `after_files_written` | manifest recorded; no commit |
| `before_commit` | the real index checked (read-only, untouched); no hook has run, no object created |
| `before_publish` | pre-commit / prepare-commit-msg / commit-msg ran; the commit object exists on the pinned parent and is verified; NO ref moved (resume runs the lifecycle again — the object is unreferenced) |
| `publishing` | the ref transaction is prepared (git holds the lock of the pinned ref, and of HEAD naming it); not committed — an interruption drops the transaction, no ref moved |
| `after_commit` | the pinned ref moved to the release commit (compare-and-swap), post-commit ran, verified; the real index not updated; not `committed` (adopted on resume, no hook runs again) |
| `after_index` | the real index follows the release commit; not `committed` (adopted on resume) |
| `after_committed` | no tag |
| `after_tag` | tag created; not `tagged` |
| `after_tagged` | not pushed |
| `before_push` | the push URL read (no tag of V elsewhere); `git push` not run; `tagged` |
| `after_push` | pushed and verified at the push URL; not `pushed` (verified, not pushed again, on resume) |
| `after_pushed` | not `completed` |

A user edit between runs inside an artifact (a version file or F) is
neither the baseline nor the approved content: the resume refuses and
leaves the files and the index exactly as they are.

### Reconstruct (`dct release reconstruct`, K-60, K-86, K-96 D8)

A reconstruction records a release made BEFORE release records existed. It
re-stamps nothing and never modifies a commit, a tag or a file — it only
reads, and with `--confirm` writes ONE ledger row. The ADAPTER is chosen by
the project's release executor (K-119): `dct` → dct's own rules
(`effects.gather_evidence`, below); `own` → the evidence the project's
tool supplies ([the own adapter](#the-own-adapter---evidence-k-119)). Each
refuses the other's project.

Evidence by dct's own rules, read-only, in this order:

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
2. **The version-file contract, as it is** (K-110): `null` is an UNKNOWN
   contract (nothing was ever confirmed), a list is a CONFIRMED one — `[]`
   is the confirmed "this project has no version files". The two are never
   merged: the preview shows `contract: {version_files, state: "unknown" |
   "confirmed"}` and the record stores it (`manifest.verification.contract`:
   null or the list).
3. **Candidates** (D8): every commit titled `release: v<V>` (any equivalent
   spelling, any local ref) and the commit the tags of V point at is a
   CANDIDATE, verified on its own evidence: exactly ONE parent (the
   candidate), the paths it changed besides F equal to the CONFIRMED
   contract (an unknown contract: the changed paths are checked one by one
   and reported as `version_files: "from_commit"` — nothing is learned
   silently, the project's contract stays unknown), F at that commit
   holding the V section EXACTLY once (any equivalent label; two sections
   such as `[1.1]` and `[1.1.0]` are a contradiction) with the database's
   entries (its date kept as `section_date`), the WHOLE older history of F
   preserved (next item), and each changed version file passing the D9
   contract against the parent (J8 — never a substring search): a file dct
   has a reader for must have its version FIELD equal to V
   (`same_version`), any other file may change only lines holding V as a
   whole version token (any equivalent spelling). `candidates`: `[{commit,
   verified, reason, history}]`.
4. **The whole changelog history against the parent** (K-109,
   `effects._changelog_history`) — not only the new block: F at the commit
   with its V section TAKEN OUT must be F at the commit's parent, byte for
   byte (or the commit must equal the parent with that section inserted by
   the workflow's own insertion rule — a file that had no version section
   before). Only the V block is ever normalised; the older content is
   compared as bytes, so a normalisation cannot hide a change of an older
   section. `history.state`:

   | state | meaning |
   | --- | --- |
   | `preserved` | the older content is exactly the parent's |
   | `unchanged` | F is identical at both (the parent already held the section) |
   | `bootstrap` | F does not exist at the parent: the release commit created it — the explicit exception; nothing earlier exists to compare, and it is never reported as `preserved` |
   | `changed` | anything else — a removed, reordered or edited older section, a changed preamble, a whitespace or line-ending change, ALSO a typo fix |

   `changed` is a deviation: the candidate is NOT verified, the diff is
   shown (`history: {state, diff (unified, at most 400 lines), diff_lines,
   diff_sha256}`; the CLI prints it; `details.history` lists the candidates
   concerned) and dct does not attest that the history was preserved —
   nothing is recorded. There is no flag that waives it.
5. **The stage the evidence reaches**: the tags of V must all point at ONE
   commit (else refused — a contradiction). With a tag: that commit must be
   a verified candidate → status `completed`, recorded WITH the tag's
   identity as verified now (`evidence.tag_proof` → the record's
   `tag_proof`, K-115 G4; other verified candidates, e.g. a backport on
   another branch, are listed, not used). Without a tag: exactly one
   verified candidate → status `committed` — the record ends where the
   evidence ends. SEVERAL verified candidates without a tag are ALL shown
   and dct picks none: choose one explicitly with `--release-commit <sha>`
   (it must be one of the verified candidates, and the tagged commit when
   a tag exists). No verified candidate → refused. Without a release
   commit nothing is reconstructed (a stamp without its candidate cannot
   be verified).
6. **Without git**: an UNKNOWN contract REFUSES (`details.contract`) and
   names the procedure that shows and confirms it — `dct release
   version-files`, then `--set <paths> --confirm` or `--none --confirm`
   (K-110): nothing is verified against a contract nobody confirmed. With
   a confirmed contract: F (the section, as above) and the contract's
   files as found (version field, or a line holding V) → `completed`. No
   commit, no tag and no history check exist there (`history:
   "not_verifiable"`) — none is invented.

**The recorded scope** (K-110, `manifest.verification`, also
`evidence.verification`) says WHAT was verified: `{adapter: "dct",
contract (null = unknown \| the confirmed list), changelog
("section_verified" \| "not_managed"), history (the state above \|
"not_verifiable" without git \| "not_managed"), version_files ("verified"
against the confirmed contract \| "from_commit" \| "none"), commit, tag
(bool: verified in the repository), limited}`. `limited: true` = NO
artifact content was verified (dct does not manage a changelog file for
the project and it has no version files — e.g. a confirmed `[]`): the
database's stamp (and, in git, the release commit) is then all the
evidence there is. That is reported (a `warnings` line, the CLI's `scope:
LIMITED`), recorded, and nothing else is claimed — no file, commit or tag
is made up; a confirmed `[]` alone never proves that a release exists.
Core `reconstruct_release` refuses a reconstruction without git whose
manifest carries no scope, or an unknown contract.

**How it is finished** (K-119 M3): a reconstruction that ends at
`committed` is never left at "do it by hand or abandon". The result names
the concrete ways (`details.finish: [{way, command, what}]`, printed by
the CLI; `[]` when completed):

- `takeover` — `dct release takeover --version V …`: dct takes the record
  over from its verified artifacts and `dct release resume` performs the
  missing tag and push ([Takeover](#takeover-dct-release-takeover-k-119-m3-k-117-j4));
- `ledger` — whoever performs the missing effects reports what was
  verified: `dct clog release-advance V <status> --tag-proof '{…}'
  [--push-proof '{…}']` (the only way for an `own` record).

The record: mode `reconstructed`, the resolved version, the status of step
5, the manifest `{tree, changelog, version_files, verification}`, the
`tag_proof` of a `completed` git release, no consent, proposal, render or
push proof (nobody approved or verified a push through dct — no
confirmation is invented for old history), the project's executor at
reconstruction. That executor (G8 / M9) is an explicitly ASSIGNED
responsibility for the record's further handling — who may record progress
on it or abandon it — NOT provenance: it does not say who made the
historical release. Every surface shows it that way (`details.executor:
{assigned, provenance: false}`; the CLI line `responsibility: … not who
made the release`). Any refusal writes nothing — also when the ledger
already holds a record of the version, or several (K-115 G6).

#### The own adapter (`--evidence`, K-119)

dct's rules above assume dct's artifact format — a commit titled `release:
v<V>`, dct's changelog layout, a version field dct can read. A project
whose release executor is `own` has its own format, so those rules are
never applied to it: `dct release reconstruct --version V` REFUSES
(`details.executor: "own"`) and names this path. Its tool supplies the
evidence, dct verifies it in the repository and records it through the
same core `reconstruct_release`:

```bash
dct release reconstruct --version V --evidence evidence.json            # preview
dct release reconstruct --version V --evidence evidence.json --confirm  # record
```

`evidence.json` (unknown keys are refused):

| key | content |
| --- | --- |
| `release_commit_sha` | the release commit (git: required) |
| `candidate_sha` | the commit the release was cut from (git: required) |
| `status` | the stage the tool vouches for: `committed` \| `tagged` \| `completed` (default `completed`; outside git only `completed`). `pushed` is not accepted here — a push is recorded afterwards through the ledger with its `push_proof` |
| `tag` | the tag NAME in the tool's own format; required for `tagged` / `completed`, refused for `committed` |
| `artifacts` | `[{path, sha256}]` — the files the tool's release produced (its changelog, its version files), repo-relative |

What dct verifies — the format stays the tool's: the version and its exact
entry set against the database (step 1 above); the release commit exists;
the candidate is one of its STRICT ancestors; every artifact exists AT THE
RELEASE COMMIT with exactly that SHA-256 (outside git: on disk); the tag
exists and points at the release commit. The record's `tag_proof` is READ
FROM THE REPOSITORY (name, object, commit, annotated) — supplied data
never becomes a proof (K-115), and no push proof or consent is written.
The manifest: `{tree, artifacts, verification: {adapter: "own", contract
(the declared paths), artifacts ("hash_verified" \| "none"), commit, tag,
limited}}`. Any contradiction refuses with nothing written. Supplied
evidence for a project executed by `dct` is refused — it would skip dct's
own rules (the history check included).

### Takeover (`dct release takeover`, K-119 M3, K-117 J4)

A record WITHOUT a consent — a reconstruction that ended at `committed`, a
record stamped through MCP `release_changelog`, an older record — blocks
every AUTOMATIC effect: `dct release resume` refuses it. It keeps the
ledger path, and — when its executor is `dct` and its release commit
exists — it can be handed to dct by the owner. The takeover supplies the
missing consent and proposal FROM THE VERIFIED ARTIFACTS:

```bash
dct release takeover --version V [--push-remote R [--push-branch B] [--push-url U] | --no-push]
#   → preview: what dct verified, the consent it would record, takeover_digest
dct release takeover --version V <the same push flags> --takeover-digest D --confirm
dct release resume        # performs ONLY the missing effects (the tag, the push)
```

1. **The record**: of V, not terminal, executor `dct` (an `own` record
   stays with its tool — refused), no consent (a record that has one is
   dct's already: `resume`), its entry set exactly the live entries
   carrying V, in a git checkout (the record's own when it names one).
2. **The artifacts are verified again** by the rules of the reconstruction
   (steps 3-5 above; the record's release commit when it names one) and
   compared with the record: its candidate, release commit and manifest
   (`tree`, `changelog`, `version_files`) must be what was verified. No
   verified release commit (nothing was made yet, the section is not the
   database's, the history changed), or a contradiction → REFUSED, nothing
   written; the ledger path stays.
3. **The push target** is chosen here and bound by the takeover digest
   ([push target](#the-push-target-k-97-f2-k-104)): the branch's upstream,
   `--push-remote` / `--push-branch` / `--push-url`, or `--no-push` — the
   preview shows it, the confirmation takes the same flags. A tag
   of V that already exists at the release commit is the tag that is
   pushed.
4. **What the owner approves is shown** (outcome `preview`, read-only, no
   lock): `details.consent` — `{version, entry_ids, candidate_sha, render,
   push_target (or null = no push), executor: "dct", takeover:
   {release_commit_sha, tree, artifacts_sha256}}` (`artifacts_sha256` =
   SHA-256 of the canonical JSON of the manifest's `tree` / `changelog` /
   `version_files`: the artifact hashes), `details.proposal` (derived from
   the artifacts: the version files' blobs at the candidate and at the
   release commit, the changelog's baseline; no script ran),
   `details.stage` (`committed` \| `tagged` — what the evidence proves),
   `details.missing_effects` (`tag`, `push`), and `details.takeover_digest`
   — the SHA-256 of consent, proposal, stage and tag identity.
5. **`--takeover-digest D --confirm`** — under the operation lock
   everything is derived again and must give the same digest (another
   record state, artifact, tag or push choice → refused: preview again).
   Then the proposal gets its stable `id`, its blobs are pinned
   (K-117, as [prepare](#prepare-dct-release-prepare-k-76) does), and core
   `take_over_release` records, in ONE transaction: the consent, the
   proposal, and whatever of candidate / checkout / release commit /
   manifest the record lacked (a record stamped without them) — outcome
   `taken_over`. Every field is write-once: filled when absent, confirmed
   when equal, REFUSED when different. A record below `committed` moves to
   `committed` (where the verified evidence starts); a record at
   `committed` / `tagged` / `pushed` keeps its status and its proofs.

What a takeover never does: a new bump, a re-stamp (no entry and no
`current_version` is touched), a file write, a commit, a tag, a push, or a
change of anything already recorded. Afterwards the record is one dct
executes (`progress_owner` `dct`): `dct release resume` verifies the
release commit and performs only what is missing under the normal rules —
the tag (an existing one at the commit is verified and recorded instead),
the push to the consented target (only the refs the destination still
lacks: a remote branch that already contains the release commit is not
pushed to), `completed`; `dct release abandon` ends it. Tests:
`tests/test_release_round4_reconstruct.py`.

### JSON (`--json`, every subcommand)

One shape, `effects.RESULT_KEYS` (pinned by
`tests/test_release_effects.py::test_result_keys_are_the_documented_schema`):

| key | content |
| --- | --- |
| `schema_version` | `1` |
| `action` | `prepare`, `apply`, `resume`, `attest`, `abandon`, `reconstruct`, `takeover`, `version-files`, `status`, `advance` (`clog release-advance`) |
| `project` | slug |
| `outcome` | `completed`, `noop`, `attested`, `abandoned`, `preview` (reconstruct, takeover, version-files: nothing written; prepare --preview: the preview slot is stored), `reconstructed`, `taken_over`, `confirmed` (version-files), `prepared`, `advanced`, `found` / `none` (status; version-files: the contract is confirmed / UNKNOWN), `refused`, `failed` (the version-sync script in prepare: non-zero exit or timeout), `conflict` (a lost serialization race — [below](#conflicts-k-88)) |
| `version` | the record's version (authoritative spelling) or null |
| `release` | the record (`core.releases.RECORD_FIELDS`) or null |
| `steps` | `[{step, result, …}]` — step ∈ `preview_tree` (prepare --preview: `tree` = T1; `skipped` outside git), `sync`, `proposal` (prepare; `pin` = the ref keeping its blobs), `stamp`, `changelog`, `version_files`, `files`, `commit`, `index` (the real index after the commit), `tag`, `push` (`remote`, `url`, `branch`, `tag`; `skipped` with `reason` without a push target), `complete`, `attest`, `takeover` (`pin` = the ref keeping the derived proposal's blobs); result ∈ `done`, `verified`, `skipped`, `adopted`, `conflict` (the last step of a conflict response) |
| `warnings` | human-readable strings (e.g. a post-commit hook that failed after the commit was published; a pin or stored blob that could not be cleaned) |
| `details` | prepare: see [Prepare](#prepare-dct-release-prepare-k-76), plus `push_target` (the upstream target the owner is shown — apply takes its `remote`, `branch` and `url` as `--push-remote` / `--push-branch` / `--push-url`, K-123 — or null, read-only); prepare --preview / --discard-preview: see [The preview](#the-preview-dct-release-prepare---preview) (its refusals add `in_progress`, `conflicted`, `intent_to_add`, `submodule`, `bad_untracked`, `hidden_index_flags`, `filtered_paths`, `secret_like`, `message_required`, `nothing_to_commit`, `index_only`, `message_empty`, `message_file`, `message_foreign_ids`, `not_unproven`, `not_attestable`, `no_git`, `remote_ahead`, `remote_tag_exists`, `version_unresolved`, `preview_too_large`, `checkout_changed`); apply: `{requested_version, previous_version, bump, entry_ids, mode, released, current_version, push_target}` plus `tracking_ref` after a push (also on a resume — K-35, step 7 of [Order of effects](#order-of-effects-apply)); apply --consent: the same plus `{consent_digest, variant, made, candidate_sha, plan_digest, proposal_sha256}`, every refusal with `made` and its own keys (`consent_flag_conflict`, `no_preview`, `consent_unknown`, `consent_in_progress`, `base_moved`, `work_changed`, `index_changed`, `consent_mismatch`, `push_error`, `hook_changed_tree`, `message_contract`, `preview_replaced`, `date_changed` — [Apply --consent](#apply---consent-the-one-dialog-release)); attest: `{previous_head, head, entry_ids}`; abandon: `{updated}`; reconstruct: `{evidence: {version (authoritative), requested_version, spellings, entry_ids, candidate_sha, release_commit_sha, tag, tag_proof ({name, object, commit, annotated} \| null), checkout_root, manifest, verification, contract, status, without_git, candidates}, executor: {assigned, provenance: false}, finish: [{way, command, what}]}` (`status` = `completed` \| `committed` — the own adapter also `tagged`; `candidates` (git, dct's rules) = `[{commit, verified, reason, history}]`; `verification` = the recorded scope, `contract` = `{version_files, state}` — [Reconstruct](#reconstruct-dct-release-reconstruct-k-60-k-86-k-96-d8); `executor` = the ASSIGNED responsibility, never provenance; `finish` = the ways to finish a release that is not `completed`); takeover: `{consent, proposal, stage, record_status, status_after, missing_effects, takeover_digest, evidence: {candidate_sha, release_commit_sha, manifest, tag_proof, candidates}, executor}` (+ `next` once taken over — [Takeover](#takeover-dct-release-takeover-k-119-m3-k-117-j4)); version-files: `{contract: {version_files, state, confirmed_at}}` (+ `proposed`, `change` in the preview; `updated` after `--confirm`); advance: `{updated, previous_status}`; conflict: [Conflicts](#conflicts-k-88); refused / failed: the reason (`error`) plus the refusal's data (e.g. `staged`, `unproven`, `proposal_digest` / `plan_digest` (a new release without an approved proposal — also the alias), `mismatch`, `checkout_changed`, `changelog_file` (the changelog file's resolution with its `problem` and `ways` — #876; problem `content` adds `content` and `detail`, K-9; on a resume also `approved`, the path the approved proposal binds, and `restore`, the setting commands that resolve it again — K-16), `changelog_changed` (`{path, approved, now}` — after the stamp, on the write or a resume: the recomposed changelog file is not the composition the consent binds, `changelog_sha256`; nothing written), `tags` (local tags of V refused before the stamp, K-8), `foreign`, `foreign_paths`, `foreign_content`, `foreign_staged`, `changed`, `path`, `sync`, `exit_code`, `environment` (the sync script's environment contract); the commit: `git_error`, `hook`, `commit`, `head`, `tree`, `parents`, `subject`, `expected_tree`, `expected_parent`, `unpublished_commit`, `pinned_ref`, `head_ref`, `signed`, `candidate`, `hook_changed_tree` (`{paths}` — a hook changed the content to be committed; `changed` names the same paths), `approved_ids` / `proven_ids` (attest), `previous_head` (attest), `git_version` / `required_git_version`, `index_lock`; the push: `push_target`, `push_urls`, `url`, `rewrite_rule`, `remote_name`, `remote_tags`, `remote_branch`, `outside_consent`; a reconstruction / takeover: `candidates`, `history` (`[{commit, state, diff, diff_lines, diff_sha256}]` — the candidates that changed older changelog history, K-109), `contract` (an unknown version-file contract, K-110), `executor`, `contradictions`, `approved_digest` / `derived_digest` (the takeover digest), `artifacts`; the operation lock: `lock` (null with `reason`: `no_path` \| `unverifiable_path` when its identity cannot be determined), `holder`, `process_groups` (groups a killed dct left running — or `[]` when that cannot be verified), `process_group` (a sync script's group that could not be stopped); the executor: `executor`, `consent`) |

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
retried automatically by the CLI: not the write, not the sync, the commit,
the tag or the push (the `/release` dialog's one re-run of `apply --consent`
before the stamp is the assistant's, made safe by adoption).

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
| `manifest` | JSON object of the verified artifacts (e.g. `changelog`, `version_files`, `tree`, `untracked_baseline`; a reconstruction adds `verification` — the recorded SCOPE of what was verified, K-110, with the version-file contract as it was: null = unknown, a list = confirmed; the own adapter stores `artifacts`) | write-once per key; the MERGED manifest ≤ 64 KiB |
| `proof` | `{"<entry id>": {commits, merge_commit, pr[, pr_source, source, ambiguous, associations]}}` (K-70, K-89); `pr_source` = `git` \| `client`, stored with every `pr` ([origin](#origin-of-a-pr-number-k-116-h7)) | written with the stamp; a re-run passing a different map (the origin included) is refused (K-94 B2) |
| `consent` | what the owner approved (K-97 F4): `{version, entry_ids, candidate_sha, proposal_sha256, render, push_target: {remote, url, branch, tag} \| null[, executor: "dct" \| "own"]}` (`push_target: null` = the EXPLICIT local release: `--no-push`, or outside git; `executor` (K-106) = who the owner approved to perform it — present on consents written since the field; plus `takeover: {release_commit_sha, tree, artifacts_sha256}` on a consent recorded by `dct release takeover` (K-119 M3) — what was VERIFIED when the owner handed an existing release to dct: it must name the record's release commit, the manifest's tree and the digest of its artifacts; plus `content_sha256` — the digest C of the consent view the owner approved on the one-dialog screen, kept only when given, so older consents digest unchanged; a consent naming it must name its `proposal_sha256`; plus `changelog_sha256` — the composed changelog file (work-tree form) the owner approved on that screen, kept only when given; the file write and every resume refuse another composition (`changelog_changed`); a consent naming it must name a recorded proposal that approves a changelog file) — what a resume EXECUTES: its version, entries and candidate must be the record's, its push target is the only one pushed to (its remote must still have that push URL) | write-once |
| `proposal` | the approved version-file proposal (K-76): `{id, paths, base_blobs: {path: blob \| null}, expected_blobs: {path: blob}, script: {identity, sha256} \| null, candidate_sha, version, plan_digest, release_mode, changelog: {path, base_blob, base_sha256} \| null}` (`id`, K-117: the proposal's stable identity, which names the ref pinning its blobs — absent on proposals stored before the field) (the last four: the release context it is bound to; blob ids are git blob ids, or SHA-256 of the content in dct's release blob store for `without_git`); `consent.proposal_sha256` = `proposal_digest(proposal)` (SHA-256 of its canonical JSON). Written by the stamp that consumed the project's PENDING proposal | write-once |
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
release-status`, `clog candidates` and the previews of `reconstruct`,
`takeover` and `version-files` (no `--confirm`) open the
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
| 1 proof | `dct release plan --json`, `dct clog candidates --json` | every project — read-only: no database, ref, index or work-tree write (the plan's future tree of uncommitted work only adds unreferenced git objects, as the preview does) |
| 2 ledger | MCP / core `release_changelog`, `get_release`, `advance_release`, `abandon_release` | the effects layer, or a project's own release tool |
| 3 effects | `dct release attest / prepare / apply / resume / abandon / reconstruct / takeover / version-files` | the default behind `/release` for projects without their own tool (`reconstruct --evidence` is the own tool's adapter) |

A client skill only runs the dialog over layers 1 and 3; the MCP
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
tool's effects. (A record with executor `dct` and no consent can be handed
to dct by the owner — [Takeover](#takeover-dct-release-takeover-k-119-m3-k-117-j4);
an `own` record never.) A release the tool made BEFORE records existed is
recorded through the own adapter: `dct release reconstruct --version V
--evidence FILE` ([the own adapter](#the-own-adapter---evidence-k-119)). Its tests: `tests/test_release_executor.py` (a fake own tool
run as a separate process: shared plan → ledger stamp → its own effects →
`advance_release`; dct's `apply` / `resume` refuse and its effects survive).

