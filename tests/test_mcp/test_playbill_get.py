"""The MCP get tool: declared parameters, the default profile, and its request."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import PlaybillByteRangeV1, PlaybillGetRequestV1
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

    assert PERMISSION_REQUIREMENTS["cruxible_playbill_get"] is PermissionMode.READ_ONLY
    schema = tools["cruxible_playbill_get"].inputSchema
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
    assert tools["cruxible_playbill_get"].outputSchema is not None


def test_get_handler_builds_the_request_for_the_mcp_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[PlaybillGetRequestV1] = []

    def local(instance_id: str, *, request: PlaybillGetRequestV1) -> Any:
        assert instance_id == "inst_get"
        captured.append(request)
        return "result"

    monkeypatch.setattr(handlers.playbill_api, "playbill_get", local)
    monkeypatch.setattr(
        handlers, "_dispatch_remote_or_local", lambda _remote, local, **_kw: local()
    )

    handlers.handle_playbill_get("inst_get", ref="CLM-0123abcd")
    handlers.handle_playbill_get(
        "inst_get",
        ref="Document:design",
        detail="body",
        range=PlaybillByteRangeV1(start=0, end=16),
        at="a" * 40,
        evaluation_time="2026-09-01T00:00:00Z",
    )

    assert captured[0].detail == "summary" and captured[0].surface == "mcp"
    assert captured[1].range == PlaybillByteRangeV1(start=0, end=16)
    assert captured[1].at == "a" * 40
    assert captured[1].evaluation_time is not None


def test_a_malformed_get_names_the_json_path_and_an_example() -> None:
    with pytest.raises(DataValidationError) as caught:
        handlers.handle_playbill_get(
            "inst_get", ref="Document:design", range=PlaybillByteRangeV1(start=0, end=4)
        )

    assert "example" in str(caught.value)
    assert any(item.startswith("$") for item in caught.value.errors)
    with pytest.raises(DataValidationError) as bad_at:
        handlers.handle_playbill_get("inst_get", ref="x", at="not-an-oid")
    assert any(item.startswith("$.at") for item in bad_at.value.errors)


def test_exact_content_reads_as_text_only_for_a_caller_who_may_read_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import request_permission_scope, reset_permissions

    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    reset_permissions()
    with request_permission_scope(PermissionMode.READ_ONLY):
        assert playbill_api._content_access().can_read_body is False
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        assert playbill_api._content_access().can_read_body is True
    reset_permissions()
