-- Migration 005: items.short_title — a 2-4 word label shown next to the item
-- number wherever an item is listed or referenced.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via metadata.create_all() + _apply_schema_updates()
-- (_add_column_if_missing: duplicate-safe, lock_timeout on PostgreSQL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related item: dct #817

-- Nullable, no backfill: rows that reference an item fall back to the title
-- compacted to 50 chars (dct/core/text.py::item_label).
ALTER TABLE items ADD COLUMN IF NOT EXISTS short_title VARCHAR(50);
