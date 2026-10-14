"""SQLAlchemy Core database layer for dct.

Defines all tables, engine creation, and connection helpers.
Uses Core (not ORM) for portability between PostgreSQL and SQLite.
"""

import contextlib
import json
import re
import sys
import time
from collections import Counter
from datetime import UTC
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event

from dct.core.provenance import get_provenance

metadata = sa.MetaData()

# Names of the creation-provenance columns added to every mutating ledger
# table (CP #1236). Single source of truth for the table defs, the
# _apply_schema_updates migration, and tests.
PROVENANCE_COLUMNS = ("actor_type", "actor_client", "session_id", "source")
PROVENANCE_TABLES = (
    "items",
    "item_notes",
    "item_checkpoints",
    "changelog_entries",
    "session_handoffs",
    "sprints",
    "decisions",
    "plans",
    "tags",
    "releases",
    "triage_cycles",
    "findings",
    "finding_options",
    "finding_reviewers",
)


def _provenance_columns() -> list:
    """Fresh Column objects per table (Columns cannot be shared). The python
    defaults read the entry-point ContextVar, so every Core INSERT records
    who created the row with zero call-site changes. Nullable by design —
    honest absence beats guessed attribution."""
    return [
        sa.Column("actor_type", sa.String(16),
                  default=lambda: get_provenance().actor_type),
        sa.Column("actor_client", sa.String(64),
                  default=lambda: get_provenance().actor_client),
        sa.Column("session_id", sa.String(128),
                  default=lambda: get_provenance().session_id),
        sa.Column("source", sa.String(16),
                  default=lambda: get_provenance().source),
    ]

projects = sa.Table(
    "projects",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("slug", sa.String(50), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("path", sa.String(500)),
    sa.Column("description", sa.Text),
    sa.Column("current_version", sa.String(50)),
    # #222: per-project opt-in for the dct changelog hook (default TRUE; the
    # migration backfills existing projects from their actual dct changelog
    # history). Opt out with `dct project changelog <slug> off`.
    sa.Column("manages_changelog", sa.Boolean, nullable=False, server_default=sa.true()),
    # #225: changelog file location relative to the project root (default repo
    # root). A project keeping its changelog in a subdir points it there and
    # /release resolves {path}/{changelog_path}. Constrained to the project root
    # by set_changelog_path (leading '/' stripped, '..' escape rejected).
    sa.Column("changelog_path", sa.String(500), nullable=False, server_default="changelog.md"),
    # #876 K-14: the explicit-choice marker of changelog_path. True = the
    # owner chose the path (set_changelog_path — also exactly 'changelog.md');
    # False = detect (core.changelog_file.resolve_changelog finds the tracked
    # changelog). The value of changelog_path alone cannot say which.
    sa.Column("changelog_path_explicit", sa.Boolean, nullable=False,
              server_default=sa.false()),
    # K-73: the project's version-file contract — JSON array of repo-relative
    # paths the release's version sync must change (NULL = not learned yet,
    # [] = learned: none). Set only with explicit confirmation
    # (core/releases.set_version_files); every replaced contract is appended
    # to version_files_history (JSON array of {version_files, confirmed_at,
    # replaced_at}) — the ledger of earlier contracts.
    sa.Column("version_files", sa.Text),
    sa.Column("version_files_confirmed_at", sa.DateTime),
    sa.Column("version_files_history", sa.Text),
    # K-85: who performs a release's effects — 'dct' (the effects layer,
    # default) or 'own' (the project's own release tool). Copied onto each
    # release record at stamp time (releases.executor); changing it never
    # takes over an unfinished release. `dct project release-executor`.
    sa.Column("release_executor", sa.String(8), nullable=False, server_default="dct"),
    # K-90: render `(#N)` PR references in the changelog (opt-in, default
    # off); persisted per release in releases.render at stamp time.
    sa.Column("changelog_pr_refs", sa.Boolean, nullable=False, server_default=sa.false()),
    # K-76: the project's PENDING version-file proposal — canonical JSON of
    # core.releases.validate_proposal, written by `dct release prepare`
    # (set_pending_proposal; a new prepare replaces it), consumed and cleared
    # by the stamp that approves it (release_entries consume_proposal) — it
    # then lives on as releases.proposal. NULL = nothing prepared.
    sa.Column("release_proposal", sa.Text),
    # The PREVIEW slot of `dct release prepare --preview` — canonical JSON of
    # core.releases.validate_preview: the consent views of the variants the
    # one-dialog release offered, their proposal contents and the work /
    # progress state (set_preview; a preview and a prepare replace each
    # other's slot; --discard-preview clears it). NULL = no preview.
    sa.Column("release_preview", sa.Text),
    # Commit-trailer preferences, asked ONCE at the project's first
    # configuration and applied by /commit without another question
    # (core.projects.set_commit_trailers; `dct project trailers`). Each is a
    # tri-state: NULL = not answered yet, false = a stored "no", true = a
    # stored "yes". No server default and no backfill: a project registered
    # before the question existed has not answered it.
    sa.Column("commit_co_authored_by", sa.Boolean),
    sa.Column("commit_session_link", sa.Boolean),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
)

items = sa.Table(
    "items",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("item_type", sa.String(20), nullable=False),
    sa.Column("title", sa.String(500), nullable=False),
    # #817: 2-4 word label shown next to the item number wherever an item is
    # listed or referenced. Nullable — rows that reference an item fall back
    # to a compacted title (dct.core.text.item_label).
    sa.Column("short_title", sa.String(50)),
    sa.Column("description", sa.Text),
    sa.Column("priority", sa.String(10), nullable=False, server_default="P2"),
    sa.Column("status", sa.String(20), nullable=False, server_default="open"),
    sa.Column("component", sa.String(100)),
    sa.Column("source_file", sa.String(500)),
    sa.Column("source_lines", sa.String(100)),
    # DEPRECATED — frozen, DROP at 2.0.0. Tags live in tags / item_tags since
    # #611; _backfill_item_tags is the ONLY sanctioned reader of this column —
    # and its ONLY writer, in exactly one case (K-22): after reporting an item
    # whose JSON yields no usable name, it blanks that item's value to '[]' so
    # the un-migratable row stops being re-selected on every bootstrap.
    sa.Column("tags", sa.Text, server_default="[]"),
    sa.Column("resolution", sa.Text),
    # NB: sprint membership is NOT a column. `in_current_sprint` is derived at
    # read time from item_sprints + the project's active sprint (single source
    # of truth, #216). The legacy boolean column is dropped by the migration in
    # _apply_schema_updates.
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("resolved_at", sa.DateTime),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

item_notes = sa.Table(
    "item_notes",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("note", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

item_checkpoints = sa.Table(
    "item_checkpoints",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
    sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    # `kind` classifies the checkpoint as a non-gate ('task' — the default —
    # or 'cc-task', the mirrored client todo) or a gate (every other kind).
    # Pending gates block resolve_item. Open string column (not ENUM) so users
    # can define custom gate kinds: core (checkpoints.add_checkpoint) refuses
    # only a blank kind and one longer than this column (ensure_fits) — no
    # whitelist — and unknown kinds are treated as gates (conservative).
    sa.Column("kind", sa.String(32), nullable=False, server_default="task"),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

changelog_entries = sa.Table(
    "changelog_entries",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("version", sa.String(50)),
    sa.Column("category", sa.String(20), nullable=False),
    sa.Column("entry", sa.Text, nullable=False),
    sa.Column("component", sa.String(100)),
    sa.Column("source_file", sa.String(500)),
    sa.Column("source_lines", sa.String(100)),
    sa.Column("related_item_id", sa.Integer, sa.ForeignKey("items.id")),
    # #801: the branch / worktree the entry was written on, so a release can
    # tell entries of other branches apart. `branch_source` records who
    # supplied them: 'client' (the caller passed them, or the CLI detected
    # them in the caller's directory) or 'server' (the MCP server detected
    # them in its own cwd). All nullable — NULL = unknown (not a repo,
    # detached HEAD for `branch`, or a pre-#801 row).
    sa.Column("branch", sa.String(255)),
    sa.Column("worktree", sa.String(500)),
    sa.Column("branch_source", sa.String(16)),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("released_at", sa.DateTime),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

session_handoffs = sa.Table(
    "session_handoffs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("prompt", sa.Text, nullable=False),
    sa.Column("context_summary", sa.Text),
    sa.Column("scope", sa.String(100)),
    sa.Column("created_by", sa.String(200), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("consumed_at", sa.DateTime),
    sa.Column("consumed_by", sa.String(200)),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

# ── v0.2.0 schema: plans / sprints / decisions ────────────────────────────────

sprints = sa.Table(
    "sprints",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("code", sa.String(50)),  # short code: "0.5", "M0", "sprint-v0.1-ship"
    sa.Column("kind", sa.String(20), nullable=False, server_default="sprint"),  # sprint|phase|milestone|block
    sa.Column("status", sa.String(20), nullable=False, server_default="planned"),  # planned|active|deferred|completed|abandoned
    sa.Column("goal", sa.Text),
    sa.Column("started_at", sa.DateTime),
    sa.Column("ended_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

plans = sa.Table(
    "plans",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("slug", sa.String(200), nullable=False),
    sa.Column("title", sa.String(500), nullable=False),
    sa.Column("kind", sa.String(20), nullable=False),  # plan|spec|roadmap|adr|retro|backlog
    sa.Column("source_file", sa.String(1000)),        # NULL for DB-only roadmap
    sa.Column("source_hash", sa.String(64)),          # sha256 of file at last scan
    sa.Column("status", sa.String(20), nullable=False, server_default="active"),  # draft|active|completed|archived
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id")),
    sa.Column("imported_at", sa.DateTime),            # NULL = never imported (first-import gate)
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("archived_at", sa.DateTime),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

plan_sections = sa.Table(
    "plan_sections",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id"), nullable=False),
    sa.Column("parent_section_id", sa.Integer, sa.ForeignKey("plan_sections.id")),
    sa.Column("heading", sa.Text, nullable=False),
    sa.Column("depth", sa.Integer, nullable=False),  # 1=H1, 2=H2, ..., 6=H6
    sa.Column("slug", sa.String(200)),
    sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    sa.Column("section_type", sa.String(30), nullable=False, server_default="generic"),
    sa.Column("section_type_confidence", sa.String(10), server_default="high"),  # high|low|uncertain
    sa.Column("needs_llm_review", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("source_lines", sa.String(50)),
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id")),
    sa.Column("converted_to_item_id", sa.Integer, sa.ForeignKey("items.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
)

plan_checkpoints = sa.Table(
    "plan_checkpoints",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("plan_section_id", sa.Integer, sa.ForeignKey("plan_sections.id"), nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("status", sa.String(10), nullable=False, server_default="pending"),  # pending|done|rejected
    sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    sa.Column("converted_to_item_id", sa.Integer, sa.ForeignKey("items.id")),
    sa.Column("source_lines", sa.String(50)),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
)

item_sprints = sa.Table(
    "item_sprints",
    metadata,
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), primary_key=True),
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id"), primary_key=True),
    sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("removed_at", sa.DateTime),
)

# ── v1.5 schema: tags ─────────────────────────────────────────────────────────

tags = sa.Table(
    "tags",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    # GLOBAL / user-scoped: deliberately NO project_id (owner decision, #611) —
    # one name means the same thing in every project.
    sa.Column("name", sa.Text, nullable=False),  # no DB length limit: the
    # 50-char rule is an API rule for NEW names only, so legacy names migrated
    # from the frozen items.tags column always fit (K-7).
    sa.Column("description", sa.Text),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

item_tags = sa.Table(  # mirror of item_sprints: composite PK, soft-removed links
    "item_tags",
    metadata,
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), primary_key=True),
    sa.Column("tag_id", sa.Integer, sa.ForeignKey("tags.id"), primary_key=True),
    sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("removed_at", sa.DateTime),
)

decisions = sa.Table(
    "decisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ulid", sa.String(26), nullable=False, unique=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("kind", sa.String(30), nullable=False),  # adr|sprint_amendment|inline_plan|open_question
    sa.Column("ref_code", sa.String(50)),
    sa.Column("title", sa.String(500), nullable=False),
    sa.Column("context", sa.Text),
    sa.Column("decision", sa.Text),
    sa.Column("consequences", sa.Text),
    sa.Column("alternatives", sa.Text),
    sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),  # proposed|accepted|superseded|rejected
    sa.Column("superseded_by", sa.Integer, sa.ForeignKey("decisions.id")),
    sa.Column("source_file", sa.String(1000)),
    sa.Column("source_lines", sa.String(50)),
    sa.Column("plan_id", sa.Integer, sa.ForeignKey("plans.id")),
    sa.Column("plan_section_id", sa.Integer, sa.ForeignKey("plan_sections.id")),
    sa.Column("sprint_id", sa.Integer, sa.ForeignKey("sprints.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)


# ── K-41: durable release records ─────────────────────────────────────────────
# One row per release of a project version, written in the SAME transaction as
# the changelog stamp (core/changelog.release_entries) and advanced
# forward-only by the release workflow (core/releases.advance_release):
# stamped → files_written → committed → tagged → completed, or ended by an
# explicit abandon_release (status 'abandoned', K-60) — the state machine lives
# in core/releases.TRANSITIONS. `entry_ids` is the JSON array of the release's
# exact entry set ([] for a version-only release). K-61: `candidate_sha` is
# immutable once written; `release_commit_sha` is set once. `manifest` (JSON
# object: the expected artifacts and their content hashes) and `proof` (JSON
# object: entry id → proving commits) are write-once per key.
# K-94 B1 / K-85 / K-97 / K-76 / K-90: `release_mode` (git | without_git, the
# verified mode), `executor` (dct | own, copied from the project at stamp
# time), `consent` / `proposal` / `render` (JSON objects) — immutable once
# written; field contracts in core/releases.py and docs/release-workflow.md.
releases = sa.Table(
    "releases",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("version", sa.String(50), nullable=False),  # normalized (core/version.py)
    sa.Column("mode", sa.String(16), nullable=False),  # all|selected|version_only
    sa.Column("entry_ids", sa.Text, nullable=False),  # JSON array of ints
    sa.Column("candidate_sha", sa.String(64)),
    sa.Column("checkout_root", sa.String(500)),  # normalize_worktree
    sa.Column("release_commit_sha", sa.String(64)),
    sa.Column("manifest", sa.Text),  # JSON object, NULL = no effect recorded yet
    sa.Column("proof", sa.Text),  # JSON {entry id: {commits, merge_commit, pr[, source, ambiguous, associations]}}
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("completed_at", sa.DateTime),
    sa.Column("abandoned_at", sa.DateTime),
    sa.Column("abandon_reason", sa.Text),
    sa.Column("release_mode", sa.String(16)),  # git | without_git; NULL = unverified (legacy)
    sa.Column("executor", sa.String(8)),  # dct | own; NULL = recorded before the field
    sa.Column("consent", sa.Text),  # JSON object: what the owner approved
    sa.Column("proposal", sa.Text),  # JSON object: the approved version-file proposal
    sa.Column("render", sa.Text),  # JSON object: changelog render settings
    # K-115 G4: the VERIFIED tag identity {name, object, commit, annotated,
    # recorded_at} and push {target, tag_commit, branch_tip, recorded_at} —
    # write-once; NULL = not proven (older record / no tag or push yet).
    sa.Column("tag_proof", sa.Text),
    sa.Column("push_proof", sa.Text),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)


# ── #909: triage findings ledger ──────────────────────────────────────────────
# A triage cycle (one review round of a project) owns findings; a finding
# carries the rubric v2 fields under FULL column names (the export maps them
# back to the template's short keys S / O / E / K / type), the coordinator's
# class (class_rec derived by the workflow's classify(), class_override with
# its reason), the owner's decision VERBATIM, the suggestions of an agent
# (proposals, never decisions), an optional promotion to an item and the
# recurrence link by normalised fingerprint. Value domains (rubric enums,
# decisions, classes) are validated in dct/workflows/triage.py, not by CHECK
# constraints — the house pattern of every status / kind column here.
triage_cycles = sa.Table(
    "triage_cycles",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
    sa.Column("code", sa.String(100), nullable=False),  # e.g. "rev-877-r2"; unique live per project
    sa.Column("topic", sa.String(200), nullable=False),
    sa.Column("scope", sa.Text),  # what was reviewed: diff range, files
    sa.Column("base_commit", sa.String(64)),
    sa.Column("rubric_version", sa.SmallInteger, nullable=False, server_default=sa.text("2")),
    sa.Column("artifact_url", sa.String(500)),
    sa.Column("created_by", sa.String(200), nullable=False),
    sa.Column("opened_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("closed_at", sa.DateTime),  # NULL = open
    sa.Column("closed_source", sa.String(200)),  # CLOSED.source: where the owner's answers are recorded
    sa.Column("closed_notes", sa.Text),  # CLOSED.notes joined by blank lines
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

findings = sa.Table(
    "findings",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("cycle_id", sa.Integer, sa.ForeignKey("triage_cycles.id"), nullable=False),
    sa.Column("seq", sa.Integer, nullable=False),  # the K-n number inside the cycle
    sa.Column("fingerprint", sa.String(500), nullable=False),
    # Lowercase, whitespace collapsed, trailing punctuation stripped — the
    # identity behind first_seen_id (a cache of the comparison, never the PK).
    sa.Column("fingerprint_norm", sa.String(500), nullable=False),
    sa.Column("title", sa.String(500), nullable=False),
    # Rubric v2 fields (full names, owner 2026-10-13).
    sa.Column("impact", sa.SmallInteger, nullable=False),  # S: 0-3
    sa.Column("reachability", sa.String(1), nullable=False),  # O: '0'-'3' | '?'
    sa.Column("evidence", sa.SmallInteger, nullable=False),  # E: 0-3
    sa.Column("fix_cost", sa.String(1), nullable=False),  # K: S | M | L
    # Required on every finding (rubric §1, K-4). An existing PostgreSQL
    # table created before the constraint is healed by
    # _heal_triage_not_null; an existing SQLite one keeps the column
    # nullable and relies on the workflow's write-time validation.
    sa.Column("origin", sa.String(8), nullable=False),  # diff | pre | latent
    sa.Column("finding_type", sa.String(16), nullable=False),  # security | spec | spec-gap | ...
    # The template's SUGGESTED slot — an agent's proposal, never a decision.
    sa.Column("suggested_decision", sa.String(16)),
    sa.Column("suggested_opt", sa.String(4)),
    sa.Column("suggested_note", sa.Text),
    sa.Column("suggested_by", sa.String(100)),
    sa.Column("suggested_at", sa.DateTime),
    # Template flags: conflict / violation:false / dispute / openDesign.
    sa.Column("conflict", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("violation", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("dispute", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("open_design", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("area", sa.String(500)),  # where: file:line list
    # The reviewer that reported it ("codex", "panel:security", "analiza") —
    # the CARD's `src`. Not named `source`: that name is the provenance
    # column every ledger table carries (_provenance_columns).
    sa.Column("reviewer", sa.String(100), nullable=False),
    sa.Column("context", sa.Text),  # ctx paragraphs, "\n\n"-joined
    sa.Column("class_rec", sa.String(16), nullable=False),  # classify(): fix|verify|quick|decide|backlog|reject|note
    sa.Column("class_override", sa.String(16)),
    sa.Column("class_reason", sa.Text),  # required (in code) when class_override is set
    sa.Column("decision", sa.String(16)),  # fix|verify|backlog|reject|note|discuss; NULL = undecided
    sa.Column("decision_opt", sa.String(4)),  # option key
    sa.Column("decision_note", sa.Text),  # the owner's comment VERBATIM
    sa.Column("decided_at", sa.DateTime),
    # Origin of the decision (K-5), NULL while undecided and written together
    # with decided_at: decided_by = the person who decided (the owner),
    # decision_source = where the owner's words are recorded (e.g. "owner
    # paste 2026-10-13"). An audit record, not authentication: an agent can
    # still write a false source.
    sa.Column("decided_by", sa.String(200)),
    sa.Column("decision_source", sa.String(200)),
    # Provenance of the decision's WRITE (K-23): the client that recorded
    # the last decision — the last decision's writer, per Q1; audit, not
    # authentication. Written by decide_findings from get_provenance() in the
    # same UPDATE as decided_at; every overwrite refreshes all four (an
    # unknown field becomes NULL, never the previous client's value).
    # NULL while undecided; never backfilled. The creation provenance block
    # below (actor_type / actor_client / session_id / source) keeps naming
    # the client that RECORDED the finding. decision_actor_source is the
    # transport ('cli' | 'mcp'), not decision_source.
    sa.Column("decision_actor_type", sa.String(16)),
    sa.Column("decision_actor_client", sa.String(64)),
    sa.Column("decision_session_id", sa.String(128)),
    sa.Column("decision_actor_source", sa.String(16)),
    # #930 K-40: the "Punkt wyjścia: …" line of the pasted decision text that
    # produced this decision (the form was seeded from an agent's proposals),
    # verbatim; written with the decision stamp by decide_findings_from_text,
    # reset to NULL by every other decision write. It proves the form was
    # prefilled, not who wrote each note; never folded into decision_note or
    # decision_source. NULL while undecided; never backfilled.
    sa.Column("decision_text_origin", sa.Text),
    sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id")),  # promoted to this item
    # Earliest live finding of the same project with an equal
    # fingerprint_norm, set at record time; NULL = this is the first.
    sa.Column("first_seen_id", sa.Integer, sa.ForeignKey("findings.id")),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

finding_options = sa.Table(
    "finding_options",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("finding_id", sa.Integer, sa.ForeignKey("findings.id"), nullable=False),
    sa.Column("key", sa.String(4), nullable=False),  # "a", "b", ... unique live per finding
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("why", sa.Text),
    sa.Column("is_root", sa.Boolean, nullable=False, server_default=sa.false()),
    # At most one recommended option per finding — checked in code.
    sa.Column("is_recommended", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)

# #930 §2a: who reported a finding, as canonical reviewer tokens ("codex",
# "panel:security") next to the free-text display value findings.reviewer
# (the CARD's src). A finding may have several reviewers (one row each); a
# finding recorded without tokens has none (reviewer_precision counts it as
# "(unattributed)" — never a token guessed from src). The token shape
# ^[a-z0-9][a-z0-9:._-]*$ (lowercased on write) is validated in
# dct/workflows/triage.py; uniqueness per finding among LIVE rows below.
finding_reviewers = sa.Table(
    "finding_reviewers",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("finding_id", sa.Integer, sa.ForeignKey("findings.id"), nullable=False),
    sa.Column("reviewer", sa.String(50), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    sa.Column("deleted_at", sa.DateTime),
    *_provenance_columns(),
)


sa.Index(
    "ix_projects_slug_active",
    projects.c.slug,
    unique=True,
    sqlite_where=projects.c.deleted_at.is_(None),
    postgresql_where=projects.c.deleted_at.is_(None),
)

sa.Index("ix_items_project_status_deleted", items.c.project_id, items.c.status, items.c.deleted_at)
# (ix_items_sprint removed with the in_current_sprint column — membership now
#  lives in item_sprints; the current-sprint EXISTS lookup is served by the
#  item_sprints PK on item_id + ix_sprints_project_status.)
sa.Index("ix_changelog_project_version_deleted", changelog_entries.c.project_id, changelog_entries.c.version, changelog_entries.c.deleted_at)
sa.Index("ix_changelog_related_item", changelog_entries.c.related_item_id)
sa.Index("ix_item_notes_item_deleted", item_notes.c.item_id, item_notes.c.deleted_at)
sa.Index("ix_checkpoints_item_status_deleted", item_checkpoints.c.item_id, item_checkpoints.c.status, item_checkpoints.c.deleted_at)
# Partial index: fast lookup of pending gates per item (hot path in resolve_item).
# Condition must match the predicate in resolve_item exactly for planner to use it.
sa.Index(
    "ix_checkpoints_pending_gate",
    item_checkpoints.c.item_id,
    sqlite_where=sa.and_(
        item_checkpoints.c.kind != "task",
        item_checkpoints.c.status == "pending",
        item_checkpoints.c.deleted_at.is_(None),
    ),
    postgresql_where=sa.and_(
        item_checkpoints.c.kind != "task",
        item_checkpoints.c.status == "pending",
        item_checkpoints.c.deleted_at.is_(None),
    ),
)
sa.Index(
    "ix_handoffs_unconsumed",
    session_handoffs.c.project_id,
    session_handoffs.c.created_at.desc(),
    sqlite_where=sa.and_(session_handoffs.c.consumed_at.is_(None), session_handoffs.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(session_handoffs.c.consumed_at.is_(None), session_handoffs.c.deleted_at.is_(None)),
)

# v0.2.0 indexes
sa.Index(
    "ix_plans_project_slug_active",
    plans.c.project_id, plans.c.slug,
    unique=True,
    sqlite_where=plans.c.deleted_at.is_(None),
    postgresql_where=plans.c.deleted_at.is_(None),
)
sa.Index("ix_plan_sections_plan", plan_sections.c.plan_id, plan_sections.c.sort_order)
sa.Index("ix_plan_sections_parent", plan_sections.c.parent_section_id)
sa.Index(
    "ix_plan_sections_uncertain",
    plan_sections.c.plan_id,
    sqlite_where=sa.and_(plan_sections.c.needs_llm_review.is_(True), plan_sections.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(plan_sections.c.needs_llm_review.is_(True), plan_sections.c.deleted_at.is_(None)),
)
sa.Index("ix_plan_checkpoints_section_status", plan_checkpoints.c.plan_section_id, plan_checkpoints.c.status, plan_checkpoints.c.deleted_at)
sa.Index(
    "ix_sprints_project_code_active",
    sprints.c.project_id, sprints.c.code,
    unique=True,
    sqlite_where=sa.and_(sprints.c.code.isnot(None), sprints.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(sprints.c.code.isnot(None), sprints.c.deleted_at.is_(None)),
)
sa.Index("ix_sprints_project_status", sprints.c.project_id, sprints.c.status, sprints.c.deleted_at)
sa.Index(
    "ix_item_sprints_sprint_active",
    item_sprints.c.sprint_id,
    sqlite_where=item_sprints.c.removed_at.is_(None),
    postgresql_where=item_sprints.c.removed_at.is_(None),
)
sa.Index(
    "ix_decisions_project_refcode_active",
    decisions.c.project_id, decisions.c.ref_code,
    unique=True,
    sqlite_where=sa.and_(decisions.c.ref_code.isnot(None), decisions.c.deleted_at.is_(None)),
    postgresql_where=sa.and_(decisions.c.ref_code.isnot(None), decisions.c.deleted_at.is_(None)),
)
sa.Index("ix_decisions_project_kind_status", decisions.c.project_id, decisions.c.kind, decisions.c.status, decisions.c.deleted_at)

# v1.5 tag indexes — the dictionary's only hard constraint (the similarity
# guard is advisory) plus the reverse lookup behind the list_items tag filter.
sa.Index(
    "ix_tags_name_active",
    tags.c.name,
    unique=True,
    sqlite_where=tags.c.deleted_at.is_(None),
    postgresql_where=tags.c.deleted_at.is_(None),
)
sa.Index(
    "ix_item_tags_tag_active",
    item_tags.c.tag_id,
    sqlite_where=item_tags.c.removed_at.is_(None),
    postgresql_where=item_tags.c.removed_at.is_(None),
)

# K-41: at most one LIVE release record per (project, version). Core also
# enforces it under the project write lock (comparing under the version
# contract, so 1.5 and 1.5.0 collide there too); this index is the last guard.
sa.Index(
    "ix_releases_project_version_live",
    releases.c.project_id,
    releases.c.version,
    unique=True,
    sqlite_where=releases.c.deleted_at.is_(None),
    postgresql_where=releases.c.deleted_at.is_(None),
)

# #909: the triage ledger's uniqueness among LIVE rows (a soft-deleted cycle,
# finding or option frees its code / seq / key) plus the recurrence lookup.
sa.Index(
    "ix_triage_cycles_project_code_live",
    triage_cycles.c.project_id,
    triage_cycles.c.code,
    unique=True,
    sqlite_where=triage_cycles.c.deleted_at.is_(None),
    postgresql_where=triage_cycles.c.deleted_at.is_(None),
)
sa.Index(
    "ix_findings_cycle_seq_live",
    findings.c.cycle_id,
    findings.c.seq,
    unique=True,
    sqlite_where=findings.c.deleted_at.is_(None),
    postgresql_where=findings.c.deleted_at.is_(None),
)
sa.Index("ix_findings_cycle", findings.c.cycle_id)
sa.Index("ix_findings_fingerprint_norm", findings.c.fingerprint_norm)
sa.Index(
    "ix_finding_options_finding_key_live",
    finding_options.c.finding_id,
    finding_options.c.key,
    unique=True,
    sqlite_where=finding_options.c.deleted_at.is_(None),
    postgresql_where=finding_options.c.deleted_at.is_(None),
)
# #930 §2a: one row per (finding, reviewer token) among LIVE rows; its leading
# column also serves the per-finding lookups (export, the reviewer filter).
sa.Index(
    "ix_finding_reviewers_finding_reviewer_live",
    finding_reviewers.c.finding_id,
    finding_reviewers.c.reviewer,
    unique=True,
    sqlite_where=finding_reviewers.c.deleted_at.is_(None),
    postgresql_where=finding_reviewers.c.deleted_at.is_(None),
)


def _set_sqlite_wal(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL may be refused (e.g. network filesystems) — plain journal is fine.
    with contextlib.suppress(Exception):
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory of a file-backed sqlite URL if missing.

    First run on a pristine machine: the default sqlite path lives under a
    directory that may not exist yet, and sqlite fails to open the database
    file with a raw OperationalError instead of creating it (#360). No-op for
    non-sqlite dialects and for memory URLs ('sqlite://', 'sqlite:///:memory:').
    """
    from dct.paths import sqlite_database_path

    database = sqlite_database_path(database_url)  # the ONE parser (K-99)
    if database is not None:
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def get_engine(database_url: str) -> sa.Engine:
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            # In-memory sqlite must share ONE connection (StaticPool): the
            # default per-thread pool hands every new thread its own empty
            # database, and the MCP SDK (2.0) runs sync tools in worker
            # threads — tools would see "no such table" (#429).
            kwargs["poolclass"] = sa.pool.StaticPool

    engine = sa.create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_wal)

    return engine


# ── K-84: the read-only engine path ──────────────────────────────────────────
# Read-only commands (release plan / status, clog candidates, the reconstruct
# preview) must not change the database in any way: get_engine() + the
# bootstrap would create the file and its directory, every table and index,
# run migrations and switch SQLite to WAL. They use get_readonly_engine() +
# check_schema_ready() instead and refuse a missing or older schema with a
# message naming the explicit upgrade.

UPGRADE_COMMAND = "dct db upgrade"


class SchemaNotReady(RuntimeError):
    """The database a read-only command opened is missing or has an older
    schema — the read-only path never creates or upgrades it."""


def _sqlite_readonly_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    # query_only: a second guard behind mode=ro. No journal_mode here — a
    # read-only connection never changes the database's journal mode.
    cursor.execute("PRAGMA query_only=ON")
    cursor.close()


def get_readonly_engine(database_url: str) -> sa.Engine:
    """An Engine that cannot change the database (K-84).

    - SQLite file: the file must exist (SchemaNotReady otherwise — nothing is
      created, not even the parent directory); it is opened through a URI
      with `mode=ro` (the path percent-encoded, so `?`/`#` in a name stay
      part of the path) plus `PRAGMA query_only`, and no connect hook sets
      the journal mode. On a database already in WAL mode SQLite itself may
      create its `-wal`/`-shm` sidecar files, as for any reader; the database
      file is never written.
    - SQLite in-memory: SchemaNotReady (an in-memory database has no schema).
    - PostgreSQL: every session starts with `default_transaction_read_only =
      on` (libpq `options`), so every transaction is READ ONLY — a write
      fails with "cannot execute ... in a read-only transaction".

    No DDL runs; pair it with check_schema_ready before reading.
    """
    from dct.paths import sqlite_database_path

    url = sa.engine.make_url(database_url)
    if url.get_backend_name() == "sqlite":
        # K-99: the path the NORMAL engine opens for this URL (one parser).
        database = sqlite_database_path(database_url)
        if database is None:
            raise SchemaNotReady(
                "an in-memory SQLite database has no schema — a read-only command "
                "needs an existing database file"
            )
        path = Path(database).expanduser().absolute()
        if not path.is_file():
            raise SchemaNotReady(
                f"the database {path} does not exist — this read-only command never "
                f"creates it; run `{UPGRADE_COMMAND}` to create the schema first"
            )
        from urllib.parse import quote

        target = f"file:{quote(str(path))}?mode=ro"

        def _connect():
            import sqlite3

            return sqlite3.connect(target, uri=True, check_same_thread=False)

        engine = sa.create_engine("sqlite://", creator=_connect, poolclass=sa.pool.NullPool)
        event.listen(engine, "connect", _sqlite_readonly_pragmas)
        return engine
    options = url.query.get("options")
    flag = "-c default_transaction_read_only=on"
    if isinstance(options, tuple):
        options = " ".join(options)
    url = url.update_query_dict({"options": f"{options} {flag}" if options else flag})
    return sa.create_engine(url)


def check_schema_ready(engine: sa.Engine) -> None:
    """Refuse (SchemaNotReady) unless every table of the current metadata
    exists with every column — the schema the read paths SELECT from.
    Indexes are not required for reading. Pure catalog reads (inspector)."""
    inspector = sa.inspect(engine)
    present = set(inspector.get_table_names())
    missing: list[str] = []
    for table in metadata.sorted_tables:
        if table.name not in present:
            missing.append(f"{table.name} (table)")
            continue
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        missing.extend(
            f"{table.name}.{column.name}" for column in table.columns
            if column.name not in columns
        )
    if missing:
        raise SchemaNotReady(
            f"the database schema is older than this dct (missing: "
            f"{', '.join(missing)}) — this read-only command never migrates it; "
            f"run `{UPGRADE_COMMAND}` to upgrade the schema first"
        )


class SerializationConflict(RuntimeError):
    """A project transaction lost a serialization race (SQLSTATE 40001) and
    was rolled back — nothing written; the caller may re-run it."""


def is_serialization_failure(exc: BaseException) -> bool:
    """The ONE detector of a lost serialization race (K-74) — True for:

    - the domain SerializationConflict (what project_transaction raises), and
    - a driver error carrying SQLSTATE 40001 (psycopg's SerializationFailure;
      `sqlstate` on psycopg 3, `pgcode` on psycopg2), bare or wrapped in a
      SQLAlchemy DBAPIError (`.orig`).

    Classified by type and SQLSTATE only, never by message text. Every
    caller that recovers from a serialization race asks this function."""
    if isinstance(exc, SerializationConflict):
        return True
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == "40001"


def lock_row(conn: sa.Connection, table: sa.Table, row_id: int) -> bool:
    """Serialise this transaction on row `row_id` of `table` and say whether
    the row exists and is not soft-deleted (#910, generalised by #909).

    For a guard that reads and then writes in one transaction against a
    writer that can change what the guard read: on PostgreSQL at READ
    COMMITTED nothing orders the two, so a write committed in between is
    closed over. Every participant takes this lock as the existence check
    of the row, BEFORE it reads what it guards, so both sides queue on that
    row. `table` must be a metadata table with an `id` primary key and a
    `deleted_at` column. Which rows an operation locks, and in which order,
    is the caller's contract (lock_item_row, lock_cycle_row,
    dct.workflows.triage).

    PostgreSQL: `SELECT … FOR NO KEY UPDATE` on the row (#909 K-21), with a
    transaction-local lock_timeout matching project_transaction's
    five-second wait (a timeout surfaces as the driver's OperationalError,
    SQLSTATE 55P03, after the rollback — nothing written). NO KEY UPDATE
    conflicts with itself, FOR UPDATE, FOR SHARE and every UPDATE / DELETE
    of the row, so the participants still exclude each other and a soft
    delete (an UPDATE of deleted_at) still waits for the holder. It does NOT
    conflict with FOR KEY SHARE, the lock PostgreSQL takes implicitly on a
    referenced row when a foreign key is checked (an INSERT of an item, a
    finding, an option or a checkpoint pointing at the locked row): such an
    insert by another transaction no longer waits for the holder, which is
    what lets a transaction holding a cycle row insert an item of a project
    another transaction holds locked without the two deadlocking. A writer
    that must exclude those inserts takes this lock itself. SQLite: the
    driver's legacy mode opens no transaction before a SELECT, so the helper
    issues `BEGIN IMMEDIATE` (reserving the whole database before reading,
    as project_transaction does) unless the connection is already in a
    transaction — one writer at a time, so no row-level order applies there.
    Call it before the first statement that reads the guarded state.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SET LOCAL lock_timeout = '5000ms'"))
        # key_share=True without read=True is FOR NO KEY UPDATE — not FOR
        # KEY SHARE (that is read=True, key_share=True).
        query = sa.select(table.c.id).with_for_update(key_share=True)
    else:
        if conn.dialect.name == "sqlite" and not conn.connection.dbapi_connection.in_transaction:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        query = sa.select(table.c.id)
    row = conn.execute(
        query.where(table.c.id == row_id, table.c.deleted_at.is_(None))
    ).first()
    return row is not None


def lock_item_row(conn: sa.Connection, item_id: int) -> bool:
    """lock_row on item `item_id` — the gate contract (#910).

    The gate guard of a terminal transition reads the pending gates and
    writes the status in one transaction; every such participant —
    update_item / resolve_item on one side, add_checkpoint and
    checkpoints.set_status on the other — takes this lock first, so a gate
    lands either before the guard reads or after the status is written.
    """
    return lock_row(conn, items, item_id)


def lock_cycle_row(conn: sa.Connection, cycle_id: int) -> bool:
    """lock_row on triage cycle `cycle_id` — the open-cycle contract (#909).

    Every writer that requires an OPEN cycle (record / decide / suggest
    findings), close_triage and promote_finding_to_item take this lock
    before they read the cycle's state, so a finding or a decision lands
    either before the close reads the cycle or is refused after it, never
    written into a closed cycle. It is the FIRST lock of decide / suggest /
    close / promote; record_findings takes the project row before it
    (first_seen is project-wide). The per-operation order is listed in the
    dct.workflows.triage module docstring.
    """
    return lock_row(conn, triage_cycles, cycle_id)


@contextlib.contextmanager
def project_transaction(engine: sa.Engine, project_slug: str, *, write: bool = False):
    """A consistent project snapshot, optionally serializing composed writers.

    SQLite explicitly begins before SELECT (the driver's legacy mode does
    not); writers reserve the database before reading. PostgreSQL uses
    REPEATABLE READ, acquiring a session advisory lock BEFORE the transaction
    for writers so a waiting session cannot retain a stale pre-lock snapshot.
    The lock identifies the project PRIMARY KEY, not its name or content.

    PostgreSQL serialization covers callers of this helper; unrelated CRUD
    writers still follow their own concurrency contracts. The database's
    global isolation policy is unchanged. Lock waits are bounded to five
    seconds, matching SQLite's default connection timeout.

    The lock protects only its participants: a writer that does not take it
    can still make a REPEATABLE READ transaction here fail with a
    serialization failure (SQLSTATE 40001). That is never retried
    automatically; it surfaces as SerializationConflict — a controlled
    refusal after the rollback (nothing written), safe to re-run.
    """
    # Local import: projects uses the table definitions in this module.
    from dct.core.projects import resolve_project_id

    with engine.connect() as conn:
        locked_project = None
        try:
            if conn.dialect.name == "postgresql":
                if write:
                    conn.execution_options(isolation_level="AUTOCOMMIT")
                    project_id = resolve_project_id(conn, project_slug)
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            acquired = conn.execute(sa.text(
                                "SELECT pg_try_advisory_lock(430, :project_id)"
                            ), {"project_id": project_id}).scalar_one()
                        except BaseException:
                            # An uncertain acquisition must never return a
                            # potentially locked physical session to the pool.
                            conn.invalidate()
                            raise
                        if acquired:
                            locked_project = project_id
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Timed out waiting for the DCT project transaction lock")
                        time.sleep(0.05)
                    conn.rollback()  # end SQLAlchemy's autocommit bookkeeping
                conn.execution_options(isolation_level="REPEATABLE READ")

            try:
                with conn.begin():
                    if conn.dialect.name == "sqlite":
                        conn.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
                    project_id = resolve_project_id(conn, project_slug)
                    yield conn, project_id
            except sa.exc.DBAPIError as exc:
                if not is_serialization_failure(exc):
                    raise
                raise SerializationConflict(
                    f"A concurrent transaction changed the data of project "
                    f"'{project_slug}' (serialization failure, SQLSTATE 40001) — "
                    f"rolled back, nothing was written; re-run the operation."
                ) from exc
        finally:
            if locked_project is not None:
                try:
                    conn.rollback()
                    conn.execution_options(isolation_level="AUTOCOMMIT")
                    conn.execute(sa.text(
                        "SELECT pg_advisory_unlock(430, :project_id)"
                    ), {"project_id": locked_project})
                    conn.rollback()
                except BaseException:
                    conn.invalidate()
                    raise


def create_tables(engine: sa.Engine) -> None:
    _create_missing_tables(engine)
    _apply_schema_updates(engine)


# Bounded attempts for the concurrent-creation race (K-59): each retry
# follows a conflict another bootstrap caused by creating the same object.
SCHEMA_CREATE_ATTEMPTS = 3

# PostgreSQL: duplicate_table (42P07 — tables AND indexes, "relation already
# exists") and duplicate_object (42710). A CREATE racing an uncommitted
# CREATE of the same name fails on the catalog's unique index instead
# (23505 on pg_type / pg_class name indexes) — only those count.
_PG_DUPLICATE_SQLSTATES = {"42P07", "42710"}
_PG_CATALOG_NAME_INDEXES = ("pg_type_typname_nsp_index", "pg_class_relname_nsp_index")
_SQLITE_DUPLICATE = re.compile(r"\b(table|index) \S+ already exists\b", re.IGNORECASE)


def _is_concurrent_creation_conflict(engine: sa.Engine, exc: sa.exc.DBAPIError) -> bool:
    """True only for "the object I am creating was just created by someone
    else" — never for any other error (those propagate unchanged)."""
    orig = getattr(exc, "orig", None)
    if engine.dialect.name == "postgresql":
        code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if code in _PG_DUPLICATE_SQLSTATES:
            return True
        return code == "23505" and any(name in str(orig) for name in _PG_CATALOG_NAME_INDEXES)
    return isinstance(exc, sa.exc.OperationalError) and bool(_SQLITE_DUPLICATE.search(str(orig)))


def _create_table_objects(engine: sa.Engine, table: sa.Table) -> None:
    """Create `table` if absent plus every metadata index of it that is absent,
    in ONE transaction (atomic on PostgreSQL; SQLite's driver commits DDL
    statement by statement). Re-reads the live schema first, so a retry only
    finishes what is still missing."""
    inspector = sa.inspect(engine)
    exists = inspector.has_table(table.name)
    present = {ix["name"] for ix in inspector.get_indexes(table.name)} if exists else set()
    missing = sorted((ix for ix in table.indexes if ix.name not in present), key=lambda ix: ix.name)
    if exists and not missing:
        return
    with engine.begin() as conn:
        if not exists:
            conn.execute(sa.schema.CreateTable(table))
        for index in missing:
            conn.execute(sa.schema.CreateIndex(index))


def _create_missing_tables(engine: sa.Engine) -> None:
    """Create every metadata table this database lacks, WITH its indexes —
    safe against a concurrent bootstrap creating the same objects (K-59).

    Scope: the tables missing at the first look. Their whole required schema
    (table + every index) is completed — a table that exists does not prove
    its unique index exists. Tables that predate this bootstrap are left to
    _apply_schema_updates' explicit blocks (no silent index retrofit on an
    old database) — except the release-ledger and triage-ledger tables
    (INDEX_HEALED_TABLES, K-93 A2 / #909), whose missing required indexes
    _heal_required_indexes
    completes on every bootstrap. A concurrent-creation conflict (duplicate table / index /
    relation, see _is_concurrent_creation_conflict) rolls the attempt back
    and re-checks the whole set; any other error propagates; at most
    SCHEMA_CREATE_ATTEMPTS attempts. No missing table → no DDL at all.
    """
    existing = set(sa.inspect(engine).get_table_names())
    pending = [table for table in metadata.sorted_tables if table.name not in existing]
    for attempt in range(1, SCHEMA_CREATE_ATTEMPTS + 1):
        if not pending:
            break
        try:
            for table in pending:  # FK order
                _create_table_objects(engine, table)
            break
        except sa.exc.DBAPIError as exc:
            if attempt == SCHEMA_CREATE_ATTEMPTS or not _is_concurrent_creation_conflict(engine, exc):
                raise
    _heal_required_indexes(engine, existing)


# K-93 A2: tables of the release-ledger feature — and, by the same reasoning,
# of the triage ledger (#909: its live-uniqueness indexes are the last guard
# of cycle code / finding seq / option key / #930 reviewer token) — whose REQUIRED indexes every
# bootstrap completes even when the table already exists — SQLite commits
# CREATE TABLE and CREATE INDEX separately, so a crash in between leaves the
# table without its unique guard. Tables of older features keep the "no
# silent retrofit" rule of _create_missing_tables.
INDEX_HEALED_TABLES = (
    "releases", "triage_cycles", "findings", "finding_options", "finding_reviewers",
)


def _heal_required_indexes(engine: sa.Engine, existed: set[str]) -> None:
    """Create the metadata indexes missing on an INDEX_HEALED_TABLES table
    that already existed before this bootstrap (one inspector read per table
    when nothing is missing — bootstraps 2-3 issue no DDL and print nothing).

    Race-safe like _create_missing_tables (a concurrent creation is retried,
    bounded). A unique index refused by the DATA (duplicate live rows — an
    IntegrityError that is not a concurrent creation) never bricks the
    bootstrap: one stderr warning names the index, the rows stay untouched
    for a manual fix, and the core keeps enforcing the rule under the
    project lock. Any other error propagates."""
    for table in metadata.sorted_tables:
        if table.name not in INDEX_HEALED_TABLES or table.name not in existed:
            continue
        present = {ix["name"] for ix in sa.inspect(engine).get_indexes(table.name)}
        missing = sorted(ix.name for ix in table.indexes if ix.name not in present)
        if not missing:
            continue
        for attempt in range(1, SCHEMA_CREATE_ATTEMPTS + 1):
            try:
                _create_table_objects(engine, table)
                break
            except sa.exc.DBAPIError as exc:
                if attempt < SCHEMA_CREATE_ATTEMPTS and _is_concurrent_creation_conflict(engine, exc):
                    continue
                if isinstance(exc, sa.exc.IntegrityError) and not _is_concurrent_creation_conflict(
                    engine, exc
                ):
                    print(
                        f"WARNING: dct could not create the required index(es) "
                        f"{', '.join(missing)} on {table.name}: the existing rows violate "
                        f"it ({exc.orig}). Nothing was changed — remove the duplicate "
                        f"live rows manually; the next bootstrap creates the index.",
                        file=sys.stderr,
                    )
                    break
                raise


def _column_exists(engine: sa.Engine, table: str, column: str) -> bool:
    """Fresh inspector read (never a cached one): does `table.column` exist?"""
    return column in {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _add_column_if_missing(
    engine: sa.Engine, table: str, column: str, ddl_type: str
) -> bool:
    """Add `table.column` of `ddl_type` unless it exists; duplicate-safe (K-25).

    Two processes bootstrapping at once (a hook and the MCP server) must not
    crash each other on the same ADD COLUMN:
    - PostgreSQL: `ADD COLUMN IF NOT EXISTS`, inside a transaction that first
      sets `lock_timeout` so a stuck lock fails fast instead of hanging every
      hook behind the ACCESS EXCLUSIVE lock the ALTER needs.
    - SQLite: inspector check, then ALTER; a "duplicate column" error is
      treated as success once a re-check confirms the column now exists (the
      other process won the race).

    `table`, `column` and `ddl_type` are code constants, never user input.
    Returns True when DDL was issued (on PostgreSQL: whenever the inspector saw
    the column missing), False when the column was already there.
    """
    if _column_exists(engine, table, column):
        return False
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(sa.text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            ))
        return True
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
    except sa.exc.OperationalError as exc:
        if "duplicate column" not in str(exc).lower() or not _column_exists(
            engine, table, column
        ):
            raise
    return True


# Columns _apply_schema_updates heals onto an existing database (K-60, K-61,
# K-73, K-85, K-90, K-94, K-76, K-97, K-115) — table → [(column, DDL type)].
# TIMESTAMP is portable (SQLite stores the value as SQLAlchemy's DateTime
# would). `{false}` is the dialect's BOOLEAN false literal (PostgreSQL is
# strict: FALSE; SQLite: 0) — keep it in sync with the column's
# server_default.
RELEASE_LEDGER_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "releases": (
        ("release_commit_sha", "VARCHAR(64)"),
        ("manifest", "TEXT"),
        ("proof", "TEXT"),
        ("abandoned_at", "TIMESTAMP"),
        ("abandon_reason", "TEXT"),
        ("release_mode", "VARCHAR(16)"),
        ("executor", "VARCHAR(8)"),
        ("consent", "TEXT"),
        ("proposal", "TEXT"),
        ("render", "TEXT"),
        ("tag_proof", "TEXT"),
        ("push_proof", "TEXT"),
    ),
    "projects": (
        ("version_files", "TEXT"),
        ("version_files_confirmed_at", "TIMESTAMP"),
        ("version_files_history", "TEXT"),
        ("release_executor", "VARCHAR(8) NOT NULL DEFAULT 'dct'"),
        ("changelog_pr_refs", "BOOLEAN NOT NULL DEFAULT {false}"),
        ("release_proposal", "TEXT"),
        ("release_preview", "TEXT"),
    ),
}


# #909 K-5: decision-origin columns, K-23: the decision writer's
# provenance, and #930 K-40: the decision text's prefill line, added to
# `findings` after the ledger first reached development databases. Nullable, no backfill (an undecided finding has none; a decision
# recorded before them stays unattributed — never a guessed author, never
# copied from the finding's creation provenance). Healed like
# RELEASE_LEDGER_COLUMNS.
TRIAGE_LEDGER_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "findings": (
        ("decided_by", "VARCHAR(200)"),
        ("decision_source", "VARCHAR(200)"),
        ("decision_actor_type", "VARCHAR(16)"),
        ("decision_actor_client", "VARCHAR(64)"),
        ("decision_session_id", "VARCHAR(128)"),
        ("decision_actor_source", "VARCHAR(16)"),
        # #930 K-40: the decision text's prefill line (see the column).
        ("decision_text_origin", "TEXT"),
    ),
}

# #909 K-4: columns that became NOT NULL after the ledger first reached
# development databases — table → columns. See _heal_triage_not_null.
TRIAGE_NOT_NULL_COLUMNS: dict[str, tuple[str, ...]] = {
    "findings": ("origin", "finding_type"),
}

# At most this many row ids are named in the NOT NULL heal's warning.
NOT_NULL_WARNING_IDS = 10


def _heal_triage_not_null(engine: sa.Engine) -> None:
    """#909 K-4 — make the TRIAGE_NOT_NULL_COLUMNS NOT NULL on an existing
    PostgreSQL table that was created while they were nullable.

    Guarded by a fresh inspector read: a column already NOT NULL costs one
    catalog query and no DDL (bootstraps 2-3 are silent no-ops). A value is
    NEVER guessed: when rows (live or soft-deleted — the constraint covers
    every row) hold NULL, nothing is changed and one stderr warning per
    column names the count and up to NOT_NULL_WARNING_IDS row ids, on every
    bootstrap until the rows are fixed manually (the _heal_required_indexes
    policy); the workflow's write-time validation keeps new rows complete
    meanwhile. A NULL row committed between the count and the ALTER makes
    the ALTER fail with an IntegrityError, reported the same way.

    SQLite cannot add NOT NULL to an existing column without rebuilding the
    table (under PRAGMA foreign_keys=ON, with finding_options referencing
    it), so there the column stays nullable — no DDL, no output — and the
    write-time validation is the guard. Such a table exists only on a
    database bootstrapped by an unreleased development build."""
    if engine.dialect.name != "postgresql":
        return
    tables_now = set(sa.inspect(engine).get_table_names())
    for table, columns in TRIAGE_NOT_NULL_COLUMNS.items():
        if table not in tables_now:
            continue
        nullable = [
            c["name"] for c in sa.inspect(engine).get_columns(table)
            if c["name"] in columns and c["nullable"]
        ]
        for column in nullable:
            with engine.connect() as conn:
                ids = [r[0] for r in conn.execute(sa.text(
                    f"SELECT id FROM {table} WHERE {column} IS NULL ORDER BY id"
                ))]
            if not ids:
                try:
                    with engine.begin() as conn:
                        conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
                        conn.execute(sa.text(
                            f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"
                        ))
                    continue
                except sa.exc.IntegrityError:
                    with engine.connect() as conn:
                        ids = [r[0] for r in conn.execute(sa.text(
                            f"SELECT id FROM {table} WHERE {column} IS NULL ORDER BY id"
                        ))]
            named = ", ".join(f"#{i}" for i in ids[:NOT_NULL_WARNING_IDS])
            more = (f" (+{len(ids) - NOT_NULL_WARNING_IDS} more)"
                    if len(ids) > NOT_NULL_WARNING_IDS else "")
            print(
                f"WARNING: dct could not make {table}.{column} NOT NULL: "
                f"{len(ids)} row(s) have no value ({named}{more}). Nothing was "
                f"changed — dct does not guess the value; set it on those rows "
                f"manually and the next bootstrap applies the constraint.",
                file=sys.stderr,
            )


# Commit-trailer preference → its projects column (core.projects reads and
# writes through this map); _apply_schema_updates heals the columns onto an
# existing database (nullable BOOLEAN, no backfill). Each is a tri-state:
# NULL = not answered yet, false = a stored "no", true = a stored "yes".
COMMIT_TRAILER_COLUMNS: dict[str, str] = {
    "co_authored_by": "commit_co_authored_by",
    "session_link": "commit_session_link",
}


def _heal_changelog_path_marker(engine: sa.Engine) -> None:
    """#876 K-22 — CONVERGENT, on every bootstrap: a stored changelog_path
    other than the default with the explicit marker unset is the owner's
    choice (the marker → true). It covers the rows the column was added to
    AND a path an OLDER dct (no marker) stores later on the same user-scoped
    database — the new code would otherwise silently detect instead. Only
    NON-default paths: the default with the marker false is detection —
    never chosen or a `--detect` reset — and is never re-marked; an explicit
    `changelog.md` already carries the marker. Guarded by a read, so a
    database in the right state is never written (bootstraps 2-3 no-ops);
    two concurrent runs set the same rows to the same value."""
    from dct.core.changelog_file import DEFAULT_CHANGELOG_PATH

    stale = sa.and_(projects.c.changelog_path != DEFAULT_CHANGELOG_PATH,
                    projects.c.changelog_path_explicit == sa.false())
    with engine.connect() as conn:
        if conn.execute(sa.select(projects.c.id).where(stale).limit(1)).first() is None:
            return
    with engine.begin() as conn:
        conn.execute(projects.update().where(stale).values(changelog_path_explicit=True))


def _ddl_type(engine: sa.Engine, ddl_type: str) -> str:
    """Fill the dialect placeholders of a RELEASE_LEDGER_COLUMNS DDL type."""
    false = "FALSE" if engine.dialect.name == "postgresql" else "0"
    return ddl_type.replace("{false}", false)


def _apply_schema_updates(engine: sa.Engine) -> None:
    """Idempotent schema updates for existing DBs (lightweight migration runner).

    Each schema change (column add, index add, ...) is checked and applied
    INDEPENDENTLY. We do NOT treat a logical group (e.g. "sprint migration")
    as atomic — a partial prior run could leave the column in place but the
    index missing, and we must heal each piece on the next call. This mirrors
    the invariant-per-field rule learned from the update_item resolved_at bug
    (core/items.py TERMINAL_STATUSES).

    Dialect note: PostgreSQL is strict about BOOLEAN literals — `0` (INTEGER)
    does NOT coerce to FALSE, the ALTER fails with DatatypeMismatch; same for
    `1` vs TRUE in index predicates. SQLite is permissive. We emit FALSE/TRUE
    for PostgreSQL and 0/1 for SQLite. Keep this in sync with the
    server_default on the SQLAlchemy column definition (which uses sa.false()
    to portably emit the correct literal at CREATE TABLE time).
    """
    inspector = sa.inspect(engine)
    if "items" not in inspector.get_table_names():
        return

    col_names = {c["name"] for c in inspector.get_columns("items")}

    # #817: items.short_title (nullable label, no backfill). Duplicate-safe
    # helper — two processes may bootstrap the same old database at once.
    if "short_title" not in col_names:
        _add_column_if_missing(engine, "items", "short_title", "VARCHAR(50)")

    # #801: changelog_entries.branch / worktree / branch_source (nullable, no
    # backfill — pre-#801 entries stay "unbranched"). Each column is healed
    # independently by the duplicate-safe helper.
    if "changelog_entries" in inspector.get_table_names():
        cl_cols = {c["name"] for c in inspector.get_columns("changelog_entries")}
        for column, ddl_type in (
            ("branch", "VARCHAR(255)"),
            ("worktree", "VARCHAR(500)"),
            ("branch_source", "VARCHAR(16)"),
        ):
            if column not in cl_cols:
                _add_column_if_missing(engine, "changelog_entries", column, ddl_type)

    # K-60/K-61: release-record columns added after the `releases` table
    # first shipped on development databases, and K-73: the project's
    # version-file contract, and #909 K-5 / K-23: the findings' decision-origin
    # and decision-writer columns. Nullable, no backfill (an older record simply has no release
    # commit / manifest / proof / decision author yet). Each column is healed
    # independently by the duplicate-safe helper; the inspector check keeps
    # bootstraps 2-3 free of DDL.
    tables_now = set(inspector.get_table_names())
    for table, additions in (*RELEASE_LEDGER_COLUMNS.items(), *TRIAGE_LEDGER_COLUMNS.items()):
        if table not in tables_now:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl_type in additions:
            if column not in present:
                _add_column_if_missing(engine, table, column, _ddl_type(engine, ddl_type))

    # #909 K-4: origin / finding_type NOT NULL on an existing findings table
    # (PostgreSQL; never a guessed value — see _heal_triage_not_null).
    _heal_triage_not_null(engine)

    # #222: opt-in changelog enforcement. Add projects.manages_changelog and,
    # on first add, backfill — a project KEEPS enforcement only if it already
    # has dct changelog history; one with none opts out (so projects registered
    # for item-tracking but keeping their own changelog aren't wrongly blocked).
    # New rows default TRUE via the column server_default; this block is a
    # one-shot (guarded by column absence) so re-runs never override a later
    # explicit `dct project changelog` toggle.
    proj_cols = {c["name"] for c in inspector.get_columns("projects")}
    if "manages_changelog" not in proj_cols:
        is_pg = engine.dialect.name == "postgresql"
        true_lit, false_lit = ("TRUE", "FALSE") if is_pg else ("1", "0")
        with engine.begin() as conn:
            conn.execute(sa.text(
                f"ALTER TABLE projects ADD COLUMN manages_changelog "
                f"BOOLEAN NOT NULL DEFAULT {true_lit}"
            ))
            conn.execute(sa.text(
                f"UPDATE projects SET manages_changelog = {false_lit} "
                "WHERE deleted_at IS NULL AND id NOT IN "
                "(SELECT DISTINCT project_id FROM changelog_entries "
                "WHERE deleted_at IS NULL AND project_id IS NOT NULL)"
            ))

    # #225: per-project changelog location. The ADD COLUMN DEFAULT backfills
    # existing rows to 'changelog.md' (repo root) — no conditional UPDATE needed.
    # Guarded by column absence so re-runs never clobber an explicit value set
    # later via `dct project changelog-path`. VARCHAR default is dialect-portable
    # (unlike the BOOLEAN literal dance above).
    if "changelog_path" not in proj_cols:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "ALTER TABLE projects ADD COLUMN changelog_path "
                "VARCHAR(500) NOT NULL DEFAULT 'changelog.md'"
            ))

    # #876 K-14 / K-22: the explicit-choice marker of changelog_path, added
    # by the duplicate-safe helper (two processes bootstrapping at once —
    # a hook and the MCP server — never crash each other). Runs after the
    # changelog_path block (a database older than #225 gains both in one
    # bootstrap).
    _add_column_if_missing(engine, "projects", "changelog_path_explicit",
                           _ddl_type(engine, "BOOLEAN NOT NULL DEFAULT {false}"))
    _heal_changelog_path_marker(engine)

    # Commit-trailer preferences (NULL = not answered yet). Nullable, no
    # backfill — a stored "no" is only ever the owner's answer, never a
    # guess. The duplicate-safe helper re-checks each column, so bootstraps
    # 2-3 issue no DDL and write nothing.
    for column in COMMIT_TRAILER_COLUMNS.values():
        if column not in proj_cols:
            _add_column_if_missing(engine, "projects", column, "BOOLEAN")

    # v0.4.0: add item_checkpoints.kind column + partial index for gate lookup.
    # See item #33 (gate checkpoints). Pre-v0.4 checkpoints default to kind='task'
    # via server_default — existing resolve_item behaviour preserved for rows
    # without explicit gate assignment.
    if "item_checkpoints" in inspector.get_table_names():
        cp_cols = {c["name"] for c in inspector.get_columns("item_checkpoints")}
        cp_idx = {ix["name"] for ix in inspector.get_indexes("item_checkpoints")}
        with engine.begin() as conn:
            if "kind" not in cp_cols:
                conn.execute(sa.text(
                    "ALTER TABLE item_checkpoints ADD COLUMN kind "
                    "VARCHAR(32) NOT NULL DEFAULT 'task'"
                ))
            if "ix_checkpoints_pending_gate" not in cp_idx:
                conn.execute(sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_checkpoints_pending_gate "
                    "ON item_checkpoints(item_id) "
                    "WHERE kind != 'task' AND status = 'pending' AND deleted_at IS NULL"
                ))

    # CP #1236 (ADR #52): creation-provenance columns on every mutating
    # ledger. Purely additive and nullable — no backfill, no literal dance,
    # portable ALTERs; each (table, column) pair heals independently, per
    # this runner's docstring.
    _PROV_SIZES = {"actor_type": 16, "actor_client": 64,
                   "session_id": 128, "source": 16}
    for prov_table in PROVENANCE_TABLES:
        if prov_table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(prov_table)}
        missing = [c for c in PROVENANCE_COLUMNS if c not in existing]
        if not missing:
            continue
        with engine.begin() as conn:
            for col in missing:
                conn.execute(sa.text(
                    f"ALTER TABLE {prov_table} ADD COLUMN {col} "
                    f"VARCHAR({_PROV_SIZES[col]})"
                ))

    # v0.2.0 + #216: migrate the legacy `in_current_sprint` boolean into the
    # item_sprints model, THEN drop the column. Sprint membership now has ONE
    # source of truth (item_sprints + the active sprint) and is derived on read.
    # The seed reads the column via raw SQL (it is intentionally absent from the
    # table metadata), so this whole block self-disables once the column is gone.
    # Order matters: seed BEFORE dropping, and drop the dependent index first
    # (SQLite refuses to drop a column an index still references).
    tables_after = set(inspector.get_table_names())

    # Legacy sprints are HISTORY, not the current sprint — keep them 'completed'.
    # INDEPENDENT, idempotent invariant (deliberately NOT nested under the column
    # check): on a DB where the column was dropped before this demote existed,
    # the legacy sprint would otherwise stay 'active' and wrongly resolve as the
    # current sprint. Heal per-piece (the rule from this runner's docstring).
    if "sprints" in tables_after:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE sprints SET status = 'completed', "
                "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE code = 'legacy' AND status = 'active' AND deleted_at IS NULL"
            ))

    # v0.2.0 + #216: migrate the legacy `in_current_sprint` boolean into the
    # item_sprints model, THEN drop the column. Sprint membership now has ONE
    # source of truth (item_sprints + the active sprint) and is derived on read.
    # The seed reads the column via raw SQL (intentionally absent from the table
    # metadata), so this block self-disables once the column is gone. Order
    # matters: seed BEFORE dropping, drop the dependent index first (SQLite).
    if "in_current_sprint" in col_names and {"sprints", "item_sprints"}.issubset(tables_after):
        _migrate_legacy_sprint_flag(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP INDEX IF EXISTS ix_items_sprint"))
            conn.execute(sa.text("ALTER TABLE items DROP COLUMN in_current_sprint"))

    # K-42 (#413): restore membership links that pre-G1 resolve_item/update_item
    # soft-removed on the terminal transition. Those writes stamped
    # item_sprints.removed_at with the SAME `now` value they wrote to
    # items.resolved_at, so an EXACT equality of the two timestamps identifies a
    # machine-made removal with no false positives worth worrying about — a
    # manual remove_item_from_sprint used a different `now` and MUST stay
    # removed. Without this, historical sprints read done=0 forever (G1 retains
    # membership; the old rows are still there to heal).
    # Idempotent by construction: a restored row has removed_at IS NULL and can
    # never match again.
    if {"items", "item_sprints", "sprints"}.issubset(tables_after):
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE item_sprints SET removed_at = NULL "
                "WHERE removed_at IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM items i WHERE i.id = item_sprints.item_id "
                "AND i.resolved_at IS NOT NULL "
                "AND i.resolved_at = item_sprints.removed_at)"
            ))

            # K-26a: cross-project links are legacy damage (an item linked to
            # another project's sprint). REPORT ONLY — deleting them would
            # destroy history we cannot reconstruct, and completion counts
            # already exclude them (_sprint_membership_stats_by_sprint filters
            # on the sprint's project). One stderr line, no mutation.
            stray = conn.execute(sa.text(
                "SELECT COUNT(*) FROM item_sprints ls "
                "JOIN items i ON i.id = ls.item_id "
                "JOIN sprints s ON s.id = ls.sprint_id "
                "WHERE ls.removed_at IS NULL "
                "AND i.deleted_at IS NULL AND s.deleted_at IS NULL "
                "AND i.project_id != s.project_id"
            )).scalar() or 0
        if stray:
            print(
                f"WARNING: dct found {stray} live cross-project sprint "
                "link(s) (item and sprint belong to different projects). "
                "Nothing was changed — review them manually and drop the "
                "bogus ones with remove_item_from_sprint.",
                file=sys.stderr,
            )

    # #611: seed tags / item_tags from the frozen items.tags JSON column. Runs
    # on EVERY bootstrap (no marker row — the facts are the truth) so an item
    # written by a still-running pre-#611 process is adopted next time (K-8).
    _backfill_item_tags(engine)


def _parse_legacy_tags_json(raw) -> tuple[list[str], list]:
    """Tolerant parse of one frozen `items.tags` value -> (names, dropped).

    NEVER raises: this feeds a block that runs on every bootstrap, so a raise
    here would brick every dct command. Bad JSON, a non-list payload and
    non-str / empty elements are dropped and REPORTED by the caller.

    K-7: legacy names are HISTORY, not input to the API — only lowercase/trim
    is applied. The comma and MAX_TAG_LENGTH rules are deliberately NOT
    enforced, so nothing is ever silently lost by length or punctuation.
    """
    if raw is None or raw == "":
        return [], []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [], [raw]
    if not isinstance(parsed, list):
        return [], [raw]

    names: list[str] = []
    dropped: list = []
    for element in parsed:
        if not isinstance(element, str):
            dropped.append(element)
            continue
        normalized = " ".join(element.split()).lower()
        if not normalized:
            dropped.append(element)
            continue
        names.append(normalized)
    return list(dict.fromkeys(names)), dropped


def _backfill_item_tags(engine: sa.Engine) -> None:
    """Seed tags / item_tags from the frozen items.tags JSON column (#611).

    Self-healing, not one-shot (the _migrate_legacy_sprint_flag pattern):
    candidates are items whose JSON column is non-empty and which have NO
    item_tags row at all. The anti-join guard is per-ITEM on purpose — once an
    item has spoken through the join table it owns its tag set, so clearing or
    re-tagging is never undone by the next bootstrap (a per-PAIR guard would
    resurrect removed tags forever, since the frozen column keeps the old JSON).

    K-22 — the ONE write to the frozen column: an item whose JSON yields no
    usable name never gets an item_tags row, so the per-item anti-join would
    re-select it on every bootstrap forever (a write transaction and a repeated
    warning per dct command). After the drop has been REPORTED (stderr, item id
    + the raw values), this function — the column's only sanctioned writer —
    overwrites that item's `items.tags` with '[]', which removes it from the
    candidate set. Scoped to ZERO-yield items: an item that migrated at least
    one name keeps its column as history. `updated_at` is deliberately left
    alone — this is a migration bookkeeping write, not an edit of the item.

    Self-disables once the column is dropped at 2.0.0.
    """
    from dct.core.tags import _active_names_conn, _add_tag_conn, _ensure_link_conn

    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    # NB two different scopes: `tables` is about the tags/item_tags TABLES,
    # `item_cols` about the frozen COLUMN on items. Both must be present.
    if not {"items", "tags", "item_tags"}.issubset(tables):
        return
    item_cols = {c["name"] for c in inspector.get_columns("items")}
    if "tags" not in item_cols:
        return

    no_links = ~(
        sa.select(sa.literal(1))
        .select_from(item_tags)
        .where(item_tags.c.item_id == items.c.id)
        .exists()
    )
    candidates_q = sa.select(items.c.id, items.c.tags, items.c.updated_at).where(
        items.c.tags.isnot(None),
        items.c.tags.notin_(("", "[]")),
        no_links,
    )

    # Steady state is ONE cheap anti-join probe on a read connection — no write
    # transaction is opened when there is nothing to migrate.
    with engine.connect() as conn:
        if conn.execute(candidates_q.limit(1)).first() is None:
            return

    migrated_names: set[str] = set()
    dictionary: list[str] = []
    with engine.begin() as conn:
        # K-9: the candidate set is re-SELECTed INSIDE the write transaction, so
        # the anti-join and the inserts see one consistent snapshot — no window
        # for a concurrent re-tagging to be overwritten.
        for row in conn.execute(candidates_q).fetchall():
            names, dropped = _parse_legacy_tags_json(row.tags)
            if dropped:
                # Printed BEFORE the converging write below is committed: a drop
                # the user never saw is unauditable data loss, so the report can
                # only ever run ahead of the blanking, never behind it.
                print(
                    f"WARNING: dct tag backfill — item #{row.id}: dropped "
                    f"{len(dropped)} unusable tag value(s) {dropped!r}",
                    file=sys.stderr,
                )
            for name in names:
                # No similarity guard here: historical names are not typos to
                # be corrected (the post-run report suggests consolidation).
                tag_id = _add_tag_conn(conn, name, force=True)["id"]
                _ensure_link_conn(conn, row.id, tag_id, row.updated_at)
                migrated_names.add(name)
            if not names:
                # K-22: nothing usable in there and no link to fall back on —
                # converge by emptying the frozen column (see the docstring).
                conn.execute(
                    sa.update(items).where(items.c.id == row.id).values(tags="[]")
                )
        if migrated_names:
            # K-30: read the FULL active dictionary (not just this batch) while
            # the write transaction is still open — the get-or-create above has
            # already populated it, so a legacy 'tests' meets an existing 'test'.
            dictionary = _active_names_conn(conn)

    if migrated_names:
        _report_possible_duplicates(sorted(migrated_names), dictionary)


# K-34: past this many active tag names the post-backfill scan degrades to a
# single pointer line. The scan is O(migrated x dictionary) and migrated names
# are themselves in the dictionary, so this one number bounds the whole report.
_REPORT_SCAN_LIMIT = 300


def _similarity_shortlist(candidate: str, names, threshold: float) -> list[str]:
    """Names that COULD reach `threshold` — a cheap SUPERSET of `_similar`'s hits.

    K-34: SequenceMatcher with autojunk=False is quadratic in the name lengths,
    so it must not run on every pair. Both bounds below are real upper bounds on
    ratio = 2*M/(len(a)+len(b)), because the matched blocks are a common
    subsequence: M <= min(len(a), len(b)) and M <= |multiset(a) & multiset(b)|.
    Anything the shortlist drops therefore could not have been flagged.

    Bucketing by first character was rejected: it drops auth/oauth (.889), a
    pair the guard's own vector table flags. The plural rule (a == b + "s") is
    kept unconditionally — it fires below the ratio threshold (ui/uis at .800).
    """
    counts = Counter(candidate)
    length = len(candidate)
    keep: list[str] = []
    for other in names:
        if other == candidate:
            continue
        if candidate == other + "s" or other == candidate + "s":
            keep.append(other)
            continue
        total = length + len(other)
        if 2 * min(length, len(other)) < threshold * total:
            continue
        if 2 * sum((counts & Counter(other)).values()) < threshold * total:
            continue
        keep.append(other)
    return keep


def _report_possible_duplicates(migrated: list[str], dictionary: list[str]) -> None:
    """Post-backfill consolidation SUGGESTION on stderr — never a block, never
    an auto-merge (that is the user's call, via `dct tags merge` / merge_tag).

    `migrated` is compared against the whole active `dictionary` (K-30), through
    the cheap prefilter and under the hard scan limit (K-34).
    """
    from dct.core.tags import SIMILARITY_THRESHOLD, _similar

    if len(dictionary) > _REPORT_SCAN_LIMIT:
        print(
            "NOTE: dct tag backfill — possible duplicates not scanned "
            f"({len(dictionary)} names); run `dct tags list` to review.",
            file=sys.stderr,
        )
        return

    pairs = set()
    for name in migrated:
        shortlist = _similarity_shortlist(name, dictionary, SIMILARITY_THRESHOLD)
        for other in _similar(name, shortlist):
            pairs.add(tuple(sorted((name, other))))
    for left, right in sorted(pairs):
        print(
            f"NOTE: dct tag backfill — possible duplicates: {left}/{right} — "
            "consider consolidating with `dct tags merge`.",
            file=sys.stderr,
        )


def _migrate_legacy_sprint_flag(engine: sa.Engine) -> None:
    """Seed a 'legacy' sprint per project with flagged items and link them.

    Idempotent — for each project with any item where `in_current_sprint=TRUE`,
    we ensure:
      1. A sprints row with code='legacy' exists (created if missing).
      2. Every flagged item has an item_sprints link ROW to that sprint,
         created once if absent. A link later soft-removed via the v0.2 API
         (removed_at set) is left untouched — this backfill is a one-shot seed,
         NOT an authority that overrides v0.2 sprint-membership decisions.
    Repeat invocations make no further writes once the invariant holds — the
    existence probe spans the FULL (item_id, sprint_id) primary key regardless
    of removed_at, so a re-run never collides with a soft-removed row.

    One-shot seed (#216): reads the legacy `in_current_sprint` column via raw
    SQL because the column is absent from the table metadata and is DROPPED
    right after this seed runs (see _apply_schema_updates). No-op once the
    column is gone, so it is always safe to call. The full-PK existence probe
    means a re-run never collides with a soft-removed link (the #215 lesson).
    """
    from datetime import datetime

    # The column only exists in pre-#216 DBs. Self-disable cleanly otherwise.
    if "in_current_sprint" not in {c["name"] for c in sa.inspect(engine).get_columns("items")}:
        return

    with engine.begin() as conn:
        # Read the legacy column via literal_column (it is absent from the table
        # metadata) while keeping updated_at a TYPED column, so the value stays a
        # datetime and can be reused as item_sprints.assigned_at.
        flagged_rows = conn.execute(
            sa.select(items.c.id, items.c.project_id, items.c.updated_at).where(
                sa.literal_column("in_current_sprint") == sa.true(),
                items.c.deleted_at.is_(None),
            )
        ).fetchall()
        if not flagged_rows:
            return

        by_project: dict[int, list[tuple[int, datetime]]] = {}
        for row in flagged_rows:
            by_project.setdefault(row.project_id, []).append((row.id, row.updated_at))

        for project_id, flagged in by_project.items():
            existing = conn.execute(
                sa.select(sprints.c.id).where(
                    sprints.c.project_id == project_id,
                    sprints.c.code == "legacy",
                    sprints.c.deleted_at.is_(None),
                )
            ).first()
            if existing:
                sprint_id = existing.id
            else:
                row = conn.execute(
                    sprints.insert()
                    .values(
                        project_id=project_id,
                        name="Pre-0.1 legacy sprint",
                        code="legacy",
                        kind="sprint",
                        # Legacy sprint is HISTORY, not the current sprint — it is
                        # 'completed' so it never resolves as the active/current
                        # sprint after #216. Items keep their membership for the
                        # record; the user sets a fresh current sprint via /handoff.
                        status="completed",
                        started_at=datetime.now(UTC),
                        ended_at=datetime.now(UTC),
                    )
                    .returning(sprints.c.id)
                ).first()
                assert row is not None, "INSERT RETURNING must return a row"
                sprint_id = row.id

            # Probe by the FULL primary key (item_id, sprint_id) — deliberately
            # NOT filtered by removed_at. item_sprints_pkey spans both columns
            # irrespective of removed_at, so a soft-removed link still occupies
            # the slot. Filtering removed_at IS NULL here (the original bug)
            # would miss a flagged item whose link was later soft-removed via
            # remove_item_from_sprint, then re-INSERT it -> UniqueViolation.
            already_linked = {
                r.item_id for r in conn.execute(
                    sa.select(item_sprints.c.item_id).where(
                        item_sprints.c.sprint_id == sprint_id,
                    )
                )
            }
            for item_id, updated_at in flagged:
                if item_id in already_linked:
                    continue
                conn.execute(
                    item_sprints.insert().values(
                        item_id=item_id,
                        sprint_id=sprint_id,
                        assigned_at=updated_at,
                    )
                )


def row_to_dict(row: sa.Row) -> dict:
    return dict(row._mapping)
