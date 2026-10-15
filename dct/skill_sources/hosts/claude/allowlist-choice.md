**Q4 — the allowlist** (only when Step 3 found no dct entry):

  question: "Let dct's tools run without asking you each time?"
  header: "Allowlist"
  options:
    - "Yes — for all my projects (recommended)" — "Adds mcp__dct__* to permissions.allow in ~/.claude/settings.json; DCT tools can write tracking state and approved plan-file anchors; release effects still use the explicit CLI consent workflow."
    - "Yes — only in this project" — "Adds it to .claude/settings.local.json in this folder."
    - "No — ask me each time"
