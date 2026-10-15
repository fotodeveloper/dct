# dct — Deterministic Context Tracker

Cross-project work tracking for MCP clients, with Claude Code and Codex adapters.
Items, changelog, notes — all in one database.

This file holds the project rules for every agent working in this repository.
Codex and Claude Code read it natively; keep it the only project instruction
file — never add a CLAUDE.md, and a private CLAUDE.local.md only with the
setting in [CONTRIBUTING.md](CONTRIBUTING.md): otherwise Claude Code skips
this file.

## Read on demand

- Before an architecture change, a new module or new shared logic, read
  `docs/architecture.md` (module map, schema, migrations, config).
- Before changing anything under `dct/hooks/**`, read `docs/hooks.md`.
- Before running the PostgreSQL test lane or changing test isolation, read
  `docs/testing.md`.
- Before working on plans (parser, ingest, anchors), read `docs/plans.md`.
- Before adding or changing an MCP tool, read `docs/mcp-tools.md`.
- Before changing a skill or its MCP-only fallback, read
  `docs/assistant-fallbacks.md`.
- Before changing an integration adapter, read `docs/integrations.md`.
- Before changing the release workflow (`dct/workflows/release/`,
  `dct/core/releases.py`), read `docs/release-workflow.md`.
- Before public-facing work or a release (OSS hygiene), read
  `docs/maintainers.md`.

## Stack

- **Python 3.11+**, SQLAlchemy Core (NOT ORM), argparse
- **Database**: PostgreSQL (primary) or SQLite (fallback/OSS)
- **MCP**: MCP SDK 2.2 (`MCPServer`), user-scoped client configuration
- **Config**: `~/.config/dctools/dct.toml` (legacy `~/.claude/dct.toml` still read; `dct migrate-home` moves it)

## Commands

```bash
# Development
.venv/bin/pytest -v                    # Run tests (in-memory SQLite)
# No env vars needed: conftest's `_suite_isolation` fixture points DCT_* at a
# throwaway SQLite file + config for the suite AND its subprocesses (#822).
.venv/bin/dct --version                # Check version
uvx ruff@0.16.7 check dct/ tests/     # Lint (config in pyproject.toml; pin matches ci.yml)

# MCP server (smoke-test — boots, lists tools)
uv run --no-project --with 'mcp' --with sqlalchemy --with 'psycopg[binary]' \
  --with python-ulid python -c \
  "import asyncio; from dct.mcp.server import mcp; print(len(asyncio.run(mcp.list_tools())), 'tools')"
```

PostgreSQL test lane: only ever point `DCT_TEST_DATABASE_URL` at a throwaway
test database holding no data anyone needs — not merely a non-production one,
and a `_test` / `_ci` name is not enough. Every test runs
`DROP SCHEMA public CASCADE` on it; passing the lane's guard is no permission
to destroy data. Before running the lane, read `docs/testing.md`.

## Shared helpers

Check these before adding similar logic (the full module map is in
`docs/architecture.md`; neither list is exhaustive — search the code too, and a
module missing from the map is not by itself evidence of duplication):

- `dct/paths.py` — platform config/data/cache dirs; `sqlite_url` /
  `sqlite_database_path` are the one SQLite URL builder / parser: never
  concatenate or slice `sqlite:///` (SQLAlchemy 2.0 and 2.1 parse the path
  differently).
- `dct/core/engine.py` — `bootstrap_engine` (config → engine → optional DDL)
  and `readonly_engine` (no DDL, schema-ready check) for commands that promise
  no side effects.
- `dct/core/limits.py` — `ensure_fits`: a value longer than its VARCHAR column
  is refused, never truncated; limits are read from the schema.
- `dct/core/text.py` — `preview` (the one N-char ceiling), `item_label` and the
  `CLEAR` sentinel for free-text fields.
- `dct/core/provenance.py` — creation provenance of ledger rows, filled from a
  context variable that each entry point sets once.
- `dct/core/records.py` — `validate_project_rows`: strict id validation of
  project-scoped rows on the caller's connection.
- `dct/core/procs.py` — the supervised process runner (`run`, `stream_lines`);
  strict git commands and the release workflow never start a process
  themselves.
- `dct/core/gitinfo.py` — the one place dct asks git (readers,
  `resolve_checkout`, the strict `verify_release_checkout`).
- `dct/core/changelog_file.py` — the one resolver of which file is a
  project's changelog.
- `dct/core/version.py` — the version contract (parse / normalize / compare /
  bump), `storable_version` as the gate of every version write,
  `detect_version`.
- `dct/workflows/session.py` — `get_context_snapshot` / `pickup_work` /
  `handoff_work`: one implementation for MCP, CLI and skills, a single
  transaction each.
- `dct/integrations/base.py` — the client integration contract,
  `atomic_write`, `managed_instructions` and the managed-copy ownership
  helpers.

## Invariants

- Core functions take `engine` as first parameter (no global state) and return dicts.
- All tables have `deleted_at` — soft deletes only (ledger pattern).
- Schema migrations are self-healing blocks in `_apply_schema_updates` that run
  on EVERY engine bootstrap (each CLI call, each MCP start). Any "steady state /
  re-run is a no-op" claim about a backfill MUST be locked by a test that
  bootstraps THREE times and asserts no writes and no repeated output.
- Every surface is **user-scoped** (one install serves all projects; details
  in [docs/architecture.md](docs/architecture.md)). Plans keep prose in
  markdown, state in DB, identity in ULID anchors
  ([docs/plans.md](docs/plans.md)).

## Conventions

- Code and docs: English
- Communication: match the user's language unless the user's own instructions fix one
- README.md doubles as the PyPI long description — every link and image in it
  MUST be an absolute URL (relative paths 404 on the dctracker PyPI page)
- Commit format: conventional commits (`feat:`, `fix:`, `refactor:`)
- All changes need a changelog entry before committing
- Tests: pytest with in-memory SQLite fixture

## Hooks

A hook (`dct/hooks/*.sh`) must never hang or block the host operation and
never prompt without a TTY. Before any change under `dct/hooks/**`, read
`docs/hooks.md` (the five rules, the shared library, failure policies).

## MCP tool conventions

The SDK exposes each tool's **docstring verbatim as the tool description** the
calling model reads — the docstring IS the schema the model fills in. Every
`@mcp.tool()` docstring MUST:

- **Document every parameter with its role** — no undocumented signature
  parameters, no documented ghosts.
- **Make the call structure obvious** — for multi-field free-text tools, state
  that each field is a SEPARATE top-level parameter. Name enums and defaults.
- **Describe the return shape** — `{cols, rows}` (list views) vs full semantic
  dict (`get_*`) vs `{"error": ...}` (and what triggers it).
- **Stay in sync with the code, in the same change.** A stale docstring is
  worse than none — drift here is a recurring failure mode.

House style: terse, information-dense. Exemplars: `create_decision`,
`create_item`, `add_changelog`, `update_item`.

## Bug-find protocol

When a bug, API surprise or inconsistency turns up during work, patch the
**system**, not the call-site. Within the task you were given: a failing
regression test first, then the root-cause fix so the pathological state cannot
recur, then the docs (docstring, AGENTS.md, SKILL.md, tool description).
Outside it: track it in dct with the proposed fix instead of fixing it on the
side.

Red flag: calling one function to fix what another should have done correctly
(e.g. following `update_item(status="resolved")` with `resolve_item` to get
`resolved_at` stamped). Double-invocation patterns are defects, not workflows.

## Gate checkpoints

Gate checkpoints (`dogfood`, `security`, `approval`, `review`) block closing an
item; the policy, its exceptions and how to handle a refusal ship with the
package in the `add_checkpoint` MCP tool description (`dct/mcp/server.py`),
the single source — not restated here.

## OSS hygiene

Never commit author-specific data: private project slugs, personal brand
names, home-directory paths, private version strings, assistant session or
artifact links (the `Claude-Session:` trailer is banned). Use placeholders
(`proj-a`, `My Project`, `/tmp/proj-a`, generic semver) from the first
keystroke. Sniff-regex, exemptions (`changelog.md`) and release-time
enforcement: [docs/maintainers.md](docs/maintainers.md).

## Skill fallbacks

The workflow contracts in `dct/skill_sources/` are the single authored source
for all 14 skills, their Claude Code and Codex adapters, and the complete
MCP-only workflows in [docs/assistant-fallbacks.md](docs/assistant-fallbacks.md).
Skills are preferred when available, but assistants must not couple behavior
to skill existence. Fix a divergent contract in `dct/skill_sources/`, then run
`python scripts/render_skills.py` to regenerate every target and
`python scripts/render_skills.py --check` to verify freshness. Never repair
an individual generated skill, asset or fallback by hand.
