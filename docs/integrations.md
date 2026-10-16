# Client integrations

DCT lets a session query the work left to do, its acceptance checkpoints,
recorded decisions and the next handoff. The database is the shared execution
ledger; a client integration supplies installation, workflow instructions and,
where supported, hooks. A working MCP connection does not establish all three.

## Release availability

DCT **1.6.0** includes the common CLI/MCP tools, Claude Code and Codex
integration installers, `dct integrate`, `dct doctor`, and packaged workflow
skills for both clients. Native Codex hooks remain experimental and require
the host capabilities and explicit trust described below.

Install DCT 1.6.0 or later, then configure the integration:

```sh
uv tool install 'dctracker[mcp]>=1.6.0'
dct integrate install codex
dct integrate status codex --json
dct doctor --client codex --json
```

Use `dct init` for the Claude setup or register `dct server` manually in
another client's MCP configuration. The capability matrix below distinguishes
packaged integrations from experimental hooks and generic MCP support.

Integration commands never initialize or migrate the database. Use the default
SQLite configuration or create `dct.toml` as described in the
[configuration reference](architecture.md). Register a project if it is not
already tracked with `dct project add myproj /path/to/myproj`; normal tracking
operations bootstrap the database as usual. The existing interactive `dct init` remains compatible and
installs Claude Code. `dct integrate install claude` explicitly installs its
MCP, skills, hooks and managed instructions.

### Shared reference and managed pointers

DCT 1.6.0 installs `dct/assets/dct-reference.md` at the canonical
`paths.config_dir() / "dct-reference.md"`: on POSIX this is normally
`~/.config/dctools/dct-reference.md`, honoring `XDG_CONFIG_HOME`; Windows uses
the existing platform directory policy. `DCT_CONFIG_PATH` selects the database
configuration file and does not relocate this integration asset. Either client
installs or refreshes the guide without a database bootstrap. The reference is a
concise generated catalog with shared operating rules; the complete workflow
fallback is generated from the same authored contracts.

The Claude `CLAUDE.md` and Codex `AGENTS.md` blocks are short pointers rendered
with that actual resolved path. Install, refresh and doctor use the same body
renderer. After an XDG change, an owned block naming the old path is outdated;
repair installs the current reference and refreshes the block while preserving
outside-marker bytes. A foreign canonical reference blocks pointer rewrites,
so a failed repair does not claim an available guide at the new location.
Canonical and Claude's legacy alias are handled and reported independently:
a conflict at the canonical path still permits refreshing a proven-owned old
alias, while preserving the earlier pointer and the foreign canonical file.

Ownership is proven by the established current-package link contract, an exact
symlink target recorded beside the file, or an unchanged marked copy. Recorded
links follow package relocations. Edited copies and foreign links, including
arbitrary links into another package directory, remain untouched and receive a
path-specific remedy. Creation is exclusive: a path appearing during install
is preserved. Repeating current content is a no-op. Doctor reports
current, missing, outdated and foreign states without writing.

Claude retains and refreshes its proven-owned `~/.claude/dct-reference.md`
compatibility alias; a Codex-only installation creates no Claude alias.
Each client's uninstall removes
only its owned assets, pointer block and any owned client-specific alias. It
retains the shared canonical reference and reports retention in both plain CLI
and JSON output. Plain output lists `retained` separately from `preserved`,
making shared-reference lifetime visible. Both uninstall
orders preserve the other client's guide. Upgrade touches only detected
clients and existing instruction blocks, introducing no new hook trust or
instruction opt-in. Installation has separately reported steps and is retryable;
it does not promise an all-file transaction.

## Codex: supported explicit workflows

The adapter configures `dct server` in `$CODEX_HOME/config.toml` (default
`~/.codex/config.toml`), installs `clog`, `commit`, `dct-import`, `dct-init`, `handoff`, `pickup`, `plan-resolve-uncertain`, `plan-status`, `release`, `track`, `track-list`, `track-resolve`, `track-update`, `triage`
in `~/.agents/skills`, and maintains a bounded block in `$CODEX_HOME/AGENTS.md`.
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

The 14 packaged skills (`clog`, `commit`, `dct-import`, `dct-init`, `handoff`, `pickup`, `plan-resolve-uncertain`, `plan-status`, `release`, `track`, `track-list`, `track-resolve`, `track-update`, `triage`) are the shared workflow inventory in DCT 1.6.0.
Each contains its complete contract, with native `agents/openai.yaml` metadata.
`dct-init`, `dct-import` and `plan-resolve-uncertain` are explicit-only. Both
adapters derive their inventory from the canonical manifest; renderer and build
checks reject stale or missing generated assets. `triage` uses the findings
ledger, publishes only when the host provides that capability, and preserves
owner-only decisions. Its proposals and scoped backlog follow the same contract
in both clients. Packaged availability is separate from client/runtime
acceptance evidence; see the [session-resumption acceptance evidence](codex-acceptance.md).

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

This matrix describes DCT 1.6.0. **Established** is the existing
Claude integration; **packaged** denotes the shared Codex workflow inventory;
**experimental** requires the stated native-host conditions; **generic** denotes
manual protocol integration, without a claim of client-specific acceptance.

| Capability | Claude Code — established | Codex — packaged | Other clients — generic MCP |
|---|---|---|---|
| Shared tracking/session MCP tools | Available | Available | Manual stdio registration; host-dependent discovery |
| Integration installer | `init`; `integrate install/repair` | `integrate install/repair` | Manual |
| Managed workflow skills | Existing collection of 14, including triage | Packaged: clog, commit, dct-import, dct-init, handoff, pickup, plan-resolve-uncertain, plan-status, release, track, track-list, track-resolve, track-update, triage | Supply client-specific instructions manually |
| Changelog checks / stop reminders | Claude hooks | Experimental native hooks; review/trust and dependencies required | No packaged adapter |
| Plan-file autoscan | Claude edit/write events | Experimental `apply_patch` events; shell edits need explicit rescan | CLI rescan or MCP `ingest_plan_file` |
| Native plan mirroring | Claude TaskCreate/TodoWrite | Experimental and conditional on actual `update_plan` events | No packaged mirror |
| Managed instructions | CLAUDE.md pointer + shared reference; owned legacy alias retained | AGENTS.md pointer + shared reference; conflicts reported | Manual |
| Integration diagnostics | `doctor` | `doctor`; runtime trust checked separately | Core runtime checks only |

Hook installation and trust are separate from explicit skills. Treat each
client/host version as its own integration surface; do not infer hook parity
from a successful MCP call.

Record real-client acceptance and benchmarks separately from unit/protocol
tests. A working MCP connection alone does not establish skill or hook behavior.
