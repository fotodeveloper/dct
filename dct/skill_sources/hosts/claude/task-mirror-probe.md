- `mcp__dct__list_items(project="", search="CC task list", tag="cc-task-list")` —
  the cc-task-list mirror probe (see the `CC tasks:` line in Step 2). The `tag`
  filter runs SERVER-SIDE; no client-side post-filtering is needed. The tag
  dictionary is closed: if `cc-task-list` was never registered the call
  returns `{"error": ...}` — that means "no mirror items exist", so treat it as
  an empty result and carry on, never as a failed preflight.
