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


def test_authoring_tools_expose_payload_and_opaque_intent_not_plumbing() -> None:
    schemas = _schemas()
    compile_schema = schemas["cruxible_playbill_authoring_compile"].inputSchema
    submit_schema = schemas["cruxible_playbill_authoring_submit"].inputSchema
    confirm_schema = schemas["cruxible_playbill_authoring_confirm_insertion"].inputSchema

    assert set(compile_schema["properties"]) == {"instance_id", "payload", "intent_id"}
    assert set(submit_schema["properties"]) == {"instance_id", "intent_id"}
    assert set(confirm_schema["properties"]) == {"instance_id", "intent_id", "observation"}
    forbidden = {"base", "claim_id", "candidate_digest", "predecessor_digest"}
    assert forbidden.isdisjoint(compile_schema["properties"])
    assert forbidden.isdisjoint(submit_schema["properties"])
    assert forbidden.isdisjoint(confirm_schema["properties"])


def test_search_schema_exposes_modes_but_not_access_or_digest_plumbing() -> None:
    schema = _schemas()["cruxible_playbill_search"].inputSchema
    assert schema["properties"]["mode"]["enum"] == ["search", "list", "orient"]
    assert "access_profile" not in schema["properties"]
    assert "selection_basis_digest" not in schema["properties"]
