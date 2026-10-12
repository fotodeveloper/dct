-- Migration 013: the project's commit-trailer preferences (#877) — whether
-- /commit adds a `Co-Authored-By` trailer and a session / artifact link.
-- Asked ONCE at the project's first configuration, stored with
-- `dct project trailers <slug> --co-authored-by on|off --session-link on|off`
-- (core/projects.py: set_commit_trailers), shown by list_projects and
-- applied by /commit without another question; a project's own rules that
-- forbid a trailer still win over a stored "yes".
--
-- AUTHORITATIVE SCHEMA: dct/db.py SQLAlchemy metadata.
-- This file is a REFERENCE rendering of the DDL for manual inspection;
-- the runtime applies it via _apply_schema_updates() (COMMIT_TRAILER_COLUMNS
-- through _add_column_if_missing: duplicate-safe, lock_timeout on
-- PostgreSQL; bootstraps 2-3 issue no DDL).
-- Keep in sync with db.py when columns/indexes change.
--
-- Related: #877

-- Tri-state, nullable, no default and NO backfill: NULL = not answered yet,
-- false = a stored "no", true = a stored "yes". A project registered before
-- the question existed has not answered it — no answer is invented.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS commit_co_authored_by BOOLEAN;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS commit_session_link BOOLEAN;
