-- Migration 010: the project's PENDING version-file proposal (K-76) —
-- written by `dct release prepare` (the version-sync script ran once in an
-- isolated worktree / copy), consumed and cleared by the stamp that approves
-- it (`dct release apply --proposal-digest`), after which it lives on as
-- releases.proposal.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _apply_schema_updates() (RELEASE_LEDGER_COLUMNS
-- through _add_column_if_missing: duplicate-safe, lock_timeout on
-- PostgreSQL; bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related cards: K-76, K-96 (D3/D4), K-97 (F3)

-- Nullable, no backfill: NULL = nothing prepared. Canonical JSON of
-- core.releases.validate_proposal.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS release_proposal TEXT;
