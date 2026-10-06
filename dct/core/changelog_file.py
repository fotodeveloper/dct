"""Which file is a project's changelog (#876) — the ONE resolver used by the
release plan, the release effects (prepare / apply / resume / reconstruct /
takeover), the CLI (`dct project changelog-path`, `dct project add`).

Whether the stored projects.changelog_path is the owner's choice is the
marker projects.changelog_path_explicit (K-14), never the value: a project
that never chose holds the default DEFAULT_CHANGELOG_PATH with the marker
false (detect); set_changelog_path() marks every choice explicit — also
exactly `changelog.md`; reset_changelog_path() goes back to detection. A
project dict without the marker is never guessed explicit.

resolve_changelog(project, project_root=, repo_root=, commit=) looks at the
TRACKED files of the candidate commit (in git: `git ls-tree`, case-sensitive,
never the work tree) or, outside git, at the project directory on disk:

  1. manages_changelog off → not managed; nothing else is looked at.
  2. An explicit path CHOOSES the file but waives no check: a tracked
     case-variant of it (any component, e.g. `Changelog.md` stored,
     `CHANGELOG.md` tracked) → problem `case_variant` (never rewritten
     silently: on a case-insensitive file system the stored spelling would
     open the tracked file, on a case-sensitive one it would create a
     second file); a name without a Markdown extension (MARKDOWN_EXTS) →
     problem `not_markdown`, whether the file exists or not (K-10); a
     stored path leaving the project root (written past the setter) →
     problem `outside_project` (K-19); else that file, existing or new
     (`stored`), after the path check below.
  3. Detection among the files DIRECTLY in the project root (regular files
     and symlinks) whose name, compared case-insensitively (NFC +
     casefold), is one of NAMES with a Markdown extension (MARKDOWN_EXTS)
     or another known one (OTHER_EXTS): exactly one Markdown candidate →
     its exact tracked spelling (`detected`) — a symlink → problem
     `symlink`; several (names differing only in case included; the
     default spelling is NOT privileged, K-13) → problem `ambiguous`; only
     non-Markdown ones → problem `not_markdown` (dct never writes Markdown
     into it and never creates a second changelog beside it); none → the
     default spelling, a new file (`default`).

EVERY component of the resolved path is checked (K-19, in the tree — or on
disk outside git): a symlink anywhere on it (the file or a directory) →
problem `symlink`; a file or submodule where a directory is expected →
`not_a_directory`; a directory or submodule where the file is expected →
`not_a_file` (each with `component`: file | directory).

A symlink is never written through (its target may lie anywhere, and the
release compares and writes the path's own bytes): its ways name the path
through the link's target as the explicit path when that path is
ADMISSIBLE (Markdown, inside the project root, no link / file / submodule
component), replacing the link, or turning dct's changelog file off. The
`ambiguous` choices are admissible paths the same way (K-24); a link
candidate without an admissible target is dropped from them, and then its
replace-link way and the off way are offered (K-27: never no way). The
`--detect` way of an explicit path is offered only when detection settles
a file that, in git, also passes check_work_tree (K-28). Outside git no
way names a git step or a commit (K-24).

check_work_tree() adds, in git, the DIRECTORIES of the path on disk (K-19:
an ignored / untracked link or file where the candidate holds a directory
or nothing → `symlink` / `not_a_directory`, `baseline: "work_tree"`; K-28: a
TRACKED directory / link / submodule whose name differs only in case,
which a case-insensitive disk opens at the path → `not_a_file` /
`symlink`, component file, `baseline: "work_tree"`) and
problem `not_baseline` when the work tree's file at
the resolved path is not the candidate's content AS A CHECKOUT WRITES IT
(gitinfo.git_cat_blob with the path: eol / smudge filters applied, K-11):
an ignored / untracked file where the candidate has none — or, on a
case-insensitive file system, a tracked file whose name differs only in
case —, or changed bytes (`baseline` absent | tracked | unfiltered — the
candidate's content in another form than a checkout writes: nothing to
commit, check it out again through its filters).

content_problem() adds problem `content` (K-9) when the release's section
cannot be composed into the file's content (not UTF-8, a different section
of the version already there, git filters that do not keep the composed
text — `filtered`, K-20): the release effects compose the WHOLE file
before the stamp with the function the write uses and report its refusal
through it — the plan does not compose (it has no release record yet).

The result is JSON-native and the same object goes into the plan
(`changelog_file`) and into every refusal (`details.changelog_file`):
{managed, configured, explicit, path, source, format, candidates, problem,
found, ways}. `path` is relative to the BASE (the repository root in git,
the project root outside git) and None when not managed or on a problem;
`ways` are the true next steps [{way, command, what}]. Read-only: nothing
is ever stored here.
"""

from __future__ import annotations

import os
import posixpath
import shlex
import stat
import unicodedata

from dct.core import gitinfo

DEFAULT_CHANGELOG_PATH = "changelog.md"
NAMES = ("changelog", "changes", "history", "news", "releases")
MARKDOWN_EXTS = (".md", ".markdown")
OTHER_EXTS = ("", ".rst", ".txt", ".adoc")
PROBLEMS = ("ambiguous", "case_variant", "not_markdown", "symlink", "not_a_directory",
            "not_a_file", "outside_project", "not_baseline", "content")
# The explicit Markdown path the not_markdown next step proposes for a NEW
# changelog beside the existing one.
PROPOSED_MARKDOWN_PATH = "CHANGELOG.md"
EXPLICIT_MARKER = "changelog_path_explicit"

_FILE_KINDS = ("file", "link")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _git_lister(repo_root: str, commit: str):
    """listdir(rel_dir) → [(name, kind)] of the TREE of `commit` (exact,
    case-sensitive names; kind file | link | tree | other)."""

    def listdir(rel_dir: str) -> list[tuple[str, str]]:
        args = ["ls-tree", "-z", commit] + (["--", rel_dir + "/"] if rel_dir else [])
        _, out = gitinfo.git_read(repo_root, *args)
        entries = []
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) != 3:
                continue
            mode, kind = parts[0], parts[1]
            if kind == "blob":
                kind = "link" if mode == "120000" else "file"
            elif kind != "tree":
                kind = "other"  # a submodule
            entries.append((path.rsplit("/", 1)[-1], kind))
        return entries

    return listdir


def _disk_lister(base: str):
    """listdir(rel_dir) → [(name, kind)] of the directory on disk."""

    def listdir(rel_dir: str) -> list[tuple[str, str]]:
        directory = os.path.join(base, *rel_dir.split("/")) if rel_dir else base
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        entries = []
        for name in names:
            full = os.path.join(directory, name)
            if os.path.islink(full):
                kind = "link"
            elif os.path.isfile(full):
                kind = "file"
            elif os.path.isdir(full):
                kind = "tree"
            else:
                kind = "other"
            entries.append((name, kind))
        return entries

    return listdir


def _memoized(listdir):
    """`listdir` reading each directory once per resolution (the path walk
    and the detection share the root listing)."""
    seen: dict[str, list[tuple[str, str]]] = {}

    def cached(rel_dir: str) -> list[tuple[str, str]]:
        if rel_dir not in seen:
            seen[rel_dir] = listdir(rel_dir)
        return seen[rel_dir]

    return cached


def _git_readlink(repo_root: str, commit: str):
    """readlink(rel) → the target text of the symlink `rel` in `commit`."""

    def readlink(rel: str) -> str | None:
        data = gitinfo.git_read_blob(repo_root, commit, rel)
        return None if data is None else data.decode("utf-8", "replace")

    return readlink


def _disk_readlink(base: str):
    """readlink(rel) → the target text of the symlink `rel` on disk."""

    def readlink(rel: str) -> str | None:
        try:
            return os.readlink(os.path.join(base, *rel.split("/")))
        except OSError:
            return None

    return readlink


def _case_matches(listdir, directory: str, parts: list[str]) -> list[str]:
    """Every file below `directory` whose path components equal `parts`
    compared case-insensitively (the exact spelling included)."""
    head, rest = parts[0], parts[1:]
    out: list[str] = []
    for name, kind in listdir(directory):
        if _fold(name) != _fold(head):
            continue
        if rest:
            if kind == "tree":
                out.extend(_case_matches(listdir, _join(directory, name), rest))
        elif kind in _FILE_KINDS:
            out.append(_join(directory, name))
    return sorted(out)


def _format_of(name: str) -> str | None:
    """'markdown' | 'other' for a changelog-like file name, else None."""
    stem, ext = os.path.splitext(name)
    if _fold(stem) not in NAMES:
        return None
    ext = _fold(ext)
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in OTHER_EXTS:
        return "other"
    return None


def _command(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _choose_way(slug: str, rel: str) -> dict:
    return {"way": "choose_path", "command": _command("dct", "project", "changelog-path", slug, rel),
            "what": f"make {rel} the project's changelog file"}


def _off_way(slug: str) -> dict:
    return {"way": "changelog_off", "command": _command("dct", "project", "changelog", slug, "off"),
            "what": "turn dct's changelog file off for this project (manages_changelog): the "
                    "release then writes no changelog file"}


def _markdown_name(rel: str) -> str:
    """`rel` with a Markdown extension (HISTORY.rst → HISTORY.md)."""
    return os.path.splitext(rel)[0] + ".md"


def _not_markdown_ways(slug: str, *, existing: str | None, new_path: str,
                       markdown: list[str], in_git: bool) -> list[dict]:
    """K-15: converting means a rewrite of the content AND a Markdown name
    AND the path setting — a rename alone converts nothing. `existing` is
    the non-Markdown changelog (project-relative; None when it does not
    exist yet), `new_path` the Markdown path proposed for a new file,
    `markdown` the Markdown files the owner may choose instead. Outside git
    (`in_git` false, K-24) the steps name no git command and no commit."""
    ways = []
    if existing:
        converted = _markdown_name(existing)
        if in_git:
            rename = (f"e.g. `{_command('git', 'mv', existing, converted)}`, but git mv alone does not convert "
                      f"the format —, commit it as work in progress")
        else:
            rename = (f"e.g. rename it with `{_command('mv', existing, converted)}`, but a rename alone does "
                      f"not convert the format —")
        ways.append({
            "way": "rewrite_markdown", "command": None,
            "what": f"convert {existing} to Markdown yourself: rewrite its content in the Keep "
                    f"a Changelog layout (`## [X.Y.Z] - date` sections) AND give it a Markdown "
                    f"extension (.md) — {rename}, choose it with `dct project changelog-path "
                    f"{slug} {converted}`, then plan again"})
    if markdown:
        ways.extend(_choose_way(slug, m) for m in markdown)
    else:
        keep = f"keep {existing} untouched and " if existing else ""
        ways.append({
            "way": "explicit_path",
            "command": _command("dct", "project", "changelog-path", slug, new_path),
            "what": f"{keep}let dct write a Markdown changelog at an explicit path (any name "
                    f"with a Markdown extension, e.g. {new_path})"})
    ways.append(_off_way(slug))
    return ways


def _symlink_ways(slug: str, rel: str, target: str | None, *, in_git: bool,
                  directory: bool = False) -> list[dict]:
    """K-11 / K-19: a symlink is never written through. `rel` is the link
    (project-relative) — the changelog file itself or, with `directory`, a
    directory of its path; `target` the changelog path through the link's
    target, project-relative, offered only when it is an ADMISSIBLE path
    (Markdown, inside the project, no link / file / submodule component),
    else None. Outside git (`in_git` false, K-24) no git step, no commit."""
    ways = []
    if target:
        ways.append({**_choose_way(slug, target),
                     "what": f"make {target} (through the link's target) the project's "
                             f"changelog file (the link {rel} stays as it is)"})
    done = ", commit it as work in progress, then plan again" if in_git else ", then plan again"
    if directory:
        replace = f"replace the link {rel} by a real directory holding the changelog{done}"
    elif target and in_git:
        replace = (f"replace the link {rel} by the regular file (e.g. `{_command('git', 'rm', rel)}` and "
                   f"`{_command('git', 'mv', target, rel)}`){done}")
    elif target:
        replace = f"replace the link {rel} by the regular file (move {target} to {rel}){done}"
    else:
        replace = f"replace the link {rel} by a regular Markdown file inside the project{done}"
    ways.append({"way": "replace_link", "command": None, "what": replace})
    ways.append(_off_way(slug))
    return ways


def _path_ways(slug: str, detected: str | None) -> list[dict]:
    """K-19: the ways out of a changelog path that cannot hold the file (a
    file or submodule where a directory is expected, a directory or
    submodule where the file is expected, a path leaving the project):
    choose another path, go back to detection when detection settles a
    file (`detected`, project-relative), or turn the changelog file off."""
    ways = [{"way": "choose_path", "command": None,
             "what": f"choose another changelog path inside the project whose directories are "
                     f"directories and whose file is a regular Markdown file (or does not exist "
                     f"yet): `dct project changelog-path {slug} <path>`, then plan again"}]
    if detected:
        ways.append({"way": "detect",
                     "command": _command("dct", "project", "changelog-path", slug, "--detect"),
                     "what": f"go back to detection — it settles {detected}"})
    ways.append(_off_way(slug))
    return ways


def _is_markdown(name: str) -> bool:
    return _fold(os.path.splitext(name)[1]) in MARKDOWN_EXTS


def resolve_changelog(project: dict, *, project_root: str | None,
                      repo_root: str | None = None, commit: str | None = None) -> dict:
    """The project's changelog file (module doc, rules 1-3). In git pass
    `repo_root` and `commit` (the candidate — never HEAD read later);
    outside git only `project_root` (the directory on disk). Whether a
    stored path is explicit is `project["changelog_path_explicit"]` (absent
    → not explicit). Read-only; raises gitinfo.GitReadError when a tree
    read fails."""
    slug = project.get("slug") or "<slug>"
    configured = os.path.normpath(project.get("changelog_path") or DEFAULT_CHANGELOG_PATH)
    configured = configured.replace(os.sep, "/")
    explicit = bool(project.get(EXPLICIT_MARKER))
    out = {"managed": bool(project.get("manages_changelog", True)), "configured": configured,
           "explicit": explicit, "path": None, "source": None, "format": None,
           "candidates": [], "problem": None, "found": [], "ways": []}
    if not out["managed"] or not project_root:
        return out
    in_git = bool(repo_root and commit)
    if in_git:
        rel = os.path.relpath(os.path.realpath(project_root), os.path.realpath(repo_root))
        prefix = "" if rel == "." else rel.replace(os.sep, "/")
        listdir = _git_lister(repo_root, commit)
        readlink = _git_readlink(repo_root, commit)
    else:
        prefix = ""
        listdir = _disk_lister(project_root)
        readlink = _disk_readlink(project_root)
    listdir = _memoized(listdir)

    def project_rel(path: str) -> str:
        return path[len(prefix) + 1:] if prefix else path

    def inside(path: str) -> bool:
        if prefix:
            return path.startswith(prefix + "/")
        return not (path == ".." or path.startswith("../") or posixpath.isabs(path))

    def shape(path: str) -> tuple[str, str] | None:
        """K-19 — (problem, component) when a component of the base-relative
        `path` cannot hold the changelog: a symlink anywhere (`symlink`), a
        file or submodule where a directory is expected (`not_a_directory`),
        a directory or submodule where the file is expected (`not_a_file`);
        None when every existing component is a directory and the file a
        regular file (or the rest does not exist yet)."""
        parts = path.split("/")
        directory = ""
        for i, name in enumerate(parts):
            kind = next((k for n, k in listdir(directory) if n == name), None)
            if kind is None:
                return None
            here = _join(directory, name)
            if kind == "link":
                return "symlink", here
            if i < len(parts) - 1 and kind != "tree":
                return "not_a_directory", here
            if i == len(parts) - 1 and kind != "file":
                return "not_a_file", here
            directory = here
        return None

    def link_target(link: str, path: str) -> str | None:
        """The changelog path `path` through the symlink `link` (the file
        itself or one of its directories) as a project-relative path — only
        when it is admissible (inside the project, Markdown, no link / file
        / submodule component, not `path` itself); else None."""
        target = readlink(link)
        if not target or posixpath.isabs(target):
            return None
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
        rest = path[len(link) + 1:]
        if rest:
            joined = f"{joined}/{rest}"
        if not inside(joined) or not _is_markdown(joined) or joined == path \
                or shape(joined) is not None:
            return None
        return project_rel(joined)

    def detected_path() -> str | None:
        """What detection settles (K-19 ways of an explicit path), else None
        — in git only when that path also passes the work-tree check (K-28:
        on a case-insensitive file system the detected default may open a
        tracked directory whose name differs only in case)."""
        if not explicit:
            return None
        found = resolve_changelog({**project, EXPLICIT_MARKER: False},
                                  project_root=project_root, repo_root=repo_root, commit=commit)
        if found["path"] and in_git:
            found = check_work_tree(found, repo_root=repo_root, commit=commit, slug=slug)
        return project_rel(found["path"]) if found["path"] else None

    def problem_at(path: str, problem: str, component: str) -> dict:
        """The resolution's problem for a `path` whose `component` cannot
        hold the changelog (shape())."""
        where = "file" if component == path else "directory"
        if problem == "symlink":
            ways = _symlink_ways(slug, project_rel(component), link_target(component, path),
                                 in_git=in_git, directory=where == "directory")
        else:
            ways = _path_ways(slug, detected_path())
        return {"problem": problem, "found": [component], "component": where, "ways": ways}

    def settle(path: str, source: str) -> dict:
        """`path` as the changelog file, or its K-19 shape problem."""
        bad = shape(path)
        if bad:
            return {"format": "markdown", **problem_at(path, *bad)}
        return {"path": path, "source": source, "format": "markdown"}

    root_entries = listdir(prefix)
    candidates = sorted(
        ({"path": _join(prefix, name), "format": fmt}
         for name, kind in root_entries if kind in _FILE_KINDS
         for fmt in [_format_of(name)] if fmt is not None),
        key=lambda c: c["path"])
    out["candidates"] = candidates
    markdown = [c["path"] for c in candidates if c["format"] == "markdown"]

    if explicit:
        target = _join(prefix, configured)
        if configured == ".." or configured.startswith("../") or posixpath.isabs(configured):
            # K-19: a stored value the setter would reject (another client,
            # an edit by hand) never leads a write out of the project.
            out.update(problem="outside_project", found=[configured], format=None,
                       ways=_path_ways(slug, detected_path()))
            return out
        matches = _case_matches(listdir, prefix, configured.split("/"))
        if matches and target not in matches:
            out.update(problem="case_variant", found=matches,
                       ways=[_choose_way(slug, project_rel(m)) for m in matches])
            return out
        if not _is_markdown(configured):
            out.update(problem="not_markdown", found=[target], format="other",
                       ways=_not_markdown_ways(
                           slug, existing=configured if matches else None,
                           new_path=(PROPOSED_MARKDOWN_PATH if matches
                                     else _markdown_name(configured)),
                           markdown=[project_rel(m) for m in markdown], in_git=in_git))
            return out
        out.update(settle(target, "stored"))
        return out

    if len(markdown) == 1:
        out.update(settle(markdown[0], "detected"))
    elif len(markdown) > 1:
        # K-24: every offered choice leads to an admissible path — a link
        # candidate offers the path through its target (when admissible).
        # K-27: a link candidate dropped for want of an admissible target
        # keeps its replace-link way, and the off way is offered — the
        # problem never comes out without a next step.
        choices, dropped = [], []
        for m in markdown:
            choice = project_rel(m) if shape(m) is None else link_target(m, m)
            if not choice:
                dropped.append(m)
            elif choice not in choices:
                choices.append(choice)
        ways = [_choose_way(slug, c) for c in choices]
        for m in dropped:
            ways.extend(w for w in _symlink_ways(slug, project_rel(m), None, in_git=in_git)
                        if w["way"] == "replace_link")
        if dropped or not choices:
            ways.append(_off_way(slug))
        out.update(problem="ambiguous", found=markdown, ways=ways)
    elif candidates:
        out.update(problem="not_markdown", found=[c["path"] for c in candidates],
                   ways=_not_markdown_ways(slug, existing=project_rel(candidates[0]["path"]),
                                           new_path=PROPOSED_MARKDOWN_PATH, markdown=[],
                                           in_git=in_git))
    else:
        out.update(settle(_join(prefix, DEFAULT_CHANGELOG_PATH), "default"))
    return out


def resolve_at_head(project: dict) -> tuple[dict, str | None]:
    """(resolution, base directory) for DISPLAY (the CLI query, `dct project
    add`): best-effort location of the project's registered path — in git
    the tracked files of its checkout's HEAD, else the directory on disk.
    The base is what the resolution's paths are relative to (None without a
    registered path). Raises gitinfo.GitReadError when a tree read fails."""
    path = project.get("path")
    if not path:
        return resolve_changelog(project, project_root=None), None
    checkout = gitinfo.resolve_checkout(path, path)
    head = gitinfo.git_head(path) if checkout else None
    if checkout and head:
        return resolve_changelog(project, project_root=checkout["project_root"],
                                 repo_root=checkout["repo_root"], commit=head), \
            checkout["repo_root"]
    return resolve_changelog(project, project_root=path), path


def _disk_bytes(path: str) -> bytes | str | None:
    """The bytes of a regular file, None when nothing exists at `path`, else
    'not_a_file' (a directory, a symlink, …)."""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        return "not_a_file"
    with open(path, "rb") as fh:
        return fh.read()


def _tracked_blob(repo_root: str, commit: str, rel: str) -> str | None:
    """The blob id of `rel` in `commit`, None when the tree has no such entry."""
    _, out = gitinfo.git_read(repo_root, "ls-tree", "-z", commit, "--", rel)
    entry = next((e for e in out.split("\0") if e), None)
    if entry is None:
        return None
    meta, _, name = entry.partition("\t")
    parts = meta.split()
    if name != rel or len(parts) != 3 or parts[1] != "blob":
        raise gitinfo.GitReadError(f"{rel} in {commit} is not a file ({meta!r})")
    return parts[2]


def _disk_component(base: str, rel: str) -> tuple[str, str] | None:
    """K-19 — the DIRECTORIES of `rel` (base-relative) on disk: (problem,
    component) for the first one that is a symlink (`symlink`) or anything
    but a directory (`not_a_directory`); None when each is a directory or
    does not exist (the write creates it). The file itself is the work-tree
    content check's (a link or a directory there is never its bytes)."""
    parts = rel.split("/")[:-1]
    path = base
    for i, name in enumerate(parts):
        path = os.path.join(path, name)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return None
        here = "/".join(parts[:i + 1])
        if stat.S_ISLNK(mode):
            return "symlink", here
        if not stat.S_ISDIR(mode):
            return "not_a_directory", here
    return None


def _disk_component_problem(resolution: dict, rel: str, problem: str, component: str) -> dict:
    """`resolution` with the K-19 problem of a directory of its path that is,
    ON DISK, a symlink or no directory — where the candidate holds a
    directory or nothing (the candidate's own components are resolve_
    changelog's): an ignored or untracked entry the write would follow."""
    what = (f"move {component} away (the candidate holds a directory or nothing there — it is "
            f"an ignored or untracked {'symbolic link' if problem == 'symlink' else 'file'}) "
            f"and plan again — dct never writes the changelog through it")
    return {**resolution, "path": None, "problem": problem, "found": [component],
            "component": "directory", "baseline": "work_tree",
            "ways": [{"way": "clear_work_tree", "command": None, "what": what}]}


def _tracked_case_entry(repo_root: str, commit: str, rel: str) -> tuple[str, str] | None:
    """(path, kind) of the entry of `commit` beside `rel` whose name equals
    `rel`'s compared case-insensitively but is no regular file (kind tree |
    link | other) — what a case-insensitive file system opens at `rel`; None
    when there is none."""
    parent, _, name = rel.rpartition("/")
    for entry, kind in _git_lister(repo_root, commit)(parent):
        if _fold(entry) == _fold(name) and kind != "file":
            return _join(parent, entry), kind
    return None


def check_work_tree(resolution: dict, *, repo_root: str, commit: str, slug: str) -> dict:
    """`resolution` (in git), with problem `not_baseline` when the work
    tree's file at its path is not the candidate's content AS A CHECKOUT
    WRITES IT (gitinfo.git_cat_blob with the path — eol / smudge filters
    applied, K-11; the form the release compares and writes): something on
    disk where the candidate holds no file (ignored, untracked, or — on a
    case-insensitive file system — a tracked file whose name differs only
    in case), or other bytes. Bytes git still considers the candidate's
    content (filter attributes changed after the checkout) get the true
    step "check it out again through its filters". K-28: a TRACKED
    directory, link or submodule the case-insensitive disk opens at the
    path (its name differs only in case) is its own problem — `not_a_file`
    / `symlink`, component file, `baseline: "work_tree"` — whose ways
    (`slug`'s commands) never move the tracked entry: another path, or off.
    Unchanged when it is not managed, already has a problem, or matches.
    Read-only."""
    rel = resolution.get("path")
    if not resolution.get("managed") or resolution.get("problem") or not rel:
        return resolution
    bad = _disk_component(repo_root, rel)
    if bad:
        return _disk_component_problem(resolution, rel, *bad)
    blob = _tracked_blob(repo_root, commit, rel)
    base = None if blob is None else gitinfo.git_cat_blob(repo_root, blob, path=rel)
    full = os.path.join(repo_root, *rel.split("/"))
    disk = _disk_bytes(full)
    if disk == base:
        return resolution
    if blob is None and disk == "not_a_file":
        tracked = _tracked_case_entry(repo_root, commit, rel)
        if tracked:
            path, kind = tracked
            return {**resolution, "path": None,
                    "problem": "symlink" if kind == "link" else "not_a_file",
                    "found": [path], "component": "file", "baseline": "work_tree",
                    "ways": _path_ways(slug, None)}
    directory, name = os.path.split(full)
    try:
        here = sorted(n for n in os.listdir(directory) if _fold(n) == _fold(name))
    except OSError:
        here = []
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    found = [_join(parent, n) for n in here] or [rel]
    baseline = "absent" if blob is None else "tracked"
    if blob is None and disk == "not_a_file":
        what = (f"move {', '.join(found)} away (an ignored or untracked directory or link — "
                f"not a regular file) and plan again — dct never writes the changelog over or "
                f"through it")
    elif blob is None:
        what = (f"move {', '.join(found)} away (or commit it if it is your changelog, then "
                f"plan again) — dct never overwrites a file the candidate does not hold")
    elif isinstance(disk, bytes) and gitinfo.git_read(
            repo_root, "hash-object", f"--path={rel}", "--", full)[1].strip() == blob:
        baseline = "unfiltered"
        what = (f"check {rel} out again through its filters "
                f"(`{_command('git', 'rm', '--cached', '-q', '--', rel)}`, "
                f"then `{_command('git', 'checkout', 'HEAD', '--', rel)}`) and plan again — its content is the "
                f"candidate's, but not in the form a checkout writes it (eol / filter "
                f"attributes changed after it was checked out)")
    else:
        what = (f"commit your change to {rel} (or check it out again) and plan again — dct "
                f"writes the changelog from the candidate's content only")
    return {**resolution, "path": None, "problem": "not_baseline", "found": found,
            "baseline": baseline,
            "ways": [{"way": "clear_work_tree", "command": None, "what": what}]}


CONTENT_REASONS = ("not_utf8", "section_differs", "insertion", "filtered")


def content_problem(resolution: dict, *, slug: str, reason: str, detail: str,
                    version: str, in_git: bool) -> dict:
    """`resolution` (a settled file) with problem `content` (K-9): composing
    the release's `version` section into the file's content refuses —
    `reason` one of CONTENT_REASONS, `detail` the composition's own text.
    Its ways are true: fix the content yourself (in git: commit it as work
    in progress; outside git, `in_git` false, no git step and no commit —
    K-24) and plan again, or turn dct's changelog file off. Read-only."""
    rel = resolution["path"]
    done = ", commit it as work in progress, then plan again" if in_git else ", then plan again"
    if reason == "not_utf8":
        fix = {"way": "reencode", "command": None,
               "what": f"re-encode {rel} as UTF-8 yourself{done}"}
    elif reason == "filtered":
        fix = {"way": "fix_filter", "command": None,
               "what": f"make the git attributes of {rel} keep the text (a line-ending "
                       f"normalisation is fine; a clean filter that rewrites the content is "
                       f"not — check `{_command('git', 'check-attr', '-a', '--', rel)}`){done}"}
    elif reason == "section_differs":
        fix = {"way": "remove_section", "command": None,
               "what": f"remove the {version} section(s) from {rel} yourself (dct writes it from "
                       f"the release's entries — or make it exactly the section dct renders)"
                       f"{done}"}
    else:
        fix = {"way": "fix_layout", "command": None,
               "what": f"fix the `## [` version sections of {rel} yourself (the section is "
                       f"inserted above the first one){done}"}
    return {**resolution, "path": None, "problem": "content", "content": reason,
            "detail": detail, "found": [rel], "ways": [fix, _off_way(slug)]}


def explain(resolution: dict) -> str:
    """One sentence naming the configured path, the file(s) found and every
    next step — the text of a plan warning and of a refusal."""
    problem = resolution.get("problem")
    configured = resolution.get("configured")
    found = ", ".join(resolution.get("found") or [])
    setting = (f"the stored changelog path is {configured}" if resolution.get("explicit")
               else f"no changelog path is chosen (dct detects the file; the default "
                    f"{configured} is used only when none is found)")
    if problem == "ambiguous":
        head = (f"{setting}, and the project root tracks several Markdown changelogs ({found}) "
                f"— dct does not guess which one is the changelog")
    elif problem == "case_variant":
        head = (f"the stored changelog path {configured} is not tracked, but {found} is — a "
                f"name differing only in case (on a case-insensitive file system it would "
                f"open {found}, on a case-sensitive one create a second file); dct never "
                f"rewrites the stored path silently")
    elif problem == "not_markdown" and resolution.get("explicit"):
        head = (f"{setting}, which is not a Markdown file name (.md / .markdown) — dct writes "
                f"Markdown only: it never writes into {found}, whether it exists or not; an "
                f"explicit path chooses the file, it waives no check")
    elif problem == "not_markdown":
        head = (f"{setting}, and the project's changelog ({found}) is not Markdown — dct "
                f"writes Markdown only: it never writes into {found} and never creates a "
                f"second changelog beside it silently")
    elif problem == "symlink" and resolution.get("component") == "directory":
        where = ("in the work tree (not in the candidate)"
                 if resolution.get("baseline") == "work_tree" else "in the candidate")
        head = (f"{setting}, and the directory {found} of the changelog path is a symbolic "
                f"link {where} — dct never writes through a link (its target may lie "
                f"anywhere, also outside the project)")
    elif problem in ("symlink", "not_a_file") and resolution.get("baseline") == "work_tree":
        kind = "a symbolic link" if problem == "symlink" else "a directory or a submodule"
        head = (f"{setting}; the candidate holds no file at that path, but it tracks {found} — "
                f"{kind} whose name differs only in case, which this case-insensitive file "
                f"system opens at the changelog path: the changelog file cannot be written "
                f"there (moving the tracked {found} is no way out)")
    elif problem == "symlink":
        head = (f"{setting}, and the changelog file {found} is a symbolic link — dct never "
                f"writes through a link (its target may lie anywhere)")
    elif problem == "not_a_directory":
        where = ("in the work tree (not in the candidate)"
                 if resolution.get("baseline") == "work_tree" else "in the candidate")
        head = (f"{setting}, but {found} — a directory of that path — is a file or a submodule "
                f"{where}, not a directory: the changelog file cannot be written there")
    elif problem == "not_a_file":
        head = (f"{setting}, but {found} is a directory or a submodule, not a regular file: "
                f"the changelog file cannot be written there")
    elif problem == "outside_project":
        head = (f"the stored changelog path {configured} leaves the project root — dct writes "
                f"only inside the project (`dct project changelog-path` never stores such a "
                f"path; it was written another way)")
    elif problem == "not_baseline":
        if resolution.get("baseline") == "absent":
            head = (f"the candidate holds no {found}, but the work tree has a file there "
                    f"({found}: ignored or untracked, or on a case-insensitive file system "
                    f"a tracked file whose name differs only in case) — not the content "
                    f"before the release")
        elif resolution.get("baseline") == "unfiltered":
            head = (f"the work tree's {found} holds the candidate's content, but not in the "
                    f"form a checkout of it writes (its eol / filter attributes)")
        else:
            head = (f"the work tree's {found} is not the candidate's content (an uncommitted "
                    f"change, or skip-worktree / assume-unchanged)")
    elif problem == "content":
        head = (f"the changelog file {found} cannot take the release's section: "
                f"{resolution.get('detail')} — refused before the stamp")
    else:
        return ""
    steps = []
    for i, way in enumerate(resolution.get("ways") or [], 1):
        command = f": `{way['command']}`" if way.get("command") else ""
        steps.append(f"({i}) {way['what']}{command}")
    return head + (" — next step: " + "; ".join(steps) if steps else "")
