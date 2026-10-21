   Which transport the agent can use depends on how it runs: an agent in an
   INTERACTIVE session with the dct MCP server (the owner's Codex session, a
   Claude Code session) writes `suggest_findings` itself — then step 2 is
   already done (check `export_triage`: `suggested.by` is the agent and the
   proposals are there; never write them a second time). An agent run
   NON-INTERACTIVELY (`codex exec`, a background subagent) cannot call MCP
   tools (its approval policy refuses them): ask it to write the proposals
   as a file in the SUGGESTED shape (e.g. `suggested.json` in the session
   scratchpad, cards taken from the `cards.json` beside the page) and
   do step 2 yourself with `by` = that agent's name. The ledger records the
   proposer either way; it never records who transported the file.
