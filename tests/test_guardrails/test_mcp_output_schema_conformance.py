"""Guardrail: every MCP tool advertises an object-rooted output schema.

The MCP specification requires a tool's ``outputSchema`` to be an object
schema. A union-annotated return derives to an ``anyOf`` ROOT, which strict
clients reject outright — so union results must be carried under an object
envelope (``{"result": <union>}``), which is also what FastMCP generates on its
own for union-annotated returns. This test fails the moment a new tool ships a
non-object root, rather than leaving it for a client to discover.
"""

from __future__ import annotations

import asyncio

import pytest

from cruxible_core.mcp.server import create_server

# Root keys that make a schema a union/alternation rather than a plain object.
_ALTERNATION_KEYS = ("anyOf", "oneOf", "allOf", "not")


@pytest.fixture(scope="module")
def tool_schemas() -> dict[str, dict]:
    server = create_server()
    tools = asyncio.run(server.list_tools())
    return {tool.name: tool.outputSchema for tool in tools}


def test_every_tool_declares_an_output_schema(tool_schemas: dict[str, dict]) -> None:
    assert tool_schemas
    missing = sorted(name for name, schema in tool_schemas.items() if schema is None)
    assert missing == [], f"tools without an outputSchema: {missing}"


def test_every_output_schema_root_is_type_object(tool_schemas: dict[str, dict]) -> None:
    offenders = sorted(
        name for name, schema in tool_schemas.items() if (schema or {}).get("type") != "object"
    )
    assert offenders == [], (
        "MCP output schemas must be object-rooted; these are not: "
        f"{offenders}. Wrap union results in a {{'result': <union>}} envelope."
    )


def test_no_output_schema_root_is_a_union(tool_schemas: dict[str, dict]) -> None:
    offenders = sorted(
        name
        for name, schema in tool_schemas.items()
        if any(key in (schema or {}) for key in _ALTERNATION_KEYS)
    )
    assert offenders == [], (
        "These tools advertise a union at the schema ROOT, which strict MCP "
        f"clients reject: {offenders}. Nest the union under a 'result' property."
    )


def test_union_tools_use_the_result_envelope(tool_schemas: dict[str, dict]) -> None:
    """The surviving union-returning tool uses the reviewed envelope convention."""
    schema = tool_schemas["cruxible_playbill_explain"]

    assert schema["type"] == "object"
    assert schema["required"] == ["result"]
    assert set(schema["properties"]) == {"result"}
    assert "anyOf" in schema["properties"]["result"]
    assert schema["$defs"]
