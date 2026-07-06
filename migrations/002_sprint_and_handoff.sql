-- dct schema v2 — sprint flag on items + session_handoffs ledger (PostgreSQL dialect)
-- Tables are created programmatically via dct init / SQLAlchemy create_all().
-- This file exists for documentation and manual PostgreSQL setup.

-- Sprint flag — "is this item in the current sprint?"
-- Auto-cleared when item transitions to a terminal status (resolved/wont_fix).
ALTER TABLE items ADD COLUMN IF NOT EXISTS in_current_sprint BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index — most rows will be false, so only the "in sprint" subset is indexed.
CREATE INDEX IF NOT EXISTS ix_items_sprint
    ON items(project_id)
    WHERE in_current_sprint = TRUE AND deleted_at IS NULL;

-- Session handoff ledger — drafts left by one session for the next.
-- Multiple unconsumed rows per project are fine (parallel sessions, different scopes).
-- Consumer marks consumed_at (history preserved) or deleted_at (discarded).
CREATE TABLE IF NOT EXISTS session_handoffs (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    prompt TEXT NOT NULL,
    context_summary TEXT,
    scope VARCHAR(100),
    created_by VARCHAR(200) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    consumed_at TIMESTAMPTZ,
    consumed_by VARCHAR(200),
    deleted_at TIMESTAMPTZ
);

-- Most reads look for unconsumed handoffs per project, newest first.
CREATE INDEX IF NOT EXISTS ix_handoffs_unconsumed
    ON session_handoffs(project_id, created_at DESC)
    WHERE consumed_at IS NULL AND deleted_at IS NULL;
