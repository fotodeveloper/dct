#!/bin/bash
# dct-version-sync template v1
# dct-version-sync.sh — dct's OWN version sync (pyproject.toml and
# dct/__init__.py). Not a template: other projects get the generic one,
# dct/assets/dct-version-sync.sh, through `dct project version-sync-script`
# (/dct-init offers it).
#
# Run by `dct release prepare` (once, in an isolated checkout of the release
# candidate — it must be committed there) when found at:
#   scripts/dct-version-sync.sh  OR  scripts/version-sync.sh
#
# It must change ONLY the version files: prepare runs it BEFORE the owner
# approves the release, and its isolated checkout is no sandbox — never
# build, tag, push, publish or call a release command from here.
#
# Usage: ./scripts/dct-version-sync.sh <new_version>

set -euo pipefail

VERSION="${1:?Usage: $0 <version>}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Syncing version $VERSION in $PROJECT_ROOT"

# ── Optional helper: has_changes <path> ────────────────────────────
# Returns 0 (true) when the given file has uncommitted changes OR is
# new to the tree (staged or unstaged). Use this to skip bumping
# version on sub-packages that didn't change — especially useful in
# monorepos (multiple package.json / Cargo.toml / tauri.conf.json).
#
# Usage:
#   if has_changes "desktop/src-tauri/Cargo.toml"; then
#       sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" \
#           "$PROJECT_ROOT/desktop/src-tauri/Cargo.toml"
#   fi
#
# The check runs `git diff` with both staged and working-tree scopes,
# plus `git ls-files --others --exclude-standard` for untracked.
has_changes() {
    local path="$1"
    # Empty path = no match
    [ -z "$path" ] && return 1
    # Modified (unstaged)
    git -C "$PROJECT_ROOT" diff --name-only -- "$path" 2>/dev/null | grep -qx "$path" && return 0
    # Modified (staged)
    git -C "$PROJECT_ROOT" diff --cached --name-only -- "$path" 2>/dev/null | grep -qx "$path" && return 0
    # Untracked (new file)
    git -C "$PROJECT_ROOT" ls-files --others --exclude-standard -- "$path" 2>/dev/null | grep -qx "$path" && return 0
    return 1
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  SYNC TARGETS — customize for your project                      ║
# ║  Uncomment and adjust the sections that apply.                   ║
# ║  Wrap each block in `if has_changes "<path>"; then ... fi`       ║
# ║  when working in a monorepo with independently-versioned         ║
# ║  sub-packages so you only bump what moved.                       ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── Python (pyproject.toml + dct/__init__.py fallback) ───────────
# dct is a single-package repo, not a monorepo — always sync on release
# (no `has_changes` gate, which would skip the bump when pyproject.toml
# hadn't been hand-edited between releases).
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" "$PROJECT_ROOT/pyproject.toml"
sed -i '' "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" "$PROJECT_ROOT/dct/__init__.py"

# ── Rust (Cargo.toml — workspace root) ────────────────────────────
# sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" "$PROJECT_ROOT/Cargo.toml"

# ── Rust (Tauri — src-tauri/Cargo.toml) ──────────────────────────
# Monorepo pattern — bump only when this sub-package has local changes:
# if has_changes "desktop/src-tauri/Cargo.toml"; then
#     sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" "$PROJECT_ROOT/desktop/src-tauri/Cargo.toml"
# fi

# ── Tauri config ─────────────────────────────────────────────────
# Use jq to update version in tauri.conf.json:
# jq --arg v "$VERSION" '.version = $v' "$PROJECT_ROOT/desktop/src-tauri/tauri.conf.json" > tmp.$$.json
# mv tmp.$$.json "$PROJECT_ROOT/desktop/src-tauri/tauri.conf.json"

# ── Node.js (package.json — root) ────────────────────────────────
# jq --arg v "$VERSION" '.version = $v' "$PROJECT_ROOT/package.json" > tmp.$$.json
# mv tmp.$$.json "$PROJECT_ROOT/package.json"

# ── Node.js (package.json — workspace member) ────────────────────
# jq --arg v "$VERSION" '.version = $v' "$PROJECT_ROOT/apps/api/package.json" > tmp.$$.json
# mv tmp.$$.json "$PROJECT_ROOT/apps/api/package.json"

# ── Claude Code plugin (plugin.json) ─────────────────────────────
# jq --arg v "$VERSION" '.version = $v' "$PROJECT_ROOT/plugin.json" > tmp.$$.json
# mv tmp.$$.json "$PROJECT_ROOT/plugin.json"

# ── Go (embed or ldflags — no file to edit) ──────────────────────
# Go typically reads version at build time via -ldflags or embed.
# No file sync needed — dct DB is the source of truth.

# ── Claude Code SKILL.md frontmatter ─────────────────────────────
# for skill in "$PROJECT_ROOT"/skills/*/SKILL.md; do
#   sed -i '' "s/^version: .*/version: $VERSION/" "$skill"
# done

# ── Swift (Info.plist) ───────────────────────────────────────────
# /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PROJECT_ROOT/Info.plist"

echo "✓ Version $VERSION synced"
