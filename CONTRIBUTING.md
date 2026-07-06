# Contributing to dct

Thanks for considering a contribution! dct is a small, focused tool — the bar
for changes is "does it make cross-project tracking better without adding
ceremony".

## Dev setup

```bash
git clone https://github.com/fotodeveloper/dct
cd dct
uv venv && uv pip install -e '.[all]'      # or: python -m venv .venv && pip install -e '.[all]'
.venv/bin/pytest -q                        # full suite, in-memory SQLite — no services needed
```

The test suite runs against in-memory SQLite; PostgreSQL is exercised in
production but not required for development.

## Quality gates

- **Tests**: `pytest -q` must stay green. Bug fixes come with a failing test
  first (regression guard); new features come with tests for the contract,
  edge cases and error paths — not just the happy path.
- **Lint**: `uvx ruff check dct/ tests/` must pass (config lives in
  `pyproject.toml`).
- **CI** runs both on every PR (Python 3.11–3.13).

## Conventions

- **Commits**: conventional commits — `feat(scope): …`, `fix(scope): …`,
  `docs: …`, `refactor: …`, `test: …`, `chore: …`.
- **Branching**: branch from `main`, open a PR. Keep PRs single-concern.
- **Code and docs are in English.**
- **Soft deletes only** — tables carry `deleted_at`; nothing is hard-deleted
  (ledger pattern). Follow it in new code.
- Core functions take `engine` as the first parameter and return dicts —
  no global state, no ORM models leaking out.

## Working on the MCP server

Boot the server and list its tools without touching your Claude Code setup:

```bash
uv run --no-project --with 'mcp[cli]' --with sqlalchemy --with 'psycopg[binary]' \
  --with python-ulid python -c \
  "import asyncio; from dct.server import mcp; print(len(asyncio.run(mcp.list_tools())), 'tools')"
```

When adding or changing an `@mcp.tool()`:

- The **docstring is the contract** — FastMCP exposes it verbatim as the tool
  description the calling model reads. Document every parameter, name enums
  and defaults, describe the return shape (`{cols, rows}` for list views, a
  full dict for `get_*`, `{"error": ...}` for failures).
- Keep the docstring in sync with the signature in the same change — a stale
  docstring confidently misleads every caller.
- New tools are only callable after the Claude Code session restarts (the MCP
  server is a per-session subprocess).

## Hooks and skills

- Hooks (`hooks/*.sh`) must follow the five rules in
  [docs/hooks.md](docs/hooks.md) — no prompts without a tty, consume stdin,
  degrade gracefully, meaningful exit codes, strip message content before
  flag inspection.
- Skills (`skills/*/SKILL.md`) are Claude Code slash commands; keep their
  frontmatter `description` trigger-rich and the body imperative.

## Release process

Maintainers release by stamping the changelog and tagging `vX.Y.Z`; the
`publish.yml` workflow builds and uploads `dctracker` to PyPI via trusted
publishing. Contributors don't need to touch versions — just add your change
and note it in the PR description.
