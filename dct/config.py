"""Configuration loading for dct.

Config file resolution (dct/paths.py, ADR #52):
    DCT_CONFIG_PATH env > platform config dir (e.g. ~/.config/dctools/dct.toml)
    > legacy ~/.claude/dct.toml (kept working, one-time migrate-home notice).
Database URL precedence:
    DCT_DATABASE_URL env > [database].url > DCT_DATA_DIR env
    > platform data dir (e.g. ~/.local/share/dctools/dct.db)
    > legacy ~/.claude/dct.db.
"""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dct import paths

_legacy_notice_emitted = False


def _stderr_is_tty() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _maybe_warn_legacy(path: Path) -> None:
    """One-time stderr notice when default resolution lands on ~/.claude.

    Only for interactive processes (tty): every hook and MCP-server boot is a
    fresh short-lived process, so an unconditional per-process notice would
    spam non-human consumers on every invocation."""
    global _legacy_notice_emitted
    if _legacy_notice_emitted or not _stderr_is_tty() or not paths.is_legacy(path):
        return
    print(
        "Found a legacy DCT installation in ~/.claude.\n"
        "Run `dct migrate-home` to move it to the platform-neutral location.",
        file=sys.stderr,
    )
    _legacy_notice_emitted = True


def _toml_str(value: object) -> str:
    """Render a TOML basic string with backslashes and quotes escaped (W1) —
    a raw C:\\-style path interpolated unescaped corrupts the file."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class PlansConfig:
    """[plans] section — parser/hook behaviour.

    ADR #52 (1.2.0 rename window): classify_model / classify_timeout_seconds
    moved to [classification]; surface_on_session_start and
    discovery_include/exclude were deleted outright — they had ZERO code
    consumers (round-1 audit), so nothing is aliased forward."""
    auto_resolve_uncertain: bool = True


@dataclass
class ClassificationConfig:
    """[classification] section — pluggable uncertain-section classifier
    (ADR #52, plan #12 §11; keys formerly [plans].classify_*).

    provider: 'command' (default — shell out to `command`) or 'disabled'
              (leave uncertain sections as-is). Other providers (rules/
              openai/ollama/…) are G2 scope; an unknown value behaves as
              'disabled' rather than silently shelling out.
    command:  argv base for provider='command'.
    """
    provider: str = "command"
    command: list[str] = field(default_factory=lambda: ["claude", "--print"])
    model: str = "claude-haiku-4-5-20251001"
    timeout_seconds: int = 30


@dataclass
class ClaudeIntegrationConfig:
    """[integrations.claude] — the Claude Code adapter's knobs.

    task_mirror (formerly [mirror].task_to_dct, permanently aliased):
        'auto' mirrors every TaskCreate/TodoWrite into a session-scoped item
        as kind='cc-task' checkpoints; 'ask' skips; 'off' disables the hook.
    """
    task_mirror: str = "auto"


@dataclass
class IntegrationsConfig:
    claude: ClaudeIntegrationConfig = field(default_factory=ClaudeIntegrationConfig)


@dataclass
class MirrorConfig:
    """Compat view over integrations.claude.task_mirror — kept so the dotted
    path `dct config get mirror.task_to_dct` (referenced by shipped hook
    docs) keeps resolving. Populated from the SAME resolved value; never
    written back to disk."""
    task_to_dct: str = "auto"


@dataclass
class WebConfig:
    """[web] section — v0.4.1 read-only browser viewer.

    The viewer is reached at ``http://localhost:<port>`` — plain localhost
    binding, no privileged port.

    host           bind address (localhost-only by design)
    port           preferred port; auto_fallback scans upward if busy
    auto_fallback  pick the next free port when `port` is occupied
    open_browser   open the browser on `dct web start`
    refresh_poll_ms SSE change-token poll interval (milliseconds)
    """
    host: str = "127.0.0.1"
    port: int = 8787
    auto_fallback: bool = True
    open_browser: bool = False
    refresh_poll_ms: int = 2000


@dataclass
class UpdateConfig:
    """[update] section — #356 passive PyPI update check.

    check  when False, disables BOTH the background version probe and the CLI
           nudge. Offline-safe and non-blocking regardless; this is the opt-out.
    """
    check: bool = True


@dataclass
class Config:
    database_url: str = ""
    # Retired key (ADR #52): still PARSED so old files load, auto-detected
    # otherwise, but never written back — nothing consumes it functionally.
    dct_root: str = ""
    plans: PlansConfig = field(default_factory=PlansConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)
    mirror: MirrorConfig = field(default_factory=MirrorConfig)
    web: WebConfig = field(default_factory=WebConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)

    def __post_init__(self):
        if not self.database_url:
            self.database_url = paths.sqlite_url(paths.default_db_path())
        if not self.dct_root:
            self.dct_root = str(Path(__file__).resolve().parent.parent)


def load_config(config_path: Path | None = None) -> Config:
    if config_path is None:
        path = paths.default_config_path()
        _maybe_warn_legacy(path)
    else:
        path = config_path
    data: dict = {}

    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    db_section = data.get("database", {})
    database_url = db_section.get("url", "")
    dct_root = data.get("dct_root", "")

    plans_section = data.get("plans", {})
    plans = PlansConfig(
        auto_resolve_uncertain=bool(plans_section.get("auto_resolve_uncertain", True)),
    )

    # [classification] with permanent aliases for the pre-1.2 [plans] keys
    # (ADR #52 rename window; new keys win when both are present).
    cls_section = data.get("classification", {})
    cls_defaults = ClassificationConfig()
    classification = ClassificationConfig(
        provider=str(cls_section.get("provider", cls_defaults.provider)),
        command=[str(c) for c in cls_section.get("command", cls_defaults.command)],
        model=str(
            cls_section.get("model")
            or plans_section.get("classify_model")
            or cls_defaults.model
        ),
        timeout_seconds=int(
            cls_section.get("timeout_seconds")
            or plans_section.get("classify_timeout_seconds")
            or cls_defaults.timeout_seconds
        ),
    )

    # [integrations.claude].task_mirror with the permanent [mirror].task_to_dct
    # alias; the MirrorConfig compat view mirrors the SAME resolved value.
    claude_section = data.get("integrations", {}).get("claude", {})
    mirror_mode = str(
        claude_section.get("task_mirror")
        or data.get("mirror", {}).get("task_to_dct")
        or "auto"
    )
    if mirror_mode not in ("auto", "ask", "off"):
        mirror_mode = "auto"
    integrations = IntegrationsConfig(
        claude=ClaudeIntegrationConfig(task_mirror=mirror_mode)
    )
    mirror = MirrorConfig(task_to_dct=mirror_mode)

    web_section = data.get("web", {})
    web = WebConfig(
        host=str(web_section.get("host", "127.0.0.1")),
        port=int(web_section.get("port", 8787)),
        auto_fallback=bool(web_section.get("auto_fallback", True)),
        open_browser=bool(web_section.get("open_browser", False)),
        refresh_poll_ms=int(web_section.get("refresh_poll_ms", 2000)),
    )

    update_section = data.get("update", {})
    update = UpdateConfig(check=bool(update_section.get("check", True)))

    # Env var overrides config file
    env_url = os.environ.get("DCT_DATABASE_URL")
    if env_url:
        database_url = env_url

    return Config(
        database_url=database_url, dct_root=dct_root,
        plans=plans, classification=classification,
        integrations=integrations, mirror=mirror, web=web, update=update,
    )


def save_config(config: Config, config_path: Path | None = None) -> Path:
    path = config_path or paths.default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Canonical writer: ONLY the post-ADR-#52 shape. Legacy keys ([mirror],
    # plans.classify_*, top-level dct_root) are read-side aliases, never
    # written back.
    lines = []
    lines.append('[database]\n')
    lines.append(f'url = {_toml_str(config.database_url)}\n\n')
    lines.append('[plans]\n')
    lines.append(f'auto_resolve_uncertain = {str(config.plans.auto_resolve_uncertain).lower()}\n')

    lines.append('\n[classification]\n')
    lines.append(f'provider = {_toml_str(config.classification.provider)}\n')
    cmd = ", ".join(_toml_str(c) for c in config.classification.command)
    lines.append(f'command = [{cmd}]\n')
    lines.append(f'model = {_toml_str(config.classification.model)}\n')
    lines.append(f'timeout_seconds = {config.classification.timeout_seconds}\n')

    lines.append('\n[integrations.claude]\n')
    lines.append(f'task_mirror = {_toml_str(config.integrations.claude.task_mirror)}\n')

    lines.append('\n[web]\n')
    lines.append(f'host = {_toml_str(config.web.host)}\n')
    lines.append(f'port = {config.web.port}\n')
    lines.append(f'auto_fallback = {str(config.web.auto_fallback).lower()}\n')
    lines.append(f'open_browser = {str(config.web.open_browser).lower()}\n')
    lines.append(f'refresh_poll_ms = {config.web.refresh_poll_ms}\n')

    lines.append('\n[update]\n')
    lines.append(f'check = {str(config.update.check).lower()}\n')

    path.write_text("".join(lines), encoding="utf-8")
    return path
