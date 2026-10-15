# DCT reference — read on demand

This client-neutral reference is installed at `paths.config_dir() / "dct-reference.md"`, resolved by the integration at call time. Read the resolved file named by this client's bounded instruction block when needed; do not paste the full reference into global instructions. A supported client integration installs workflows for that host; a generic MCP connection does not establish hooks, publishers or native tasks.

## Workflow inventory

{{inventory}}

Explicit-only workflows require the user's named request. The remaining descriptions select a use case; they never expand consent to commit, push, release, deploy, publication or instruction edits. Prefer the installed native workflow, using its complete self-contained contract.

## Tool and identity conventions

{{shared.discovery}}

{{shared.identity}}

Creation/fetch returns integer primary keys; retain them as handles. Write acknowledgements can be minimal scalar dictionaries, not a full record. Fetch full context deliberately (`get_item`, `get_handoff`, `get_plan`). Plan MCP identifiers are integer primary keys; CLI plan/section aliases are explicit ULIDs guarded by project, never polymorphic ids.

Structured list views return `{cols, rows}`: map fields using the returned column names. `list_items` brief rows have no descriptions or source_file; use `view="full"` and `format="dict"` for full rows only when needed. `list_handoffs` gives 200-character prompt previews; `get_handoff` supplies the complete prompt/context summary. Empty project generally detects the server CWD, not an explicit cross-project request; the agent's actual checkout may differ, so pass the selected slug when relevant.

{{shared.read-schema}}

## Workflow use without duplication

Each installed native skill contains its complete workflow and refusal/recovery contract. Invoke the skill for the selected workflow through the current host's supported discovery. No installed documentation path or client-specific slash syntax is required. Release effects require the local `dct release` executor; missing CLI is a readable refusal, never ledger-only emulation.

{{shared.language}}

{{shared.consent}}

{{shared.type-cues}}

## Status questions versus pickup commands

{{shared.pickup-mode}}

## Gate and correction rules

The packaged `add_checkpoint` description defines required gates and explicit exceptions. Evaluate a gate by its recorded condition and required evidence, not a client-specific convention. A person's act or approval needs that person's confirmation; unchanged explicit confirmation is reused. A gate is rejected only on the user's decision (full gate policy: `add_checkpoint`), never independently to close an item. A stale or missing required check leaves it pending.

Cheap explicit tracking writes acknowledge their saved ids and a correction hint; an idea merely mentioned does not authorize a write or implementation. Complete checkpoints when proven, reject an obsolete task checkpoint on the scoped owner decision, and preserve ledger history.

## Checkpoints when work starts

{{shared.creation-checkpoints}}

{{shared.checkpoint-plan}}

## Stored commit preferences

{{shared.trailer-preferences}}
