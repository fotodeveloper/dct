# Release workflow — proof layer and effects layer

The release workflow is layered (K-72):

1. **Proof layer** (this document) — `dct release plan --json` and
   `dct clog candidates --json`. Read-only: no database write, no ref, index
   or work-tree change (every git read runs with `--no-optional-locks`).
   Usable by every project, also one whose own release tool performs the
   effects.
2. **Ledger layer** — core / MCP: `release_changelog`, `get_release`,
   `advance_release` (`dct/core/releases.py`).
3. **Effects layer** — `dct release attest / apply / resume / abandon /
   reconstruct` (changelog file, version files, release commit, tag), the
   default behind `/release` ([below](#the-effects-layer)). A project with its
   own release tool replaces only this layer: it verifies its own effects and
   reports progress through `advance_release`.

Implementation: `dct/workflows/release/proof.py` (pure rules over commit
messages), `dct/workflows/release/plan.py` (git + database reads) and
`dct/workflows/release/effects.py` (the effects layer). The
rules in prose: [assistant-fallbacks.md](assistant-fallbacks.md#changelog-proof-trailers-and-range-shared-by-commit-and-release).

Both documents carry `schema_version` (currently `1`); it moves when the
meaning of a key changes. The top-level keys below are pinned by
`tests/test_release_plan.py::test_json_documents_keep_their_top_level_keys`.
Datetimes are ISO 8601 strings (UTC when stored naive). Paths in `index`
are relative to the repository root.

## `dct release plan [-P slug] [--version V | --patch | --minor | --major] [--json]`

The checkout is verified STRICTLY (K-52): the current directory must be a
checkout (main or linked worktree) of the project's repository with a first
commit; anything not confirmed exits 1 — never a fallback to the main
checkout. A project whose registered path is confirmed outside git (or that
has no path) gets a plan without a range.

| key | content |
| --- | --- |
| `schema_version` | `1` |
| `project` | slug |
| `kind` | `"release"`, or `"resume"` when an unfinished release record exists |
| `version` | `{current, target, bump}` — `target` from `--version` (normalized) or the bump; for a resume without a version, the record's version |
| `checkout` | `{git: true, repo_root, project_root, candidate_sha (full HEAD), branch (null on a detached HEAD), detached, matches_record?}` or `{git: false, reason: "no_path" \| "not_a_repository", project_root}`; `matches_record` (resume only) = `{candidate_sha, checkout_root}` booleans against the record |
| `unfinished_release` | the unfinished release record (`core.releases.RECORD_FIELDS`) or `null` |
| `range` | see below; `null` without git |
| `entries` | `{proven: [...], unproven: [...]}` — every unreleased entry of the project, by id |
| `foreign_citations` | `[{id, reason: "unknown" \| "other_project" \| "released" \| "deleted", version, commits}]` — cited in the range, never stamped |
| `reverts` | `[{sha, target, target_sha, status: "in_range" \| "outside_range" \| "ambiguous", effective, affected}]` |
| `squash_commits` | `[{sha, subject, signals, pr, lost_ids, proves}]` — `signals` ⊆ `pr_suffix`, `local_squash`, `indented_trailers` |
| `attestation_groups` | `[{branch, entry_ids, squash_commits, signals, proposal}]` — ONE proposed collective attestation per source branch; `signals` ⊆ `indented_trailer`, `branch_named`, `branch_commits_absent` |
| `proof` | the proof map the release record stores: `{"<id>": {commits, merge_commit, pr}}` (`core.releases.validate_proof`) |
| `index` | `{clean, staged, unstaged, untracked, conflicted, release_artifacts: [{path, role: "changelog" \| "version_file", staged, unstaged, untracked, conflicted}]}` — `clean` ignores untracked files; `staged` = the index differs from HEAD; `null` without git |
| `version_files` | the project's version-file contract `{version_files (null = to be learned at the first release), confirmed_at, history}` |
| `warnings` | human-readable strings (range to confirm, approximate range, revert outside the range, unfinished release, no git) |

**`range`**: `{candidate_sha, base_tag, base_sha, base_version, expected_tag,
confirm_required, approximate, since, revs, display, commit_count}`.

- `base_tag`: the nearest release tag that is an ancestor of the candidate —
  the ancestor tag with the HIGHEST version under the version contract
  (ties: the exact `v<current_version>`, then a `v`-prefixed name, then the
  lowest name). Tags whose name is not a version are ignored.
- `expected_tag`: `{name: "v<current_version>", exists, ancestor}` (null
  without a current version). `confirm_required` is true when the base is
  not the current version's tag — show it and confirm it.
- No ancestor release tag: `approximate: true`, `since` = the oldest
  unreleased entry's `created_at` minus one day, `revs = ["--since=…",
  candidate]`; without unreleased entries the range is empty (`revs: null`).

**Entry objects** (both lists): `{id, category, entry, component, source_file,
branch, worktree, branch_source, uncertain, created_at}` — `uncertain` is
true when `branch_source` is not `client` (K-53: server-detected or no
provenance; still classified by proof). Plus:

- proven: `commits` (proving SHAs, oldest first), `merge_commit` (the
  first-parent merge that brought the first proving commit in, or null),
  `pr` (from a `Merge pull request #N` merge subject, else a `… (#N)`
  proving-commit subject, or null);
- unproven: `reason` ∈ `no_trailer`, `reverted` (+ `commits`,
  `reverted_by`), `below_range` (+ `commits`: cited only below the base,
  scanned since the oldest unreleased entry minus one day),
  `squash_suspected` (+ `group`: index into `attestation_groups`), `no_git`.

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
PR number. The groups are proposals — the owner confirms an attestation.

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
| `confirmed` | client-provenance entries of the current branch whose worktree is this toplevel (or not recorded) |
| `uncertain` | entries with server-detected or no provenance — whatever branch / worktree they store — and, on a detached HEAD, every client entry of this checkout; each with `uncertain: true` and `uncertain_reason` ∈ `server_detected`, `no_provenance`, `detached_head`. NEVER attached automatically: match against `staged_files`, confirm (`update_changelog` branch + worktree) first |
| `other_branch` | client entries of this worktree recorded on another branch (offer to correct the branch) |
| `already_proven` | ids a commit of the range already proves — removed from every list |
| `staged_files` | `git diff --cached --name-only` (repo-root-relative) |
| `warnings` | detached HEAD, not a checkout, unreadable range |

Client entries of other checkouts are not listed.

## The effects layer

`dct/workflows/release/effects.py` — ONE implementation for the CLI and the
skills of both clients (they only run the dialog). Every effect is VERIFIED
against reality before the ledger records its progress; a status is never
taken as proof. Nothing is pushed. The MCP server never runs this code.

### Commands

| command | effect |
| --- | --- |
| `dct release attest --entries 16,17 --confirm` | the EMPTY attestation commit on the current branch (or detached HEAD): subject `chore: attest changelog entries`, final paragraph `Changelog: <ids ascending>`. Only ids the plan lists as unproven; refused when anything is staged, an operation (merge, rebase, cherry-pick, revert) is in progress, or a release is unfinished. The candidate changes; no record exists yet. |
| `dct release apply [--version V \| --patch \| --minor \| --major] (--entries … \| --no-entries) --confirm [--confirm-version-files]` | one release from a fresh plan — steps 1-6 below |
| `dct release resume [--confirm-version-files]` | finishes the unfinished record (table below) |
| `dct release abandon --version V --reason "…" --confirm` | core `abandon_release`: the record ends `abandoned`; stamps, `current_version` and every effect stay; V stays taken |
| `dct release reconstruct --version V [--confirm]` | a legacy release WITHOUT a record: evidence is gathered read-only and shown; `--confirm` records it (core `reconstruct_release`, no re-stamp) |
| `dct release status [--version V]` | the record (read-only) |
| `dct clog release [V] [--patch …] [--entries … \| --no-entries] [--confirm-version-files]` | alias of `apply`; running it is the confirmation. Without `--entries` it releases every unreleased entry — all must be proven. Refused instead of guessed: unproven entries, a proof range whose base is not `v<current_version>` (`range.confirm_required`), the first release's version files without `--confirm-version-files`. |

### Order of effects (`apply`)

1. **Pre-checks** — strict checkout (K-52); no unfinished record (else
   `resume`); index and work tree clean: any staged, unstaged or conflicted
   path, a release artifact that is untracked, or an operation in progress
   refuses — dct never resets, stashes or cleans (K-50); the requested ids
   must be proven by the plan (an id the plan reports as unproven is never
   stamped; attest it first). Ids the plan does not list (unknown, foreign,
   released) go to core's strict validation, where an entry already
   carrying V is legal (K-61). Entries of a project confirmed outside git
   have no commit proof — they are released as the owner selects them
   (K-63). Version-only only with `--no-entries`.
2. **Stamp** — `release_entries(V, entry_ids, proof=<plan proof map for
   those ids>, candidate_sha=HEAD, checkout_root=<repo root>)`. From here
   the record's `version` is the spelling of every artifact (K-55); the
   requested V is only reported. The untracked files present before the
   release are recorded in the manifest (`untracked_baseline`) — they are
   never version files, also on a resume. A recorded / historical / legacy
   release returns outcome `noop` with no side effect.
3. **Changelog file F** (`<project root>/<changelog_path>`, only when the
   project manages its changelog — the plan's artifact rule). The expected
   content is deterministic: the CANDIDATE commit's F + the V section
   rendered from the database export of the record's version and headed
   `## [<record version>] - <record created_at date, UTC>` (also on a
   resume); a version-only release gets `- Version-only release — no
   changelog entries.`. Existing F: the section is inserted above the first
   `## [` line, every earlier byte stays, one section more, V exactly once;
   F already holding the IDENTICAL V section → unchanged; any other V
   section → refused. Bootstrap (no F in the candidate): the full export
   without Unreleased, with the section of V exactly once. The work-tree F
   must equal the candidate's F (→ written, atomically) or the expected
   content (→ verified); anything else is a contradiction — not
   overwritten. A project without git has no candidate: F is used as found
   when it holds no V section, or must hold exactly the expected one.
4. **Version sync** — `scripts/dct-version-sync.sh` (else
   `scripts/version-sync.sh`) under the project root, run as `bash <script>
   <V>` with cwd = project root, ONLY while `current_version` is the
   record's version; a non-zero exit stops (exit code = the script's,
   nothing advanced). Changed files = staged + unstaged + new untracked
   paths vs HEAD, minus `untracked_baseline`, minus F. The project's
   version-file contract (K-73): none yet → the changed set is the
   proposal — refused unless `--confirm-version-files`, then stored
   (`set_version_files(confirm=True)`); with a contract the changed set must
   equal it exactly. Every version file must contain the record's version
   string. The manifest is recorded with `files_written`:
   `{changelog: {path, sha256, section_date}, version_files: [{path,
   sha256}], tree, untracked_baseline}` (paths repo-root-relative; for a
   project without git relative to the project root and without `tree`).
5. **Release commit** — the EXPECTED tree = the candidate's tree with
   exactly the manifest blobs, built in a temporary index (`GIT_INDEX_FILE`
   + `read-tree <candidate>^{tree}` + `hash-object -w` + `update-index
   --cacheinfo` + `write-tree`); it must equal `manifest.tree`. The real
   index is then checked: paths staged against the candidate must be
   manifest paths whose staged blob is already the expected one — a foreign
   path or foreign CONTENT inside a release file (e.g. a staged hunk in
   `pyproject.toml`) refuses and leaves the staging untouched. The pending
   artifacts are staged (`git add -- <paths>`), the real index's
   `write-tree` must equal the expected tree, then `git commit-tree
   <tree> -p <candidate> -m "release: v<V>"` and `git update-ref -m … HEAD
   <new> <candidate>` — a compare-and-swap: HEAD moves only if it is still
   the candidate (no race with another commit, no commit hooks run). The
   index already holds that tree and the work tree its files, so both are
   consistent with the new HEAD. The commit's tree and its single parent are
   verified before `committed` (with `release_commit_sha`).
6. **Tag** — `v<V>`, lightweight (like the repository's existing tags), at
   the verified release commit (tree and parent re-checked). A tag of the
   same version (any spelling) at another commit refuses; one at the
   release commit is accepted (no second tag for another spelling).
   `tagged`, then `completed`.

### Resume and interruptions

`resume` first checks the record against the checkout (K-51): the record's
`checkout_root` must be THIS checkout (another worktree → refused, the
record is never re-pinned), a git record needs its candidate and checkout,
no operation may be in progress. Then, by status:

| status | HEAD must be | what resume does |
| --- | --- | --- |
| `stamped` | the candidate | index: nothing staged or conflicted; unstaged only F and contract files (any while no contract exists). F: verified / written / contradiction (step 3); the sync is re-run (a legal retry on the candidate); version files and manifest as in step 4 |
| `files_written` | the candidate — or the release commit itself (its only parent is the candidate, its tree is `manifest.tree`, its subject `release: v<V>`), which is adopted | every manifest file must still hold its recorded hash (a reverted version file → refused, not overwritten); then step 5 |
| `committed` | the release commit or a descendant | the release commit is verified (tree = `manifest.tree`, parent = candidate); step 6. The sync is never re-run. |
| `tagged` | the release commit or a descendant | the tag must point at the release commit; `completed` |
| `pushed` | the release commit or a descendant | as `tagged` (the push itself is not repeated); `completed` |
| `completed` / `abandoned` | — | no unfinished record: outcome `noop`, no side effect |

Interruption points (`effects.INTERRUPTION_POINTS`, the `on_point` hook of
`apply_release` / `resume_release`) — a crash at each one is resumed by
`resume` exactly once (one section, one release commit, one tag; the sync
re-run only for `after_sync`):

| point | state left behind |
| --- | --- |
| `before_stamp` | nothing (no record — run `apply` again) |
| `after_stamp` | record `stamped` (+ `untracked_baseline`) |
| `before_changelog` / `after_changelog` | F not written / written; `stamped` |
| `before_sync` / `after_sync` | sync not run / run and verified; `stamped` |
| `after_files_written` | manifest recorded; no commit |
| `after_stage` | artifacts staged in the real index; no commit |
| `after_commit` | release commit created, HEAD moved; not `committed` (adopted on resume) |
| `after_committed` | no tag |
| `after_tag` | tag created; not `tagged` |
| `after_tagged` | not `completed` |

A crash between the stamp and the `untracked_baseline` write leaves a record
without a baseline: a resume of a FIRST release then counts every untracked
file as changed (each must contain V, so local junk is refused rather than
learned).

### Reconstruct (`dct release reconstruct`, K-60)

Evidence, read-only: the entries carrying V (version contract), exactly ONE
commit titled `release: v<V>` (any spelling, any ref) whose single parent is
the candidate, every tag of V pointing exactly at it, F at that commit
holding the V section exactly once with the database's entries (its date is
kept as `section_date`), and the files the commit changed besides F — equal
to the project's contract when one exists — each containing V. Any
ambiguity (several release commits, a tag missing or elsewhere, a merge as
release commit, a section mismatch, a version file without V) refuses —
never a guess. A project without git: F and the contract's files as found.
The record: mode `reconstructed`, status `completed`, the manifest above.

### JSON (`--json`, every subcommand)

One shape, `effects.RESULT_KEYS` (pinned by
`tests/test_release_effects.py::test_result_keys_are_the_documented_schema`):

| key | content |
| --- | --- |
| `schema_version` | `1` |
| `action` | `apply`, `resume`, `attest`, `abandon`, `reconstruct`, `status` |
| `project` | slug |
| `outcome` | `completed`, `noop`, `attested`, `abandoned`, `preview`, `reconstructed`, `found` / `none` (status), `refused`, `failed` (version-sync script) |
| `version` | the record's version (authoritative spelling) or null |
| `release` | the record (`core.releases.RECORD_FIELDS`) or null |
| `steps` | `[{step, result, …}]` — step ∈ `stamp`, `changelog`, `sync`, `version_files`, `files`, `commit`, `tag`, `complete`, `attest`; result ∈ `done`, `verified`, `skipped`, `adopted`, `learned` |
| `warnings` | human-readable strings |
| `details` | apply: `{requested_version, previous_version, bump, entry_ids, mode, released, current_version}`; attest: `{previous_head, head, entry_ids}`; abandon: `{updated}`; reconstruct: `{evidence: {version, entry_ids, candidate_sha, release_commit_sha, tag, checkout_root, manifest, status, without_git}}`; refused / failed: the reason (`error`) plus the refusal's data (e.g. `staged`, `unproven`, `proposed_version_files`, `foreign_paths`, `foreign_content`, `changed`, `contract`, `exit_code`) |

Exit codes: 0 success (also `noop`), 1 refused, the script's own code when
the version sync fails, 2 for a usage error.

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
| `executor` | who performs the effects (K-85): `dct` (the effects layer) or `own` (the project's own tool) — copied from the project's `release_executor` at stamp time (and at reconstruction); null on records older than the field | immutable — a later project-setting change never takes over an unfinished release |
| `status` | the state machine below | forward only |
| `release_commit_sha` | the verified release commit | set once, from `committed` on |
| `manifest` | JSON object of the verified artifacts (e.g. `changelog`, `version_files`, `tree`, `untracked_baseline`) | write-once per key; the MERGED manifest ≤ 64 KiB |
| `proof` | `{"<entry id>": {commits, merge_commit, pr}}` (K-70) | written with the stamp; a re-run passing a different map is refused (K-94 B2) |
| `consent` | what the owner approved (K-97 F4): `{version, entry_ids, candidate_sha, proposal_sha256, render, push_target: {remote, url, branch, tag} \| null}` — a resume compares its own scope with it | write-once |
| `proposal` | the approved version-file proposal (K-76): `{paths, base_blobs: {path: blob \| null}, expected_blobs: {path: blob}, script: {identity, sha256} \| null, candidate_sha}`; `consent.proposal_sha256` = `proposal_digest(proposal)` (SHA-256 of its canonical JSON) | write-once |
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
off; `dct project changelog-pr-refs <slug> on|off`).

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
| 3 effects | `dct release attest / apply / resume / abandon / reconstruct` | the default behind `/release` for projects without their own tool |

The skills of both clients only run the dialog over layers 1 and 3; the MCP
process never verifies files (it runs in its own directory, not in the
release checkout).

### Integrating a project's own release tool

A project whose own tool writes the changelog, bumps the version, commits,
tags or publishes REPLACES ONLY layer 3. It keeps the proof and the ledger:

1. **Proof** — run `dct release plan --version V --json` in the release
   checkout. Release only ids from `entries.proven` (or ids attested first
   with `dct release attest`); take `candidate_sha` and `repo_root` from
   `checkout`, and the proof map from `proof`.
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

A record the project's own tool advanced is finished by that tool: `dct
release resume` verifies the effects layer's own manifest and refuses what
it cannot verify, it does not adopt a foreign tool's effects.

