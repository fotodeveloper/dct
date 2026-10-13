# Moved

The CLAUDE.md guidance now ships inside the package in two parts:

- **[dct/assets/dct-reference.md](../dct/assets/dct-reference.md)** — the full
  reference (when to reach for each skill and MCP tool). `dct init` installs it
  as `~/.claude/dct-reference.md`, a symlink into the package (a marked copy
  where symlinks are unavailable); the assistant reads it on demand. Do not
  paste it into `CLAUDE.md`: a rules file is loaded in every session.
- **[dct/assets/claude-md-snippet.md](../dct/assets/claude-md-snippet.md)** —
  the short pointer `dct init` offers to attach to your personal
  `~/.claude/CLAUDE.md` (step 6). It names `~/.claude/dct-reference.md` as
  plain text, never as an `@` import.

Run `dct init` (it installs the reference and offers the pointer), or paste the
pointer file's content below its first `---` separator into
`~/.claude/CLAUDE.md` yourself. A managed block that still holds the old full
text is refreshed to the pointer by the next `dct init` or `dct upgrade`.
