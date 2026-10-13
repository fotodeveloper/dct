-- Migration 014: the triage findings ledger (#909) — review findings recorded
-- separately from items: a triage cycle (`triage_cycles`) owns findings
-- (`findings`), each finding owns its fix options (`finding_options`). A
-- finding carries the rubric v2 fields, the coordinator's class, an agent's
-- suggestion, the owner's decision (verbatim), an optional promotion to an
-- item and the recurrence link by normalised fingerprint. Workflow:
-- dct/workflows/triage.py.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _create_missing_tables() (race-safe; an existing
-- database gains the tables with their indexes on its next bootstrap, and
-- _heal_required_indexes completes a missing index — INDEX_HEALED_TABLES;
-- bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related: #909

-- ── Tables ────────────────────────────────────────────────────────────────────

-- One review round of a project. code: e.g. "rev-877-r2", unique among the
-- project's LIVE cycles. closed_at NULL = open; closed_source / closed_notes
-- are the CLOSED block's source and notes (notes joined by blank lines).
-- The four trailing columns are the shared creation-provenance block applied to
-- every mutating ledger table (dct/db.py::_provenance_columns).
CREATE TABLE IF NOT EXISTS triage_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    code            VARCHAR(100) NOT NULL,
    topic           VARCHAR(200) NOT NULL,
    scope           TEXT,
    base_commit     VARCHAR(64),
    rubric_version  SMALLINT NOT NULL DEFAULT 2,
    artifact_url    VARCHAR(500),
    created_by      VARCHAR(200) NOT NULL,
    opened_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at       TIMESTAMP,
    closed_source   VARCHAR(200),
    closed_notes    TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMP,
    actor_type      VARCHAR(16),
    actor_client    VARCHAR(64),
    session_id      VARCHAR(128),
    source          VARCHAR(16)
);

-- seq: the K-n number inside the cycle (unique among the cycle's live rows).
-- fingerprint_norm: lowercase, whitespace collapsed, trailing punctuation
-- stripped — the identity behind first_seen_id (earliest live finding of the
-- same project with an equal fingerprint_norm; NULL = this is the first).
-- Rubric v2 fields under full names: impact = S (0-3), reachability = O
-- ('0'-'3' | '?'), evidence = E (0-3), fix_cost = K (S | M | L), origin
-- (diff | pre | latent), finding_type (security | spec | spec-gap |
-- correctness | robustness | perf | maintainability | opinion).
-- suggested_*: an agent's proposal (the template's SUGGESTED), never a decision.
-- reviewer: who reported it ("codex", "panel:security", ...) — the CARD's src;
-- `source` is the provenance column. class_rec: classify() of the workflow
-- (fix | verify | quick | decide | backlog | reject | note); decision: fix |
-- verify | backlog | reject | note | discuss, NULL = undecided; decision_note:
-- the owner's comment verbatim. decided_by / decision_source: who decided
-- (the owner) and where the owner's words are recorded — NULL while
-- undecided, written together with decided_at. decision_actor_type /
-- decision_actor_client / decision_session_id / decision_actor_source: the
-- provenance of the decision's WRITE (K-23) — the last decision's writer,
-- per Q1; audit, not authentication — written from the entry point's
-- provenance in the same UPDATE as decided_at, all four refreshed on every
-- overwrite (unknown = NULL), NULL while undecided, never backfilled;
-- decision_actor_source is the transport (cli | mcp). The trailing creation
-- provenance columns keep naming the client that recorded the finding.
-- origin and finding_type are required on every finding (rubric §1). Value
-- domains are validated in code.
--
-- Existing databases (created while origin / finding_type were nullable and
-- before the decision columns existed): the bootstrap adds the six decision
-- columns (nullable, no backfill — TRIAGE_LEDGER_COLUMNS) and, on PostgreSQL, sets NOT NULL
-- on origin / finding_type when no row holds NULL there — otherwise nothing
-- is changed and a warning names the rows (a value is never guessed). On
-- SQLite the existing columns stay nullable (no ALTER COLUMN without a table
-- rebuild); write-time validation guards them. See _heal_triage_not_null.
CREATE TABLE IF NOT EXISTS findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            INTEGER NOT NULL REFERENCES triage_cycles(id),
    seq                 INTEGER NOT NULL,
    fingerprint         VARCHAR(500) NOT NULL,
    fingerprint_norm    VARCHAR(500) NOT NULL,
    title               VARCHAR(500) NOT NULL,
    impact              SMALLINT NOT NULL,
    reachability        VARCHAR(1) NOT NULL,
    evidence            SMALLINT NOT NULL,
    fix_cost            VARCHAR(1) NOT NULL,
    origin              VARCHAR(8) NOT NULL,
    finding_type        VARCHAR(16) NOT NULL,
    suggested_decision  VARCHAR(16),
    suggested_opt       VARCHAR(4),
    suggested_note      TEXT,
    suggested_by        VARCHAR(100),
    suggested_at        TIMESTAMP,
    conflict            BOOLEAN NOT NULL DEFAULT FALSE,
    violation           BOOLEAN NOT NULL DEFAULT TRUE,
    dispute             BOOLEAN NOT NULL DEFAULT FALSE,
    open_design         BOOLEAN NOT NULL DEFAULT FALSE,
    area                VARCHAR(500),
    reviewer            VARCHAR(100) NOT NULL,
    context             TEXT,
    class_rec           VARCHAR(16) NOT NULL,
    class_override      VARCHAR(16),
    class_reason        TEXT,
    decision            VARCHAR(16),
    decision_opt        VARCHAR(4),
    decision_note       TEXT,
    decided_at          TIMESTAMP,
    decided_by          VARCHAR(200),
    decision_source     VARCHAR(200),
    decision_actor_type VARCHAR(16),
    decision_actor_client VARCHAR(64),
    decision_session_id VARCHAR(128),
    decision_actor_source VARCHAR(16),
    item_id             INTEGER REFERENCES items(id),
    first_seen_id       INTEGER REFERENCES findings(id),
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at          TIMESTAMP,
    actor_type          VARCHAR(16),
    actor_client        VARCHAR(64),
    session_id          VARCHAR(128),
    source              VARCHAR(16)
);

-- key: the option letter, unique among the finding's live options. At most
-- one recommended option per finding — checked in code.
CREATE TABLE IF NOT EXISTS finding_options (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id      INTEGER NOT NULL REFERENCES findings(id),
    key             VARCHAR(4) NOT NULL,
    text            TEXT NOT NULL,
    why             TEXT,
    is_root         BOOLEAN NOT NULL DEFAULT FALSE,
    is_recommended  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMP,
    actor_type      VARCHAR(16),
    actor_client    VARCHAR(64),
    session_id      VARCHAR(128),
    source          VARCHAR(16)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- Uniqueness among LIVE rows: a soft-deleted cycle / finding / option frees
-- its code / seq / key.
CREATE UNIQUE INDEX IF NOT EXISTS ix_triage_cycles_project_code_live
    ON triage_cycles(project_id, code) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ix_findings_cycle_seq_live
    ON findings(cycle_id, seq) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_findings_cycle
    ON findings(cycle_id);

-- Recurrence lookup (first_seen_id, finding_history).
CREATE INDEX IF NOT EXISTS ix_findings_fingerprint_norm
    ON findings(fingerprint_norm);

CREATE UNIQUE INDEX IF NOT EXISTS ix_finding_options_finding_key_live
    ON finding_options(finding_id, key) WHERE deleted_at IS NULL;
