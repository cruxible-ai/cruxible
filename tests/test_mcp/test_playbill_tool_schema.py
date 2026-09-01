"""Schema laws for the reduced Playbill MCP tool catalog."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cruxible_core.mcp.server import create_server
from cruxible_core.playbill.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS


@pytest.fixture(autouse=True)
def _full_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")


def _schemas() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(create_server().list_tools())}


def test_registered_schema_catalog_matches_permission_catalog() -> None:
    assert set(_schemas()) == set(TOOL_PERMISSIONS)


def test_init_and_explain_publish_their_protocol_enums() -> None:
    schemas = _schemas()
    init = schemas["cruxible_playbill_init"].inputSchema
    assert {"instance_id", "principals"} <= set(init["required"])
    assert init["properties"]["operating_profile"]["enum"] == ["local", "cloud"]
    assert init["properties"]["require_independent_approval"]["default"] is False

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
    prepare_schema = schemas["cruxible_playbill_authoring_prepare_publication"].inputSchema

    assert set(compile_schema["properties"]) == {"instance_id", "payload", "intent_id"}
    assert set(submit_schema["properties"]) == {"instance_id", "intent_id"}
    assert set(confirm_schema["properties"]) == {"instance_id", "intent_id", "observation"}
    assert set(prepare_schema["properties"]) == {"instance_id", "intent_id", "observation"}
    forbidden = {"base", "claim_id", "candidate_digest", "predecessor_digest"}
    assert forbidden.isdisjoint(compile_schema["properties"])
    assert forbidden.isdisjoint(submit_schema["properties"])
    assert forbidden.isdisjoint(confirm_schema["properties"])
    assert forbidden.isdisjoint(prepare_schema["properties"])

    example_schema = schemas["cruxible_playbill_authoring_example"].inputSchema
    assert example_schema["properties"]["name"]["enum"] == [
        "claim-existing-capture",
        "claim-flow-a",
        "claim-self-source",
        "procedure",
        "claim-adjudicate-contradicting-evidence",
        "claim-cite-supporting-evidence",
        "claim-adjudicate-unreviewed-evidence",
        "query-claims-by-type",
        "subject",
        "approval-policy",
        "procedure-runtime-policy",
        "procedure-mandate",
    ]
    bind_schema = schemas["cruxible_playbill_authoring_bind"].inputSchema
    assert set(bind_schema["properties"]) == {
        "instance_id",
        "source_path",
        "anchor",
        "payload",
        "window_lines",
    }
    assert forbidden.isdisjoint(bind_schema["properties"])


def test_search_schema_exposes_modes_but_not_access_or_digest_plumbing() -> None:
    schema = _schemas()["cruxible_playbill_search"].inputSchema
    assert schema["properties"]["mode"]["enum"] == ["search", "list", "orient"]
    kind_schema = next(
        member for member in schema["properties"]["kinds"]["anyOf"] if member.get("type") == "array"
    )
    assert kind_schema["items"]["enum"] == ["claim", "procedure", "demand"]
    assert "access_profile" not in schema["properties"]
    assert "selection_basis_digest" not in schema["properties"]


def test_since_schema_exposes_the_frozen_history_wire() -> None:
    schema = _schemas()["cruxible_playbill_since"].inputSchema
    assert set(schema["properties"]) == {
        "instance_id",
        "generation",
        "at",
        "access_profile",
        "max_rows",
        "max_bytes",
        "cursor",
    }
    assert set(schema["required"]) == {"instance_id", "generation"}
    assert schema["properties"]["max_rows"]["maximum"] == 1000
    assert schema["properties"]["max_bytes"]["maximum"] == 1_048_576


def test_audit_schema_uses_the_central_budget_calibration() -> None:
    schema = _schemas()["cruxible_playbill_audit"].inputSchema
    rows = schema["properties"]["max_rows"]
    bytes_ = schema["properties"]["max_bytes"]

    assert (rows["default"], rows["minimum"], rows["maximum"]) == (
        AUDIT_BUDGET_DEFAULT_MAX_ROWS,
        AUDIT_BUDGET_MIN_MAX_ROWS,
        AUDIT_BUDGET_MAX_MAX_ROWS,
    )
    assert (bytes_["default"], bytes_["minimum"], bytes_["maximum"]) == (
        AUDIT_BUDGET_DEFAULT_MAX_BYTES,
        AUDIT_BUDGET_MIN_MAX_BYTES,
        AUDIT_BUDGET_MAX_MAX_BYTES,
    )
