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
