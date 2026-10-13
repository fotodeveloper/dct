"""Guards for the repository's agent instruction files (#917).

AGENTS.md is the ONLY project instruction file: Codex and Claude Code
(v2.1.277+, default "Project instructions" setting) both read it natively, from
the repository root and from subdirectories. The failure modes locked here:

- a conflicting Claude file: Claude Code reads AGENTS.md only while no
  `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` exists in the working
  directory or any directory above it — one such file (even an untracked or
  gitignored `CLAUDE.local.md`) silently switches the project rules off for
  every session started at or below it. The user's own `~/.claude/CLAUDE.md`
  does not count;
- a stray `@path` in AGENTS.md: Claude Code would import that file into every
  session, while Codex shows it as plain text — on-demand documents are
  referenced as "Before <activity>, read <path>" instead;
- a malformed or dangling "Read on demand" entry: an index entry the checker
  cannot resolve, or one that names a moved or deleted file, sends every agent
  to a path that does not exist.

The path checks cover a LIMITED convention, not arbitrary Markdown: inline
code spans, inline links, reference-style links with their definitions, and
bare slash-separated file paths in prose. The "Read on demand" index has a
strict entry format (see `read_on_demand_problems`).
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

import pytest
from conftest import REAL_HOME, git

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FENCE_RE = re.compile(r"^(```|~~~)")
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# `[label]: target` — a reference-style link definition (CommonMark allows up
# to three spaces of indentation).
_LINK_DEF_RE = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*(\S+)")
# A bare repo-relative file path in prose: at least one `/`, a known file
# extension, not part of a URL, a home path (`~/`) or an absolute path.
_BARE_PATH_RE = re.compile(
    r"(?<![\w/~.:@-])([\w.-]+(?:/[\w.-]+)+\.(?:md|py|sh|yml|yaml|toml|json|txt|sql))\b")
# A repo-relative path: plain path characters (globs allowed), optional
# `::name` suffix (a test or symbol inside the file).
_PATH_RE = re.compile(r"^[\w.\-*]+(?:/[\w.\-*]*)*(?:::[\w.]+)?$")
_FILE_EXT_RE = re.compile(r"\.(md|py|sh|yml|yaml|toml|json|txt|sql)$")

# The three files that make Claude Code read CLAUDE.md instead of AGENTS.md
# (https://code.claude.com/docs/en/memory#agents-md), relative to a directory.
CONFLICTING_NAMES = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md")
# The built-in plugin that reads AGENTS.md (its ID before v2.1.285 second).
_AGENTS_MD_PLUGINS = ("cc-plugin-agents-md@builtin", "agents-md@builtin")
_BOTH_MODE = "claude-md-and-agents-md"


def reads_claude_md_and_agents_md(settings_files: list[Path]) -> bool:
    """Whether Claude Code's "Project instructions" setting is
    `claude-md-and-agents-md` — then a CLAUDE.local.md no longer switches
    AGENTS.md off. `settings_files` in precedence order (local project,
    shared project, user); the first one that sets the option decides.
    Read-only; an unreadable or malformed file sets nothing."""
    for path in settings_files:
        try:
            with open(path, encoding="utf-8") as fh:
                configs = json.load(fh).get("pluginConfigs") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for plugin in _AGENTS_MD_PLUGINS:
            entry = configs.get(plugin) if isinstance(configs, dict) else None
            options = entry.get("options") if isinstance(entry, dict) else None
            mode = options.get("instructionFiles") if isinstance(options, dict) else None
            if isinstance(mode, str):
                return mode == _BOTH_MODE
    return False


def _local_settings_files() -> list[Path]:
    """This checkout's and the real user's Claude Code settings, by precedence."""
    root = Path(REPO_ROOT)
    return [root / ".claude" / "settings.local.json", root / ".claude" / "settings.json",
            REAL_HOME / ".claude" / "settings.json"]


def _allowed(found: list[str], both_mode: bool) -> list[str]:
    """Drop the CLAUDE.local.md hits a `claude-md-and-agents-md` setup allows
    (a CLAUDE.md stays a conflict: the repository keeps none)."""
    if not both_mode:
        return found
    return [path for path in found if os.path.basename(path) != "CLAUDE.local.md"]


def _read(name: str) -> str:
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _prose_lines(text: str):
    """Lines outside fenced code blocks; a fence line yields "" (a paragraph
    break), so text on both sides of a fence never joins."""
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line.lstrip()):
            in_fence = not in_fence
            yield ""
        elif not in_fence:
            yield line


def _prose_blocks(text: str):
    """Paragraphs outside fenced code blocks, each joined into one line;
    reference-style link definitions are left out (see link_definitions).

    A code span may continue on the next line (CommonMark turns the line end
    into a space), so spans are matched per paragraph, never per line: a path
    broken across lines then carries a space and is no path at all.
    """
    block: list[str] = []
    for line in _prose_lines(text):
        if line.strip() and not _LINK_DEF_RE.match(line):
            block.append(line.strip())
            continue
        if block:
            yield " ".join(block)
            block = []
    if block:
        yield " ".join(block)


def stray_at_signs(text: str) -> list[str]:
    """Prose paragraphs that carry an `@` outside inline code spans and fences."""
    return [
        block for block in _prose_blocks(text)
        if "@" in _INLINE_CODE_RE.sub("", block)
    ]


def _as_repo_path(candidate: str) -> str | None:
    """Normalize one referenced string to a repo-relative path, or None."""
    candidate = candidate.strip()
    if candidate.startswith(("~", "/", "#", "<")) or "://" in candidate:
        return None
    candidate = candidate.split("#", 1)[0]
    if not candidate or not _PATH_RE.match(candidate):
        return None
    candidate = candidate.split("::", 1)[0]
    if "/" not in candidate and not _FILE_EXT_RE.search(candidate):
        return None  # an identifier such as `get_*` or `feat:`, not a path
    return candidate


def link_definitions(text: str) -> dict[str, str]:
    """Reference-style link definitions, label (case-folded) → target."""
    defs: dict[str, str] = {}
    for line in _prose_lines(text):
        match = _LINK_DEF_RE.match(line)
        if match:
            defs[match.group(1).strip().casefold()] = match.group(2)
    return defs


def referenced_paths(text: str) -> set[str]:
    """Repo-relative paths named in code spans, inline link targets, link
    definitions or bare in prose (the limited convention of this module)."""
    found: set[str] = set()
    candidates: list[str] = list(link_definitions(text).values())
    for block in _prose_blocks(text):
        candidates += [span for _, span in _INLINE_CODE_RE.findall(block)]
        candidates += _LINK_RE.findall(block)
        # Bare paths: what remains once code spans and link targets are gone.
        rest = _LINK_RE.sub("]", _INLINE_CODE_RE.sub(" ", block))
        candidates += _BARE_PATH_RE.findall(rest)
    for candidate in candidates:
        path = _as_repo_path(candidate)
        if path:
            found.add(path)
    return found


def missing_paths(paths: set[str], root: str = REPO_ROOT) -> list[str]:
    """The paths that name nothing in `root`; a `**` glob needs its directory."""
    missing = []
    for path in sorted(paths):
        if path.endswith("/**"):
            ok = os.path.isdir(os.path.join(root, path[: -len("/**")]))
        elif "*" in path:
            ok = bool(glob.glob(os.path.join(root, path), recursive=True))
        else:
            ok = os.path.exists(os.path.join(root, path))
        if not ok:
            missing.append(path)
    return missing


# --- the "Read on demand" index ----------------------------------------------

_INDEX_HEADING = "## Read on demand"
# One logical entry: "Before <activity>, read <target>[ (<note>)]." where the
# target is ONE backticked path, an inline link or a reference-style link.
_ENTRY_RE = re.compile(
    r"^Before (?P<activity>.+?), read "
    r"(?:`(?P<code>[^`]+)`|\[[^\]]+\]\((?P<link>[^)\s]+)\)|\[(?P<text>[^\]]+)\]\[(?P<label>[^\]]*)\])"
    r"(?: \([^()]*\))?\.$"
)


def read_on_demand_entries(text: str) -> tuple[list[str], list[str]]:
    """(logical entries, problems) of the "## Read on demand" section.

    An entry is a `- ` list item plus its indented continuation lines, joined
    with single spaces. Any other non-blank line in the section is a problem,
    so nothing the parser does not understand can drop out unnoticed.
    """
    lines = text.splitlines()
    try:
        start = lines.index(_INDEX_HEADING) + 1
    except ValueError:
        return [], [f"no {_INDEX_HEADING!r} section"]
    entries: list[str] = []
    problems: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if not line.strip():
            continue
        if line.startswith("- "):
            entries.append(line[2:].strip())
        elif line.startswith("  ") and entries:
            entries[-1] += " " + line.strip()
        else:
            problems.append(f"not an index entry: {line!r}")
    if not entries:
        problems.append("the index has no entries")
    return entries, problems


def read_on_demand_problems(text: str, root: str = REPO_ROOT) -> tuple[set[str], list[str]]:
    """(resolved target paths, problems) of the "Read on demand" index.

    Every entry must read "Before <activity>, read <target>." (an optional
    parenthesized note may follow the target) with ONE unambiguous target: a
    backticked repo path, an inline link, or a reference-style link whose
    label is defined in the file. A target that does not exist is a problem.
    """
    entries, problems = read_on_demand_entries(text)
    defs = link_definitions(text)
    targets: set[str] = set()
    for entry in entries:
        match = _ENTRY_RE.match(entry)
        if not match:
            problems.append(f"not 'Before …, read <target>.': {entry!r}")
            continue
        raw = match["code"] or match["link"]
        if raw is None:
            label = (match["label"] or match["text"]).strip().casefold()
            raw = defs.get(label)
            if raw is None:
                problems.append(f"undefined link label {label!r}: {entry!r}")
                continue
        path = _as_repo_path(raw)
        if path is None:
            problems.append(f"target is not a repo path ({raw!r}): {entry!r}")
            continue
        if missing_paths({path}, root):
            problems.append(f"target does not exist ({path}): {entry!r}")
            continue
        targets.add(path)
    return targets, problems


# --- conflicting Claude instruction files ------------------------------------


def _is_virtualenv(dirpath: str) -> bool:
    return os.path.exists(os.path.join(dirpath, "pyvenv.cfg"))


def conflicting_instruction_files(root: str) -> list[str]:
    """Every CLAUDE.md / CLAUDE.local.md under `root` (so `.claude/CLAUDE.md`
    too), whether tracked, untracked or gitignored — a plain directory walk,
    because Claude Code sees the working tree, not the index. Skipped:

    - `.git` — repository metadata, never a session directory;
    - virtualenvs (a `pyvenv.cfg`, e.g. `.venv`) and `node_modules` —
      third-party trees nobody starts a session in, and large;
    - `<root>/.claude/worktrees/` — Claude Code's worktrees, each a separate
      checkout with its own state; its own run of this test guards it, and
      the ancestor check covers the main checkout above it.
    """
    found: list[str] = []
    worktrees = os.path.join(root, ".claude", "worktrees")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "node_modules")
            and os.path.join(dirpath, d) != worktrees
            and not _is_virtualenv(os.path.join(dirpath, d))
        ]
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            if name in filenames or name in dirnames:
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def ancestor_instruction_files(start: str, user_home: Path) -> list[str]:
    """The conflicting files in the directories ABOVE `start` (its lexical and
    its resolved parents), except the user's own `~/.claude/CLAUDE.md`, which
    does not count and keeps loading alongside AGENTS.md."""
    own = {
        os.path.join(str(home), ".claude", "CLAUDE.md")
        for home in (user_home, Path(os.path.realpath(user_home)))
    }
    found: set[str] = set()
    for base in (os.path.abspath(start), os.path.realpath(start)):
        for parent in Path(base).parents:
            for name in CONFLICTING_NAMES:
                candidate = os.path.join(str(parent), name)
                if candidate not in own and os.path.lexists(candidate):
                    found.add(candidate)
    return sorted(found)


# --- the repository's own files ----------------------------------------------


def test_no_conflicting_claude_instruction_file_in_the_working_tree():
    """AGENTS.md is the only project instruction file (no CLAUDE.md loader,
    no symlink, no second copy) — any of these files would make Claude Code
    skip AGENTS.md for sessions started at or below it. A private
    CLAUDE.local.md is accepted only where Claude Code is set to
    `claude-md-and-agents-md` (CONTRIBUTING.md, "Agent instructions")."""
    both = reads_claude_md_and_agents_md(_local_settings_files())
    assert _allowed(conflicting_instruction_files(REPO_ROOT), both) == []


def test_no_conflicting_claude_instruction_file_above_the_checkout():
    """LOCAL check of this machine's directories above the checkout (a dct
    worktree elsewhere, or a Claude Code worktree under the main checkout's
    `.claude/worktrees/`, is covered by its own run). CI's ancestors differ,
    so a green CI proves nothing here, and no test can protect the start of
    a session: it only reports a conflicting file once the suite runs. The
    user's real home comes from conftest (captured before the suite-wide fake
    HOME); the check only reads, it never writes."""
    both = reads_claude_md_and_agents_md(_local_settings_files())
    assert _allowed(ancestor_instruction_files(REPO_ROOT, REAL_HOME), both) == []


def test_agents_md_has_no_import_outside_code():
    assert stray_at_signs(_read("AGENTS.md")) == []


def test_read_on_demand_index_is_well_formed_and_resolves():
    """Every entry parses and resolves — and the parser really sees the whole
    index, so an empty or partial extraction cannot pass."""
    targets, problems = read_on_demand_problems(_read("AGENTS.md"))
    assert problems == []
    assert targets == {
        "docs/architecture.md",
        "docs/hooks.md",
        "docs/testing.md",
        "docs/plans.md",
        "docs/mcp-tools.md",
        "docs/assistant-fallbacks.md",
        "docs/integrations.md",
        "docs/release-workflow.md",
        "docs/maintainers.md",
    }


def test_agents_md_extractor_sees_the_helper_paths():
    """The general extractor finds the paths named outside the index too."""
    expected = {
        "dct/hooks/**",
        "dct/core/limits.py",
        "dct/core/text.py",
        "dct/core/engine.py",
        "dct/core/provenance.py",
        "CONTRIBUTING.md",
    }
    assert expected <= referenced_paths(_read("AGENTS.md"))


def test_every_path_agents_md_references_exists():
    assert missing_paths(referenced_paths(_read("AGENTS.md"))) == []


# --- the checkers themselves (so a green run means something) ----------------


@pytest.mark.parametrize(
    "line",
    [
        "Read @docs/hooks.md first.",
        "@AGENTS.md",
        "mail someone@example.org",
    ],
)
def test_stray_at_is_reported_in_prose(line):
    assert stray_at_signs(line) == [line]


def test_at_inside_code_is_allowed():
    text = (
        "Every `@mcp.tool()` docstring and ``a ` @x`` span.\n"
        "```python\n"
        "@mcp.tool()\n"
        "```\n"
        "~~~\n"
        "@decorator\n"
        "~~~\n"
    )
    assert stray_at_signs(text) == []


def test_at_inside_a_code_span_continued_on_the_next_line_is_allowed():
    assert stray_at_signs("Use the `@mcp.tool()\ndecorator` here.\n") == []


def test_extractor_normalizes_and_skips():
    text = (
        "Predicate `tests/conftest.py::throwaway_refusal_reason`, see\n"
        "[docs](docs/hooks.md#the-five-rules) and `CLAUDE.md`.\n"
        "Not paths: `~/.claude/x.md`, `/tmp/proj-a`, `get_*`, `feat:`,\n"
        "`{cols, rows}`, `host.example/x/…`, [site](https://example.org/a.md),\n"
        "https://example.org/b/c.md, ~/.codex/AGENTS.md, /etc/x/y.md.\n"
        "```\n"
        "`docs/inside-fence.md` and docs/inside-fence-bare.md\n"
        "```\n"
    )
    assert referenced_paths(text) == {"tests/conftest.py", "docs/hooks.md", "CLAUDE.md"}


def test_extractor_reads_a_path_written_in_plain_prose():
    text = "Before touching migrations, read docs/migrations.md.\n"
    assert referenced_paths(text) == {"docs/migrations.md"}


def test_extractor_reads_reference_style_link_definitions():
    text = "See the [guide][g] first.\n\n[g]: docs/gone.md\n"
    assert referenced_paths(text) == {"docs/gone.md"}


def test_extractor_reads_a_code_span_continued_on_the_next_line():
    text = "Before a change, read `docs/hooks.md` and the\n`dct/core/text.py` helpers.\n"
    assert referenced_paths(text) == {"docs/hooks.md", "dct/core/text.py"}


def test_missing_paths_flags_what_does_not_exist(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "one.sh").write_text("x")
    paths = {
        "docs/a.md", "docs/gone.md",
        "hooks/**", "nothooks/**",
        "hooks/*.sh", "hooks/*.py",
    }
    assert missing_paths(paths, root=str(tmp_path)) == [
        "docs/gone.md", "hooks/*.py", "nothooks/**",
    ]


def _index(*entries: str, tail: str = "") -> str:
    return "# T\n\n## Read on demand\n\n" + "\n".join(entries) + "\n\n## Next\n" + tail


@pytest.fixture
def docs_root(tmp_path):
    (tmp_path / "docs").mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / "docs" / name).write_text("x")
    return str(tmp_path)


def test_index_accepts_every_supported_entry_form(docs_root):
    text = _index(
        "- Before a, read `docs/a.md`.",
        "- Before b, with a note, read [the b doc](docs/b.md) (why it matters).",
        "- Before c, which spans",
        "  two lines, read [c][c-doc].",
        tail="\n[c-doc]: docs/c.md\n",
    )
    assert read_on_demand_problems(text, docs_root) == (
        {"docs/a.md", "docs/b.md", "docs/c.md"}, [])


@pytest.mark.parametrize(
    ("entry", "tail", "reason"),
    [
        ("- Before touching migrations, read docs/a.md.", "", "not 'Before"),
        ("- Before a, read `docs/a.md`, `docs/b.md`.", "", "not 'Before"),
        ("- Before a, read `docs/a.md`", "", "not 'Before"),
        ("- Read `docs/a.md` before a.", "", "not 'Before"),
        ("- Before a, read `docs/\n  a.md`.", "", "not a repo path"),
        ("- Before a, read [the doc][nolabel].", "", "undefined link label"),
        ("- Before a, read `~/.claude/a.md`.", "", "not a repo path"),
        ("- Before a, read `docs/gone.md`.", "", "does not exist"),
        ("- Before a, read [gone][g].", "\n[g]: docs/gone.md\n", "does not exist"),
    ],
    ids=["prose-path", "two-targets", "no-period", "wrong-order", "broken-span",
         "undefined-reference", "home-path", "missing-file", "missing-reference-target"],
)
def test_index_reports_an_entry_it_cannot_resolve(docs_root, entry, tail, reason):
    targets, problems = read_on_demand_problems(_index(entry, tail=tail), docs_root)
    assert targets == set()
    assert len(problems) == 1 and reason in problems[0]


def test_index_reports_a_line_that_is_no_entry(docs_root):
    text = _index("- Before a, read `docs/a.md`.", "Also read docs/b.md before b.")
    targets, problems = read_on_demand_problems(text, docs_root)
    assert targets == {"docs/a.md"}
    assert len(problems) == 1 and "not an index entry" in problems[0]


@pytest.mark.parametrize("text", ["# T\n\nno index\n", _index()])
def test_index_missing_or_empty_is_a_problem(text, docs_root):
    targets, problems = read_on_demand_problems(text, docs_root)
    assert targets == set() and len(problems) == 1


# --- the conflict guards themselves --------------------------------------------


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")


def test_conflict_guard_finds_tracked_untracked_and_ignored_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    _touch(root / ".gitignore")
    (root / ".gitignore").write_text("CLAUDE.local.md\nignored/\n")
    _touch(root / "CLAUDE.md")
    git(root, "add", "CLAUDE.md")                          # tracked
    _touch(root / ".claude" / "CLAUDE.md")                 # untracked
    _touch(root / "CLAUDE.local.md")                       # gitignored
    _touch(root / "ignored" / "deep" / "CLAUDE.local.md")  # inside an ignored dir
    _touch(root / "sub" / "CLAUDE.md")                     # a subdirectory's own
    (root / "AGENTS.md").write_text("x\n")
    assert conflicting_instruction_files(str(root)) == sorted([
        "CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md",
        "ignored/deep/CLAUDE.local.md", "sub/CLAUDE.md",
    ])


def test_conflict_guard_finds_a_symlink_named_claude_md(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("x\n")
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    (root / "dangling").mkdir()
    (root / "dangling" / "CLAUDE.local.md").symlink_to("nowhere.md")
    assert conflicting_instruction_files(str(root)) == ["CLAUDE.md", "dangling/CLAUDE.local.md"]


def test_conflict_guard_skips_only_git_dependencies_and_worktrees(tmp_path):
    root = tmp_path / "repo"
    _touch(root / ".git" / "CLAUDE.md")
    _touch(root / ".venv" / "pyvenv.cfg")
    _touch(root / ".venv" / "lib" / "CLAUDE.md")
    _touch(root / "envs" / "py311" / "pyvenv.cfg")
    _touch(root / "envs" / "py311" / "CLAUDE.md")
    _touch(root / "web" / "node_modules" / "pkg" / "CLAUDE.md")
    _touch(root / ".claude" / "worktrees" / "w1" / "CLAUDE.md")
    # Not skipped: a `worktrees` dir elsewhere, a `.venv` without pyvenv.cfg.
    _touch(root / "docs" / "worktrees" / "CLAUDE.md")
    _touch(root / "fake" / ".venv" / "CLAUDE.local.md")
    assert conflicting_instruction_files(str(root)) == [
        "docs/worktrees/CLAUDE.md", "fake/.venv/CLAUDE.local.md",
    ]


def test_ancestor_guard_ignores_only_the_users_own_claude_md(tmp_path):
    home = tmp_path / "home"
    repo = home / "dev" / "_worktrees" / "proj-a" / "w1"
    repo.mkdir(parents=True)
    _touch(home / ".claude" / "CLAUDE.md")       # the user's own: does not count
    _touch(repo / "CLAUDE.md")                    # inside, not above: not this check
    assert ancestor_instruction_files(str(repo), home) == []
    _touch(home / "CLAUDE.md")
    _touch(home / "dev" / "CLAUDE.local.md")
    _touch(home / "dev" / "_worktrees" / ".claude" / "CLAUDE.md")
    assert ancestor_instruction_files(str(repo), home) == sorted([
        str(home / "CLAUDE.md"),
        str(home / "dev" / "CLAUDE.local.md"),
        str(home / "dev" / "_worktrees" / ".claude" / "CLAUDE.md"),
    ])


def test_ancestor_guard_covers_a_worktree_inside_the_main_checkout(tmp_path):
    """A Claude Code worktree lives under `<repo>/.claude/worktrees/<name>`:
    the main checkout's files are its ancestors' files."""
    main = tmp_path / "main"
    worktree = main / ".claude" / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    _touch(main / "CLAUDE.local.md")
    assert ancestor_instruction_files(str(worktree), tmp_path / "home") == [
        str(main / "CLAUDE.local.md"),
    ]


def test_ancestor_guard_follows_a_symlinked_checkout(tmp_path):
    real = tmp_path / "real" / "repo"
    real.mkdir(parents=True)
    _touch(tmp_path / "real" / "CLAUDE.md")
    (tmp_path / "link").symlink_to(real)
    assert ancestor_instruction_files(str(tmp_path / "link"), tmp_path / "home") == [
        str(tmp_path / "real" / "CLAUDE.md"),
    ]


def _settings(path: Path, plugin: str, mode) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"pluginConfigs": {plugin: {"options": {"instructionFiles": mode}}}}))
    return path


@pytest.mark.parametrize("plugin", ["cc-plugin-agents-md@builtin", "agents-md@builtin"])
def test_both_mode_is_read_from_either_plugin_id(tmp_path, plugin):
    f = _settings(tmp_path / "s.json", plugin, "claude-md-and-agents-md")
    assert reads_claude_md_and_agents_md([f]) is True


def test_both_mode_first_file_that_sets_it_wins(tmp_path):
    local = _settings(tmp_path / "local.json", "cc-plugin-agents-md@builtin", "claude-md-or-agents-md")
    user = _settings(tmp_path / "user.json", "cc-plugin-agents-md@builtin", "claude-md-and-agents-md")
    assert reads_claude_md_and_agents_md([local, user]) is False
    assert reads_claude_md_and_agents_md([tmp_path / "absent.json", user]) is True


@pytest.mark.parametrize("content", [
    "", "not json", "[]", '{"pluginConfigs": []}',
    '{"pluginConfigs": {"cc-plugin-agents-md@builtin": {"options": null}}}',
    '{"pluginConfigs": {"cc-plugin-agents-md@builtin": {"options": {"instructionFiles": 1}}}}',
    '{"pluginConfigs": {"other@builtin": {"options": {"instructionFiles": "claude-md-and-agents-md"}}}}',
])
def test_both_mode_defaults_to_off_on_anything_else(tmp_path, content):
    f = tmp_path / "s.json"
    f.write_text(content)
    assert reads_claude_md_and_agents_md([f]) is False


def test_both_mode_allows_only_claude_local_md():
    found = ["CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md", "sub/CLAUDE.local.md"]
    assert _allowed(found, both_mode=False) == found
    assert _allowed(found, both_mode=True) == ["CLAUDE.md", ".claude/CLAUDE.md"]
