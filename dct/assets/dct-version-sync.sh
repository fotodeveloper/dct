#!/bin/bash
# dct-version-sync template v1
# scripts/dct-version-sync.sh — writes the new version into this project's
# version files. dct's release (`dct release prepare`, behind /release) runs it
# with the new version as its only argument:
#
#   scripts/dct-version-sync.sh 0.3.0
#
# When: once per release, BEFORE you approve the release, in an isolated
# temporary checkout of the commit being released — so the script runs only
# once it is committed. What it may do: change the version fields of the
# version files, nothing else. Never build, tag, push, publish or call a
# release command from here: it runs before your approval, and the isolated
# checkout is no sandbox (it can reach the network and your home directory).
#
# Each block between ">>> <file>" and "<<< <file>" sets the version in one
# file. `dct project version-sync-script` switches on the block of the version
# file it finds; to add another file, remove the leading "# " from its block.

set -euo pipefail

VERSION="${1:?usage: $0 <new version>}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# edit_file <path> <filter…> — run a filter (sed, awk) over one file and write
# the result back into the same file, so its mode stays. Portable: no `sed -i`
# (its syntax differs between macOS and Linux) and no temporary file; the
# trailing "x" keeps the file's final newlines through the substitution.
edit_file() {
    local file="$PROJECT_ROOT/$1" out
    shift
    out="$("$@" "$file" && printf x)" || return 1
    printf '%s' "${out%x}" > "$file"
}

# >>> VERSION
# printf '%s\n' "$VERSION" > "$PROJECT_ROOT/VERSION"
# <<< VERSION

# >>> pyproject.toml
# # the `version = "…"` line of the [project] table (PEP 621)
# edit_file pyproject.toml sed -E "/^\[project\]/,/^\[/s/^version = \"[^\"]*\"/version = \"$VERSION\"/"
# <<< pyproject.toml

# >>> package.json
# # the first "version" key — the top-level one in a standard package.json
# edit_file package.json awk -v v="$VERSION" '!done && /"version"[[:space:]]*:/ { sub(/"version"[[:space:]]*:[[:space:]]*"[^"]*"/, "\"version\": \"" v "\""); done = 1 } { print }'
# <<< package.json

# >>> Cargo.toml
# # the `version = "…"` line of the [package] table
# edit_file Cargo.toml sed -E "/^\[package\]/,/^\[/s/^version = \"[^\"]*\"/version = \"$VERSION\"/"
# <<< Cargo.toml

# >>> src-tauri/tauri.conf.json
# # the first "version" key — top-level in Tauri v2, package.version in v1
# edit_file src-tauri/tauri.conf.json awk -v v="$VERSION" '!done && /"version"[[:space:]]*:/ { sub(/"version"[[:space:]]*:[[:space:]]*"[^"]*"/, "\"version\": \"" v "\""); done = 1 } { print }'
# <<< src-tauri/tauri.conf.json

# More files: copy a block's line and change the path and pattern — for
# example a Python package's __version__:
#   edit_file src/mypkg/__init__.py sed -E "s/^__version__ = \"[^\"]*\"/__version__ = \"$VERSION\"/"
