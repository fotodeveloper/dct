"""Claude Code integration adapter (ADR #51 §6.3, moved from dct/setup.py
in CP #1235): register the MCP server user-scope, install hook and skill
symlinks into the host's ~/.claude, wire hooks into settings.json."""

import json
import os
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
    is_managed_copy,
    managed_instructions,
    mark_managed_copy,
)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Symlink dst → src; on OSError fall back to a real copy (W5: Windows
    without Developer Mode / SeCreateSymbolicLinkPrivilege raises
    WinError 1314). Copies drift until the next `dct init` / `dct upgrade`
    re-run — say so instead of failing the whole init."""
    try:
        dst.symlink_to(src)
    except OSError:
        if src.is_dir():
            shutil.copytree(src, dst)
            mark_managed_copy(dst)
        else:
            shutil.copy2(src, dst)
        print(
            f"  note: symlinks unavailable — copied {src.name} instead "
            "(copies drift; re-run `dct init` after upgrades)"
        )

SKILLS_DIR = assets.skills_dir()
HOOKS_DIR = assets.hooks_dir()
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

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
        _link_or_copy(src, dst)
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

        _link_or_copy(src, dst)
        installed += 1

    return installed > 0


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
    """Append (or refresh) the packaged dct snippet in ~/.claude/CLAUDE.md (#443).

    The packaged snippet file opens with a human-facing preamble; everything
    below its FIRST ``---`` separator is the actual instruction block. That
    block is written between ``SNIPPET_START``/``SNIPPET_END`` markers:

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
        return {"status": "ok", "installed": installed, "hooks": hooks, "wiring": wiring}

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
        if CLAUDE_MD.exists():
            original = CLAUDE_MD.read_text(encoding="utf-8")
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
