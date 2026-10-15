"""Deferred MCP classification retains integer identity and core project guards."""
import pytest

from dct.core.plans.crud import add_plan_section, create_plan, get_plan_section


def records(engine):
    plan = create_plan(engine, "test", slug="spec", title="Specification", kind="spec")
    return plan, add_plan_section(engine, plan["id"], "Unclear", 2, section_type="uncertain")


def test_mcp_setter_and_revocation_share_core_ack_and_identity(srv, engine):
    _, section = records(engine)
    ack = srv.set_plan_section_type(section["id"], "prose", "test")
    assert ack == {"id": section["id"], "ulid": section["ulid"], "section_type": "prose", "classification_source": "manual", "changed": True}
    assert srv.set_plan_section_type(section["id"], "prose", "test")["changed"] is False
    assert srv.set_plan_section_type(section["id"], "uncertain", "test")["changed"] is True
    assert get_plan_section(engine, section["id"])["needs_llm_review"] is True


@pytest.mark.parametrize("identifier", ("1", "0" * 26, True, 0, -1))
def test_mcp_refuses_non_integer_primary_keys_without_change(srv, engine, identifier):
    _, section = records(engine)
    assert "error" in srv.set_plan_section_type(identifier, "prose", "test")
    assert "error" in srv.resolve_uncertain_sections(identifier, "test")
    assert get_plan_section(engine, section["id"])["section_type"] == "uncertain"


def test_mcp_project_guard_refuses_other_project_and_unknown_type(srv, engine):
    from dct.core.projects import create_project
    create_project(engine, "other", "Other", "/tmp/other")
    plan, section = records(engine)
    assert "error" in srv.set_plan_section_type(section["id"], "prose", "other")
    assert "error" in srv.resolve_uncertain_sections(plan["id"], "other")
    assert "error" in srv.set_plan_section_type(section["id"], "invented", "test")
    assert get_plan_section(engine, section["id"])["classification_source"] is None


def test_deleted_parent_or_section_is_refused_without_type_write(srv, engine):
    """A CLI/MCP alias never resurrects a deleted parent or section."""
    from dct.core.plans.crud import delete_plan, delete_plan_section
    plan, section = records(engine)
    delete_plan_section(engine, section["id"])
    assert "error" in srv.set_plan_section_type(section["id"], "prose", "test")
    delete_plan(engine, plan["id"])
    assert "error" in srv.resolve_uncertain_sections(plan["id"], "test")


def test_new_semantic_tools_are_registered_deferred_with_separate_typed_parameters():
    """The host must actually discover the promised tools and their correct schemas."""
    import asyncio

    from dct.mcp.server import mcp
    tools = {t.name: t.model_dump(by_alias=True, exclude_none=True)
             for t in asyncio.run(mcp.list_tools())}
    expected = {
        "set_plan_section_type": {"section_id": "integer", "section_type": "string", "project": "string"},
        "resolve_uncertain_sections": {"plan_id": "integer", "project": "string"},
        "import_released_changelog": {"project": "string", "category": "string", "entry": "string", "version": "string"},
    }
    for name, fields in expected.items():
        wire = tools[name]
        assert "anthropic/alwaysLoad" not in wire.get("_meta", {})
        schema = wire["inputSchema"]
        assert set(fields) <= set(schema["required"])
        assert {key: schema["properties"][key]["type"] for key in fields} == fields
