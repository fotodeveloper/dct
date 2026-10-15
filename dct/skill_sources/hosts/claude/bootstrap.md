Check three things in parallel:

1. MCP registration: read `~/.claude.json`; does `mcpServers.dct` exist?
2. Skills: does `~/.claude/skills/track` exist (as symlink or dir)?
3. Hooks: does `~/.claude/hooks/dct-check-changelog.sh` exist?

Derive `bootstrap_complete` = all three present. The short dct pointer in
`~/.claude/CLAUDE.md` is NOT part of it: it is the owner's choice, offered by
`dct init` itself, and it only names the reference — never paste the
reference into `CLAUDE.md`.

The skill never runs `dct init`: it is interactive (it asks for the
database in a terminal) and a Bash call has no terminal, so its first
prompt fails. It never runs `dct upgrade` either: that also upgrades the
package.

If NOT complete:
- the dct MCP tools answer in this session (`mcp__dct__list_projects`
  returns, even an empty list) — dct's database is set up and a part of the
  Claude integration is missing: ask the repair question below, its
  question naming what is missing. On "Repair" probe again after it; on
  "Not now" stop with the line "Bootstrap: incomplete — `dct integrate
  repair claude` completes it".
- otherwise: no question. The final text says that dct is not set up for
  Claude Code here: if dct was never set up on this machine, run `dct init`
  in your own terminal (it asks for the database and installs the
  integration); if it was (its config and database exist), `dct integrate
  repair claude` restores the Claude integration without touching them.
  Then `{{host.skill-prefix}}dct-init` again. Stop.

If complete: run the reference check, then continue to Step 2.

**The reference** — an asset check, never part of the bootstrap: is
`<canonical reference path reported by doctor>` (the dct reference read on demand, which the
pointer in `~/.claude/CLAUDE.md` names) installed and current? The skill
never decides that itself — a copy's header or a link's target proves
nothing (an edited copy keeps its header; only dct knows which links it
recorded). It reads dct's own diagnosis, the one the installer and the
repair act on. Run via {{host.shell}} (read-only: it opens no database):

```
dct doctor --client claude --json
```

In its JSON, take `integrations` → the entry with `"client": "claude"` →
its `checks` → the row with `"check": "shared_reference"` (canonical): its `state` is
`current`, `missing`, `outdated` or `foreign`, its `message` names the path
(and the link target), its `remedy` the fix. The command exits with code 1
whenever any check fails (the optional `CLAUDE.md` pointer, a hook file, a
Python dependency); a valid JSON on stdout still carries the diagnosis —
read the reference row from it, never take the exit code for no answer. Separately inspect `"check": "reference"` for Claude's legacy alias: if it
is not current, report its own state, path and remedy. Do not substitute its
state for the canonical row or infer either file's ownership. The other rows
are not this check's business and never block the registration.
Only when stdout holds no JSON or no canonical shared_reference row, the reports say
"Reference: unknown — `dct doctor --client claude` did not report it" and
the skill continues to Step 2.

- **`current`** — nothing to say; continue to Step 2.
- **`missing`** (typical after the package was upgraded without `dct
  upgrade`) or **`outdated`** (dct's, from an older package: an old
  unchanged copy or a link from a previous install) — ask the repair
  question below, then continue to Step 2 whatever the answer: the
  reference never blocks the registration.
- **`foreign`** (not dct's: the owner's own file, a copy edited since dct
  wrote it, or a link dct did not create) — every dct installer leaves it
  untouched, so repair is never offered as its fix and no question is
  asked. The report says "Reference: <the row's `message`, naming the path
  and the link target> — <its `remedy`>": move it aside (to an unused
  name) or replace it first, keeping what the owner needs from it; then
  `dct integrate repair claude` installs dct's. Continue to
  Step 2.

**The repair question** — ONE {{host.question}}; the repair runs only after
the owner's "Repair" answer, because it changes more than the missing piece:

  question: "<What is missing or outdated — e.g. dct's reference for Claude, or its hooks.> Repair dct's Claude integration now?"
  header: "Repair"
  options:
    - "Repair (recommended)" — "Runs `dct integrate repair claude`: registers dct's MCP server in ~/.claude.json (an existing dct entry is kept), links dct's skills and hooks into ~/.claude, wires the hooks into ~/.claude/settings.json, installs <canonical reference path reported by doctor> and adds or refreshes dct's short pointer in ~/.claude/CLAUDE.md, even if you declined it before. It leaves dct's database, its config, the package and this folder alone."
    - "Not now" — "Nothing changes; the report names the command."

On "Repair": run `dct integrate repair claude`, show its output and probe
again (the bootstrap probe and `dct doctor --client claude --json`). If the
reference row then reads `foreign`, handle it as above.

The `Reference:` line of the reports (Step 2's status, Step 7) is shown only
when the reference is not `current`: "Reference: missing — `dct integrate
repair claude` installs it (the full current-client integration repair)",
"Reference: outdated — `dct integrate repair claude` refreshes it (the full
Claude integration repair)", the foreign line or the unknown line above.

The canonical shared reference is diagnosed at its resolved config-directory path. Claude retains its proven-owned legacy `~/.claude/dct-reference.md` alias; the bounded pointer names the canonical reference. Do not create or infer ownership of an alias from its filename.
