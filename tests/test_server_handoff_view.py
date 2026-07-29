"""Handoff read-view tests (#352 server part).

Covers the get_handoff MCP tool (full untruncated fetch + missing-id error)
and the list_handoffs prompt-preview truncation (200-char boundary, ellipsis
marker only when truncated) across every output format. The invariant: lists
carry a lightweight preview, the full prompt is one get_handoff(id) away.

Tools are called directly (FastMCP's @mcp.tool() leaves the function callable),
with server._get_engine monkeypatched onto the in-memory fixture engine.
"""


def _prompt_from_structured(result, handoff_id):
    """Pull the prompt cell for a given handoff id out of a {cols, rows} view."""
    cols = result["cols"]
    id_i, prompt_i = cols.index("id"), cols.index("prompt")
    for row in result["rows"]:
        if row[id_i] == handoff_id:
            return row[prompt_i]
    raise AssertionError(f"handoff #{handoff_id} not in view")


# ── get_handoff ───────────────────────────────────────────────────────────────


def test_get_handoff_returns_full_record(srv):
    h = srv.create_handoff(
        prompt="do the next thing",
        created_by="tester",
        project="test",
        scope="sprint-x",
        context_summary="lots of surrounding context",
    )
    out = srv.get_handoff(h["id"])
    assert "error" not in out
    assert out["id"] == h["id"]
    assert out["prompt"] == "do the next thing"
    assert out["context_summary"] == "lots of surrounding context"
    assert out["scope"] == "sprint-x"
    assert out["created_by"] == "tester"
    assert "created_at" in out
    assert out["consumed_at"] is None
    assert out["consumed_by"] is None


def test_get_handoff_missing_id_error(srv):
    out = srv.get_handoff(90909)
    assert out == {"error": "Handoff #90909 not found"}


def test_get_handoff_soft_deleted_is_not_found(srv):
    h = srv.create_handoff(prompt="ephemeral", created_by="tester", project="test")
    srv.delete_handoff(h["id"])
    out = srv.get_handoff(h["id"])
    assert out == {"error": f"Handoff #{h['id']} not found"}


# ── list_handoffs prompt-preview boundary ─────────────────────────────────────
#
# Contract (K-35, #413): the documented "200-char preview" is a hard CEILING —
# the ellipsis is counted, so len(preview) <= 200 always. The server and the
# snapshot share ONE implementation (dct.workflows.session._preview).


def test_server_preview_is_the_workflows_one(srv):
    """K-35: no second copy — the two names must be the same object."""
    from dct.workflows.session import _preview as canonical

    assert srv._preview is canonical


def test_preview_exactly_200_chars_unchanged(srv):
    prompt = "x" * 200
    h = srv.create_handoff(prompt=prompt, created_by="tester", project="test")
    got = _prompt_from_structured(srv.list_handoffs(project="test"), h["id"])
    assert got == prompt
    assert len(got) == 200
    assert "…" not in got


def test_preview_201_chars_truncated_with_marker(srv):
    prompt = "y" * 201
    h = srv.create_handoff(prompt=prompt, created_by="tester", project="test")
    got = _prompt_from_structured(srv.list_handoffs(project="test"), h["id"])
    # 199 content chars + '…' == 200 total — the ellipsis is INSIDE the budget.
    assert got == "y" * 199 + "…"
    assert len(got) == 200
    assert got.endswith("…")


def test_preview_never_exceeds_200_for_any_length(srv):
    """Property: whatever the input length, the preview fits the budget."""
    from dct.workflows.session import _preview

    for n in (0, 1, 199, 200, 201, 1000, 100_000):
        got = _preview("q" * n)
        assert len(got) <= 200
        assert got.endswith("…") is (n > 200)


def test_preview_applies_to_dict_and_markdown_formats(srv):
    prompt = "z" * 201
    h = srv.create_handoff(prompt=prompt, created_by="tester", project="test")

    dict_rows = srv.list_handoffs(project="test", format="dict")
    row = next(r for r in dict_rows if r["id"] == h["id"])
    assert row["prompt"] == "z" * 199 + "…"

    md = srv.list_handoffs(project="test", format="markdown")
    assert "z" * 199 + "…" in md
    assert "z" * 201 not in md


def test_full_prompt_still_available_via_get_handoff(srv):
    prompt = "w" * 500
    h = srv.create_handoff(prompt=prompt, created_by="tester", project="test")
    # list view is truncated ...
    listed = _prompt_from_structured(srv.list_handoffs(project="test"), h["id"])
    assert listed == "w" * 199 + "…"
    assert len(listed) == 200
    # ... but the full text is one get_handoff away
    assert srv.get_handoff(h["id"])["prompt"] == prompt
