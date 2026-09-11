---
name: clog
description: "[dct] Record implemented changes in the project changelog."
---
<!-- dct-managed-codex-skill -->

# Record a change

Discover the connected DCT MCP `add_changelog` schema and use its separate
top-level fields. Resolve the project from CWD (`project=""`). Inspect the actual
change and existing changelog entries before adding; update an existing entry
when appropriate rather than duplicate it. Write a concise English description
of observable behavior, with the appropriate category and linked item ID when
known. Do not describe planned or unverified behavior as delivered. Return the
entry ID. Recording a change is not permission to commit, bump, publish or release.
