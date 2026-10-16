# dct — Architecture & Configuration

The module map, database schema, migrations, the full `dct.toml` reference, MCP scope precedence, and project auto-detection. ← back to the [README](../README.md).

## Global-by-design scope model

dct is a **cross-project tool** — every surface is user-scoped, not per-project:

| Artefact | Location | Mechanism |
|---|---|---|
| Database config | platform config dir (`~/.config/dctools/dct.toml`; legacy `~/.claude/dct.toml` fallback) | TOML read by `load_config()`; `dct config path` shows the resolved location |
| Database | SQLite file or PostgreSQL | `[database].url` or `DCT_DATABASE_URL` |
| MCP server | Claude: `~/.claude.json`; Codex: `$CODEX_HOME/config.toml` | User-scoped client registration |
| Skills | Claude: `~/.claude/skills/<name>`; Codex: `~/.agents/skills/<name>` | Both inventories derive from `dct/skill_sources/manifest.toml` |
| Claude hooks | `~/.claude/hooks/dct-*.sh` | Symlinks → `<dct-install>/dct/hooks/`, wired into `~/.claude/settings.json` |
| Codex hooks | `$CODEX_HOME/hooks.json` | Native definitions calling the packaged Python bridge; explicit client trust is required |
| dct reference | `paths.config_dir() / "dct-reference.md"` | Shared generated catalog; client instruction blocks name the resolved path; Claude retains its owned legacy alias |

Register dct once per client and it is available across that client's sessions. Opening a new project directory needs zero integration setup. DCT 1.6.0 includes these shared-reference and Codex adapters; native Codex hooks remain experimental and require explicit client trust. See [client availability](integrations.md#release-availability).

## Module map

The modules under `dct/` that have a role of their own (empty package
`__init__.py` files are left out). A role is summarized from the module's
docstring where it has one, otherwise from its code. Entries marked
`[one impl]` are the single implementation of their concern: reuse them —
never add a second copy of that logic. The map is an aid, not proof: search
the code before adding similar logic; a module missing here is not by itself
evidence of duplication.

```
dct/__init__.py          — Package version (__version__ from the installed
                           dctracker metadata, constant fallback)
dct/config.py            — Config loading (platform config dir + legacy
                           ~/.claude fallback + env vars; ADR #52)
dct/paths.py             — Platform-neutral config/data/cache/state dirs
                           (XDG on POSIX, platformdirs on Windows); [one impl]
                           the SQLite URL builder / parser (sqlite_url,
                           sqlite_database_path — K-99: never concatenate or
                           slice "sqlite:///", SQLAlchemy 2.0 and 2.1 parse
                           the path differently)
dct/migrate.py           — dct migrate-home (legacy → platform-neutral home)
dct/update.py            — Passive update check (#356): check_for_update
                           (24h cache, daemon-thread PyPI probe, never blocks
                           or raises), get_cached_notice (no network); the
                           install-method detection behind `dct upgrade`
dct/db.py                — SQLAlchemy Core tables + engine factory + migrations
dct/core/engine.py       — [one impl] Engine bootstrap: bootstrap_engine
                           (config → engine → optional DDL; create=False runs
                           none — the read-only web viewer) and
                           readonly_engine (K-84: no DDL, no file creation,
                           db.check_schema_ready; for every command that
                           promises no side effects)
dct/core/limits.py       — [one impl] ensure_fits: column-length guard shared
                           by every core writer (#792) — a value longer than
                           its VARCHAR(N) is refused, never truncated; limits
                           read from the schema, never typed in
dct/core/text.py         — [one impl] Free-text helpers shared by every
                           surface: preview (the N-char ceiling, ellipsis
                           included), item_label / visible_label, the CLEAR
                           sentinel of free-text clears
dct/core/provenance.py   — [one impl] Write-path creation provenance
                           (actor_type / actor_client / session_id / source)
                           filled by column defaults from a ContextVar; each
                           entry point calls set_provenance(from_environment(
                           ...)) once; unknown stays NULL
dct/core/projects.py     — Projects CRUD (+ realpath-canonical CWD detection)
dct/core/items.py        — Items / notes / checkpoints CRUD; item creation
                           on the caller's connection: create_item_conn (full
                           validation, live project, tags) — used by
                           create_item, promote_checkpoint_to_item,
                           promote_finding_to_item and promote_section_to_item;
                           the task mirror inserts its own item directly in its
                           composed transaction
dct/core/changelog.py    — Changelog entries CRUD + release stamping (the stamp
                           is the ONLY writer of versions, K-102: add_entry
                           refuses one; import_released_entry = old history);
                           [one impl] branch metadata rules (#801:
                           resolve_branch_fields, decide_branch_fields,
                           normalize_worktree)
dct/core/releases.py     — [one impl] Release ledger (K-41/K-60/K-61): records
                           written with the stamp, one state machine
                           (TRANSITIONS; advance, abandon), immutable
                           candidate, write-once release commit / manifest /
                           proof, legacy reconstruction (its verification
                           scope recorded, K-110), the controlled takeover of
                           a record without consent (take_over_release, K-119
                           M3 — the only way a record becomes dct's after its
                           stamp), the project's version-file contract
                           (K-73); the verified release_mode, executor and
                           write-once consent / proposal / render; K-115:
                           tag_proof / push_proof (a status is never proof),
                           mutual consistency of a record's fields,
                           duplicates of one version refuse writes; every
                           mutation checks its actor (docs/release-workflow.md)
dct/core/sprint.py       — Current-sprint ops; delegates to item_sprints; the
                           conn-level readers shared by review_sprint and the
                           session snapshot
dct/core/sprints.py      — First-class sprints (single source of truth)
dct/core/handoff.py      — Session handoffs (cross-session prompt ledger)
dct/core/decisions.py    — ADR / sprint-amendment / open-question records
dct/core/tags.py         — Global tag dictionary + item_tags links (similarity
                           guard, merge)
dct/core/analytics.py    — Read-only aggregation helpers for the web viewer
                           (radar, backlog health, velocity): grouped queries,
                           never a per-project loop
dct/core/changelog_file.py — [one impl] Which file is a project's changelog
                           (#876): the resolver (explicit path — projects.
                           changelog_path_explicit — / detection among the
                           candidate's tracked files; every path component
                           checked in the tree and on disk; filter-aware
                           work-tree check) for plan, effects and CLI;
                           problems refuse before the stamp
dct/core/version.py      — [one impl] Version contract (parse / normalize /
                           compare / bump, X.Y | X.Y.Z, leading v dropped;
                           storable_version gates every version write) +
                           project-file autodetect (VERSION_FILES)
dct/core/version_sync.py — The project's scripts/dct-version-sync.sh, written
                           from the packaged generic template (K-76); never
                           overwrites
dct/core/gitinfo.py      — [one impl] Git readers (toplevel / branch / common
                           dir / head) + resolve_checkout: same-repo checkout
                           of a project (repo root vs project root),
                           best-effort; the STRICT verify_release_checkout
                           (confirmed repo / confirmed non-repo / error) gates
                           the CLI release; git_read / git_write for the
                           strict git commands
dct/core/procs.py        — [one impl] Supervised process runner (K-108/K-113):
                           own process group, TERM→KILL + reap on timeout /
                           interruption / exception, bounded or
                           complete-or-refuse capture, streamed lines; used by
                           every strict git command and the release
                           workflow's sync script
dct/core/records.py      — [one impl] Shared STRICT project-row id validator
                           (validate_project_rows)
dct/core/checkpoints.py  — Item checkpoint helpers
dct/core/ids.py          — ULID generation
dct/core/plans/          — Plans subsystem: parser.py (pure MD parser),
                           classify.py (rules + optional command), classification.py
                           (explicit/manual and revision-guarded batch resolution),
                           crud.py, ingest.py
dct/services/            — Service layer (reserved for future shared app ops)
dct/workflows/session.py — [one impl] G1 composed ops: get_context_snapshot /
                           pickup_work / handoff_work (one impl for MCP,
                           CLI and skills; single transaction each)
dct/workflows/task_mirror.py — Append-only client plan/task observations
dct/workflows/changelog_import.py — Explicit validated historical changelog import
dct/workflows/triage.py  — [one impl] Triage findings ledger (#909): cycles,
                           batch-atomic record / decide / suggest of findings,
                           close (CLOSED payload), promotion to an item,
                           recurrence by normalised fingerprint, reviewer
                           precision, CARDS / CLOSED export for the triage
                           page; classify() is the one port of the template's
                           class rule; suggestion_digest() is the one hash of
                           a cycle's cards + proposals (export's
                           SUGGESTED.digest, checked by accept_suggestions
                           under the cycle lock); parse_decision_batch() the
                           one parser of the page's decision text; writers of
                           an open cycle serialise on its row
                           (db.lock_cycle_row)
dct/workflows/triage_page.py — Triage page builder (#930, no DB):
                           build_triage_page(export, template_html, ...)
                           fills the three constants (CARDS / CLOSED /
                           SUGGESTED, via page_constants_json) and the
                           metadata placeholders of the ORIGINAL template
                           once — inserted ledger text is never re-scanned.
                           Metadata has two escaping contexts: HTML outside
                           <script> (HTML-escaped) and a quoted JS string
                           literal inside it (JS string escapes, read back
                           verbatim; `/`, `[` and `]` are escaped too, a
                           second layer so a value cannot reshape a regex);
                           any other script position — code, a comment, a
                           regex or template literal — is REFUSED, never
                           substituted. The scan tells a regex `/` from
                           division by the token before it (K-42 / K-43): a
                           `)` closing an if / while / for / with head →
                           regex; a `)` of a call or grouping, an
                           identifier, number, literal, `]` or a postfix
                           `++` / `--` (after an operand, same line) →
                           division; a `/` it cannot classify (after `}`,
                           or after `of` / `await` / `yield` — keyword or
                           identifier — directly or through a `++` / `--`,
                           K-46) → the template is refused naming the line,
                           never guessed. A template literal with an
                           unescaped `${` (K-47) is refused naming the line
                           — the scan does not follow the code inside it;
                           plain ones and an escaped `\${` are kept.
                           STORAGE_KEY =
                           `triage-<project slug>-<code slug>-c<cycle id>-v1`
                           (the immutable id last, so the key is injective;
                           drafts kept under the earlier key order are not
                           migrated — ledger decisions and proposals are
                           unaffected). Used by `dct triage page`; dct ships
                           no template
dct/workflows/release/   — Release workflow (K-72): proof layer, read-only for
                           index, refs, work tree and database (the plan may
                           write unreferenced git objects to build the future
                           tree of uncommitted work, as the preview does) —
                           proof.py ([one impl] trailer / revert / squash
                           rules), plan.py (`dct release plan`, `dct clog
                           candidates`); effects layer — effects.py (`dct
                           release attest / prepare / apply / resume / abandon
                           / reconstruct / takeover / version-files`,
                           alias `clog release`: the version sync runs once,
                           isolated, in prepare; commits through the repo's
                           hooks, tag, push; every effect verified before
                           the ledger records it; schema: docs/release-workflow.md);
                           oplock.py (the per-repository operation lock, K-96 D7)
                           — dct/workflows never imports from dct/integrations
dct/mcp/server.py        — MCP server, SDK 2.2 MCPServer (76 tools); the MCP
                           layer is client-neutral (no host names);
                           dct/server.py is a compat shim
dct/cli.py               — argparse CLI entry point
dct/export.py            — Keep a Changelog MD export
dct/integrations/        — Host-client adapters; core / db / services /
                           workflows / mcp never import from this package
dct/integrations/base.py — [one impl] Shared contract (ClientIntegration) and
                           conservative file ownership (atomic_write,
                           managed_instructions, managed-copy markers)
dct/integrations/manager.py — Client selection (get_integration) + shared
                           lifecycle (status / install / refresh), no database
                           bootstrap
dct/integrations/claude.py — Claude Code adapter: MCP registration, skill/hook
                           symlinks, settings.json wiring (`dct hooks wire`);
                           dct/setup.py is a compat shim
dct/integrations/codex.py — Codex MCP, workflow skills and managed AGENTS.md
dct/integrations/reference.py — [one impl] Shared reference ownership, installation
                           and resolved-path instruction rendering
dct/integrations/codex_hooks.py — Native event bridge over shared hook policies
dct/integrations/doctor.py — Read-only per-runtime dependency diagnostics
dct/server.py, dct/setup.py — Compat shims; tests monkeypatch the
                           implementation modules (dct/mcp/server.py,
                           dct/integrations/claude.py), never the shims
dct/assets/              — Indirection for packaged asset dirs (physical
                           skills/ + hooks/ stay put until 2.0.0 — host
                           symlinks point into them) + packaged text assets
                           (the user-level CLAUDE.md snippet and the
                           dct-reference.md it points at, the Codex
                           instructions and skills, the version-sync script
                           template)
dct/web/                 — Read-only Flask dashboard (dct web): app.py
                           (factory; bootstrap_engine(create=False), never
                           DDL), blueprints/ (one per view), changes.py (SSE
                           change token), serializers.py (JSON-native +
                           sanitized markdown)
dct/skill_sources/       — Authored contracts and manifest for all 14 workflows
dct/skill_render.py      — [one impl] Offline renderer of both clients' skills,
                           metadata, full fallback and concise reference;
                           scripts/render_skills.py regenerates/checks freshness
dct/skills/              — Generated Claude Code skills (symlinked by dct init)
dct/hooks/               — Packaged Claude Code hooks (symlinked by dct init);
                           [one impl] dct-changelog-count.lib.sh, the shared
                           changelog-count library both changelog hooks source
```

## Database Schema

SQLAlchemy Core tables (not the ORM) with a `deleted_at` column for soft deletes (ledger pattern — nothing is ever hard-deleted). 18 tables:

**Core**:
- `projects` — slug, name, path, current_version, manages_changelog, changelog_path + changelog_path_explicit (#876 K-14: the marker says whether the path is the owner's choice — `dct project changelog-path <slug> <path>`, also exactly `changelog.md`; false = "detect": `core.changelog_file` finds the tracked changelog of the project root and the default `changelog.md` is used only when none exists; `--detect` goes back to it. Added by a one-shot bootstrap block that marked stored non-default paths explicit and the historical default not), version_files / version_files_confirmed_at / version_files_history (K-73: the owner-confirmed JSON list of repo-relative files a release must change; NULL = not learned; every replaced list is appended to the history), release_executor (K-85: `dct` default \| `own`; `dct project release-executor`), changelog_pr_refs (K-90: render `(#N)` PR references, default off; `dct project changelog-pr-refs`), release_proposal (K-76: the PENDING version-file proposal of `dct release prepare` — canonical JSON, replaced by a new prepare, consumed and cleared by the stamp that approves it; NULL = nothing prepared), commit_co_authored_by / commit_session_link (the commit-trailer preferences — add a `Co-Authored-By` trailer / a session or artifact link; each NULL = not answered yet, false = a stored "no", true = a stored "yes"; asked once at the project's first configuration, set with `dct project trailers`, read by /commit through `list_projects`; a project's own rules forbidding a trailer still win over a stored "yes". Nullable, added without backfill by a self-healing bootstrap block)
- `items` — project_id, type, title, short_title (nullable 50-char label, #817), priority, status, component, source_file, source_lines, resolution, tags (**DEPRECATED** — see below). Sprint membership is **not** a column: `in_current_sprint` is derived at read time from `item_sprints` (#216 dropped the legacy boolean).
- `item_notes` — item_id, note (append-only)
- `item_checkpoints` — item_id, text, status, kind, sort_order
- `changelog_entries` — project_id, version, category, entry, component, source_file, source_lines, related_item_id, branch / worktree / branch_source (nullable git context the entry was written on, #801; `branch_source` = `client` or `server`). `version` is written ONLY by the release stamp (`release_entries`, K-102): `add_entry` refuses a version and runs under the project lock; old released history is imported with `import_released_entry` (refused for a version that has a release record)
- `session_handoffs` — project_id, prompt, context_summary, scope, created_by, consumed_at, consumed_by
- `releases` — durable release records (K-41): project_id, version (normalized), mode, entry_ids (JSON array of the exact set, `[]` = version-only), candidate_sha (immutable), checkout_root, release_commit_sha (set once), manifest (JSON object of verified artifacts, write-once per key), proof (JSON map entry id → proving commits / merge commit / PR), status (one state machine, `core/releases.TRANSITIONS`: stamped → files_written → committed → tagged → pushed → completed, plus terminal `abandoned` from any unfinished status), completed_at, abandoned_at, abandon_reason, release_mode (git \| without_git, the verified mode), executor (dct \| own, from the project at stamp time — the stamp compares it with the executor its caller expects, K-106), consent / proposal / render (write-once JSON — [release-workflow.md](release-workflow.md#the-release-ledger-record-fields)), tag_proof / push_proof (K-115 G4: the VERIFIED tag identity `{name, object, commit, annotated, recorded_at}` and push `{target, tag_commit, branch_tip, recorded_at}`, write-once JSON; NULL = not proven — a record with git cannot be `tagged` / `pushed` / `completed` without them, see [proofs](release-workflow.md#proof-of-the-tag-and-of-the-push-k-115)); one live record per (project, version) — a ledger that still holds several live records of one semantic version refuses every write of it and names them on read (K-115 G6). Written in the same transaction as the stamp (`release_entries`) — or, for a release older than records, by `reconstruct_release` without re-stamping — advanced by `core/releases.advance_release`, ended by `abandon_release`.

**Plans / sprints / decisions**:
- `plans` — ulid, project_id, slug, title, kind, source_file, source_hash, status, sprint_id, imported_at
- `plan_sections` — ulid, plan_id, parent_section_id (self-FK), heading, depth, slug, sort_order, section_type, section_type_confidence, needs_llm_review, status, sprint_id, converted_to_item_id, classification_source (rules/command/manual; NULL = unknown legacy or generic add_plan_section without a typed result), classification_provider, classification_model (only when actually known), classified_at (UTC), classification_revision (non-null monotonic concurrency token, default 0). Manual resolved types survive automatic rescans until explicitly revoked; classification metadata/type/confidence/review state are written coherently. New ingested rows atomically record parser/provider provenance with type/review/time and revision 0; legacy/generic rows never infer provenance from confidence/type. The conditional classification UPDATE against identity, observed revision and heading/parent input linearizes at its SQL statement snapshot; it does not promise that parent content remains unchanged afterwards. Target-row predicates prevent concurrent manual/ABA overwrite, and no provider call holds a write transaction. Revision is neither identity nor a content hash.
- `plan_checkpoints` — ulid, plan_section_id, text, status, sort_order, converted_to_item_id
- `sprints` — project_id, name, code, kind, status, goal, started_at, ended_at
- `item_sprints` — link table (item_id, sprint_id, assigned_at, removed_at)
- `decisions` — ulid, project_id, kind, ref_code, title, context, decision, consequences, alternatives, status, superseded_by, source_file, plan_id, plan_section_id, sprint_id

**Tags** (#611):
- `tags` — global tag dictionary: name (UNIQUE among active rows), description. Deliberately **no `project_id`** — the dictionary is user-scoped, so one name means the same thing in every project, while the item lists that filter on it stay project-scoped.
- `item_tags` — link table (item_id, tag_id, assigned_at, removed_at), mirroring `item_sprints`: composite PK, links are soft-removed and reactivated in place.

**Triage findings ledger** (#909, `dct/workflows/triage.py`):
- `triage_cycles` — one review round: project_id, code (e.g. `rev-877-r2`, UNIQUE per project among live rows), topic, scope (what was reviewed), base_commit, rubric_version (default 2), artifact_url, created_by, opened_at, closed_at (NULL = open), closed_source / closed_notes (the CLOSED block's source and notes).
- `findings` — cycle_id, seq (the K-n number, UNIQUE per cycle among live rows), fingerprint + fingerprint_norm (the recurrence identity, normalisation v1 below), title; the rubric v2 fields under full names: impact (S, 0-3), reachability (O, `0`-`3` \| `?`), evidence (E, 0-3), fix_cost (K, S \| M \| L), origin (diff \| pre \| latent), finding_type — all six NOT NULL (rubric §1, K-4); suggested_decision / suggested_opt / suggested_note / suggested_by / suggested_at (an agent's proposal — the template's SUGGESTED — never a decision); the template flags conflict / violation (default true) / dispute / open_design; area (where), reviewer (the CARD's `src`: `codex`, `panel:security`, … — not `source`, which is the provenance column), context; class_rec (classify()) / class_override / class_reason; decision (NULL = undecided) / decision_opt / decision_note (the owner's comment verbatim) / decided_at / decided_by (who decided) / decision_source (where the owner's words are recorded) — the last three NULL while undecided and written together (K-5 / decision_actor_type / decision_actor_client / decision_session_id / decision_actor_source (the LAST decision's writer, read from the provenance context in the same UPDATE as decided_at, refreshed as a set on every overwrite and NULL before a decision — an audit record, never backfilled, not authentication; the row's creation provenance stays untouched); an audit record distinct from the provenance columns, not authentication); decision_text_origin (TEXT, NULL — #930 K-40: the "Punkt wyjścia:" line of the pasted decision text that wrote this decision, verbatim; written with the decision stamp, reset to NULL by a later decision without one; it proves the form was seeded from proposals, not who wrote the note; exposed as CLOSED.answers[K-n].origin — key present only when non-NULL, K-45); item_id (promoted to), first_seen_id (self-FK: the earliest live finding of the project with the same fingerprint_norm; NULL = first). Indexed on cycle_id and fingerprint_norm. fingerprint_norm uses normalisation v1 (NFC + lower + whitespace collapse + fixed-point trailing punctuation strip, `triage.normalize_fingerprint`); the ledger shipped with it, so no reconciliation of earlier norms exists.
- `finding_options` — finding_id, key (UNIQUE per finding among live rows), text, why, is_root, is_recommended (at most one per finding, checked in code).
- `finding_reviewers` (#930, migration 015) — finding_id (FK findings, NOT NULL), reviewer (VARCHAR(50), a canonical token `^[a-z0-9][a-z0-9:._-]*$`, lowercased on write and refused otherwise — never guessed from the free-text `src`); UNIQUE (finding_id, reviewer) among live rows. The reviewer identity behind `reviewer_precision` (a finding with two reviewers counts for both; one without rows counts under `(unattributed)`) and the `reviewer` filter of `list_findings`; the CARD's `reviewers` list. No backfill: findings recorded before #930 stay unattributed.

Value domains (rubric enums, decisions, classes) are validated by the workflow, not by CHECK constraints; every VARCHAR goes through `ensure_fits`. The four tables carry the provenance columns and are in `INDEX_HEALED_TABLES`.

`items.tags` (the old JSON-array-in-TEXT column) is **deprecated and frozen**:
nothing writes it any more, `_backfill_item_tags` is its only sanctioned reader,
and it is dropped at 2.0.0.

### Migrations

Full DDL lives in:
- `migrations/001_init.sql`
- `migrations/002_sprint_and_handoff.sql`
- `migrations/003_plans_sprints_decisions.sql`
- `migrations/004_tags.sql`
- `migrations/005_items_short_title.sql`
- `migrations/006_changelog_branch.sql`
- `migrations/007_releases.sql`
- `migrations/008_release_ledger.sql`
- `migrations/009_release_ledger_fields.sql`
- `migrations/010_release_pending_proposal.sql`
- `migrations/011_release_tag_push_proof.sql`
- `migrations/012_release_preview.sql`
- `migrations/013_commit_trailer_preferences.sql`
- `migrations/014_triage_findings.sql`
- `migrations/015_finding_reviewers.sql` (also `findings.decision_text_origin`, healed at runtime through `TRIAGE_LEDGER_COLUMNS`)
- `migrations/016_plan_classification.sql` (classification provenance and revision token)

At runtime, the engine creates every MISSING table with all its indexes (`_create_missing_tables` — race-safe against a concurrent bootstrap: a duplicate table / index / relation error rolls the attempt back, the whole schema of those tables is re-checked and finished, at most `SCHEMA_CREATE_ATTEMPTS` tries, any other error propagates; tables that already exist are left to the explicit blocks below — except the release-ledger and triage-ledger tables (`INDEX_HEALED_TABLES`, K-93 A2 / #909), whose missing required indexes every bootstrap completes, warning once instead of failing when existing rows violate a unique index) plus an idempotent `_apply_schema_updates()` that:
- adds later columns in place (e.g. `item_checkpoints.kind` for gate checkpoints; new columns go through the duplicate-safe `_add_column_if_missing` — `ADD COLUMN IF NOT EXISTS` under a 5 s `lock_timeout` on PostgreSQL, a re-checked "duplicate column" error on SQLite),
- handles dialect-specific bool literals (PostgreSQL strict, SQLite permissive),
- sets NOT NULL on `findings.origin` / `finding_type` of a ledger created while they were nullable (`_heal_triage_not_null`, #909 K-4): PostgreSQL only, and only for a column without NULL rows — a NULL is never filled with a guessed value; such rows leave the column as it is and a stderr warning names them on every bootstrap until they are fixed by hand. SQLite keeps the existing column nullable (no ALTER COLUMN without a table rebuild); the workflow's write-time validation guards it,
- auto-migrates legacy `items.in_current_sprint=TRUE` flags into the new `item_sprints` table as a per-project "legacy sprint" (then drops the column), and
- backfills `tags` / `item_tags` from the frozen `items.tags` JSON column (`_backfill_item_tags`): tolerant parse (bad JSON is dropped with a stderr note, never raised), per-item anti-join guard so re-tagged items are never re-seeded, and a one-shot stderr similarity report suggesting `dct tags merge` for near-duplicates — a suggestion, never a block.

Schema updates run on every engine bootstrap — verify destructive changes against a throwaway database and keep each step idempotent.

Read-only commands (`dct pickup --consume never`, the MCP `get_context_snapshot` / `pickup_work(consume="false")` paths including project resolution, `dct release plan` / `status`, `dct clog candidates`, the previews of `dct release reconstruct` / `takeover` / `version-files`) never bootstrap (K-84): they open the database read-only (`core.engine.readonly_engine` — SQLite `mode=ro` without a journal-mode change or file creation, PostgreSQL READ ONLY transactions), check that every table and column of the current schema exists (`db.check_schema_ready`) and otherwise refuse with a message naming `dct db upgrade`, the explicit bootstrap. Caveat: on a SQLite database already in WAL mode SQLite itself may create the `-wal` / `-shm` sidecar files for any reader, also a read-only one — the database file, its schema and its data stay unchanged.

SQLite URLs are built and parsed in ONE place (K-99): `dct.paths.sqlite_url(path)` renders through SQLAlchemy's own URL object and `dct.paths.sqlite_database_path(url)` parses with `make_url` — never by concatenating or slicing `sqlite:///`. SQLAlchemy 2.1 percent-decodes the database part of a URL and 2.0 does not, so a hand-built URL names another file on 2.1 as soon as the path holds a `%`; the helpers behave the same on both (the supported floor stays `sqlalchemy>=2.0.52`). A path the installed SQLAlchemy cannot express (a `?` on the 2.0 line) is refused instead of silently opening another file. A URL you write by hand into `dct.toml` follows the rules of the SQLAlchemy you run.

### Concurrency contracts

Two helpers in `dct/db.py` serialise writers that read-then-write:

- `project_transaction(engine, slug, write=True)` — composed project-level
  writers (releases, sprint and handoff ops, #430): a PostgreSQL advisory lock
  on the project id taken before a REPEATABLE READ transaction, `BEGIN
  IMMEDIATE` on SQLite; waits are bounded to five seconds.
- `lock_row(conn, table, row_id)` — the row-level variant (#910, generalised
  by #909): `SELECT … FOR NO KEY UPDATE` on the row (live rows only; returns
  whether it exists) with a transaction-local `lock_timeout` of five seconds on
  PostgreSQL, `BEGIN IMMEDIATE` before the read on SQLite. Two wrappers name
  its contracts:
- `lock_item_row(conn, item_id)` — the gate contract (#910). It is
  the existence check of every participant in the gate contract — the
  terminal transition (`update_item` / `resolve_item`, whose gate guard reads
  pending gates and writes the status in one transaction) and every writer
  that creates or reopens a gate checkpoint (`add_checkpoint`,
  `checkpoints.set_status`) — so a gate lands either before the guard reads
  or after the status is written, never in between. Any new writer that can
  make a closed item's gates pending, or that must see the latest gates,
  takes this lock first. Lock-in: `tests/test_gate_race.py` (PG lane and a
  file SQLite database, the competitor proven blocked before the holder
  commits).
- `lock_cycle_row(conn, cycle_id)` — the open-cycle contract of the triage
  ledger (#909): every writer that requires an open cycle (record / decide /
  suggest findings) and `close_triage` take it, so nothing is written into a
  cycle after its close. Where it sits among each operation's locks: Lock
  order below. Lock-in: `tests/test_triage_schema.py`.

#### Lock order

The row locks each operation takes explicitly, in the order it takes them
(all through `lock_row` or its wrappers; one transaction each):

| Operation | Explicit locks, in order |
|---|---|
| `record_findings` | project row → cycle row (first_seen is project-wide, so recorders of one project queue on the project first) |
| `decide_findings` | cycle row → the promoted item row of each finding re-decided reject / note (`lock_item_row`, K-2) |
| `suggest_findings`, `close_triage` | cycle row |
| `promote_finding_to_item` | cycle row → finding row; the new item then takes FK `KEY SHARE` locks (project row on insert, item row on the finding's back-link) |
| `promote_checkpoint_to_item`, `promote_section_to_item` | their own checkpoint / section row; the new item takes FK `KEY SHARE` locks as above |
| gate contract (`update_item` / `resolve_item`, `add_checkpoint`, `checkpoints.set_status`) | item row (`lock_item_row`) |

Not every operation starts at the project row: only `record_findings`
takes it. The implicit `KEY SHARE` locks that PostgreSQL takes on the
referenced row of every foreign-key insert or update do not conflict with
`lock_row`, because it locks `FOR NO KEY UPDATE` (no key column is ever
changed under it), not `FOR UPDATE` — so, for example, a promotion inserting
an item of a project whose row a recorder holds does not wait on it. The
helpers still exclude each other: two `lock_row` calls on one row queue.

## Configuration

### The dct config file (`dct.toml`)

The full reference (all sections; `dct init` writes a subset with sensible defaults):

```toml
[database]
url = "postgresql+psycopg://localhost:5432/dct"
# or: url = "sqlite:////path/to/dct.db"   # four slashes: an absolute path

[plans]
# Resolution is enabled; local classification rules require no model
auto_resolve_uncertain = true

[classification]
# Pluggable classifier (ADR #52; formerly [plans].classify_* — old keys
# still read as permanent aliases). provider: "rules" | "command" | "disabled"
# (other providers are future scope; unknown values behave as "disabled").
provider = "rules"
command = ["claude", "--print"]
model = "claude-haiku-4-5-20251001"
timeout_seconds = 30

[integrations.claude]
# TaskCreate/TodoWrite ↔ dct integration (formerly [mirror].task_to_dct,
# still read as a permanent alias):
#   auto — mirror every task into a session-scoped item as cc-task checkpoints
#   ask  — keep Claude Code tasks and dct separate
#   off  — disable the task-mirror hook entirely
task_mirror = "auto"

[web]
host = "127.0.0.1"       # localhost-only by design
port = 8787              # preferred port; auto_fallback scans upward if busy
auto_fallback = true
open_browser = false
refresh_poll_ms = 2000   # SSE change-token poll interval

[update]
# Passive PyPI update check (#356). When false, disables BOTH the background
# version probe and the CLI/MCP nudge. Offline-safe and non-blocking regardless.
check = true

[triage]
# The triage page template used by `dct triage page` (#930) when no
# --template is given. dct ships no template; without either the command
# refuses and names both ways.
template = "/path/to/triage-template.html"
```

**Environment overrides**: `DCT_DATABASE_URL` overrides `[database].url`;
`DCT_CONFIG_PATH` pins the config file location; `DCT_DATA_DIR` pins the data
dir. Default resolution (ADR #52): platform config dir (e.g.
`~/.config/dctools/dct.toml`) with a permanent legacy `~/.claude/dct.toml`
fallback — `dct config path` prints the resolved locations, `dct migrate-home`
moves a legacy install. The retired top-level `dct_root` key is still parsed
from old files but never written.

### Update check (`dct/update.py`)

`dct/update.py` implements a passive, offline-safe update check (#356). A cheap
`check_for_update()` reads a 24h on-disk cache (`dct-update-check.json` in the platform cache dir, e.g. `~/.cache/dctools/`);
when stale it spawns a daemon thread that queries PyPI (2s timeout) and rewrites
the cache — it never blocks and never raises. `get_cached_notice()` is a pure
cache read (zero network) that returns a one-line "newer release available" notice
when the cached `latest` is strictly newer than the installed version. CLI commands
print the notice on stderr; the MCP server appends it to its instructions at start.
`dct upgrade` detects the install method (editable / uv tool / pipx / pip), runs the
matching upgrade, and refreshes the skill/hook symlinks. Opt out via `[update] check = false`.

### MCP server registration: `~/.claude.json`

`dct init` registers the MCP server at user scope via `claude mcp add --scope user`, which writes:

```json
{
  "mcpServers": {
    "dct": {
      "type": "stdio",
      "command": "dct",
      "args": ["server"]
    }
  }
}
```

No hardcoded paths — `dct server` resolves via PATH, so the package must be installed system-wide (see the [README installation](../README.md#installation)).

**CC MCP scope precedence**: `local > project .mcp.json > user (~/.claude.json) > plugin > connector`. User-scope means dct is available in every Claude Code session regardless of CWD, but a project can still override with its own `.mcp.json`.

To re-register (e.g. after moving the dct install):

```bash
claude mcp remove dct --scope user
claude mcp add dct --scope user -- dct server
```

### Project auto-detection

Each Claude Code session spawns its own `dct server` subprocess, inheriting the session's working directory. MCP tools call `os.getcwd()` + `get_project_by_path()`, so zero-argument calls like `mcp__dct__add_changelog(category="fixed", entry="…")` auto-resolve the project from where you opened Claude Code. Pass `project=<slug>` explicitly to override.

Project paths are stored canonically (`os.path.realpath`), and detection canonicalizes both sides before matching, so CWD detection works under symlinked roots (e.g. `/tmp` → `/private/tmp`) instead of silently failing.

## Project registration & version detection

Two layers, one entry point:

- **`dct init`** — global, one-time bootstrap (MCP registration, skill/hook
  symlinks, consent-gated hook wiring). Idempotent, safe to re-run.
- **`dct project add <slug> <path>`** — per-project registration; inserts a
  projects row and auto-detects `current_version` when `--version` is absent.
- **`dct project rename <slug> [--slug X] [--name Y] [--path P]`** — in-place
  rename; `project_id` and all FKs (items, changelog, checkpoints, handoffs)
  are preserved. Slug collisions with live projects are rejected.
- **`dct project trailers <slug> [--co-authored-by on|off] [--session-link on|off]`**
  — the project's commit-trailer preferences, asked ONCE at its first
  configuration and stored with the project; /commit applies them without
  another question. Without a flag it prints the stored answers; a flag left
  out keeps that answer. Each is on, off or "not answered" (NULL).
- **`dct project version-sync-script <slug> [--kind FILE]`** — writes the
  project's `scripts/dct-version-sync.sh` from the generic template shipped
  in the package (`dct/assets/dct-version-sync.sh`, every block commented)
  with the block of its version file switched on (default: the detected
  one). Never overwrites an existing script (`dct/core/version_sync.py`).

The `/dct-init` skill orchestrates both layers interactively (probes bootstrap
state, permissions allowlist, project-local conflicts, version-sync scripts,
then registers the CWD project) — it never re-implements the CLI, only drives it.

Version detection order (first hit wins): `VERSION` file →
`pyproject.toml` `[project].version` → `package.json` → `Cargo.toml` →
`src-tauri/tauri.conf.json`. Malformed files are skipped silently so
registration never fails on unrelated parse errors. No match: a tty gets an
interactive prompt (`0.0.1 / 0.1.0 / 1.0.0` shortcuts, default `0.1.0`);
non-tty registers `current_version=NULL` with a warning to run
`dct project set-version <slug> <v>` later (a NULL version would otherwise
crash `/release` much later — the detect-or-ask flow closes that trap).
Helper: `dct/core/version.py::detect_version`; tests:
`tests/test_version.py::TestDetectVersion`.

## Workflow layer (`dct/workflows`)

`dct/workflows/session.py` hosts the G1 composed ops (#413):
`get_context_snapshot`, `pickup_work`, `handoff_work` — ONE implementation
consumed by the MCP tools, the CLI (`dct pickup` / `dct handoff create`) and
the packaged /pickup + /handoff skills. `dct/services/` stays reserved for
future shared application ops.

Conventions the layer is built on:

- **Conn-level privates in core.** Public core functions keep their
  `fn(engine, ...)` signatures; each extracted a `_fn(conn, ...)` private
  (e.g. `_create_handoff_conn`, `_set_current_sprint_items_conn`,
  `_consume_handoff_conn`, the `_query_*` readers). Composed ops open ONE
  `engine.begin()` and call the privates, so every op is a single
  transaction: `handoff_work` writes sprint membership FIRST and the handoff
  second — a handoff validation failure rolls the sprint write back.
  Validation lives inside the conn privates so no caller can bypass it.
- **Project-wide gates.** Snapshots include pending gate checkpoints of every
  live nonterminal project item, including items outside active sprints.
  `in_current_sprint` reports membership; gate counts use the same scope.
- **Structured data only.** Snapshots carry facts (sprint state, handoffs,
  gates, decisions, recent changes) — never interpretation like
  `recommended_action`; that belongs to the consuming skill/assistant. A
  future interpretive layer arrives additively (`annotate_snapshot`), never
  as a base-shape change.
- **Multi-sprint semantics.** The single-active-sprint cap is lifted:
  several sprints may be active; the "current" one is the PRIMARY (most
  recently started, id tie-break). Ops needing one unambiguous target
  (`handoff_work`) take an explicit `sprint` selector and raise a conflict
  error listing candidates instead of guessing.
- **`review_sprint` is preserved.** The legacy dashboard keeps its exact
  shape (web + tests depend on it) and shares the same conn-level readers,
  so the queries stay single-sourced; deprecation is a later-group decision.

## PostgreSQL setup

SQLite (the default) needs zero setup. To use PostgreSQL instead:

```bash
uv pip install -e '.[pg]' --python .venv/bin/python   # install the psycopg extra
createdb dct                                           # create the database
dct init                                               # choose "postgresql", enter the URL
```
