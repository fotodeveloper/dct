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

# Exempt patterns — files that don't require changelog entries
exempt_patterns=(
  "^\.claude/"
  "^\.github/"
  "^\.cursor/"
  "^\.vscode/"
  "^\.gstack/"
  "^docs/"
  "^scripts/"
  "^tests/"
  "^test_"
  "conftest\.py$"
  "CLAUDE\.md$"
  "CLAUDE\.local\.md$"
  "AGENTS\.md$"
  "README\.md$"
  "LICENSE"
  "\.gitignore$"
  "^changelog"
  "^CHANGELOG"
  "^VERSION$"
  "pyproject\.toml$"
  "Cargo\.toml$"
  "package\.json$"
  "biome\.json$"
  "Dockerfile"
  "docker-compose"
)

# Check if ALL staged files are exempt
has_app_code=false
while IFS= read -r file; do
  is_exempt=false
  for pattern in "${exempt_patterns[@]}"; do
    if echo "$file" | grep -qE "$pattern"; then
      is_exempt=true
      break
    fi
  done
  if [ "$is_exempt" = false ]; then
    has_app_code=true
    break
  fi
done <<< "$staged"

if [ "$has_app_code" = false ]; then
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
# commit does not contain, so they do not satisfy the gate; entries with no
# recorded branch (pre-tracking / undetected) still count.
# Version skew: an older dct rejects --branch → fall back to the project-wide
# count. Fail-open: if even that yields no integer (dct or its database is
# broken), let the commit through — a hook must never block the host
# operation because of its own failure. Never pass an empty --branch.
branch=""
if [ "$has_changelog" = false ]; then
  branch=$(git branch --show-current 2>/dev/null)
  count=""
  [ -n "$branch" ] && count=$(dct clog count --project "$project" --branch "$branch" --include-unbranched 2>/dev/null)
  case "$count" in ''|*[!0-9]*) count=$(dct clog count --project "$project" 2>/dev/null);; esac
  case "$count" in ''|*[!0-9]*) exit 0;; esac
  if [ "$count" -gt 0 ]; then
    has_changelog=true
  fi
fi

if [ "$has_changelog" = true ]; then
  exit 0
fi

# Block the commit
reason="App code staged but no changelog entry found for project '$project'"
if [ -n "$branch" ]; then
  reason="$reason on branch '$branch'. Unreleased entries written on other branches do not count — move one here with: dct clog update <id> --branch $branch."
else
  reason="$reason."
fi
reason="$reason Add a changelog entry with /clog or stage a changelog file."
# jq builds the JSON: a branch name may legally contain '"', which string
# interpolation would turn into invalid JSON (jq is a hard dependency above).
jq -cn --arg reason "$reason" \
  '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
exit 2
