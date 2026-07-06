-- Migration 003: plans, plan_sections, plan_checkpoints, sprints,
-- item_sprints, decisions — plus auto-migration of legacy
-- items.in_current_sprint flag into the new sprints/item_sprints tables.
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via metadata.create_all() + _apply_schema_updates().
-- Keep in sync with db.py when columns/indexes change.
--
-- Spec: docs/superpowers/specs/2026-04-17-plans-roadmaps-decisions-design.md
-- Related item: dct #26
-- Target release: v0.2.0

-- ── Tables ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sprints (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    name               VARCHAR(200) NOT NULL,
    code               VARCHAR(50),
    kind               VARCHAR(20) NOT NULL DEFAULT 'sprint',
    status             VARCHAR(20) NOT NULL DEFAULT 'planned',
    goal               TEXT,
    started_at         TIMESTAMP,
    ended_at           TIMESTAMP,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plans (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ulid               VARCHAR(26) NOT NULL UNIQUE,
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    slug               VARCHAR(200) NOT NULL,
    title              VARCHAR(500) NOT NULL,
    kind               VARCHAR(20) NOT NULL,   -- plan|spec|roadmap|adr|retro|backlog
    source_file        VARCHAR(1000),          -- NULL for DB-only roadmap
    source_hash        VARCHAR(64),            -- sha256 of file at last scan
    status             VARCHAR(20) NOT NULL DEFAULT 'active',
    sprint_id          INTEGER REFERENCES sprints(id),
    imported_at        TIMESTAMP,              -- NULL = first-import gate open
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at        TIMESTAMP,
    deleted_at         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plan_sections (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    ulid                     VARCHAR(26) NOT NULL UNIQUE,
    plan_id                  INTEGER NOT NULL REFERENCES plans(id),
    parent_section_id        INTEGER REFERENCES plan_sections(id),
    heading                  TEXT NOT NULL,
    depth                    INTEGER NOT NULL,
    slug                     VARCHAR(200),
    sort_order               INTEGER NOT NULL DEFAULT 0,
    section_type             VARCHAR(30) NOT NULL DEFAULT 'generic',
    section_type_confidence  VARCHAR(10) DEFAULT 'high',
    needs_llm_review         BOOLEAN NOT NULL DEFAULT FALSE,
    status                   VARCHAR(20) NOT NULL DEFAULT 'pending',
    source_lines             VARCHAR(50),
    sprint_id                INTEGER REFERENCES sprints(id),
    converted_to_item_id     INTEGER REFERENCES items(id),
    created_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at               TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plan_checkpoints (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ulid                   VARCHAR(26) NOT NULL UNIQUE,
    plan_section_id        INTEGER NOT NULL REFERENCES plan_sections(id),
    text                   TEXT NOT NULL,
    status                 VARCHAR(10) NOT NULL DEFAULT 'pending',
    sort_order             INTEGER NOT NULL DEFAULT 0,
    converted_to_item_id   INTEGER REFERENCES items(id),
    source_lines           VARCHAR(50),
    created_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at             TIMESTAMP
);

CREATE TABLE IF NOT EXISTS item_sprints (
    item_id        INTEGER NOT NULL REFERENCES items(id),
    sprint_id      INTEGER NOT NULL REFERENCES sprints(id),
    assigned_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at     TIMESTAMP,
    PRIMARY KEY (item_id, sprint_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ulid               VARCHAR(26) NOT NULL UNIQUE,
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    kind               VARCHAR(30) NOT NULL,    -- adr|sprint_amendment|inline_plan|open_question
    ref_code           VARCHAR(50),
    title              VARCHAR(500) NOT NULL,
    context            TEXT,
    decision           TEXT,
    consequences       TEXT,
    alternatives       TEXT,
    status             VARCHAR(20) NOT NULL DEFAULT 'proposed',
    superseded_by      INTEGER REFERENCES decisions(id),
    source_file        VARCHAR(1000),
    source_lines       VARCHAR(50),
    plan_id            INTEGER REFERENCES plans(id),
    plan_section_id    INTEGER REFERENCES plan_sections(id),
    sprint_id          INTEGER REFERENCES sprints(id),
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at         TIMESTAMP
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

CREATE UNIQUE INDEX IF NOT EXISTS ix_plans_project_slug_active
    ON plans(project_id, slug) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_plan_sections_plan
    ON plan_sections(plan_id, sort_order);

CREATE INDEX IF NOT EXISTS ix_plan_sections_parent
    ON plan_sections(parent_section_id);

CREATE INDEX IF NOT EXISTS ix_plan_sections_uncertain
    ON plan_sections(plan_id)
    WHERE needs_llm_review = TRUE AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_plan_checkpoints_section_status
    ON plan_checkpoints(plan_section_id, status, deleted_at);

CREATE UNIQUE INDEX IF NOT EXISTS ix_sprints_project_code_active
    ON sprints(project_id, code)
    WHERE code IS NOT NULL AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_sprints_project_status
    ON sprints(project_id, status, deleted_at);

CREATE INDEX IF NOT EXISTS ix_item_sprints_sprint_active
    ON item_sprints(sprint_id) WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ix_decisions_project_refcode_active
    ON decisions(project_id, ref_code)
    WHERE ref_code IS NOT NULL AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_decisions_project_kind_status
    ON decisions(project_id, kind, status, deleted_at);

-- ── Legacy sprint migration (idempotent; seeds 'legacy' sprint per project) ───
-- Python equivalent lives in dct/db.py::_migrate_legacy_sprint_flag; the
-- SQL below is informational — actual run-time uses the Python code so
-- detection (existing legacy sprint check) and bool literal dispatch
-- (PostgreSQL TRUE vs SQLite 1) both work correctly.

-- For each project with flagged items but no 'legacy' sprint yet, create
-- the sprint row. Then link every flagged item to it. Already-linked items
-- are skipped.
--
-- See _migrate_legacy_sprint_flag() for the authoritative implementation.
