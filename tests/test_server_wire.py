"""#429: real-binary wire tests — the server is SPAWNED as a subprocess and
spoken to over stdio with raw newline-delimited JSON-RPC, exactly like an MCP
host does. This is the layer in-memory Client tests cannot see (process boot,
stdio transport, version negotiation with arbitrary client eras).

DB safety: initialize / tools/list never touch the engine (it bootstraps
lazily on the first tool CALL), so spawning against the developer's real
config is read-nothing.
"""

import json
import queue
import subprocess
import sys
import threading

import pytest

# Client protocol eras the server must serve (release-notes claim: "one SDK,
# both protocol eras"). Empirically (2026-07-30): pre-2026 versions are echoed
# back verbatim; a 2026-07-28 request is negotiated DOWN to 2025-11-25 over
# bare stdio.
ECHOED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]


def _handshake(protocol_version: str, extra_requests: list[dict] | None = None) -> list[dict]:
    """Spawn `python -m dct.mcp.server`, run initialize (+ extras), return the
    id-bearing responses. stdin stays OPEN until every expected response has
    arrived — closing early races the server's EOF shutdown against request
    processing (observed: tools/list silently dropped)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "dct.mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )
    out: queue.Queue[str] = queue.Queue()
    threading.Thread(
        target=lambda: [out.put(line) for line in proc.stdout], daemon=True
    ).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv_id(expected: int) -> dict:
        # Skip server-initiated notifications/logs — match on the request id.
        while True:
            line = out.get(timeout=30)
            if not line.strip().startswith("{"):
                continue
            msg = json.loads(line)
            if msg.get("id") == expected:
                return msg

    responses = []
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": protocol_version, "capabilities": {},
            "clientInfo": {"name": "wire-test", "version": "0"}}})
        responses.append(recv_id(1))
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for req in extra_requests or []:
            send(req)
            responses.append(recv_id(req["id"]))
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return responses


@pytest.mark.parametrize("version", ECHOED_VERSIONS)
def test_pre_2026_client_eras_negotiate_their_own_version(version):
    responses = _handshake(version)
    result = next(r for r in responses if r.get("id") == 1)["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["name"] == "dct"
    assert result.get("instructions")  # v2 kept the instructions= constructor path


def test_2026_era_request_negotiates_a_supported_version():
    responses = _handshake("2026-07-28")
    result = next(r for r in responses if r.get("id") == 1)["result"]
    # Bare-stdio serving caps below the newest revision — the reply must be a
    # version the server actually serves, never an error.
    assert result["protocolVersion"] in {"2025-11-25", "2026-07-28"}


def test_tools_list_over_spawned_stdio_carries_52_tools_and_13_alwaysload():
    responses = _handshake("2025-06-18", extra_requests=[
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    tools = next(r for r in responses if r.get("id") == 2)["result"]["tools"]
    assert len(tools) == 52
    hot = [t["name"] for t in tools
           if (t.get("_meta") or {}).get("anthropic/alwaysLoad") is True]
    assert len(hot) == 13
