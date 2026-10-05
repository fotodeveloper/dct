# Unreleased

## Fixed

- Require an explicit confirmation question in Codex handoff and pickup skills instead of ending with an approval reminder.
- Reject outer function-call wrappers in MCP free-text fields and tag elements before database access (#427).
- Refresh unchanged managed skill copies, preserve custom MCP settings and quote hook paths containing spaces.
- Keep symlinked configuration and instruction files attached to their existing targets.
- Use coherent workflow snapshots and serialize cooperating sprint/handoff writers on SQLite and PostgreSQL (#430).

## Added

- Add native Codex hook definitions and a bounded event bridge for shared changelog checks, stop reminders, worktree-scoped autoscan and append-only plan mirroring; keep installation separate from Codex trust (#690).
- Add an opt-in three-arm Codex benchmark with persisted model-step usage, identical source payloads and offline validity gates (#688).
- Share an integration contract and conservative user-file ownership helpers across Claude Code and Codex.
- Add the Codex MCP adapter, four packaged workflow skills and managed AGENTS.md instructions.
- Add `dct integrate install/status/repair/uninstall` and read-only `dct doctor --json` with per-Python dependency checks, including current editable-source requirements.

## Changed

- Describe DCT as queryable work state, distinguish released and development client capabilities, and document contributor sign-off and project identity guidance (#415).
- Refresh only detected client integrations on upgrade; retain existing instruction and hook opt-ins.
- Default new plan-classification configurations to local rules; honor explicit command providers and legacy settings without requiring Claude for basic ingestion (#432, partial).
- Refresh the supported dependency baseline and resolver lock, including MCP 2.2, SQLAlchemy 2.0.52, psycopg 3.3.5, Markdown 3.10.3 and Ruff 0.16.7 (#687).
