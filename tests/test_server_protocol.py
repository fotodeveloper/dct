"""#429 / K-51: protocol-level smoke through the SDK v2 in-memory Client.

The migration ships structured_output=False on every tool — behavior-neutral:
clients keep reading JSON text out of `content`, no structured_content, and
dct's {"error": ...} payloads remain NORMAL results (is_error stays False).
Deliberate structured-output adoption is #442. These tests pin that contract
end-to-end through a real client connection, not by calling tool functions
directly like the rest of the server suite.
"""

import asyncio
import json

from mcp import Client

from dct.core.items import create_item as core_create_item
from dct.mcp import server as S


def _call(srv, name: str, args: dict):
    async def go():
        async with Client(srv.mcp) as client:
            return await client.call_tool(name, args)
    return asyncio.run(go())


def test_list_view_arrives_as_json_text_without_structured_content(srv):
    eng = srv._get_engine()
    core_create_item(eng, "test", "todo", "A")

    result = _call(srv, "list_items", {"project": "test"})

    assert result.is_error is False
    assert result.structured_content is None
    payload = json.loads(result.content[0].text)
    assert payload["cols"][0] == "id"
    assert len(payload["rows"]) == 1


def test_error_payload_is_a_normal_result_on_the_wire(srv):
    result = _call(srv, "update_item", {"item_id": 999999, "title": "x"})

    assert result.is_error is False  # dct's {"error"} contract, not a protocol error
    assert result.structured_content is None
    payload = json.loads(result.content[0].text)
    assert set(payload) == {"error"}


def test_str_returning_tool_stays_plain_text(srv):
    """K-51: a str-returning tool (export_changelog) must arrive as PLAIN text
    in content — not JSON-quoted, no structured wrapper."""
    result = _call(srv, "export_changelog", {"project": "test"})

    assert result.is_error is False
    assert result.structured_content is None
    assert result.content[0].text.startswith("# Changelog")


def test_every_tool_survives_a_wire_call(srv):
    """All-52 sweep (#429): every registered tool is called through the wire
    with EMPTY args in one client session. Tools with defaults execute (their
    {"error"}/data payloads are normal results); tools with required params
    come back as SDK validation errors (is_error=True) — and in BOTH cases the
    response carries text and the CONNECTION survives to the next call."""
    async def sweep():
        outcomes = {}
        async with Client(srv.mcp) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 52
            for tool in tools:
                result = await client.call_tool(tool.name, {})
                assert result.content and result.content[0].text.strip(), tool.name
                outcomes[tool.name] = result.is_error
            # The session survived all 52 calls — one more proves liveness.
            final = await client.call_tool("list_projects", {})
            assert final.is_error is False
        return outcomes

    outcomes = asyncio.run(sweep())
    assert len(outcomes) == 52


def test_every_tool_description_and_schema_match_signatures():
    """Docstring pipeline + schema derivation hold for ALL 52 tools on v2:
    the wire description is non-empty (the docstring IS the contract the
    calling model reads) and inputSchema properties equal the function
    signature exactly — no dropped params, no ghosts."""
    import inspect

    tools = asyncio.run(S.mcp.list_tools())
    assert len(tools) == 52
    for tool in tools:
        assert (tool.description or "").strip(), f"{tool.name}: empty description"
        fn = getattr(S, tool.name)
        sig_params = set(inspect.signature(fn).parameters)
        schema_props = set((tool.input_schema or {}).get("properties", {}))
        assert schema_props == sig_params, (
            f"{tool.name}: schema {sorted(schema_props)} != signature {sorted(sig_params)}"
        )
