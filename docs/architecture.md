# dct — Architecture & Configuration

Database schema, migrations, the full `dct.toml` reference, MCP scope precedence, and project auto-detection. ← back to the [README](../README.md).

## Global-by-design scope model

dct is a **cross-project tool** — every surface is user-scoped, not per-project:

| Artefact | Location | Mechanism |
|---|---|---|
| Database config | platform config dir (`~/.config/dctools/dct.toml`; legacy `~/.claude/dct.toml` fallback) | TOML read by `load_config()`; `dct config path` shows the resolved location |
| Database | SQLite file or PostgreSQL | `[database].url` or `DCT_DATABASE_URL` |
| MCP server | `~/.claude.json` | `claude mcp add --scope user` |
| Skills | `~/.claude/skills/<name>` | Symlinks → `<dct-install>/dct/skills/<name>` |
| Claude hooks | `~/.claude/hooks/dct-*.sh` | Symlinks → `<dct-install>/dct/hooks/`, wired into `~/.claude/settings.json` |
| Codex hooks | `$CODEX_HOME/hooks.json` | Native definitions calling the packaged Python bridge; explicit client trust is required |

Register dct once and it is available in every Claude Code session. Opening a new project directory needs zero dct setup.

## Database Schema

SQLAlchemy Core tables (not the ORM) with a `deleted_at` column for soft deletes (ledger pattern — nothing is ever hard-deleted). 14 tables:

**Core**:
- `projects` — slug, name, path, current_version, manages_changelog, changelog_path + changelog_path_explicit (#876 K-14: the marker says whether the path is the owner's choice — `dct project changelog-path <slug> <path>`, also exactly `changelog.md`; false = "detect": `core.changelog_file` finds the tracked changelog of the project root and the default `changelog.md` is used only when none exists; `--detect` goes back to it. Added by a one-shot bootstrap block that marked stored non-default paths explicit and the historical default not), version_files / version_files_confirmed_at / version_files_history (K-73: the owner-confirmed JSON list of repo-relative files a release must change; NULL = not learned; every replaced list is appended to the history), release_executor (K-85: `dct` default \| `own`; `dct project release-executor`), changelog_pr_refs (K-90: render `(#N)` PR references, default off; `dct project changelog-pr-refs`), release_proposal (K-76: the PENDING version-file proposal of `dct release prepare` — canonical JSON, replaced by a new prepare, consumed and cleared by the stamp that approves it; NULL = nothing prepared), commit_co_authored_by / commit_session_link (the commit-trailer preferences — add a `Co-Authored-By` trailer / a session or artifact link; each NULL = not answered yet, false = a stored "no", true = a stored "yes"; asked once at the project's first configuration, set with `dct project trailers`, read by /commit through `list_projects`; a project's own rules forbidding a trailer still win over a stored "yes". Nullable, added without backfill by a self-healing bootstrap block)
- `items` — project_id, type, title, short_title (nullable 50-char label, #817), priority, status, component, source_file, source_lines, resolution, tags (**DEPRECATED** — see below). Sprint membership is **not** a column: `in_current_sprint` is derived at read time from `item_sprints` (#216 dropped the legacy boolean).
- `item_notes` — item_id, note (append-only)
- `item_checkpoints` — item_id, text, status, kind, sort_order
- `changelog_entries` — project_id, version, category, entry, component, source_file, source_lines, related_item_id, branch / worktree / branch_source (nullable git context the entry was written on, #801; `branch_source` = `client` or `server`). `version` is written ONLY by the release stamp (`release_entries`, K-102): `add_entry` refuses a version and runs under the project lock; old released history is imported with `import_released_entry` (refused for a version that has a release record)
- `session_handoffs` — project_id, prompt, context_summary, scope, created_by, consumed_at, consumed_by
- `releases` — durable release records (K-41): project_id, version (normalized), mode, entry_ids (JSON array of the exact set, `[]` = version-only), candidate_sha (immutable), checkout_root, release_commit_sha (set once), manifest (JSON object of verified artifacts, write-once per key), proof (JSON map entry id → proving commits / merge commit / PR), status (one state machine, `core/releases.TRANSITIONS`: stamped → files_written → committed → tagged → pushed → completed, plus terminal `abandoned` from any unfinished status), completed_at, abandoned_at, abandon_reason, release_mode (git \| without_git, the verified mode), executor (dct \| own, from the project at stamp time — the stamp compares it with the executor its caller expects, K-106), consent / proposal / render (write-once JSON — [release-workflow.md](release-workflow.md#the-release-ledger-record-fields)), tag_proof / push_proof (K-115 G4: the VERIFIED tag identity `{name, object, commit, annotated, recorded_at}` and push `{target, tag_commit, branch_tip, recorded_at}`, write-once JSON; NULL = not proven — a record with git cannot be `tagged` / `pushed` / `completed` without them, see [proofs](release-workflow.md#proof-of-the-tag-and-of-the-push-k-115)); one live record per (project, version) — a ledger that still holds several live records of one semantic version refuses every write of it and names them on read (K-115 G6). Written in the same transaction as the stamp (`release_entries`) — or, for a release older than records, by `reconstruct_release` without re-stamping — advanced by `core/releases.advance_release`, ended by `abandon_release`.

**Plans / sprints / decisions**:
- `plans` — ulid, project_id, slug, title, kind, source_file, source_hash, status, sprint_id, imported_at
- `plan_sections` — ulid, plan_id, parent_section_id (self-FK), heading, depth, slug, sort_order, section_type, section_type_confidence, needs_llm_review, status, sprint_id, converted_to_item_id
- `plan_checkpoints` — ulid, plan_section_id, text, status, sort_order, converted_to_item_id
- `sprints` — project_id, name, code, kind, status, goal, started_at, ended_at
- `item_sprints` — link table (item_id, sprint_id, assigned_at, removed_at)
- `decisions` — ulid, project_id, kind, ref_code, title, context, decision, consequences, alternatives, status, superseded_by, source_file, plan_id, plan_section_id, sprint_id

**Tags** (#611):
- `tags` — global tag dictionary: name (UNIQUE among active rows), description. Deliberately **no `project_id`** — the dictionary is user-scoped, so one name means the same thing in every project, while the item lists that filter on it stay project-scoped.
- `item_tags` — link table (item_id, tag_id, assigned_at, removed_at), mirroring `item_sprints`: composite PK, links are soft-removed and reactivated in place.

`items.tags` (the old JSON-array-in-TEXT column) is **deprecated and frozen**:
nothing writes it any more, `_backfill_item_tags` is its only sanctioned reader,
and it is dropped at 2.0.0.

### Migrations

Full DDL lives in:
- `migrations/001_init.sql`
- `migrations/002_sprint_and_handoff.sql`
- `migrations/003_plans_sprints_decisions.sql`
- `migrations/004_tags.sql`
- `migrations/005_items_short_title.sql`
- `migrations/006_changelog_branch.sql`
- `migrations/007_releases.sql`
- `migrations/008_release_ledger.sql`
- `migrations/009_release_ledger_fields.sql`
- `migrations/010_release_pending_proposal.sql`
- `migrations/011_release_tag_push_proof.sql`
- `migrations/012_release_preview.sql`
- `migrations/013_commit_trailer_preferences.sql`

At runtime, the engine creates every MISSING table with all its indexes (`_create_missing_tables` — race-safe against a concurrent bootstrap: a duplicate table / index / relation error rolls the attempt back, the whole schema of those tables is re-checked and finished, at most `SCHEMA_CREATE_ATTEMPTS` tries, any other error propagates; tables that already exist are left to the explicit blocks below — except the release-ledger tables (`INDEX_HEALED_TABLES`, K-93 A2), whose missing required indexes every bootstrap completes, warning once instead of failing when existing rows violate a unique index) plus an idempotent `_apply_schema_updates()` that:
- adds later columns in place (e.g. `item_checkpoints.kind` for gate checkpoints; new columns go through the duplicate-safe `_add_column_if_missing` — `ADD COLUMN IF NOT EXISTS` under a 5 s `lock_timeout` on PostgreSQL, a re-checked "duplicate column" error on SQLite),
- handles dialect-specific bool literals (PostgreSQL strict, SQLite permissive), and
- auto-migrates legacy `items.in_current_sprint=TRUE` flags into the new `item_sprints` table as a per-project "legacy sprint" (then drops the column), and
- backfills `tags` / `item_tags` from the frozen `items.tags` JSON column (`_backfill_item_tags`): tolerant parse (bad JSON is dropped with a stderr note, never raised), per-item anti-join guard so re-tagged items are never re-seeded, and a one-shot stderr similarity report suggesting `dct tags merge` for near-duplicates — a suggestion, never a block.

Schema updates run on the first engine bootstrap of a new-code session — verify destructive changes against a throwaway database and keep each step idempotent.

Read-only commands (`dct release plan` / `status`, `dct clog candidates`, the previews of `dct release reconstruct` / `takeover` / `version-files`) never bootstrap (K-84): they open the database read-only (`core.engine.readonly_engine` — SQLite `mode=ro` without a journal-mode change or file creation, PostgreSQL READ ONLY transactions), check that every table and column of the current schema exists (`db.check_schema_ready`) and otherwise refuse with a message naming `dct db upgrade`, the explicit bootstrap. Caveat: on a SQLite database already in WAL mode SQLite itself may create the `-wal` / `-shm` sidecar files for any reader, also a read-only one — the database file, its schema and its data stay unchanged.

SQLite URLs are built and parsed in ONE place (K-99): `dct.paths.sqlite_url(path)` renders through SQLAlchemy's own URL object and `dct.paths.sqlite_database_path(url)` parses with `make_url` — never by concatenating or slicing `sqlite:///`. SQLAlchemy 2.1 percent-decodes the database part of a URL and 2.0 does not, so a hand-built URL names another file on 2.1 as soon as the path holds a `%`; the helpers behave the same on both (the supported floor stays `sqlalchemy>=2.0.52`). A path the installed SQLAlchemy cannot express (a `?` on the 2.0 line) is refused instead of silently opening another file. A URL you write by hand into `dct.toml` follows the rules of the SQLAlchemy you run.

## Configuration

### The dct config file (`dct.toml`)

The full reference (all sections; `dct init` writes a subset with sensible defaults):

```toml
[database]
url = "postgresql+psycopg://localhost:5432/dct"
# or: url = "sqlite:////home/you/.local/share/dctools/dct.db"

[plans]
# Resolution is enabled; local classification rules require no model
auto_resolve_uncertain = true

[classification]
# Pluggable classifier (ADR #52; formerly [plans].classify_* — old keys
# still read as permanent aliases). provider: "rules" | "command" | "disabled"
# (other providers are future scope; unknown values behave as "disabled").
provider = "rules"
command = ["claude", "--print"]
model = "claude-haiku-4-5-20251001"
timeout_seconds = 30

[integrations.claude]
# TaskCreate/TodoWrite ↔ dct integration (formerly [mirror].task_to_dct,
# still read as a permanent alias):
#   auto — mirror every task into a session-scoped item as cc-task checkpoints
#   ask  — keep Claude Code tasks and dct separate
#   off  — disable the task-mirror hook entirely
task_mirror = "auto"

[web]
host = "127.0.0.1"       # localhost-only by design
port = 8787              # preferred port; auto_fallback scans upward if busy
auto_fallback = true
open_browser = false
refresh_poll_ms = 2000   # SSE change-token poll interval

[update]
# Passive PyPI update check (#356). When false, disables BOTH the background
# version probe and the CLI/MCP nudge. Offline-safe and non-blocking regardless.
check = true
```

**Environment overrides**: `DCT_DATABASE_URL` overrides `[database].url`;
`DCT_CONFIG_PATH` pins the config file location; `DCT_DATA_DIR` pins the data
dir. Default resolution (ADR #52): platform config dir (e.g.
`~/.config/dctools/dct.toml`) with a permanent legacy `~/.claude/dct.toml`
fallback — `dct config path` prints the resolved locations, `dct migrate-home`
moves a legacy install. The retired top-level `dct_root` key is still parsed
from old files but never written.

### Update check (`dct/update.py`)

`dct/update.py` implements a passive, offline-safe update check (#356). A cheap
`check_for_update()` reads a 24h on-disk cache (`dct-update-check.json` in the platform cache dir, e.g. `~/.cache/dctools/`);
when stale it spawns a daemon thread that queries PyPI (2s timeout) and rewrites
the cache — it never blocks and never raises. `get_cached_notice()` is a pure
cache read (zero network) that returns a one-line "newer release available" notice
when the cached `latest` is strictly newer than the installed version. CLI commands
print the notice on stderr; the MCP server appends it to its instructions at start.
`dct upgrade` detects the install method (editable / uv tool / pipx / pip), runs the
matching upgrade, and refreshes the skill/hook symlinks. Opt out via `[update] check = false`.

### MCP server registration: `~/.claude.json`

`dct init` registers the MCP server at user scope via `claude mcp add --scope user`, which writes:

```json
{
  "mcpServers": {
    "dct": {
      "type": "stdio",
      "command": "dct",
      "args": ["server"]
    }
  }
}
```

No hardcoded paths — `dct server` resolves via PATH, so the package must be installed system-wide (see the [README installation](../README.md#installation)).

**CC MCP scope precedence**: `local > project .mcp.json > user (~/.claude.json) > plugin > connector`. User-scope means dct is available in every Claude Code session regardless of CWD, but a project can still override with its own `.mcp.json`.

To re-register (e.g. after moving the dct install):

```bash
claude mcp remove dct --scope user
claude mcp add dct --scope user -- dct server
```

### Project auto-detection

Each Claude Code session spawns its own `dct server` subprocess, inheriting the session's working directory. MCP tools call `os.getcwd()` + `get_project_by_path()`, so zero-argument calls like `mcp__dct__add_changelog(category="fixed", entry="…")` auto-resolve the project from where you opened Claude Code. Pass `project=<slug>` explicitly to override.

Project paths are stored canonically (`os.path.realpath`), and detection canonicalizes both sides before matching, so CWD detection works under symlinked roots (e.g. `/tmp` → `/private/tmp`) instead of silently failing.

## Project registration & version detection

Two layers, one entry point:

- **`dct init`** — global, one-time bootstrap (MCP registration, skill/hook
  symlinks, consent-gated hook wiring). Idempotent, safe to re-run.
- **`dct project add <slug> <path>`** — per-project registration; inserts a
  projects row and auto-detects `current_version` when `--version` is absent.
- **`dct project rename <slug> [--slug X] [--name Y] [--path P]`** — in-place
  rename; `project_id` and all FKs (items, changelog, checkpoints, handoffs)
  are preserved. Slug collisions with live projects are rejected.
- **`dct project trailers <slug> [--co-authored-by on|off] [--session-link on|off]`**
  — the project's commit-trailer preferences, asked ONCE at its first
  configuration and stored with the project; /commit applies them without
  another question. Without a flag it prints the stored answers; a flag left
  out keeps that answer. Each is on, off or "not answered" (NULL).
- **`dct project version-sync-script <slug> [--kind FILE]`** — writes the
  project's `scripts/dct-version-sync.sh` from the generic template shipped
  in the package (`dct/assets/dct-version-sync.sh`, every block commented)
  with the block of its version file switched on (default: the detected
  one). Never overwrites an existing script (`dct/core/version_sync.py`).

The `/dct-init` skill orchestrates both layers interactively (probes bootstrap
state, permissions allowlist, project-local conflicts, version-sync scripts,
then registers the CWD project) — it never re-implements the CLI, only drives it.

Version detection order (first hit wins): `VERSION` file →
`pyproject.toml` `[project].version` → `package.json` → `Cargo.toml` →
`src-tauri/tauri.conf.json`. Malformed files are skipped silently so
registration never fails on unrelated parse errors. No match: a tty gets an
interactive prompt (`0.0.1 / 0.1.0 / 1.0.0` shortcuts, default `0.1.0`);
non-tty registers `current_version=NULL` with a warning to run
`dct project set-version <slug> <v>` later (a NULL version would otherwise
crash `/release` much later — the detect-or-ask flow closes that trap).
Helper: `dct/core/version.py::detect_version`; tests:
`tests/test_version.py::TestDetectVersion`.

## Workflow layer (`dct/workflows`)

`dct/workflows/session.py` hosts the G1 composed ops (#413):
`get_context_snapshot`, `pickup_work`, `handoff_work` — ONE implementation
consumed by the MCP tools, the CLI (`dct pickup` / `dct handoff create`) and
the packaged /pickup + /handoff skills. `dct/services/` stays reserved for
future shared application ops.

Conventions the layer is built on:

- **Conn-level privates in core.** Public core functions keep their
  `fn(engine, ...)` signatures; each extracted a `_fn(conn, ...)` private
  (e.g. `_create_handoff_conn`, `_set_current_sprint_items_conn`,
  `_consume_handoff_conn`, the `_query_*` readers). Composed ops open ONE
  `engine.begin()` and call the privates, so every op is a single
  transaction: `handoff_work` writes sprint membership FIRST and the handoff
  second — a handoff validation failure rolls the sprint write back.
  Validation lives inside the conn privates so no caller can bypass it.
- **Structured data only.** Snapshots carry facts (sprint state, handoffs,
  gates, decisions, recent changes) — never interpretation like
  `recommended_action`; that belongs to the consuming skill/assistant. A
  future interpretive layer arrives additively (`annotate_snapshot`), never
  as a base-shape change.
- **Multi-sprint semantics.** The single-active-sprint cap is lifted:
  several sprints may be active; the "current" one is the PRIMARY (most
  recently started, id tie-break). Ops needing one unambiguous target
  (`handoff_work`) take an explicit `sprint` selector and raise a conflict
  error listing candidates instead of guessing.
- **`review_sprint` is preserved.** The legacy dashboard keeps its exact
  shape (web + tests depend on it) and shares the same conn-level readers,
  so the queries stay single-sourced; deprecation is a later-group decision.

## PostgreSQL setup

SQLite (the default) needs zero setup. To use PostgreSQL instead:

```bash
uv pip install -e '.[pg]' --python .venv/bin/python   # install the psycopg extra
createdb dct                                           # create the database
dct init                                               # choose "postgresql", enter the URL
```
