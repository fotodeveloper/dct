The project's own rules win: read them from the project's instruction
  files on disk at the repository top level — `AGENTS.md` and, where the
  project still keeps rules there, `CLAUDE.md` / `CLAUDE.local.md` (also
  `.claude/CLAUDE.md`); a project with only `AGENTS.md` is read the same
  way. Never rely on the client's automatic `@import` expansion: a line
  `@<path>` in one of them means: read `<path>` yourself. When the project's
  own rules in any of them forbid a trailer, it is never written whatever
  is stored, never offered as an option to approve again, and stored as
  `off` when the preferences are asked. dct's own repository forbids a
  session link (OSS hygiene, in its `AGENTS.md`).
