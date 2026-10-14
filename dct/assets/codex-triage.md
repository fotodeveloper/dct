---
name: triage
description: "[dct] Run review findings through the dct triage ledger: record, build the page, prefill, close."
---
<!-- dct-managed-codex-skill -->

# Triage review findings

Discover the connected DCT MCP triage tools and use their separate top-level
fields. The ledger is the single source: every page version is derived from
`export_triage` by `dct triage page`; never hand-edit a page or its `CARDS`,
`SUGGESTED` or `CLOSED` constants, and never publish a page file another agent
edited. Choose one mode from the user's words: record the consolidated findings
(open, then page), build or publish the page (page), an agent's proposals
(prefill), or the owner's pasted decision text or acceptance of the proposals
(close). MCP writes take `cycle_id`, or `cycle` (the code) with an explicit
`project`; the CLI resolves a code from CWD unless `--project` is given.

Inspect first: `export_triage` shows the cycle's state. Reuse an open cycle
whose record failed; never record cards into a cycle that already has them (a
re-recorded card is a new finding); never decide, accept or close a closed
cycle again — retry only the page build, publication and closure note, and
report "ledger closed; publication pending" until they are done.

Open: only after every parallel review of the diff has finished. Call
`open_triage(code, topic, created_by, project, scope, base_commit)` with a
truthful client identity and UTC time, then `record_findings(findings=<CARDS>,
cycle_id=<id>)` once with the whole array. Each finding carries `src` (reviewers
as display text) and `reviewers` (lowercase tokens such as `codex`,
`panel:security`; never guessed from `src`). Before recording, check each
card against the tool's purpose and trust model: an input the owner's own
process writes, or a surface reached only through an artefact the owner authors,
is not attacker-controlled — such a card keeps its S, gets O ≤ 1, is not
`security` and gets a class override (`cls` backlog or note with `clsWhy`); a new
trigger of an invariant already fixed in this item, in an approximation that
cannot be completed case by case, is recorded as `cls` decide ("class
recurrence: structural change or backlog"), never another fix round. Name every
finding whose `first_seen_id` is not null as recurring.

Page: create the directory, then run `dct triage page <code> --project <slug>
--out <path>` (`--template <path>` unless `[triage] template` is configured).
Path: `.context/artifacts/triage-<safe code>/<safe code>.html` when the project
has `.context/artifacts/`, else a session temporary directory; the safe code
replaces characters outside `[A-Za-z0-9._-]` with `-`. Add `--force` only to
rebuild the same cycle onto its own path. Publish only where the host supports
it; otherwise return the local path and the exact command. After the first
publication call `update_triage(artifact_url=<url>, cycle_id=<id>)`. Report the
link and counts per class (`list_findings`), never a second set of cards.

Page descriptions are written in the language of the conversation, their
first word included. Prefill: pass the agent's `SUGGESTED.answers` (not the
envelope) as `suggest_findings(by=<SUGGESTED.by>, suggestions=<answers>,
cycle_id=<id>)`, rebuild with `--force`, republish to the same location.
Record the `digest=<hex>` the build printed and name it in the report: it
identifies the proposals on the published page (its `SUGGESTED.digest`).
Proposals are never decisions.

Close: pasted text → `decide_findings_from_text(text=<verbatim>,
decided_by="owner", decision_source="chat <YYYY-MM-DD>", cycle_id=<id>)`; a
parse error writes nothing — report it and ask the owner for a corrected line.
A card left "— (brak decyzji)" without a note is not addressed: it stays
unchanged and is listed in the ack's `skipped` — report those cards. A
"Punkt wyjścia" line is stored as the decisions' text origin (the form was
seeded, not the authorship of each note).
Acceptance → `accept_suggestions(decided_by="owner", decision_source=...,
expected_by=<the proposer the owner saw>, expected_digest=<the digest recorded
for the published page>, cycle_id=<id>)` (CLI: `dct triage accept <code>
--project <slug> --expected-by <by> --expected-digest <hex> --decided-by owner
--source ...`). Pass exactly the saved digest of the page the owner saw (or the
`SUGGESTED.digest` of that published page), never a fresh one from
`export_triage` or a new build; without it, rebuild, republish and let him look
again. `overwrite=true` only on the owner's explicit word; cards without a
proposal need his own text, and his text is decided after the acceptance.
While `undecided` is not empty, report the outstanding cards and do not close.
Close only after the owner's recorded text or acceptance and with `undecided`
empty, never on proposals alone: `close_triage(source=..., notes=[...], cycle_id=<id>)` with notes
stating what the answers do not approve (commit, push, release), rebuild with
`--force`, republish with a description starting with "Closed" and the date in
the language of the conversation. A `discuss` card stays open in substance; a
closed triage records decisions, it does not mean any fix is done. List this
cycle's backlog with `list_findings(project=<slug>, cycle_id=<closing cycle>,
decision="backlog", limit=0)` and skip every finding that already has an
`item_id`; promote each remaining one with `promote_finding_to_item` only after
the owner confirms in a question, showing the list inside it. Add a closure
note to the reviewed item when one is known. The report names, for every open
fix / verify finding, one explicit follow-up (the fix to implement, or the
check to run and its reopen condition), plus the skipped and discuss cards.

Consent: decisions come only from the owner's own text; an agent's proposals
never become decisions without the owner's acceptance; note and reject never
create items. No answer approves a commit, push or release. Use the available
question UI (prefer `request_user_input_async`) for every owner decision, with
the content he must read inside the question.

Language: A quoted question, option, screen or report fixes what it says, not
its wording: write it in the language of the conversation. Keep unchanged:
identifiers, commands, paths, file contents, commit messages, changelog
entries, short titles and anything shown verbatim.
