"""#429: real-binary wire tests — the server is SPAWNED as a subprocess and
spoken to over stdio with raw newline-delimited JSON-RPC, exactly like an MCP
host does. This is the layer in-memory Client tests cannot see (process boot,
stdio transport, version negotiation with arbitrary client eras).

DB safety: every process uses a temporary config, cache and SQLite database.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# Client protocol eras the server must serve (release-notes claim: "one SDK,
# both protocol eras"). Empirically (2026-07-30): pre-2026 versions are echoed
# back verbatim; a 2026-07-28 request is negotiated DOWN to 2025-11-25 over
# bare stdio.
ECHOED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]


def isolated_wire_environment(tmp_path: Path) -> dict:
    """No inherited project/database/client settings or network update checks."""
    home = tmp_path / 'wire-home'
    home.mkdir(exist_ok=True)
    config = home / 'dct.toml'
    config.write_text('[update]\ncheck = false\n', encoding='utf-8')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DCT_', 'CLAUDE'))}
    env.update(DCT_CONFIG_PATH=str(config),
               DCT_DATABASE_URL=f'sqlite:///{tmp_path / "wire.sqlite"}',
               DCT_CACHE_DIR=str(home / 'cache'),
               DCT_DATA_DIR=str(home / 'data'), DCT_STATE_DIR=str(home / 'state'),
               PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    return env


class WireClient:
    """Bounded JSON-RPC transport using the installed production console script."""

    def __init__(self, env: dict, cwd: Path, client: str = 'wire-test'):
        binary = Path(sys.executable).parent / ('dct.exe' if os.name == 'nt' else 'dct')
        command = [str(binary), 'server'] if binary.exists() else [
            sys.executable, '-c', 'from dct.cli import main; main()', 'server']
        self.command = command
        self.env, self.cwd, self.client = env, cwd, client
        self.next_id = 0
        self.tool_exchanges = []

    def __enter__(self):
        self.proc = subprocess.Popen(self.command, env=self.env, cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.out = queue.Queue()
        self.errors = []

        def read_stdout():
            for line in self.proc.stdout:
                self.out.put(line)
            self.out.put(None)

        def read_stderr():
            for line in self.proc.stderr:
                self.errors.append(line)
                self.errors[:] = self.errors[-100:]

        self.readers = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=read_stderr, daemon=True)]
        for thread in self.readers:
            thread.start()
        return self

    def __exit__(self, *exc):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for thread in self.readers:
            thread.join(timeout=2)
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + '\n')
        self.proc.stdin.flush()

    def request(self, method, params=None, request_id=None):
        self.next_id += 1
        expected = request_id if request_id is not None else self.next_id
        request = {'jsonrpc': '2.0', 'id': expected, 'method': method}
        if params is not None:
            request['params'] = params
        self.send(request)
        deadline = time.monotonic() + 30
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                line = self.out.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError('MCP response timed out: ' + ''.join(self.errors)) from exc
            if line is None:
                raise AssertionError('MCP exited: ' + ''.join(self.errors))
            if not line.strip().startswith('{'):
                continue
            response = json.loads(line)
            if response.get('id') == expected:
                if method == 'tools/call':
                    self.tool_exchanges.append((request, response))
                return response

    def initialize(self, version='2025-11-25'):
        response = self.request('initialize', {'protocolVersion': version,
            'capabilities': {}, 'clientInfo': {'name': self.client, 'version': '0'}})
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return response

    def call(self, name, **arguments):
        response = self.request('tools/call', {'name': name, 'arguments': arguments})
        assert 'error' not in response, response
        result = response['result']
        assert not result.get('isError'), result
        return json.loads(next(block['text'] for block in result['content'] if block['type'] == 'text'))


def _handshake(tmp_path, protocol_version: str, extra_requests: list[dict] | None = None) -> list[dict]:
    with WireClient(isolated_wire_environment(tmp_path), Path(__file__).resolve().parents[1]) as client:
        responses = [client.initialize(protocol_version)]
        for request in extra_requests or []:
            responses.append(client.request(request['method'], request.get('params'), request['id']))
        return responses


@pytest.mark.parametrize("version", ECHOED_VERSIONS)
def test_pre_2026_client_eras_negotiate_their_own_version(version, tmp_path):
    responses = _handshake(tmp_path, version)
    result = next(r for r in responses if r.get("id") == 1)["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["name"] == "dct"
    assert result.get("instructions")  # v2 kept the instructions= constructor path


def test_2026_era_request_negotiates_a_supported_version(tmp_path):
    responses = _handshake(tmp_path, "2026-07-28")
    result = next(r for r in responses if r.get("id") == 1)["result"]
    # Bare-stdio serving caps below the newest revision — the reply must be a
    # version the server actually serves, never an error.
    assert result["protocolVersion"] in {"2025-11-25", "2026-07-28"}


def test_tools_list_over_spawned_stdio_carries_55_tools_and_13_alwaysload(tmp_path):
    responses = _handshake(tmp_path, "2025-06-18", extra_requests=[
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    tools = next(r for r in responses if r.get("id") == 2)["result"]["tools"]
    assert len(tools) == 55
    hot = [t["name"] for t in tools
           if (t.get("_meta") or {}).get("anthropic/alwaysLoad") is True]
    assert len(hot) == 13
