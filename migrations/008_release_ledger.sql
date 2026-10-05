-- Migration 008: the release LEDGER — immutable candidate + separate release
-- commit, write-once manifest / proof, explicit abandonment (`releases`), and
-- the project's version-file contract (`projects`).
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via metadata.create_all() + _apply_schema_updates()
-- (RELEASE_LEDGER_COLUMNS through _add_column_if_missing: duplicate-safe,
-- lock_timeout on PostgreSQL; bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related cards: K-60, K-61, K-70, K-73

-- All nullable, no backfill: an older record simply has no release commit,
-- manifest or proof yet.
-- release_commit_sha: set once, on the move to 'committed' or later.
-- manifest: JSON object of the verified artifacts (content hashes); write-once per key.
-- proof: JSON object {"<entry id>": {"commits": [...], "merge_commit", "pr"}}.
-- status gains the terminal 'abandoned' (core/releases.TRANSITIONS).
ALTER TABLE releases ADD COLUMN IF NOT EXISTS release_commit_sha VARCHAR(64);
ALTER TABLE releases ADD COLUMN IF NOT EXISTS manifest TEXT;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS proof TEXT;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS abandoned_at TIMESTAMP;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS abandon_reason TEXT;

-- version_files: JSON array of repo-relative paths (NULL = not learned yet,
-- [] = learned: none); set only with explicit confirmation.
-- version_files_history: JSON array of replaced contracts
-- [{version_files, confirmed_at, replaced_at}] — appended, never rewritten.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS version_files TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS version_files_confirmed_at TIMESTAMP;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS version_files_history TEXT;
