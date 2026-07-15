"""#241/#349: daily-driver dct tools are exempt from Claude Code MCP tool-search
deferral via the `anthropic/alwaysLoad` _meta hint, so the working session does
not pay a ToolSearch round-trip on the tools it touches turn after turn.
Session-edge and specialist tools stay deferred (frugal split).
"""

import asyncio

from dct.server import mcp, _ALWAYS_LOAD_TOOLS

# The exact mid-session hot-set (#349): item + checkpoint lifecycle plus the
# changelog add. Session-edge tools (pickup/handoff/release) are deferred.
EXPECTED_HOT_SET = {
    "create_item", "get_item", "list_items", "update_item", "resolve_item",
    "add_note", "add_checkpoint", "complete_checkpoint", "reject_checkpoint",
    "add_changelog",
}

# Once-per-session / specialist tools that MUST stay deferred.
EXPECTED_COLD = {
    "review_sprint", "set_sprint", "list_projects", "count_unreleased_changelog",
    "create_handoff", "list_handoffs", "consume_handoff", "delete_handoff",
    "get_handoff",
    "create_decision", "create_plan", "export_changelog",
    "list_sprints", "ingest_plan_file", "supersede_decision",
}


def _wire_meta() -> dict[str, dict]:
    """name -> serialized wire `_meta` dict (what the CC client actually reads)."""
    tools = asyncio.run(mcp.list_tools())
    return {
        t.name: (t.model_dump(by_alias=True, exclude_none=True).get("_meta") or {})
        for t in tools
    }


def test_hot_set_is_exactly_ten():
    """Lock the exact hot-set: 10 mid-session daily drivers, no more, no less."""
    assert _ALWAYS_LOAD_TOOLS == EXPECTED_HOT_SET
    assert len(_ALWAYS_LOAD_TOOLS) == 10


def test_always_load_names_all_exist():
    """Typo guard: every name in the hot-set must be a real registered tool."""
    registered = set(mcp._tool_manager._tools.keys())
    missing = _ALWAYS_LOAD_TOOLS - registered
    assert not missing, f"hot-set names not registered as tools: {sorted(missing)}"


def test_hot_tools_emit_alwaysload_meta():
    wire = _wire_meta()
    for name in _ALWAYS_LOAD_TOOLS:
        assert wire[name].get("anthropic/alwaysLoad") is True, name


def test_cold_set_names_all_exist():
    """Typo/drift guard: every name asserted deferred must be a real registered
    tool — otherwise a dropped/renamed tool would pass the deferral check
    vacuously (a missing name trivially carries no alwaysLoad flag)."""
    registered = set(mcp._tool_manager._tools.keys())
    missing = EXPECTED_COLD - registered
    assert not missing, f"EXPECTED_COLD names not registered as tools: {sorted(missing)}"


def test_session_edge_tools_stay_deferred():
    """The frugal split must hold — session-edge and specialist tools carry no
    alwaysLoad flag, so they load behind a ToolSearch round-trip when needed."""
    wire = _wire_meta()
    for name in EXPECTED_COLD:
        assert "anthropic/alwaysLoad" not in wire.get(name, {}), name
