---
name: clog
description: "[dct] Record implemented changes in the project changelog."
---
<!-- dct-managed-codex-skill -->

# Record a change

Discover the connected DCT MCP `add_changelog` schema and use its separate
top-level fields. Resolve the project from CWD (`project=""`). Inspect the actual
change and existing changelog entries before adding; update an existing entry
when appropriate rather than duplicate it. Before `add_changelog`, run
`git branch --show-current` and `git rev-parse --show-toplevel` in your current
working directory and pass them as `branch` and `worktree` (omit `branch` when
detached); the server's own directory is the session start directory, not
necessarily yours. If the ack reports `branch_source` "server" (with a
`warning`), the values describe the server's checkout, not yours: confirm or
correct them at once with `update_changelog` (pass your `branch` and
`worktree`) — a server-detected entry is never treated as confirmed. Write a concise English description of observable behavior,
with the appropriate category and linked item ID when known. Do not describe
planned or unverified behavior as delivered. Return the entry ID. Recording a
change is not permission to commit, bump, publish or release.
