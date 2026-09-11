"""Bounded native Codex hook translation over shared DCT policy scripts.

Command entry: python -m dct.integrations.codex_hooks MODE. Only an explicit
valid PreToolUse policy denial blocks. Passive hooks fail open with a JSON
systemMessage on operational failure; no plain stdout is forwarded to Codex.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dct.assets import hooks_dir

MAX_INPUT = 2 * 1024 * 1024
MAX_PATCH = 1024 * 1024
MAX_PATHS = 64
TIMEOUT = 10
MODES = ('check-changelog', 'stop', 'autoscan', 'mirror')


def _warning(message: str) -> tuple[int, dict, str]:
    return 0, {'systemMessage': 'DCT hook skipped: ' + message}, ''


def _cwd(payload: dict) -> Path | None:
    value = payload.get('cwd')
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    resolved = path.resolve()
    return resolved if resolved.is_dir() else None


def _literal_directory(value: str, base: Path) -> Path | None:
    # Do not evaluate shell syntax or infer the result of a dynamic directory.
    if not value or any(char in value for char in ('$', '`', '\n', '\x00', '~')):
        return None
    path = Path(value)
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    return path if path.is_dir() else None


def _shell_contexts(payload: dict, cwd: Path) -> list[tuple[Path, str]]:
    """Keep proven commit contexts when a later && segment is unsupported."""
    data = payload.get('tool_input')
    if not isinstance(data, dict):
        return []
    command = data.get('command', data.get('cmd'))
    if not isinstance(command, str) or len(command) > MAX_PATCH:
        return []
    if data.get('workdir') is not None:
        if not isinstance(data['workdir'], str):
            return []
        cwd = _literal_directory(data['workdir'], cwd)
        if cwd is None:
            return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    if any(token in ('||', '|', '&', ';') for token in tokens):
        return []  # branch-dependent working directories are not guessed
    segments = [[]]
    for token in tokens:
        if token == '&&':
            segments.append([])
        else:
            segments[-1].append(token)
    contexts = []
    for parts in segments:
        if not parts:
            return []
        if parts[0] == 'cd':
            if len(parts) != 2:
                return contexts
            cwd = _literal_directory(parts[1], cwd)
            if cwd is None:
                return contexts
            continue
        if Path(parts[0]).name != 'git':
            return contexts  # unknown suffix cannot erase a preceding commit
        target, index = cwd, 1
        while index < len(parts) and parts[index] == '-C':
            if index + 1 >= len(parts):
                return contexts
            target = _literal_directory(parts[index + 1], target)
            if target is None:
                return contexts
            index += 2
        if index < len(parts) and parts[index] == 'commit':
            contexts.append((target, shlex.join(['git', *parts[index:]])))
    return contexts


def _worktree_root(cwd: Path) -> Path:
    """Bound autoscan ownership to this Git worktree, or cwd outside Git."""
    git = shutil.which('git')
    if git:
        try:
            env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
            result = subprocess.run([git, '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                                    env=env, capture_output=True, text=True, timeout=2, check=False)
            if result.returncode == 0:
                root = Path(result.stdout.rstrip('\r\n')).resolve()
                if cwd.resolve().is_relative_to(root):
                    return root
        except (OSError, subprocess.SubprocessError):
            pass
    return cwd.resolve()


def patch_paths(patch: str, cwd: Path) -> list[Path]:
    """Existing final destinations from bounded apply_patch headers only.

    The shared autoscan shell script remains the authoritative plan-pattern
    filter. Resolved destinations must stay in the current Git worktree (cwd
    outside Git), including through symlinks. This parser never applies patches
    or reads a patched file's body.
    """
    if not isinstance(patch, str) or len(patch.encode('utf-8')) > MAX_PATCH:
        return []
    lines = patch.strip('\r\n').splitlines()
    if not lines or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        return []
    candidates = []
    current = None
    for line in lines[1:-1]:
        if line.startswith(('*** Add File: ', '*** Update File: ', '*** Delete File: ')):
            if current is not None:
                candidates.append(current)
            current = None if line.startswith('*** Delete File: ') else line.split(': ', 1)[1]
        elif line.startswith('*** Move to: '):
            if current is None:
                return []
            current = line.split(': ', 1)[1]
        if len(candidates) >= MAX_PATHS:
            return []
    if current is not None:
        candidates.append(current)
    result = []
    root = _worktree_root(cwd)
    for value in candidates:
        if not value or '\x00' in value:
            continue
        path = (cwd / value).resolve()
        if path.is_relative_to(root) and path.is_file() and path not in result:
            result.append(path)
    return result


def _successful(payload: dict, tool: str) -> bool:
    response = payload.get('tool_response')
    if isinstance(response, dict):
        if (response.get('isError') or response.get('is_error') or response.get('error')
                or response.get('success') is False
                or response.get('status') in ('failed', 'error', 'cancelled')
                or ('exit_code' in response and response['exit_code'] != 0)):
            return False
        explicit_success = response.get('success') is True or response.get('exit_code') == 0
        if isinstance(response.get('content'), list):
            return all(isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str) for part in response['content']) and _successful(
                {'tool_response': '\n'.join(part.get('text', '') for part in response['content'])}, tool)
        if 'output' not in response and 'text' not in response:
            return explicit_success
        response = response.get('output', response.get('text'))
    if not isinstance(response, str) or len(response) > MAX_INPUT:
        return False
    response = response.strip()
    # Native code-mode PostToolUse exposes this execution envelope as a STRING.
    # Decode exactly one anchored envelope; nested/failed bodies are not success.
    if response.startswith('Exit code:'):
        envelope = re.fullmatch(
            r'Exit code: (-?\d+)\nWall time: \d+(?:\.\d+)? seconds\nOutput:\n(.*)',
            response, flags=re.DOTALL,
        )
        if envelope is None or envelope.group(1) != '0':
            return False
        response = envelope.group(2)
    return (response == 'Plan updated' if tool == 'update_plan'
            else response.startswith('Success. Updated the following files:'))


def _environment(payload: dict) -> dict[str, str]:
    env = os.environ.copy()
    for key in ('CLAUDECODE', 'CLAUDE_SESSION_ID', 'DCT_SESSION_ID'):
        env.pop(key, None)
    env.update(DCT_ACTOR_CLIENT='codex', DCT_ACTOR_TYPE='agent')
    if isinstance(payload.get('session_id'), str) and payload['session_id'].strip():
        env['DCT_SESSION_ID'] = payload['session_id']
    # The bridge and CLI must use the same installed DCT, not a stale PATH copy.
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
    return env


def _run_policy(name: str, payload: dict, cwd: Path, env: dict, timeout: float):
    bash = shutil.which('bash', path=env.get('PATH'))
    if not bash:
        return subprocess.CompletedProcess([], 0, '', '')
    return subprocess.run([bash, str(hooks_dir() / name)], input=json.dumps(payload),
                          cwd=cwd, env=env, text=True, capture_output=True,
                          timeout=timeout, check=False)


def _run_mirror(payload: dict, cwd: Path, env: dict, timeout: float):
    # Shared worker owns client-specific config gating before DB bootstrap and
    # the append-only mirror policy. Running it in a bounded child avoids an
    # unresponsive database holding the native hook indefinitely.
    return subprocess.run([sys.executable, '-m', 'dct.cli', '_task-mirror', '--client', 'codex'],
                          input=json.dumps(payload), cwd=cwd, env=env, text=True,
                          capture_output=True, timeout=timeout, check=False)


def handle_event(mode: str, payload: dict) -> tuple[int, dict | None, str]:
    """Translate one native event; irrelevant events never load config or DB."""
    if not isinstance(payload, dict):
        return 0, None, ''
    phase, tool = payload.get('hook_event_name'), payload.get('tool_name')
    if mode == 'stop':
        relevant = phase == 'Stop'
    elif mode == 'check-changelog':
        relevant = phase == 'PreToolUse' and tool in ('Bash', 'exec_command', 'shell_command')
    else:
        relevant = (phase == 'PostToolUse' and tool == ('apply_patch' if mode == 'autoscan' else 'update_plan')
                    and mode in ('autoscan', 'mirror') and _successful(payload, tool))
    if not relevant:
        return 0, None, ''
    if mode in ('autoscan', 'mirror'):
        if not isinstance(payload.get('session_id'), str) or not payload['session_id'].strip():
            return 0, None, ''
        if mode == 'mirror' and (not isinstance(payload.get('turn_id'), str) or not payload['turn_id'].strip()):
            return 0, None, ''
    try:
        cwd = _cwd(payload)
        if cwd is None:
            return _warning('missing or invalid event working directory')
        env = _environment(payload)
        deadline = time.monotonic() + TIMEOUT
        if mode == 'check-changelog':
            for target, command in _shell_contexts(payload, cwd):
                if time.monotonic() >= deadline:
                    return _warning('changelog policy exceeded its time budget; commit was not blocked')
                normalized = {**payload, 'tool_name': 'Bash', 'tool_input': {'command': command}}
                result = _run_policy('check-changelog.sh', normalized, target, env, max(0.01, deadline - time.monotonic()))
                if result.returncode == 2:
                    try:
                        decision = json.loads(result.stdout).get('hookSpecificOutput', {})
                    except (ValueError, AttributeError):
                        return _warning('changelog policy returned an invalid denial')
                    reason = decision.get('permissionDecisionReason')
                    if decision.get('permissionDecision') == 'deny' and isinstance(reason, str) and reason.strip():
                        return 2, None, reason
                    return _warning('changelog policy did not return an explicit denial')
                if result.returncode != 0:
                    return _warning('changelog policy failed; commit was not blocked')
        elif mode == 'stop':
            result = _run_policy('check-changelog-on-stop.sh', payload, cwd, env, TIMEOUT)
            if result.returncode != 0:
                return _warning('stop reminder failed')
            if result.stdout.strip():
                return 0, {'systemMessage': result.stdout.strip()}, ''
        elif mode == 'autoscan':
            data = payload.get('tool_input')
            if not isinstance(data, dict):
                return 0, None, ''
            for path in patch_paths(data.get('command'), cwd):
                if time.monotonic() >= deadline:
                    return _warning('autoscan dispatch exceeded its time budget')
                normalized = {**payload, 'tool_name': 'Edit', 'tool_input': {'file_path': str(path)}}
                result = _run_policy('dct-plan-autoscan.sh', normalized, cwd, env, max(0.01, deadline - time.monotonic()))
                if result.returncode != 0:
                    return _warning('autoscan dispatch failed')
        elif mode == 'mirror':
            result = _run_mirror(payload, cwd, env, TIMEOUT)
            if result.returncode != 0:
                return _warning('task mirror worker failed')
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        return _warning('runtime unavailable, malformed event or timeout; no blocking decision')
    return 0, None, ''


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        result = _warning('unknown bridge mode')
    else:
        try:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                result = _warning('event exceeds the input limit')
            else:
                result = handle_event(sys.argv[1], json.loads(raw))
        except (ValueError, OSError, RecursionError):
            result = _warning('invalid JSON event')
    code, output, reason = result
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    if reason:
        print(reason, file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
