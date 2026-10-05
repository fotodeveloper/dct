#!/bin/bash
# dct global Stop hook — non-blocking reminder after every turn.
# Checks if app files changed without unreleased changelog entries.
# Install: ~/.claude/hooks/dct-check-changelog-on-stop.sh
#
# Latency design (#354): the cheap git change-detection runs FIRST, so the
# common "nothing changed" and "only exempt files changed" turns exit with
# ZERO Python cold starts. Only when un-exempt app files changed without a
# changelog touch do we need the project verdict — and that verdict
# (project detect + manages-changelog) is cached per project-root path for
# an hour, so at most one detection per project per hour instead of two
# Python starts every turn.

# Rule 2: drain stdin (Claude Code supplies Stop-hook JSON there).
input=$(cat 2>/dev/null || true)

# Rule 3: graceful on missing deps. git gates the whole hook and is checked
# first because change-detection is the cheapest possible short-circuit.
command -v git >/dev/null || exit 0
command -v dct >/dev/null || exit 0

# ── Fast path 1: no changed files → nothing to remind about (0 Python). ──
changed=$(git diff --name-only 2>/dev/null; git diff --cached --name-only 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
[ -z "$changed" ] && exit 0
changed=$(echo "$changed" | sort -u)

# Exempt patterns — docs/tests/config/meta files never require a changelog.
exempt_re="^\.(claude|github|cursor|vscode|gstack)/|^docs/|^scripts/|^tests/|^test_|conftest\.py$|CLAUDE\.md$|README\.md$|LICENSE|\.gitignore$|^changelog|^CHANGELOG|^VERSION$|pyproject\.toml$|Cargo\.toml$|package\.json$"

# ── Fast path 2: every change is exempt → nothing to remind about (0 Python). ──
app_files=$(echo "$changed" | grep -vE "$exempt_re")
[ -z "$app_files" ] && exit 0

# ── Fast path 3: changelog itself was touched → already satisfied (0 Python). ──
changelog_changed=$(echo "$changed" | grep -iE "^changelog|^CHANGELOG" | head -1)
[ -n "$changelog_changed" ] && exit 0

# Un-exempt app files changed and no changelog touched. Only now do we need
# to know whether this project opts into dct changelog enforcement (#222).
# Cache that verdict per project-root path to avoid two Python cold starts
# on every turn — key = checksum of the git top-level path, TTL 3600s.
root=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
key=$(printf '%s' "$root" | cksum | cut -d' ' -f1)
cache_dir="${TMPDIR:-/tmp}"
cache_file="${cache_dir%/}/dct-changelog-verdict-${key}"
ttl_min=60   # 3600s

# Security: the cache lives under a shared TMPDIR at a predictable path. A
# pre-planted symlink there would make a through-write (`>`) follow the link and
# truncate its target — any user-writable file. Remove a symlink before reading
# (treat as a miss) and write via mktemp-in-same-dir + `mv -f`: rename REPLACES
# the link itself, it never follows it. This also removes partial-read races.
[ -L "$cache_file" ] && rm -f "$cache_file"

# Atomic, symlink-safe verdict write (recompute path only — keeps the fast paths
# free of any mktemp/rename cost). $1 = verdict line.
write_verdict() {
  local tmp
  tmp=$(mktemp "${cache_file}.XXXXXX" 2>/dev/null) || return 0
  printf '%s\n' "$1" >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
  mv -f "$tmp" "$cache_file" 2>/dev/null || rm -f "$tmp"
}

cached=""
if [ -f "$cache_file" ] && [ -n "$(find "$cache_file" -mmin -"$ttl_min" 2>/dev/null)" ]; then
  # Strip NUL bytes BEFORE the command substitution captures the output: a
  # corrupted cache holding NULs would otherwise make bash >=4.4 emit a
  # "command substitution: ignored null byte in input" warning on the Stop
  # hook's stderr (head's 2>/dev/null does not suppress that shell-level note).
  cached=$(tr -d '\0' <"$cache_file" 2>/dev/null | head -n1 || true)
fi

case "$cached" in
  skip:*)     exit 0 ;;                    # cached: not a dct-managed project
  manages:?*)
    project="${cached#manages:}"
    # Corrupted slug (invalid chars) → treat as a miss and recompute.
    case "$project" in
      *[!a-zA-Z0-9._-]*) cached="" ;;
    esac
    ;;
  *)          cached="" ;;                 # miss / empty / corrupted → recompute
esac

if [ -z "$cached" ]; then
  # Cache miss — run the real detection (≤ two Python starts, ≤ once/hour/project).
  project=$(dct project detect 2>/dev/null || true)
  if [ -z "$project" ]; then
    write_verdict 'skip:'
    exit 0
  fi
  if dct project manages-changelog "$project" 2>/dev/null; then
    write_verdict "manages:${project}"
  else
    write_verdict 'skip:'
    exit 0
  fi
fi

# Project manages its changelog in dct. Check dct DB for unreleased entries of
# THIS branch (#801; unbranched and server-detected entries count too, K-36),
# falling back to the project-wide count when an older dct rejects --branch.
# The count logic and the guidance live in the shared lib (K-48). Unlike the
# blocking commit hook, THIS hook's failure policy is to keep REMINDING: a
# missing lib or a failing count yields the generic reminder (non-blocking,
# so erring towards the reminder costs nothing).
#
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
guidance=""
if [ -n "$_lib" ] && . "$_lib"; then
  dct_changelog_count "$project"
  case "$DCT_CL_COUNT" in
    ''|*[!0-9]*) ;;                        # broken count → remind generically
    *)
      [ "$DCT_CL_COUNT" -gt 0 ] && exit 0
      guidance=$(dct_changelog_guidance)
      ;;
  esac
fi

# Reminder (non-blocking — always exit 0)
file_count=$(echo "$app_files" | wc -l | tr -d ' ')
echo ""
echo "📝 Reminder: ${file_count} app file(s) changed in ${project}/ but no changelog entry found."
if [ -n "$guidance" ]; then
  echo "   $guidance"
else
  echo "   Use /clog to add an entry."
fi
echo ""
exit 0
