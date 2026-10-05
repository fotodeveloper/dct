"""The release EFFECTS layer (K-72 layer 3) — the default executable release
workflow behind `/release`: `dct release attest / apply / resume / abandon /
reconstruct` (and `dct clog release`, an alias of apply).

ONE implementation for the CLI and both clients' skills (they only run the
dialog, K-49): every effect is performed by this module and VERIFIED against
reality before the ledger (dct.core.releases) records the progress — a
status is never taken as proof that an effect happened. The MCP process
never runs this code (K-72: it never verifies files).

Order of effects of one release (apply; resume continues from the record):

  1. pre-checks — strict checkout (K-52; a registered path), no unfinished
     record, clean index and work tree (K-50); the plan re-derived FOR THE
     PINNED CANDIDATE must match the approved snapshot digest (K-80); the
     requested set ⊆ the plan's proven ids; HEAD unchanged right before the
     stamp;
  2. stamp — core release_entries (the record, status 'stamped'), bound to
     the plan inside its transaction (K-81: stampable_ids, expected_entries);
     from here the record's `version` is the spelling of every artifact
     (K-55);
  3. changelog file F — the expected content is DETERMINISTIC: the
     candidate commit's F + the V section rendered from the database and
     the record (date = the record's created_at, K-32);
  4. version sync — the project's script, only while current_version is
     the record's version (K-31); the changed files must equal the
     project's version-file contract (K-73, learned once at the first
     release with --confirm-version-files) and contain V; the manifest
     {changelog, version_files, tree} is recorded with 'files_written';
  5. commit — the expected tree (candidate tree + exactly the manifest
     blobs) is built in a TEMPORARY index; the real index is checked for
     foreign paths / content, the artifacts are staged, the real index must
     then write exactly the expected tree; `git commit-tree` + `git
     update-ref HEAD <new> <candidate>` (compare-and-swap) create the commit,
     whose tree and parent are verified before 'committed';
  6. tag `v<V>` (lightweight) at the verified release commit → 'tagged',
     then 'completed'. Nothing is pushed.

Every effect is checked against reality on resume: present and matching →
skipped; missing → performed; contradicting → ReleaseRefused, nothing
overwritten. Interruption points (INTERRUPTION_POINTS) let tests stop the
workflow before each effect and between each effect and its progress write.
Schema of the results and the interruption / resume table:
docs/release-workflow.md.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core import gitinfo
from dct.core import releases as ledger
from dct.core.changelog import list_entries, normalize_worktree, release_entries
from dct.core.projects import get_project
from dct.core.version import normalize_version, same_version
from dct.export import export_changelog_md
from dct.workflows.release import plan as planning
from dct.workflows.release.proof import proven_ids

SCHEMA_VERSION = 1
RESULT_KEYS = (
    "schema_version", "action", "project", "outcome", "version", "release",
    "steps", "warnings", "details",
)
OUTCOMES = (
    "completed", "noop", "attested", "abandoned", "preview", "reconstructed",
    "refused", "failed", "found", "none",
)

# Where a test (or a debugger) may stop the workflow: before each effect and
# between each effect and the progress write that records it.
INTERRUPTION_POINTS = (
    "before_stamp",          # nothing done yet
    "after_stamp",           # record 'stamped' (the stamp IS its progress write)
    "before_changelog",
    "after_changelog",       # F written + verified; no progress write yet
    "before_sync",
    "after_sync",            # sync ran + version files verified; not 'files_written'
    "after_files_written",   # manifest recorded; before the commit
    "after_stage",           # artifacts staged in the real index; no commit yet
    "after_commit",          # commit created + HEAD moved + verified; not 'committed'
    "after_committed",       # before the tag
    "after_tag",             # tag created + verified; not 'tagged'
    "after_tagged",          # before 'completed'
)

VERSION_SYNC_SCRIPTS = ("scripts/dct-version-sync.sh", "scripts/version-sync.sh")
VERSION_ONLY_LINE = "- Version-only release — no changelog entries."
ATTEST_SUBJECT = "chore: attest changelog entries"
RELEASE_SUBJECT = "release: v{version}"

_MARKER_RE = re.compile(r"^## \[([^\]\r\n]*)\]")
_RELEASE_SUBJECT_RE = re.compile(r"^release: (\S+)$")
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)


class ReleaseRefused(RuntimeError):
    """A controlled refusal: the workflow stopped WITHOUT the effect it was
    about to perform (and without overwriting anything). `details` is a
    JSON-native dict for the --json output; `exit_code` the CLI's code."""

    exit_code = 1

    def __init__(self, message: str, *, details: dict | None = None,
                 steps: list[dict] | None = None, release: dict | None = None):
        super().__init__(message)
        self.details = details or {}
        self.steps = steps or []
        self.release = release


class SyncFailed(ReleaseRefused):
    """The version-sync script exited non-zero — nothing advanced."""

    def __init__(self, message: str, code: int, **kwargs):
        super().__init__(message, **kwargs)
        self.exit_code = code if 0 < code < 256 else 1


def result(action: str, project: str, outcome: str, **values) -> dict:
    """The ONE result shape of every subcommand (RESULT_KEYS)."""
    out = {key: None for key in RESULT_KEYS}
    out.update(schema_version=SCHEMA_VERSION, action=action, project=project,
               outcome=outcome, steps=[], warnings=[], details={})
    out.update(values)
    return planning._jsonable(out)


# ── small helpers ────────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _write_atomic(path: str, data: bytes) -> None:
    """Replace `path` with `data` (temp file in the same directory + rename),
    keeping an existing file's mode; read back and compared."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".dct-release-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if _read_bytes(path) != data:
        raise ReleaseRefused(f"{path} does not read back what was written")


def _date_of(moment) -> str:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.date().isoformat()


def _rel(path: str) -> str:
    return path.replace(os.sep, "/")


def _point(on_point: Callable[[str], None] | None, name: str) -> None:
    assert name in INTERRUPTION_POINTS, name
    if on_point is not None:
        on_point(name)


# ── git ──────────────────────────────────────────────────────────────────────


def _rev(repo: str, spec: str) -> str | None:
    code, out = gitinfo.git_read(repo, "rev-parse", "-q", "--verify", spec, ok_codes=(0, 1))
    return out.strip().lower() if code == 0 and out.strip() else None


def _head(repo: str) -> str:
    head = _rev(repo, "HEAD^{commit}")
    if head is None:
        raise ReleaseRefused(f"HEAD of {repo} cannot be read")
    return head


def _tree(repo: str, commit: str) -> str:
    tree = _rev(repo, f"{commit}^{{tree}}")
    if tree is None:
        raise ReleaseRefused(f"commit {commit} has no readable tree")
    return tree


def _parents(repo: str, commit: str) -> list[str]:
    _, out = gitinfo.git_read(repo, "rev-list", "--parents", "-n", "1", commit, "--")
    return out.split()[1:]


def _subject(repo: str, commit: str) -> str:
    _, out = gitinfo.git_read(repo, "log", "-1", "--format=%s", commit, "--")
    return out.strip()


def _diff_names(repo: str, a: str, b: str) -> set[str]:
    """Paths whose entries differ between trees / commits `a` and `b`."""
    _, out = gitinfo.git_read(repo, "diff-tree", "-r", "--name-only", "--no-renames", "-z", a, b)
    return {p for p in out.split("\0") if p}


def _index_tree(repo: str) -> str:
    """The tree the user's REAL index would commit (`git write-tree`; an
    unmerged index is refused)."""
    try:
        _, out = gitinfo.git_write(repo, "write-tree")
    except gitinfo.GitWriteError as exc:
        raise ReleaseRefused(f"the index cannot be written as a tree (unmerged paths?): {exc}") \
            from None
    return out.strip()


def _operation_in_progress(repo: str) -> list[str]:
    found = []
    for marker in _IN_PROGRESS:
        _, out = gitinfo.git_read(repo, "rev-parse", "--git-path", marker)
        path = out.strip()
        if path and os.path.exists(path if os.path.isabs(path) else os.path.join(repo, path)):
            found.append(marker)
    return found


def _file_mode(repo: str, commit: str, rel: str, absolute: str) -> str:
    _, out = gitinfo.git_read(repo, "ls-tree", "-z", commit, "--", rel)
    entry = next((e for e in out.split("\0") if e), None)
    if entry:
        return entry.split()[0]
    return "100755" if os.stat(absolute).st_mode & 0o111 else "100644"


def expected_tree(repo: str, candidate: str, paths: Iterable[str]) -> str:
    """The tree of the release commit: the candidate's tree with EXACTLY the
    work-tree content of `paths` (repo-relative) — computed in a temporary
    index (GIT_INDEX_FILE), so the user's index is never touched here.
    Blobs are written with `git hash-object -w` (path filters apply, as for
    `git add`)."""
    with tempfile.TemporaryDirectory(prefix="dct-release-index-") as tmp:
        index = os.path.join(tmp, "index")
        gitinfo.git_write(repo, "read-tree", f"{candidate}^{{tree}}", index_file=index)
        for rel in sorted(paths):
            absolute = os.path.join(repo, rel)
            _, blob = gitinfo.git_write(repo, "hash-object", "-w", "--", rel)
            mode = _file_mode(repo, candidate, rel, absolute)
            gitinfo.git_write(repo, "update-index", "--add", "--cacheinfo",
                              f"{mode},{blob.strip()},{rel}", index_file=index)
        _, tree = gitinfo.git_write(repo, "write-tree", index_file=index)
    return tree.strip()


def _version_tags(repo: str, version: str) -> list[dict]:
    """Tags whose name is the same version as `version` (any spelling):
    [{name, commit (peeled)}]."""
    _, out = gitinfo.git_read(
        repo, "for-each-ref", "--format=%(refname:strip=2)%1f%(objectname)%1f%(*objectname)",
        "refs/tags")
    tags = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        name, obj, peeled = parts
        try:
            if not same_version(normalize_version(name), version):
                continue
        except ValueError:
            continue
        tags.append({"name": name, "commit": (peeled or obj).lower()})
    return sorted(tags, key=lambda t: t["name"])


# ── location ─────────────────────────────────────────────────────────────────


def _locate(engine: sa.Engine, slug: str, cwd: str | None) -> tuple[dict, dict | None, str | None]:
    """(project, checkout | None, project_root | None) — STRICT (K-52)."""
    project = get_project(engine, slug)
    if project is None:
        raise ReleaseRefused(f"Project '{slug}' not found")
    path = project.get("path")
    if not path:
        return project, None, None
    try:
        checkout = gitinfo.verify_release_checkout(path, cwd)
    except gitinfo.NotACheckoutError:
        raise ReleaseRefused(
            f"The current directory is not a checkout of project '{slug}' ({path}) — run "
            f"the release from the project's main checkout or one of its linked worktrees; "
            f"nothing released.") from None
    except gitinfo.CheckoutVerificationError as exc:
        raise ReleaseRefused(
            f"Cannot verify the release checkout of project '{slug}' ({path}): {exc} — "
            f"nothing released.") from None
    if checkout is None:
        return project, None, path
    return project, checkout, checkout["project_root"]


class _Run:
    """State of one apply / resume run."""

    def __init__(self, engine, slug, project, checkout, project_root, *,
                 on_point, confirm_version_files):
        self.engine = engine
        self.slug = slug
        self.project = project
        self.checkout = checkout
        self.repo = checkout["repo_root"] if checkout else None
        self.project_root = project_root
        self.on_point = on_point
        self.confirm_version_files = confirm_version_files
        self.steps: list[dict] = []
        self.warnings: list[str] = []
        self.record: dict | None = None
        self.changelog_rel = self._changelog_rel()

    def _changelog_rel(self) -> str | None:
        if not self.project_root:
            return None
        if self.checkout:
            found = [a["path"] for a in planning._artifacts(self.project, self.checkout, None)
                     if a["role"] == "changelog"]
            return found[0] if found else None
        if not self.project.get("manages_changelog", True):
            return None
        return _rel(os.path.normpath(self.project.get("changelog_path") or "changelog.md"))

    @property
    def base(self) -> str:
        """Paths in the manifest are relative to this directory."""
        return self.repo or self.project_root

    def abspath(self, rel: str) -> str:
        return os.path.join(self.base, *rel.split("/"))

    def step(self, name: str, outcome: str, **detail) -> None:
        self.steps.append({"step": name, "result": outcome, **detail})

    def refuse(self, message: str, **details) -> ReleaseRefused:
        return ReleaseRefused(message, details=details, steps=self.steps, release=self.record)

    def advance(self, status: str, **kwargs) -> None:
        out = ledger.advance_release(self.engine, self.slug, self.record["version"], status,
                                     **kwargs)
        self.record = out["release"]

    @property
    def version(self) -> str:
        return self.record["version"]


# ── pre-checks ───────────────────────────────────────────────────────────────


def _clean_tree(run: _Run, artifacts: list[dict]) -> dict:
    """K-50 for a NEW release: nothing staged, unstaged or conflicted, no
    operation in progress, no release artifact untracked. Returns the index
    state (its untracked list is the pre-sync snapshot)."""
    busy = _operation_in_progress(run.repo)
    if busy:
        raise run.refuse(f"a git operation is in progress ({', '.join(busy)}) — finish or "
                         f"abort it first; nothing released.", in_progress=busy)
    state = planning.index_state(run.repo, artifacts)
    dirty = {"staged": state["staged"], "unstaged": state["unstaged"],
             "conflicted": state["conflicted"]}
    if not state["clean"]:
        raise run.refuse(
            "the index / work tree is not clean — commit or clean it yourself (dct never "
            "resets, stashes or cleans your changes); nothing released.", **dirty)
    untracked_artifacts = [a["path"] for a in state["release_artifacts"] if a["untracked"]]
    if untracked_artifacts:
        raise run.refuse(
            f"release artifacts are untracked: {', '.join(untracked_artifacts)} — commit or "
            f"remove them first; nothing released.", untracked=untracked_artifacts)
    return state


def _check_record_location(run: _Run) -> None:
    """K-51: the record belongs to THIS checkout — never re-pinned."""
    record = run.record
    if run.checkout is None:
        if record["candidate_sha"] or record["checkout_root"]:
            raise run.refuse(
                f"release {record['version']} was cut from a git checkout "
                f"({record['checkout_root'] or '?'}) but the project is not in one now — "
                f"nothing done.")
        return
    if not record["candidate_sha"] or not record["checkout_root"]:
        raise run.refuse(
            f"release {record['version']} has no recorded candidate / checkout — it cannot "
            f"be verified in a git checkout; nothing done (abandon it).",
            candidate_sha=record["candidate_sha"], checkout_root=record["checkout_root"])
    here = normalize_worktree(run.repo)
    if record["checkout_root"] != here:
        raise run.refuse(
            f"release {record['version']} belongs to the checkout {record['checkout_root']}, "
            f"not to {here} — resume it there; the record is never re-pinned; nothing done.",
            recorded=record["checkout_root"], here=here)


# ── step 3: the changelog file ───────────────────────────────────────────────


def _blocks(text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """(preamble, [(version label, start, end)]) — character offsets of each
    `## [label]` section (from its marker line to the next marker / EOF)."""
    offsets, pos = [], 0
    for line in text.splitlines(keepends=True):
        match = _MARKER_RE.match(line)
        if match:
            offsets.append((match.group(1), pos))
        pos += len(line)
    blocks = []
    for i, (label, start) in enumerate(offsets):
        end = offsets[i + 1][1] if i + 1 < len(offsets) else len(text)
        blocks.append((label, start, end))
    preamble = text[:offsets[0][1]] if offsets else text
    return preamble, blocks


def _version_blocks(text: str, version: str) -> list[tuple[str, int, int]]:
    return [b for b in _blocks(text)[1] if _same_label(b[0], version)]


def _same_label(label: str, version: str) -> bool:
    try:
        return same_version(normalize_version(label), version)
    except ValueError:
        return False


def _norm_block(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def render_section(engine: sa.Engine, slug: str, record: dict) -> tuple[str, str]:
    """(section, date) of the release — deterministic, from the database
    and the record only (K-32/K-49): the export's block of the record's
    version (every equivalent stored spelling, K-55) headed `## [<record
    version>] - <record created_at date>`; a version-only release gets
    VERSION_ONLY_LINE. Ends with exactly one newline."""
    version, date = record["version"], _date_of(record["created_at"])
    block = export_changelog_md(engine, slug, version=version)
    if record["entry_ids"]:
        lines = block.split("\n") if block else []
        match = _MARKER_RE.match(lines[0]) if lines else None
        if match is None or not _same_label(match.group(1), version):
            raise ReleaseRefused(
                f"the export of {version} does not render its section although the record "
                f"holds entries {record['entry_ids']} — nothing written.")
        lines[0] = f"## [{version}] - {date}"
        section = "\n".join(lines)
    else:
        if block:
            raise ReleaseRefused(
                f"entries carry {version} but its record is version-only — contradiction; "
                f"nothing written.")
        section = f"## [{version}] - {date}\n\n{VERSION_ONLY_LINE}\n"
    return section.rstrip("\n") + "\n", date


def _insert_section(base: str, section: str, version: str) -> str:
    """`base` with `section` inserted as the TOP version section (directly
    above the first `## [` marker line, a blank line between), or appended
    after the existing text when there is no marker. Verified: V exactly
    once, one marker more, every earlier byte unchanged."""
    newline = "\r\n" if "\r\n" in base else "\n"
    section = section.replace("\n", newline)
    _, blocks = _blocks(base)
    if blocks:
        pos = blocks[0][1]
        inserted = section + newline
    elif not base:
        pos, inserted = 0, f"# Changelog{newline}{newline}" + section
    else:
        pos = len(base)
        lead = "" if base.endswith(newline) else newline
        lead += "" if base.endswith(newline * 2) else newline
        inserted = lead + section
    new = base[:pos] + inserted + base[pos:]
    if not (new[:pos] == base[:pos] and new[pos + len(inserted):] == base[pos:]):
        raise ReleaseRefused("the changelog insertion would change earlier history")
    if len(_version_blocks(new, version)) != 1:
        raise ReleaseRefused(f"the changelog would not hold the {version} section exactly once")
    if len(_blocks(new)[1]) != len(blocks) + 1:
        raise ReleaseRefused("the changelog insertion did not add exactly one section")
    return new


def expected_changelog(engine: sa.Engine, slug: str, record: dict, base: bytes | None) -> bytes:
    """The expected content of F after the release (K-32) from its content
    BEFORE the release (`base`; None = F does not exist — bootstrap):
      - bootstrap: the full export (no Unreleased) with the release's block
        replaced by / completed with the rendered section — V exactly once;
      - existing F without a V section: the section inserted on top, the
        rest byte-identical, one section more;
      - existing F already holding the IDENTICAL V section once: unchanged;
        any other V section (different text, several) → ReleaseRefused.
    """
    version = record["version"]
    section, _ = render_section(engine, slug, record)
    if base is None:
        full = export_changelog_md(engine, slug, include_unreleased=False)
        for _, start, end in reversed(_version_blocks(full, version)):
            full = full[:start] + full[end:]
        return _insert_section(full, section, version).encode("utf-8")
    try:
        text = base.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseRefused("the changelog file is not UTF-8 — not touched") from None
    existing = _version_blocks(text, version)
    if len(existing) == 1 and _norm_block(text[existing[0][1]:existing[0][2]]) == \
            _norm_block(section):
        return base
    if existing:
        raise ReleaseRefused(
            f"the changelog file already holds {len(existing)} {version} section(s) that "
            f"differ from the expected one — not overwritten.",
            details={"sections": len(existing)})
    return _insert_section(text, section, version).encode("utf-8")


def _changelog_base(run: _Run) -> tuple[bytes | None, bool]:
    """(F before the release, known) — the candidate's blob in git; for a
    project without git the current file when it holds no V section."""
    if run.repo:
        return gitinfo.git_read_blob(run.repo, run.record["candidate_sha"], run.changelog_rel), True
    current = _read_bytes(run.abspath(run.changelog_rel))
    if current is None:
        return None, True
    try:
        has_section = bool(_version_blocks(current.decode("utf-8"), run.version))
    except UnicodeDecodeError:
        raise run.refuse("the changelog file is not UTF-8 — not touched") from None
    return (None, False) if has_section else (current, True)


def _step_changelog(run: _Run) -> dict | None:
    """Write / verify F; returns the manifest entry {path, sha256,
    section_date} (None when the project has no changelog artifact)."""
    if run.changelog_rel is None:
        run.step("changelog", "skipped", reason="no changelog file managed")
        return None
    _point(run.on_point, "before_changelog")
    path = run.abspath(run.changelog_rel)
    current = _read_bytes(path)
    base, known = _changelog_base(run)
    _, date = render_section(run.engine, run.slug, run.record)
    try:
        if known:
            expected = expected_changelog(run.engine, run.slug, run.record, base)
        else:  # no git, F already holds a V section: it must be the identical one
            expected = expected_changelog(run.engine, run.slug, run.record, current)
    except ReleaseRefused as exc:
        raise run.refuse(str(exc), path=run.changelog_rel, **exc.details) from None
    if current == expected:
        run.step("changelog", "verified", path=run.changelog_rel)
    elif known and current == base:
        _write_atomic(path, expected)
        run.step("changelog", "done", path=run.changelog_rel)
    else:
        raise run.refuse(
            f"{run.changelog_rel} differs from both its content before the release and the "
            f"expected content (a partial or foreign edit?) — not overwritten; fix it "
            f"(restore it or finish the {run.version} section exactly) and resume.",
            path=run.changelog_rel)
    _point(run.on_point, "after_changelog")
    return {"path": run.changelog_rel, "sha256": _sha256(expected), "section_date": date}


# ── step 4: version sync + the version-file contract ─────────────────────────


def _sync_script(project_root: str) -> str | None:
    for rel in VERSION_SYNC_SCRIPTS:
        if os.path.isfile(os.path.join(project_root, rel)):
            return rel
    return None


def _run_sync(run: _Run) -> None:
    """Run the version-sync script from project_root (cwd = project_root),
    ONLY while current_version is the record's version (K-31/K-73). A
    non-zero exit raises SyncFailed (nothing advanced)."""
    _point(run.on_point, "before_sync")
    current = get_project(run.engine, run.slug)["current_version"]
    if not same_version(current, run.version):
        run.warnings.append(f"version-sync skipped: {run.version} is not the current "
                            f"version ({current})")
        run.step("sync", "skipped", reason="not the current version")
        return
    script = _sync_script(run.project_root)
    if script is None:
        run.step("sync", "skipped", reason="no version-sync script")
        return
    proc = subprocess.run(["bash", os.path.join(run.project_root, script), run.version],
                          cwd=run.project_root, capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    if output:
        print(output, file=sys.stderr, end="" if output.endswith("\n") else "\n")
    if proc.returncode != 0:
        raise SyncFailed(
            f"version-sync script failed (exit {proc.returncode}): {script} in "
            f"{run.project_root}. Release {run.version} is recorded ({run.record['status']}) "
            f"— fix the script and run `dct release resume`.",
            proc.returncode, details={"script": script, "exit_code": proc.returncode},
            steps=run.steps, release=run.record)
    run.step("sync", "done", script=script)


def _changed_files(run: _Run, pre_untracked: set[str]) -> list[str]:
    """Paths changed against HEAD (staged, unstaged, new untracked — minus
    the untracked files present before the release: the manifest's
    `untracked_baseline`, recorded with the stamp), without F."""
    state = planning.index_state(run.repo, [])
    if state["conflicted"]:
        raise run.refuse(f"conflicted paths: {', '.join(state['conflicted'])}",
                         conflicted=state["conflicted"])
    changed = set(state["staged"]) | set(state["unstaged"]) | (set(state["untracked"]) -
                                                               pre_untracked)
    changed.discard(run.changelog_rel)
    return sorted(changed)


def _version_files(run: _Run, pre_untracked: set[str]) -> list[dict]:
    """The verified version files [{path, sha256}] (K-73)."""
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    if run.repo is None:
        if contract is None:
            run.warnings.append("no git — the version-file set cannot be learned; none recorded")
            return []
        files = contract
    else:
        changed = _changed_files(run, pre_untracked)
        if contract is None:
            if not run.confirm_version_files:
                raise run.refuse(
                    f"first release of '{run.slug}': the version sync changed "
                    f"{', '.join(changed) or 'no files'} — review the list and confirm it as "
                    f"the project's version-file contract with `dct release resume "
                    f"--confirm-version-files` (nothing was committed).",
                    proposed_version_files=changed)
            ledger.set_version_files(run.engine, run.slug, changed, confirm=True)
            run.step("version_files", "learned", version_files=changed)
            files = changed
        elif changed != contract:
            raise run.refuse(
                f"the version sync changed {changed or 'no files'} but the project's "
                f"version-file contract is {contract} — nothing committed; fix the script or "
                f"the files and resume.", changed=changed, contract=contract)
        else:
            files = contract
    out, missing = [], []
    for rel in files:
        data = _read_bytes(run.abspath(rel))
        if data is None or run.version.encode("utf-8") not in data:
            missing.append(rel)
            continue
        out.append({"path": rel, "sha256": _sha256(data)})
    if missing:
        raise run.refuse(
            f"version files without {run.version}: {', '.join(missing)} — nothing committed.",
            missing=missing)
    return out


def _check_resume_tree(run: _Run) -> None:
    """Before re-running the file effects of a 'stamped' record: nothing
    staged or conflicted; unstaged paths only release artifacts (F and the
    contract; any path while no contract exists — the sync re-learns it)."""
    contract = ledger.get_version_files(run.engine, run.slug)["version_files"]
    state = planning.index_state(run.repo, [])
    if state["staged"] or state["conflicted"]:
        raise run.refuse(
            "staged or conflicted paths before the release files were recorded — unstage "
            "them yourself; nothing done.", staged=state["staged"],
            conflicted=state["conflicted"])
    if contract is not None:
        allowed = set(contract) | {run.changelog_rel}
        foreign = sorted(p for p in state["unstaged"] if p not in allowed)
        if foreign:
            raise run.refuse(f"foreign changes in the work tree: {', '.join(foreign)} — "
                             f"nothing done.", foreign=foreign)


def _step_files(run: _Run, pre_untracked: set[str]) -> None:
    """Steps 3-4 for a 'stamped' record → 'files_written' with the manifest."""
    manifest: dict = {}
    changelog = _step_changelog(run)
    if changelog:
        manifest["changelog"] = changelog
    if run.project_root:
        _run_sync(run)
        manifest["version_files"] = _version_files(run, pre_untracked)
    else:
        run.step("sync", "skipped", reason="no project path")
        manifest["version_files"] = []
    if run.repo:
        paths = [m["path"] for m in manifest["version_files"]]
        if changelog:
            paths.append(changelog["path"])
        manifest["tree"] = expected_tree(run.repo, run.record["candidate_sha"], paths)
    _point(run.on_point, "after_sync")
    run.advance("files_written", manifest=manifest)
    _point(run.on_point, "after_files_written")


def _manifest_paths(manifest: dict) -> list[str]:
    paths = [m["path"] for m in manifest.get("version_files") or []]
    if manifest.get("changelog"):
        paths.append(manifest["changelog"]["path"])
    return sorted(paths)


def _verify_manifest_files(run: _Run) -> None:
    """Every artifact of the manifest holds its recorded content."""
    manifest = run.record["manifest"] or {}
    entries = list(manifest.get("version_files") or [])
    if manifest.get("changelog"):
        entries.append(manifest["changelog"])
    bad = []
    for item in entries:
        data = _read_bytes(run.abspath(item["path"]))
        if data is None or _sha256(data) != item["sha256"]:
            bad.append(item["path"])
    if bad:
        raise run.refuse(
            f"release artifacts differ from the recorded manifest: {', '.join(bad)} — "
            f"not overwritten; restore them (or abandon the release).", changed=bad)


# ── step 5: the release commit ───────────────────────────────────────────────


def _verify_release_commit(run: _Run, commit: str) -> None:
    manifest = run.record["manifest"] or {}
    tree, parents = _tree(run.repo, commit), _parents(run.repo, commit)
    if tree != manifest.get("tree") or parents != [run.record["candidate_sha"]]:
        raise run.refuse(
            f"commit {commit} is not the release commit of {run.version}: tree {tree} "
            f"(expected {manifest.get('tree')}), parents {parents} (expected "
            f"[{run.record['candidate_sha']}]) — nothing done.",
            commit=commit, tree=tree, parents=parents)


def _step_commit(run: _Run) -> None:
    """'files_written' → 'committed' (see module doc, step 5)."""
    record, repo = run.record, run.repo
    candidate, manifest = record["candidate_sha"], record["manifest"] or {}
    subject = RELEASE_SUBJECT.format(version=run.version)
    head = _head(repo)
    if head != candidate:
        # Interrupted between update-ref and the progress write: adopt HEAD
        # only when it IS the release commit (parent, tree, subject).
        if _parents(repo, head) == [candidate] and _tree(repo, head) == manifest.get("tree") \
                and _subject(repo, head) == subject:
            _verify_manifest_files(run)
            run.step("commit", "adopted", commit=head)
            _point(run.on_point, "after_commit")
            run.advance("committed", release_commit_sha=head)
            _point(run.on_point, "after_committed")
            return
        raise run.refuse(
            f"HEAD is {head}, not the release candidate {candidate} — the release commit "
            f"is created on its candidate only; nothing done.", head=head, candidate=candidate)
    _verify_manifest_files(run)
    paths = _manifest_paths(manifest)
    expected = expected_tree(repo, candidate, paths)
    if expected != manifest.get("tree"):
        raise run.refuse(f"the artifacts no longer build the recorded tree "
                         f"{manifest.get('tree')} (got {expected}) — nothing committed.")
    index = _index_tree(repo)
    candidate_tree = _tree(repo, candidate)
    staged = _diff_names(repo, candidate_tree, index)
    pending = _diff_names(repo, index, expected)
    foreign_paths = sorted(staged - set(paths))
    foreign_content = sorted(staged & pending)
    if foreign_paths or foreign_content:
        raise run.refuse(
            "the index holds changes that are not the release artifacts "
            f"({', '.join(foreign_paths + foreign_content)}) — your staging is left "
            "untouched; unstage them yourself and resume.",
            foreign_paths=foreign_paths, foreign_content=foreign_content)
    if pending:
        gitinfo.git_write(repo, "add", "--", *sorted(pending))
    staged_tree = _index_tree(repo)
    if staged_tree != expected:
        raise run.refuse(
            f"after staging the release artifacts the index writes {staged_tree}, not the "
            f"expected tree {expected} — the index changed meanwhile; nothing committed.",
            index_tree=staged_tree, expected_tree=expected)
    _point(run.on_point, "after_stage")
    _, out = gitinfo.git_write(repo, "commit-tree", expected, "-p", candidate, "-m", subject)
    commit = out.strip()
    try:
        gitinfo.git_write(repo, "update-ref", "-m", subject, "HEAD", commit, candidate)
    except gitinfo.GitWriteError as exc:
        raise run.refuse(f"HEAD moved while the release commit was created ({exc}) — "
                         f"nothing committed; the artifacts stay staged.") from None
    if _head(repo) != commit:
        raise run.refuse(f"HEAD is not the new release commit {commit}")
    _verify_release_commit(run, commit)
    run.step("commit", "done", commit=commit)
    _point(run.on_point, "after_commit")
    run.advance("committed", release_commit_sha=commit)
    _point(run.on_point, "after_committed")


def _check_committed_head(run: _Run) -> None:
    """From 'committed' on HEAD must be the release commit or a descendant —
    no file effect remains, the sync is never re-run on a newer HEAD."""
    commit, head = run.record["release_commit_sha"], _head(run.repo)
    if head != commit and not gitinfo.git_is_ancestor(run.repo, commit, head):
        raise run.refuse(
            f"HEAD {head} does not contain the release commit {commit} — nothing done.",
            head=head, release_commit=commit)


# ── step 6: the tag ──────────────────────────────────────────────────────────


def _step_tag(run: _Run) -> None:
    """'committed' → 'tagged': `v<V>` (lightweight) at the verified commit."""
    commit = run.record["release_commit_sha"]
    _verify_release_commit(run, commit)
    name = f"v{run.version}"
    tags = _version_tags(run.repo, run.version)
    elsewhere = [t for t in tags if t["commit"] != commit]
    if elsewhere:
        raise run.refuse(
            f"tag(s) {', '.join(t['name'] for t in elsewhere)} of {run.version} point at "
            f"another commit — nothing tagged.", tags=elsewhere)
    here = [t["name"] for t in tags]
    if here:
        run.step("tag", "verified", tag=here[0] if name not in here else name)
    else:
        gitinfo.git_write(run.repo, "tag", name, commit)
        if _rev(run.repo, f"refs/tags/{name}^{{commit}}") != commit:
            raise run.refuse(f"tag {name} does not point at {commit} after creating it")
        run.step("tag", "done", tag=name)
    _point(run.on_point, "after_tag")
    run.advance("tagged")
    _point(run.on_point, "after_tagged")


# ── the pipeline (apply after the stamp, and resume) ─────────────────────────


def _continue(run: _Run, *, fresh: bool) -> None:
    """Perform / verify every effect still missing for run.record (`fresh`:
    straight after the stamp of a pre-checked apply; else a resume)."""
    status = run.record["status"]
    baseline = set((run.record["manifest"] or {}).get("untracked_baseline") or ())
    if run.repo:
        if status == "stamped":
            head = _head(run.repo)
            if head != run.record["candidate_sha"]:
                raise run.refuse(
                    f"HEAD is {head}, not the release candidate {run.record['candidate_sha']} "
                    f"— artifacts are created on the candidate only; nothing done.",
                    head=head, candidate=run.record["candidate_sha"])
            if not fresh:
                _check_resume_tree(run)
            _step_files(run, baseline)
        elif status == "files_written":
            run.step("files", "verified")
        if run.record["status"] == "files_written":
            _step_commit(run)
        elif run.record["status"] in ("committed", "tagged", "pushed"):
            _check_committed_head(run)
            _verify_release_commit(run, run.record["release_commit_sha"])
            run.step("commit", "verified", commit=run.record["release_commit_sha"])
        if run.record["status"] == "committed":
            _step_tag(run)
        elif run.record["status"] in ("tagged", "pushed"):
            commit = run.record["release_commit_sha"]
            if not any(t["commit"] == commit for t in _version_tags(run.repo, run.version)):
                raise run.refuse(f"release {run.version} is recorded as tagged but no tag of "
                                 f"it points at {commit} — nothing done.")
            run.step("tag", "verified")
    else:
        if status == "stamped":
            _step_files(run, set())
        else:
            _verify_manifest_files(run)
            run.step("files", "verified")
    run.advance("completed")
    run.step("complete", "done")


def _finish(action: str, run: _Run, outcome: str, **details) -> dict:
    return result(action, run.slug, outcome, version=run.record["version"] if run.record else None,
                  release=run.record, steps=run.steps, warnings=run.warnings, details=details)


# ── public API ───────────────────────────────────────────────────────────────


def _entry_selection(slug: str, plan: dict, target: str, entry_ids, no_entries: bool,
                     strict: bool) -> list[int]:
    proven = {e["id"] for e in plan["entries"]["proven"]}
    unproven = {e["id"]: e["reason"] for e in plan["entries"]["unproven"]}
    # A confirmed project without git (K-63) has no commits to prove with:
    # its entries are the owner's explicit choice, as before the proof layer.
    proven |= {i for i, reason in unproven.items() if reason == "no_git"}
    unproven = {i: r for i, r in unproven.items() if i not in proven}
    if no_entries:
        return []
    if entry_ids is None:
        if not strict:
            raise ReleaseRefused("choose the entries explicitly: --entries <ids> or --no-entries")
        if unproven:
            listing = ", ".join(f"#{i} ({r})" for i, r in sorted(unproven.items()))
            raise ReleaseRefused(
                f"unreleased entries without proof: {listing} — decide them first "
                f"(`dct release attest --entries … --confirm`, correct, withdraw, or release "
                f"a selection with `dct release apply --entries …`); nothing released.",
                details={"unproven": sorted(unproven)})
        if not proven:
            raise ReleaseRefused(
                f"No unreleased entries for '{slug}' — a version-only release of {target} "
                f"must be explicit (--no-entries); nothing released.")
        return sorted(proven)
    ids = sorted(set(entry_ids))
    if not ids:
        raise ReleaseRefused("--entries is empty — use --no-entries for a version-only release")
    # Ids the plan does not list at all (unknown, foreign, deleted, released)
    # go to core's STRICT validation — the stamp transaction admits a NEW
    # stamp only for the plan's proven ids (stampable_ids, K-81); the one
    # exception is an entry already carrying V (the legacy set, K-61).
    bad = [i for i in ids if i in unproven]
    if bad:
        listing = ", ".join(f"#{i} ({unproven[i]})" for i in bad)
        raise ReleaseRefused(
            f"entries the plan does not prove: {listing} — attest them first "
            f"(`dct release attest`) or leave them out; nothing released.",
            details={"unproven": bad})
    return ids


def apply_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    version: str | None = None,
    bump_type: str | None = None,
    entry_ids: Iterable[int] | None = None,
    no_entries: bool = False,
    confirm: bool = False,
    confirm_version_files: bool = False,
    strict: bool = False,
    plan_digest: str | None = None,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """One release from the APPROVED plan (`dct release apply`), see module doc.

    K-80: `plan_digest` is the `snapshot_digest` of the plan the owner
    approved (`dct release plan --version V --entries … --json`; required
    unless `strict`). The candidate is read ONCE (the strict checkout's
    HEAD) and the plan is re-derived FOR THAT CANDIDATE with the same
    version and selection — never on a HEAD read later; a snapshot that
    differs from the approved digest (another HEAD, range base, entry set
    or entry state, selection, version or proof) is refused before any
    database write. HEAD is checked again right before the stamp; the stamp
    itself binds the plan (core release_entries `stampable_ids` /
    `expected_entries`, K-81: only approved, proven ids get a new stamp; an
    entry added, edited or removed since the plan refuses inside the stamp
    transaction). A HEAD move after the stamp never re-pins the record: the
    effects refuse (they run on the recorded candidate only).

    `entry_ids` (owner-chosen ids, each proven by the plan or already
    carrying V) or `no_entries=True` (version-only) — one is required unless
    `strict` (the `clog release` alias: no ids = every unreleased entry, all
    of which must be proven; a range base other than v<current_version> is
    refused instead of being confirmed). `confirm=True` is required.
    `confirm_version_files` confirms the first release's learned
    version-file set (K-73). Returns result() with outcome 'completed' or
    'noop' (a recorded / historical / legacy release — no side effect).
    Raises ReleaseRefused (nothing done beyond what the result's steps
    say), SyncFailed, or gitinfo errors."""
    if confirm is not True:
        raise ReleaseRefused("apply needs --confirm (it stamps, writes, commits and tags)")
    if entry_ids is not None and no_entries:
        raise ReleaseRefused("--entries and --no-entries are mutually exclusive")
    if plan_digest is None and not strict:
        raise ReleaseRefused(
            "apply needs --plan-digest <snapshot_digest> of the plan the owner approved "
            "(`dct release plan --version V --entries … --json`); nothing released.")
    if plan_digest is not None and not _DIGEST_RE.fullmatch(plan_digest):
        raise ReleaseRefused(f"--plan-digest must be a lowercase hex SHA-256, got {plan_digest!r}")
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    if not project.get("path"):
        # K-95 C1: without a path the absence of a repository is NOT confirmed.
        raise ReleaseRefused(
            f"project '{project_slug}' has no registered path — its checkout cannot be "
            f"verified (a project outside git must be CONFIRMED outside git); register the "
            f"path first; nothing released.")
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=on_point,
               confirm_version_files=confirm_version_files)
    unfinished = ledger.get_release(engine, project_slug)
    if unfinished:
        raise ReleaseRefused(
            f"release {unfinished['version']} is unfinished ({unfinished['status']}) — run "
            f"`dct release resume` (or abandon it) first; nothing released.",
            details={"unfinished": unfinished["version"]}, release=unfinished)
    pre_untracked: set[str] = set()
    if checkout:
        state = _clean_tree(run, planning._artifacts(
            project, checkout, ledger.get_version_files(engine, project_slug)["version_files"]))
        pre_untracked = set(state["untracked"])
    try:
        plan = planning.build_plan(
            engine, project_slug, version=version, bump_type=bump_type, cwd=cwd,
            entry_ids=None if entry_ids is None else list(entry_ids), no_entries=no_entries,
            pinned_checkout=checkout)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    target = plan["version"]["target"]
    if not target:
        raise ReleaseRefused("Provide a version or --patch/--minor/--major")
    if plan_digest is not None and plan["snapshot_digest"] != plan_digest:
        raise ReleaseRefused(
            "the release context differs from the approved plan (HEAD, range base, "
            "unreleased entries, selection, version or proof changed) — run `dct release "
            "plan … --json` again, review it and approve its snapshot_digest; nothing "
            "released.",
            details={"approved_digest": plan_digest, "derived_digest": plan["snapshot_digest"],
                     "snapshot": plan["snapshot"]})
    if strict and plan["range"] and plan["range"]["confirm_required"]:
        raise ReleaseRefused(
            f"the proof range starts at {plan['range']['base_tag']}, not at "
            f"v{plan['version']['current']} — confirm it with `dct release apply … --confirm` "
            f"after reviewing `dct release plan`; nothing released.")
    run.warnings.extend(plan["warnings"])
    ids = _entry_selection(project_slug, plan, target, entry_ids, no_entries, strict)
    snapshot = plan["snapshot"]
    proof = {str(i): snapshot["proof"][str(i)] for i in ids if str(i) in snapshot["proof"]} \
        or None
    stampable = [i for i in ids if str(i) in snapshot["proof"]]
    if not checkout:  # K-63: a project CONFIRMED outside git — the owner's selection
        stampable = list(ids)
    before = ledger.get_release(engine, project_slug, target)
    _point(on_point, "before_stamp")
    if checkout and _head(checkout["repo_root"]) != checkout["head"]:
        raise ReleaseRefused(
            f"HEAD moved since the plan was derived for the candidate {checkout['head']} — "
            f"nothing released; plan again.", details={"candidate": checkout["head"]})
    try:
        stamped = release_entries(
            engine, project_slug, target, entry_ids=ids,
            candidate_sha=checkout["head"] if checkout else None,
            checkout_root=checkout["repo_root"] if checkout else None, proof=proof,
            # K-94 B1: _locate verified the checkout strictly — None means the
            # project is CONFIRMED outside git (a detection error refuses).
            release_mode="git" if checkout else "without_git",
            stampable_ids=stampable, expected_entries=snapshot["entries"])
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    details = {"requested_version": target, "previous_version": plan["version"]["current"],
               "bump": plan["version"]["bump"], "entry_ids": ids, "mode": stamped["mode"],
               "released": stamped["released"], "current_version": stamped["current_version"]}
    run.record = stamped["release"]
    if run.record is None:
        run.warnings.append(
            f"{target} is a legacy release without a record — nothing done "
            f"(`dct release reconstruct --version {target}` records it)")
        return _finish("apply", run, "noop", **details)
    if before is not None:
        run.step("stamp", "verified", status=run.record["status"])
        if run.record["status"] not in ledger.TERMINAL_STATUSES:
            raise run.refuse(f"release {run.version} is unfinished — run `dct release resume`")
        return _finish("apply", run, "noop", **details)
    run.step("stamp", "done", released=stamped["released"])
    if checkout:
        try:  # the pre-sync untracked files are never version files (resume too)
            run.advance("stamped", manifest={"untracked_baseline": sorted(pre_untracked)})
        except ValueError as exc:
            run.warnings.append(f"untracked baseline not recorded ({exc}) — a resume counts "
                                f"every untracked file as changed by the sync")
    _point(on_point, "after_stamp")
    _check_record_location(run)
    _continue(run, fresh=True)
    return _finish("apply", run, "completed", **details)


def resume_release(
    engine: sa.Engine,
    project_slug: str,
    *,
    confirm_version_files: bool = False,
    cwd: str | None = None,
    on_point: Callable[[str], None] | None = None,
) -> dict:
    """Finish the unfinished release record (`dct release resume`): verify
    the checkout against the record (same checkout root; HEAD == candidate
    before the commit, the release commit or a descendant after it — K-51;
    never re-pinned), then check each effect against reality and the
    manifest — present & matching → skipped, missing → performed (a legal
    retry of an interrupted sync on the candidate included), contradicting
    → ReleaseRefused without overwriting. No unfinished record → outcome
    'noop' (completed / abandoned releases have no side effects)."""
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=on_point,
               confirm_version_files=confirm_version_files)
    run.record = ledger.get_release(engine, project_slug)
    if run.record is None:
        run.warnings.append("no unfinished release — nothing to resume")
        return _finish("resume", run, "noop")
    _check_record_location(run)
    if run.repo and _operation_in_progress(run.repo):
        raise run.refuse("a git operation is in progress — finish or abort it first")
    _continue(run, fresh=False)
    return _finish("resume", run, "completed")


def abandon(engine: sa.Engine, project_slug: str, version: str, reason: str, *,
            confirm: bool = False) -> dict:
    """`dct release abandon` — core abandon_release (K-60): the record ends
    'abandoned', every effect stays."""
    try:
        out = ledger.abandon_release(engine, project_slug, version, reason, confirm=confirm)
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    record = out["release"]
    return result("abandon", project_slug, "abandoned" if out["updated"] else "noop",
                  version=record["version"], release=record, details={"updated": out["updated"]})


def attest(
    engine: sa.Engine,
    project_slug: str,
    entry_ids: Iterable[int],
    *,
    confirm: bool = False,
    cwd: str | None = None,
) -> dict:
    """`dct release attest` (K-16/K-69): the EMPTY attestation commit
    `chore: attest changelog entries` whose final paragraph is the trailer
    `Changelog: <ids ascending>` — on the current branch (or detached
    HEAD), only for ids the plan lists as unproven, only when nothing is
    staged and no operation is in progress. `git commit-tree HEAD^{tree}` +
    `git update-ref HEAD <new> <old>`: no hook runs, the index and the work
    tree are untouched. Returns result() with details {previous_head, head,
    entry_ids}; the candidate changes (no record exists yet)."""
    if confirm is not True:
        raise ReleaseRefused("attest needs --confirm (it creates a commit)")
    ids = sorted(set(entry_ids or ()))
    if not ids:
        raise ReleaseRefused("attest needs --entries <ids>")
    _, checkout, _ = _locate(engine, project_slug, cwd)
    if checkout is None:
        raise ReleaseRefused(f"project '{project_slug}' is not in a git checkout — nothing to "
                             f"attest in")
    unfinished = ledger.get_release(engine, project_slug)
    if unfinished:
        raise ReleaseRefused(f"release {unfinished['version']} is unfinished — resume or "
                             f"abandon it before attesting (its candidate is pinned)")
    repo = checkout["repo_root"]
    plan = planning.build_plan(engine, project_slug, cwd=cwd, pinned_checkout=checkout)
    unproven = {e["id"] for e in plan["entries"]["unproven"]}
    bad = [i for i in ids if i not in unproven]
    if bad:
        raise ReleaseRefused(
            f"the plan does not list {', '.join(f'#{i}' for i in bad)} as unproven (already "
            f"proven, released or unknown) — nothing attested.", details={"not_unproven": bad})
    busy = _operation_in_progress(repo)
    if busy:
        raise ReleaseRefused(f"a git operation is in progress ({', '.join(busy)})")
    head = checkout["head"]
    tree = _tree(repo, head)
    if _index_tree(repo) != tree:
        raise ReleaseRefused("the index holds staged changes — the attestation commit is "
                             "empty; unstage them first (nothing is reset for you).",
                             details={"staged": planning.staged_files(repo)})
    message = f"{ATTEST_SUBJECT}\n\nChangelog: {', '.join(str(i) for i in ids)}\n"
    _, out = gitinfo.git_write(repo, "commit-tree", tree, "-p", head, "-m", message)
    commit = out.strip()
    try:
        gitinfo.git_write(repo, "update-ref", "-m", ATTEST_SUBJECT, "HEAD", commit, head)
    except gitinfo.GitWriteError as exc:
        raise ReleaseRefused(f"HEAD moved while attesting ({exc}) — nothing attested") from None
    commits = planning.read_commits(repo, "-1", commit)
    if _head(repo) != commit or not set(ids) <= proven_ids(commits[0]):
        raise ReleaseRefused(f"the attestation commit {commit} does not prove {ids}")
    return result("attest", project_slug, "attested",
                  steps=[{"step": "attest", "result": "done", "commit": commit}],
                  details={"previous_head": head, "head": commit, "entry_ids": ids})


# ── reconstruct (K-60) ───────────────────────────────────────────────────────


def _release_commits(repo: str, version: str) -> list[str]:
    """Commits (any ref) whose subject is `release: v<same version>`."""
    _, out = gitinfo.git_read(repo, "log", "--all", "--format=%H%x1f%s")
    found = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x1f")
        match = _RELEASE_SUBJECT_RE.match(subject.strip())
        if match and _same_label(match.group(1), version):
            found.append(sha.lower())
    return sorted(set(found))


def gather_evidence(engine: sa.Engine, project_slug: str, version: str, *,
                    cwd: str | None = None) -> dict:
    """Read-only evidence of a legacy release of `version` WITHOUT a record
    (K-60). Refuses (ReleaseRefused) on ANY ambiguity — never guesses:
    a record exists; no entry carries V and it is not current_version;
    several release commits or tags pointing elsewhere; a release commit
    without a tag or a tag without the release commit; a release commit
    that is a merge; the changelog section missing / doubled / different
    from the database render; version files that do not contain V or
    differ from the project's contract. Returns {version, entry_ids,
    candidate_sha, release_commit_sha, tag, checkout_root, manifest,
    status, without_git}."""
    version = normalize_version(version)
    project, checkout, project_root = _locate(engine, project_slug, cwd)
    if ledger.get_release(engine, project_slug, version) is not None:
        raise ReleaseRefused(f"version {version} already has a release record — nothing to "
                             f"reconstruct")
    entries = list_entries(engine, project_slug, version=version)
    ids = sorted(e["id"] for e in entries)
    current = project.get("current_version")
    if not ids and not same_version(current, version):
        raise ReleaseRefused(f"no entry carries {version} and it is not the current version "
                             f"({current}) — the database does not prove this release")
    run = _Run(engine, project_slug, project, checkout, project_root, on_point=None,
               confirm_version_files=False)
    contract = ledger.get_version_files(engine, project_slug)["version_files"]
    evidence = {"version": version, "entry_ids": ids, "candidate_sha": None,
                "release_commit_sha": None, "tag": None, "checkout_root": None,
                "without_git": checkout is None, "status": "completed"}
    manifest: dict = {}
    if checkout is None:
        if not project_root:
            raise ReleaseRefused("the project has no path — no artifact can be verified")
        def read(rel: str) -> bytes | None:
            return _read_bytes(run.abspath(rel))
        files = contract or []
    else:
        repo = checkout["repo_root"]
        commits = _release_commits(repo, version)
        tags = _version_tags(repo, version)
        tag_commits = sorted({t["commit"] for t in tags})
        if len(commits) != 1:
            raise ReleaseRefused(
                f"{len(commits)} commits titled `release: v{version}` — "
                f"{'none' if not commits else 'ambiguous'}; refused, never guessed.",
                details={"release_commits": commits})
        commit = commits[0]
        if tag_commits != [commit]:
            raise ReleaseRefused(
                f"the tag(s) of {version} ({', '.join(t['name'] for t in tags) or 'none'}) do "
                f"not point exactly at the release commit {commit} — refused.",
                details={"tags": tags, "release_commit": commit})
        parents = _parents(repo, commit)
        if len(parents) != 1:
            raise ReleaseRefused(f"the release commit {commit} has {len(parents)} parents — "
                                 f"the candidate is ambiguous; refused.")
        evidence.update(candidate_sha=parents[0], release_commit_sha=commit,
                        tag=next((t["name"] for t in tags if t["name"] == f"v{version}"),
                                 tags[0]["name"]),
                        checkout_root=repo)
        manifest["tree"] = _tree(repo, commit)
        def read(rel: str, _commit: str = commit) -> bytes | None:
            return gitinfo.git_read_blob(repo, _commit, rel)
        changed = sorted(_diff_names(repo, parents[0], commit) - {run.changelog_rel})
        if contract is not None and changed != contract:
            raise ReleaseRefused(
                f"the release commit changed {changed} but the version-file contract is "
                f"{contract} — refused.", details={"changed": changed, "contract": contract})
        files = changed
    if run.changelog_rel:
        data = read(run.changelog_rel)
        text = data.decode("utf-8", "strict") if data is not None else None
        blocks = _version_blocks(text, version) if text is not None else []
        if len(blocks) != 1:
            raise ReleaseRefused(f"{run.changelog_rel} holds {len(blocks)} {version} sections — "
                                 f"refused.")
        block = text[blocks[0][1]:blocks[0][2]]
        header = block.split("\n", 1)[0].rstrip("\r")
        date = header.rsplit(" - ", 1)[-1].strip() if " - " in header else None
        stored = {"version": blocks[0][0], "entry_ids": ids,
                  "created_at": date or datetime.now(UTC).date().isoformat()}
        try:
            section, _ = render_section(engine, project_slug, {
                **stored, "version": normalize_version(blocks[0][0])})
        except ReleaseRefused as exc:
            raise ReleaseRefused(f"the section cannot be rendered: {exc}") from None
        body = _norm_block(block).split("\n", 1)[-1]
        if body != _norm_block(section).split("\n", 1)[-1]:
            raise ReleaseRefused(f"the {version} section of {run.changelog_rel} differs from the "
                                 f"database's entries — refused.")
        manifest["changelog"] = {"path": run.changelog_rel, "sha256": _sha256(data),
                                 "section_date": date}
    missing, version_files = [], []
    for rel in files:
        data = read(rel)
        if data is None or version.encode("utf-8") not in data:
            missing.append(rel)
        else:
            version_files.append({"path": rel, "sha256": _sha256(data)})
    if missing:
        raise ReleaseRefused(f"version files without {version}: {', '.join(missing)} — refused.",
                             details={"missing": missing})
    manifest["version_files"] = version_files
    evidence["manifest"] = manifest
    return evidence


def reconstruct(engine: sa.Engine, project_slug: str, version: str, *,
                confirm: bool = False, cwd: str | None = None) -> dict:
    """`dct release reconstruct` (K-60): gather_evidence (read-only) and,
    only with confirm=True, core reconstruct_release with exactly that
    evidence (no re-stamp). Outcome 'preview' (nothing written) or
    'reconstructed'."""
    evidence = gather_evidence(engine, project_slug, version, cwd=cwd)
    if not confirm:
        return result("reconstruct", project_slug, "preview", version=evidence["version"],
                      details={"evidence": evidence})
    try:
        record = ledger.reconstruct_release(
            engine, project_slug, evidence["version"], entry_ids=evidence["entry_ids"],
            candidate_sha=evidence["candidate_sha"],
            release_commit_sha=evidence["release_commit_sha"], manifest=evidence["manifest"],
            status=evidence["status"], confirm=True, checkout_root=evidence["checkout_root"],
            without_git=evidence["without_git"])
    except ValueError as exc:
        raise ReleaseRefused(str(exc)) from None
    return result("reconstruct", project_slug, "reconstructed", version=record["version"],
                  release=record, details={"evidence": evidence})
