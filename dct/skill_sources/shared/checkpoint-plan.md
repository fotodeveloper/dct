When the update starts work on an item (status → `in_progress`: "start
working on #N", "wip"), look at its checkpoints and its description in the
`get_item` result. An item that already has checkpoints keeps its plan: name
the next pending one and add nothing.

When work starts on a non-trivial feature or improvement — or when the user
asks to break the work down — propose a plan of at least 3 checkpoints:
concrete, actionable steps, as many as the work needs (3 is a minimum, never
a cap), written at once with `mcp__dct__add_checkpoint(item_id, text)`
(remember each id) and listed under the acknowledgement. The user adjusts it
in plain words: `mcp__dct__reject_checkpoint(id)` drops a step the user
removes (ledger-kept), `add_checkpoint` adds one.

A small bug or a one-line fix starts with no checkpoints: name the next step
in the acknowledgement instead of writing an artificial plan. A complex issue
or todo may get checkpoints when its work has several real steps, with no
fixed minimum taken from its type.

