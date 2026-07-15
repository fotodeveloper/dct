#!/usr/bin/env bash
# dct-task-mirror.sh — PreToolUse hook for Claude Code.
#
# Mirrors TaskCreate / TodoWrite calls to dct checkpoints. Lean shell
# dispatcher: drains stdin, jq-checks the tool name, then immediately
# nohups the backgrounded `dct _task-mirror` worker and exits. Nothing
# that touches the dct database or config runs on this path — the worker
# owns the config gate ([mirror].task_to_dct: auto/ask/off) and the
# project autodetect, so a synchronous Python cold start never blocks the
# tool call (#354 hook latency).
#
# Each todo becomes a `kind='cc-task'` checkpoint on a session-scoped item
# titled "CC task list — session <id>". cc-task is non-blocking for
# resolve_item gate semantics.
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

# Fast path: only the two task tools proceed; everything else exits before
# any worker is spawned.
tool=$(echo "$input" | jq -r '.tool_name // empty' 2>/dev/null || true)
case "$tool" in
    TaskCreate|TodoWrite) ;;
    *) exit 0 ;;
esac

log_dir="${TMPDIR:-/tmp}"
log="${log_dir%/}/dct-task-mirror-$(date +%s)-$$.log"

# Fire-and-forget. The background subshell pipes the payload to the internal
# CLI worker (which reads config, autodetects the project, and does the DB
# work) and logs any output for diagnosis. session_id travels inside the
# payload — the worker reads it there.
{
    nohup bash -c "printf '%s' \"\$1\" | dct _task-mirror >>\"\$2\" 2>&1" \
        _ "$input" "$log" &
    disown
} </dev/null >/dev/null 2>&1

exit 0
