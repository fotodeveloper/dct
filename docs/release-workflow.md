# Release workflow — the proof layer (`dct release plan`, `dct clog candidates`)

The release workflow is layered (K-72):

1. **Proof layer** (this document) — `dct release plan --json` and
   `dct clog candidates --json`. Read-only: no database write, no ref, index
   or work-tree change (every git read runs with `--no-optional-locks`).
   Usable by every project, also one whose own release tool performs the
   effects.
2. **Ledger layer** — core / MCP: `release_changelog`, `get_release`,
   `advance_release` (`dct/core/releases.py`).
3. **Effects layer** — `dct release apply/resume` (changelog file, version
   files, release commit, tag), the default behind `/release`.

Implementation: `dct/workflows/release/proof.py` (pure rules over commit
messages) and `dct/workflows/release/plan.py` (git + database reads). The
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
