# dct shared hook library — NOT a hook (never wired into settings).
# Sourced by check-changelog.sh (blocking PreToolUse guard) and
# check-changelog-on-stop.sh (non-blocking Stop reminder): ONE source of the
# branch detection, the per-branch changelog count with its older-CLI
# fallback, and the guidance text (#801, K-33, K-48). Each hook keeps its OWN
# failure policy — this file only reports what happened, it never exits.
#
# Installed next to the hooks under the same name (symlink, or a copy where
# symlinks are unavailable); a symlinked hook also finds it next to its real
# path inside the package. Bash 3.2 compatible (macOS /bin/bash).

# dct_changelog_count PROJECT
#   Counts unreleased changelog entries that may satisfy a commit on the
#   current branch. Sets three globals:
#     DCT_CL_BRANCH   — current branch name, empty on detached HEAD / no git
#     DCT_CL_COUNT    — non-negative integer, or EMPTY when no count could be
#                       obtained (dct or its database broken) — the caller
#                       applies its own failure policy
#     DCT_CL_FALLBACK — 0: per-branch count (`--branch=<b> --include-unbranched`:
#                       this branch + unbranched + server-detected entries,
#                       the K-36 contract lives in core); 1: project-wide count
#                       (detached HEAD, or an older CLI rejected --branch)
#   The count is a gate, never proof that an entry describes the staged
#   change — trailer selection works from the final staged diff (K-36).
#   The branch is passed as ONE `--branch=<value>` word, so a name starting
#   with `-` can never be parsed as an option, and an empty --branch is never
#   passed (K-33).
dct_changelog_count() {
  local project="$1" count=""
  DCT_CL_BRANCH=$(git branch --show-current 2>/dev/null)
  DCT_CL_FALLBACK=0
  if [ -n "$DCT_CL_BRANCH" ]; then
    count=$(dct clog count --project="$project" --branch="$DCT_CL_BRANCH" --include-unbranched 2>/dev/null)
  fi
  case "$count" in
    ''|*[!0-9]*)
      DCT_CL_FALLBACK=1
      count=$(dct clog count --project="$project" 2>/dev/null)
      ;;
  esac
  case "$count" in ''|*[!0-9]*) count="" ;; esac
  DCT_CL_COUNT=$count
}

# dct_changelog_guidance
#   Prints (no trailing newline) what to do about a missing entry, worded for
#   the path dct_changelog_count actually took. Only the per-branch path
#   mentions other branches and offers the metadata move. The command is a
#   TEMPLATE: `<ENTRY_ID>` is explicitly marked as text to replace (pasted
#   unedited, bash refuses the `<` redirection and runs nothing, K-65); its
#   branch argument is quoted WHOLE with `printf %q`, because agents paste
#   this text into a shell and Git allows `;`, `$(...)` and backticks in
#   branch names (K-33).
dct_changelog_guidance() {
  local arg
  if [ "$DCT_CL_FALLBACK" = 0 ] && [ -n "$DCT_CL_BRANCH" ]; then
    arg=$(printf '%q' "--branch=$DCT_CL_BRANCH")
    printf '%s' "No unreleased entry on branch '$DCT_CL_BRANCH' (this branch, unbranched and server-detected entries count; entries written on other branches do not). Only if an existing entry describes THIS specific change, move it here — replace <ENTRY_ID> with the number of that entry first (the line does not run as is): dct clog update <ENTRY_ID> $arg — otherwise add a new entry with /clog."
  elif [ -n "$DCT_CL_BRANCH" ]; then
    printf '%s' "No unreleased entry in the whole project (this dct CLI could not count per branch, so the project-wide count was used). Add a new entry with /clog."
  else
    printf '%s' "No unreleased entry in the whole project (no branch is checked out, so the project-wide count was used). Add a new entry with /clog."
  fi
}
