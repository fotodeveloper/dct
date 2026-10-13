# Assistant fallback workflows

Skills in `dct/skills/` are **the preferred** way to drive dct from Claude Code.
But skill names can be renamed, disabled per-session, or missing in future
versions. Do not couple logic to skill existence — the workflows below
describe what each skill does so an assistant can perform the steps manually
via MCP tools when the named skill is unavailable.

**Language.** A quoted question, option, screen or report fixes what it says,
not its wording: write it in the language of the conversation. Keep unchanged:
identifiers, commands, paths, file contents, commit messages, changelog
entries, short titles and anything shown verbatim.

**The screen travels inside the dialog.** What the owner must read before
answering is the preview of the option it belongs to, at most 15 lines as the
dialog shows them. The dialog wraps long lines itself, about 60 characters
wide: a paragraph is one line, never broken by hand (a second break leaves
single words on a line), and a list or table line stays under 60 characters.
Message text written before a dialog is never the screen: it can land in the
thinking block, which the owner sees only as a one-line summary, so no
question or option says "shown above". A longer screen is split into pages of
at most 15 lines: the option's preview shows page 1, and one more
single-select question, "Details", has one option per page (2 to 4), each with
that page as its preview; its answer is not a decision.

**Write ops return a minimal ack**, not the full record — the created/updated
`id` plus a few post-state scalars. **Remember the `id`** returned by
`create_item` / `add_checkpoint` / `create_handoff` (etc.); you need it for
every follow-up call. Fetch full context deliberately: `get_item(id)` (fields,
notes, checkpoints, progress) or `get_handoff(id)` (untruncated prompt +
context_summary). `list_handoffs` returns 200-char prompt previews — use
`get_handoff(id)` for the full text.

## `/pickup` fallback

On an unambiguous "continue" / "pickup" / "resume" the skill consumes the
single matching handoff without a question and names it in the report
("resume" counting as "continue" is a proposed refinement of the owner's
decision, which named only "continue"); it asks which one only when several
match. A status question ("where were we", "what's next") only reads:
`consume="false"`. A word the user merely quotes or mentions is not a
command. Without the skill:

1. `pickup_work(project="", consume="", consumed_by="<agent> @ <slug> <iso_utc>Z")`
   — AUTO mode: exactly one matching unconsumed handoff is consumed in the
   same transaction and returned as `picked_up` with its FULL prompt
   (`consumed: true`); with several, nothing is consumed and `handoff.latest`
   (full prompt) + `handoff.others` (200-char previews) list them; with none,
   nothing is written. `scope=<tag>` narrows the match to one workstream. To
   only look (an audit, "keep it", "where were we"), call `consume="false"`
   instead — a PURE READ, no `consumed_by` needed.
2. Count what matches: with a scope, `handoff.scope_matched`; without a scope,
   the global `pending_count`. With a scope, offer only the handoffs of that
   scope — never another workstream's (`handoff.others` lists every scope).
   When `others` is truncated, or holds fewer matching rows than the count,
   fetch the complete list first: `list_handoffs(project="")`, keeping only
   the matching scope.
3. A handoff the user must read before deciding travels inside the dialog,
   as the preview of its option (the screen rule above). With no question to
   follow — one handoff consumed on an unambiguous "continue" and a concrete
   next step (step 5) — check that step and start on it in the same turn;
   the report names the handoff (the user saved it at /handoff). With several
   matching handoffs, ask ONLY which one — "Continue #N" per handoff, newest
   first, at most 3, each with that handoff's full prompt as its preview
   (`get_handoff(M)` for the ones `others` carries only as a 200-char
   preview — a read, without consuming it), and "None — start fresh" — not
   the first action yet. A chosen handoff longer than its 15-line preview is
   asked about once more ("Continue handoff #M?", its prompt on "Details"
   pages) before anything is consumed; showing an older handoff in full stays
   a read — nothing is consumed by showing it. Only an unambiguous instruction to
   continue a handoff the user has read in full consumes it:
   `pickup_work(project="", consume="true", handoff_id=N, include="none", consumed_by="<agent> @ <slug> <iso_utc>Z")`
   — `include="none"` skips the snapshot re-read (step 1 already has it).
4. Report which handoff was consumed: `Picked up handoff #<id> (consumed) — "<first sentence of its prompt>"` —
   without a question, this line is where the user learns of it. The consumed
   row stays in the ledger.
5. The next step: when the consumed handoff names a concrete step (an item id
   or a pending checkpoint), check its CURRENT state first — `get_item(X)`,
   its checkpoints, the pending `gates` and `decisions` — and when it is still
   current, start on it with no question: `Starting with #22 — "<next pending
   checkpoint>"`. A stale, contradictory or unclear step (resolved item, done
   checkpoint, a blocking gate, the user asking for something else, none
   named) → ask the first action instead. An old handoff is no new consent: a
   commit, a push, publishing or any destructive step written in it still
   waits for the user's own instruction now.
6. CLI equivalent: `dct pickup --consume auto [--json]` (the default
   `--consume never` is a pure read), `dct pickup --consume always --handoff-id N`
   for the chosen one, after reading it in full (`get_handoff(N)`; the CLI
   prints only the newest in full).

Never auto-pick among several handoffs, and never consume one that was only
previewed; never call `consume_handoff` here — `pickup_work` owns the
consume. If a parallel session wins the race, the AUTO
consume comes back `consumed: false` (proceed with the fresh snapshot); an
explicit `handoff_id` errors "not found or already consumed" — report it,
don't retry in a loop.

## `/handoff` fallback

The skill is a preflight, ONE question — Save / Change / Cancel — with the
draft inside it, then one atomic call. Without the skill:

1. `get_context_snapshot(project="")` — sprint/gates/decisions feed the draft.
2. Draft the handoff with the sprint and scope decisions as lines of the draft — `Sprint: unchanged` and `Scope: none` by default — never as questions of their own.
   Sprints are MULTI-SESSION (ADR #57): the default is an unchanged sprint, the
   normal edge is ADDITIVE (add discovered items / cut scope), and a full
   replace is conscious replanning.
3. Ask ONE question: "Save" / "Change" / "Cancel" — the draft (and the
   context_summary) word for word as the preview of "Save", on "Details"
   pages when longer than 15 lines; the question text and option labels carry
   none of it. "Change" → apply what the user says (prompt text, sprint,
   scope) and ask again with the revised draft in the previews; with several
   active sprints a sprint change also asks which sprint. "Cancel" → nothing
   is written.
4. On "Save": `handoff_work(prompt=..., created_by="<agent> @ <slug> <iso_utc>Z", project="",
   item_ids="__keep__" (default) | "22,21,6" (replace), add_item_ids="22,6",
   remove_item_ids="7", sprint=<id-or-code when several active>, scope=...,
   context_summary=<the approved summary, verbatim, or "">)` —
   one transaction; a failed sprint write leaves no handoff. Every line of the
   saved draft maps to an argument (prompt, context summary, sprint, target
   sprint, scope); the context summary is exactly the text the user saved —
   never shortened or regenerated after "Save". Replace and the
   additive pair are mutually exclusive. **Never `item_ids=""`** — an empty
   replace errors; emptying a sprint is `clear_sprint`'s job. Unknown item ids
   abort everything (strict), removing a TERMINAL member errors (resolved work
   is the sprint's history), and an ambiguous / membership-less `sprint`
   selector errors too.
5. **"Sprint only, no prompt"** (a Change that drops the prompt): `handoff_work`
   requires a prompt, so the `Sprint:` line maps to the existing additive
   tools, one item at a time — an add adds, a cut cuts, nothing else is
   touched. No active sprint: add → `add_item_to_current_sprint(item_id,
   project)` (creates the sprint); a cut has nothing to cut — stop and say so.
   One active sprint: add → `add_item_to_current_sprint(item_id, project)`, cut
   → `remove_item_from_current_sprint(item_id, project)` for an OPEN member
   only (a terminal member is history: stop and say so). Several active
   sprints: the target sprint the user chose — add →
   `assign_item_to_sprint(item_id, sprint_id)`, cut →
   `remove_item_from_sprint(item_id, sprint_id)`; a missing or ambiguous target
   stops readably, never a guess. `set_sprint(project, item_ids=...)` only for
   a replace the user accepted on the draft, with no or one active sprint;
   with several active sprints stop — that replace needs a prompt
   (`handoff_work` with `sprint=`). Never compute a full list for `set_sprint`
   from a read plus the change: an item another session added meanwhile would
   be dropped.
6. CLI equivalent: `dct handoff create --prompt-file - --add-items 22,6 --scope g1 --context-summary-file summary.md`
   (`--remove-items` cuts scope, `--items` replaces; omit all three to keep the
   sprint; `--context-summary "…"` passes a short summary inline — the file
   form keeps a long, multi-line one exactly; only one of the two `-file`
   flags can read stdin).

## Changelog proof: trailers and range (shared by `/commit` and `/release`)

A changelog entry is **proven** for a release when a commit in the release
range names its id in a `Changelog:` trailer. `/commit` writes the trailers,
the release workflow reads them. The rules are implemented ONCE in code —
`dct/workflows/release/proof.py` (trailers, reverts, squash signals) and
`dct/workflows/release/plan.py` (range, provenance, candidates) — and
computed read-only by two commands whose JSON schemas, range rule (K-71),
revert rule (K-68), squash heuristic and its limits (K-69) and provenance
(K-70) are documented in [release-workflow.md](release-workflow.md):

- `dct release plan [-P slug] [--version V | --patch|--minor|--major]
  [--entries <ids> | --no-entries] --json` — the release plan of the
  current checkout; on the CLI path its `snapshot` / `snapshot_digest` is
  what the owner approves (K-80) — `/release` reads it for routing and
  approves the preview's screen instead. No side effects.
- `dct clog candidates [-P slug] --json` — `/commit`'s candidates:
  `confirmed` vs `uncertain`, minus the ids the range already proves, plus
  the staged file list.

The essentials an assistant must know to write and read proof (the code is
authoritative):

- **Trailer format** (written by `/commit`): one line `Changelog: <id>, <id>`
  — ids ascending, comma + space — in the final trailer block, before
  `Co-Authored-By:`. Release commits (`release: vX.Y.Z`) carry none.
- **What counts** (per commit of the range): git's trailer parser
  (`%(trailers:key=Changelog,valueonly,separator=%x2C)`) plus every body line
  matching `^Changelog: [0-9]+(, [0-9]+)*$` in column 0. The strict format is
  RESERVED (K-66): a column-0 line in an example counts too, so quote or
  indent examples; quoted, bulleted and indented lines never count.
- **Range**: from the nearest release tag that is an ancestor of the
  candidate; the plan flags `confirm_required` when that is not the tag of
  `current_version` (show it, the owner confirms) and `approximate` when no
  ancestor tag exists (warn).
- **Reverts**: `/commit` writes reverts with git's standard message
  (`Revert "<subject>"` + `This reverts commit <sha>.`) — that is what the
  workflow detects; the reverted commit's ids lose their proof.
- **Squash merges lose trailers**: a title-only squash drops the bodies, a
  local `git merge --squash` indents them. The plan proposes ONE collective
  attestation per source branch (`attestation_groups`) — a proposal the
  owner confirms, never an automatic assignment. Recommended merge modes:
  [release-workflow.md](release-workflow.md#merge-policy).
- **Uncertain entries** (`branch_source='server'` — the MCP server's own
  checkout — or no provenance) and every entry on a **detached HEAD** are
  never attached as found: match them against the staged diff, ask, and
  confirm them with `update_changelog(entry_id, branch=<current>,
  worktree=<toplevel>)` (detached: `worktree` only) first. Never fall into an
  unfiltered list of every branch. The commit hook's count is only a gate
  (are there entries at all?), never proof that an entry belongs to the
  commit — the trailer set is always chosen from the final staged diff.
- **Attestation** — the only way to add proof after the fact — is the empty
  commit `chore: attest changelog entries` created by `dct release attest
  --entries <ids> --confirm` once the owner confirms the changes are in the
  candidate (in `/release`: the owner's "Release with all N" or "Change
  something" answer, and `apply --consent` makes the same commit).

## `/release` fallback

The release runs in the local **dct CLI** (`dct release …`, the effects
layer) as ONE workflow from the first plan to the pushed tag; the assistant
runs the dialog. **Without the CLI** (an MCP-only client) say plainly that
a release is not possible from here: the ledger tools (`release_changelog`,
`get_release`, `advance_release`) record a release but cannot verify the
changelog file, the version files, the commit, the tag or the push. A
project with its OWN release tool integrates as described in
[release-workflow.md](release-workflow.md#integrating-a-projects-own-release-tool).

The assistant never edits the changelog file FOR THE RELEASE, never runs the
version script itself, never stages, commits, tags or pushes FOR THE RELEASE,
never calls `advance_release` and does not use the `clog release` alias —
the workflow (`apply --consent`, `resume`) makes the commit of the
uncommitted changes, the empty commit that marks waiting entries as done,
the release commit and the tag through the repository's hooks, pushes them
to the target the screen showed, verifies and records every effect. An
entry added or corrected during the dialog never takes a version: the stamp
of `apply` is the only writer of versions. Run every command in the CURRENT
checkout.

A typical release asks ONE AskUserQuestion with two questions. Before it
the owner sees one line, "Checking proj-a…", and nothing else. The screen
travels inside that dialog — the preview of the "Release" option (the screen
rule above) — never as message text before it. Steps 0-5 are the typical path; **Rare cases** (after
step 5) and the Resume path are read only when a step points there, the
Legacy path only when the owner asks for it. What the owner reads uses
plain words; identifiers appear only on the final "Technical details" line
of a message. The one phrase for coming back, everywhere: "run /release
again". The steps are the `/release` skill's, one for one:

| internal | say |
| --- | --- |
| candidate / HEAD | your latest commit abc1234 |
| proven / stamped | goes in / released in 0.2.0 |
| `no_trailer` | not included yet — no commit says it is done |
| `reverted` / `revert_unconfirmed` | its change was undone by 0f1e2d3 / a later commit may have undone it |
| `below_range` | only linked to commits from before v0.1.0 |
| `squash_suspected` | probably came in with 'Add watcher (#42)' |
| attest | mark as done — it is already committed (report: "added an empty commit recording that") |
| `Changelog:` line | this commit marks as done: … |
| uncertain candidate | #2 Dark mode might be part of this work too |
| version-only | only the version number, no changelog entries |
| contract change | dct will update VERSION in every future release |
| version-sync script | your version script (scripts/dct-version-sync.sh — its path, `consent.version_files.script.identity`) |
| push target / no upstream | push to origin/main (url) / this branch isn't connected to a remote branch |
| range base / approximate | counting changes since v0.1.1 / no earlier release tag — commits since 12 Mar |
| record, reconstruct | report only: 0.1.0 was released before you used dct — nothing to do |
| unfinished record | release 0.2.0 isn't finished (tagged here, not pushed) |
| own executor | released with its own tool |
| busy lock (exit 1, `details.lock`) / conflict (exit 75) | another release command is running / the dct database changed at the same moment |
| digest, snapshot, consent, plan / prepare / apply | never shown |

0. **Probe and plan** — `dct release plan --json`. A missing / older CLI
   (unknown subcommand or flag, or a plan whose `schema_version` lower than 5
   shows it has no `executor`, no `changelog_file`, no `version.suggested`
   or no `pending_consent` key) → STOP: "Your dct is missing or older than
   this /release. Upgrade it with `dct upgrade` (or `uv tool upgrade
   dctracker`, or `pipx upgrade dctracker`; not installed: `uv tool install
   'dctracker[mcp]'`), restart Claude Code, then run /release again."
   Exit 1 → one plain sentence with its next step, then STOP; never retry
   from another directory. Project not found: "This folder isn't registered
   with dct yet. Register it with `dct project add <slug> .` (a short name
   of your choice), then run /release again." A checkout that could not be
   verified: the next step the CLI's message names, in plain words.
1. **Route — from this ONE plan result** (K-111, K-119). The plan carries
   `kind`, `unfinished_release` and `executor`: `executor.project` (the
   project's release-executor setting TODAY) and `executor.record` (`null`,
   or the unfinished record's `version`, `status`, stored `executor`,
   `progress_owner` and `consent`), read in one transaction. Decide from
   this ONE result — no other command is run to find out who releases (a
   later read can disagree with the plan). An unfinished release (`kind ==
   "resume"`) is routed by what the RECORD stores, whatever the setting says
   today; `executor.project` applies ONLY to a NEW release. Never ask the
   owner again merely because the setting for future releases changed.

   | the plan shows (the FIRST matching row routes) | who finishes it | path |
   | --- | --- | --- |
   | `kind == "resume"` and `executor.record.progress_owner == "dct"` | dct, on the consent STORED on the record | **Resume path** → `dct release resume` at once when the owner asked to continue (no second question) |
   | `kind == "resume"` and `executor.record.executor == "own"` | the project's own release tool | **Resume path** → hand over: its tool finishes it through the ledger path; dct runs nothing and takes nothing over |
   | `kind == "resume"` and `executor.record.consent == false` | nobody automatically — a `dct` record stamped outside `apply` | **Resume path** → offer `dct release takeover` (a NEW consent the owner approves) or the ledger path |
   | `kind == "release"` and `executor.project == "own"` | the project's own release tool | the hand-over sentence (Rare cases), then STOP |
   | `kind == "release"` and `executor.project == "dct"` | dct | Step 2 |

   Every later plan of the dialog is routed by the same table. For a new
   release dct runs, two more reads of the same plan, in this order (no
   commit made under the owner's Release goes unmentioned; a Finish refused
   for the changelog file comes back through its refusal row):
   `pending_consent.consent_digest` set and `pending_consent.adoptable` true
   (dct made a commit under an earlier Release) → the Finish question (Rare
   cases), nothing else; `consent_digest` null but `progress` records a
   commit → "A commit made under an earlier Release" (Rare cases); no commit
   recorded → a preview without a commit is only a draft: go on — the next
   preview replaces it. THEN `changelog_file.problem` set → the
   changelog-file question (Rare cases), then plan again from step 0. The
   plan checks the file as the preview will — against the tree the
   work-in-progress commit of the uncommitted changes will have — so a
   difference that commit settles (an uncommitted edit of the changelog, a
   conversion to Markdown, a link replaced by a file) is no problem and
   never asked. A problem that comes back after its way was taken is never
   asked a second time: go on to the preview (step 2) — it checks the file
   against the commit it builds, and a real problem comes back as its
   `details.changelog_file` refusal.
2. **Before the screen — silent** ("Checking proj-a…"), in this order:
   `dct release status --version <current> --json` (only with a
   `version.current`) — read for ONE sentence of the report (K-2); outcome
   `none` (released before dct kept records) is said there.
   **Uncommitted changes — the work in progress** (the plan's `index.clean`
   is false — staged, unstaged or deleted paths — OR any
   `index.release_artifacts[].untracked` is true OR a new file of
   `index.untracked` is plainly part of the same work) are committed INSIDE
   the release, in this workflow: `apply --consent` commits your changes
   through the repository's hooks, as the screen shows them. Drafting that
   commit's message is the assistant's job, not a request to the owner: do
   not tell the owner to commit and come back. Run
   `dct clog candidates --json`, read the diff (`git diff HEAD`,
   `git status --porcelain=v2`) and draft the message in conventional format (the `/commit` fallback's
   format): a subject with the `(v<current>)` suffix, then the line
   `Changelog: <confirmed ids that describe this diff>` — only `confirmed`
   candidates whose text matches the diff; an `uncertain` one never goes in
   (the screen says it "might be part of this work too"). Write it to a
   scratch file (never into the project); it becomes `--message-file`, for
   a dirty tree only (dct refuses a message on a clean tree). A new
   untracked file (the plan's `index.untracked`) goes in with
   `--include-untracked <path>` only when it is plainly part of the same
   work — then there is work to commit, also on an otherwise clean tree, so
   the message is drafted; every other one stays out and the screen lists
   it ("Not included", from the preview's `details.left_out`) — never a
   path that looks like a secret (`dct clog candidates --json` lists them
   under `secret_like`) or build output. Do not ask the nested /commit
   questions (the version-bump redirect, "add an entry?", "track the
   untracked files?"): the screen replaces them. A merge, rebase,
   cherry-pick or revert in progress, or a conflicted path → STOP: "A merge
   is in progress — finish it (`git merge --continue`) or cancel it (`git
   merge --abort`), then run /release again." (the others: their own
   `--continue` / `--abort`).
   **The preview** — ONE call computes the whole release (the commit of the
   uncommitted changes, the entries, the changelog section, the version
   files, the checks, the push) on commits no branch points to, and stores
   it; nothing is committed, staged, tagged or pushed. It runs the version
   script BEFORE the owner answers, in an isolated copy that is not a
   sandbox, so the script must change only the version files and never
   wrap a release, publish or deploy command:
   `dct release prepare --preview --json --message-file <scratch file> --include-untracked <path>`
   (uncommitted changes) or `dct release prepare --preview --json` (a clean
   tree). Outcome `preview` → step 3 (nothing goes in and more than 4
   entries wait → first the question of Rare cases, "Before the screen");
   otherwise test these two FIRST, before any refusal row:
   `details.version_unresolved` (a refusal, exit 1: no version can be
   suggested) → the "Which version is this release?" question, and outcome
   `failed` (the version script failed or timed out — `details.sync`; a
   failed result has no `display`) → the stop, both in Rare cases, "Before
   the screen"; only then any other refusal → "When a command refuses"
   (Rare cases).
   **Publishing workflows** — any `.github/workflows/*.yml` or `*.yaml`
   with an `on: push: tags` trigger: the screen's "Then:" block says that
   pushing the tag publishes.
3. **The screen — ONE AskUserQuestion with two questions.** The screen is
   rendered only from this preview's `variants[shown].consent` and its
   `display` (the work block from the same preview's `details.work`, the
   one remote line from `details.remote`), with no other source; the
   headline's version is `variants[shown].consent.version.target`
   (`variants[shown]` is the entry of the preview's `variants` list whose
   `name` is `shown`; `variants[all]` the one whose `name` is `all`). The
   skill's worked example:

   ```
   Ready to release proj-a 0.1.1 (now 0.1.0) — a patch, because it only fixes things

   Your uncommitted changes are committed first:
     src/help.py, src/cli.py (all of it, including the parts you didn't stage), README.md
     Not included: notes.txt (new, not tracked by git)
     Message: "fix: correct the help text (v0.1.0)"
     This commit marks as done: #1 Typo in help
     #2 Dark mode might be part of this work too. If so, choose 'Release with all 2'.
   Goes in (1):
     Fixed    Typo in help                   #1
   Not included yet (1) — no commit says it is done (a commit says so with a line like 'Changelog: 2'):
     Added    Dark mode                      #2
   Files:  CHANGELOG.md (new 0.1.1 section) · VERSION 0.1.0 → 0.1.1
           First release with dct: dct will update VERSION in every future release.
   Counting changes since v0.1.0.
   With all 2: 0.2.0 — also #2; VERSION 0.1.0 → 0.2.0
   Then:   commit "release: v0.1.1", tag v0.1.1, push both to origin/main
           (git@github.com:me/proj-a.git)
           Pushing the tag starts your publish workflow (publish.yml).
   ```

   (Fixes only → a patch; "with all 2" adds something → a minor version. On
   0.x the two differ only when the shown entries are fixes only.)
   Its blocks, an empty one left out: the work block (the paths — a path
   whose `staged` is `part` or `none` marked "all of it, including the
   parts you didn't stage" —, the "Not included" line, the message
   verbatim, "marks as done: …" from `work.proves`, each `work.uncertain`
   entry as "#2 Dark mode might be part of this work too. If so, choose
   'Release with all 2'." — no "all" variant: "If so, say so under 'Change
   something'."); "Not included" — every entry of `details.left_out`, the
   new untracked files the release does not commit, also on a clean tree
   (`details.work` is null): "Not included: notes.txt (new, not tracked by
   git)"; one whose `secret_like` is true: "Not included: .env (looks like
   a secret — left out on purpose)" — never included or offered; the
   assistant stages nothing itself, a file goes in only through
   `--include-untracked`; the empty commit, only when
   `variants[shown].consent.attest` is not null ("First an empty commit
   records that #2 is already done.", first in "Then:"); what goes in and
   what waits (`display.entries`, each waiting reason in plain words; the
   first waiting heading carries once "(a commit says so with a line like
   'Changelog: 2')"; nothing selected → "No changelog entries — only the
   version number changes", and above the list ONE sentence: "None of your
   commits is linked to these entries yet (a commit links one with a line
   like 'Changelog: 3'). If the work is already committed, choose 'Release
   with all 3': dct adds an empty commit recording that." — no "all"
   variant: "… tell me which ones under 'Change something'."; outside git
   "Goes in (4) — not checked against commits (this project isn't in
   git)"); the files and checks (`display.checks`, the section
   `display.section`; the contract line ("dct will update VERSION in every
   future release") only when its `change` is true (`display.contract`);
   `consent.version_files.script` null → "No version file changes — dct
   changes them only through scripts/dct-version-sync.sh, which proj-a
   doesn't have."; script output: "Your version script
   (scripts/dct-version-sync.sh) printed 14 lines — say 'show details'"
   from `display.sync`); the range line (`display.range` — its text and
   its notice; answering Release confirms it, it is never asked again;
   approximate → "No earlier release tag — looking at commits since 12
   Mar"); the "with all N" line (`display.delta`); "Then:" from
   `consent.push` — `push.tag`, `push.remote`/`push.branch` and
   `push.url`, the publishing workflow; `details.remote.checked` false
   adds "origin could not be reached now — it is checked again when you
   release." — or, for `mode` `local`, "not pushed: <reason>" in plain
   words: `requested` "you asked not to push", `no_upstream` "this branch
   isn't connected to a remote branch", `upstream_invalid` "this branch's
   remote branch can't be pushed to", `detached` "you are not on a
   branch", `several_urls` "origin pushes to 2 places and dct pushes to one
   — pick one under Change something", `url_not_literal` "git would
   rewrite origin's push address"; every `display.warnings` line; the
   shown variant's `suggested.safe` false → the headline says why, keyed on
   its `suggested.reason`: `tag_taken` "v0.2.0 already exists — releasing
   0.2.1"; `breaking` "a major version, because something was removed";
   `after_tag` "no version is recorded yet, so it counts on from the tag
   v0.1.0".
   Every version a label names is the `consent.version.target` of the
   variant that label applies — never `suggested.target`, which differs
   after an explicit `--version`: V_shown =
   `variants[shown].consent.version.target`, V_all =
   `variants[all].consent.version.target`.
   **Q1 — the action** — its question names no version ("Release proj-a
   and push it to origin/main?"; "Release proj-a here, without pushing?"
   for a local release; after "None" in the more-than-4 question "Release
   only the version number?"); options in this order: (1) "Release"
   ("Commit, tag and push to origin/main. The version you pick can't be
   released again.") — the label follows the shown variant, and IS that
   consent: "Release locally (no push)" when `push.mode == "local"`,
   "Release (version only)" when `version_only` is true, described "Only
   the version number changes; the changelog gets no new section, and this
   version won't appear in the exported changelog."; (2) "Release with all
   N" — only when the preview made the "all" variant (`all_unavailable` is
   null), never first; dct makes it only when 1 to 4 entries wait and none
   is reverted — never for reverted entries or more than 4 waiting ("Only
   if the work for #2 is already committed. dct records that with an empty
   commit, then releases."; when V_all differs from V_shown the label names
   it: "Release with all 2 as 0.2.0"); (3) "Change something" ("Which
   entries, where it is pushed, edit an entry — or 'show details'."; for
   `first_version`, which has no version question: "Another version, which
   entries, where it is pushed, edit an entry — or 'show details'."); (4)
   "Stop" ("Nothing is released; nothing in your project changes." — after
   a commit was made: "Stop — nothing is released; your changes stay
   committed as abc1234.").
   The screen is in Q1's previews: "Release" (in each of its forms) carries
   the screen of the shown variant, "Release with all N" the screen of the
   "all" variant, "Change something" one line per thing it can change,
   "Stop" what stays as it is. A screen longer than 15 lines: the option's
   preview is a summary page — the headline, one line per block with its
   count ("Goes in: 25 entries — on the Details pages") and the "Then:"
   lines — and the whole screen, in order, is on the pages of one more
   single-select question, "Details" (after Q2; for a first version, after
   Q1), each page at most 15 lines; its answer is not a decision.
   **Q2 — the version** ("Which version?"): Keep, listed first and marked
   recommended, built from V_shown — "Keep 0.1.1 (recommended)"; when the
   "all" variant exists and V_all differs, the label names both: "Keep the
   suggested version: 0.1.1, or 0.2.0 with all 2 (recommended)"; on a
   screen after the owner typed another version, V_shown IS that version:
   "Keep 0.3.0 (your choice)" —, then the `suggested.alternatives`
   (leaving out only V_shown; when V_all differs it stays in the list for
   the shown entries alone: "0.2.0 — without #2"), then Other to type one.
   Q2 is omitted for `first_version` (`suggested.reason`: no version and
   no release tag yet): the headline shows the proposed version, "Ready to
   release proj-a 0.1.0 — its first release", and the dialog is the action
   question alone. The version is proposed by dct and approved by the owner
   with Release: Q2 changes it, and for `first_version` (no Q2) "Change
   something" does.
   There is NO "include without proof" option: an entry goes in only when a commit says it is done.
4. **After the answer:**

   | answer | what runs |
   | --- | --- |
   | "Release" + "Keep" | `dct release apply --consent <variants[shown].consent_digest> --confirm --json`, then Step 5 — no other question |
   | "Release with all N" + "Keep" | `dct release apply --consent <variants[all].consent_digest> --confirm --json`, then Step 5 |
   | "Release" + another version V2 | `dct release prepare --preview --version <V2> --json` with every earlier flag — the same message file, untracked files, `--attest`, `--entries` / `--no-entries` and push flags as before; then the screen again, headed "Version changed to 0.3.0" |
   | "Release with all N" + another version V2 | the "all" variant's selection REPLACES the earlier one: drop every earlier `--entries`, `--no-entries` and `--attest`, then `dct release prepare --preview --version <V2> --entries <variants[all].consent.entries.selected> --attest <variants[all].consent.attest.ids> --json` — the message file, untracked files and push flags stay as before — so that the new screen's first option IS the release with all N; then the screen again, headed "Version changed to 0.3.0" |
   | "Change something" + any version | Dialog B (Rare cases), then `update_changelog` / `delete_changelog` for edits, then the preview again with every earlier flag plus the changed ones (a push choice replaces the earlier push flags — Dialog B; another version in Q2 → also `--version <V2>`; another first version typed in Dialog B → `--version <V>`); then the screen again |
   | "Stop" + any version | `dct release prepare --discard-preview --json`. Nothing else. |

   A first version has no Q2 (`first_version`): its answers read as +
   "Keep" — "Release" applies `variants[shown].consent_digest`, the version
   the screen showed.

   Never pass a digest the screen did not show: `apply --consent` only with the consent_digest of the variant whose content the screen rendered. The one exception is the Finish question (Rare cases): it passes `pending_consent.consent_digest`, the screen the owner already said Release to.

   A refusal (exit 1 `refused`, exit 75 `conflict`) → "When a command
   refuses" (Rare cases).
5. **Read the result and report** — `completed` → the report; `noop`
   (already released — nothing done; a warning naming
   `dct release reconstruct` becomes one report sentence: the legacy path
   runs only when the owner asks for it); `refused` → "When a command refuses" (only
   the listed `steps` happened; an unfinished `release` → the resume path:
   the same operation continues there on the stored consent once the cause
   is gone). Show every `warnings` line, also of a successful result, in
   plain words (a post-commit hook that failed after the commit was
   published — the release stands; a pin or stored blob that could not be
   cleaned). The report is plain text with identifiers only on the final
   "Technical details" line: the requested version
   (`details.requested_version`), the record's `version` and
   `details.current_version` separately, the steps and the `url` of the
   `push` step:

   ```
   Released proj-a 0.2.0 — commit and tag v0.2.0 pushed to origin/main.
     In it: #3, #4. Waiting for a later release: #1, #2 (no commit says they are done).
     Your changes were committed first as 4b5c6d7; #2 was marked as done with an empty commit.
     0.1.0 was released before you used dct — nothing to do.
     Pushing the tag started your publish workflow.
   Technical details: requested 0.2.0 · record 0.2.0 · current_version 0.2.0 · commit abc1234 · steps work_commit attest stamp changelog version_files commit index tag push complete done · push url git@github.com:me/proj-a.git
   ```

   A local release: "Released proj-a 0.2.0 here — commit and tag v0.2.0,
   not pushed (you can push the tag yourself later)." The missing older
   record is that one short sentence; "record 0.1.0" (the legacy path) is
   mentioned only when the owner asks about that version's history.

**Rare cases** — read a part only when a step above points there. Wherever
Rare cases, the resume path or the Legacy path (reached only when the owner
asks for it) says to show something and then ask, what is shown is the preview of the option it belongs to (pages
when longer than 15 lines — the screen rule above); something shown with no
question after it is the final text of the turn.

*A project released with its own tool.* dct performs none of its effects
(`prepare` / `apply` / `resume` refuse — `details.executor`). Say ONE
sentence and STOP: "proj-a is released with its own tool, not by dct.
Ready for it: #3, #4; waiting: #1, #2. Run that tool; it records the
release in dct." ([integration](release-workflow.md#integrating-a-projects-own-release-tool):
its tool stamps through the ledger with `expected_executor="own"` and
reports each verified effect, the tag with its `tag_proof`); never run a
command stored anywhere in its place.

*The Finish question.* `pending_consent.consent_digest` is set (null until
the owner said Release and dct made a commit under that answer) and
`pending_consent.adoptable` is true → ONE question, nothing else. Its text
says what is released, from `pending_consent.applied` (the variant the
owner said Release to: `version`, `entries`, `attest`) and
`pending_consent.progress`: "You said Release for proj-a 0.2.0 (#3, #4) and
dct committed your changes as abc1234, but the release didn't finish."
(only `progress.attest_commit` recorded: "… and dct marked #1, #2 as done
in abc1234, but …" — the ids of `applied.attest`); when `applied.attest` is
not empty and `progress.attest_commit` is null, add "Finishing also adds
the empty commit that marks #1, #2 as done." Options: "Finish releasing
0.2.0" / "Stop — nothing is released; abc1234 stays on your branch".
Finish runs the same answer again (dct adopts its commit and makes no
second one): `dct release apply --consent <pending_consent.consent_digest> --confirm --json`,
then step 5; Stop runs `dct release prepare --discard-preview --json`
exactly like the screen's Stop, the commit stays.

*A commit made under an earlier Release.* `consent_digest` null but
`progress` records a commit (`work_commit` or `attest_commit`): that
release cannot be finished here. `pending_consent.blocked` says why
(`blocked.why`) and the step that works instead (`blocked.next_step`). By
`blocked.why`: `other_checkout` (`pending_consent.checkout_root` is not
this checkout) → ONE sentence and STOP ("The release you approved earlier
was started in <checkout_root> — run /release again in that folder.");
`branch_switched` (HEAD is on another branch than at the preview, or
detached where a branch was, or the reverse) → ONE sentence,
`blocked.next_step` in plain words with the command it names exactly as
written, and STOP. It is never a new preview on the switched branch: that
would replace the release the owner approved and leave its commit behind.
The owner takes that step and runs /release again (→ the Finish question);
only when the owner says to release from this branch instead does the
dialog go on to the new preview. `head_moved` / `progress_lost` →
`blocked.next_step` IS a new preview: ONE line before it, chosen by what
still exists, with every plan `warnings` line that names the commit: a recorded commit
the `warnings` say no longer exists → "The commit dct made for your earlier
release (abc1234) is gone — here is the release again." (uncommitted
changes need a drafted message again); only `attest_commit` → "#1, #2 were
already marked as done in abc1234; the release you approved earlier didn't
finish, so here it is again."; a `work_commit` that still exists → "Your
changes are already committed as abc1234; the release you approved earlier
didn't finish, so here it is again."

*Previewing again after a commit was made* (a refusal of apply, the Finish
question refused, or the line above): in this conversation it keeps every
earlier flag; after a Finish, or in a new conversation, there are none —
take them from `pending_consent.applied`: `--version <applied.version>`,
`--entries <applied.entries>` (`applied.entries` empty → `--no-entries`: an
empty `--entries` is refused, and leaving the flag out would select every
proven entry — another release than the one the owner approved),
`--attest <applied.attest>` when not empty.
The push target is not recorded, so the screen shows the default one: say
once "Check where it is pushed — it was set again." Drop the `--attest` ids
a made `attest_commit` already marks (they are proven now; passing them
again is refused); pass a newly drafted `--message-file` whenever the plan
shows uncommitted changes — also after a commit was made (never on a clean
tree); the screen names what `made` holds, and its Stop reads "Stop —
nothing is released; your changes stay committed as abc1234".

*Change something — Dialog B.* ONE AskUserQuestion with at most 4
questions, each one's first option keeping what the screen showed: "Which
waiting entries are already committed?" (multiSelect, one option per
waiting entry — "#2 Dark mode — only if already committed"; more than 4:
"All waiting" or Other; a reverted entry is not offered →
`dct release prepare --preview --attest <ids> --json`); "Which entries go
in?" (multiSelect, with "None — release only the version number", the
version-only consent; AskUserQuestion takes at most 4 options: more than 3
entries go in → "Keep all" and "None — release only the version number",
the question text lists every entry with its id, and Other to type the ids
that go in → `dct release prepare --preview --entries <ids> --json`
or `dct release prepare --preview --no-entries --json`); "Where is it
pushed?" (keyed on the shown variant's `consent.push.mode`, never on a
target the screen did not show: `push.mode` `push` → "Keep
<remote>/<branch>" from `consent.push.remote`/`consent.push.branch`,
"Don't push — keep it on this computer", one option per other push
address of that remote, `git remote get-url --push --all <remote>`,
read-only; `push.mode` `local` → "Keep it on this computer", then one
option per configured remote (`git remote`, read-only) with the branch the
preview read, `details.base.branch`: "Push to <remote>/<branch>" — a
detached HEAD has no branch: Other only —, and for the reason
`several_urls` instead one option per push address of the branch's remote
(`git config branch.<branch>.remote`, then `git remote get-url --push
--all <remote>`, read-only); at most 4 options: more than 2 other push
addresses → only the first two options, the question text lists the other
addresses (and the remotes that do not fit), and Other takes one of them,
typed as listed, or a remote and a branch; Other: a remote and a branch →
`dct release prepare --preview --push-remote <remote> --push-branch <branch> --json`
only — the preview derives and checks the address and the screen again
shows it; `--push-url` is added only for an address the owner picked from
that list, never one the assistant derived (a fetch URL can differ from
the push URL):
`dct release prepare --preview --push-remote <remote> --push-branch <branch> --push-url <the address the owner picked> --json`;
or `dct release prepare --preview --no-push --json`; the owner picks ONE —
there is no mirror mode. The push choice REPLACES every earlier push flag
(`--no-push`, `--push-remote`, `--push-branch`, `--push-url`) instead of
adding to them: dct refuses `--no-push` beside a push target. So a push
target drops `--no-push`; "Don't push" / "Keep it on this computer" drops
every `--push-remote`, `--push-branch` and `--push-url`; another remote or
address drops the earlier `--push-url` (it named the old address); "Keep
<remote>/<branch>" changes nothing); "Anything else? To edit or delete an entry, type
'edit #2: new text' or 'delete #2' under Other." (for `first_version` it
adds: "To release another first version, type 'version 0.5.0'.") —
"Nothing else", "Also commit notes.txt" (only for a file of
`details.left_out` whose `secret_like` is false — a secret-looking one is
never offered) →
`dct release prepare --preview --include-untracked <path> --message-file <scratch file> --json`,
"Show details" — the section, the script's output, each entry's proof with
a PR number WITH its origin, `pr_source`: `git` = recognised in the
repository; `client` = declared by a client, never presented as git's
recognition — on the "Details" pages of the screen's dialog, asked again;
Other: "edit #2: <new text>" → `update_changelog`, "delete
#2" → `delete_changelog`, and for `first_version` only (it has no Q2)
"version <V>" → `--version <V>` on the next preview. Each preview keeps every earlier flag; then the
screen again. Version files cannot be changed there: "The file changes come
from your version script (scripts/dct-version-sync.sh) — fix it, commit it,
then run /release again." (no version script: "dct changes version files
only through scripts/dct-version-sync.sh — add one that takes the version
as its argument and changes only the version files, commit it, then run
/release again.")

*The changelog file* (`changelog_file.problem`, #876; prepare and apply
refuse it before the stamp, so it is settled before the preview) — one
question from its `found` and `ways`, then plan again: `ambiguous` → the
owner picks one of the offered paths (`dct project changelog-path <slug>
<path>`; a link is offered as the path through its target — one without an
admissible target is not a choice: its replace-link way and the off way are
offered, K-27); `case_variant` → the tracked spelling (the same command);
`not_markdown` → one of its ways: convert the existing changelog to
Markdown (the assistant rewrites its content in the Keep a Changelog layout
AND renames it to a Markdown extension, e.g. `git mv HISTORY.rst
HISTORY.md` — git mv alone does not convert the format —, then `dct
project changelog-path <slug> HISTORY.md` chooses it; the change stays
uncommitted and the screen's commit of uncommitted changes commits it —
outside git: renamed with `mv`, nothing to commit), an explicit Markdown
file (`dct project changelog-path <slug> CHANGELOG.md` — any Markdown name,
`changelog.md` too), or turn dct's changelog file off (`dct project
changelog <slug> off`); `symlink` (the file or a directory of its path —
`component`) → the path through the link's target as the explicit path
(offered only when admissible), the link replaced (committed by the screen;
outside git nothing to commit), or the changelog file off — with
`baseline: "work_tree"` the link is only on disk: moved away;
`not_a_directory` / `not_a_file` / `outside_project` → another path,
detection (`--detect`, offered only when the detected file also passes the
work-tree check) or the changelog file off — `not_a_file` with `baseline:
"work_tree"` is a TRACKED directory whose name differs only in case that a
case-insensitive file system opens at the path: never moved away (K-28);
`not_baseline` → by its `baseline`: `absent` / `tracked` → the owner moves
the file away (never an uncommitted edit of the tracked file: the plan
checks that against the work-in-progress commit, which settles it);
`unfiltered` → nothing to commit (the content is the commit's, but
not in the form a checkout through its filters writes): the file is checked
out again through its filters (`git rm --cached -q -- <path>`, then `git
checkout HEAD -- <path>`).

*Before the screen.* `details.version_unresolved` (a refusal: the current
version cannot be read, or every nearby version is taken) → ONE question,
"Which version is this release?" (Other: type it), then the preview again
with `--version <V>`. Outcome `failed` (the version script failed or timed
out, exit code 124) → STOP: "Your version script
(scripts/dct-version-sync.sh) failed (exit 1). It runs in a clean copy of
your last commit (no .venv, node_modules or untracked tools). Last lines: …
Fix it, commit it, then run /release again. Nothing was released." (the
last lines from `details.sync` — a failed result has no `display` —, with
`output_dropped_bytes` and `details.environment` under Technical details). **Nothing proven with more than 4 waiting entries** — keyed on
the waiting entries themselves, whatever `all_unavailable` says (it reads
`reverted` before `too_many`): `display.entries.selected` of the shown
variant is empty and `display.entries.waiting` holds more than 4 entries
that are not reverted (no `reverted_by`, reason not `reverted` /
`revert_unconfirmed`). ONE single-select question first, "Which of these
are already in your commits?" — the question lists every such entry;
options "All of them", "None — release only the version number" and
"Stop"; Other: type the ids; a reverted entry is not listed. "All of them"
or typed ids → `dct release prepare --preview --attest <ids> --json` (with
every earlier flag) and the screen; "None" → the screen of the SAME preview
(it already releases only the version number), its Q1 question reading
"Release only the version number?"; "Stop" — also "stop" typed under
Other, or the question dismissed — → `dct release prepare --discard-preview --json`,
exactly like the screen's Stop.

*When a command refuses.* Every refusal is one plain sentence with a true
next step; `details` only under "Technical details". Find its row in this
order: first the four bold cases below (in their order), then the table of
preview and apply refusals, then the table of the release's own refusals.
A case or a row matches when the refusal carries one of its keys (a row: of
its first column) and every condition it states holds (WITH / without,
before / after the stamp); the first match wins — each table lists its most
specific rows first. Nothing matches → the CLI's message in plain words,
and stop. One rule for every sentence: every refusal of apply
carries `details.made`; when `details.made` holds a commit, the sentence
names it — "your changes are already committed as abc1234"
(`made.work_commit`), or "#1, #2 were marked as done in abc1234" (only
`made.attest_commit`) — and never says "nothing was changed"; the error
text "commit made, release not started" means exactly that: say that the
changes are committed as abc1234 and nothing was released. A refusal of the
preview that follows such a commit names it the same way.
**Something moved** — `base_moved`, `work_changed`, `index_changed`,
`hook_changed_tree`, `consent_mismatch`, `preview_replaced`,
`date_changed` (or `missing_blobs`) → `dct release prepare --preview`
again (with every earlier flag and the same message file while nothing was
made — drafted again when the uncommitted changes themselves changed; after
a commit: "Previewing again after a commit was made"), then the screen
again headed "Something changed since you said Release: <what, in the words
of the table below>", naming what `made` holds ("Your changes are already
committed as abc1234."). Never apply again silently. One exception: a
`base_moved` whose `why` is `branch_switched` after a commit was made
(`made`) is not previewed again here — plan again (`dct release plan
--json`) and follow its `pending_consent.blocked` ("A commit made under an
earlier Release"), never a new preview on the switched branch. **The one automatic re-run** — only
BEFORE the stamp (the refusal has no `release` record and no `stamp` among
its `steps`): a database conflict (exit 75, outcome `conflict`) or a
network error, `details.git_error` with `details.url` and none of `hook`,
`rewrite_rule`, `remote_name` (the push destination could not be read) →
ONE automatic re-run of the same `apply --consent` (dct adopts what the
first run made, so it is safe); fails again → a plain sentence and stop:
"origin could not be reached — nothing was released (your changes are
committed as abc1234)." That one re-run is the only retry the assistant
makes on its own: a busy lock (exit 1) is not re-run, and every row below
that says "the same command again" waits for the owner to say the cause is
gone ("run /release again" — which leads to the Finish question when a
commit was made). **The same network error AFTER the stamp** (the refusal
carries the `release` record, status `tagged`: commit and tag exist here)
→ never re-run apply and never say "nothing was released": "Release 0.2.0
is half-done: commit and tag made here; origin could not be reached. When
the network is back, say 'finish the release'." → then the resume path.
**A hook rejects the commit** (`details.hook`, BEFORE the stamp: no
`release` record) → Stop with the hook's output ("Your pre-commit hook
stopped the commit: <its output>. Fix that, then run /release again." —
with `made` set, add "(your changes are already committed as abc1234)");
never `--no-verify`; a hook that changed files is `hook_changed_tree` above
("Your pre-commit hook changed: …"). A commit stopped for another cause
(signing, a moved branch, the commit identity), and every one after the
stamp: the release table's commit row, its sentence for that cause.

What changed, in plain words — the keys of `consent_mismatch.changed` (and
the other refusals of the list above; the same table as the skill's):

| key | say |
| --- | --- |
| `work` | your uncommitted changes |
| `candidate_tree` | the files of the release |
| `attest` | which entries are marked as done |
| `entries` | the changelog entries |
| `changelog` | the changelog section |
| `version_files` | the version files |
| `version` | the version |
| `push` | where it is pushed |
| `base` | your latest commit |
| `range` | which commits are counted |
| `project`, `project_root`, `checkout_root`, `executor`, `release_mode`, `format` | where and how proj-a is released |
| `base_moved` | your branch moved (another commit, or another branch) |
| `work_changed`, `index_changed` | your files changed |
| `hook_changed_tree` | your pre-commit hook changed <paths> |
| `preview_replaced` | a newer release screen was made meanwhile |
| `date_changed`, `missing_blobs` | the date (it is past midnight UTC) / files dct had kept for the release are gone |

Preview and apply refusals, in plain words — one row per key (the same
table as the skill's):

| refusal | say (one plain sentence) | then |
| --- | --- | --- |
| `in_progress`, `conflicted` | "A merge is in progress — finish it (`git merge --continue`) or cancel it (`git merge --abort`), then run /release again." (a rebase, cherry-pick or revert: its own `--continue` / `--abort`) | stop |
| `intent_to_add` | "notes.txt is marked to be added, but git has no content for it yet. Commit it with /commit, or unmark it (`git rm --cached notes.txt`), then run /release again." | stop |
| `hidden_index_flags` | "git is told to ignore changes to config.yml. Run `git update-index --no-skip-worktree config.yml` (or `--no-assume-unchanged`), then run /release again." | stop |
| `submodule`, `filtered_paths` | "dct can't commit these changes for you: <path> is <a changed submodule / stored through a filter such as LFS>. Commit them with /commit, then run /release again." | stop |
| `secret_like` | a TRACKED path (not in the plan's `index.untracked`): "<path> looks like a secret, so dct won't commit it for you, and /commit won't either. If it belongs in the repository, commit it yourself with git; otherwise stop tracking it (`git rm --cached -- <path>` and add it to .gitignore), or throw your edit away (`git restore --staged --worktree -- <path>`). Then run /release again."; an untracked one: "<path> looks like a secret, so dct won't commit it. Move it out of the repository or add it to .gitignore, then run /release again." | stop |
| `bad_untracked` | (yours: an untracked path you named is tracked, ignored, missing or outside) | drop it, preview again |
| `index_only` | "<paths> differ from your last commit only in what is staged — the files themselves are unchanged, so there is nothing to commit. Unstage them (`git restore --staged -- <paths>`) or make the change you meant, then run /release again." (the paths from `details.index_only.paths`; the dialog never unstages for the owner) | stop |
| `message_required`, `nothing_to_commit`, `message_empty`, `message_file`, `message_foreign_ids` | (yours: the message file does not fit the tree) | fix the draft, preview again |
| `not_unproven`, `not_attestable`, `no_git` | (yours: an id that cannot be marked or selected; a selected entry without proof, `unproven`, is the release table's row) | drop it, preview again |
| `attest_without_entries` | (yours: `--attest` beside `--no-entries` — marking entries as done that nothing releases) | drop `--no-entries` (the "Release with all N" row of step 4 replaces the selection), preview again |
| `remote_ahead` | "origin/main has commits you don't have. Run `git pull` (if git says your local changes would be overwritten, save them first with /commit), then run /release again." — after a commit was made, add "(your changes are already committed as abc1234)" | stop |
| `remote_tag_exists` | "v0.3.0 already exists on origin. Pick another version." | the previous screen again IN FULL (its preview is still the stored one — a refused preview stores nothing), this sentence opening its question text; no previous screen (the version came from "Which version is this release?") → that question again, this sentence opening its question text |
| `version_lower`, `version_taken`, `version_invalid` | `version_lower` (`version`, `current`): "0.1.0 is lower than the current version 0.2.0. Pick another version."; `version_taken` (`version`, `status`): "0.2.0 was already released. Pick another version." (status `abandoned`: "0.2.0 was used by a release that was abandoned. Pick another version."); `version_invalid` (`version`): "1.x isn't a version — use numbers like 1.2.3. Pick another version." | the previous screen again IN FULL (a refused preview stores nothing, so it is still the stored one), this sentence opening its question text — its Q2 (a first version: "Change something") picks another version → `dct release prepare --preview --version <V2>` with every earlier flag and the new screen; no previous screen (the version came from "Which version is this release?") → that question again, this sentence opening its question text, then the preview with `--version <V2>` and a new screen — never the CLI's message alone |
| `version_unresolved` | (a question, not a sentence: no version can be suggested — the current version cannot be read, or every nearby version is taken) "Which version is this release?" (Other: type it) | Rare cases, "Before the screen": that question, then preview again with `--version <V>` — never the CLI's message and stop |
| `preview_too_large` | "These changes are too large for one release screen. Commit them with /commit, then run /release again." | stop |
| `checkout_changed` with `details.sync` | "Your version script (scripts/dct-version-sync.sh) changed files in your project (<files>). It may change only the version files, in its own copy. Fix it, commit it, then run /release again." | stop |
| `checkout_changed` without `details.sync` (`checkout_changed.head`, or the files) | "Your files or branch changed while dct was checking. Run /release again once nothing else is changing them." | stop |
| `no_preview`, `consent_unknown`, `consent_in_progress` | "That screen is no longer valid — nothing was released. Run /release again." | stop |
| `consent_flag_conflict` | (yours: a binding flag beside `--consent`) | the documented apply only |
| `base_moved`, `work_changed`, `index_changed`, `hook_changed_tree`, `consent_mismatch`, `preview_replaced`, `date_changed` | "Something changed since you said Release: <what — the table above>." | preview again and the new screen (above) |
| `message_contract` | "Your commit-msg hook changed the first line of the commit (or its Changelog: line), so it is no longer the commit you approved. Nothing was committed. Commit the changes yourself with /commit, then run /release again." (the release commit's first line, "release: v0.2.0", must stay as dct writes it too — a hook that rewrites every first line has to leave dct's commits alone) | stop — a new screen would meet the same hook |

Refusals of the release itself (the keys are the workflow's own,
`dct/workflows/release/effects.py`; the same table as the skill's):

| details | what it means — say it in one plain sentence | next action |
| --- | --- | --- |
| `details.process_groups`, `details.process_group` | a dct that was killed left processes running (`process_groups`, beside `lock` and `holder`; `[]` = it cannot be verified), or a version script could not be stopped (`process_group`) — the repository may still be written to | never loop and never clear it yourself: show the groups and the hint of `details.error`; the owner decides (ends those processes and clears the lock file as the message says); only then the same command once |
| `details.lock` WITH `details.reason` | `lock: null` — the lock's identity cannot be determined: the project has no registered path (`reason: "no_path"`) or its path cannot be verified (`"unverifiable_path"`, with its `path`); dct never locks on a substitute key: "dct doesn't know where proj-a lives on this computer." | this is not a busy lock: the owner registers or corrects the path (`dct project rename <slug> --path <dir>`), or the command is run where the registered path exists; then the same command once |
| `details.lock` WITH `details.holder` (no `process_groups`) | exit 1: another release command holds the repository's operation lock (pid, action, start): "Another release command is running (process 4242, since 12:03)." | stop with the sentence; the same command again only when the owner says the other command has ended ("run /release again" → the Finish question when a commit was made) — never on your own, never loop |
| `details.executor`, `details.consent` | the project (`executor: "own"`) or the record (`own`, or `consent: null` — stamped outside `apply`) is not executed by dct: "proj-a is released with its own tool." | a new release: the hand-over sentence (Rare cases); a record: the Resume path — the own tool's ledger path, or for a `dct` record the takeover (a NEW consent); this dialog runs nothing in its place |
| `details.git_version`, `details.required_git_version` | git is older than the workflow needs (the hooks are run explicitly) — refused before any effect, never a fallback that skips hooks: "Your git is too old for dct's release (2.36 or newer needed)." | the owner upgrades git; then run /release again |
| `details.staged`, `details.unstaged`, `details.conflicted`, `details.untracked`, `details.in_progress` | the tree is not clean or a release artifact is untracked: "Something changed in your files after the screen." — a merge / rebase / cherry-pick / revert in progress or a conflicted path: the first table's `in_progress` sentence | uncommitted changes are committed by the screen: preview again (Step 2); `in_progress` / `conflicted` → the sentence and stop of the first table's `in_progress` row (never "something changed") |
| `details.unfinished` | an unfinished release exists: "Release 0.2.0 isn't finished yet." | the Resume path |
| `details.unproven` | a selected entry has no proof: "#4 is not marked as done by any commit." | preview again without it, or mark it as done ("Change something") |
| `details.approved_ids`, `details.proven_ids` | the empty commit that marks entries as done: a commit hook changed the id set — the commit would prove `proven_ids`, the owner approved `approved_ids` (equality is required); nothing was published: "Your commit-msg hook changed which entries the commit marks as done." | the owner fixes the hook; then run /release again |
| `details.approved_digest`, `details.derived_digest` | for a takeover: the record differs from the approved takeover preview: "Release 0.2.0 changed since you looked at it." | a takeover: preview it again |
| `details.proposal_digest`, `details.plan_digest` | a new release was started without approved version files (`proposal_digest: null`) — equal paths or a checked version field are no consent to the content | `dct release prepare --preview` again and the screen again |
| `details.mismatch`, `details.pending`, `details.approved_script`, `details.missing_blobs` | the approved files are void: another commit / version, a newer preview, a changed script, pruned blobs: "Something changed since you said Release." | preview again — a new screen |
| `details.changed` (without `tree` / `expected_tree` / `hook` — with them it is the hook row below) | before the stamp: a version file changed since the preview (the approval is void); on resume: a release file differs from the recorded manifest, or a post-commit hook changed the work tree after the commit: "<file> changed after the screen." | before the stamp: preview again; on resume the owner restores the files, then resume |
| `details.checkout_changed` WITH `details.sync`, `details.sync`, `details.environment` (a `checkout_changed` without `sync` is the first table's "Your files or branch changed while dct was checking" row) | the version script wrote outside its isolated copy, failed / timed out (`sync`, with its `environment` contract: it ran isolated, with only the commit's files — never in the checkout), touched the changelog, or broke a version file's contract: "Your version script (scripts/dct-version-sync.sh) failed." | STOP; the owner fixes the script (it must provide what it needs itself, committed), then run /release again |
| `details.changelog_file`, `details.approved` | the changelog file is not settled (`changelog_file.problem`: `ambiguous`, `case_variant`, `not_markdown`, `symlink`, `not_a_directory`, `not_a_file`, `outside_project`, `not_baseline`, or `content` — K-9: composing the release's section into the file refuses, e.g. not UTF-8, a different pre-written V section or git filters that rewrite the text (`filtered`, K-20), `changelog_file.detail` — refused BEFORE the stamp, nothing released) — or, on resume, `approved` (the file the approved files bind) is not the file resolved now (a changelog setting changed since the approval, or a record stamped before #876: neither file is written): "dct can't write your changelog file: <why>." | before the stamp: the changelog-file question (Rare cases; its `ways`; `content` → the owner fixes the file — re-encodes it / removes the pre-written section — or turns the changelog file off), then plan again from Step 0; on resume FIRST set a changelog setting consistent with the approved path (which setting the approval was made under is not recorded) — `details.restore`, the `dct project changelog …` / `dct project changelog-path …` commands the refusal names, derived from the stored proposal — on the owner's yes, then `dct release resume`; only when `restore` is empty (a record stamped before #876) or the owner declines, the last resort: `dct release abandon` (the owner's reason) ends the record, its stamps and `current_version` stay |
| `details.tags` | K-8 (the ONE row for this key): a tag of V (any spelling) already exists in THIS repository (not on origin). BEFORE the stamp (apply or the preview; nothing released, the message names a delete command): "v0.3.0 already exists in this repository (not on origin). Remove it with <the delete command the refusal names in `details.error`> if it is a leftover, or pick another version." — with `made` set, add "(your changes are already committed as abc1234)". AFTER the stamp (the tag step of apply or resume; V is stamped, nothing tagged): "Release 0.2.0 is half-done: v0.2.0 already exists here and points at another commit, so dct didn't tag it. If that tag is a leftover, delete it yourself, then say 'finish the release'." | before the stamp: not applied again until the owner has removed the tag themselves (the dialog never runs it) — then "run /release again" (with a commit made, the Finish question re-applies that same approved screen: the one sanctioned exception; without one, a new preview); or the owner picks another version → `dct release prepare --preview --version <V2>` with every earlier flag ("Previewing again after a commit was made" when one was) and the screen again. After the stamp: never another version (V is taken) — once the owner removed the tag, `dct release resume` |
| `details.foreign` | a release file differs from both its baseline and the approved content — nothing overwritten: "<file> was edited during the release." | an edited file: the owner restores or finishes it, then resume; a changelog file whose baseline is absent (the message says so — an ignored / untracked file, a case-variant, or an edit of the file the release wrote): the owner moves it away, never "restores" it, then resume |
| `details.foreign_paths`, `details.foreign_content` | the index holds staged changes that are not the release's — left untouched: "You staged other changes meanwhile." | the owner unstages them, then resume |
| `details.commit` WITH `details.parents`, `details.tree`, `details.expected_tree`, `details.candidate` | on resume HEAD is neither the release's base nor the release commit — a commit made by hand, or the leftover of an older run (`commit`, its `parents`, its `tree` against the approved `expected_tree`, the `candidate`); reported exactly: dct touched no ref, no staging and no file, and it never resets: "There is a commit on top that this release doesn't know." | show it. The hint of `details.error` is conditional: IF that commit is the owner's to discard and nothing is based on it, the owner moves the branch back, then resume. The owner decides — never this dialog |
| `details.git_error` WITH `details.push_target` | a failed push → the record stays `tagged` (the commit and the tag exist locally): "Release 0.2.0 is half-done: commit and tag made here, the push failed: <git error>." | fix the cause, then resume — it retries ONLY the push, to the approved URL |
| `details.url` WITH `details.rewrite_rule` or `details.remote_name` (a `url` with only `git_error` = the remote could not be read — "The one automatic re-run" before the stamp, the Resume path after it) | git would not send the approved address `url` to the repository it names: an `insteadOf` / `pushInsteadOf` rule (`rewrite_rule`) would rewrite it, or the string is also the name of a git remote (`remote_name` — any config scope; a slash does not make it a path) — dct pushes only to the exact approved URL: "git would push to a different place than the one you approved." | the owner removes that rule (or points the remote at the final address); for a remote name BEFORE the stamp: writes the remote's push URL explicitly (`./name`, an absolute path or a real URL) or renames the colliding remote, then preview again; AFTER the stamp the consent keeps the approved string, so only renaming or removing the colliding remote works, then resume (or abandon) |
| `details.foreign_staged` | after a commit dct made: the owner's own staged version of a file was preserved: "You have other staged changes in <file>." | the owner unstages or commits it, then resume (before the stamp: run /release again) |
| `details.git_error`, `details.hook`, `details.head`, `details.expected_parent`, `details.tree`, `details.expected_tree`, `details.changed`, `details.subject`, `details.unpublished_commit`, `details.pinned_ref`, `details.head_ref`, `details.signed` | the release commit (or the work or marking commit) was stopped and nothing was published — no ref moved: a hook refused it (`hook`, `git_error`), a hook changed the approved content (`changed`, `tree` ≠ `expected_tree`) or the message outside its contract (`subject`; `unpublished_commit` = an unreferenced object git removes), signing failed (`signed`), or HEAD moved / the branch was switched meanwhile (`head` ≠ `expected_parent`, `pinned_ref` ≠ `head_ref`) — or, refused by apply BEFORE the stamp (`git_error` alone, nothing released), git cannot name the commit identity. One sentence per cause, the one that is true: `hook` → "Your pre-commit hook stopped the release commit: <output>."; `changed` or `tree` ≠ `expected_tree` → "Your pre-commit hook changed the files of the release commit: <paths>."; `subject` → "Your commit-msg hook changed the first line of the release commit."; `signed` → "git couldn't sign the release commit: <its error>."; `head` ≠ `expected_parent` or `pinned_ref` ≠ `head_ref` → "Your branch moved (or you switched branches) while dct was committing, so it stopped."; `git_error` alone before the stamp → "git doesn't know who you are, so it can't make the commit." — each, with `made` set, adds "(your changes are already committed as abc1234)". `git_error` with `url` and none of `hook` / `rewrite_rule` / `remote_name` is not this row: the push destination could not be read ("The one automatic re-run") | show the hook output; the owner fixes the hook or puts the files back as approved (after a branch switch: switches back), then resume on the same candidate (a work or marking commit before the stamp: run /release again) — no branch has to be moved back; signing: the owner fixes the signing setup (the key, or `commit.gpgsign`), then resume (before the stamp: run /release again); the identity: the owner sets `user.name` / `user.email`, then run /release again |
| `details.index_lock` | before the stamp (apply, K-25): another git process holds `index.lock` (never removed by dct) — refused, nothing stamped or recorded, so `dct release resume` only warns "nothing to resume": "Another git command is running." | stop with the sentence; the same `dct release apply` again only when the owner says the other git command has ended ("run /release again" → the Finish question when a commit was made, else a new preview) — never on your own |
| `details.index_lock` | after the stamp (the commit step of apply or resume): another git process holds `index.lock` (never removed by dct) — the record stays as it is: "Another git command is running — release 0.2.0 is half-done. When it has finished, say 'finish the release'." | stop with the sentence; when the owner says it has finished, `dct release resume` once (it continues the recorded release) |
| `details.remote_tags` | the push destination already has a tag of V. BEFORE the stamp (apply's check before any write; no `release` record, nothing released): "v0.2.0 already exists on origin." — with `made` set, add "(your changes are already committed as abc1234)". AFTER the stamp (the push step of apply or resume; the refusal carries the `release` record — V is stamped and tagged here): "Release 0.2.0 is half-done: origin already has v0.2.0 at another commit." | before the stamp: the owner picks another version → `dct release prepare --preview --version <V2>` with every earlier flag ("Previewing again after a commit was made" when one was) and the screen again; or the owner sorts out the remote's tag, then run /release again. After the stamp: never another version (V is taken) — the owner sorts out the remote's tag; when the owner says it is done, `dct release resume` |
| `details.remote_branch` | the remote branch is not contained in the release: "origin/main has commits you don't have." | the owner pulls and integrates them, then run /release again |
| `details.push_target`, `details.push_urls` | before the stamp, nothing released: the push target is incomplete or the named URL is not a push address of that remote (`push_urls`: the remote changed since the screen), or the remote has several push URLs (`push_urls`) — a release goes to ONE approved address (no mirror mode): "origin's push address changed after the screen." AFTER the stamp (the push step of apply or resume; the refusal carries the `release` record and `push_target` alone — no `git_error`, no `push_urls`): the push ran, but origin does not show the branch and the tag at the release commit: "Release 0.2.0 is half-done: the push ran but origin doesn't show it yet." | the owner picks ONE address under "Change something" (`--push-remote … --push-branch … --push-url <url>` on the preview), or "Release locally (no push)"; then the screen again. On resume `push_urls` without the approved URL = the remote no longer pushes to the approved address (outside the consent): the owner restores the remote, then resume. After the stamp with `push_target` alone: never "Change something" or "Release locally" (V is stamped and tagged) — the owner checks the remote; when the owner says it is sorted out, `dct release resume` |
| `details.outside_consent` | the record's version, entries or commit left the stored consent — resume does not cover it: "Something changed since you approved release 0.2.0: <what>." | show what differs; the owner restores the recorded scope, then resume. The dialog never widens a consent (Resume path) |
| `details.recorded` | the record belongs to another checkout: "Release 0.2.0 was started in <path>." | resume in that checkout (`details.recorded`) |
| `details.conflict`, `details.observed_effects`, `details.record_state` | outcome `conflict`, exit 75: a concurrent database write; the rolled-back write, the effects that DID happen (they stay), the re-read record (`unknown` → check `dct release status`): "The dct database changed at the same moment." | `apply --consent` before the stamp: the ONE automatic re-run above; any other conflict: never retry on your own — the owner decides: a conflict at the stamp → the same apply again; later → resume |
| `details.error` | every refusal: the CLI's message (verbatim only under Technical details) — this row decides only when no other row matches | the CLI's message in plain words, and stop |

**Resume path** (K-97 F4, K-111, K-112) — step 1 routes here. Show the
RECORDED release (`resume`: candidate, entry set, proof — a PR number with
its origin, `pr_source` — effect state) and, separately, today's HEAD
(`resume.head`, `checkout.matches_record`), the entries added since
(`resume.new_unreleased` — not part of this release) and who finishes it
(`executor.record`).

*A release dct executes* (`progress_owner` is `dct`): the owner's consent of
`apply` (or of a takeover) is stored on the record — version, entries,
candidate, proposal, push target — and is not asked for a second time.
`resume.blocked` is the precondition of every branch below that runs resume:
read it first; while it is not null, no branch runs resume — not on "finish
it", not after a restored scope. The plan already knows resume will refuse
(`resume.blocked` not null — K-34: the approved proposal binds a changelog
file the candidate no longer resolves; the same check resume refuses with)
→ do not offer finishing the release as doable and do not run resume: show
`resume.blocked.approved` and `resume.blocked.now`, then the ways in order —
the `restore` commands (`dct project …` settings, run on the owner's yes,
then plan again until `resume.blocked` is null, then resume) and
`resume.blocked.abandon` as the last resort (the only way when `restore` is
empty; the owner's decision, with the owner's reason); resume re-checks it
itself. The owner asked to continue it, `resume.blocked` is null and its
scope is unchanged → show the stored state and run `dct release resume --json`
at once, on the stored consent: no new approval, no menu, no second
question (the command takes no confirmation flag). It performs only the
missing effects — a `tagged` record: ONLY the push — and never re-runs the
sync; another checkout or HEAD is refused (run it in the record's
`checkout_root`; the record is never re-pinned); the release commit, the tag
and the push are finished by this one command, inside the same workflow.
The request was something else (a new version, a bare "release" in another
session) → a record in the database never starts anything by itself, and a
new release can start only after this one is finished or abandoned: while
`resume.blocked` is null ask ONE question — its text the stored state in
plain words, "Release 0.2.0 isn't finished: committed and tagged here, not
pushed yet." — with a Finish option and "Not now". The Finish label names
what is still missing: the FIRST status of `resume.effects` (in its order
`stamped`, `files_written`, `committed`, `tagged`, `pushed`, `completed`)
that is false, and the stored push target,
`unfinished_release.consent.push_target` (null = a local release;
`origin/main` = its `remote`/`branch`) — `files_written` or `committed` →
"Finish 0.2.0 (commit, tag and push to origin/main)"; `tagged` → "Finish
0.2.0 (tag and push to origin/main)"; `pushed` → "Finish 0.2.0 (push it to
origin/main)"; `completed` → "Finish 0.2.0 (it is pushed — dct records it
as done)". A local release names no push: `files_written` or `committed`
→ "Finish 0.2.0 here (commit and tag, no push)"; `tagged` → "Finish 0.2.0
here (tag it, no push)"; `pushed` or `completed` → "Finish 0.2.0 here (dct
records it as done)". Finish → `dct release resume --json`
at once, then step 5; "Not now" → nothing runs, and one sentence: "0.2.0
stays unfinished, and a new release can start only after it is finished or
abandoned. To abandon it, say 'abandon 0.2.0' and why — the version stays
used." (`resume.blocked` not null → the ways above, never the resume and
never this question). An earlier explicit "stop", or a withdrawn consent,
stays binding until the owner says to continue. A refusal or a conflict is
reported, not retried — never loop; run it again only when the owner says
the cause is gone. The scope changed (`details.outside_consent`) → the
stored consent does not cover it: show what differs; a new or wider consent
is asked for only then — for that concrete change of scope — and the
assistant never widens one by itself (the owner restores the recorded
scope, then resume once `resume.blocked` is null, or decides how the
release ends). Abandon only on the owner's explicit request or decision,
with the owner's reason — never a routine option (the record, stamps,
`current_version` and effects stay, V stays taken) →
`dct release abandon --version V --reason "<reason>" --confirm --json` —
the only way to abandon a release dct executes: the MCP tool
`abandon_release` refuses such a record (it holds no operation lock,
K-119 M6).

*A record nobody finishes automatically* (`progress_owner` is `ledger`):
`resume` refuses it (`details.executor`, `details.consent`) — do not run it.
The record's `executor` is `own` → its own tool finishes it through the
ledger path (its stamp carried `expected_executor="own"`; it reports each
verified effect, the tag with its `tag_proof`, a push with its `push_proof`
— [integration](release-workflow.md#integrating-a-projects-own-release-tool));
hand over. An `own` record stays with its tool: dct never offers the
takeover for it, whatever the setting says today. The record's `executor`
is `dct` (or null) and it has no consent (stamped outside `dct release
apply`: the MCP stamp, an older record, a reconstruction that ended at
`committed`) → two ways, the owner chooses. (1) The takeover: dct records
the missing consent from the VERIFIED artifacts — a NEW consent that needs
the owner's explicit approval ("no second question" covers only the
unchanged resume above), with an explicit push decision (the remote and
branch, or no push; when the remote has several push addresses, add
`--push-url <url>` only for an address the owner picked from `git remote
get-url --push --all <remote>` or from the refusal's `details.push_urls` —
never one the assistant derived). Preview:
`dct release takeover --version V --push-remote <remote> --push-branch <branch> --json`
or `dct release takeover --version V --no-push --json`
→ outcome `preview`, nothing written: show `details.consent` (version,
entries, candidate, release commit, tree, artifacts digest, the push target
or no push), `details.stage`, `details.missing_effects`, and keep
`details.takeover_digest` (`T`); only after the owner approves exactly that,
with the same push flags:
`dct release takeover --version V --push-remote <remote> --push-branch <branch> --takeover-digest <T> --confirm --json`
→ outcome `taken_over` (no effect performed); then `dct release resume --json`
performs only the missing effects. Refused (no release commit verifies,
`details.contradictions`, a digest that no longer matches → preview again)
→ report it; the ledger path stays. (2) The ledger path: whoever performs
the missing effects verifies them and records the progress
([executor](release-workflow.md#executor-k-85)) — never this dialog.

**Legacy path** — only when the owner asks for it ("record 0.1.0"):
`dct release reconstruct --version V --json` shows the evidence (outcome `preview`, nothing written): the resolved `version` — the
stored spelling, the request only in `requested_version` — the `candidates`
(verified or not, with the reason and the `history` state), the `status` the
evidence reaches (`completed` with a tag, `committed` without one),
`evidence.verification` (the scope that WAS verified; `limited: true` = no
artifact content was verified, only the database's stamp and, in git, the
commit prove the release — say so), `evidence.contract` (`state: "unknown"`
is not a `"confirmed"` empty list), and beside it `details.executor` (the
executor ASSIGNED to the record now — a responsibility, `provenance: false`,
not who made the release) and `details.finish` (the ways to finish a release
left at `committed`). Only after the owner confirms,
`dct release reconstruct --version V --confirm --json` records it (outcome
`reconstructed`, no re-stamp). Refused = contradicting or ambiguous
evidence: report it; never reconstruct by hand. The step for each refusal
and for finishing:

| situation | step |
| --- | --- |
| several verified candidates, no tag (`details.candidates`) | show them all; dct picks none — the owner picks one: `dct release reconstruct --version V --release-commit <sha> --json`, then the same with `--confirm` |
| the release commit changed older changelog history (`details.history`) | show the diff (`state`, `diff`, `diff_lines`); nothing is recorded and no flag or waiver exists — also for a typo fix; the release stays without a record |
| outside git, the contract is unknown (`details.contract`, `state: "unknown"`) | `dct release version-files --json` (outcome `none` = UNKNOWN, `found` = confirmed); ask the owner which files hold the version, or none; with THAT answer `dct release version-files --set <a,b> --confirm --json` or `dct release version-files --none --confirm --json` (outcome `confirmed`); then reconstruct again |
| the project's executor is `own` (`details.executor: "own"`) | its tool supplies the evidence file: `dct release reconstruct --version V --evidence <file.json> --json`, then the same with `--confirm` — the file comes from the own tool, never composed by the assistant |
| the record ends at `committed` (`details.finish`) | way `takeover` → the takeover of the resume path above (preview, the owner approves that digest, resume); way `ledger` → whoever performs the missing tag and push records them ([executor](release-workflow.md#executor-k-85); the only way for an `own` record) |

**Question count** — a dialog is one AskUserQuestion; an answer is one
question inside it (the count is in answers, the decisions; the "Details"
question of a screen longer than 15 lines is reading, not a decision, and is
not counted):

| path | dialogs | answers |
| --- | --- | --- |
| Typical, clean tree | 1 | 2 (Keep listed first, marked recommended) |
| Dirty tree (uncommitted changes) | 1 (+1 when a hook changes files or the state moves) | 2 |
| With all N (≤ 4 waiting) | 1 | 2 |
| Another version | 2 (the screen again) | 4 |
| Nothing proven, ≤ 4 waiting | 1 | 2 |
| Nothing proven, > 4 waiting | 2 (one question + screen) | 3 |
| First version | 1 | 1 (Q2 omitted: the screen shows the proposed version) |
| Change something | +1 dialog B and +1 screen per round | — |
| Finish an approved release | 1 (instead of the screen) | 1 |
| Unfinished record | 1 (instead of the screen) | 1 |
| Changelog file to settle | +1 before the screen | +1 |
| Version can't be read | +1 before the screen | +1 (Q2 then keeps the typed one) |
| Own release tool | 0 | 0 |

## `/commit` fallback

The order is fixed: **stage → select → commit**. The `Changelog:` trailer
attests what the commit CONTAINS, so it is chosen only after the index holds
exactly what will be committed, and nothing is staged after the choice. The
owner answers ONE screen — files, message, entries — before anything is
committed.

1. Pre-flight: run in parallel — `git status`, `git diff --cached --stat`, `git diff --stat`, `git log --oneline -5`, `git branch --show-current`, `git rev-parse --show-toplevel`, and in a dct project `dct clog candidates --json` (its `secret_like` list: `{path, staged, unstage}` for every changed file — staged, unstaged and untracked — whose path looks like a secret). New (untracked) files are changes like any other: the same staging rules apply to them, with no question of their own.
2. Detect project via CWD (`list_projects`: the row also carries the stored trailer preferences `commit_co_authored_by` / `commit_session_link`).
3. `mcp__dct__count_unreleased_changelog(project, branch=<current>, include_unbranched=true)` (detached HEAD → project-wide count, no `branch` — a gate only, never a candidate list) — if 0 and non-exempt files staged, prompt user to run `/clog` or add entry inline via `mcp__dct__add_changelog`. Exempt paths decide only whether an entry is REQUIRED; attaching an existing matching entry is always allowed. The count is only this gate — never proof that an entry belongs to the commit.
4. Determine commit type + scope + description. Read `current_version` from project.
5. **Stage first.** Stage the chosen files explicitly, path by path:
   `git add -- <path> <path>`. Never `git add -A` / `git add .`. Never stage binaries or generated artifacts. A path that is ALREADY partially staged (it shows in both `git diff --cached` and `git diff`) is a deliberate choice — leave it as it is; never re-add the whole file unless the owner asks for its unstaged hunks too. Ask about the staging only when there is a real choice — changed files that do not all belong to the entries; a blacklisted path is never an option of that question; otherwise stage without a question.
   **Secret-like paths** — ONE predicate, shared with `/release`: `secret_like` of `dct clog candidates --json`, judged by the file name only, never a content scanner (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `credentials*.json`; the templates `*.example` / `*.sample`, e.g. `.env.example`, are ordinary files). Without the CLI apply exactly these name patterns by hand. A secret-like path that is unstaged or untracked is never offered for staging and never staged; the screen lists it as not included.

   A secret-like path that is already staged STOPS the commit before the screen: say so in one plain sentence and show its `unstage` command exactly as the candidates give it (quoted, with the `--` separator: `git restore --staged -- .env`; before the first commit `git rm --cached -q -- .env`). Never run that command yourself and never change the user's index; if the file really belongs in the repository, the user commits it with git directly.
6. **Then select the trailer** against the complete staged diff (`git diff --cached`) — the index is now final. `dct clog candidates --json`: keep the `confirmed` candidates whose entry text / `source_file` describe what is staged (ask when unsure). `uncertain` candidates are matched too but attached only after the owner confirms them and `update_changelog(entry_id, branch=<current>, worktree=<toplevel>)` fixes both fields against the actual checkout — never automatically. Detached HEAD → no confirmed list, every entry of this checkout is uncertain: ask (confirm with `worktree` only). No match, but `other_branch` entries describe the change → offer `update_changelog(entry_id, branch=<current>)`. Ids in `already_proven` are never cited again. Without the CLI: `list_changelog(project, unreleased_only=true)` and the same classification by hand (client provenance of this branch + toplevel = confirmed; `branch_source` not `client` = uncertain).
7. **One screen.** The staged files, the full message and the entries the `Changelog:` trailer marks as done travel inside the dialog — the preview of "Commit" ("Details" pages when longer than 15 lines; the screen rule above) — in ONE question: "Commit" / "Change" / "Stop". Never commit without the owner's "Commit" on that screen. "Change" → apply it; a change that touches the files (a path added or dropped, a re-stage) changes the index, so the selection is stale: stage by step 5, select again by step 6, then show the screen again. "Stop" → nothing is committed. Trailers follow the project's stored preferences, applied without a question: `commit_co_authored_by` true → `Co-Authored-By:`; `commit_session_link` true → the session link the client gives for this session — a session link is written only on a stored true (yes); false → none. null = not answered yet (never a "no"): ask ONCE, before the screen, then store the answers with `dct project trailers <slug> --co-authored-by on|off --session-link on|off` and apply them. The project's own rules win: read them from the project's instruction files on disk at the repository top level — `AGENTS.md` and, where the project still keeps rules there, `CLAUDE.md` / `CLAUDE.local.md` (also `.claude/CLAUDE.md`); a project with only `AGENTS.md` is read the same way. Never rely on the client's automatic `@import` expansion: a line `@<path>` in one of them means: read `<path>` yourself. When the project's own rules in any of them forbid a trailer, it is never written whatever is stored, never offered as an option to approve again, and stored as `off` when the preferences are asked (dct's own repository forbids a session link, in its `AGENTS.md`). Not a dct project → `Co-Authored-By` by default, never a session link. "Change" can drop or add a trailer for this one commit.
8. **Then commit — WITHOUT another `git add`.** HEREDOC: `git commit -m "<type>(<scope>): <description> (v<version>) …"`; the final trailer block is `Changelog: <ids>` (when any were selected) followed by `Co-Authored-By:`. A trailer-format example inside the body is quoted (`> Changelog: 12`). A revert keeps git's standard message: `git revert --no-commit <sha>` stages it, the subject is `Revert "<original subject>"` and the body has `This reverts commit <full sha>.`.
9. Verify: `git log --oneline -3`, report the commit hash and the attached changelog ids.
10. On hook rejection: fix the reported issue. ANY change to the index afterwards (re-staging, a new file from the fix) means the selection is stale: re-run step 6 against the new `git diff --cached`, show the screen again, then create a NEW commit (never `--amend`, never `--no-verify`).

## `/track` (create item) fallback

1. Determine project (CWD detect); ask only when the project is ambiguous (CWD not registered, or several projects fit).
2. Gather: item_type (from the user's phrasing; no type cue → `todo` without a question; ask about the type only when the phrase is genuinely ambiguous — it carries cues for two different types — and the choice changes the scope), title, `short_title` (the label: a 2-4 word English noun phrase naming what the item is about, ≤ 50 chars, no item number, no project name — e.g. "Windows hook paths"), priority, description, component, tags (optional).
3. An unambiguous instruction creates the item without a confirmation question: `mcp__dct__create_item(item_type, title, project, short_title, ...)` at once — **REMEMBER the returned `id`**. The ack must echo `short_title` (null when none was stored); a MISSING key means the MCP server predates the field — report an unsupported (outdated) server contract and tell the user to restart/upgrade it. Do NOT retry the create (the item already exists) and do not claim the label was saved.
4. No checkpoints at creation: the plan comes when work on the item starts (`/track-update`, status → `in_progress`), which proposes at least 3 checkpoints via `mcp__dct__add_checkpoint(item_id, text)` when the work is a non-trivial feature or improvement, or when the user asks to break the work down. A small bug or a one-line fix gets none; a complex issue or todo may get some, with no fixed minimum. Add them now only when the user asks for that breakdown.
5. Acknowledge in one line, ending with how to correct it in words the user can type back: `✓ #<id> <type>/<priority> [<short_title>] — <title> · to change it: /track-update #<id> · to drop it: "#<id> won't fix"`. To correct a field, `/track-update #<id>` (`update_item`); an item that should not exist is closed as `wont_fix`.

## `/clog` (changelog entry) fallback

1. Determine project (CWD detect); ask only when the project is ambiguous (CWD not registered).
2. Gather: category (added/changed/fixed/removed — ask only when the category is genuinely ambiguous), entry text, component, source_file, source_lines, related_item_id (the last four optional).
3. In the agent's CURRENT working directory read `git branch --show-current` (empty = detached → omit `branch`) and `git rev-parse --show-toplevel` → `branch` / `worktree`. Pass them explicitly: the MCP server's directory is the session start directory, which may be another checkout.
4. An unambiguous instruction writes the entry without a confirmation question: `mcp__dct__add_changelog(category, entry, project, component, source_file, source_lines, related_item_id, branch, worktree)` at once.
5. If the ack carries `branch_source='server'` (or a `warning`), the entry is NOT confirmed: correct it with `update_changelog(id, branch=<current>, worktree=<toplevel>)`.
6. Acknowledge in one line per entry, ending with how to correct it in words the user can type back: `✓ clog #<id> [<category>] — <entry> (<branch>) · to change it: "#<id>: <new text>" · to remove it: "delete #<id>"`. To correct it, `update_changelog(id, ...)`; an entry that should not exist is withdrawn with `delete_changelog(id)`.

## `/track-resolve` (resolve/close item) fallback

1. Parse ID(s) from user input. Accept: `#42`, `42`, `ITEM-42`, `0042` — normalize via regex `^(#|ITEM-|item-)?0*(\d+)$`. Ask which item only when it is ambiguous (no id given and several open items fit the description).
2. For each id: `mcp__dct__get_item(id)` — its state and checkpoints.
3. A pending gate checkpoint (`kind` other than `task` / `cc-task`) blocks the item: `resolve_item` refuses it (so does `update_item` with a terminal status) and there is no resolving around it. Within the close the user asked for, take each pending gate by its recorded condition — its text — as the packaged gate policy (the `add_checkpoint` tool description) does:
   - Evidence sufficient — a technical condition (a test run, a check, a review report) that is really met, with the evidence its text requires and that evidence current for the state being closed (produced after the last change, by whoever the text names): `mcp__dct__complete_checkpoint(id)` without asking for a separate "done" — the request to close the item covers it; the acknowledgement names the gate and its evidence.
   - Evidence missing or stale — not produced, produced for an older state, or nothing the assistant can point to: the gate stays pending and goes into the stop below. The assistant's own test never replaces an independent review the text names.
   - A person's act or acceptance — only that person's confirmation completes a gate whose condition is a person's act or acceptance: dogfood (the user's own check in a fresh session), approval (a stakeholder's sign-off), or any gate whose text names a person who must perform or accept it. Finished implementation, green tests or the instruction to close the item do not replace it; it goes into the stop below. The kind alone does not decide who performs a gate: a `review` or `security` gate may need a named reviewer, or be met by a review report already in hand.
   - Consent already given — a confirmation of this gate that its person already gave explicitly is not asked again: the gate is completed on it.

   A gate is rejected (`mcp__dct__reject_checkpoint(id)`) only on the user's own decision — never on the assistant's own initiative, never to get the item closed. STOP before `resolve_item` while any gate stays pending: tell the user in plain words — no tool names, no `kind` identifiers — what is still to be checked, one line per gate:

   ```
   #42 can't be closed yet — it waits on a check: "try the new command in a fresh session" (#17). Once that check is really done, say "done"; to drop it on purpose, say "skip it". Then I close #42.
   ```

   That answer is the instruction: "done" → `mcp__dct__complete_checkpoint(id)`, "skip it" → the user's own decision to skip, `mcp__dct__reject_checkpoint(id)`; then `resolve_item(id, status, resolution)` in the same turn, with no further question, once no gate is left pending. A "done" or "skip it" covers only the gates named in the stop — with several gates and an answer that does not say which, ask which. "done" is never a substitute for the evidence a gate requires: when the gate's text names a sign-off, a review or a fresh-session test that has not happened, the gate stays pending. Pending `task` checkpoints do not block.
4. Determine resolution status: `resolved` (fixed/implemented) or `wont_fix` (scope decision).
5. Build resolution note: "Fixed in commit <hash>" if recent commit, or "Decision: <reason>" from context; ask only when the status or the reason is genuinely unclear.
6. An unambiguous instruction resolves without a confirmation question: `mcp__dct__resolve_item(id, status, resolution)` at once.
7. Acknowledge in one line per item, ending with how to reopen it: `✓ #<id> → <status> · to reopen: /track-update #<id> reopen` (plus how many `task` checkpoints stay open, if any). A wrong resolve is corrected by reopening the item: `/track-update #<id> reopen` (`update_item(id, status="open")`).

These prose descriptions are the authoritative behavior. Skill files in
`dct/skills/` are shortcuts. If they diverge — fix the skill, not this doc.
