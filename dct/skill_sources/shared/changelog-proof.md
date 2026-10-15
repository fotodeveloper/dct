## Changelog proof: trailers and range (shared by `{{host.skill-prefix}}commit` and `{{host.skill-prefix}}release`)

A changelog entry is **proven** for a release when a commit in the release
range names its id in a `Changelog:` trailer. `{{host.skill-prefix}}commit` writes the trailers,
the release workflow reads them. The rules are implemented ONCE in code —
`dct/workflows/release/proof.py` (trailers, reverts, squash signals) and
`dct/workflows/release/plan.py` (range, provenance, candidates) — and
computed read-only by two commands whose JSON schemas, range rule (K-71),
revert rule (K-68), squash heuristic and its limits (K-69) and provenance
(K-70) are documented in [release-workflow.md](release-workflow.md):

- `dct release plan [-P slug] [--version V | --patch|--minor|--major]
  [--entries <ids> | --no-entries] --json` — the release plan of the
  current checkout; on the CLI path its `snapshot` / `snapshot_digest` is
  what the owner approves (K-80) — `{{host.skill-prefix}}release` reads it for routing and
  approves the preview's screen instead. No side effects.
- `dct clog candidates [-P slug] --json` — `{{host.skill-prefix}}commit`'s candidates:
  `confirmed` vs `uncertain`, minus the ids the range already proves, plus
  the staged file list.

The essentials an assistant must know to write and read proof (the code is
authoritative):

- **Trailer format** (written by `{{host.skill-prefix}}commit`): one line `Changelog: <id>, <id>`
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
- **Reverts**: `{{host.skill-prefix}}commit` writes reverts with git's standard message
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
  candidate (in `{{host.skill-prefix}}release`: the owner's "Release with all N" or "Change
  something" answer, and `apply --consent` makes the same commit).
