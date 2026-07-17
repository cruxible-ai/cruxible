"""MCP read-profile tests: compact is the agent-surface default.

Pins requirement (d) of the output-profiles work item: entity-shaped MCP read
tools default to `profile=compact`, the `CRUXIBLE_MCP_READ_PROFILE` env var
overrides that default, and an explicit `profile="standard"` restores exact
parity with the HTTP-standard serialization chain (the same
`api.*` -> contract-model path the HTTP routes use).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.graph.assertion_state import EntityLifecycleState
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
)
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime import api
from cruxible_core.runtime.instance import CruxibleInstance

ACTOR_CONTEXT = {
    "actor_type": "human_user",
    "actor_id": "operator",
    "org_id": "local",
    "operation_id": "op_profiles",
    "timestamp": "2026-01-01T00:00:00+00:00",
}


def call_tool(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


@pytest.fixture
def server():
    return create_server()


@pytest.fixture
def governed_read_instance_id(tmp_project: Path) -> str:
    """Local instance with a lifecycle-marked entity and a pending edge."""
    instance = CruxibleInstance.init(tmp_project, "config.yaml")
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
            metadata=EntityMetadata(
                lifecycle=EntityLifecycleState(status="superseded"),
                actor_context=ACTOR_CONTEXT,  # type: ignore[arg-type]
            ),
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1", "name": "Brake Pads", "category": "brakes"},
            metadata=EntityMetadata(actor_context=ACTOR_CONTEXT),  # type: ignore[arg-type]
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2",
            properties={"vehicle_id": "V-2", "year": 2024, "make": "Honda", "model": "Accord"},
            metadata=EntityMetadata(actor_context=ACTOR_CONTEXT),  # type: ignore[arg-type]
        )
    )
    pending_edge = RelationshipInstance(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        properties={"verified": True},
    )
    pending_edge.metadata.assertion.review.status = "pending"
    pending_edge.metadata.assertion.review.source = "agent"
    pending_edge.metadata.provenance = RelationshipProvenance(
        source="direct",
        source_ref="add_relationship",
    )
    graph.add_relationship(pending_edge)
    live_edge = RelationshipInstance(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-2",
        properties={"verified": True},
    )
    live_edge.metadata.provenance = RelationshipProvenance(
        source="direct",
        source_ref="add_relationship",
    )
    graph.add_relationship(live_edge)
    # Non-default governance combination for the F-003 integration cases:
    # a RETIRED vehicle behind a rejected + inactive + group_override edge.
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-3",
            properties={"vehicle_id": "V-3", "year": 2019, "make": "Honda", "model": "Insight"},
            metadata=EntityMetadata(
                lifecycle=EntityLifecycleState(status="retired", reason="discontinued"),
                actor_context=ACTOR_CONTEXT,  # type: ignore[arg-type]
            ),
        )
    )
    rejected_edge = RelationshipInstance(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-3",
        properties={"verified": False},
    )
    rejected_edge.metadata.assertion.review.status = "rejected"
    rejected_edge.metadata.assertion.review.source = "human"
    rejected_edge.metadata.assertion.review.updated_by = "reviewer-1"
    rejected_edge.metadata.assertion.lifecycle.status = "inactive"
    rejected_edge.metadata.assertion.lifecycle.reason = "bad fitment"
    rejected_edge.metadata.assertion.group_override = True
    rejected_edge.metadata.provenance = RelationshipProvenance(
        source="direct",
        source_ref="add_relationship",
    )
    graph.add_relationship(rejected_edge)
    instance.save_graph(graph)
    return str(tmp_project)


# Exact compact assertion payload for the rejected/inactive/group_override edge.
REJECTED_EDGE_COMPACT_ASSERTION = {
    "review": {"status": "rejected", "source": "human", "updated_by": "reviewer-1"},
    "lifecycle": {"status": "inactive", "reason": "bad fitment"},
    "group_override": True,
}


class TestMcpDefaultsCompact:
    def test_get_entity_defaults_compact_and_keeps_lifecycle(
        self, server, governed_read_instance_id: str
    ) -> None:
        result = call_tool(
            server,
            "cruxible_get_entity",
            {
                "instance_id": governed_read_instance_id,
                "entity_type": "Vehicle",
                "entity_id": "V-1",
            },
        )
        assert result["found"] is True
        # Governance marker survives; actor_context does not.
        assert result["metadata"] == {"lifecycle": {"status": "superseded"}}
        assert "actor_context" not in json.dumps(result)
        # No display key present: the bounded scalar fallback keeps all four
        # scalar properties (under the N=5 cap), in stored order.
        assert result["properties"] == {
            "vehicle_id": "V-1",
            "year": 2024,
            "make": "Honda",
            "model": "Civic",
        }

    def test_list_edges_defaults_compact_and_keeps_review_marker(
        self, server, governed_read_instance_id: str
    ) -> None:
        result = call_tool(
            server,
            "cruxible_list",
            {
                "instance_id": governed_read_instance_id,
                "resource_type": "edges",
            },
        )
        edge = result["items"][0]
        assert edge["relationship_type"] == "fits"
        assert edge["metadata"]["assertion"]["review"]["status"] == "pending"
        assert "provenance" not in edge["metadata"]
        assert "actor_context" not in json.dumps(result)

    def test_inspect_entity_defaults_compact(self, server, governed_read_instance_id: str) -> None:
        result = call_tool(
            server,
            "cruxible_inspect_entity",
            {
                "instance_id": governed_read_instance_id,
                "entity_type": "Part",
                "entity_id": "BP-1",
            },
        )
        assert result["found"] is True
        assert "actor_context" not in json.dumps(result)
        neighbor = next(
            row
            for row in result["neighbors"]
            if row["metadata"]["assertion"]["review"]["status"] == "pending"
        )
        assert neighbor["entity"]["metadata"] == {"lifecycle": {"status": "superseded"}}


class TestMcpProfileOverrides:
    def test_env_override_restores_standard(
        self, server, governed_read_instance_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CRUXIBLE_MCP_READ_PROFILE", "standard")
        result = call_tool(
            server,
            "cruxible_get_entity",
            {
                "instance_id": governed_read_instance_id,
                "entity_type": "Vehicle",
                "entity_id": "V-1",
            },
        )
        assert result["metadata"]["actor_context"]["actor_id"] == "operator"
        assert result["properties"]["year"] == 2024

    def test_invalid_env_override_is_rejected(
        self, server, governed_read_instance_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CRUXIBLE_MCP_READ_PROFILE", "tiny")
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(
                server.call_tool(
                    "cruxible_get_entity",
                    {
                        "instance_id": governed_read_instance_id,
                        "entity_type": "Vehicle",
                        "entity_id": "V-1",
                    },
                )
            )
        assert "CRUXIBLE_MCP_READ_PROFILE" in str(exc_info.value)

    def test_explicit_standard_matches_http_standard_chain(
        self, server, governed_read_instance_id: str
    ) -> None:
        """profile='standard' per call restores the HTTP-standard payload."""
        mcp_result = call_tool(
            server,
            "cruxible_get_entity",
            {
                "instance_id": governed_read_instance_id,
                "entity_type": "Vehicle",
                "entity_id": "V-1",
                "profile": "standard",
            },
        )
        http_standard = api.get_entity(
            governed_read_instance_id, "Vehicle", "V-1", profile="standard"
        ).model_dump(mode="json")
        assert mcp_result == http_standard
        assert mcp_result["metadata"]["actor_context"]["actor_id"] == "operator"

        mcp_query = call_tool(
            server,
            "cruxible_query",
            {
                "instance_id": governed_read_instance_id,
                "query_name": "vehicles_for_part",
                "params": {"part_number": "BP-1"},
                "profile": "standard",
            },
        )
        http_query = api.query(
            governed_read_instance_id,
            "vehicles_for_part",
            {"part_number": "BP-1"},
            profile="standard",
        ).model_dump(mode="json")
        assert mcp_query["items"] == http_query["items"]

    def test_explicit_compact_query_drops_blobs_keeps_envelope(
        self, server, governed_read_instance_id: str
    ) -> None:
        result = call_tool(
            server,
            "cruxible_query",
            {
                "instance_id": governed_read_instance_id,
                "query_name": "vehicles_for_part",
                "params": {"part_number": "BP-1"},
                "profile": "compact",
            },
        )
        assert result["total"] == 1
        assert result["receipt_id"] is not None
        assert "actor_context" not in json.dumps(result["items"])


class TestNonDefaultGovernanceIntegration:
    """F-003: exact non-default markers survive compact end-to-end.

    One case each for list / inspect / query over the rejected + inactive +
    group_override edge and the retired entity, asserting marker VALUES.
    """

    def test_list_edges_compact_keeps_rejected_inactive_override_markers(
        self, server, governed_read_instance_id: str
    ) -> None:
        result = call_tool(
            server,
            "cruxible_list",
            {
                "instance_id": governed_read_instance_id,
                "resource_type": "edges",
                "profile": "compact",
            },
        )
        rejected = next(item for item in result["items"] if item["to_id"] == "V-3")
        assert rejected["metadata"] == {"assertion": REJECTED_EDGE_COMPACT_ASSERTION}
        assert rejected["properties"] == {"verified": False}

    def test_inspect_compact_keeps_non_default_markers_on_edge_and_entity(
        self, server, governed_read_instance_id: str
    ) -> None:
        result = call_tool(
            server,
            "cruxible_inspect_entity",
            {
                "instance_id": governed_read_instance_id,
                "entity_type": "Part",
                "entity_id": "BP-1",
                "profile": "compact",
            },
        )
        rejected = next(row for row in result["neighbors"] if row["entity"]["entity_id"] == "V-3")
        assert rejected["metadata"] == {"assertion": REJECTED_EDGE_COMPACT_ASSERTION}
        assert rejected["entity"]["metadata"] == {
            "lifecycle": {"status": "retired", "reason": "discontinued"}
        }

    def test_query_inline_compact_keeps_non_default_markers_on_path(
        self, server, governed_read_instance_id: str
    ) -> None:
        result = call_tool(
            server,
            "cruxible_query_inline",
            {
                "instance_id": governed_read_instance_id,
                "definition": {
                    "name": "all_fitments",
                    "mode": "traversal",
                    "entry_point": "Part",
                    "traversal": [{"relationship": "fits", "direction": "outgoing"}],
                    "returns": "list[Vehicle]",
                    "relationship_state": "all",
                },
                "params": {"part_number": "BP-1"},
                "profile": "compact",
            },
        )
        assert result["total"] == 3
        rejected_row = next(row for row in result["items"] if row["result"]["entity_id"] == "V-3")
        assert rejected_row["path"][0]["metadata"] == {"assertion": REJECTED_EDGE_COMPACT_ASSERTION}
        assert rejected_row["result"]["metadata"] == {
            "lifecycle": {"status": "retired", "reason": "discontinued"}
        }
        assert "actor_context" not in json.dumps(result["items"])
