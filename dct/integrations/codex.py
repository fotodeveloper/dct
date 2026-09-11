"""Codex user-scope adapter with conservative, format-preserving TOML edits."""

import json
import os
import shlex
import shutil
import subprocess
import sys
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
HOOK_ACTIONS = ('check-changelog', 'stop', 'autoscan', 'mirror')
HOOK_EVENT_NAMES = {
    'PreToolUse', 'PermissionRequest', 'PostToolUse', 'PreCompact', 'PostCompact',
    'SessionStart', 'SessionEnd', 'UserPromptSubmit', 'SubagentStart',
    'SubagentStop', 'Stop', 'Interrupt',
}
LEGACY_HOOK_SCRIPTS = {
    'dct-check-changelog.sh',
    'dct-check-changelog-on-stop.sh',
    'dct-plan-autoscan.sh',
    'dct-task-mirror.sh',
}
LEGACY_HOOK_ACTIONS = {
    'dct-check-changelog.sh': 'check-changelog',
    'dct-check-changelog-on-stop.sh': 'stop',
    'dct-plan-autoscan.sh': 'autoscan',
    'dct-task-mirror.sh': 'mirror',
}
LEGACY_HOOK_LAYOUTS = {
    'check-changelog': ('PreToolUse', {'Bash', '^Bash$'}),
    'stop': ('Stop', {None}),
    'autoscan': ('PostToolUse', {'Edit|Write|MultiEdit', '^apply_patch$'}),
    'mirror': ('PostToolUse', {'update_plan', '^update_plan$'}),
}
LEGACY_HOOK_RETIRE_LAYOUTS = {
    'mirror': ('PreToolUse', {'TodoWrite|TaskCreate'}),
}


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


def _module_command(action: str) -> str:
    argv = [sys.executable, '-m', 'dct.integrations.codex_hooks', action]
    return subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(argv)


def _hook_definitions() -> dict[str, dict]:
    specs = {
        'check-changelog': ('PreToolUse', '^Bash$', False),
        'stop': ('Stop', None, False),
        'autoscan': ('PostToolUse', '^apply_patch$', True),
        'mirror': ('PostToolUse', '^update_plan$', True),
    }
    result = {}
    for action, (event, matcher, asynchronous) in specs.items():
        handler = {'type': 'command', 'command': _module_command(action), 'timeout': 30}
        if asynchronous:
            handler['async'] = True
        group = {'hooks': [handler]}
        if matcher is not None:
            group['matcher'] = matcher
        result[action] = {'event': event, 'definition': group}
    return result


def _split_command(command: str, *, windows: bool | None = None) -> list[str]:
    windows = os.name == 'nt' if windows is None else windows
    argv = shlex.split(command, posix=not windows)
    if windows:
        argv = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
                for token in argv]
    return argv


def _read_json_object(path: Path, *, empty: dict) -> tuple[str, dict]:
    raw = path.read_text(encoding='utf-8') if path.exists() else ''
    if not raw:
        return raw, empty
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{path}: invalid JSON; nothing was written: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected a JSON object; nothing was written')  # noqa: TRY004 — user-file validation contract
    return raw, value


def _validate_hook_groups(path: Path, hooks: dict) -> None:
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' must be an object; nothing was written")  # noqa: TRY004 — user-file validation contract
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
            raise ValueError(f'{path}: hook event {event!r} must contain object groups; nothing was written')
        for group in groups:
            handlers = group.get('hooks')
            if not isinstance(handlers, list) or not all(isinstance(handler, dict) for handler in handlers):
                raise ValueError(f'{path}: hook group handlers must be objects; nothing was written')


def _contains_action(group: dict, action: str) -> bool:
    for handler in group.get('hooks', []):
        command = handler.get('command')
        if not isinstance(command, str):
            continue
        try:
            argv = _split_command(command)
        except ValueError:
            continue
        if len(argv) >= 4 and argv[-3:] == ['-m', 'dct.integrations.codex_hooks', action]:
            return True
    return False


class CodexIntegration(ClientIntegration):
    """Install MCP, workflows, instructions, and native Codex lifecycle hooks."""

    name = 'codex'

    def __init__(self):
        self.home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
        self.config_path = self.home / 'config.toml'
        self.instructions_path = self.home / 'AGENTS.md'
        self.skills_path = Path.home() / '.agents' / 'skills'
        self.hooks_path = self.home / 'hooks.json'
        self.hooks_ownership_path = self.home / '.dct-hooks-managed.json'

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

    def _repair_missing_legacy_hooks(
            self, hooks: dict, desired: dict) -> tuple[list[str], list[str]]:
        repaired, retired = [], []
        legacy_dir = self.home / 'hooks'
        for event, groups in hooks.items():
            for index, group in enumerate(groups):
                handlers = group['hooks']
                if len(handlers) != 1:
                    continue
                command = handlers[0].get('command')
                try:
                    argv = _split_command(command) if isinstance(command, str) else []
                except ValueError:
                    argv = []
                target = Path(argv[1]) if len(argv) == 2 and argv[0] == 'bash' else None
                if (target is None or target.parent != legacy_dir
                        or target.name not in LEGACY_HOOK_SCRIPTS or target.exists()):
                    continue
                action = LEGACY_HOOK_ACTIONS[target.name]
                expected_event, expected_matchers = LEGACY_HOOK_LAYOUTS[action]
                if (desired[action]['event'] == event and expected_event == event
                        and group.get('matcher') in expected_matchers):
                    groups[index] = desired[action]['definition']
                    repaired.append(action)
                    continue
                retire_layout = LEGACY_HOOK_RETIRE_LAYOUTS.get(action)
                if (retire_layout is not None and event == retire_layout[0]
                        and group.get('matcher') in retire_layout[1]):
                    groups[index] = {'hooks': [], 'matcher': group['matcher']}
                    retired.append(action)
        return repaired, retired

    def _read_hook_ownership(self) -> tuple[str, dict | None]:
        if not self.hooks_ownership_path.exists():
            return '', None
        raw, ownership = _read_json_object(self.hooks_ownership_path, empty={})
        entries = ownership.get('entries')
        if ownership.get('owner') != 'dct' or ownership.get('version') != 1 or not isinstance(entries, dict):
            raise ValueError(f'{self.hooks_ownership_path}: invalid DCT hook ownership sidecar; nothing was written')
        desired = _hook_definitions()
        valid_entries = True
        for action, entry in entries.items():
            if action not in HOOK_ACTIONS or not isinstance(entry, dict):
                valid_entries = False
                break
            definition = entry.get('definition')
            expected = desired[action]
            if (set(entry) != {'event', 'definition'} or entry.get('event') != expected['event']
                    or not isinstance(definition, dict)
                    or set(definition) != set(expected['definition'])
                    or definition.get('matcher') != expected['definition'].get('matcher')
                    or not _contains_action(definition, action)):
                valid_entries = False
                break
            try:
                _validate_hook_groups(self.hooks_ownership_path,
                                      {entry['event']: [definition]})
            except ValueError:
                valid_entries = False
                break
        if not valid_entries:
            raise ValueError(f'{self.hooks_ownership_path}: invalid DCT hook ownership entries; nothing was written')
        return raw, ownership

    def _inline_dct_hook_actions(self) -> list[str]:
        _, config = _read_config(self.config_path)
        inline = config.get('hooks', {})
        if not isinstance(inline, dict):
            raise ValueError(f"{self.config_path}: 'hooks' must be a TOML table; nothing was written")  # noqa: TRY004 — user-file validation contract
        lifecycle = {event: groups for event, groups in inline.items()
                     if event in HOOK_EVENT_NAMES}
        _validate_hook_groups(self.config_path, lifecycle)
        return [action for action in HOOK_ACTIONS
                if any(_contains_action(group, action)
                       for groups in lifecycle.values() for group in groups)]

    def _update_hooks(self, *, refresh_only: bool) -> dict:
        inline_actions = self._inline_dct_hook_actions()
        if inline_actions:
            names = ', '.join(inline_actions)
            raise ValueError(
                f'{self.config_path}: inline DCT hooks already define {names}; '
                'remove or reconcile them before installing managed hooks.json entries')
        raw, data = _read_json_object(self.hooks_path, empty={'hooks': {}})
        hooks = data.setdefault('hooks', {})
        _validate_hook_groups(self.hooks_path, hooks)
        ownership_raw, ownership = self._read_hook_ownership()
        if refresh_only and ownership is None:
            return {'status': 'not_owned', 'refreshed': [], 'skipped_modified': [],
                    'path': str(self.hooks_path)}

        entries = dict(ownership['entries']) if ownership else {}
        desired = _hook_definitions()
        legacy_repaired, legacy_retired = (
            self._repair_missing_legacy_hooks(hooks, desired) if not refresh_only else ([], []))
        for action in legacy_repaired:
            entries[action] = desired[action]
        installed, refreshed, skipped_modified, skipped_existing = [], [], [], []
        installed.extend(legacy_repaired)
        for action, current in desired.items():
            old = entries.get(action)
            if old is not None:
                old_groups = hooks.setdefault(old.get('event'), [])
                try:
                    index = old_groups.index(old.get('definition'))
                except ValueError:
                    related = any(_contains_action(group, action) for groups in hooks.values() for group in groups)
                    related = related or any(group.get('matcher') == old.get('definition', {}).get('matcher')
                                             for group in old_groups)
                    if related:
                        skipped_modified.append(action)
                    elif not refresh_only:
                        hooks.setdefault(current['event'], []).append(current['definition'])
                        entries[action] = current
                        installed.append(action)
                    continue
                if old != current:
                    old_groups[index] = current['definition']
                    entries[action] = current
                    refreshed.append(action)
                continue
            if refresh_only:
                continue
            groups = hooks.setdefault(current['event'], [])
            if current['definition'] in groups:
                skipped_existing.append(action)
            elif any(_contains_action(group, action) for event_groups in hooks.values() for group in event_groups):
                skipped_modified.append(action)
            else:
                groups.append(current['definition'])
                entries[action] = current
                installed.append(action)

        updated = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
        hooks_backup = atomic_write(self.hooks_path, updated) if updated != raw else None
        sidecar_backup = None
        if entries:
            sidecar = json.dumps({'version': 1, 'owner': 'dct', 'entries': entries},
                                 indent=2, ensure_ascii=False) + '\n'
            if sidecar != ownership_raw:
                sidecar_backup = atomic_write(self.hooks_ownership_path, sidecar)
        changed = bool(installed or refreshed or updated != raw)
        return {'status': 'updated' if changed else 'unchanged', 'installed': installed,
                'refreshed': refreshed, 'skipped_modified': skipped_modified,
                'skipped_existing': skipped_existing,
                'legacy_repaired': sorted(name for name, action in LEGACY_HOOK_ACTIONS.items()
                                          if action in legacy_repaired),
                'legacy_retired': sorted(name for name, action in LEGACY_HOOK_ACTIONS.items()
                                         if action in legacy_retired),
                'backup': hooks_backup, 'ownership_backup': sidecar_backup,
                'path': str(self.hooks_path), 'trust': 'review_required_via_/hooks'}

    def install_hooks(self) -> dict:
        """Install or repair only hook definitions proven to be DCT-owned."""
        return self._update_hooks(refresh_only=False)

    def refresh_hooks(self) -> dict:
        """Refresh exact owned definitions without introducing new hook opt-in."""
        return self._update_hooks(refresh_only=True)

    def _uninstall_hooks(self) -> tuple[list[str], list[str]]:
        if not self.hooks_ownership_path.exists():
            return [], []
        try:
            _, data = _read_json_object(self.hooks_path, empty={'hooks': {}})
            hooks = data.setdefault('hooks', {})
            _validate_hook_groups(self.hooks_path, hooks)
            _, ownership = self._read_hook_ownership()
        except (ValueError, OSError, UnicodeError) as exc:
            return [], [f'hooks: {exc}']

        removed, retained = [], []
        changed = False
        for action, entry in ownership['entries'].items():
            groups = hooks.get(entry['event'], [])
            try:
                index = groups.index(entry['definition'])
            except ValueError:
                retained.append(f'hook:{action}: user-owned or modified')
            else:
                placeholder = {'hooks': []}
                if 'matcher' in entry['definition']:
                    placeholder['matcher'] = entry['definition']['matcher']
                groups[index] = placeholder
                removed.append(f'hook:{action}')
                changed = True
        if changed:
            atomic_write(self.hooks_path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
        self.hooks_ownership_path.unlink()
        removed.append('hook_ownership')
        return removed, retained

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
            _, data = _read_json_object(self.hooks_path, empty={'hooks': {}})
            hooks = data.setdefault('hooks', {})
            _validate_hook_groups(self.hooks_path, hooks)
            _, ownership = self._read_hook_ownership()
            exact = ownership is not None and all(
                entry['definition'] in hooks.get(entry['event'], [])
                for entry in ownership['entries'].values())
            complete = exact and set(ownership['entries']) == set(HOOK_ACTIONS)
            if complete:
                add('hooks', 'ok', f'Four native DCT hook definitions installed in {self.hooks_path}')
                add('hooks_trust', 'warning', 'Installed hook definitions require runtime review and trust via /hooks; offline trust status is unknown')
                required = ('bash', 'jq', 'git')
                missing = [name for name in required if not shutil.which(name)]
                runtime_missing = []
                for action, entry in ownership['entries'].items():
                    command = entry['definition']['hooks'][0]['command']
                    try:
                        executable = _split_command(command)[0]
                    except (ValueError, IndexError):
                        runtime_missing.append(action)
                        continue
                    available = (Path(executable).is_file() if Path(executable).is_absolute()
                                 else bool(shutil.which(executable)))
                    if not available:
                        runtime_missing.append(action)
                add('hook_dependencies', 'error' if missing or runtime_missing else 'ok',
                    'Shared check/stop/autoscan policies require bash, jq and git; '
                    'mirror uses the pinned Python runtime only. '
                    + ('Missing: ' + ', '.join(missing) if missing
                       else 'All shared-policy dependencies are available')
                    + ('. Missing pinned runtime for: ' + ', '.join(runtime_missing)
                       if runtime_missing else ''))
            elif ownership is not None:
                add('hooks', 'error', 'DCT-owned hook definitions are missing or modified; repair preserves modified entries')
            else:
                add('hooks', 'warning', 'Native DCT hooks are not installed; explicit install/repair can add them without trusting them')
        except (ValueError, OSError, UnicodeError) as exc:
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
        hook_removed, hook_retained = self._uninstall_hooks()
        removed.extend(hook_removed)
        retained.extend(hook_retained)
        return {'removed': removed, 'retained': retained}
