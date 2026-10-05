# Client integrations

DCT lets a session query the work left to do, its acceptance checkpoints,
recorded decisions and the next handoff. The database is the shared execution
ledger; a client integration supplies installation, workflow instructions and,
where supported, hooks. A working MCP connection does not establish all three.

## Release availability

The published PyPI release **1.5.0** includes the common CLI/MCP tools and the
established Claude Code integration. This guide also describes **unreleased
development changes**: `dct integrate`, `dct doctor`, Codex workflow skills and
experimental native Codex hooks. Those additions are not installed by
`uv tool install 'dctracker[mcp]'` from PyPI 1.5.0.

To try the new commands, use a checkout containing the integration changes:

```sh
uv tool install --editable '/path/to/dct[mcp]'
dct integrate install codex
dct integrate status codex --json
dct doctor --client codex --json
```

For a published installation, use `dct init` for the existing Claude setup or
register `dct server` manually in another client's MCP configuration. Do not
interpret the development matrix below as a claim about PyPI 1.5.0.

Integration commands never initialize or migrate the database. Use the default
SQLite configuration or create `dct.toml` as described in the
[configuration reference](architecture.md). Register a project if it is not
already tracked with `dct project add myproj /path/to/myproj`; normal tracking
operations bootstrap the database as usual. The existing interactive `dct init` remains compatible and
installs Claude Code. `dct integrate install claude` explicitly installs its
MCP, skills, hooks and managed instructions.

## Codex: supported explicit workflows in development builds

The adapter configures `dct server` in `$CODEX_HOME/config.toml` (default
`~/.codex/config.toml`), installs `pickup`, `handoff`, `track` and `clog` in
`~/.agents/skills`, and maintains a bounded block in `$CODEX_HOME/AGENTS.md`.
It sets `DCT_ACTOR_CLIENT=codex` and `DCT_ACTOR_TYPE=agent`. A session identifier
must come from the host; the installer never invents or persists one globally.

Existing MCP command, arguments, environment values, unrelated tables and
comments are preserved. Unsupported inline/dotted TOML layouts are rejected
before writing; use explicit `[mcp_servers.dct]` and `[mcp_servers.dct.env]`
tables. Existing remote MCP registrations require server-side identity setup.
Diagnostics flag conflicts, stale assets, disabled MCP and missing executables.

Restart/reconnect the client after installation. File diagnostics do not prove
that a running client has connected or loaded new instructions.
A nonempty `AGENTS.override.md` takes precedence over `AGENTS.md`; doctor
reports this as an error. Merge needed override instructions manually before
removing or emptying the override. Repair does not modify that user-owned file.

The four explicit skills (`pickup`, `handoff`, `track`, `clog`) are the supported
Codex workflow surface in this checkout. Their session-resumption behavior has
[separate acceptance evidence](codex-acceptance.md).

### Experimental native hooks

Native hook definitions cover changelog checks, stop reminders, apply-patch
autoscan and conditional plan-snapshot mirroring. The runtime
bridge translates Codex payloads and reuses shared DCT policies. After explicit
installation, review and trust the definitions through Codex `/hooks`; files
alone do not establish active hooks. Runtime validation currently targets the
local POSIX client. Plan mirroring also requires the host to expose and emit
`update_plan`; its emission has not been demonstrated in the tested
noninteractive Sol configuration. A plan written in conversation is not a native
plan event. Mirroring keeps an append-only history of observed steps and source
statuses, not live or bidirectional checkpoint synchronization. See the [hook guide](hooks.md) for platform
dependencies, supported events, ownership and limitations. Official host documentation:
[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) and
[skills](https://learn.chatgpt.com/docs/build-skills).

## Repair, upgrade and uninstall

```sh
dct integrate repair codex
dct upgrade
dct integrate uninstall codex --json
```

Repair repeats installation. Only unchanged managed copies and managed
instruction blocks are refreshed. Unknown or modified skill directories remain
untouched and are errors in diagnostics. Pre-existing unmarked copies cannot
reliably be distinguished from user content; move conflicts aside yourself
before reinstalling if desired.

Config and instruction changes use atomic writes with backups. Identical writes
are no-ops. Installation has multiple steps: a later failure may leave earlier
steps installed. Fix the reported problem and repeat repair. Do not run concurrent
installers on the same configuration file.

Upgrade refreshes detected integrations without introducing new instruction
blocks or hook opt-ins. Native Codex hook definitions are refreshed only when
their ownership sidecar already exists. Uninstall removes proven-owned assets
and unchanged native definitions, never database state. Customized or pre-existing
MCP entries remain. Legacy Claude hook settings and MCP registrations lack
ownership metadata and require manual review.

## Runtime diagnostics

Doctor parses configuration without opening the database and validates Python
and package requirements. It probes its own interpreter and the interpreter in
the PATH `dct` entrypoint and configured MCP executables when readable Python
shebangs are available. Probes use isolated Python to avoid importing a checkout
from the current working directory in place of an installed wheel. Editable
installs are compared against current `pyproject.toml` requirements, rather than
only potentially stale installed metadata. For custom launch wrappers:

```sh
dct doctor --python /path/to/other-venv/bin/python --json
```

Errors return exit status 1. Warnings identify unsupported capabilities or
checks needing a real client session. Doctor performs no migrations, package
upgrades, model calls or network requests.

## Capability matrix

This matrix describes the development checkout. **Established** is the existing
Claude integration; **supported** denotes the packaged explicit Codex workflows;
**experimental** requires the stated native-host conditions; **generic** denotes
manual protocol integration, without a claim of client-specific acceptance.

| Capability | Claude Code — established | Codex — development | Other clients — generic MCP |
|---|---|---|---|
| Shared tracking/session MCP tools | Available | Available | Manual stdio registration; host-dependent discovery |
| Integration installer | `init`; new `integrate` commands in development | `integrate install/repair` | Manual |
| Managed workflow skills | Existing collection | Supported: pickup, handoff, track, clog | Supply client-specific instructions manually |
| Changelog checks / stop reminders | Claude hooks | Experimental native hooks; review/trust and dependencies required | No packaged adapter |
| Plan-file autoscan | Claude edit/write events | Experimental `apply_patch` events; shell edits need explicit rescan | CLI rescan or MCP `ingest_plan_file` |
| Native plan mirroring | Claude TaskCreate/TodoWrite | Experimental and conditional on actual `update_plan` events | No packaged mirror |
| Managed instructions | CLAUDE.md | AGENTS.md; conflicts reported | Manual |
| Integration diagnostics | Development `doctor` | Development `doctor`; runtime trust checked separately | Core runtime checks only |

Hook installation and trust are separate from explicit skills. Treat each
client/host version as its own integration surface; do not infer hook parity
from a successful MCP call.

Record real-client acceptance and benchmarks separately from unit/protocol
tests. A working MCP connection alone does not establish skill or hook behavior.
