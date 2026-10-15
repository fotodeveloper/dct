# {{host.skill-prefix}}plan-resolve-uncertain — Classify Uncertain Plan Sections

This workflow is explicit-only. Use it when the user asks to classify indexed uncertain sections or accepts that named follow-up after import; a status question alone does not run it.

## Workflow

### Step 1: Project and selected plan

Resolve the explicit project or match the current checkout against registered project paths. Ask only if ambiguous. `list_plans(project=<slug>, status="active")` returns `{cols, rows}` with integer id and ULID. Select the plan the user named; several candidates require a choice. Use `get_plan(plan_id=<integer>)` and `list_plan_sections(plan_id=<integer>, section_type="uncertain", format="structured")` to show every selected section's integer id, ULID, depth and heading. No direct query, full-file rescan or anchor injection.

### Step 2: One strategy decision on the exact table

Show the selected project's plan and full uncertain-section table through the presentation rule below. Offer:
- Resolve using configured rules/command.
- Review one-by-one (manual choice per section).
- Mark these listed sections as prose.
- Skip — leave uncertain.

Inspect the current classification configuration without changing it. Rules need no model. A configured command may invoke an external provider; show that actual provider/known model and the known cost basis, or say that cost is unknown. Never promise Claude/Haiku, invent prices or force a provider. Obtain explicit strategy consent before invoking a configured external command. Disabled or unavailable providers leave sections unresolved; offer the manual strategy only as a new explicit choice.

### Step 3a: Configured batch

Call `resolve_uncertain_sections(plan_id=<integer>, project=<slug>)`.
CLI equivalent: `dct plan resolve-uncertain <plan-ulid> --project <slug> --json`.
MCP accepts integer primary keys only; CLI takes the explicit plan ULID with project guard. Retain `{resolved:[ids], unresolved:[{id,reason}], skipped:[{id,reason}], remaining_uncertain:n}`. The operation snapshots candidates, executes configured rules/command outside a write transaction and conditionally writes only still-live, unchanged, uncertain rows. Invalid plan/project/config refuses before invocation. Disabled/missing/failed command is a non-secret reason, never a made-up result. Preserve successful per-section progress after later failures; a retry targets only remaining uncertain rows.

### Step 3b: Manual per section or the approved prose list

For each listed section the user chose, apply exactly the approved type via
`set_plan_section_type(section_id=<integer>, section_type=<type>, project=<slug>)`.
CLI equivalent: `dct plan set-section-type <section-ulid> <type> --project <slug> --json`.
Types: sprint, phase, task, block, milestone, decision, prose, changelog, adr_section, reference, generic, uncertain. Skip writes nothing. A bulk prose choice uses the same setter once per explicitly listed section; it never invents a new section or applies to rows the owner did not see.

Retain each acknowledgement's section primary key, ULID, resulting type, classification source and changed flag. A manual resolved type survives rescans even after changed headings or automatic inference until explicitly revoked; setting uncertain revokes the override and coherently sets uncertain confidence and review=true. The setter preserves heading, anchors, status and links. Same application is a no-op. Never call `add_plan_section` as an update or direct generic classification update, never mutate checkpoints or provider config.

### Step 4: Report actual results

Report acknowledged ids, remaining uncertain count and reasons for unresolved/skipped rows. On partial failure keep acknowledged ids, show precisely which rows were completed and which remain; do not create replacements or blindly retry an ambiguous write response. Rerunning already-resolved sections makes no new classification write. End with the named `plan-status` progress hint.

## Rules

{{shared.language}}

{{shared.consent}}

{{shared.discovery}}

No provider is silently enabled, and unresolved stays unresolved on unavailable capability. Manual and bulk choices use the same semantic setter; no direct SQL or ad hoc Python fallback.
