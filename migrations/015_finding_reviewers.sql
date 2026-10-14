-- Migration 015: reviewer identity of triage findings (#930 §2a) — the
-- canonical reviewer tokens of a finding ("codex", "panel:security"), one
-- row per (finding, token), next to the free-text display value
-- findings.reviewer (the CARD's `src`). A finding recorded without tokens has
-- no rows: reviewer_precision counts it under "(unattributed)", and a token is
-- never guessed from `src`. No backfill: findings recorded before this
-- migration stay unattributed. Also (K-40) findings.decision_text_origin:
-- the "Punkt wyjścia: …" line of the pasted decision text that produced a
-- decision. Workflow: dct/workflows/triage.py.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _create_missing_tables() (race-safe; an existing
-- database gains the table with its index on its next bootstrap, and
-- _heal_required_indexes completes a missing index — INDEX_HEALED_TABLES;
-- bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related: #930, #909

-- ── Tables ────────────────────────────────────────────────────────────────────

-- reviewer: a token matching ^[a-z0-9][a-z0-9:._-]*$, lowercased on write
-- (validated in code; refused otherwise, never truncated). The four trailing
-- columns are the shared creation-provenance block applied to every mutating
-- ledger table (dct/db.py::_provenance_columns).
CREATE TABLE IF NOT EXISTS finding_reviewers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id      INTEGER NOT NULL REFERENCES findings(id),
    reviewer        VARCHAR(50) NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMP,
    actor_type      VARCHAR(16),
    actor_client    VARCHAR(64),
    session_id      VARCHAR(128),
    source          VARCHAR(16)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- One row per (finding, token) among LIVE rows: a soft-deleted row frees its
-- token. The leading column serves the per-finding lookups.
CREATE UNIQUE INDEX IF NOT EXISTS ix_finding_reviewers_finding_reviewer_live
    ON finding_reviewers(finding_id, reviewer) WHERE deleted_at IS NULL;

-- ── Columns ───────────────────────────────────────────────────────────────────

-- K-40: the prefill line ("Punkt wyjścia: propozycje <by> …") of the pasted
-- decision text, verbatim — it proves the form was seeded from an agent's
-- proposals, not who wrote each note. Written with the decision stamp by
-- decide_findings_from_text, reset to NULL by every other decision write;
-- never folded into decision_note / decision_source. Nullable, no backfill.
-- The runtime adds it through TRIAGE_LEDGER_COLUMNS (dct/db.py) — once, on
-- the next bootstrap; bootstraps 2-3 issue no DDL.
ALTER TABLE findings ADD COLUMN decision_text_origin TEXT;
