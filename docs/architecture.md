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
| Hooks | `~/.claude/hooks/dct-*.sh` | Symlinks → `<dct-install>/dct/hooks/`, wired into `~/.claude/settings.json` |

Register dct once and it is available in every Claude Code session. Opening a new project directory needs zero dct setup.

## Database Schema

SQLAlchemy Core tables (not the ORM) with a `deleted_at` column for soft deletes (ledger pattern — nothing is ever hard-deleted). 12 tables:

**Core**:
- `projects` — slug, name, path, current_version, manages_changelog, changelog_path
- `items` — project_id, type, title, priority, status, component, source_file, tags, resolution, in_current_sprint (legacy boolean, derived from `item_sprints`)
- `item_notes` — item_id, note (append-only)
- `item_checkpoints` — item_id, text, status, kind, sort_order
- `changelog_entries` — project_id, version, category, entry, component, source_file, related_item_id
- `session_handoffs` — project_id, prompt, context_summary, scope, created_by, consumed_at, consumed_by

**Plans / sprints / decisions**:
- `plans` — ulid, project_id, slug, title, kind, source_file, source_hash, status, sprint_id, imported_at
- `plan_sections` — ulid, plan_id, parent_section_id (self-FK), heading, depth, slug, sort_order, section_type, section_type_confidence, needs_llm_review, status, sprint_id, converted_to_item_id
- `plan_checkpoints` — ulid, plan_section_id, text, status, sort_order, converted_to_item_id
- `sprints` — project_id, name, code, kind, status, goal, started_at, ended_at
- `item_sprints` — link table (item_id, sprint_id, assigned_at, removed_at)
- `decisions` — ulid, project_id, kind, ref_code, title, context, decision, consequences, alternatives, status, superseded_by, source_file, plan_id, plan_section_id, sprint_id

### Migrations

Full DDL lives in:
- `migrations/001_init.sql`
- `migrations/002_sprint_and_handoff.sql`
- `migrations/003_plans_sprints_decisions.sql`

At runtime, the engine applies schema via `metadata.create_all()` plus an idempotent `_apply_schema_updates()` that:
- adds later columns in place (e.g. `item_checkpoints.kind` for gate checkpoints),
- handles dialect-specific bool literals (PostgreSQL strict, SQLite permissive), and
- auto-migrates legacy `items.in_current_sprint=TRUE` flags into the new `item_sprints` table as a per-project "legacy sprint".

Schema updates run on the first engine bootstrap of a new-code session — verify destructive changes against a throwaway database and keep each step idempotent.

## Configuration

### The dct config file (`dct.toml`)

The full reference (all sections; `dct init` writes a subset with sensible defaults):

```toml
[database]
url = "postgresql+psycopg://localhost:5432/dct"
# or: url = "sqlite:////home/you/.local/share/dctools/dct.db"

[plans]
# LLM classification of uncertain plan sections — always-on by default
auto_resolve_uncertain = true

[classification]
# Pluggable classifier (ADR #52; formerly [plans].classify_* — old keys
# still read as permanent aliases). provider: "command" | "disabled"
# (other providers are future scope; unknown values behave as "disabled").
provider = "command"
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

## PostgreSQL setup

SQLite (the default) needs zero setup. To use PostgreSQL instead:

```bash
uv pip install -e '.[pg]' --python .venv/bin/python   # install the psycopg extra
createdb dct                                           # create the database
dct init                                               # choose "postgresql", enter the URL
```
