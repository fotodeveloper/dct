A pending **gate** checkpoint (`kind` other than `task` / `cc-task` —
`dogfood`, `security`, `approval`, `review`, or an unknown kind) blocks the
item: `resolve_item` refuses to close it. Never use `update_item` with a
terminal status to bypass it; that guard refuses too. There is no "resolve anyway" path. Within the close the
user asked for, take each pending gate by its recorded condition — its
text — as the packaged gate policy (the `add_checkpoint` tool description)
does:

- **Evidence sufficient** — a technical condition (a test run, a check, a
  review report) that is really met, with the evidence its text requires
  and that evidence current for the state being closed (produced after the
  last change, by whoever the text names): call
  `mcp__dct__complete_checkpoint(id)` without asking for a separate "done"
  — the request to close the item covers it. The acknowledgement names the
  gate and its evidence.
- **Evidence missing or stale** — not produced, produced for an older
  state, or nothing you can point to: the gate stays pending and goes into
  the stop below. Your own test never replaces an independent review the
  text names.
- **A person's act or acceptance** — only that person's confirmation
  completes a gate whose condition is a person's act or acceptance:
  dogfood (the user's own check in a fresh session), approval (a
  stakeholder's sign-off), or any gate whose text names a person who must
  perform or accept it. Finished implementation, green tests or the
  instruction to close the item do not replace it; it goes into the stop
  below. The kind alone does not decide who performs a gate: a `review` or
  `security` gate may need a named reviewer, or be met by a review report
  already in hand.
- **Consent already given** — a confirmation of this gate that its person
  already gave explicitly is not asked again: complete the gate on it (for
  instance "I tried it in a fresh session, it works", said earlier in this
  conversation).

A gate is rejected only on the user's decision (full gate policy: `add_checkpoint`),
using `mcp__dct__reject_checkpoint(id)` — never on your own initiative, never to get the item closed.

STOP before calling `resolve_item` while any gate stays pending. Tell the
user in plain words — no tool names, no `kind` identifiers — what is still
to be checked, one line per gate, its text and number:

```
#42 can't be closed yet — it waits on a check: "try the new command in a fresh session" (#17). Once that check is really done, say "done"; to drop it on purpose, say "skip it". Then I close #42.
```

That answer is the instruction: "done" → `mcp__dct__complete_checkpoint(id)`,
"skip it" → the user's own decision to skip, `mcp__dct__reject_checkpoint(id)`
(ledger-preserved); then `mcp__dct__resolve_item(item_id, status, resolution)`
in the same turn, with no further question, once no gate is left pending.
A "done" or "skip it" covers only the gates named in the stop — with several
gates and an answer that does not say which, ask which. "done" is never a
substitute for the evidence a gate requires: when the gate's text names a
sign-off, a review or a fresh-session test that has not happened, the gate
stays pending and the item stays open.

Pending `task` checkpoints do not block: the item is resolved and the
acknowledgement names how many stay open.
