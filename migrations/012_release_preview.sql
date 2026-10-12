-- Migration 012: the project's PREVIEW slot of the one-dialog release (#877)
-- — written by `dct release prepare --preview` (the consent view of each
-- variant the owner is shown, their proposal contents and the work /
-- progress state), read by `dct release apply --consent`, cleared by the
-- stamp that releases it and by `prepare --discard-preview`; a preview and
-- a prepare replace each other's slot (core/releases.py: set_preview,
-- clear_preview, discard_preview).
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _apply_schema_updates() (RELEASE_LEDGER_COLUMNS
-- through _add_column_if_missing: duplicate-safe, lock_timeout on
-- PostgreSQL; bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related: #877

-- Nullable, no backfill: NULL = no preview. Canonical JSON of
-- core.releases.validate_preview.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS release_preview TEXT;
