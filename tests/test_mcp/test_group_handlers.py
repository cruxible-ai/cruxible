"""Tests for MCP group write tools (propose, resolve, update_trust_status)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.mcp.server import create_server

GROUP_CONFIG_YAML = """\
version: "1.0"
name: group_mcp_tests
description: For MCP group tool tests

integrations:
  check_v1:
    kind: generic
    contract: null

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]
      price:
        type: float
        optional: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
      source:
        type: string
        optional: true
    matching:
      integrations:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only

named_queries:
  parts_for_vehicle:
    description: Find parts for vehicle
    entry_point: Vehicle
    traversal:
      - relationship: fits
        direction: incoming
    returns: "list[Part]"

constraints: []
ingestion: {}
"""


def call_tool(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool and parse the JSON result."""
    result = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):
        return result[1]
    text = result[0].text
    return json.loads(text)


def call_tool_expect_error(server, name: str, args: dict[str, Any]) -> str:
    """Call a tool expecting failure. Returns the error message."""
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(server.call_tool(name, args))
    return str(exc_info.value)


@pytest.fixture
def server(governed_client):
    del governed_client
    return create_server()


@pytest.fixture
def group_project(tmp_path):
    """Create a project with matching config + seeded entities."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(GROUP_CONFIG_YAML)
    return tmp_path


@pytest.fixture
def tuple_identity_group_project(tmp_path):
    """Create a project where fits uses relationship tuple proposal identity."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        GROUP_CONFIG_YAML.replace(
            "    matching:\n",
            "    proposal_identity: relationship_tuple\n    matching:\n",
            1,
        )
    )
    return tmp_path


def _init_group_instance(server, project):
    """Initialize instance and add entities for group tests."""
    result = call_tool(
        server,
        "cruxible_init",
        {"root_dir": str(project), "config_path": "config.yaml"},
    )
    iid = result["instance_id"]
    # Add entities
    call_tool(
        server,
        "cruxible_add_entity",
        {
            "instance_id": iid,
            "entities": [
                {
                    "entity_type": "Part",
                    "entity_id": "BP-1",
                    "properties": {
                        "part_number": "BP-1",
                        "name": "Pads",
                        "category": "brakes",
                    },
                },
                {
                    "entity_type": "Part",
                    "entity_id": "BP-2",
                    "properties": {
                        "part_number": "BP-2",
                        "name": "Pads 2",
                        "category": "brakes",
                    },
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
                {
                    "entity_type": "Vehicle",
                    "entity_id": "V-2",
                    "properties": {
                        "vehicle_id": "V-2",
                        "year": 2024,
                        "make": "Toyota",
                        "model": "Camry",
                    },
                },
            ],
        },
    )
    return iid


@pytest.fixture
def instance_id(server, group_project):
    return _init_group_instance(server, group_project)


@pytest.fixture
def tuple_identity_instance_id(server, tuple_identity_group_project):
    return _init_group_instance(server, tuple_identity_group_project)


def _member(from_id="BP-1", to_id="V-1"):
    return {
        "from_type": "Part",
        "from_id": from_id,
        "to_type": "Vehicle",
        "to_id": to_id,
        "relationship_type": "fits",
        "signals": [{"integration": "check_v1", "signal": "support"}],
    }


class TestProposeGroup:
    def test_propose_basic(self, server, instance_id):
        result = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"k": "v"},
            },
        )
        assert result["group_id"].startswith("GRP-")
        assert result["status"] == "pending_review"
        assert result["member_count"] == 1
        assert result["signature"]

    def test_tuple_suppression_surfaces_conflicts(self, server, tuple_identity_instance_id):
        first = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": tuple_identity_instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"bucket": "first"},
            },
        )
        second = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": tuple_identity_instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"bucket": "second"},
            },
        )

        assert second["status"] == "suppressed"
        assert second["group_id"] is None
        assert second["suppressed_members"] == [
            {
                "relationship_type": "fits",
                "from_type": "Part",
                "from_id": "BP-1",
                "to_type": "Vehicle",
                "to_id": "V-1",
                "reason": "pending_proposal",
                "existing_group_id": first["group_id"],
                "existing_group_status": "pending_review",
                "existing_signature": first["signature"],
                "source_workflow_name": None,
            }
        ]

    def test_propose_with_thesis(self, server, instance_id):
        result = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member()],
                "thesis_text": "these fit because they're brake pads",
                "thesis_facts": {"category": "brakes"},
                "analysis_state": {"centroid": [0.1, 0.2]},
            },
        )
        assert result["status"] == "pending_review"
        assert result["review_priority"] == "review"

    def test_propose_invalid_relationship(self, server, instance_id):
        error = call_tool_expect_error(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "nonexistent",
                "members": [_member()],
                "thesis_facts": {"k": "v"},
            },
        )
        assert "nonexistent" in error


class TestResolveGroup:
    def test_approve_creates_edges(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"k": "v"},
            },
        )
        result = call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": pr["group_id"],
                "action": "approve",
                "expected_pending_version": 1,
            },
        )
        assert result["action"] == "approve"
        assert result["edges_created"] == 1
        assert result["edges_skipped"] == 0

    def test_reject_no_edges(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"k": "v"},
            },
        )
        result = call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": pr["group_id"],
                "action": "reject",
                "expected_pending_version": 1,
            },
        )
        assert result["action"] == "reject"
        assert result["edges_created"] == 0

    def test_resolve_not_found(self, server, instance_id):
        error = call_tool_expect_error(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": "GRP-nonexistent",
                "action": "approve",
                "expected_pending_version": 1,
            },
        )
        assert "GRP-nonexistent" in error


class TestUpdateTrustStatus:
    def test_promote_to_trusted(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"k": "v"},
            },
        )
        call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": pr["group_id"],
                "action": "approve",
                "expected_pending_version": 1,
            },
        )
        # Get resolution_id from the group
        from cruxible_core.runtime.instance_manager import get_manager

        inst = get_manager().get(instance_id)
        store = inst.get_group_store()
        try:
            group = store.get_group(pr["group_id"])
            res_id = group.resolution_id
        finally:
            store.close()

        result = call_tool(
            server,
            "cruxible_update_trust_status",
            {
                "instance_id": instance_id,
                "resolution_id": res_id,
                "trust_status": "trusted",
                "reason": "reviewed",
            },
        )
        assert result["resolution_id"] == res_id
        assert result["trust_status"] == "trusted"

    def test_invalid_trust_status(self, server, instance_id):
        error = call_tool_expect_error(
            server,
            "cruxible_update_trust_status",
            {
                "instance_id": instance_id,
                "resolution_id": "RES-whatever",
                "trust_status": "bogus",
            },
        )
        # FastMCP validates the Literal type
        assert error


class TestGroupPermissions:
    def test_propose_requires_governed_write(self, server, instance_id, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        error = call_tool_expect_error(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member()],
                "thesis_facts": {"k": "v"},
            },
        )
        assert "GOVERNED_WRITE" in error

    def test_resolve_requires_graph_write(self, server, instance_id, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        error = call_tool_expect_error(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": "GRP-xxx",
                "action": "approve",
                "expected_pending_version": 1,
            },
        )
        assert "GRAPH_WRITE" in error

    def test_update_trust_requires_graph_write(self, server, instance_id, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        error = call_tool_expect_error(
            server,
            "cruxible_update_trust_status",
            {
                "instance_id": instance_id,
                "resolution_id": "RES-xxx",
                "trust_status": "trusted",
            },
        )
        assert "GRAPH_WRITE" in error


class TestGetGroup:
    def test_get_group_returns_members(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1"), _member("BP-2", "V-2")],
                "thesis_text": "brake pads",
                "thesis_facts": {"k": "v"},
            },
        )
        result = call_tool(
            server,
            "cruxible_get_group",
            {"instance_id": instance_id, "group_id": pr["group_id"]},
        )
        assert result["group"]["group_id"] == pr["group_id"]
        assert len(result["members"]) == 2
        assert result["group"]["thesis_text"] == "brake pads"
        assert result["bucket_status"]["pending_group_id"] == pr["group_id"]
        assert len(result["member_review"]) == 2
        assert result["member_review"][0]["current_edge_count"] == 0

    def test_get_group_not_found(self, server, instance_id):
        error = call_tool_expect_error(
            server,
            "cruxible_get_group",
            {"instance_id": instance_id, "group_id": "GRP-nonexistent"},
        )
        assert "GRP-nonexistent" in error


class TestListGroups:
    def test_list_all(self, server, instance_id):
        call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"a": 1},
            },
        )
        call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-2", "V-2")],
                "thesis_facts": {"a": 2},
            },
        )
        result = call_tool(
            server,
            "cruxible_list_groups",
            {"instance_id": instance_id},
        )
        assert result["total"] == 2
        assert len(result["groups"]) == 2

    def test_filter_by_status(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"a": 1},
            },
        )
        call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": pr["group_id"],
                "action": "reject",
                "expected_pending_version": 1,
            },
        )
        pending = call_tool(
            server,
            "cruxible_list_groups",
            {"instance_id": instance_id, "status": "pending_review"},
        )
        assert pending["total"] == 0
        resolved = call_tool(
            server,
            "cruxible_list_groups",
            {"instance_id": instance_id, "status": "resolved"},
        )
        assert resolved["total"] == 1


class TestListResolutions:
    def test_list_resolutions(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"k": "v"},
                "analysis_state": {"centroid": [0.1]},
            },
        )
        call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": pr["group_id"],
                "action": "approve",
                "rationale": "looks good",
                "expected_pending_version": 1,
            },
        )
        result = call_tool(
            server,
            "cruxible_list_resolutions",
            {"instance_id": instance_id},
        )
        assert result["total"] == 1
        r = result["resolutions"][0]
        assert r["analysis_state"] == {"centroid": [0.1]}
        assert r["thesis_facts"] == {"k": "v"}
        assert r["trust_status"] == "watch"

    def test_filter_by_action(self, server, instance_id):
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_facts": {"k": "v"},
            },
        )
        call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": pr["group_id"],
                "action": "reject",
                "expected_pending_version": 1,
            },
        )
        approvals = call_tool(
            server,
            "cruxible_list_resolutions",
            {"instance_id": instance_id, "action": "approve"},
        )
        assert approvals["total"] == 0
        rejects = call_tool(
            server,
            "cruxible_list_resolutions",
            {"instance_id": instance_id, "action": "reject"},
        )
        assert rejects["total"] == 1


class TestGroupStatus:
    def test_status_by_group_and_signature(self, server, instance_id):
        proposed = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member("BP-1", "V-1")],
                "thesis_text": "brake pad fit",
                "thesis_facts": {"rule_id": "fit_rule", "rule_version": 1},
            },
        )
        signature = proposed["signature"]
        group_result = call_tool(
            server,
            "cruxible_group_status",
            {"instance_id": instance_id, "group_id": proposed["group_id"]},
        )
        assert group_result["signature"] == signature
        assert group_result["pending_group_id"] == proposed["group_id"]
        assert group_result["pending_version"] == 1
        assert group_result["pending_delta_count"] == 1
        assert group_result["accepted_tuple_count"] == 0
        assert group_result["approved_history"] == []

        call_tool(
            server,
            "cruxible_resolve_group",
            {
                "instance_id": instance_id,
                "group_id": proposed["group_id"],
                "action": "approve",
                "expected_pending_version": 1,
            },
        )
        signature_result = call_tool(
            server,
            "cruxible_group_status",
            {"instance_id": instance_id, "signature": signature},
        )
        assert signature_result["signature"] == signature
        assert signature_result["pending_group_id"] is None
        assert signature_result["pending_version"] is None
        assert signature_result["pending_delta_count"] == 0
        assert signature_result["accepted_tuple_count"] == 1
        assert signature_result["latest_trust_status"] == "watch"
        assert signature_result["latest_approved_resolution_id"]
        assert len(signature_result["approved_history"]) == 1


class TestReadPermissions:
    def test_get_group_read_only(self, server, instance_id, monkeypatch):
        """Read-only mode should allow get_group."""
        pr = call_tool(
            server,
            "cruxible_propose_group",
            {
                "instance_id": instance_id,
                "relationship_type": "fits",
                "members": [_member()],
                "thesis_facts": {"k": "v"},
            },
        )
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        result = call_tool(
            server,
            "cruxible_get_group",
            {"instance_id": instance_id, "group_id": pr["group_id"]},
        )
        assert result["group"]["group_id"] == pr["group_id"]

    def test_list_groups_read_only(self, server, instance_id, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        result = call_tool(
            server,
            "cruxible_list_groups",
            {"instance_id": instance_id},
        )
        assert result["total"] == 0

    def test_list_resolutions_read_only(self, server, instance_id, monkeypatch):
        monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
        reset_permissions()
        result = call_tool(
            server,
            "cruxible_list_resolutions",
            {"instance_id": instance_id},
        )
        assert result["total"] == 0


class TestFeedbackGroupOverride:
    def test_feedback_with_group_override(self, server, instance_id):
        """group_override=True via MCP stamps edge property."""
        # Add edge first
        call_tool(
            server,
            "cruxible_add_relationship",
            {
                "instance_id": instance_id,
                "relationships": [
                    {
                        "from_type": "Part",
                        "from_id": "BP-1",
                        "relationship": "fits",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                        "properties": {"verified": True},
                    }
                ],
            },
        )
        # Query to get receipt
        query_result = call_tool(
            server,
            "cruxible_query",
            {
                "instance_id": instance_id,
                "query_name": "parts_for_vehicle",
                "params": {"vehicle_id": "V-1"},
            },
        )
        # Feedback with group_override needs a named query that returns the edge
        # Use a simple config — the query filters on verified=true so BP-1→V-1 should appear
        receipt_id = query_result["receipt_id"]
        result = call_tool(
            server,
            "cruxible_feedback",
            {
                "instance_id": instance_id,
                "receipt_id": receipt_id,
                "action": "approve",
                "source": "human",
                "from_type": "Part",
                "from_id": "BP-1",
                "relationship": "fits",
                "to_type": "Vehicle",
                "to_id": "V-1",
                "group_override": True,
            },
        )
        assert result["applied"] is True

        # Verify the edge has group_override property
        edge = call_tool(
            server,
            "cruxible_get_relationship",
            {
                "instance_id": instance_id,
                "from_type": "Part",
                "from_id": "BP-1",
                "relationship_type": "fits",
                "to_type": "Vehicle",
                "to_id": "V-1",
            },
        )
        assert edge["properties"].get("group_override") is True
