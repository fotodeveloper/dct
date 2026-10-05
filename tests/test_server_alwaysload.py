"""#241/#349: daily-driver dct tools are exempt from Claude Code MCP tool-search
deferral via the `anthropic/alwaysLoad` _meta hint, so the working session does
not pay a ToolSearch round-trip on the tools it touches turn after turn.
Session-edge and specialist tools stay deferred (frugal split).
"""

import asyncio

from dct.mcp.server import _ALWAYS_LOAD_TOOLS, mcp

# The exact hot-set: the mid-session drivers (#349) — item + checkpoint lifecycle
# plus the changelog add — and the composed workflow entry points (#413), which
# ARE the session edge. The granular session-edge tools they compose stay deferred.
EXPECTED_HOT_SET = {
    "create_item", "get_item", "list_items", "update_item", "resolve_item",
    "add_note", "add_checkpoint", "complete_checkpoint", "reject_checkpoint",
    "add_changelog",
    "get_context_snapshot", "pickup_work", "handoff_work",
}

# Once-per-session / specialist tools that MUST stay deferred.
EXPECTED_COLD = {
    "review_sprint", "set_sprint", "list_projects", "count_unreleased_changelog",
    "create_handoff", "list_handoffs", "consume_handoff", "delete_handoff",
    "get_handoff",
    "create_decision", "create_plan", "export_changelog",
    "list_sprints", "ingest_plan_file", "supersede_decision",
    # tag dictionary admin (#611) — tagging itself rides on the hot item tools
    "add_tag", "list_tags", "merge_tag",
    # changelog edit / delete (#359) — rare, surgical corrections
    "update_changelog", "delete_changelog",
    # release records (K-41) — /release resume + progress, session-edge only
    "get_release", "advance_release",
}


def _wire_meta() -> dict[str, dict]:
    """name -> serialized wire `_meta` dict (what the CC client actually reads)."""
    tools = asyncio.run(mcp.list_tools())
    return {
        t.name: (t.model_dump(by_alias=True, exclude_none=True).get("_meta") or {})
        for t in tools
    }


def _registered_names() -> set[str]:
    """All registered tool names via the PUBLIC surface (K-48: no _tool_manager)."""
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_hot_set_is_exactly_thirteen():
    """Lock the exact hot-set: 10 mid-session daily drivers + 3 workflow entry
    points, no more, no less."""
    assert _ALWAYS_LOAD_TOOLS == EXPECTED_HOT_SET
    assert len(_ALWAYS_LOAD_TOOLS) == 13


def test_always_load_names_all_exist():
    """Typo guard: every name in the hot-set must be a real registered tool.
    Since K-47 the meta rides the decorators, so a frozenset typo no longer
    fails at import — this test is the guard now."""
    missing = _ALWAYS_LOAD_TOOLS - _registered_names()
    assert not missing, f"hot-set names not registered as tools: {sorted(missing)}"


def test_hot_tools_emit_alwaysload_meta():
    wire = _wire_meta()
    for name in _ALWAYS_LOAD_TOOLS:
        assert wire[name].get("anthropic/alwaysLoad") is True, name


def test_wire_hot_set_equals_spec_exactly():
    """K-47 binding: _ALWAYS_LOAD_TOOLS is the spec, the decorators' meta= the
    mechanism — the alwaysLoad set on the wire must equal the frozenset
    EXACTLY, so a stray meta on a cold tool fails as loudly as a missing one."""
    wire = _wire_meta()
    hot_on_wire = {n for n, m in wire.items() if m.get("anthropic/alwaysLoad") is True}
    assert hot_on_wire == set(_ALWAYS_LOAD_TOOLS)


def test_cold_set_names_all_exist():
    """Typo/drift guard: every name asserted deferred must be a real registered
    tool — otherwise a dropped/renamed tool would pass the deferral check
    vacuously (a missing name trivially carries no alwaysLoad flag)."""
    missing = EXPECTED_COLD - _registered_names()
    assert not missing, f"EXPECTED_COLD names not registered as tools: {sorted(missing)}"


def test_session_edge_tools_stay_deferred():
    """The frugal split must hold — session-edge and specialist tools carry no
    alwaysLoad flag, so they load behind a ToolSearch round-trip when needed."""
    wire = _wire_meta()
    for name in EXPECTED_COLD:
        assert "anthropic/alwaysLoad" not in wire.get(name, {}), name
