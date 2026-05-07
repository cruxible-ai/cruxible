"""Contract tests for MCP handlers after governed-only public mutation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.mcp.handlers import handle_query
from cruxible_core.mcp.server import create_server
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.runtime.instance import CruxibleInstance


def call_tool(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool and parse the structured JSON result."""
    result = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def call_tool_expect_error(server, name: str, args: dict[str, Any]) -> str:
    """Call a tool expecting failure and return the error text."""
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(server.call_tool(name, args))
    return str(exc_info.value)


@pytest.fixture
def server():
    return create_server()


@pytest.fixture
def dev_graph_instance_id(tmp_project: Path) -> str:
    instance = CruxibleInstance.init(tmp_project, "config.yaml")
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2024-CIVIC-EX",
            properties={
                "vehicle_id": "V-2024-CIVIC-EX",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2024-ACCORD-SPORT",
            properties={
                "vehicle_id": "V-2024-ACCORD-SPORT",
                "year": 2024,
                "make": "Honda",
                "model": "Accord",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1001",
            properties={"part_number": "BP-1001", "name": "Brake Pads", "category": "brakes"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1002",
            properties={"part_number": "BP-1002", "name": "Rotor", "category": "brakes"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            from_type="Part",
            from_id="BP-1001",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
            properties={"verified": True},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            from_type="Part",
            from_id="BP-1002",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
            properties={"verified": True},
        )
    )
    instance.save_graph(graph)
    return str(tmp_project)


@pytest.fixture
def workflow_instance_id(canonical_workflow_project: Path) -> str:
    CruxibleInstance.init(canonical_workflow_project, "config.yaml")
    return str(canonical_workflow_project)


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("cruxible_init", {"root_dir": "/tmp/project", "config_yaml": "name: demo"}),
        (
            "cruxible_world_create_overlay",
            {"transport_ref": "file:///tmp/release", "root_dir": "/tmp/overlay"},
        ),
        (
            "cruxible_run_workflow",
            {"instance_id": "inst_123", "workflow_name": "wf", "input_payload": {}},
        ),
        ("cruxible_add_entity", {"instance_id": "inst_123", "entities": []}),
        (
            "cruxible_feedback",
            {
                "instance_id": "inst_123",
                "receipt_id": "RCP-1",
                "action": "approve",
                "source": "human",
                "from_type": "Part",
                "from_id": "BP-1",
                "relationship": "fits",
                "to_type": "Vehicle",
                "to_id": "V-1",
            },
        ),
        (
            "cruxible_outcome",
            {"instance_id": "inst_123", "receipt_id": "RCP-1", "outcome": "correct"},
        ),
    ],
)
def test_mutating_tools_require_server(server, tool_name: str, args: dict[str, Any]) -> None:
    error = call_tool_expect_error(server, tool_name, args)
    assert "configure a server" in error.lower()


def test_validate_valid_config(server, tmp_project: Path) -> None:
    result = call_tool(
        server, "cruxible_validate", {"config_path": str(tmp_project / "config.yaml")},
    )
    assert result["valid"] is True
    assert result["name"] == "car_parts_compatibility"
    assert "Vehicle" in result["entity_types"]
    assert "fits" in result["relationships"]


def test_validate_bad_path(server) -> None:
    error = call_tool_expect_error(
        server,
        "cruxible_validate",
        {"config_path": "/no/such/file.yaml"},
    )
    assert "file.yaml" in error


def test_query_and_receipt_work_locally_for_seeded_dev_instance(
    server,
    dev_graph_instance_id: str,
) -> None:
    query = call_tool(
        server,
        "cruxible_query",
        {
            "instance_id": dev_graph_instance_id,
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
        },
    )
    assert query["total_results"] == 2
    assert query["receipt_id"].startswith("RCP-")
    assert query["receipt"] is not None

    receipt = call_tool(
        server,
        "cruxible_receipt",
        {
            "instance_id": dev_graph_instance_id,
            "receipt_id": query["receipt_id"],
        },
    )
    assert receipt["receipt_id"] == query["receipt_id"]
    assert receipt["query_name"] == "parts_for_vehicle"


def test_trace_tools_work_locally_for_dev_instance(
    server,
    dev_graph_instance_id: str,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = ExecutionTrace(
        trace_id="TRC-mcp-001",
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
        provider_ref="tests.support.workflow_test_providers.provider",
        runtime="python",
        deterministic=True,
        side_effects=False,
        input_payload={"input": True},
        output_payload={"rows": 4},
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0.0,
    )
    instance = CruxibleInstance.load(Path(dev_graph_instance_id))
    store = instance.get_receipt_store()
    try:
        store.save_trace(trace)
    finally:
        store.close()

    fetched = call_tool(
        server,
        "cruxible_get_trace",
        {"instance_id": dev_graph_instance_id, "trace_id": trace.trace_id},
    )
    listed = call_tool(
        server,
        "cruxible_list_traces",
        {"instance_id": dev_graph_instance_id, "workflow_name": "wf"},
    )

    assert fetched["output_payload"]["rows"] == 4
    assert listed["traces"][0]["trace_id"] == trace.trace_id


def test_list_sample_schema_and_getters_work_locally_for_dev_instance(
    server,
    dev_graph_instance_id: str,
) -> None:
    listed = call_tool(
        server,
        "cruxible_list",
        {
            "instance_id": dev_graph_instance_id,
            "resource_type": "entities",
            "entity_type": "Vehicle",
        },
    )
    assert listed["total"] == 2

    sample = call_tool(
        server,
        "cruxible_sample",
        {"instance_id": dev_graph_instance_id, "entity_type": "Part"},
    )
    assert sample["count"] == 2

    entity = call_tool(
        server,
        "cruxible_get_entity",
        {
            "instance_id": dev_graph_instance_id,
            "entity_type": "Vehicle",
            "entity_id": "V-2024-CIVIC-EX",
        },
    )
    assert entity["properties"]["make"] == "Honda"

    relationship = call_tool(
        server,
        "cruxible_get_relationship",
        {
            "instance_id": dev_graph_instance_id,
            "from_type": "Part",
            "from_id": "BP-1001",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-CIVIC-EX",
        },
    )
    assert relationship["properties"]["verified"] is True

    schema = call_tool(server, "cruxible_schema", {"instance_id": dev_graph_instance_id})
    assert schema["name"] == "car_parts_compatibility"


def test_evaluate_works_locally_for_dev_instance(
    server,
    dev_graph_instance_id: str,
) -> None:
    evaluation = call_tool(
        server,
        "cruxible_evaluate",
        {"instance_id": dev_graph_instance_id},
    )
    assert evaluation["entity_count"] > 0
    assert evaluation["edge_count"] > 0
    assert isinstance(evaluation["findings"], list)


def test_workflow_lock_and_plan_stay_local_safe(
    server,
    workflow_instance_id: str,
) -> None:
    lock_result = call_tool(server, "cruxible_lock_workflow", {"instance_id": workflow_instance_id})
    assert lock_result["providers_locked"] == 1
    assert lock_result["artifacts_locked"] == 1

    plan_result = call_tool(
        server,
        "cruxible_plan_workflow",
        {
            "instance_id": workflow_instance_id,
            "workflow_name": "build_reference",
            "input_payload": {},
        },
    )
    assert plan_result["plan"]["workflow"] == "build_reference"


def test_query_limit_validation_raises(dev_graph_instance_id: str) -> None:
    with pytest.raises(ConfigError, match="limit must be a positive integer"):
        handle_query(
            dev_graph_instance_id,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
            limit=0,
        )
