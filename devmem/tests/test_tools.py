import anyio

from devmem.server import create_http_app, mcp

EXPECTED_TOOLS = {
    "memory_start_session",
    "memory_search",
    "memory_save",
    "memory_supersede",
    "task_create",
    "task_update",
    "task_list",
    "memory_handoff",
}


def test_tool_names_and_search_parameters():
    tools = {tool.name: tool for tool in anyio.run(mcp.list_tools)}
    assert set(tools) == EXPECTED_TOOLS
    schema = tools["memory_search"].inputSchema
    assert {"query", "project", "kinds", "task_id", "k", "fmt"} <= set(schema["properties"])
    assert "query" in schema["required"]
    assert "project" in schema["required"]
    assert "k" not in schema.get("required", [])
    save_schema = tools["memory_save"].inputSchema
    assert {"project", "kind", "content", "task_id", "refs", "commit_sha"} <= set(
        save_schema["properties"]
    )


def test_brief_resource_template():
    templates = anyio.run(mcp.list_resource_templates)
    assert any(template.uriTemplate == "devmem://project/{slug}/brief" for template in templates)


def test_empty_token_does_not_build_the_app():
    import pytest

    with pytest.raises(RuntimeError, match="DEVMEM_TOKEN"):
        create_http_app("")
    with pytest.raises(RuntimeError, match="DEVMEM_TOKEN"):
        create_http_app("   ")
