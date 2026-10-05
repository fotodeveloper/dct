-- Migration 007: durable release records (`releases`) — one row per release
-- of a project version, written in the SAME transaction as the changelog
-- stamp and advanced forward-only by the release workflow, so an interrupted
-- release can be resumed instead of guessed.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via metadata.create_all() (checkfirst — an existing
-- database gains the table on its next bootstrap, later bootstraps issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related card: K-41 (variant b)

-- ── Tables ────────────────────────────────────────────────────────────────────

-- version: normalized (dct/core/version.py). mode: all | selected | version_only.
-- entry_ids: JSON array of the release's exact entry set ([] = version-only).
-- status (forward only): stamped → files_written → committed → tagged → completed.
-- The four trailing columns are the shared creation-provenance block applied to
-- every mutating ledger table (dct/db.py::_provenance_columns).
CREATE TABLE IF NOT EXISTS releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id),
    version        VARCHAR(50) NOT NULL,
    mode           VARCHAR(16) NOT NULL,
    entry_ids      TEXT NOT NULL,
    candidate_sha  VARCHAR(64),
    checkout_root  VARCHAR(500),
    status         VARCHAR(16) NOT NULL,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at   TIMESTAMP,
    deleted_at     TIMESTAMP,
    actor_type     VARCHAR(16),
    actor_client   VARCHAR(64),
    session_id     VARCHAR(128),
    source         VARCHAR(16)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- At most one LIVE record per (project, version). Core additionally compares
-- under the version contract (1.5 == 1.5.0) under the project write lock.
CREATE UNIQUE INDEX IF NOT EXISTS ix_releases_project_version_live
    ON releases(project_id, version) WHERE deleted_at IS NULL;
