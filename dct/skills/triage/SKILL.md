---
name: triage
description: >-
  [dct] Run a review's findings through the dct triage ledger: record the
  consolidated findings, build and publish the triage page from the ledger,
  take an agent's prefill as proposals, and close the triage with the owner's
  answers. Use when: "record the findings", "build/publish the triage page",
  "Codex filled in the proposals", "redeploy the triage", the owner pastes the
  page's decision text, or "accept the proposals as final" — in any language.
---

# /triage — Review findings through the ledger

The ledger is the single source of a triage: findings are recorded right
after the review is consolidated, every version of the page is DERIVED from
`export_triage` by `dct triage page`, an agent's prefill goes in through
`suggest_findings`, the owner's answers through `decide_findings_from_text` /
`accept_suggestions`, and the closed page is derived from `close_triage`.
Nobody edits a page file by hand, and nobody hand-writes its `CARDS`,
`SUGGESTED` or `CLOSED`.

## Deferred-tool load (do this first)

The triage tools are deferred — load the ones the mode needs with ONE
ToolSearch call, e.g. for every mode:

```
ToolSearch("select:mcp__dct__open_triage,mcp__dct__record_findings,mcp__dct__export_triage,mcp__dct__update_triage,mcp__dct__suggest_findings,mcp__dct__decide_findings_from_text,mcp__dct__accept_suggestions,mcp__dct__close_triage,mcp__dct__list_findings,mcp__dct__promote_finding_to_item")
```

A tool missing from the server means the server is older than this skill:
tell the user to restart or upgrade it; never imitate the tool by hand.

## Choose the mode

One mode per request, chosen from the user's words in any language:

| The user says | Mode |
|---|---|
| "record the findings", "open the triage" (after a consolidated review) | **open**, then **page** |
| "build the page", "publish the triage page", "rebuild it" | **page** |
| "Codex filled in the proposals", "redeploy the triage", an agent's answer file | **prefill** |
| the page's generated decision text pasted into the chat, "accept the proposals as final" | **close** |

A word the user merely quotes or mentions is not a command. When the cycle
is unclear (several open cycles fit), ask which one — one option per cycle
code.

**Addressing.** Every MCP triage write takes the cycle as `cycle_id` (the
global id from `open_triage`) OR `cycle` (its code) plus an explicit
`project` — a code needs the project, it is never read as an id, and the MCP
tools never auto-detect it from the working directory. The CLI resolves a
`<code>` from the working directory unless `--project` is given; `--cycle-id
N` needs no project.

**Inspect first (resume rule).** Every mode starts by reading the cycle:
`export_triage(cycle=<code>, project=<slug>)` (or `cycle_id`). What it shows
decides what is still to do — a mode never repeats a write that already
succeeded:

- No such cycle → only **open** may create it.
- An open cycle without cards (a record that failed) → reuse it: record the
  findings into it; never open a second cycle for the same review.
- An open cycle WITH cards → never record them again (a re-recorded card is a
  NEW finding, a duplicate).
- A closed cycle (`closed` is not null) → never decide, accept or close it
  again; only the page build, the publication and the closure note are
  retried. Report it as "ledger closed; publication pending" until they are
  done.

## Mode: open

The coordinator has consolidated the reviews (rubric §3: findings merged by
fingerprint, class derived) and every review of the diff has finished — the
review → triage → fix gate below.

1. `mcp__dct__open_triage(code, topic, created_by, project, scope,
   base_commit)` — `code` a short handle (e.g. `rev-877-r2`), unique among
   the project's live cycles; `created_by` the client's truthful
   self-identification and the UTC time (e.g. `Claude Code @
   2026-10-14T14:30Z`); `scope` what was reviewed (diff range, files);
   `base_commit` the cycle's fixed base commit. REMEMBER the returned
   `cycle_id`.
2. **Purpose and use check** before recording (the class is a recommendation
   and the rubric's threat model is an input to it, not a constant): for every
   card say in its context who supplies the trigger input and whether the
   surface lies within the tool's purpose and trust model — an internal tool
   whose inputs the owner's own process writes, or a surface reached only
   through an artefact the owner authors (a private template), is not
   attacker-controlled. A card reachable only outside that model keeps its
   S but gets O ≤ 1, is not `security`, and gets a class override
   (`cls` backlog or note, `clsWhy` naming the model) instead of fix; its
   context keeps the reviewer's original O and type and the reason for the
   change (rubric §3: a changed field keeps its source value visible). A card
   whose invariant was already fixed in this item and that only adds a new
   trigger of an approximation which cannot be completed case by case (a
   hand-written lexer, parser or sanitizer) is recorded as `cls` decide with
   `clsWhy` "class recurrence: structural change or backlog" — never another
   fix round; the summary says so.
3. `mcp__dct__record_findings(findings=<CARDS>, cycle_id=<id>)` — ONE call
   with the whole array (atomic: one invalid finding = nothing written). Each
   finding carries `src` — the reviewer(s) as free display text (`codex`,
   `panel:security + codex`) — AND `reviewers`, the list of canonical
   reviewer tokens (lowercase, `^[a-z0-9][a-z0-9:._-]*$`, no duplicates, e.g.
   `["codex", "panel:security"]`). The coordinator knows who reported what:
   always pass `reviewers`; never derive them by guessing from `src`.
4. The ack's `recorded[]` lists `{seq, finding_id, first_seen_id}`: every
   finding with a non-null `first_seen_id` is a RECURRING fingerprint —
   name it in the summary (K-n and the finding it recurs from).
5. Continue with **page** in the same turn.

## Mode: page

1. The output path: when the project has `.context/artifacts/`, use
   `.context/artifacts/triage-<safe code>/<safe code>.html`; otherwise the
   session scratchpad, `<scratchpad>/triage-<safe code>/<safe code>.html`.
   `<safe code>` is the cycle code with every character outside
   `[A-Za-z0-9._-]` replaced by `-`. Create the directory first (`mkdir -p`)
   — the CLI refuses a missing parent directory.
2. Build from the ledger:
   `dct triage page <code> --project <slug> --out <path>` (add `--template
   <path>` when `[triage] template` is not set in `dct.toml`; dct ships no
   template — a refusal names both ways, pass it on to the user). The CLI
   is read-only on the database and prints `<out>: <n> cards, closed=<yes|no>,
   suggested=<by>, digest=<hex>` (or `suggested=none`) — the digest of the
   proposals exactly as this build put them on the page. A rebuild of THE
   SAME cycle onto its own path adds
   `--force`; never `--force` onto any other path — the published or closed
   page of another cycle is never overwritten.
3. When the host refuses the CLI call, give the user the exact command to run
   (in Claude Code with the `!` prefix: `! dct triage page …`) and continue
   from its output.
4. Publish the file with the Artifact tool (`file_path` = that path; the
   first publish adds an icon such as `checklist`). The description is
   written in the language of the conversation, its first word included
   ("Open — …" in English, "Otwarty — …" in Polish), and says the page
   collects the owner's answers. A republish uses the
   same `file_path`, so the URL stays the same; the page's storage key comes
   from the cycle id, so a rebuild keeps every viewer's draft.
5. After the FIRST publish write the URL back:
   `mcp__dct__update_triage(artifact_url=<url>, cycle_id=<id>)` (open cycles
   only).
6. Host without the Artifact tool (or without a scratchpad): give the user the
   local page path and the exact command that built it; publication happens
   only where the host supports it.
7. The chat carries the status, the link and the count per class
   (`mcp__dct__list_findings(project=<slug>, cycle_id=<id>, limit=0)`, column
   `class`) — never a second set of cards.

## Mode: prefill

An agent (Codex, a consultant) proposed answers for an OPEN cycle.

1. Read its proposals: a file it left or its answer in the chat, in the
   page's SUGGESTED shape `{by, date, answers: {"K-n": {dec, opt, note}}}`
   (an older flat map `{"K-n": {dec, opt, note}}` is the `answers` alone). An
   agent that edited a page file instead: read its `SUGGESTED` constant from
   that file and NEVER publish the edited file — it is rebuilt from the
   ledger below.
   Which transport the agent can use depends on how it runs: an agent in an
   INTERACTIVE session with the dct MCP server (the owner's Codex session, a
   Claude Code session) writes `suggest_findings` itself — then step 2 is
   already done (check `export_triage`: `suggested.by` is the agent and the
   proposals are there; never write them a second time). An agent run
   NON-INTERACTIVELY (`codex exec`, a background subagent) cannot call MCP
   tools (its approval policy refuses them): ask it to write the proposals
   as a file in the SUGGESTED shape (e.g. `suggested.json` in the session
   scratchpad, cards taken from the published page's `CARDS` constant) and
   do step 2 yourself with `by` = that agent's name. The ledger records the
   proposer either way; it never records who transported the file.
2. `mcp__dct__suggest_findings(by=<SUGGESTED.by>,
   suggestions=<SUGGESTED.answers>, cycle_id=<id>)` — the `answers` object,
   never the whole `{by, date, answers}` envelope. Proposals never touch a
   decision.
3. Rebuild onto the same path (`dct triage page <code> --project <slug>
   --out <path> --force`) and republish to the same URL with a description
   saying, in the language of the conversation, that the page is open and
   carries proposals by <by>, not decisions ("Open — proposals by <by>, not
   decisions"). **Record the digest** the build printed (`digest=<hex>`):
   it identifies the proposals the owner sees on the published page. Name
   it in the report (`SUGGESTED.digest <hex>`) so a later session finds it;
   the published page also carries it in its `SUGGESTED` constant. A later
   rebuild that changes the proposals publishes a new digest — only the
   digest of the page actually published counts.
4. Optional check: render the page headless, or `node --check` its script.
5. Report the link, how many cards got a proposal, every proposal whose
   variant differs from the card's recommendation, and that the owner's
   answers still come back as the page's generated text pasted into the chat
   (or his explicit acceptance of the proposals).

## Mode: close

1. **The owner pasted the decision text** →
   `mcp__dct__decide_findings_from_text(text=<the pasted text, verbatim>,
   decided_by="owner", decision_source="chat <YYYY-MM-DD>", cycle_id=<id>)`.
   The text is parsed before anything is written; a parse error (it names
   the line number and the key) writes nothing — report it verbatim and ask
   the owner for the corrected line; never repair his text yourself. A card
   the page left "— (brak decyzji)" with no note is NOT addressed: it keeps
   its stored state and comes back in the ack's `skipped` — name every
   skipped K-n in the report (they still need the owner's answer). A
   "Punkt wyjścia" line in the text is stored with the decisions as their
   text origin: it shows the form was seeded from proposals, not who wrote
   each note.
2. **The owner accepts the proposals as final** →
   `mcp__dct__accept_suggestions(decided_by="owner",
   decision_source="chat <YYYY-MM-DD>", expected_by=<the SUGGESTED.by of the
   page he saw>, expected_digest=<the digest recorded when that page was
   built and published>, cycle_id=<id>)`. The digest is the one saved in
   **prefill** (the `digest=` line of the `dct triage page` build that was
   published, or the `SUGGESTED.digest` of that local file exactly as
   published in this session) — NEVER a fresh one from `export_triage` or a
   new build, and never a read of the page's current URL as the only proof:
   both can carry proposals he has not seen. No saved digest → do not
   accept: rebuild, republish and let him look again. It refuses when a proposal's
   author differs from `expected_by` or the digest differs (cards or
   proposals changed after he saw them, even by the same author — rebuild,
   republish, record the new digest and let him look again) and when an
   addressed finding already carries a decision; `overwrite=true` only when
   the owner said that his proposals replace earlier decisions. Each
   accepted note is prefixed with its author's name, so a consultant's
   proposal is never presented as the owner's words. `without_suggestion`
   lists the cards it left untouched —
   they need the owner's own text. When he accepts the proposals AND changes
   some cards, accept first, then decide his text: his text wins.
   CLI twins, for a host without the MCP tools (CLI texts are English; the
   parser reads the page's own labels): `dct triage decide <code> --project
   <slug> --from-text <file> --decided-by owner --source "chat <YYYY-MM-DD>"`
   (`--from-text -` reads stdin), `dct triage accept <code> --project <slug>
   --decided-by owner --source "chat <YYYY-MM-DD>" --expected-by <by>
   --expected-digest <the saved digest>`, and
   for step 4 `dct triage close <code> --project <slug> --source "chat
   <YYYY-MM-DD>" --note "<paragraph>"` (`--note` repeatable).
3. **Partial answers.** Only the addressed cards change; the ack's
   `undecided` is the real state after the write. While it is not empty, do
   not close: report the outstanding K-n (the skipped ones included) and
   that the next paste continues (an open page does not show partial
   decisions — they live in the ledger).
4. **Close.** Close only after a real owner step in this mode — his pasted
   text recorded or his acceptance recorded — AND when `undecided` is empty.
   Never close on an agent's proposals alone, and never close a cycle no
   owner answer reached. A `discuss` decision counts as decided for the
   ledger, but the card stays open in substance: the report names it as
   still to be discussed. A closed triage records decisions; it does not
   mean any fix is done.
   `mcp__dct__close_triage(source="chat <YYYY-MM-DD>", notes=[…],
   cycle_id=<id>)` — the notes say what was executed, what the answers do
   NOT approve (a commit, a push, a release) and, after an acceptance, the
   owner's words of acceptance quoted verbatim plus one sentence that the
   per-card notes are the accepted proposal text, not his words. When the
   pasted text carried a "Punkt wyjścia" line, it is stored with the decision
   and comes back as `CLOSED.answers[K-n].origin`; a note may quote it too.
5. Rebuild onto the same path with `--force` and republish to the same URL
   with a description starting with the word "Closed" and the date, in the
   language of the conversation ("Closed <YYYY-MM-DD>" in English,
   "Zamknięty <YYYY-MM-DD>" in Polish).
6. **Backlog → dct.** `mcp__dct__list_findings(project=<slug>,
   cycle_id=<the closing cycle's id>, decision="backlog", limit=0)` — only
   THIS cycle's backlog (never the project's whole backlog: earlier cycles
   were handled at their own close), and of those only the findings whose
   `item_id` is still empty (an already linked one is skipped).
   When there are any, ONE `AskUserQuestion`: "Create dct items for the
   backlog findings of <code>?" with the options "Promote to dct"
   (recommended) — its preview: the screen, one line per finding (`K-n ·
   title`, under 60 characters) — and "Not now". On "Promote to dct":
   `mcp__dct__promote_finding_to_item(finding_id=<id>, project=<slug>)` per
   finding. Neither deciding nor closing creates an item by itself; note and
   reject never create one.
7. **Closure note.** When the review belongs to a tracked item (the item the
   reviewed work was done for), `mcp__dct__add_note(item_id, note="Triage
   <code> closed <YYYY-MM-DD>: <n> fix, <n> verify, <n> backlog, <n> reject,
   <n> note, <n> discuss — <page url>")`. No item known → say so; never pick
   one by guess.
8. **Report**: the link; which decisions approve fixes (each fix / verify K-n
   with its variant); for every open fix / verify finding one explicit
   follow-up sentence — who does what next (the fix to implement, or the
   check to run and its reopen condition per the rubric; an unreproduced
   verify goes to dct tagged `unreproduced`), since closing the triage
   finished none of them; every `discuss` card as still open; the skipped
   cards, if any; the items promoted; and, in plain words, that commit, push
   and release are NOT approved by these answers.

## Rules

- **Language.** A quoted question, option, screen or report fixes what it
  says, not its wording: write it in the language of the conversation. Keep
  unchanged: identifiers, commands, paths, file contents, commit messages,
  changelog entries, short titles and anything shown verbatim.
- **The screen travels inside the dialog.** What the owner must read before
  answering is the preview of the option it belongs to, at most 15 lines as
  the dialog shows them. The dialog wraps long lines itself, about 60
  characters wide: a paragraph is one line, never broken by hand (a second
  break leaves single words on a line), and a list or table line stays under
  60 characters. Message text written before a dialog is never the screen: it
  can land in the thinking block, which the owner sees only as a one-line
  summary, so no question or option says "shown above". A longer screen is
  split into pages of at most 15 lines: the option's preview shows page 1, and
  one more single-select question, "Details", has one option per page (2 to
  4), each with that page as its preview; its answer is not a decision.
- **Every user decision is an `AskUserQuestion` call.** No plain-text prompts.
- **Consent.** Decisions come only from the owner's own text. An agent's
  proposals never become decisions without the owner's acceptance. Note and
  reject never create items. `decided_by` / `decision_source` are an audit
  record, never filled for a decision the owner did not make.
- **The review → triage → fix gate.** A review stays read-only; the findings
  are recorded only after every parallel review of the diff has finished; no
  fix starts before the owner's answers approve it — and those answers never
  approve a commit, a push or a release.
- **The page is derived, never edited.** Every page version comes from
  `export_triage` through `dct triage page`; never rebuild a page from
  anything else, never hand-edit its constants, never publish a file another
  agent edited. A published or closed page file is overwritten only by a
  rebuild of its own cycle from the ledger (`--force` onto its own path).
- **Resume, never repeat.** Inspect the cycle first (above). A failed step is
  retried from the ledger's state: a closed cycle is never decided or closed
  again, and an open cycle with cards is never recorded again.
- Errors from a triage tool are reported verbatim; nothing is retried blindly
  and nothing is "fixed" around a refusal.
