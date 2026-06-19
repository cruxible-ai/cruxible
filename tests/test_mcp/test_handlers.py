"""Contract tests for MCP handlers after governed-only public mutation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_client import contracts
from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.mcp.handlers import (
    handle_add_relationship,
    handle_batch_direct_write,
    handle_query,
    handle_query_inline,
)
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


def test_handle_add_relationship_preserves_evidence_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def add_relationships(self, instance_id, relationships, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["relationships"] = relationships
            captured["dry_run"] = dry_run
            return contracts.AddRelationshipResult(
                added=1,
                updated=0,
                receipt_id="RCP-add",
            )

    monkeypatch.setattr("cruxible_core.mcp.handlers._get_client", lambda: StubClient())
    result = handle_add_relationship(
        "inst_123",
        [
            contracts.RelationshipInput(
                from_type="Part",
                from_id="BP-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-1",
                pending=True,
                evidence_refs=[
                    contracts.EvidenceRef(
                        source="roadmap_doc",
                        source_record_id="section-p0",
                    )
                ],
                source_evidence=[
                    contracts.SourceEvidenceInput(
                        source_artifact_id="SRC-1",
                        chunk_id="CHK-1",
                    )
                ],
                evidence_rationale="Accepted direct source-backed assertion.",
            )
        ],
    )

    assert result.added == 1
    assert captured["instance_id"] == "inst_123"
    relationships = captured["relationships"]
    assert isinstance(relationships, list)
    relationship = relationships[0]
    assert isinstance(relationship, contracts.RelationshipInput)
    assert relationship.pending is True
    assert relationship.evidence_refs[0].source == "roadmap_doc"
    assert relationship.source_evidence[0].source_artifact_id == "SRC-1"
    assert relationship.evidence_rationale == "Accepted direct source-backed assertion."


def test_handle_batch_direct_write_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=1,
            )

    monkeypatch.setattr("cruxible_core.mcp.handlers._get_client", lambda: StubClient())
    payload = contracts.BatchDirectWritePayload(
        entities=[
            contracts.EntityInput(
                entity_type="Vehicle",
                entity_id="V-BATCH",
            )
        ]
    )

    result = handle_batch_direct_write("inst_123", payload, dry_run=True)

    assert result.dry_run is True
    assert captured["instance_id"] == "inst_123"
    assert captured["payload"] == payload
    assert captured["dry_run"] is True


def test_handle_query_inline_preserves_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def query_inline(
            self,
            instance_id,
            definition,
            params,
            *,
            limit=None,
            relationship_state=None,
            decision_record_id=None,
        ):
            captured["instance_id"] = instance_id
            captured["definition"] = definition
            captured["params"] = params
            captured["limit"] = limit
            captured["relationship_state"] = relationship_state
            captured["decision_record_id"] = decision_record_id
            return contracts.QueryToolResult(
                items=[],
                receipt_id="RCP-inline",
                receipt=None,
                total=0,
                limit=50,
                truncated=False,
                steps_executed=0,
            )

    definition = contracts.InlineQueryDefinition(
        name="brake_parts",
        mode="collection",
        returns="Part",
        result_shape="entity",
    )

    monkeypatch.setattr("cruxible_core.mcp.handlers._get_client", lambda: StubClient())
    result = handle_query_inline(
        "inst_123",
        definition,
        {"category": "brakes"},
        limit=10,
        relationship_state="reviewable",
        decision_record_id="DR-1",
    )

    assert result.receipt_id == "RCP-inline"
    assert captured == {
        "instance_id": "inst_123",
        "definition": definition,
        "params": {"category": "brakes"},
        "limit": 10,
        "relationship_state": "reviewable",
        "decision_record_id": "DR-1",
    }


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
            "cruxible_state_create_overlay",
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
                "action": "approve",
                "source": "human",
                "from_type": "Part",
                "from_id": "BP-1",
                "relationship_type": "fits",
                "to_type": "Vehicle",
                "to_id": "V-1",
            },
        ),
        (
            "cruxible_feedback_from_query",
            {
                "instance_id": "inst_123",
                "receipt_id": "RCP-1",
                "result_index": 0,
                "action": "approve",
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
        server,
        "cruxible_validate",
        {"config_path": str(tmp_project / "config.yaml")},
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
    assert query["total"] == 2
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
    with instance.write_transaction() as uow:
        uow.receipts.save_trace(trace)

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
    assert listed["items"][0]["trace_id"] == trace.trace_id

    large_payload = {"body": "x" * 40000}
    large_trace = ExecutionTrace(
        trace_id="TRC-mcp-large",
        workflow_name="wf",
        step_id="large",
        provider_name="provider",
        provider_version="1.0.0",
        provider_ref="tests.support.workflow_test_providers.provider",
        runtime="python",
        deterministic=True,
        side_effects=False,
        input_payload=large_payload,
        output_payload=large_payload,
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0.0,
    )
    with instance.write_transaction() as uow:
        uow.receipts.save_trace(large_trace)

    preview = call_tool(
        server,
        "cruxible_get_trace",
        {"instance_id": dev_graph_instance_id, "trace_id": large_trace.trace_id},
    )
    assert preview["input_payload"] != large_payload
    assert preview["input_payload_metadata"]["retention"] == "preview"
    assert preview["input_payload_metadata"]["stored_inline"] is False


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
            "fields": ["make"],
        },
    )
    assert listed["total"] == 2
    assert listed["items"][0]["properties"] == {"make": "Honda"}

    list_error = call_tool_expect_error(
        server,
        "cruxible_list",
        {
            "instance_id": dev_graph_instance_id,
            "resource_type": "entities",
            "entity_type": "TypoType",
        },
    )
    assert "Entity type 'TypoType' not found in schema" in list_error
    assert "Known entity types: Part, Vehicle" in list_error

    sample = call_tool(
        server,
        "cruxible_sample",
        {"instance_id": dev_graph_instance_id, "entity_type": "Part", "fields": ["name"]},
    )
    assert sample["total"] == 2
    assert sample["items"][0]["properties"] == {"name": "Brake Pads"}

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


def test_list_and_sample_raise_tool_error_for_unknown_entity_type(
    server,
    dev_graph_instance_id: str,
) -> None:
    for tool_name, args in [
        (
            "cruxible_list",
            {
                "instance_id": dev_graph_instance_id,
                "resource_type": "entities",
                "entity_type": "TypoType",
            },
        ),
        (
            "cruxible_sample",
            {
                "instance_id": dev_graph_instance_id,
                "entity_type": "TypoType",
            },
        ),
    ]:
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(server.call_tool(tool_name, args))

        error = str(exc_info.value)
        assert "Entity type 'TypoType' not found in schema" in error
        assert "Known entity types: Part, Vehicle" in error


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


def test_list_tool_pages_with_offset(
    server,
    dev_graph_instance_id: str,
) -> None:
    page1 = call_tool(
        server,
        "cruxible_list",
        {
            "instance_id": dev_graph_instance_id,
            "resource_type": "entities",
            "entity_type": "Vehicle",
            "limit": 1,
            "offset": 0,
        },
    )
    page2 = call_tool(
        server,
        "cruxible_list",
        {
            "instance_id": dev_graph_instance_id,
            "resource_type": "entities",
            "entity_type": "Vehicle",
            "limit": 1,
            "offset": 1,
        },
    )

    assert page1["total"] == 2
    assert page1["truncated"] is True
    assert page2["truncated"] is False
    ids = {page1["items"][0]["entity_id"], page2["items"][0]["entity_id"]}
    assert ids == {"V-2024-CIVIC-EX", "V-2024-ACCORD-SPORT"}


def test_query_tool_pages_with_offset(
    server,
    dev_graph_instance_id: str,
) -> None:
    full = call_tool(
        server,
        "cruxible_query",
        {
            "instance_id": dev_graph_instance_id,
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
        },
    )
    page1 = call_tool(
        server,
        "cruxible_query",
        {
            "instance_id": dev_graph_instance_id,
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
            "limit": 1,
            "offset": 0,
        },
    )
    page2 = call_tool(
        server,
        "cruxible_query",
        {
            "instance_id": dev_graph_instance_id,
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
            "limit": 1,
            "offset": 1,
        },
    )

    assert full["total"] == 2
    assert page1["offset"] == 0
    assert page1["truncated"] is True
    assert page2["offset"] == 1
    assert page2["truncated"] is False
    assert [page1["items"][0], page2["items"][0]] == full["items"]
