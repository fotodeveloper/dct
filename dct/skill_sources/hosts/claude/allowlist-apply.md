4. Q4 "for all my projects" → edit `~/.claude/settings.json`, append
   `"mcp__dct__*"` to `permissions.allow` (preserve order; dedupe); "only in
   this project" → `.claude/settings.local.json` in the CWD (create the file
   if missing) with the same entry.


Preserve existing allowlist order: append, do not sort; dedupe before write.
