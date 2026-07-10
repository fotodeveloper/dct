#!/bin/bash
# dct global Stop hook — non-blocking reminder after every turn.
# Checks if app files changed without unreleased changelog entries.
# Install: ~/.claude/hooks/dct-check-changelog-on-stop.sh

# Require dct CLI
command -v dct >/dev/null || exit 0

# Check if we're in a dct-registered project
project=$(dct project detect 2>/dev/null)
if [ -z "$project" ]; then
  exit 0
fi

# #222: opt-in — only remind for projects that manage their changelog in dct.
if ! dct project manages-changelog "$project" 2>/dev/null; then
  exit 0
fi

# Exempt patterns
exempt_re="^\.(claude|github|cursor|vscode|gstack)/|^docs/|^scripts/|^tests/|^test_|conftest\.py$|CLAUDE\.md$|README\.md$|LICENSE|\.gitignore$|^changelog|^CHANGELOG|^VERSION$|pyproject\.toml$|Cargo\.toml$|package\.json$"

# Check for modified/staged/untracked app files
changed=$(git diff --name-only 2>/dev/null; git diff --cached --name-only 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
[ -z "$changed" ] && exit 0

# Deduplicate and filter exempt files
app_files=$(echo "$changed" | sort -u | grep -vE "$exempt_re")
[ -z "$app_files" ] && exit 0

# Check if changelog was modified
changelog_changed=$(echo "$changed" | sort -u | grep -iE "^changelog|^CHANGELOG" | head -1)
if [ -n "$changelog_changed" ]; then
  exit 0
fi

# Check dct DB for unreleased entries
count=$(dct clog count --project "$project" 2>/dev/null)
if [ "${count:-0}" -gt 0 ]; then
  exit 0
fi

# Reminder (non-blocking — always exit 0)
file_count=$(echo "$app_files" | wc -l | tr -d ' ')
echo ""
echo "📝 Reminder: ${file_count} app file(s) changed in ${project}/ but no changelog entry found."
echo "   Use /clog to add an entry."
echo ""
exit 0
