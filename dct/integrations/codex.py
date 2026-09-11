"""Codex user-scope adapter with conservative, format-preserving TOML edits."""

import json
import os
import shutil
import tomllib
from pathlib import Path

from dct.integrations.base import (
    ClientIntegration,
    atomic_write,
    is_managed_copy,
    managed_instructions,
    mark_managed_copy,
)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SKILLS = ("pickup", "handoff", "track", "clog")
START = "<!-- dct-codex:start -->"
END = "<!-- dct-codex:end -->"
MCP_START = "# dct-codex:mcp:start"
MCP_END = "# dct-codex:mcp:end"
ACTOR_ENV = {"DCT_ACTOR_CLIENT": "codex", "DCT_ACTOR_TYPE": "agent"}
MCP_BODY = '[mcp_servers.dct]\ncommand = "dct"\nargs = ["server"]\n\n[mcp_servers.dct.env]\nDCT_ACTOR_CLIENT = "codex"\nDCT_ACTOR_TYPE = "agent"'
MCP_BLOCK = f"{MCP_START}\n{MCP_BODY}\n{MCP_END}"


def _read_config(path: Path) -> tuple[str, dict]:
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        return raw, tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML; nothing was written: {exc}") from exc


def _table(data: dict, *parts: str) -> dict:
    for part in parts:
        data = data.get(part, {})
        if not isinstance(data, dict):
            raise ValueError(f"{'.'.join(parts)} must be a TOML table; nothing was written")  # noqa: TRY004 — user-file validation contract
    return data


def _insert_missing(raw: str, path: tuple[str, ...], fields: dict) -> str:
    """Insert absent scalar keys without reserializing unrelated TOML.

    Parse each candidate prefix to distinguish real headers from header-shaped
    text in multiline strings. Unsupported inline/dotted layouts fail validation
    before writing, rather than risking a lossy rewrite of a user's config.
    """
    if not fields:
        return raw
    assignment = ''.join(f'{key} = {json.dumps(value)}\n' for key, value in fields.items())
    offset = 0
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and not stripped.startswith('[['):
            try:
                tomllib.loads(raw[:offset])
                probe = tomllib.loads(stripped + '\n__dct_probe__ = true\n')
                for part in path:
                    probe = probe[part]
                matches = probe.get('__dct_probe__') is True
            except (ValueError, KeyError, TypeError, AttributeError):
                matches = False
            if matches:
                end = offset + len(line)
                return raw[:end] + ('\n' if not line.endswith('\n') else '') + assignment + raw[end:]
        offset += len(line)
    return raw + ('\n\n' if raw else '') + '[' + '.'.join(path) + ']\n' + assignment


class CodexIntegration(ClientIntegration):
    """Install MCP, explicit workflow skills, and a bounded AGENTS.md block.

    Hooks are intentionally not translated from another client's event schema.
    Doctor reports their unsupported parity and existing DCT hook references.
    """

    name = 'codex'

    def __init__(self):
        self.home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
        self.config_path = self.home / 'config.toml'
        self.instructions_path = self.home / 'AGENTS.md'
        self.skills_path = Path.home() / '.agents' / 'skills'

    def detect(self) -> bool:
        try:
            _, data = _read_config(self.config_path)
            if 'dct' in _table(data, 'mcp_servers'):
                return True
        except ValueError:
            pass
        if self.instructions_path.exists() and START in self.instructions_path.read_text(encoding='utf-8'):
            return True
        return any(is_managed_copy(self.skills_path / name) for name in SKILLS)

    def install_mcp(self) -> dict:
        raw, data = _read_config(self.config_path)
        servers = _table(data, 'mcp_servers')
        if 'dct' not in servers:
            updated = raw + ('\n\n' if raw else '') + MCP_BLOCK + '\n'
        else:
            server = _table(servers, 'dct')
            if 'url' in server:
                raise ValueError('Existing dct MCP uses a remote URL; preserve it and configure server-side identity manually')
            updated = _insert_missing(raw, ('mcp_servers', 'dct'), {
                key: value for key, value in {'command': 'dct', 'args': ['server']}.items() if key not in server
            })
            env = _table(server, 'env')
            updated = _insert_missing(updated, ('mcp_servers', 'dct', 'env'), {
                key: value for key, value in ACTOR_ENV.items() if key not in env
            })
        try:
            tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError('DCT config uses an unsupported inline/dotted table layout; use explicit [mcp_servers.dct] and [mcp_servers.dct.env] tables. Nothing was written') from exc
        return {'status': 'unchanged' if raw == updated else 'installed',
                'backup': atomic_write(self.config_path, updated), 'path': str(self.config_path)}

    def install_skills(self) -> dict:
        installed, skipped = [], []
        for name in SKILLS:
            destination = self.skills_path / name
            if (destination.exists() or destination.is_symlink()) and not is_managed_copy(destination):
                skipped.append(name)
                continue
            destination.mkdir(parents=True, exist_ok=True)
            skill = destination / 'SKILL.md'
            content = (ASSETS / f'codex-{name}.md').read_text(encoding='utf-8')
            if not skill.exists() or skill.read_text(encoding='utf-8') != content:
                # Backups live outside the owned skill directory so upgrades do
                # not change the inventory or advertise duplicate skills.
                backup = atomic_write(skill, content)
                if backup:
                    backup_path = Path(backup)
                    self.home.mkdir(parents=True, exist_ok=True)
                    backup_path.replace(self.home / backup_path.name)
            mark_managed_copy(destination)
            installed.append(name)
        return {'installed': installed, 'skipped': skipped, 'path': str(self.skills_path)}

    def install_instructions(self) -> dict:
        return managed_instructions(self.instructions_path,
                                    (ASSETS / 'codex-agents.md').read_text(encoding='utf-8'), START, END)

    def doctor(self) -> list[dict]:
        rows = []

        def add(check, status, message):
            rows.append({'check': check, 'status': status, 'message': message})

        try:
            _, data = _read_config(self.config_path)
            servers = _table(data, 'mcp_servers')
            server = _table(servers, 'dct')
            add('mcp', 'ok' if 'dct' in servers else 'error', str(self.config_path))
            env = _table(server, 'env')
            valid = all(env.get(key) == value for key, value in ACTOR_ENV.items())
            add('actor', 'ok' if valid else 'error', 'Expected DCT_ACTOR_CLIENT=codex and DCT_ACTOR_TYPE=agent; custom values are preserved')
            if server.get('enabled') is False:
                add('mcp_enabled', 'error', 'DCT MCP is disabled in config.toml')
            command = server.get('command')
            found = isinstance(command, str) and bool(shutil.which(command))
            add('command', 'ok' if found else 'error', f'MCP executable: {command or "missing"}; server connection requires a fresh client session')
        except (ValueError, OSError) as exc:
            add('mcp', 'error', str(exc))
        for name in SKILLS:
            try:
                destination = self.skills_path / name
                owned = is_managed_copy(destination)
                current = owned and (destination / 'SKILL.md').read_text(encoding='utf-8') == (ASSETS / f'codex-{name}.md').read_text(encoding='utf-8')
                add(f'skill:{name}', 'ok' if current else 'error', 'Current DCT skill' if current else 'Missing, outdated or user-owned skill; repair preserves conflicts')
            except (OSError, UnicodeError) as exc:
                add(f'skill:{name}', 'error', str(exc))
        try:
            raw = self.instructions_path.read_text(encoding='utf-8') if self.instructions_path.exists() else ''
            body = (ASSETS / 'codex-agents.md').read_text(encoding='utf-8').strip('\n')
            valid = raw.count(START) == raw.count(END) == 1 and f'{START}\n{body}\n{END}' in raw
            add('instructions', 'ok' if valid else 'error', str(self.instructions_path))
        except (OSError, UnicodeError) as exc:
            add('instructions', 'error', str(exc))
        try:
            override = self.home / 'AGENTS.override.md'
            shadowed = override.exists() and bool(override.read_text(encoding='utf-8').strip())
            add('instructions_shadowed', 'error' if shadowed else 'ok',
                'AGENTS.override.md shadows the managed AGENTS.md. Manually merge needed override instructions into AGENTS.md and remove or empty the override when appropriate; repair leaves it untouched.'
                if shadowed else 'No nonempty AGENTS.override.md shadows the managed instructions')
        except (OSError, UnicodeError) as exc:
            add('instructions_shadowed', 'error', str(exc))
        try:
            hooks = self.home / 'hooks.json'
            references = hooks.exists() and 'dct-' in hooks.read_text(encoding='utf-8')
            add('hooks', 'warning', 'DCT hook references exist; event support and script paths are unverified. Repair does not copy foreign hooks.' if references else 'Automatic hook parity is not implemented; use explicit pickup/handoff/track/clog workflows.')
        except (OSError, UnicodeError) as exc:
            add('hooks', 'error', str(exc))
        return rows

    def uninstall(self) -> dict:
        # Validate config before making any changes to other integration files.
        raw, data = _read_config(self.config_path)
        removed, retained = [], []
        managed = MCP_BLOCK in raw
        if managed:
            try:
                tomllib.loads(raw[:raw.index(MCP_BLOCK)])
            except tomllib.TOMLDecodeError:
                managed = False  # Marker-shaped text inside a multiline string.
        if managed and _table(data, 'mcp_servers', 'dct') == tomllib.loads(MCP_BODY)['mcp_servers']['dct']:
            updated = raw.replace(MCP_BLOCK, '', 1)
            tomllib.loads(updated)
            atomic_write(self.config_path, updated)
            removed.append('mcp')
        elif 'dct' in _table(data, 'mcp_servers'):
            retained.append('mcp: pre-existing or customized registration')
        if self.instructions_path.exists():
            text = self.instructions_path.read_text(encoding='utf-8')
            if text.count(START) == text.count(END) == 1 and text.index(START) < text.index(END):
                left, right = text.index(START), text.index(END) + len(END)
                atomic_write(self.instructions_path, text[:left] + text[right:])
                removed.append('instructions')
            elif START in text or END in text:
                retained.append('instructions: malformed managed markers')
        for name in SKILLS:
            destination = self.skills_path / name
            if is_managed_copy(destination):
                shutil.rmtree(destination)
                removed.append(f'skill:{name}')
            elif destination.exists() or destination.is_symlink():
                retained.append(f'skill:{name}: user-owned or modified')
        return {'removed': removed, 'retained': retained}
