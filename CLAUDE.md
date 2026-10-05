# dct — Deterministic Context Tracker

Cross-project work tracking for MCP clients, with Claude Code and Codex adapters.
Items, changelog, notes — all in one database.

Deep-dives live in `docs/` and are read on demand — keep this file lean:
[architecture & config](docs/architecture.md) · [plans subsystem](docs/plans.md) ·
[hooks](docs/hooks.md) · [MCP tool catalogue](docs/mcp-tools.md) ·
[assistant fallbacks](docs/assistant-fallbacks.md) · [maintainer notes](docs/maintainers.md)

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

# PG test lane (#426) — the same suite against a real PostgreSQL server.
# DCT_TEST_DATABASE_URL switches the `engine` fixture; isolation is a per-test
# DROP SCHEMA public CASCADE, so it MUST point at a THROWAWAY database.
# A session-scoped guard (K-31) refuses anything else BEFORE the first DDL:
# the database NAME must carry a `test`/`ci` segment (dct_ci, dct_ci_local,
# foo_test — `dct` is rejected), it must not resolve to the configured live
# database, and `items` must hold <= 50 rows. Predicate:
# tests/conftest.py::throwaway_refusal_reason, unit-tested in tests/test_pg_lane.py.
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=dct \
  -e POSTGRES_USER=dct -e POSTGRES_DB=dct_ci --name dct-pg postgres:16
DCT_TEST_DATABASE_URL=postgresql+psycopg://dct:dct@localhost:5433/dct_ci \
  DCT_EXPECT_PG=1 .venv/bin/pytest -q -rs   # ~3.5 min vs ~16 s on SQLite
# DCT_EXPECT_PG=1 arms the sentinel that fails if the lane silently fell back
# to SQLite (K-32); it is set in ci.yml's tests-postgres job.
# Local server instead of docker: createdb dct_ci_local, point the URL at it.
# A handful of SQLite-specific assertions are skipped on this lane — the list
# and the reason for each live in tests/conftest.py (PG_LANE_SKIPS).
.venv/bin/dct --version                # Check version
uvx ruff@0.16.7 check dct/ tests/     # Lint (config in pyproject.toml; pin matches ci.yml — K-46)

# MCP server (smoke-test — boots, lists tools)
uv run --no-project --with 'mcp' --with sqlalchemy --with 'psycopg[binary]' \
  --with python-ulid python -c \
  "import asyncio; from dct.mcp.server import mcp; print(len(asyncio.run(mcp.list_tools())), 'tools')"
```

## Architecture

```
dct/config.py            — Config loading (platform config dir + legacy
                           ~/.claude fallback + env vars; ADR #52)
dct/paths.py             — Platform-neutral config/data/cache/state dirs
                           (XDG on POSIX, platformdirs on Windows); the ONE
                           SQLite URL builder / parser (sqlite_url,
                           sqlite_database_path — K-99: never concatenate or
                           slice "sqlite:///", SQLAlchemy 2.0 and 2.1 parse
                           the path differently)
dct/migrate.py           — dct migrate-home (legacy → platform-neutral home)
dct/db.py                — SQLAlchemy Core tables + engine factory + migrations
dct/core/projects.py     — Projects CRUD (+ realpath-canonical CWD detection)
dct/core/items.py        — Items / notes / checkpoints CRUD
dct/core/changelog.py    — Changelog entries CRUD + release stamping (the stamp
                           is the ONLY writer of versions, K-102: add_entry
                           refuses one; import_released_entry = old history)
dct/core/releases.py     — Release ledger (K-41/K-60/K-61): records written with
                           the stamp, one state machine (TRANSITIONS; advance,
                           abandon), immutable candidate, write-once release
                           commit / manifest / proof, legacy reconstruction
                           (its verification scope recorded, K-110), the
                           controlled takeover of a record without consent
                           (take_over_release, K-119 M3 — the only way a
                           record becomes dct's after its stamp), the
                           project's version-file contract (K-73); the
                           verified release_mode, executor and write-once
                           consent / proposal / render; K-115: tag_proof /
                           push_proof (a status is never proof), mutual
                           consistency of a record's fields, duplicates of
                           one version refuse writes; every mutation checks
                           its actor (docs/release-workflow.md)
dct/core/sprint.py       — Current-sprint ops; delegates to item_sprints
dct/core/sprints.py      — First-class sprints (single source of truth)
dct/core/handoff.py      — Session handoffs (cross-session prompt ledger)
dct/core/decisions.py    — ADR / sprint-amendment / open-question records
dct/core/tags.py         — Global tag dictionary + item_tags links (similarity
                           guard, merge)
dct/core/version.py      — Version contract (parse / normalize / compare / bump,
                           X.Y | X.Y.Z, leading v dropped) + project-file autodetect
dct/core/gitinfo.py      — Git readers (toplevel / branch / common dir) +
                           resolve_checkout: same-repo checkout of a project
                           (repo root vs project root), best-effort; the
                           STRICT verify_release_checkout (confirmed repo /
                           confirmed non-repo / error) gates the CLI release
dct/core/procs.py        — Supervised process runner (K-108/K-113): own process
                           group, TERM→KILL + reap on timeout / interruption /
                           exception, bounded or complete-or-refuse capture,
                           streamed lines; used by every strict git command
                           and the release workflow's sync script
dct/core/records.py      — Shared STRICT project-row id validator
dct/core/checkpoints.py  — Item checkpoint helpers
dct/core/ids.py          — ULID generation
dct/core/plans/          — Plans subsystem: parser.py (pure MD parser),
                           classify.py (rules + optional command), crud.py, ingest.py
dct/services/            — Service layer (reserved for future shared app ops)
dct/workflows/session.py — G1 composed ops: get_context_snapshot /
                           pickup_work / handoff_work (one impl for MCP,
                           CLI and skills; single transaction each)
dct/mcp/server.py        — MCP server, SDK 2.2 MCPServer (60 tools);
                           dct/server.py is a compat shim
dct/cli.py               — argparse CLI entry point
dct/export.py            — Keep a Changelog MD export
dct/integrations/claude.py — Claude Code adapter: MCP registration, skill/hook
                           symlinks, settings.json wiring (`dct hooks wire`);
                           dct/setup.py is a compat shim
dct/integrations/codex.py — Codex MCP, workflow skills and managed AGENTS.md
dct/integrations/codex_hooks.py — Native event bridge over shared hook policies
dct/workflows/task_mirror.py — Append-only client plan/task observations
dct/workflows/release/   — Release workflow (K-72): proof layer, read-only —
                           proof.py (trailer / revert / squash rules),
                           plan.py (`dct release plan`, `dct clog candidates`);
                           effects layer — effects.py (`dct release attest /
                           prepare / apply / resume / abandon / reconstruct /
                           takeover / version-files`,
                           alias `clog release`: the version sync runs once,
                           isolated, in prepare; commits through the repo's
                           hooks, tag, push; every effect verified before
                           the ledger records it; schema: docs/release-workflow.md);
                           oplock.py (the per-repository operation lock, K-96 D7)
dct/integrations/base.py — Shared contract and conservative file ownership
dct/integrations/doctor.py — Read-only per-runtime dependency diagnostics
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
- Schema migrations are self-healing blocks in `_apply_schema_updates` that run
  on EVERY engine bootstrap (each CLI call, each MCP start). Any "steady state /
  re-run is a no-op" claim about a backfill MUST be locked by a test that
  bootstraps THREE times and asserts no writes and no repeated output (#611/K-22:
  three independent reviews found the same non-converging candidate set).
- Every surface is **user-scoped** (one install serves all projects; details
  in [docs/architecture.md](docs/architecture.md)). Plans keep prose in
  markdown, state in DB, identity in ULID anchors
  ([docs/plans.md](docs/plans.md)).

## Conventions

- Code and docs: English
- Communication: match the user's language
- README.md doubles as the PyPI long description — every link and image in it
  MUST be an absolute URL (relative paths 404 on the dctracker PyPI page)
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

Reference implementation: `dct/hooks/check-changelog.sh`. Logic shared by
several hooks lives in a sourced `dct/hooks/*.lib.sh` (never wired; installed
beside the hooks — `HOOK_LIBS` in `dct/integrations/claude.py`), while each
hook keeps its own failure policy; commands a hook prints for pasting quote
every interpolated argument with `printf %q`.

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
names, home-directory paths, private version strings, assistant session or
artifact URLs (`claude.ai/code/…` — the `Claude-Session:` trailer is banned). Use placeholders
(`proj-a`, `My Project`, `/tmp/proj-a`, generic semver) from the first
keystroke. Sniff-regex, exemptions (`changelog.md`) and release-time
enforcement: [docs/maintainers.md](docs/maintainers.md).

## Skill fallbacks

Skills in `dct/skills/` are the preferred way to drive dct, but assistants must
not couple logic to skill existence. Step-by-step MCP-only equivalents for
`/release`, `/commit`, `/track`, `/clog`, `/track-resolve` live in
[docs/assistant-fallbacks.md](docs/assistant-fallbacks.md) — those prose
descriptions are the authoritative behavior; if a skill diverges, fix the skill.
