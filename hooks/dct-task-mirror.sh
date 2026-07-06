#!/usr/bin/env bash
# dct-task-mirror.sh — PreToolUse hook for Claude Code.
#
# Mirrors TaskCreate / TodoWrite calls to dct checkpoints. Thin shell
# dispatcher: validates payload, checks config gate, detects project,
# then delegates actual work to `dct _task-mirror` CLI subcommand (which
# uses the venv-installed Python with dct importable — shell/system
# python cannot reliably find dct on PYTHONPATH).
#
# Each todo becomes a `kind='cc-task'` checkpoint on a session-scoped item
# titled "CC task list — session <id>". cc-task is non-blocking for
# resolve_item gate semantics.
#
# Respects [mirror].task_to_dct config: auto (default) / ask / off.
#
# Follows the five hook rules from dct CLAUDE.md:
#   1. No interactive prompts.
#   2. Consume stdin.
#   3. Graceful on missing deps.
#   4. exit 0 on every path.
#   5. No commit-message inspection.
set -euo pipefail

# Rule 2: drain stdin once; reuse for both tool detection and forwarding.
input=$(cat)

# Rule 3: graceful on missing deps.
command -v jq  >/dev/null || exit 0
command -v dct >/dev/null || exit 0

tool=$(echo "$input" | jq -r '.tool_name // empty' 2>/dev/null || true)
session_id=$(echo "$input" | jq -r '.session_id // "unknown"' 2>/dev/null || true)

case "$tool" in
    TaskCreate|TodoWrite) ;;
    *) exit 0 ;;
esac

# Config gate.
mode=$(dct config get mirror.task_to_dct 2>/dev/null || echo "auto")
case "$mode" in
    off|ask) exit 0 ;;
    auto|"") ;;
    *) exit 0 ;;
esac

# Project autodetect (hook CWD = current project).
project=$(dct project detect 2>/dev/null || true)
[ -z "$project" ] && exit 0

log_dir="${TMPDIR:-/tmp}"
log="${log_dir%/}/dct-task-mirror-$(date +%s)-$$.log"

# Fire-and-forget. The background subshell pipes the payload to the
# internal CLI worker and logs any output for diagnosis.
{
    nohup bash -c "printf '%s' \"\$1\" | dct _task-mirror --session-id \"\$2\" --project \"\$3\" >>\"\$4\" 2>&1" \
        _ "$input" "$session_id" "$project" "$log" &
    disown
} </dev/null >/dev/null 2>&1

exit 0
