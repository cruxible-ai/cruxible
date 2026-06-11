"""Tests for governed-write MCP tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.mcp.server import create_server

CONFIG_YAML = """\
version: "1.0"
name: governed_write_tools
description: MCP governed-write coverage

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
      year: {type: int}
      make: {type: string}
      model: {type: string}
  Part:
    properties:
      part_number: {type: string, primary_key: true}
      name: {type: string}
      category: {type: string}
      price: {type: float, optional: true}

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified: {type: bool}
      source: {type: string, optional: true}

named_queries:
  parts_for_vehicle:
    mode: traversal
    description: Find parts for vehicle
    entry_point: Vehicle
    traversal:
      - relationship: fits
        direction: incoming
    returns: "list[Part]"

constraints: []
"""


def call_tool(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def call_tool_expect_error(server, name: str, args: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(server.call_tool(name, args))
    return str(exc_info.value)


@pytest.fixture
def server(governed_client):
    del governed_client
    return create_server()


@pytest.fixture
def instance_id(server, tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    result = call_tool(
        server,
        "cruxible_init",
        {"root_dir": str(tmp_path), "config_path": "config.yaml"},
    )
    iid = result["instance_id"]
    call_tool(
        server,
        "cruxible_add_entity",
        {
            "instance_id": iid,
            "entities": [
                {
                    "entity_type": "Part",
                    "entity_id": "BP-1",
                    "properties": {"part_number": "BP-1", "name": "Pads", "category": "brakes"},
                },
                {
                    "entity_type": "Part",
                    "entity_id": "BP-2",
                    "properties": {"part_number": "BP-2", "name": "Rotor", "category": "brakes"},
                },
                {
                    "entity_type": "Vehicle",
                    "entity_id": "V-1",
                    "properties": {
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                },
            ],
        },
    )
    call_tool(
        server,
        "cruxible_add_relationship",
        {
            "instance_id": iid,
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1",
                    "relationship": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True, "source": "catalog"},
                },
                {
                    "from_type": "Part",
                    "from_id": "BP-2",
                    "relationship": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True, "source": "catalog"},
                },
            ],
        },
    )
    return iid


def test_feedback_batch_tool(server, instance_id):
    query = call_tool(
        server,
        "cruxible_query",
        {
            "instance_id": instance_id,
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-1"},
        },
    )
    receipt_id = query["receipt_id"]
    result = call_tool(
        server,
        "cruxible_feedback_batch",
        {
            "instance_id": instance_id,
            "source": "human",
            "items": [
                {
                    "receipt_id": receipt_id,
                    "action": "approve",
                    "target": {
                        "from_type": "Part",
                        "from_id": "BP-1",
                        "relationship": "fits",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                    },
                },
                {
                    "receipt_id": receipt_id,
                    "action": "reject",
                    "target": {
                        "from_type": "Part",
                        "from_id": "BP-2",
                        "relationship": "fits",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                    },
                },
            ],
        },
    )
    assert result["total"] == 2
    assert result["applied_count"] == 2
    assert len(result["feedback_ids"]) == 2
    assert result["receipt_id"]


def test_decision_record_tools_and_workflow_context_round_trip(
    server,
    tmp_path,
    workflow_config_yaml: str,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(workflow_config_yaml)
    init = call_tool(
        server,
        "cruxible_init",
        {"root_dir": str(tmp_path), "config_path": "config.yaml"},
    )
    instance_id = init["instance_id"]
    call_tool(
        server,
        "cruxible_add_entity",
        {
            "instance_id": instance_id,
            "entities": [
                {
                    "entity_type": "Product",
                    "entity_id": "SKU-123",
                    "properties": {
                        "sku": "SKU-123",
                        "category": "soda",
                        "base_margin": 0.2,
                    },
                }
            ],
        },
    )
    call_tool(server, "cruxible_lock_workflow", {"instance_id": instance_id})

    created = call_tool(
        server,
        "cruxible_create_decision_record",
        {
            "instance_id": instance_id,
            "question": "Should the promo run?",
            "subject_type": "Product",
            "subject_id": "SKU-123",
            "opened_by": "agent",
        },
    )
    decision_record_id = created["record"]["decision_record_id"]

    fetched = call_tool(
        server,
        "cruxible_get_decision_record",
        {"instance_id": instance_id, "decision_record_id": decision_record_id},
    )
    assert fetched["record"]["question"] == "Should the promo run?"

    listed = call_tool(
        server,
        "cruxible_list_decision_records",
        {"instance_id": instance_id, "status": "open"},
    )
    assert [record["decision_record_id"] for record in listed["items"]] == [decision_record_id]

    run = call_tool(
        server,
        "cruxible_run_workflow",
        {
            "instance_id": instance_id,
            "workflow_name": "evaluate_promo",
            "input_payload": {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
            "decision_record_id": decision_record_id,
        },
    )
    assert run["receipt_id"].startswith("RCP-")

    events = call_tool(
        server,
        "cruxible_list_decision_events",
        {"instance_id": instance_id, "decision_record_id": decision_record_id},
    )
    assert len(events["items"]) == 1
    assert events["items"][0]["command"] == "workflow_run:evaluate_promo"
    assert events["items"][0]["receipt_id"] == run["receipt_id"]
    assert events["items"][0]["surface"] == "mcp"

    finalized = call_tool(
        server,
        "cruxible_finalize_decision_record",
        {
            "instance_id": instance_id,
            "decision_record_id": decision_record_id,
            "final_decision": "Run the promo",
            "decision_class": "recommended",
            "rationale": "Predicted lift is positive",
        },
    )
    assert finalized["record"]["status"] == "finalized"
    assert finalized["record"]["decision_class"] == "recommended"

    abandoned_record = call_tool(
        server,
        "cruxible_create_decision_record",
        {
            "instance_id": instance_id,
            "question": "Superseded question",
        },
    )
    abandoned = call_tool(
        server,
        "cruxible_abandon_decision_record",
        {
            "instance_id": instance_id,
            "decision_record_id": abandoned_record["record"]["decision_record_id"],
            "reason": "Superseded",
        },
    )
    assert abandoned["record"]["status"] == "abandoned"
    assert abandoned["record"]["abandoned_reason"] == "Superseded"
