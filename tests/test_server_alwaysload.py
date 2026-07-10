"""#241: daily-driver dct tools are exempt from Claude Code MCP tool-search
deferral via the `anthropic/alwaysLoad` _meta hint, so /pickup and friends do
not pay a ToolSearch round-trip. Specialist tools stay deferred (frugal split).
"""

import asyncio

from dct.server import mcp, _ALWAYS_LOAD_TOOLS


def _wire_meta() -> dict[str, dict]:
    """name -> serialized wire `_meta` dict (what the CC client actually reads)."""
    tools = asyncio.run(mcp.list_tools())
    return {
        t.name: (t.model_dump(by_alias=True, exclude_none=True).get("_meta") or {})
        for t in tools
    }


def test_always_load_names_all_exist():
    """Typo guard: every name in the hot-set must be a real registered tool."""
    registered = set(mcp._tool_manager._tools.keys())
    missing = _ALWAYS_LOAD_TOOLS - registered
    assert not missing, f"hot-set names not registered as tools: {sorted(missing)}"


def test_hot_tools_emit_alwaysload_meta():
    wire = _wire_meta()
    for name in _ALWAYS_LOAD_TOOLS:
        assert wire[name].get("anthropic/alwaysLoad") is True, name


def test_cold_tools_stay_deferred():
    """The frugal split must hold — specialist tools carry no alwaysLoad flag."""
    wire = _wire_meta()
    for name in ["create_decision", "create_plan", "export_changelog",
                 "list_sprints", "ingest_plan_file", "supersede_decision"]:
        assert "anthropic/alwaysLoad" not in wire.get(name, {}), name


def test_pickup_tools_are_hot():
    """Contract: every tool dct/skills/pickup/SKILL.md calls must be loaded upfront."""
    pickup_tools = {
        "list_projects", "review_sprint", "list_handoffs",
        "consume_handoff", "delete_handoff", "get_item",
    }
    assert pickup_tools <= _ALWAYS_LOAD_TOOLS
