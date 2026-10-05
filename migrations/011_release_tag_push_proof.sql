-- Migration 011: proof of the tag and of the push in the release ledger
-- (K-115 G4) — a status alone is never proof. `releases.tag_proof` holds the
-- VERIFIED identity of the release tag, `releases.push_proof` the verified
-- push target; the ledger refuses 'tagged' / 'pushed' / 'completed' of a
-- record with git that does not carry them (core/releases.py:
-- check_proof_requirements, check_record_consistency).
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _apply_schema_updates() (RELEASE_LEDGER_COLUMNS
-- through _add_column_if_missing: duplicate-safe, lock_timeout on
-- PostgreSQL; bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related cards: K-115 (G4), K-94 (B1)

-- Both nullable, NO backfill: an older record keeps NULL = "not proven" —
-- no confirmation is invented for old history. Such a record that is still
-- unfinished gets its proof with its next progress write (dct release
-- resume re-verifies the repository; a ledger-path caller reports it).
-- tag_proof: canonical JSON {name, object, commit, annotated, recorded_at}
--   (object = what refs/tags/<name> resolves to; commit = the release
--   commit it targets; recorded_at = when the ledger stored it).
-- push_proof: canonical JSON {target: {remote, url, branch, tag},
--   tag_commit, branch_tip, recorded_at}.
-- Write-once (core/releases.advance_release / reconstruct_release).
ALTER TABLE releases ADD COLUMN IF NOT EXISTS tag_proof TEXT;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS push_proof TEXT;
