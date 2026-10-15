-- Migration 016: plan classification provenance (#432).
-- Reference DDL; dct/db.py metadata and duplicate-safe self-healing are authoritative.
-- Legacy history stays unknown; adding revision 0 never invents provenance.
ALTER TABLE plan_sections ADD COLUMN classification_source VARCHAR(16);
ALTER TABLE plan_sections ADD COLUMN classification_provider VARCHAR(32);
ALTER TABLE plan_sections ADD COLUMN classification_model VARCHAR(200);
ALTER TABLE plan_sections ADD COLUMN classified_at TIMESTAMP;
ALTER TABLE plan_sections ADD COLUMN classification_revision INTEGER NOT NULL DEFAULT 0;
