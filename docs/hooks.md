# dct hooks — wiring reference

## Native Codex hooks

`dct integrate install codex` (or `repair codex`) adds four native definitions
to `$CODEX_HOME/hooks.json`, using the Python environment that installed DCT.
The runtime bridge is packaged as `dct.integrations.codex_hooks`; it reuses the
existing changelog and autoscan policy scripts below.

| Event / matcher | Behavior |
|---|---|
| `PreToolUse` / `^Bash$` | Check a recognized Git commit using the shared changelog policy. Only an explicit policy denial blocks. |
| `Stop` | Convert the shared reminder to nonblocking Codex `systemMessage` JSON. |
| `PostToolUse` / `^apply_patch$` (async) | On successful patches, dispatch existing plan files within the worktree to the shared autoscan script. |
| `PostToolUse` / `^update_plan$` (async) | Record successful native plan snapshots as auxiliary, non-gating DCT checkpoints. |

Codex maps `exec_command` to the canonical hook name `Bash`. `apply_patch`
provides the patch in `tool_input.command`, rather than a single `file_path`.
The bridge handles add/update/move destinations, skips deletions and failed
operations, and prevents autoscan from crossing the worktree boundary. Shell
edits are not an autoscan source; use `dct plan rescan` explicitly for those.

Commit detection supports literal Git commands, `git -C`, and leading literal
`cd ... && git ...`. Dynamic working directories and unsupported shell constructs
are skipped rather than checking a different project. Hooks are workflow
guardrails, not an exhaustive shell-security boundary. The shared policy checks
the index as it exists before the tool runs: stage changes in a separate command
when relying on the check, rather than combining staging and commit or relying
on `git commit -a`. Bash, jq, Git and DCT on
PATH are required by the reused shell policies; missing dependencies degrade
gracefully. Runtime validation currently targets the local POSIX client.

### Review and trust

Installing definitions does **not** trust them. In a fresh Codex CLI session,
open `/hooks`, review the four DCT commands and trust the definitions you intend
to run. Codex skips new or changed non-managed hooks until their current hashes
are trusted. DCT never writes Codex trust storage or enables a trust bypass.
`dct doctor --client codex` checks installation separately and reports that
runtime trust must be checked in Codex. See the
[official hook contract](https://learn.chatgpt.com/docs/hooks).

The installer keeps a separate ownership sidecar. Repair and upgrade refresh
only unchanged owned definitions; other hooks and modified entries are retained.
Explicit repair replaces recognized missing legacy references in place when
the event and single-handler group allow it. Shared or mismatched groups remain
for manual review: removing their positions could invalidate another hook's
trust in the verified Codex client. Existing scripts and custom commands are not
claimed as owned. Upgrade does not introduce hooks into an installation that
never opted in. Uninstall removes unchanged owned definitions and retains
modified or unrelated hooks for review. Empty matcher groups are deliberately
left as position placeholders: in the verified Codex client, deleting a group
can change the keys and invalidate trust of later, unrelated hooks. Do not
compact these groups without reviewing the affected hooks again in Codex.

### Native plan history

`[integrations.codex].task_mirror` is independent of the Claude setting:
`auto` records snapshots; `ask` and `off` skip them. Codex records require actual
`session_id` and `turn_id`; no synthetic identity is substituted. Session and
turn together separate plans because subagents may share the parent session ID.

Each changed snapshot appends observations. Equal steps remain distinct rows;
exact event replays are skipped transactionally. Native status is preserved in
the text, while checkpoint status remains pending with the historical non-gating
`cc-task` kind. This is an append-only observation history, not bidirectional
plan synchronization or automatic acceptance of DCT work. Codex mirror writes
carry `actor_client=codex`, the real session and `source=hook`.

Mirroring applies when the client exposes and emits `update_plan`. Its emission
has not yet been demonstrated in the tested noninteractive Sol configuration;
model statements about tool availability are not proof of the exposed catalog.
The decoder and transactional mirror are covered by isolated event/worker tests.

## Claude Code hooks

`dct init` installs four hooks as symlinks in `~/.claude/hooks/` and (with your
consent) wires them into `~/.claude/settings.json`. This page is the manual
reference for that wiring — useful when you skipped the automatic step, manage
`settings.json` yourself, or want to relocate a hook.

## Automatic wiring

```bash
dct hooks wire                      # wire into ~/.claude/settings.json
dct hooks wire --settings <path>    # custom settings file
dct init --wire-hooks               # non-interactive init: wire without prompting
dct init --no-wire-hooks            # non-interactive init: skip wiring
```

Properties of the merge:

- **Idempotent** — a hook already mentioned anywhere in `settings.json`
  (matched by script filename, regardless of path form or event) is skipped.
  Re-running after an upgrade is always safe.
- **Respects your layout** — if you deliberately wired a dct hook under a
  different event or matcher, it stays where you put it.
- **Never destructive** — your own hooks and unrelated settings keys are left
  untouched; the original file is copied to `settings.json.bak-dct-<timestamp>`
  before the first write; malformed JSON aborts with an error and no write.

## The four hooks

| Hook | Event / matcher | Purpose |
|---|---|---|
| `dct-check-changelog.sh` | `PreToolUse` / `Bash` | blocks `git commit` when app code changed but no unreleased changelog entry exists; **on by default** per project once wired (`dct project changelog <slug> off` opts a project out); bypasses `release:` commits and doc-only changes |
| `dct-check-changelog-on-stop.sh` | `Stop` | session-end reminder if unreleased entries are pending (same per-project gate) |
| `dct-plan-autoscan.sh` | `PostToolUse` / `Edit\|Write\|MultiEdit` | background rescan of plan-like markdown (plans, specs, ADRs, roadmaps); fire-and-forget, never blocks |
| `dct-task-mirror.sh` | `PreToolUse` / `TodoWrite\|TaskCreate` | mirrors Claude Code tasks into dct as non-blocking `cc-task` checkpoints |

## Manual wiring

Equivalent JSON, if you prefer to edit `~/.claude/settings.json` yourself
(replace `~` with your absolute home path if your setup does not expand it):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/dct-check-changelog.sh",
            "timeout": 10
          }
        ]
      },
      {
        "matcher": "TodoWrite|TaskCreate",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/dct-task-mirror.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/dct-check-changelog-on-stop.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/dct-plan-autoscan.sh"
          }
        ]
      }
    ]
  }
}
```

## Latency design (#354)

Both always-on hooks are structured so the common case spends **zero Python
cold starts** — a synchronous `dct` invocation costs ~200 ms, and firing two
of them on every turn was the whole of the observed latency.

### `dct-task-mirror.sh` — lean shell dispatcher

The shell does the absolute minimum: drain stdin, `jq`-check that
`tool_name` is `TaskCreate`/`TodoWrite`, then `nohup` the backgrounded
`dct _task-mirror` worker and exit. Nothing that reads config or the database
runs in the foreground. The **config gate** (`[integrations.claude].task_mirror` =
`auto`/`ask`/`off`) and the **project autodetect** live inside the worker
(`dct _task-mirror`): it reads config, resolves the project from the CWD, and
exits silently when the mode is not `auto` or the CWD is not a registered
project. `session_id` travels inside the payload, so the shell never parses it
out. Measured fast paths: non-task tool and task dispatch both under ~10 ms.

### `check-changelog-on-stop.sh` — git-first + cached verdict

Ordering is cheapest-first:

1. **git change-detection runs first.** No changed files → `exit 0` with zero
   Python. Only exempt files changed (docs/tests/config/…) → `exit 0`. The
   changelog itself changed → `exit 0`.
2. Only when un-exempt app files changed without a changelog touch does the
   hook need the project verdict (`dct project detect` + `dct project
   manages-changelog`). That verdict is **cached per project-root path**: cache
   file under `${TMPDIR:-/tmp}`, key = `cksum` of the git top-level path, TTL
   **3600 s** (freshness checked with `find -mmin`). A cached `skip:` verdict
   short-circuits; a cached `manages:<slug>` proceeds to the unreleased-entry
   check. On a miss the two detections run once and the verdict is written.
   Stale (over-TTL) and corrupted cache files are tolerated — both are treated
   as a miss and recomputed, never crashing the hook.

Measured fast paths (`time bash …`): no-changes / exempt-only / changelog-touched
all ~40–50 ms (shell + git forks, no Python); the verdict path drops from
~415 ms on a cache miss (two Python starts) to ~50 ms on a cache hit.

## The five rules every dct hook follows

Hooks run in environments that may lack a tty (Claude Code, CI, git
pre-commit) and must never hang or block the host operation:

1. **No interactive prompts** without a tty check.
2. **Consume stdin** — Claude Code supplies JSON there; leaving it unread can
   deadlock downstream commands.
3. **Graceful degradation** when dependencies are missing
   (`command -v dct >/dev/null || exit 0`).
4. **Exit codes carry meaning** — `0` = pass-through, `2` = block with a
   structured reason; other codes avoided.
5. **Strip message content before flag inspection** — commit-message text must
   not produce false flag matches.

Contributor-facing details and code examples live in the repo `CLAUDE.md`
("Hook conventions").
