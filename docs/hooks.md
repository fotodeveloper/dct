# dct hooks — wiring reference

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
