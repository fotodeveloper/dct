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
| `dct-check-changelog.sh` | `PreToolUse` / `Bash` | blocks `git commit` when app code changed but no unreleased changelog entry exists; opt-in per project (`dct project changelog <slug> on\|off`); bypasses `release:` commits and doc-only changes |
| `dct-check-changelog-on-stop.sh` | `Stop` | session-end reminder if unreleased entries are pending (same opt-in gate) |
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
