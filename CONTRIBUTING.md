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

The default test suite runs against in-memory SQLite. CI also runs the suite
against an isolated PostgreSQL service; PostgreSQL is not required for local
development. See [docs/testing.md](docs/testing.md) for the guarded throwaway-database lane.

## Agent instructions

`AGENTS.md` is the only project instruction file; there is deliberately no
project `CLAUDE.md`, no `@AGENTS.md` loader and no symlink.

- **Codex** reads `AGENTS.md` natively, from the repository root and from the
  subdirectory it works in.
- **Claude Code** reads `AGENTS.md` natively from v2.1.277 on, under the
  default *Project instructions* setting (`claude-md-or-agents-md`) — and only
  while no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` exists in the
  working directory or any directory above it. Your user-level
  `~/.claude/CLAUDE.md` (and an organization's managed one) does not count and
  keeps loading alongside. Older versions, a disabled built-in plugin (`/plugin`)
  or, before v2.1.281, some sessions (Bedrock, telemetry disabled) read
  `CLAUDE.md` only; the first session after an upgrade may not pick it up yet.
- **A private `CLAUDE.local.md`** switches `AGENTS.md` off under the default
  setting. To keep both, set *Project instructions* to `claude-md-and-agents-md`
  (`/config`, or in `~/.claude/settings.json` under `pluginConfigs` →
  `"cc-plugin-agents-md@builtin"` → `options.instructionFiles`; the ID was
  `agents-md@builtin` before v2.1.285).
- The same holds in worktrees: neither a linked worktree elsewhere on disk
  (`git worktree add`) nor a Claude Code worktree under
  `<repo>/.claude/worktrees/<name>` — whose ancestors include the main
  checkout — may have any of the three files in it or above it.

`tests/test_instruction_files.py` fails when one of those files exists in the
working tree (tracked, untracked or ignored) or, on your machine, in a
directory above it; a `CLAUDE.local.md` passes only while your Claude Code
settings select `claude-md-and-agents-md`. That ancestor check is local —
CI's directories differ — and no test can protect the start of a session.
Details:
[Claude Code memory docs](https://code.claude.com/docs/en/memory#agents-md).

## Quality gates

- **Tests**: `pytest -q` must stay green. Bug fixes come with a failing test
  first (regression guard); new features come with tests for the contract,
  edge cases and error paths — not just the happy path.
- **Lint**: `uvx ruff@0.16.7 check dct/ tests/` must pass (config lives in
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

Boot the server and list its tools without changing client configuration:

```bash
uv run --no-project --with 'mcp>=2.2,<3' --with sqlalchemy --with 'psycopg[binary]' \
  --with python-ulid python -c \
  "import asyncio; from dct.mcp.server import mcp; print(len(asyncio.run(mcp.list_tools())), 'tools')"
```

When adding or changing an `@mcp.tool()`:

- The **docstring is the contract** — MCPServer exposes it verbatim as the tool
  description the calling model reads. Document every parameter, name enums
  and defaults, describe the return shape (`{cols, rows}` for list views, a
  full dict for `get_*`, `{"error": ...}` for failures).
- Keep the docstring in sync with the signature in the same change — a stale
  docstring confidently misleads every caller.
- After changing server code, restart or reconnect the client's DCT server
  process. Verify the new behavior in a fresh session; an editable install does
  not reload an already-running Python process.

## Hooks and skills

- Hooks (`dct/hooks/*.sh`) must follow the five rules in
  [docs/hooks.md](docs/hooks.md) — no prompts without a tty, consume stdin,
  degrade gracefully, meaningful exit codes, strip message content before
  flag inspection.
- Claude skills live in `dct/skills/`; Codex workflow templates are
  `dct/assets/codex-*.md`. Keep their frontmatter `description` trigger-rich
  and the body imperative. See the [client capability matrix](docs/integrations.md).
- The native Codex bridge reuses shared hook policies. Test payload handling and
  ownership preservation independently of global client trust; use isolated
  fixtures for runtime checks.

## Contribution sign-off

Contributions use the [Developer Certificate of Origin 1.1](https://developercertificate.org/)
(DCO) and the project's [MIT license](LICENSE). Read the DCO before signing:
your sign-off certifies your right to submit the contribution and acknowledges
that the contribution and its sign-off are public records.

Add your own sign-off to contributed commits with `git commit -s`:

```text
Signed-off-by: Your Name <your-email@example.com>
```

Only sign for yourself. A `Co-Authored-By` trailer credits collaboration but
does not replace a contributor's DCO sign-off. Maintainers review sign-offs when
accepting contributions; the current CI does not enforce them automatically.

## Project name and identity

Use “DCT”, “Deterministic Context Tracker” and the project logo to identify the
official project accurately. Describe third-party work as “compatible with DCT”,
“built on DCT” or an “unofficial DCT integration” where appropriate. Give forks
and services their own identity and do not imply official endorsement.

This naming guidance does not change the MIT license on the code or assert that
the project name is a registered trademark. Contact the maintainers before using
the project's identity to present a product or service as official.

## Release process

Maintainers release by stamping the changelog and tagging `vX.Y.Z`; the
`publish.yml` workflow builds and uploads `dctracker` to PyPI via trusted
publishing. Contributors don't need to touch versions — just add your change
and note it in the PR description.
