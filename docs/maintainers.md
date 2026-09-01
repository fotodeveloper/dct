# Maintainer notes

Release-time hygiene for whoever maintains a public dct fork or the upstream
repo. Contributor-facing rules live in [CONTRIBUTING.md](../CONTRIBUTING.md);
this page covers what must happen *before code becomes public*.

## OSS hygiene — anonymize while writing

Any identifier that ships in the repo — tests, docs, skill examples, README,
docstrings, CLI help text — is visible to every user of the tool. Never commit
author-specific data: real slugs from other private projects, personal brand
names, home-directory paths (`/Users/<name>`, `/home/<name>`,
`C:\Users\<name>`), or version strings tied to a private project's release
cadence.

Write with anonymous placeholders from the first keystroke:

| Kind | Good | Bad |
|---|---|---|
| Slug | `proj-a`, `proj-b`, `mono`, `sub`, `myproj` | real slugs from other projects |
| Display name | `Project A`, `My Project`, `Example` | "SuperTool Pro", brand names |
| Path | `/tmp/proj-a`, `tmp_path` fixture | `/Users/<name>/dev/...` |
| Version | `1.0`, `1.1`, `0.1.0` (generic semver) | real private-project versions |
| Rename fixtures | `old-slug` → `new-slug`, name = `example.io` (keeps dot coverage) | ex-names with real brand |

### Enforcement

1. Think before typing — if it's a private name, swap it before the
   keystroke, not after during review.
2. Pre-commit check (manual — not yet automated): run a sniff regex over
   touched files before staging:

   ```bash
   rg -l '<private-slug-1>|<private-slug-2>|/Users/|/home/|C:\\\\Users|claude\.ai/code' \
      -- tests skills README.md CLAUDE.md dct/
   # commit messages are history too — session/artifact URLs must never land:
   git log --all --grep='claude\.ai/code' --oneline
   ```

   Swap the `<private-slug-*>` placeholders for your own private project
   slugs — the list is your registered projects (`list_projects`) minus the
   current one. Empty output = clean. Any hit = swap to placeholder or
   justify below.
3. Pre-release block (future): `/release` Step 0 runs the audit automatically
   and refuses to stamp if it finds leaks.

**Remember**: the working tree is only the tip — `git grep <pattern> <ref>`
audits a whole historical tree. Leaks that predate a cleanup live on in
history; a fresh-start initial commit is the reliable remedy before first
publication.

### Intentionally exempt

- `changelog.md` — shipped history; rewriting breaks chronology.
- Internal design history (`docs/superpowers/`) — kept out of the public repo
  entirely (gitignored), exempt by absence.
- Commit messages and tags already in git history.

If a leak lands in those: note it, move on. Only anonymize going forward.

**Why this matters**: a leaked private slug in a README example looks
unprofessional, leaks project-portfolio info, and forces every user to wonder
"what's this `xyz-tool` thing — do I need it?". Generic placeholders are also
better pedagogy — a reader parses `proj-a` as a role, not as a mystery.

## Releasing

The release flow (stamp changelog → append section to changelog.md → version
sync → `release: vX.Y.Z` commit → tag) is documented step by step in
[docs/assistant-fallbacks.md](assistant-fallbacks.md#release-fallback); the
`/release` skill automates it. Pushing a `v*` tag triggers
`.github/workflows/publish.yml` (PyPI trusted publishing), whose test matrix
must stay identical to `ci.yml`'s — a release gate weaker than the branch
gate published 1.2.0 while CI was red on 3.11.

### Advertised numbers

Every number dct quotes about itself is derived, never retyped. Between the
version sync and the release commit, run:

```bash
python scripts/check_numbers.py --fix --site /path/to/dc-tools
```

dct-side edits belong in the release commit; site-side edits are committed
and pushed in the site repo, which deploys them. CI runs the same script
without `--fix` (README only — the site lives in a separate repo), so drift
fails the branch gate.

Two tiers, by how fast each surface moves: **in-repo numbers are exact**,
because they are edited in the same commit that changes them; **published
numbers that churn are approximate** — they carry a `~`, are rounded down to
the nearest ten, and are accepted within 5%. A page deployed once per release
cannot hold an exact test count honestly: the count moved 938 → 948 within an
hour of one being pinned to it. `--fix` rewrites only the matched number, and
refuses to write anything when a pattern stops matching, so a redesigned page
fails loudly instead of passing on a technicality.
