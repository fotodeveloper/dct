-- Migration 006: changelog_entries.branch / worktree / branch_source — the
-- git branch and worktree a changelog entry was written on, so a release can
-- stamp an explicit set of entries instead of every unreleased one.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via metadata.create_all() + _apply_schema_updates()
-- (_add_column_if_missing: duplicate-safe, lock_timeout on PostgreSQL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related item: dct #801

-- All nullable, no backfill: pre-#801 entries are "unbranched" (NULL).
-- branch_source: 'client' (supplied by / detected in the caller's directory)
-- or 'server' (detected in the MCP server's own working directory).
ALTER TABLE changelog_entries ADD COLUMN IF NOT EXISTS branch VARCHAR(255);
ALTER TABLE changelog_entries ADD COLUMN IF NOT EXISTS worktree VARCHAR(500);
ALTER TABLE changelog_entries ADD COLUMN IF NOT EXISTS branch_source VARCHAR(16);
