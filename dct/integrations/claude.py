"""Claude Code integration adapter (ADR #51 §6.3, moved from dct/setup.py
in CP #1235): register the MCP server user-scope, install hook and skill
symlinks into the host's ~/.claude, wire hooks into settings.json."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dct import assets
from dct.config import Config


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
    data.setdefault("mcpServers", {})["dct"] = {
        "type": "stdio",
        "command": server_cmd[0],
        "args": server_cmd[1:],
    }
    with open(CLAUDE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
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
        entry = {"type": "command", "command": f"bash {CLAUDE_HOOKS_DIR / name}"}
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
        if raw is not None and raw.strip():
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            backup_path = settings_path.with_name(
                f"{settings_path.name}.bak-dct-{stamp}"
            )
            backup_path.write_text(raw, encoding="utf-8")
            backup = str(backup_path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = settings_path.with_name(settings_path.name + ".dct-tmp")
        tmp_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_path, settings_path)

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
            continue  # don't overwrite non-symlink dirs

        _link_or_copy(src, dst)
        installed += 1

    return installed > 0
