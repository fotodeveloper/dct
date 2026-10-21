3. When the host refuses the CLI call, give the user the exact command to run
   (in Claude Code with the `!` prefix: `! dct triage page …`) and continue
   from its output.
4. Publish the file with the Artifact tool (`file_path` = that path; the
   first publish adds an icon such as `checklist`). The description is
   written in the language of the conversation, its first word included
   ("Open — …" in English, "Otwarty — …" in Polish), and says the page
   collects the owner's answers. A republish uses the
   same `file_path`, so the URL stays the same; the page's storage key comes
   from the cycle id, so a rebuild keeps every viewer's draft. With the
   plain-language layer, every publish (open, prefill, closed) also passes
   `files: {"plain.json": <the copy beside the page>}` — a publish that omits
   it keeps the earlier published copy, so a changed text would never reach
   the page.
5. After the FIRST publish write the URL back:
   `mcp__dct__update_triage(artifact_url=<url>, cycle_id=<id>)` (open cycles
   only).
6. Host without the Artifact tool (or without a scratchpad): give the user the
   local page path and the exact command that built it; publication happens
   only where the host supports it.
