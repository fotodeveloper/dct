# Client integrations

DCT stores work state independently of the client. Claude Code and Codex use
the same MCP tools, database and composed session workflows.

```sh
uv tool install 'dctracker[mcp]'
# or: pipx install 'dctracker[mcp]'
# or, inside a virtual environment: pip install 'dctracker[mcp]'
dct integrate install codex
dct integrate status codex --json
dct doctor --client codex --json
```

Integration commands never initialize or migrate the database. Configure DCT
using its existing config commands; normal tracking operations bootstrap the
database as usual. The existing interactive `dct init` remains compatible and
installs Claude Code. `dct integrate install claude` explicitly installs its
MCP, skills, hooks and managed instructions.

## Codex

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

Codex supports explicit skills and native hook definitions for changelog checks,
stop reminders, apply-patch autoscan and plan-snapshot mirroring. The runtime
bridge translates Codex payloads and reuses shared DCT policies. After explicit
installation, review and trust the definitions through Codex `/hooks`; files
alone do not establish active hooks. See the [hook guide](hooks.md) for platform
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

| Capability | Claude Code | Codex | Generic MCP client |
|---|---|---|---|
| Shared tracking/session MCP tools | Yes | Yes | Yes |
| Integration installer | Yes | Yes | Manual |
| Managed workflow skills | Existing collection | pickup, handoff, track, clog | Host-dependent |
| Task mirror/autoscan/changelog hooks | Claude events | Native events; explicit trust required | Host-dependent |
| Managed instructions | CLAUDE.md | AGENTS.md | Manual |
| Integration diagnostics | Yes | Yes | Core runtime checks |

Record real-client acceptance and benchmarks separately from unit/protocol
tests. A working MCP connection alone does not establish skill or hook behavior.
