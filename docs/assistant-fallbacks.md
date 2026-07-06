# Assistant fallback workflows

Skills in `skills/` are **the preferred** way to drive dct from Claude Code.
But skill names can be renamed, disabled per-session, or missing in future
versions. Do not couple logic to skill existence — the workflows below
describe what each skill does so an assistant can perform the steps manually
via MCP tools when the named skill is unavailable.

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
`skills/` are shortcuts. If they diverge — fix the skill, not this doc.
