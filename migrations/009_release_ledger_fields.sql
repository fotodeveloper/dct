-- Migration 009: release-ledger fields of the third fix round — the verified
-- release mode, the executor, the owner's consent, the approved version-file
-- proposal and the render settings on `releases`; the release executor and
-- the PR-reference render opt-in on `projects`. The `releases` status set
-- gains 'pushed' (between 'tagged' and 'completed'; no DDL — VARCHAR(16)).
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _apply_schema_updates() (RELEASE_LEDGER_COLUMNS
-- through _add_column_if_missing: duplicate-safe, lock_timeout on
-- PostgreSQL; bootstraps 2-3 issue no DDL). The bootstrap also re-creates a
-- missing ix_releases_project_version_live on an existing `releases` table
-- (K-93 A2, db._heal_required_indexes).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related cards: K-76, K-85, K-90, K-93, K-94, K-97

-- All nullable, no backfill: an older record is unverified (release_mode
-- NULL), has no recorded executor, consent, proposal or render.
-- release_mode: 'git' | 'without_git' (write-once).
-- executor: 'dct' | 'own', copied from projects.release_executor at stamp time.
-- consent / proposal / render: JSON objects, write-once (core/releases.py).
ALTER TABLE releases ADD COLUMN IF NOT EXISTS release_mode VARCHAR(16);
ALTER TABLE releases ADD COLUMN IF NOT EXISTS executor VARCHAR(8);
ALTER TABLE releases ADD COLUMN IF NOT EXISTS consent TEXT;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS proposal TEXT;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS render TEXT;

-- release_executor: who performs a project's release effects (default dct).
-- changelog_pr_refs: render `(#N)` PR references (opt-in, default off;
-- SQLite stores the default as 0).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS release_executor VARCHAR(8) NOT NULL DEFAULT 'dct';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS changelog_pr_refs BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS ix_releases_project_version_live
    ON releases (project_id, version) WHERE deleted_at IS NULL;
