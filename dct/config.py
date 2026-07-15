"""Configuration loading for dct.

Priority: DCT_DATABASE_URL env var > ~/.claude/dct.toml > defaults.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".claude" / "dct.toml"
DEFAULT_DB_PATH = Path.home() / ".claude" / "dct.db"


@dataclass
class PlansConfig:
    """[plans] section — v0.2.0 parser/classifier/hook behaviour."""
    auto_resolve_uncertain: bool = True
    classify_model: str = "claude-haiku-4-5-20251001"
    classify_timeout_seconds: int = 30
    surface_on_session_start: bool = False
    discovery_include: list[str] = field(default_factory=list)
    discovery_exclude: list[str] = field(default_factory=list)


@dataclass
class MirrorConfig:
    """[mirror] section — v0.4.0 TaskCreate ↔ dct integration.

    task_to_dct: 'auto' mirrors every TaskCreate/TodoWrite into a session-scoped
                 item as kind='cc-task' checkpoints (non-blocking).
                 'ask'  skips mirror — Claude keeps TaskCreate and dct separate.
                 'off'  disables the hook entirely.
    """
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
    dct_root: str = ""  # absolute path to the dct repo (for MCP server references)
    plans: PlansConfig = field(default_factory=PlansConfig)
    mirror: MirrorConfig = field(default_factory=MirrorConfig)
    web: WebConfig = field(default_factory=WebConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)

    def __post_init__(self):
        if not self.database_url:
            self.database_url = f"sqlite:///{DEFAULT_DB_PATH}"
        if not self.dct_root:
            self.dct_root = str(Path(__file__).resolve().parent.parent)


def load_config(config_path: Path | None = None) -> Config:
    path = config_path or Path(os.environ.get("DCT_CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))
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
        classify_model=str(plans_section.get("classify_model", "claude-haiku-4-5-20251001")),
        classify_timeout_seconds=int(plans_section.get("classify_timeout_seconds", 30)),
        surface_on_session_start=bool(plans_section.get("surface_on_session_start", False)),
        discovery_include=list(plans_section.get("discovery_include", [])),
        discovery_exclude=list(plans_section.get("discovery_exclude", [])),
    )

    mirror_section = data.get("mirror", {})
    mirror_mode = str(mirror_section.get("task_to_dct", "auto"))
    if mirror_mode not in ("auto", "ask", "off"):
        mirror_mode = "auto"
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
        plans=plans, mirror=mirror, web=web, update=update,
    )


def save_config(config: Config, config_path: Path | None = None) -> Path:
    path = config_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if config.dct_root:
        lines.append(f'dct_root = "{config.dct_root}"\n\n')
    lines.append('[database]\n')
    lines.append(f'url = "{config.database_url}"\n\n')
    lines.append('[plans]\n')
    lines.append(f'auto_resolve_uncertain = {str(config.plans.auto_resolve_uncertain).lower()}\n')
    lines.append(f'classify_model = "{config.plans.classify_model}"\n')
    lines.append(f'classify_timeout_seconds = {config.plans.classify_timeout_seconds}\n')
    lines.append(f'surface_on_session_start = {str(config.plans.surface_on_session_start).lower()}\n')
    if config.plans.discovery_include:
        inc = ", ".join(f'"{p}"' for p in config.plans.discovery_include)
        lines.append(f'discovery_include = [{inc}]\n')
    if config.plans.discovery_exclude:
        exc = ", ".join(f'"{p}"' for p in config.plans.discovery_exclude)
        lines.append(f'discovery_exclude = [{exc}]\n')

    lines.append('\n[mirror]\n')
    lines.append(f'task_to_dct = "{config.mirror.task_to_dct}"\n')

    lines.append('\n[web]\n')
    lines.append(f'host = "{config.web.host}"\n')
    lines.append(f'port = {config.web.port}\n')
    lines.append(f'auto_fallback = {str(config.web.auto_fallback).lower()}\n')
    lines.append(f'open_browser = {str(config.web.open_browser).lower()}\n')
    lines.append(f'refresh_poll_ms = {config.web.refresh_poll_ms}\n')

    lines.append('\n[update]\n')
    lines.append(f'check = {str(config.update.check).lower()}\n')

    path.write_text("".join(lines))
    return path
