#!/usr/bin/env bash
# dct-plan-autoscan.sh — PostToolUse hook for Claude Code.
#
# Rescans plan-like markdown files (plans, specs, ADRs, roadmaps, backlogs,
# retros) after an Edit/Write/MultiEdit. Runs in the background so the user
# never waits. Handles brand-new plans (ULID injection on first save) and
# re-sync of already-tracked plans.
#
# Follows the five hook rules from dct docs/hooks.md:
#   1. No interactive prompts (non-interactive only).
#   2. Consume stdin (Claude Code hooks supply JSON there).
#   3. Graceful when deps missing (command -v dct / jq).
#   4. Exit codes meaningful: 0 = pass-through, non-zero avoided.
#   5. No message-content inspection needed here (not a commit hook).
set -euo pipefail

# Rule 2: consume stdin even if we partially inspect it.
input=$(cat)

# Rule 3: graceful on missing deps — dct or jq absent means no-op.
command -v dct >/dev/null || exit 0
command -v jq  >/dev/null || exit 0

# Extract tool name + file path from the JSON payload CC hooks pass.
tool=$(echo "$input" | jq -r '.tool_name // empty' 2>/dev/null || true)
file=$(echo "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)

# Only trigger on edit/write-style tools.
case "$tool" in
    Edit|Write|MultiEdit) ;;
    *) exit 0 ;;
esac

# Only act on .md files matching our discovery patterns.
case "$file" in
    */docs/plans/*.md|*/docs/specs/*.md|*/docs/adr/*.md|*/docs/retros/*.md) ;;
    */ROADMAP.md|*ROADMAP.md|*/TODOS.md|*TODOS.md|*/FUTURE.md|*FUTURE.md) ;;
    */BACKLOG.md|*BACKLOG.md) ;;
    *) exit 0 ;;
esac

# Must exist (edit may have been rejected / the file was deleted).
[ -f "$file" ] || exit 0

# Fire-and-forget: detach a background rescan, log to a unique temp file so
# parallel hook invocations don't clobber each other. The `disown` is key —
# without it, the hook would block until the subprocess exits, defeating the
# whole point of the background pattern.
log_dir="${TMPDIR:-/tmp}"
log="${log_dir%/}/dct-rescan-$(date +%s)-$$.log"
nohup dct plan rescan "$file" --quiet --resolve-uncertain >"$log" 2>&1 &
disown

exit 0
