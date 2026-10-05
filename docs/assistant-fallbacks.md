# Assistant fallback workflows

Skills in `dct/skills/` are **the preferred** way to drive dct from Claude Code.
But skill names can be renamed, disabled per-session, or missing in future
versions. Do not couple logic to skill existence — the workflows below
describe what each skill does so an assistant can perform the steps manually
via MCP tools when the named skill is unavailable.

**Write ops return a minimal ack**, not the full record — the created/updated
`id` plus a few post-state scalars. **Remember the `id`** returned by
`create_item` / `add_checkpoint` / `create_handoff` (etc.); you need it for
every follow-up call. Fetch full context deliberately: `get_item(id)` (fields,
notes, checkpoints, progress) or `get_handoff(id)` (untruncated prompt +
context_summary). `list_handoffs` returns 200-char prompt previews — use
`get_handoff(id)` for the full text.

## `/pickup` fallback

The skill is TWO-PHASE — a read, a dialog, then (only if the user says so)
the consume. Without the skill:

1. `pickup_work(project="", consume="false")` — a PURE READ, no `consumed_by`
   needed and nothing written. `handoff.latest` carries the newest unconsumed
   handoff with its FULL prompt; `handoff.others` are 200-char previews;
   `handoff.pending_count` says how many wait.
2. Show the prompt and let the USER decide: consume and follow / follow but
   keep it unconsumed (audit) / start fresh — and, with several handoffs,
   which one.
3. On "consume":
   `pickup_work(project="", consume="true", handoff_id=N, include="none", consumed_by="<agent> @ <slug> <iso_utc>Z")`
   — `include="none"` skips the snapshot re-read (step 1 already has it).
4. Read the next step from `sprint.checkpoints` (first `pending` row of the
   chosen item); honor `gates` and `decisions` before acting.
5. CLI equivalent: `dct pickup [--json]` (its `--consume` default is `never`),
   `dct pickup --consume always --handoff-id N` for the write.

Never auto-pick among several handoffs; never call `consume_handoff` here —
`pickup_work` owns the consume. `consume=""` (auto) and `--consume always`
belong to AUTONOMOUS flows (CI, unattended agents) where nobody can be asked.
If a parallel session wins the race, a server-selected consume comes back
`consumed: false` (proceed with the fresh snapshot); an explicit `handoff_id`
errors "not found or already consumed" — report it, don't retry in a loop.

## `/handoff` fallback

The skill is preflight + dialogs + one atomic call. Without the skill:

1. `get_context_snapshot(project="")` — sprint/gates/decisions feed the draft.
2. Confirm the membership change + prompt text with the user. Sprints are
   MULTI-SESSION (ADR #57): the default is "sprint untouched", the normal edge
   is ADDITIVE (add discovered items / cut scope), and a full replace is
   conscious replanning.
3. `handoff_work(prompt=..., created_by="<agent> @ <slug> <iso_utc>Z", project="",
   item_ids="__keep__" (default) | "22,21,6" (replace), add_item_ids="22,6",
   remove_item_ids="7", sprint=<id-or-code when several active>, scope=...)` —
   one transaction; a failed sprint write leaves no handoff. Replace and the
   additive pair are mutually exclusive. **Never `item_ids=""`** — an empty
   replace errors; emptying a sprint is `clear_sprint`'s job. Unknown item ids
   abort everything (strict), removing a TERMINAL member errors (resolved work
   is the sprint's history), and an ambiguous / membership-less `sprint`
   selector errors too.
4. "No prompt" case: `handoff_work` requires a prompt — with ≤ 1 active sprint
   fall back to `set_sprint(project, item_ids=...)`; with ≥ 2 active sprints
   `set_sprint` errors (current-sprint writes refuse to guess), so target the
   chosen sprint via `assign_item_to_sprint(item_id, sprint_id)` /
   `remove_item_from_sprint(item_id, sprint_id)`.
5. CLI equivalent: `dct handoff create --prompt-file - --add-items 22,6 --scope g1`
   (`--remove-items` cuts scope, `--items` replaces; omit all three to keep the
   sprint).

## Changelog proof: trailers and range (shared by `/commit` and `/release`)

A changelog entry is **proven** for a release when a commit in the release
range names its id in a `Changelog:` trailer. `/commit` writes the trailers,
`/release` reads them; both use exactly these rules.

**Trailer format (written by `/commit`).** One line `Changelog: <id>, <id>` —
ids ascending, separated by comma + space — in the final trailer block of the
message, before `Co-Authored-By:`. Release commits (`release: vX.Y.Z`) carry
no `Changelog:` trailer.

**Range `R` for a target commit `SHA`.** Let `CUR` = the project's
`current_version` (from `list_projects`).
- If the tag exists (`git rev-parse -q --verify "refs/tags/v$CUR"`) AND is an
  ancestor of the target (`git merge-base --is-ancestor "v$CUR" "$SHA"`):
  `R = v$CUR..$SHA`.
- Otherwise: `R = --since=<oldest unreleased entry's created_at minus 1 day> $SHA`,
  and WARN that the boundary is approximate.

**Reading proof for `R`** — the union of:
1. git's trailer parser:
   `git log --format='%(trailers:key=Changelog,valueonly,separator=%x2C)' R`;
2. a strict full-body scan: `git log --format=%B R`, keeping ONLY lines that
   match `^Changelog: [0-9]+(, [0-9]+)*$` exactly. This covers squash merges
   that keep the original commit messages inside the body; quoted or bulleted
   prose (`> Changelog: 12`, `- Changelog: 12`) never counts.

A squash configured as "PR title and description" drops the commit bodies —
the trailers are lost, and those entries need an attestation commit (below).
An entry whose trailer lies below the previous tag is "without proof in range".

**Attestation commit (created only by `/release`).** The only way to add proof
after the fact: once the owner confirms the entries' changes are in the
candidate, an empty commit on the candidate branch whose last paragraph is the
trailer block (`Changelog:` first, then the usual `Co-Authored-By:`):

```bash
git commit --allow-empty -m "$(cat <<'MSG'
chore: attest changelog entries

Changelog: 12, 15
MSG
)"
```

## `/release` fallback

1. **Capability probe.** Load the schemas (`list_projects`,
   `count_unreleased_changelog`, `list_changelog`, `release_changelog`,
   `export_changelog`, `update_changelog`, `delete_changelog`). The loaded
   `release_changelog` schema MUST list `entry_ids`; otherwise STOP: "The dct
   MCP server is older than this skill — restart it (or upgrade dct) before
   releasing." (an old server silently ignores unknown arguments and would
   stamp every entry).
2. Detect current project: match CWD against `mcp__dct__list_projects()` results, take longest path prefix.
3. **Unsafe git state stops the flow:** a merge / rebase / cherry-pick in
   progress — resolve each marker with `git rev-parse --git-path MERGE_HEAD`
   / `rebase-merge` / `rebase-apply` / `CHERRY_PICK_HEAD` (worktree-safe) and
   test whether the path exists. A dirty tree is NOT a stop: commit the WIP
   first as its own commit(s), each following the `/commit` trailer rule.
4. Pin the candidate `SHA=$(git rev-parse HEAD)`, compute `R`, read the proof
   (rules above), and split `list_changelog(project, unreleased_only=true)`
   into entries **with proof in range** and **without proof**.
5. For EACH entry without proof (entries with a null branch included) the
   owner chooses: (i) **attest** — confirm the change is in the candidate,
   then create the attestation commit; (ii) **correct** it
   (`update_changelog`); (iii) **withdraw** it (`delete_changelog`); (iv)
   **leave it unreleased**. There is no "include without proof" option.
6. After every commit the flow itself creates (WIP, attestation): re-pin `SHA`
   and re-scan `R`.
7. Ask via AskUserQuestion: `patch` | `minor` | `major` | explicit version.
   Compute `V` ONCE from that choice; every retry reuses the same `V`.
8. Confirm the proven set. When it is empty, ask explicitly whether to release
   the version only (no entries).
9. `mcp__dct__release_changelog(project, version=V, entry_ids=[<proven ids>])`
   (`entry_ids=[]` for a version-only release) — always an EXPLICIT version,
   never `bump_type`; repeating the same call is a no-op. Check that the
   answer's `mode`, `entry_ids` and `released` (plus `already_released` on a
   retry) match the selection; STOP on mismatch.
10. Render ONLY the just-released version's section: `mcp__dct__export_changelog(project, version=V)` (headerless, single-version). If it returns an `{"error": ...}` dict, STOP (the DB was already stamped in step 9 — fix the export and re-run step 10, or revert the stamp). `""` is expected only for a version-only release: the section is then `## [V] - YYYY-MM-DD` followed by the line `- Version-only release — no changelog entries.` (note: `since=last_release` does not move, and a full export omits V).
11. **Append-only:** resolve the project's changelog file `F = {root}/{changelog_path}`, where `root` = the CURRENT checkout's toplevel (`git rev-parse --show-toplevel`) when it lies inside `project.path`, else `project.path` (`changelog_path` comes from the step 2 `list_projects` result, default `changelog.md`; authoritative cross-check `dct project changelog-path <slug>` prints the resolved path). PREPEND that section to `F`, directly below the `# Changelog` header (above the previous top version) — NEVER overwrite/regenerate the whole file (a full-DB export drops pre-adoption history). Bootstrap exception: if `F` does not exist yet, write the full `export_changelog(project, include_unreleased=false)`. Mandatory safety check: re-read `F` and confirm the count of lines starting with `## [` (anchored to line start) rose by exactly 1 — one section added, zero lost — before committing.
12. If `{root}/scripts/dct-version-sync.sh` exists, run it with the new version. This bumps `pyproject.toml`/`Cargo.toml`/`package.json`/`tauri.conf.json`/etc. per the project-specific template. dct itself bumps `pyproject.toml` + `dct/__init__.py` fallback `__version__`.
13. Stage the changelog file `F` + any files modified by the sync script; commit with literal message `release: vX.Y.Z` — no scope, no description, no suffix, no `Changelog:` trailer.
14. **Tag** the release: `git tag vX.Y.Z` on HEAD. The hook's release bypass already let the commit through; the tag is the canonical marker for git describe / GitHub / PyPI.
15. Tell the user the tag is **local only** — pushing to remote is a separate step they must confirm (`git push origin vX.Y.Z` or `git push --tags`).

## `/commit` fallback

1. Pre-flight: run in parallel — `git status`, `git diff --cached --stat`, `git diff --stat`, `git log --oneline -5`, `git branch --show-current`, `git rev-parse --show-toplevel`.
2. Detect project via CWD.
3. `mcp__dct__count_unreleased_changelog(project, branch=<current>, include_unbranched=true)` (detached HEAD → project-wide count, no `branch`) — if 0 and non-exempt files staged, prompt user to run `/clog` or add entry inline via `mcp__dct__add_changelog`. Exempt paths decide only whether an entry is REQUIRED; attaching an existing matching entry is always allowed.
4. **Trailer selection.** Candidates = `mcp__dct__list_changelog(project, unreleased_only=true, branch=<current>)` rows whose `worktree` equals the current toplevel or is null, minus the ids already proven in `R` (target `SHA` = `HEAD`; rules above). Against the FINAL staged diff (`git diff --cached`), keep the candidates whose entry text / `source_file` describe what is being committed; ask when unsure. Never attach an entry with a null `branch` automatically. No candidates, but unreleased entries of THIS worktree carry another branch → offer `update_changelog(entry_id, branch=<current>)`.
5. Determine commit type + scope + description. Read `current_version` from project.
6. Stage files explicitly (path by path). Never `git add -A`. Never stage `.env`, credentials, binaries.
7. HEREDOC commit: `<type>(<scope>): <description> (v<version>)`; the final trailer block is `Changelog: <ids>` (when any were selected) followed by `Co-Authored-By:`.
8. Verify: `git log --oneline -3`, report the commit hash and the attached changelog ids.
9. On hook rejection: fix the reported issue, re-stage, recompute the trailer selection against the new staged diff, create a NEW commit (never `--amend`, never `--no-verify`).

## `/track` (create item) fallback

1. Determine project (CWD detect or ask).
2. Gather: item_type, title, `short_title` (the label: a 2-4 word English noun phrase naming what the item is about, ≤ 50 chars, no item number, no project name — e.g. "Windows hook paths"), priority, description, component, tags (optional).
3. Present draft to user (the label shown as "Label"), confirm.
4. `mcp__dct__create_item(item_type, title, project, short_title, ...)` — **REMEMBER the returned `id`**. The ack must echo `short_title`; if it does not, the MCP server is outdated — tell the user to restart it instead of claiming the label was saved.
5. For feature/improvement items: break work into 3-7 checkpoints via `mcp__dct__add_checkpoint(item_id, text)` — each one concrete and actionable. **REMEMBER each returned `id`**.
6. Report to user: `#<id> <type>/<priority> [<short_title>] — <title>` plus checkpoint count.

## `/clog` (changelog entry) fallback

1. Determine project (CWD detect or ask).
2. Gather: category (added/changed/fixed/removed), entry text, component, source_file, source_lines, related_item_id (the last four optional).
3. In the agent's CURRENT working directory read `git branch --show-current` (empty = detached → omit `branch`) and `git rev-parse --show-toplevel` → `branch` / `worktree`. Pass them explicitly: the MCP server's directory is the session start directory, which may be another checkout.
4. Present draft, confirm.
5. `mcp__dct__add_changelog(category, entry, project, component, source_file, source_lines, related_item_id, branch, worktree)`.
6. Report: `✓ clog #<id> [<category>] — <entry>`.

## `/track-resolve` (resolve/close item) fallback

1. Parse ID(s) from user input. Accept: `#42`, `42`, `ITEM-42`, `0042` — normalize via regex `^(#|ITEM-|item-)?0*(\d+)$`.
2. For each id: `mcp__dct__get_item(id)` — show current state, any unfinished checkpoints.
3. Determine resolution status: `resolved` (fixed/implemented) or `wont_fix` (scope decision).
4. Build resolution note: "Fixed in commit <hash>" if recent commit, or "Decision: <reason>" from context, else ask user.
5. If item has pending checkpoints, ask whether to `reject` them (preserved in ledger) or proceed anyway.
6. `mcp__dct__resolve_item(id, status, resolution)` — set terminal status.
7. Report: `✓ #<id> → <status>`.

These prose descriptions are the authoritative behavior. Skill files in
`dct/skills/` are shortcuts. If they diverge — fix the skill, not this doc.
