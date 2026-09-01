-- Migration 004: normalized tags — global `tags` dictionary + `item_tags`
-- link table, replacing the JSON-array-in-TEXT `items.tags` column.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via metadata.create_all() + _apply_schema_updates().
-- Keep in sync with db.py when columns/indexes change.
--
-- Related item: dct #611
-- Target release: v1.5.0

-- ── Tables ────────────────────────────────────────────────────────────────────

-- GLOBAL / user-scoped dictionary: deliberately NO project_id — one name means
-- the same thing in every project (the item lists that filter on it stay
-- project-scoped). `name` has no DB length limit on purpose: the 50-char rule
-- is an API rule for NEW names only, so legacy names migrated from the frozen
-- items.tags column always fit.
-- The four trailing columns are the shared creation-provenance block applied to
-- every mutating ledger table (dct/db.py::_provenance_columns).
CREATE TABLE IF NOT EXISTS tags (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    description    TEXT,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at     TIMESTAMP,
    actor_type     VARCHAR(16),
    actor_client   VARCHAR(64),
    session_id     VARCHAR(128),
    source         VARCHAR(16)
);

-- Mirror of item_sprints: composite PK, links are soft-removed (removed_at) and
-- reactivated in place rather than re-inserted.
CREATE TABLE IF NOT EXISTS item_tags (
    item_id        INTEGER NOT NULL REFERENCES items(id),
    tag_id         INTEGER NOT NULL REFERENCES tags(id),
    assigned_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at     TIMESTAMP,
    PRIMARY KEY (item_id, tag_id)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS ix_tags_name_active
    ON tags(name) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_item_tags_tag_active
    ON item_tags(tag_id) WHERE removed_at IS NULL;

-- ── Backfill from the frozen items.tags column (idempotent) ───────────────────
-- Python equivalent lives in dct/db.py::_backfill_item_tags and is the
-- authoritative implementation; the runtime never executes SQL from this file.
-- It is the ONLY sanctioned reader of `items.tags`, which stays DEPRECATED and
-- frozen until it is dropped at 2.0.0.
--
-- Shape: candidate items are those with a non-empty JSON value and NO row in
-- item_tags at all (anti-join guard, per ITEM — an item that has ever spoken
-- through the join table owns its own tag set and is never re-seeded). Parsing
-- is tolerant (malformed JSON / non-string members are dropped with a stderr
-- note, never raised — a raise here would break every bootstrap). Dictionary
-- lookup is get-or-create with resurrection of a soft-deleted row, and WITHOUT
-- the similarity guard: historical data is not a typo to be corrected. After a
-- migrating run, a one-shot stderr report suggests `dct tags merge` for
-- near-duplicate names — a suggestion, never a block, never an auto-merge.
--
-- See _backfill_item_tags() for the authoritative implementation.
