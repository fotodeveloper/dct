"""Init workflow helpers: register MCP user-scope, install hook, install skills."""

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from dct.config import Config

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
HOOKS_DIR = Path(__file__).resolve().parent / "hooks"
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

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

    if shutil.which("claude"):
        cmd = ["claude", "mcp", "add", "dct", "--scope", "user", "--"] + server_cmd
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True, "registered via `claude mcp add --scope user`"
        if "already exists" in (result.stderr + result.stdout).lower():
            return True, "already registered"
        # fall through to direct edit

    # Fallback: direct ~/.claude.json edit
    data = {}
    if CLAUDE_JSON.exists():
        with open(CLAUDE_JSON) as f:
            data = json.load(f)
    data.setdefault("mcpServers", {})["dct"] = {
        "type": "stdio",
        "command": server_cmd[0],
        "args": server_cmd[1:],
    }
    with open(CLAUDE_JSON, "w") as f:
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
        dst.symlink_to(src)
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

    raw = settings_path.read_text() if settings_path.exists() else None
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
        raise ValueError(
            f"{settings_path}: top-level JSON must be an object; nothing was written"
        )
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(
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
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = settings_path.with_name(
                f"{settings_path.name}.bak-dct-{stamp}"
            )
            backup_path.write_text(raw)
            backup = str(backup_path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = settings_path.with_name(settings_path.name + ".dct-tmp")
        tmp_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp_path, settings_path)

    return {
        "added": added,
        "skipped": skipped,
        "backup": backup,
        "settings_path": str(settings_path),
    }


def install_skills(config: Config) -> bool:
    """Symlink every packaged skill (dct/skills/*) into ~/.claude/skills/.

    Sources come from the installed package — site-packages for a wheel
    install, the repo working tree for an editable one (where a repo fix is
    live without reinstall).
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

        dst.symlink_to(src)
        installed += 1

    return installed > 0
