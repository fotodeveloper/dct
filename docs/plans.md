# dct — Plans, Roadmaps & Decisions

How dct indexes markdown plans, specs, roadmaps, ADRs, and retros: stable ULID anchors, DB-owned state, the ingest pipeline, and LLM-assisted classification. ← back to the [README](../README.md).

dct extends the flat items model with **hierarchical plans** — plan markdown files stay the source of truth for content, while dct indexes their structure (sections, checkpoints, decisions) and owns their state (done/pending/rejected, resolved/superseded).

## Concepts

| Entity | What it is | Content source | State source |
|---|---|---|---|
| **plan** | Markdown document indexed by dct (plan/spec/roadmap/ADR/retro/backlog) | MD file + DB index | DB |
| **plan_section** | Heading (H1-H6) within a plan, ULID-anchored | MD (heading text) | DB (section_type, status) |
| **plan_checkpoint** | `- [ ]` checkbox under a section | MD (text) | DB (pending/done/rejected) |
| **sprint** | Named lifecycle-tracked unit of work | DB (first-class) | DB |
| **decision** | ADR, sprint amendment, inline plan decision | DB (first-class) | DB |
| **item** | tracked unit (issue/todo/feature/…) — can be promoted from a plan_section or plan_checkpoint | DB | DB |

## ULID anchors

Every indexed element gets a stable Crockford base32 ULID in an HTML comment. Markdown renderers ignore the comment — the file looks clean but is linkable:

```markdown
<!-- dct:plan:01HX9Z4K5M7PQR0001 -->
# v0.18 Traversal Semantics Refresh

## Sprint 1: Hub Redesign
<!-- dct:section:01HX9Z4KA2B3CD0010 parent=01HX9Z4K5M7PQR0001 -->

- [ ] <!-- dct:cp:01HX9Z4KM3N4OP0100 parent=01HX9Z4KA2B3CD0010 --> Write NodeType struct
- [ ] <!-- dct:cp:01HX9Z4KM8QR5S0101 parent=01HX9Z4KA2B3CD0010 --> Add unit tests
```

**Properties**: stable across heading rename, reorder, or copy-paste between plans (the parent anchor detects cross-plan copy).

## State semantics

- **MD file defines WHAT** the checkpoints and sections are.
- **DB owns WHEN** they are done (checkpoint status, section status, sprint assignment, decision lifecycle).
- User edits `- [ ]` → `[x]` in MD **are NOT** read after first import — state is DB-only (use `complete_plan_checkpoint` MCP tool or CLI).
- **First-import gate** (`plans.imported_at IS NULL`) reads `[x]` as `status='done'` once to capture pre-dct history; subsequent rescans ignore MD brackets.

## Discovery patterns

`/dct-import` and the autoscan hook detect these paths (relative to project root):

| Pattern | `kind` |
|---|---|
| `docs/plans/**/*.md`, `**/plans/*.md` | `plan` |
| `docs/specs/**/*.md`, `**/specs/*.md` | `spec` |
| `docs/adr/**/*.md`, `**/adr/*.md` | `adr` |
| `ROADMAP.md`, `*/ROADMAP.md`, `roadmaps/**/*.md` | `roadmap` |
| `TODOS.md`, `*/TODOS.md` | `backlog` |
| `FUTURE.md`, `*/FUTURE.md` | `backlog` (kind-hint=future) |
| `docs/retros/*.md`, `retros/*.md` | `retro` |

**Hardcoded exclusions**: `node_modules/`, `.venv/`, `venv/`, `.git/`, `target/`, `dist/`, `build/`, `.next/`, `coverage/`, `*.min.md` (fixed list — the once-documented `[plans].discovery_include/exclude` overrides never had a code consumer and were removed in the ADR #52 key audit).

## Ingest pipeline

When `/dct-import` or the hook ingests a file:

1. **Parse** (mechanical, code-fence-aware) → sections, checkpoints, existing ULID anchors, warnings.
2. **Ensure ULIDs** — missing plan/section/checkpoint ULIDs are generated + structurally-derived parents filled.
3. **Write-back** (atomic, `os.replace` via `<file>.dct-tmp`) — the MD file is mutated to embed new ULID HTML comments; original text / ordering / code blocks preserved. Idempotent: subsequent runs do not re-inject.
4. **Hash** → `sha256(file)` stored as `plans.source_hash`. Second ingest of an unchanged file is a no-op (hash short-circuit).
5. **Upsert** plans/sections/checkpoints. On first import reads `[x]` as historical done-state. On rescan, MD brackets are ignored (DB owns state).
6. **Classify uncertain sections** (if `auto_resolve_uncertain=true`, default) — invoke `claude --print --model <haiku>` on each `section_type='uncertain'` row and apply the resolved label. LLM failure / timeout / missing `claude` binary → row stays uncertain; never raises.
7. **Sticky resolved**: a row previously resolved (via LLM or manual) is never downgraded back to `uncertain` on a later rescan, even if the current parser output re-infers uncertainty.

## LLM classification — always-on by default

**The default flow runs the LLM for every uncertain section, every ingest.** Three knobs:

| Layer | Default | To disable |
|---|---|---|
| Config `[plans].auto_resolve_uncertain` | `true` | set to `false` in the dct config file (`dct config path` shows where) |
| MCP `ingest_plan_file(auto_resolve_uncertain)` | `True` | pass `auto_resolve_uncertain=false` |
| CLI `dct plan rescan --resolve-uncertain` | flag absent = off; hook adds it | hook always adds it → always on |
| Hook `dct-plan-autoscan.sh` | calls with `--resolve-uncertain` | edit the hook if you want to skip |

**Model**: haiku-4-5 (`claude-haiku-4-5-20251001`) at ~$0.001 per classification. Configurable via `[classification].model` — drop to a larger model if you see accuracy regressions on your plans.

**Backend**: headless `claude --print` CLI subprocess (no Anthropic SDK dependency). Reuses your existing Claude Code credentials + billing.

**Graceful degradation**: if the `claude` binary is not in PATH, the subprocess fails, or the response is not a valid label, the section stays `uncertain` with `needs_llm_review=true`. Run the `/plan-resolve-uncertain` skill later to retry, or override manually.

## Classification rules

The parser first applies regex patterns (high confidence, instant, no cost):

- `sprint` — `^Sprint \d+:?`
- `phase` — `^Phase \d+\b` or `^Phase [A-Z]\b`
- `task` — `^Task \d[\d.-]*`
- `block` — `^Block [A-Z]\d*`
- `milestone` — `^Milestone [MA-Z]\d+`
- `decision` — `^ADR[- ]\d+`
- `changelog` — `^(Added|Changed|Fixed|Removed)$` (as H3 inside a plan)
- `adr_section` — `^(Context|Decision|Consequences|Alternatives)$` (inside an ADR)
- `reference` — `^(Appendix|References?|Open questions?)\b`
- `prose` — numbered spec/plan headings: `^\d+(\.\d+)*\.?\s+\S` (covers `1. Problem`, `4.1 Glossary`, etc.)
- **fallback** → `uncertain` with `needs_llm_review=true` → the LLM classifier picks up the slack.

## Hook-driven passive sync

`dct/hooks/dct-plan-autoscan.sh` is a PostToolUse hook installed by `dct init`. It fires after `Edit`/`Write`/`MultiEdit` on files matching the discovery patterns.

```bash
# ~/.claude/settings.json — wire the hook
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {"type": "command", "command": "bash ~/.claude/hooks/dct-plan-autoscan.sh"}
        ]
      }
    ]
  }
}
```

`dct init` wires this automatically (with your consent) — see [docs/hooks.md](hooks.md).

**Fire-and-forget**: the hook spawns `nohup dct plan rescan <file> --quiet --resolve-uncertain & disown` so the user never waits. The log lands in `$TMPDIR/dct-rescan-<ts>-<pid>.log`.

**Handles two cases**:
- Brand-new plan file without ULIDs → full ingest + ULID injection on first save.
- Already-tracked plan edit → hash compare → re-parse → upsert changed rows.

## Promote section/checkpoint → item or sprint

Plan fragments that deserve their own lifecycle can be promoted:

```
mcp__dct__promote_section_to_item(section_id, item_type, priority?, title?)
mcp__dct__promote_checkpoint_to_item(checkpoint_id, item_type, priority?, title?)
mcp__dct__promote_section_to_sprint(section_id, code, name?, goal?, kind?)
```

**`promote_section_to_sprint`** is the opt-in materialization of a `## Sprint N` plan heading into a first-class `sprints` row with a unique project-scoped `code`, lifecycle, and optional goal. Sections labeled `section_type='sprint'` are **not** auto-materialized — automatic promotion would cause namespace collisions (plan A's "Sprint 1" vs plan B's "Sprint 1" vs DB sprint "v0.2-ship"). The user picks `code` explicitly (e.g. `v0.2-hub`).

The original section/checkpoint stays in the plan; the new item/sprint is back-linked via `converted_to_item_id` or `plan_sections.sprint_id`.
