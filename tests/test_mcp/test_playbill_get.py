"""The MCP get tool: declared parameters, the default profile, and its request."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import ByteRange, GetRequest
from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import PERMISSION_REQUIREMENTS, PermissionMode


def _object_properties(schema: dict[str, Any], defs: dict[str, Any]) -> list[dict[str, Any]]:
    """Every object schema a parameter can take, with $refs resolved."""

    found: list[dict[str, Any]] = []
    for option in schema.get("anyOf", [schema]):
        if "$ref" in option:
            option = defs[option["$ref"].rsplit("/", 1)[1]]
        if option.get("type") == "object":
            found.append(option)
    return found


def test_get_is_a_read_only_default_tool_with_only_declared_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    assert PERMISSION_REQUIREMENTS["cruxible_get"] is PermissionMode.READ_ONLY
    schema = tools["cruxible_get"].inputSchema
    defs = schema.get("$defs", {})
    assert schema["required"] == ["ref"]
    assert set(schema["properties"]) == {
        "instance_id",
        "ref",
        "detail",
        "range",
        "at",
        "evaluation_time",
        "limit",
        "cursor",
    }
    assert schema["properties"]["detail"]["enum"] == [
        "summary",
        "evidence",
        "why",
        "history",
        "proof",
        "body",
    ]
    # No free-form object: every object a parameter takes declares its fields.
    for name, prop in schema["properties"].items():
        for option in _object_properties(prop, defs):
            assert option.get("properties"), name
            assert option.get("additionalProperties") is not True, name
    assert tools["cruxible_get"].outputSchema is not None


def test_get_handler_builds_the_request_for_the_mcp_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[GetRequest] = []

    def get(instance_id: str, *, request: GetRequest) -> Any:
        assert instance_id == "inst_get"
        captured.append(request)
        return "result"

    monkeypatch.setattr(handlers, "_get_client", lambda: SimpleNamespace(get=get))

    handlers.handle_playbill_get("inst_get", ref="CLM-0123abcd")
    handlers.handle_playbill_get(
        "inst_get",
        ref="Document:design",
        detail="body",
        range=ByteRange(start=0, end=16),
        at="a" * 40,
        evaluation_time="2026-09-01T00:00:00Z",
    )

    assert captured[0].detail == "summary" and captured[0].surface == "mcp"
    assert captured[1].range == ByteRange(start=0, end=16)
    assert captured[1].at == "a" * 40
    assert captured[1].evaluation_time is not None


def test_a_malformed_get_names_the_json_path_and_an_example() -> None:
    with pytest.raises(DataValidationError) as caught:
        handlers.handle_playbill_get(
            "inst_get", ref="Document:design", range=ByteRange(start=0, end=4)
        )

    assert "example" in str(caught.value)
    assert any(item.startswith("$") for item in caught.value.errors)
    with pytest.raises(DataValidationError) as bad_at:
        handlers.handle_playbill_get("inst_get", ref="x", at="not-an-oid")
    assert any(item.startswith("$.at") for item in bad_at.value.errors)
