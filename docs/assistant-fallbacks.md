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

## `/release` fallback

1. Detect current project: match CWD against `mcp__dct__list_projects()` results, take longest path prefix.
2. `git status --porcelain` — if non-empty, STOP and tell user to commit WIP first (`/commit`).
3. `mcp__dct__count_unreleased_changelog(project)` — if 0, inform "no unreleased entries" and stop.
4. `mcp__dct__list_changelog(project, unreleased_only=true)` — present to user.
5. Ask user via AskUserQuestion: `patch` | `minor` | `major` | explicit version.
6. `mcp__dct__release_changelog(project, version=<explicit>)` or `(project, bump_type=...)` — stamps entries with version and updates `current_version`.
7. Render ONLY the just-released version's section: `mcp__dct__export_changelog(project, version=<V>)` (headerless, single-version). If it returns an `{"error": ...}` dict or `""`, STOP (the DB was already stamped in step 6 — fix the export and re-run step 7, or revert the stamp).
8. **Append-only:** resolve the project's changelog file `F = {project.path}/{changelog_path}` (`changelog_path` comes from the step 1 `list_projects` result, default `changelog.md`; authoritative cross-check `dct project changelog-path <slug>` prints the resolved path). PREPEND that section to `F`, directly below the `# Changelog` header (above the previous top version) — NEVER overwrite/regenerate the whole file (a full-DB export drops pre-adoption history). Bootstrap exception: if `F` does not exist yet, write the full `export_changelog(project, include_unreleased=false)`. Mandatory safety check: re-read `F` and confirm the count of lines starting with `## [` (anchored to line start) rose by exactly 1 — one section added, zero lost — before committing.
9. If `{project.path}/scripts/dct-version-sync.sh` exists, run it with new version. This bumps `pyproject.toml`/`Cargo.toml`/`package.json`/`tauri.conf.json`/etc. per the project-specific template. dct itself bumps `pyproject.toml` + `dct/__init__.py` fallback `__version__`.
10. Stage the changelog file `F` + any files modified by the sync script; commit with literal message `release: vX.Y.Z` — no scope, no description, no suffix.
11. **Tag** the release: `git tag vX.Y.Z` on HEAD. The hook's release bypass already let the commit through; the tag is the canonical marker for git describe / GitHub / PyPI.
12. Tell the user the tag is **local only** — pushing to remote is a separate step they must confirm (`git push origin vX.Y.Z` or `git push --tags`).

## `/commit` fallback

1. Pre-flight: run in parallel — `git status`, `git diff --cached --stat`, `git diff --stat`, `git log --oneline -5`.
2. Detect project via CWD.
3. `mcp__dct__count_unreleased_changelog(project)` — if 0 and non-exempt files staged, prompt user to run `/clog` or add entry inline via `mcp__dct__add_changelog`.
4. Determine commit type + scope + description. Read `current_version` from project.
5. Stage files explicitly (path by path). Never `git add -A`. Never stage `.env`, credentials, binaries.
6. HEREDOC commit: `<type>(<scope>): <description> (v<version>)` with `Co-Authored-By:` trailer.
7. Verify: `git log --oneline -3`, report commit hash.
8. On hook rejection: fix the reported issue, re-stage, create a NEW commit (never `--amend`, never `--no-verify`).

## `/track` (create item) fallback

1. Determine project (CWD detect or ask).
2. Gather: item_type, title, priority, description, component, tags (optional).
3. Present draft to user, confirm.
4. `mcp__dct__create_item(item_type, title, project, ...)` — **REMEMBER the returned `id`**.
5. For feature/improvement items: break work into 3-7 checkpoints via `mcp__dct__add_checkpoint(item_id, text)` — each one concrete and actionable. **REMEMBER each returned `id`**.
6. Report to user: `#<id> <type>/<priority> — <title>` plus checkpoint count.

## `/clog` (changelog entry) fallback

1. Determine project (CWD detect or ask).
2. Gather: category (added/changed/fixed/removed), entry text, component (optional), related_item_id (optional).
3. Present draft, confirm.
4. `mcp__dct__add_changelog(category, entry, project, component, related_item_id)`.
5. Report: `✓ clog #<id> [<category>] — <entry>`.

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
