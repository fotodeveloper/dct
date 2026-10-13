#!/bin/bash
# dct global hook — PreToolUse for Bash(git commit*)
# Blocks commits without changelog entries (staged changelog file OR dct DB entry).
# Opt-in (#222): enforces ONLY projects with manages_changelog=TRUE — a project
# keeping its own changelog opts out via `dct project changelog <slug> off`.
# Install: ~/.claude/hooks/dct-check-changelog.sh

# Consume stdin (required by hook protocol)
input=$(cat)

# Require jq
command -v jq >/dev/null || exit 0

# Require dct CLI
command -v dct >/dev/null || exit 0

# Extract command from hook input
command=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)

# Only check git commit commands
case "$command" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

# Skip amend commits (strip message content to avoid false matches)
flags=$(echo "$command" | sed 's/ -m [^ ]*//g; s/ -m ".*"//g; s/ --message[= ][^ ]*//g; s/ --message ".*"//g')
case "$flags" in
  *"--amend"*) exit 0 ;;
esac

# Release-commit bypass: skip hook entirely when commit message starts with "release:"
# /release skill produces clean release commits with zero unreleased entries after stamping.
# Two forms to handle: inline -m "release: ..." and HEREDOC heredoc where the first line
# of the message body is "release: ...".

# Form 1 — inline -m "..." / -m '...' / --message="..."
inline_msg=$(echo "$command" | sed -n \
  -e 's/.*-m "\([^"]*\)".*/\1/p' \
  -e "s/.*-m '\([^']*\)'.*/\1/p" \
  -e 's/.*--message="\([^"]*\)".*/\1/p' \
  | head -1)
case "$inline_msg" in
  release:*) exit 0 ;;
esac

# Form 2 — HEREDOC: multi-line command containing a line that starts with "release:"
if echo "$command" | grep -qE '^release:'; then
  exit 0
fi

# Check if we're in a dct-registered project
project=$(dct project detect 2>/dev/null)
if [ -z "$project" ]; then
  exit 0  # Not a dct project — pass through
fi

# #222: opt-in — only enforce projects that manage their changelog in dct.
# A project registered for item-tracking but keeping its own changelog opts out
# via `dct project changelog <slug> off` and is skipped here.
if ! dct project manages-changelog "$project" 2>/dev/null; then
  exit 0
fi

# Get staged files
staged=$(git diff --cached --name-only 2>/dev/null)
if [ -z "$staged" ]; then
  exit 0  # Nothing staged
fi

# The shared lib (K-48) holds the count, the guidance AND the exemption
# policy shared with the Stop hook (K-59). THIS hook's failure policy is
# fail-OPEN: no lib → let the commit through.
# Locate the lib next to this script's REAL path (a symlinked install points
# into the package) or next to the invoked path (a copied install carries a
# copy). Portable symlink walk: plain `readlink`, no GNU `-f`.
_dirof() { case "$1" in */*) printf '%s' "${1%/*}" ;; *) printf . ;; esac; }
_self=${BASH_SOURCE[0]}
_hops=0
while [ -L "$_self" ] && [ "$_hops" -lt 40 ]; do
  _link=$(readlink "$_self") || break
  case "$_link" in /*) _self=$_link ;; *) _self=$(_dirof "$_self")/$_link ;; esac
  _hops=$((_hops + 1))
done
_lib=""
for _dir in "$(_dirof "$_self")" "$(_dirof "${BASH_SOURCE[0]}")"; do
  if [ -f "$_dir/dct-changelog-count.lib.sh" ]; then
    _lib=$_dir/dct-changelog-count.lib.sh
    break
  fi
done
[ -n "$_lib" ] || exit 0
# shellcheck source=dct-changelog-count.lib.sh
. "$_lib" || exit 0

# Exempt files (docs/tests/config/meta, the project instruction files) never
# require a changelog entry: when every staged file is exempt, pass through.
app_files=$(printf '%s\n' "$staged" | dct_changelog_app_files)
if [ -z "$app_files" ]; then
  exit 0  # All files are exempt
fi

# Check for changelog in staged files
has_changelog=false
while IFS= read -r file; do
  case "$file" in
    changelog*|CHANGELOG*|changelog_unreleased*) has_changelog=true; break ;;
  esac
done <<< "$staged"

# If no changelog file staged, check dct database for unreleased entries of
# THIS branch (#801) — entries written on other branches describe code this
# commit does not contain, so they do not satisfy the gate; unbranched and
# server-detected entries still count (K-36). The count logic and the deny
# guidance live in the shared lib (K-48); THIS hook's failure policy is
# fail-OPEN: no integer even from the project-wide fallback (dct or its
# database is broken) → let the commit through — a hook must never block the
# host operation because of its own failure.
if [ "$has_changelog" = true ]; then
  exit 0
fi

dct_changelog_count "$project"
case "$DCT_CL_COUNT" in ''|*[!0-9]*) exit 0 ;; esac
if [ "$DCT_CL_COUNT" -gt 0 ]; then
  exit 0
fi

# Block the commit
reason="App code staged but no changelog entry found for project '$project'. $(dct_changelog_guidance) Or stage a changelog file."
# jq builds the JSON: a branch name may legally contain '"', which string
# interpolation would turn into invalid JSON (jq is a hard dependency above).
jq -cn --arg reason "$reason" \
  '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
exit 2
