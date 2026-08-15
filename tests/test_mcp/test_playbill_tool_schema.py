"""Schema laws for the reduced Playbill MCP tool catalog."""

from __future__ import annotations

import asyncio
from typing import Any

from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS


def _schemas() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(create_server().list_tools())}


def test_registered_schema_catalog_matches_permission_catalog() -> None:
    assert set(_schemas()) == set(TOOL_PERMISSIONS)


def test_init_and_explain_publish_their_protocol_enums() -> None:
    schemas = _schemas()
    init = schemas["cruxible_playbill_init"].inputSchema
    assert {"instance_id", "principals"} <= set(init["required"])
    assert init["properties"]["operating_profile"]["enum"] == ["local", "cloud"]

    explain = schemas["cruxible_playbill_explain"].inputSchema
    assert explain["properties"]["detail"]["enum"] == ["summary", "evidence", "proof"]


def test_agent_schema_never_accepts_private_keys_or_local_paths() -> None:
    forbidden = {"private_key", "private_key_path", "local_path", "workspace_root"}
    violations: list[str] = []
    for name, tool in _schemas().items():
        properties = set(tool.inputSchema.get("properties", {}))
        overlap = properties & forbidden
        if overlap:
            violations.append(f"{name}: {sorted(overlap)}")
    assert violations == []


def test_playbill_tools_publish_typed_output_schemas() -> None:
    schemas = _schemas()
    for name, tool in schemas.items():
        if name == "cruxible_version":
            continue
        assert tool.outputSchema is not None, name
