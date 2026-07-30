# dct — Deterministic Context Tracker

Cross-project work tracking tool for Claude Code. Items, changelog, notes — all in one database.

Deep-dives live in `docs/` and are read on demand — keep this file lean:
[architecture & config](docs/architecture.md) · [plans subsystem](docs/plans.md) ·
[hooks](docs/hooks.md) · [MCP tool catalogue](docs/mcp-tools.md) ·
[assistant fallbacks](docs/assistant-fallbacks.md) · [maintainer notes](docs/maintainers.md)

## Stack

- **Python 3.11+**, SQLAlchemy Core (NOT ORM), argparse
- **Database**: PostgreSQL (primary) or SQLite (fallback/OSS)
- **MCP**: FastMCP via `mcp[cli]`, **user-scoped** (`~/.claude.json`)
- **Config**: `~/.config/dctools/dct.toml` (legacy `~/.claude/dct.toml` still read; `dct migrate-home` moves it)

## Commands

```bash
# Development
.venv/bin/pytest -v                    # Run tests (in-memory SQLite)
.venv/bin/dct --version                # Check version
uvx ruff@0.16.0 check dct/ tests/     # Lint (config in pyproject.toml; pin matches ci.yml — K-46)

# MCP server (smoke-test — boots, lists tools)
uv run --no-project --with 'mcp[cli]' --with sqlalchemy --with 'psycopg[binary]' \
  --with python-ulid python -c "from dct.mcp.server import mcp; print(len(mcp._tool_manager._tools), 'tools')"
```

## Architecture

```
dct/config.py            — Config loading (platform config dir + legacy
                           ~/.claude fallback + env vars; ADR #52)
dct/paths.py             — Platform-neutral config/data/cache/state dirs
                           (XDG on POSIX, platformdirs on Windows)
dct/migrate.py           — dct migrate-home (legacy → platform-neutral home)
dct/db.py                — SQLAlchemy Core tables + engine factory + migrations
dct/core/projects.py     — Projects CRUD (+ realpath-canonical CWD detection)
dct/core/items.py        — Items / notes / checkpoints CRUD
dct/core/changelog.py    — Changelog entries CRUD + release stamping
dct/core/sprint.py       — Current-sprint ops; delegates to item_sprints
dct/core/sprints.py      — First-class sprints (single source of truth)
dct/core/handoff.py      — Session handoffs (cross-session prompt ledger)
dct/core/decisions.py    — ADR / sprint-amendment / open-question records
dct/core/version.py      — Version bump + project-file autodetect
dct/core/checkpoints.py  — Item checkpoint helpers
dct/core/ids.py          — ULID generation
dct/core/plans/          — Plans subsystem: parser.py (pure MD parser),
                           classify.py (claude --print), crud.py, ingest.py
dct/services/            — Service layer (reserved for future shared app ops)
dct/workflows/session.py — G1 composed ops: get_context_snapshot /
                           pickup_work / handoff_work (one impl for MCP,
                           CLI and skills; single transaction each)
dct/mcp/server.py        — FastMCP MCP server (52 tools); dct/server.py is
                           a compat shim
dct/cli.py               — argparse CLI entry point
dct/export.py            — Keep a Changelog MD export
dct/integrations/claude.py — Claude Code adapter: MCP registration, skill/hook
                           symlinks, settings.json wiring (`dct hooks wire`);
                           dct/setup.py is a compat shim
dct/assets/              — Indirection for packaged asset dirs (physical
                           skills/ + hooks/ stay put until 2.0.0 — host
                           symlinks point into them)
dct/web/                 — Read-only Flask dashboard (dct web)
dct/skills/              — Packaged Claude Code skills (symlinked by dct init)
dct/hooks/               — Packaged Claude Code hooks (symlinked by dct init)
```

Invariants:

- Core functions take `engine` as first parameter (no global state) and return dicts.
- All tables have `deleted_at` — soft deletes only (ledger pattern).
- Every surface is **user-scoped** (one install serves all projects; details
  in [docs/architecture.md](docs/architecture.md)). Plans keep prose in
  markdown, state in DB, identity in ULID anchors
  ([docs/plans.md](docs/plans.md)).

## Conventions

- Code and docs: English
- Communication: match the user's language
- Commit format: conventional commits (`feat:`, `fix:`, `refactor:`)
- All changes need a changelog entry before committing
- Tests: pytest with in-memory SQLite fixture

## Hook conventions

Hooks (`dct/hooks/*.sh`) run without a tty and must never hang or block the host
operation. The five rules — full versions with code samples in
[docs/hooks.md](docs/hooks.md):

1. No interactive prompts without a tty check.
2. Consume stdin (Claude Code supplies JSON there).
3. Graceful degradation when dependencies are missing (`command -v dct || exit 0`).
4. Exit codes carry meaning: `0` pass-through, `2` block with structured reason.
5. Strip `-m "message"` content before inspecting commit flags.

Reference implementation: `dct/hooks/check-changelog.sh`.

## MCP tool conventions

FastMCP exposes each tool's **docstring verbatim as the tool description** the
calling model reads — the docstring IS the schema the model fills in. Every
`@mcp.tool()` docstring MUST:

- **Document every parameter with its role** — no undocumented signature
  parameters, no documented ghosts.
- **Make the call structure obvious** — for multi-field free-text tools, state
  that each field is a SEPARATE top-level parameter. Name enums and defaults.
- **Describe the return shape** — `{cols, rows}` (list views) vs full semantic
  dict (`get_*`) vs `{"error": ...}` (and what triggers it).
- **Stay in sync with the code, in the same change.** A stale docstring is
  worse than none — a past audit found 7 such drifts; this is a real,
  recurring failure mode.

House style: terse, information-dense. Exemplars: `create_decision`,
`create_item`, `add_changelog`, `update_item`.

## Bug-find protocol

When any bug, API surprise, or inconsistency is discovered during work, patch
the **system**, not the call-site — three steps in the same turn:

1. **Fix the root cause in code** so the pathological state cannot recur.
2. **Update documentation** (docstring, this file, SKILL.md, or tool description).
3. **Add a regression test** locking in the invariant.

Red flag: calling one function to fix what another should have done correctly
(e.g. following `update_item(status="resolved")` with `resolve_item` to get
`resolved_at` stamped). Double-invocation patterns are defects, not workflows.

## Gate checkpoints

`item_checkpoints.kind` defaults to `'task'` (non-blocking). Any other kind
except `'cc-task'` is a **gate**: `resolve_item` refuses to close the item
while a gate is `pending` — complete it (done) or reject it (consciously
skipped, ledger-preserved). Known kinds: `dogfood` (user validates a
user-facing feature in a fresh session), `security`, `approval`, `review`;
unknown kinds are conservatively treated as gates.

Rule: every new user-facing feature item ends with a `kind='dogfood'`
checkpoint. Exceptions: internal refactors/docs/CI (no user-facing surface),
bug fixes with a regression test (the test IS the validation), and features
behind a not-yet-enabled flag (gate deferred until the flip). When in doubt,
add the gate — rejecting is always available.

## OSS hygiene

Never commit author-specific data: private project slugs, personal brand
names, home-directory paths, private version strings. Use placeholders
(`proj-a`, `My Project`, `/tmp/proj-a`, generic semver) from the first
keystroke. Sniff-regex, exemptions (`changelog.md`) and release-time
enforcement: [docs/maintainers.md](docs/maintainers.md).

## Skill fallbacks

Skills in `dct/skills/` are the preferred way to drive dct, but assistants must
not couple logic to skill existence. Step-by-step MCP-only equivalents for
`/release`, `/commit`, `/track`, `/clog`, `/track-resolve` live in
[docs/assistant-fallbacks.md](docs/assistant-fallbacks.md) — those prose
descriptions are the authoritative behavior; if a skill diverges, fix the skill.
