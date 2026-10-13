"""Claude Code integration adapter (ADR #51 §6.3, moved from dct/setup.py
in CP #1235): register the MCP server user-scope, install hook and skill
symlinks into the host's ~/.claude, wire hooks into settings.json."""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from dct import assets
from dct.config import Config
from dct.integrations.base import (
    ClientIntegration,
    _copy_inventory,
    atomic_write,
    forget_managed_link,
    is_managed_copy,
    is_recorded_link,
    managed_instructions,
    mark_managed_copy,
    read_exact_text,
    record_managed_link,
)


def _link_or_copy(src: Path, dst: Path, *, mark_file: bool = False) -> str:
    """Symlink dst → src; when symlinks are unavailable fall back to a real
    copy (W5: Windows without Developer Mode / SeCreateSymbolicLinkPrivilege
    raises WinError 1314). Copies drift until the next `dct upgrade`
    refreshes them — say so instead of failing the whole init.

    Never overwrites (K-58): the caller checked or cleared ``dst``, but a
    file, directory or link that appears there before the creation is left
    untouched — the symlink and the copy are both exclusive creations, never
    a write through an existing path — and ``"preserved"`` is returned. Any
    other error of the copy (permissions, I/O) propagates: it is never
    reported as an installed asset.

    A directory copy carries the content manifest (``mark_managed_copy``);
    ``mark_file=True`` gives a single Markdown file the same proof inline
    (``_write_marked_copy``) — hooks stay unmarked, a header would break a
    script. Returns ``"symlink"``, ``"copy"`` or ``"preserved"``."""
    try:
        dst.symlink_to(src)
    except FileExistsError:
        return "preserved"
    except OSError:
        try:
            if src.is_dir():
                # copytree creates dst itself and refuses an existing path
                shutil.copytree(src, dst)
                mark_managed_copy(dst)
            elif mark_file:
                _write_marked_copy(src, dst)
            else:
                _create_exclusive(dst, src.read_bytes(), mode=src.stat().st_mode)
        except FileExistsError:
            return "preserved"
        print(
            f"  note: symlinks unavailable — copied {src.name} instead "
            "(copies drift; `dct upgrade` refreshes them)"
        )
        return "copy"
    return "symlink"


def _create_exclusive(dst: Path, data: bytes, *, mode: int | None = None) -> None:
    """Create ``dst`` holding ``data``; raise FileExistsError when anything —
    a file, a directory, a live or dangling symlink — already sits there
    (O_EXCL never follows a link). A failed write removes the file this call
    created, so no truncated copy is left behind as someone's file."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode & 0o7777)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


# Single-file counterpart of base.mark_managed_copy / is_managed_copy: the
# first line records the sha256 of the rest, so only an UNCHANGED copy dct
# wrote is recognised as dct's (an edited one becomes the user's file).
_COPY_HEADER = re.compile(r"<!-- dct:managed-copy sha256=([0-9a-f]{64}) [^\n]*-->\n")


def _write_marked_copy(src: Path, dst: Path) -> None:
    body = src.read_bytes()
    header = (f"<!-- dct:managed-copy sha256={hashlib.sha256(body).hexdigest()} "
              "(managed by dct init; an edited copy is left alone) -->\n")
    _create_exclusive(dst, header.encode("utf-8") + body)


def _marked_copy_body(path: Path) -> bytes | None:
    """The copied bytes of an unchanged marked copy; None for anything else."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    match = _COPY_HEADER.match(raw.decode("utf-8", errors="replace"))
    if not match:
        return None
    body = raw[len(match.group(0).encode("utf-8")):]
    return body if hashlib.sha256(body).hexdigest() == match.group(1) else None

SKILLS_DIR = assets.skills_dir()
HOOKS_DIR = assets.hooks_dir()
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
# K-36: the dct reference read on demand — the path the attached CLAUDE.md
# block names (dct/assets/claude-md-snippet.md); one constant for both ends.
CLAUDE_REFERENCE = Path.home() / ".claude" / "dct-reference.md"

# Pre-unlink symlink recheck — a dedicated seam (not a direct os.path.islink
# call) so tests can fake the TOCTOU swap without patching the global
# predicate, which since Python 3.13 also backs Path.is_symlink.
_still_symlink = os.path.islink

# Canonical wiring of the four dct hooks into ~/.claude/settings.json.
# event/matcher come from each hook script's own header (the authoritative
# self-description) — e.g. dct-task-mirror.sh declares PreToolUse on
# TodoWrite|TaskCreate. matcher=None means the event takes no matcher (Stop).
HOOK_WIRING = [
    {"script": "dct-check-changelog.sh", "event": "PreToolUse",
     "matcher": "Bash", "timeout": 10},
    {"script": "dct-check-changelog-on-stop.sh", "event": "Stop",
     "matcher": None, "timeout": 5},
    {"script": "dct-plan-autoscan.sh", "event": "PostToolUse",
     "matcher": "Edit|Write|MultiEdit", "timeout": None},
    {"script": "dct-task-mirror.sh", "event": "PreToolUse",
     "matcher": "TodoWrite|TaskCreate", "timeout": None},
]

# Shared bash libraries the hooks source (K-48) — NOT hooks, never wired into
# settings.json. Installed next to the hooks under the SAME name: a symlinked
# hook finds the lib beside its real path in the package anyway, but a COPIED
# hook (no symlink privilege) only finds the copy installed beside it.
HOOK_LIBS = ["dct-changelog-count.lib.sh"]

def discover_skills() -> list[str]:
    """Skill directories shipped inside the package (each holds a SKILL.md).

    Derived from the filesystem on every call — dct/skills/ is the single
    source of truth. Never reintroduce a hardcoded name list: a stale one
    silently dropped plan-status and plan-resolve-uncertain from every
    install for two releases.
    """
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def register_mcp(config: Config) -> tuple[bool, str]:
    """Register dct MCP server at user scope (~/.claude.json).

    Uses the `dct server` subcommand as the stdio entry point — requires that
    `dct` itself is installed in PATH (via `uv tool install`, `pipx install`,
    or `pip install`). Zero hardcoded filesystem paths in the resulting config.

    Tries `claude mcp add --scope user` CLI first; falls back to direct
    ~/.claude.json edit if `claude` CLI is unavailable.
    """
    server_cmd = ["dct", "server"]

    claude_exe = shutil.which("claude")
    if claude_exe:
        cmd = [claude_exe, "mcp", "add", "dct", "--scope", "user", "--"] + server_cmd
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError:
            # W6: on Windows `which` finds the claude.cmd shim via PATHEXT,
            # but CreateProcess cannot exec a .cmd from an argv list — fall
            # through to the direct edit instead of crashing dct init.
            result = None
        if result is not None:
            if result.returncode == 0:
                return True, "registered via `claude mcp add --scope user`"
            if "already exists" in (result.stderr + result.stdout).lower():
                return True, "already registered"
        # fall through to direct edit

    # Fallback: direct ~/.claude.json edit (UTF-8 both ways — W7: this file
    # is the user's whole Claude Code config; a locale re-encode corrupts it)
    data = {}
    if CLAUDE_JSON.exists():
        with open(CLAUDE_JSON, encoding="utf-8") as f:
            data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError("Claude MCP configuration must contain an object; nothing was written")  # noqa: TRY004 — user-file validation contract
    servers = data.setdefault("mcpServers", {})
    if "dct" in servers:
        # Existing entries may use wrappers, extra args, env, or custom fields.
        # Their presence is not ownership permission to normalize them.
        return True, "already registered (existing configuration preserved)"
    servers["dct"] = {
        "type": "stdio", "command": server_cmd[0], "args": server_cmd[1:],
    }
    atomic_write(CLAUDE_JSON, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True, "registered via direct ~/.claude.json edit"


def install_hook(config: Config | None = None) -> bool:
    """Symlink dct hooks into ~/.claude/hooks/ (single source of truth, #224).

    Installs four hooks as SYMLINKS to the packaged dct/hooks/ directory (like
    install_skills) — site-packages for a wheel install, the repo working tree
    for an editable one, where a repo fix is live immediately without reinstall.
    Copies drift from source — a stale installed copy is exactly what masked the
    #223 clog-count bug timeline. The installed name is `dct-`-prefixed while the
    packaged source is not (e.g. dct/hooks/check-changelog.sh → dct-check-changelog.sh):
    - dct-check-changelog.sh (PreToolUse) — blocks commits without changelog
    - dct-check-changelog-on-stop.sh (Stop) — reminds user at session end
    - dct-plan-autoscan.sh (PostToolUse, v0.2.0) — re-scans plan-like MD files
      in the background after edit/write, injecting ULIDs and updating DB state
    - dct-task-mirror.sh (PostToolUse) — mirrors Claude Code tasks into dct
    plus the shared libs in ``HOOK_LIBS`` (same name, not wired — sourced by
    the two changelog hooks; a copied hook needs its copied lib).

    Each hook is independently optional: a source missing from the dct repo (old
    checkout) is skipped silently. An existing pre-#224 regular-file copy is
    replaced in place by the symlink. Wiring into `~/.claude/settings.json` is
    handled separately by `wire_hooks_into_settings()` — called from `dct init`
    (consent-gated) or via `dct hooks wire`; manual reference in docs/hooks.md.

    Caveat: a symlinked hook tracks the repo WORKING TREE, including uncommitted
    edits — intentional for a dev checkout (instant fixes) but means a dirty repo
    affects live hook behaviour. `config` is accepted for call-site symmetry with
    the other install_* helpers but is unused.
    """
    CLAUDE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    installed = 0

    for src_name, dst_name in [
        ("check-changelog.sh", "dct-check-changelog.sh"),
        ("check-changelog-on-stop.sh", "dct-check-changelog-on-stop.sh"),
        ("dct-plan-autoscan.sh", "dct-plan-autoscan.sh"),
        ("dct-task-mirror.sh", "dct-task-mirror.sh"),
        *((name, name) for name in HOOK_LIBS),
    ]:
        src = HOOKS_DIR / src_name
        dst = CLAUDE_HOOKS_DIR / dst_name
        if not src.exists():
            continue
        # Replace any existing symlink OR a pre-#224 regular-file copy in place.
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        if _link_or_copy(src, dst) == "preserved":
            print(_appeared_note(dst))
            continue
        installed += 1

    return installed > 0


def wire_hooks_into_settings(settings_path: Path | None = None) -> dict:
    """Merge the four dct hook entries into ~/.claude/settings.json (idempotent).

    Merge semantics:
    - Missing or empty settings file → treated as ``{}`` and created.
    - Idempotency probe is GLOBAL by script basename: a hook already mentioned
      anywhere in ``hooks`` (any event, any matcher group, any path form —
      absolute, ``~``-relative, with or without ``bash `` prefix) is skipped.
      This deliberately respects a user's custom placement of a dct hook.
    - A missing event list or matcher group is created; an existing group with
      the same matcher gets the entry appended. User-defined hooks and all
      unrelated top-level keys are never modified.
    - Before the first actual write of a pre-existing file, the original is
      copied to ``settings.json.bak-dct-<timestamp>`` next to it.
    - The write is atomic (temp file + ``os.replace``).
    - Malformed JSON or a non-object ``hooks`` key raises ``ValueError`` and
      NOTHING is written — never destructive.

    Returns a summary dict: ``{"added": [script names], "skipped": [...],
    "backup": str | None, "settings_path": str}``.
    """
    if settings_path is None:
        settings_path = CLAUDE_SETTINGS
    settings_path = Path(settings_path)

    raw = settings_path.read_text(encoding="utf-8") if settings_path.exists() else None
    if raw is None or not raw.strip():
        settings = {}
    else:
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{settings_path} is not valid JSON ({exc}) — fix it manually; "
                "nothing was written"
            ) from exc
    if not isinstance(settings, dict):
        # Suppression rationale: user-file schema validation — ValueError is
        # the user-facing contract here, not an API argument type check.
        raise ValueError(  # noqa: TRY004
            f"{settings_path}: top-level JSON must be an object; nothing was written"
        )
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(  # noqa: TRY004 — same user-file validation contract
            f"{settings_path}: 'hooks' must be an object, "
            f"found {type(hooks).__name__}; nothing was written"
        )

    existing_commands = []
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                if isinstance(hook, dict):
                    existing_commands.append(str(hook.get("command", "")))

    added, skipped = [], []
    for spec in HOOK_WIRING:
        name = spec["script"]
        if any(name in cmd for cmd in existing_commands):
            skipped.append(name)
            continue
        entry = {"type": "command", "command": f"bash {shlex.quote(str(CLAUDE_HOOKS_DIR / name))}"}
        if spec["timeout"] is not None:
            entry["timeout"] = spec["timeout"]
        groups = hooks.setdefault(spec["event"], [])
        target = next(
            (g for g in groups
             if isinstance(g, dict) and g.get("matcher") == spec["matcher"]),
            None,
        )
        if target is None:
            target = {"hooks": []}
            if spec["matcher"] is not None:
                target["matcher"] = spec["matcher"]
            groups.append(target)
        target.setdefault("hooks", []).append(entry)
        added.append(name)

    backup = None
    if added:
        backup = atomic_write(
            settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )

    return {
        "added": added,
        "skipped": skipped,
        "backup": backup,
        "settings_path": str(settings_path),
    }


def prune_dangling_symlinks() -> list[str]:
    """Remove dangling symlinks under ~/.claude/skills and ~/.claude/hooks whose
    targets point into the dct package but no longer exist (#356).

    Only DANGLING links (``entry.exists()`` is False after following the link)
    whose resolved target lies inside the packaged ``dct/skills`` or ``dct/hooks``
    source tree are removed — a live link, or a broken link to some other tool,
    is left untouched. Complements ``install_skills`` / ``install_hook`` after a
    ``dct upgrade`` that dropped a skill or hook from the package.

    Returns the list of removed link paths (strings), sorted for determinism.
    """
    removed: list[str] = []
    for base, source in ((CLAUDE_SKILLS_DIR, SKILLS_DIR), (CLAUDE_HOOKS_DIR, HOOKS_DIR)):
        if not base.is_dir():
            continue
        source_real = os.path.realpath(source)
        for entry in sorted(base.iterdir()):
            # Only dangling symlinks: a live link resolves and .exists() is True.
            if not entry.is_symlink() or entry.exists():
                continue
            # os.path.realpath resolves a dangling link to its (absent) target,
            # normalizing /var→/private/var symlinks on both sides.
            target_real = os.path.realpath(entry)
            if target_real == source_real or target_real.startswith(source_real + os.sep):
                # Re-verify it is STILL a symlink immediately before unlinking:
                # narrows the TOCTOU window so a path that stopped being a symlink
                # between the scan and here (e.g. a regular file swapped in) is
                # never removed. Tolerate a concurrent unlink (FileNotFoundError).
                try:
                    if _still_symlink(entry):
                        entry.unlink()
                        removed.append(str(entry))
                except FileNotFoundError:
                    pass
    return removed


def install_skills(config: Config | None = None) -> bool:
    """Symlink every packaged skill (dct/skills/*) into ~/.claude/skills/.

    Sources come from the installed package — site-packages for a wheel
    install, the repo working tree for an editable one (where a repo fix is
    live without reinstall). ``config`` is accepted for call-site symmetry with
    the other install_* helpers but is unused (default ``None``).
    Fallback copies carry a content manifest: only unchanged managed copies
    refresh on upgrade. Unmarked or locally modified directories are preserved.
    """
    CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    installed = 0

    for name in discover_skills():
        src = SKILLS_DIR / name
        dst = CLAUDE_SKILLS_DIR / name

        # Remove existing symlink or directory
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            if not is_managed_copy(dst):
                continue  # preserve unmarked or locally modified directories
            shutil.rmtree(dst)

        if _link_or_copy(src, dst) == "preserved":
            print(_appeared_note(dst))
            continue
        installed += 1

    return installed > 0


def _appeared_note(dst: Path) -> str:
    return f"  note: {dst} appeared while dct was installing it — left untouched"


def _owned_reference(path: Path) -> bool:
    """dct's own reference install (K-57: proven, never guessed): a symlink
    resolving into the RUNNING package's assets dir (also a dangling one —
    a file an upgrade dropped — and the hand-made link to
    assets/claude-md-snippet.md, K-76), a symlink whose exact target dct
    recorded when it created it (``is_recorded_link`` — survives a move of
    the package: new venv, new Python, editable <-> wheel), or an unchanged
    marked copy. A dangling link or a path that merely looks like another
    dct install proves nothing."""
    if path.is_symlink():
        target = os.path.realpath(path)
        source = os.path.realpath(assets.assets_dir())
        return target.startswith(source + os.sep) or is_recorded_link(path)
    return _marked_copy_body(path) is not None


def _reference_current(path: Path) -> bool:
    """The installed reference is the current packaged one (doctor's byte
    comparison, as for hook files)."""
    src = assets.dct_reference_path()
    try:
        if path.is_symlink():
            return path.resolve() == src.resolve()
        return _marked_copy_body(path) == src.read_bytes()
    except OSError:
        return False


# The one description of `dct integrate repair claude` (install_integration
# over this adapter) — every remedy offering it names its whole scope, since
# it is run only with the user's consent to all of it (K-73).
REPAIR_REMEDY = (
    "`dct integrate repair claude` installs it as part of the full Claude Code "
    "integration repair: it registers the dct MCP server (an existing entry is "
    "kept), installs the skills and hook files, wires the hooks into "
    "~/.claude/settings.json, adds the dct pointer block to ~/.claude/CLAUDE.md "
    "(refreshes it if present) and installs this reference. Run it only if you "
    "want all of that."
)


def reference_state(path: Path | None = None) -> dict:
    """Classify the installed reference (K-66): ``current`` / ``missing`` /
    ``outdated`` (dct's, but not the running package's reference) /
    ``foreign`` (not proven dct's — dct never touches it).

    Returns ``{"state", "path", "target" (link target or None), "message",
    "remedy" (None when current)}``. A ``foreign`` remedy names that specific
    file (and its link target) and asks the user to move it to an unused name
    or replace it after keeping what they need, then repair: a repair alone
    would preserve it again. It never carries a ready-made shell command or
    a fixed backup name (K-78): a pasted command breaks on spaces or quotes
    and could overwrite an earlier backup.
    """
    path = CLAUDE_REFERENCE if path is None else Path(path)
    target = os.readlink(path) if path.is_symlink() else None
    report = {"path": str(path), "target": target}
    if target is None and not path.exists():
        return {**report, "state": "missing", "message": f"dct reference missing at {path}",
                "remedy": REPAIR_REMEDY}
    if not _owned_reference(path):
        if target is None:
            what = "a file dct did not install, or a copy edited since"
        else:
            dangling = "" if os.path.exists(path) else "dangling "
            what = f"a {dangling}link to {target} that dct did not create"
        subject = str(path) if target is None else f"{path} (the link itself, to {target})"
        return {**report, "state": "foreign",
                "message": f"{path} is not dct's ({what}) — dct leaves it untouched",
                "remedy": f"To use dct's reference, first move {subject} to an unused name, "
                          f"or replace it once you have kept what you need from it; dct never "
                          f"moves, replaces or removes it for you. After that, "
                          + REPAIR_REMEDY}
    if _reference_current(path):
        return {**report, "state": "current", "message": f"Packaged dct reference at {path}",
                "remedy": None}
    was = f"dct's link to {target}" if target is not None else "dct's copy from an older package"
    return {**report, "state": "outdated",
            "message": f"Outdated dct reference at {path} ({was})", "remedy": REPAIR_REMEDY}


def reference_row() -> dict:
    """The doctor row for ``CLAUDE_REFERENCE``: ``reference_state`` plus the
    shared row keys (``check``, ``status``: ok only when current)."""
    state = reference_state()
    return {"check": "reference", "status": "ok" if state["state"] == "current" else "error",
            "state": state["state"], "message": state["message"], "remedy": state["remedy"]}


def install_reference(config: Config | None = None) -> dict:
    """Install ~/.claude/dct-reference.md → the packaged dct reference (K-36).

    The attached CLAUDE.md block only names this path; the reference itself
    is read on demand. Installed on every path that installs skills/hooks,
    independent of the CLAUDE.md attach consent. Ownership is proven, never
    guessed (``_owned_reference``): an owned link — also one into a previous
    install location that dct recorded — or an unchanged marked copy is
    replaced; a user's own file or a foreign symlink is never touched — it is
    preserved and reported with its cause and remedy (``reference_state``).
    A symlink dct creates is recorded (``record_managed_link``), so it
    re-points after the package moves. A file appearing at the path during
    the install is preserved too (``_link_or_copy``). ``config`` is unused
    (call-site symmetry with the other install_* helpers).

    Returns ``{"status": "installed" | "refreshed" | "unchanged" |
    "preserved", "path": str, "mode": "symlink" | "copy" | None,
    "target": str | None, "message": str, "remedy": str | None}``. Raises
    ``OSError`` when the file cannot be created (never a false success).
    """
    src = assets.dct_reference_path()
    dst = CLAUDE_REFERENCE
    state = reference_state(dst)
    if state["state"] == "foreign":
        return {"status": "preserved", "path": str(dst), "mode": None, "target": state["target"],
                "message": state["message"], "remedy": state["remedy"]}
    if state["state"] == "current":
        mode = "symlink" if dst.is_symlink() else "copy"
        if mode == "symlink" and not is_recorded_link(dst):
            record_managed_link(dst)  # adopt a current link made before records existed
        return {"status": "unchanged", "path": str(dst), "mode": mode, "target": state["target"],
                "message": f"dct reference current at {dst}", "remedy": None}
    present = state["state"] == "outdated"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if present:
        dst.unlink()
        forget_managed_link(dst)
    mode = _link_or_copy(src, dst, mark_file=True)
    if mode == "preserved":
        now = reference_state(dst)
        return {"status": "preserved", "path": str(dst), "mode": None, "target": now["target"],
                "message": f"{dst} appeared while dct was installing the reference — left untouched; "
                           + now["message"],
                "remedy": now["remedy"]}
    if mode == "symlink":
        record_managed_link(dst)
    status = "refreshed" if present else "installed"
    return {"status": status, "path": str(dst), "mode": mode, "target": str(src) if mode == "symlink" else None,
            "message": f"dct reference {status} at {dst}", "remedy": None}


# CLAUDE.md snippet markers — the managed block attach_claude_md_snippet()
# owns inside the user's ~/.claude/CLAUDE.md. Content between them is
# refreshed on re-attach; everything outside is never touched.
SNIPPET_START = (
    "<!-- dct:claude-md-snippet:start "
    "(managed by dct init — edits inside this block are overwritten on refresh) -->"
)
SNIPPET_END = "<!-- dct:claude-md-snippet:end -->"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"


def attach_claude_md_snippet(claude_md_path: Path | None = None) -> dict:
    """Append (or refresh) the packaged dct pointer in ~/.claude/CLAUDE.md (#443).

    The packaged snippet file opens with a human-facing preamble; everything
    below its FIRST ``---`` separator is the actual instruction block — since
    K-36 a short pointer naming ``CLAUDE_REFERENCE`` (the full reference is
    installed separately by ``install_reference``, never pasted here; a block
    still holding the old full body refreshes to the pointer). That block is
    written between ``SNIPPET_START``/``SNIPPET_END`` markers:

    - target file missing or empty → created with just the managed block;
    - markers absent → block appended at the end (blank-line separated);
    - markers present → content between them replaced (refresh); when the
      refreshed text is byte-identical, nothing is written;
    - user content outside the markers is never modified;
    - before the first actual write of a pre-existing file, the original is
      copied to ``CLAUDE.md.bak-dct-<timestamp>`` next to it; the write is
      atomic (temp file + ``os.replace``) — same contract as
      ``wire_hooks_into_settings``.

    Returns ``{"status": "added" | "refreshed" | "unchanged",
    "backup": str | None, "path": str}``. Raises ``ValueError`` — with nothing
    written — when the packaged snippet is missing or has no ``---``
    separator, or when the target has malformed markers (start without end,
    or a stray end without start).
    """
    if claude_md_path is None:
        claude_md_path = CLAUDE_MD
    claude_md_path = Path(claude_md_path)

    src = assets.claude_snippet_path()
    if not src.is_file():
        raise ValueError(f"packaged snippet missing: {src}")
    raw_snippet = src.read_text(encoding="utf-8")
    separator = "\n---\n"
    if separator not in raw_snippet:
        raise ValueError(
            f"{src}: no '---' separator — cannot split off the preamble"
        )
    body = raw_snippet.split(separator, 1)[1].strip("\n")
    return managed_instructions(claude_md_path, body, SNIPPET_START, SNIPPET_END)


class ClaudeIntegration(ClientIntegration):
    """Claude adapter preserving the legacy installer API and user configuration."""

    name = "claude"

    def detect(self) -> bool:
        try:
            data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "dct" in data.get("mcpServers", {}):
                return True
        except (OSError, ValueError, TypeError):
            pass
        if any((CLAUDE_SKILLS_DIR / name).is_symlink()
               or is_managed_copy(CLAUDE_SKILLS_DIR / name) for name in discover_skills()):
            return True
        return CLAUDE_MD.is_file() and SNIPPET_START in CLAUDE_MD.read_text(encoding="utf-8")

    def install_mcp(self) -> dict:
        ok, message = register_mcp(None)
        return {"status": "ok" if ok else "error", "message": message}

    def install_skills(self) -> dict:
        installed = install_skills()
        hooks = install_hook()
        wiring = wire_hooks_into_settings()
        reference = install_reference()
        return {"status": "ok", "installed": installed, "hooks": hooks, "wiring": wiring,
                "reference": reference}

    def install_instructions(self) -> dict:
        return attach_claude_md_snippet()

    def doctor(self) -> list[dict]:
        rows = []
        try:
            data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
            entry = data.get("mcpServers", {}).get("dct")
            if not isinstance(entry, dict):
                raise ValueError("DCT MCP missing")  # noqa: TRY004 — user-file validation contract
            command, args, env = entry.get("command"), entry.get("args", []), entry.get("env", {})
            env_valid = isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            args_valid = isinstance(args, list) and all(isinstance(arg, str) for arg in args)
            found = isinstance(command, str) and bool(command) and env_valid and bool(
                shutil.which(command, path=env.get("PATH"))
            )
            enabled = entry.get("disabled") is not True and entry.get("enabled") is not False
            valid = found and args_valid and enabled
            rows.append({"check": "mcp", "status": "ok" if valid else "error",
                         "message": "DCT MCP executable and configuration present; connection requires a fresh client session"
                         if valid else "DCT MCP disabled, executable missing, or args/env invalid"})
        except (OSError, ValueError, AttributeError, TypeError):
            rows.append({"check": "mcp", "status": "error", "message": "Claude MCP config unavailable or invalid"})
        invalid = []
        for name in discover_skills():
            path = CLAUDE_SKILLS_DIR / name
            try:
                valid = ((path.is_symlink() and path.resolve() == (SKILLS_DIR / name).resolve())
                         or (is_managed_copy(path) and _copy_inventory(path) == _copy_inventory(SKILLS_DIR / name)))
            except (OSError, ValueError):
                valid = False
            if not valid or not (path / "SKILL.md").is_file():
                invalid.append(name)
        rows.append({"check": "skills", "status": "error" if invalid else "ok",
                     "message": "Missing, modified or unowned skills: " + ", ".join(invalid)
                     if invalid else "Packaged skills present"})
        try:
            body = CLAUDE_MD.read_text(encoding="utf-8")
        except OSError:
            body = ""
        block_valid = body.count(SNIPPET_START) == body.count(SNIPPET_END) == 1
        if block_valid:
            block_valid = body.index(SNIPPET_START) < body.index(SNIPPET_END)
        rows.append({"check": "instructions", "status": "ok" if block_valid else "error",
                     "message": "DCT managed instruction block" if block_valid else "Missing or malformed DCT instruction block"})
        rows.append(reference_row())
        missing_hooks = []
        for script in [spec["script"] for spec in HOOK_WIRING] + HOOK_LIBS:
            path = CLAUDE_HOOKS_DIR / script
            source = HOOKS_DIR / script
            if not source.is_file():
                source = HOOKS_DIR / script.removeprefix("dct-")
            try:
                valid = path.read_bytes() == source.read_bytes()
                if path.is_symlink():
                    valid = valid and path.resolve() == source.resolve()
            except OSError:
                valid = False
            if not valid:
                missing_hooks.append(script)
        rows.append({"check": "hook_assets", "status": "error" if missing_hooks else "ok",
                     "message": "Missing or outdated hook files: " + ", ".join(missing_hooks)
                     if missing_hooks else "Current packaged hook files present"})
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
            commands = [hook.get("command", "") for groups in settings.get("hooks", {}).values()
                        for group in groups for hook in group.get("hooks", [])
                        if isinstance(hook, dict)]
            for spec in HOOK_WIRING:
                expected = str(CLAUDE_HOOKS_DIR / spec["script"])
                matching = False
                for command in commands:
                    try:
                        parts = shlex.split(command)
                        matching |= expected in parts or any(
                            str(Path(part).expanduser()) == expected for part in parts
                        )
                    except (ValueError, TypeError):
                        continue
                if not matching and spec["script"] not in missing_hooks:
                    missing_hooks.append(spec["script"])
        except (OSError, ValueError, AttributeError, TypeError):
            missing_hooks = [spec["script"] for spec in HOOK_WIRING]
        rows.append({"check": "hooks", "status": "error" if missing_hooks else "ok",
                     "message": "Missing hook files or wiring: " + ", ".join(missing_hooks)
                     if missing_hooks else "Hook files and wiring present"})
        return rows

    def uninstall(self) -> dict:
        """Remove proven-owned assets only; keep MCP and hooks settings for review.

        Older installers did not record ownership of JSON entries or hook copies.
        Never infer permission to remove those from a familiar name alone.
        """
        removed = []
        for directory, source in ((CLAUDE_SKILLS_DIR, SKILLS_DIR), (CLAUDE_HOOKS_DIR, HOOKS_DIR)):
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                owned_link = entry.is_symlink() and entry.resolve().is_relative_to(source.resolve())
                if owned_link:
                    entry.unlink()
                elif directory == CLAUDE_SKILLS_DIR and is_managed_copy(entry):
                    shutil.rmtree(entry)
                else:
                    continue
                removed.append(str(entry))
        if (CLAUDE_REFERENCE.is_symlink() or CLAUDE_REFERENCE.exists()) and _owned_reference(CLAUDE_REFERENCE):
            CLAUDE_REFERENCE.unlink()
            forget_managed_link(CLAUDE_REFERENCE)
            removed.append(str(CLAUDE_REFERENCE))
        if CLAUDE_MD.exists():
            original = read_exact_text(CLAUDE_MD)
            if original.count(SNIPPET_START) == original.count(SNIPPET_END) == 1:
                start, end = original.index(SNIPPET_START), original.index(SNIPPET_END)
                if end > start:
                    atomic_write(CLAUDE_MD, original[:start] + original[end + len(SNIPPET_END):])
                    removed.append(str(CLAUDE_MD) + " (managed block)")
        preserved = []
        for path in (CLAUDE_JSON, CLAUDE_SETTINGS):
            if path.is_file():
                preserved.append(str(path))
        return {"status": "partial" if preserved else "ok", "removed": removed,
                "preserved": preserved,
                "message": "MCP entries and hook settings preserved for manual review; remove DCT references before restarting the client"
                if preserved else "Owned integration assets removed"}
