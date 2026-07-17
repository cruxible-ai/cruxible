"""HTTP output-profile tests: standard stays bit-identical, compact trims.

Freeze-guard intent: the `profile` query/body param is ADDITIVE. Requests that
do not ask for a profile get exactly the pre-profile payload (pinned here
snapshot-style with volatile fields normalized), and `profile=standard` is
byte-identical to omitting the parameter on every surfaced route.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    yield TestClient(create_app())
    get_manager().clear()
    reset_registry()


@pytest.fixture
def seeded_instance(app_client: TestClient, tmp_path: Path) -> str:
    root = tmp_path / "project"
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    response = app_client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CAR_PARTS_YAML},
    )
    assert response.status_code == 200
    instance_id = response.json()["instance_id"]

    seeded = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
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
                        "make": "Honda",
                        "model": "Accord",
                    },
                },
                {
                    "entity_type": "Part",
                    "entity_id": "BP-1",
                    "properties": {
                        "part_number": "BP-1",
                        "name": "Ceramic Brake Pads",
                        "category": "brakes",
                        "price": 49.99,
                    },
                },
            ]
        },
    )
    assert seeded.status_code == 200

    # One live governed edge so live-state queries return rows.
    live = app_client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-2",
                    "properties": {"verified": True},
                }
            ]
        },
    )
    assert live.status_code == 200

    # A reviewable-state (pending) governed edge: the norm for governed
    # overlays and the pilot's read path.
    pending = app_client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True},
                    "pending": True,
                }
            ]
        },
    )
    assert pending.status_code == 200
    return instance_id


# Volatile LEAF keys only: normalization never replaces a whole subdocument, so
# every structural key (including the full receipt tree) stays pinned.
VOLATILE_LEAF_KEYS = {
    "timestamp",
    "operation_id",
    "created_at",
    "updated_at",
    "last_modified_at",
    "receipt_id",
    "duration_ms",
    "head_snapshot_id",
    # wi-read-revision-and-continuation: monotonic state revision — value is
    # the fixture's mutation count, structurally pinned but value-normalized.
    "read_revision",
}


def _normalized(payload: object) -> object:
    """Normalize volatile leaf values (ids/timestamps/durations) for pinning."""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key in VOLATILE_LEAF_KEYS and not isinstance(value, (dict, list)):
                out[key] = "<varies>" if value is not None else None
            else:
                out[key] = _normalized(value)
        return out
    if isinstance(payload, list):
        return [_normalized(item) for item in payload]
    return payload


NORMALIZED_ACTOR_CONTEXT = {
    "actor_type": "human_user",
    "actor_id": "operator",
    "org_id": "local",
    "operation_id": "<varies>",
    "timestamp": "<varies>",
}
# Non-trimmed (mode="json" without exclude_none) actor-context form used on
# query path segments and receipts.
NORMALIZED_ACTOR_CONTEXT_FULL = {**NORMALIZED_ACTOR_CONTEXT, "request_id": None}

PART_PROPERTIES = {
    "category": "brakes",
    "name": "Ceramic Brake Pads",
    "price": 49.99,
    "part_number": "BP-1",
}
V1_PROPERTIES = {"make": "Honda", "model": "Civic", "year": 2024, "vehicle_id": "V-1"}
V2_PROPERTIES = {"make": "Honda", "model": "Accord", "year": 2024, "vehicle_id": "V-2"}


def _standard_entity(entity_type: str, entity_id: str, properties: dict) -> dict:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "properties": properties,
        "metadata": {"actor_context": NORMALIZED_ACTOR_CONTEXT},
    }


PART_STANDARD = _standard_entity("Part", "BP-1", PART_PROPERTIES)
V1_STANDARD = _standard_entity("Vehicle", "V-1", V1_PROPERTIES)
V2_STANDARD = _standard_entity("Vehicle", "V-2", V2_PROPERTIES)

# exclude_none serialization (list edges / inspect neighbors).
EDGE_PROVENANCE_TRIMMED = {
    "source": "http_api",
    "source_ref": "add_relationship",
    "created_at": "<varies>",
    "receipt_id": "<varies>",
    "created_actor_context": NORMALIZED_ACTOR_CONTEXT,
}
PENDING_ASSERTION_TRIMMED = {
    "review": {
        "status": "pending",
        "source": "agent",
        "updated_at": "<varies>",
        "updated_by": "relationship:add_pending",
        "actor_context": NORMALIZED_ACTOR_CONTEXT,
    },
    "lifecycle": {"status": "active"},
    "group_override": False,
}
UNREVIEWED_ASSERTION_TRIMMED = {
    "review": {"status": "unreviewed", "source": "system"},
    "lifecycle": {"status": "active"},
    "group_override": False,
}

# Full serialization (query path segments carry None subfields).
EDGE_PROVENANCE_FULL = {
    "source": "http_api",
    "source_ref": "add_relationship",
    "created_at": "<varies>",
    "last_modified_at": None,
    "last_modified_by": None,
    "receipt_id": "<varies>",
    "resolution_id": None,
    "clone_origin": None,
    "created_actor_context": NORMALIZED_ACTOR_CONTEXT_FULL,
    "last_modified_actor_context": None,
}
UNREVIEWED_ASSERTION_FULL = {
    "review": {
        "status": "unreviewed",
        "source": "system",
        "updated_at": None,
        "updated_by": None,
        "actor_context": None,
    },
    "lifecycle": {
        "status": "active",
        "reason": None,
        "effective_from": None,
        "effective_until": None,
        "closed_at": None,
        "closed_by": None,
        "supersedes": None,
        "superseded_by": None,
    },
    "group_override": False,
}

# Deliberate wi-read-revision-and-continuation envelope extension:
# read_revision on every list envelope; continuation_token on ListResult
# (present iff truncated and resumable).
LIST_ENTITIES_STANDARD = {
    "total": 1,
    "limit": 50,
    "offset": 0,
    "truncated": False,
    "read_revision": "<varies>",
    "items": [PART_STANDARD],
    "continuation_token": None,
}

# Live edge (V-2) seeded first (edge_key 0), pending edge (V-1) second
# (edge_key 1); list edges sorts by endpoints so V-1 leads.
LIST_EDGES_STANDARD = {
    "total": 2,
    "limit": 50,
    "offset": 0,
    "truncated": False,
    "read_revision": "<varies>",
    "items": [
        {
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-1",
            "relationship_type": "fits",
            "edge_key": 1,
            "properties": {"verified": True},
            "metadata": {
                "provenance": EDGE_PROVENANCE_TRIMMED,
                "assertion": PENDING_ASSERTION_TRIMMED,
            },
        },
        {
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-2",
            "relationship_type": "fits",
            "edge_key": 0,
            "properties": {"verified": True},
            "metadata": {
                "provenance": EDGE_PROVENANCE_TRIMMED,
                "assertion": UNREVIEWED_ASSERTION_TRIMMED,
            },
        },
    ],
    "continuation_token": None,
}

SAMPLE_STANDARD = {
    "total": 1,
    "limit": 5,
    "offset": 0,
    "truncated": False,
    "read_revision": "<varies>",
    "items": [PART_STANDARD],
    "entity_type": "Part",
}

# Inspect neighbors follow edge insertion order: live V-2 first, pending V-1.
INSPECT_STANDARD = {
    "found": True,
    "entity_type": "Part",
    "entity_id": "BP-1",
    "properties": PART_PROPERTIES,
    "metadata": {"actor_context": NORMALIZED_ACTOR_CONTEXT},
    "neighbors": [
        {
            "direction": "outgoing",
            "relationship_type": "fits",
            "edge_key": 0,
            "properties": {"verified": True},
            "metadata": {
                "provenance": EDGE_PROVENANCE_TRIMMED,
                "assertion": UNREVIEWED_ASSERTION_TRIMMED,
            },
            "entity": V2_STANDARD,
        },
        {
            "direction": "outgoing",
            "relationship_type": "fits",
            "edge_key": 1,
            "properties": {"verified": True},
            "metadata": {
                "provenance": EDGE_PROVENANCE_TRIMMED,
                "assertion": PENDING_ASSERTION_TRIMMED,
            },
            "entity": V1_STANDARD,
        },
    ],
    "total_neighbors": 2,
    "read_revision": "<varies>",
}

QUERY_EXECUTION_OPTIONS = {
    "relationship_state": "live",
    "relationship_state_source": "query_config",
    "result_shape": "path",
    "dedupe": "path",
}

# The live path row (Part BP-1 -[fits]-> Vehicle V-2); shared verbatim by
# `items` and the receipt's `results`.
QUERY_PATH_ROW_STANDARD = {
    "entry": PART_STANDARD,
    "result": V2_STANDARD,
    "entities": [PART_STANDARD, V2_STANDARD],
    "path": [
        {
            "relationship_type": "fits",
            "from_type": "Part",
            "from_id": "BP-1",
            "to_type": "Vehicle",
            "to_id": "V-2",
            "edge_key": 0,
            "properties": {"verified": True},
            "metadata": {
                "provenance": EDGE_PROVENANCE_FULL,
                "assertion": UNREVIEWED_ASSERTION_FULL,
                "evidence": None,
            },
            "alias": None,
        }
    ],
    "includes": {},
}

QUERY_RUN_STANDARD = {
    "items": [QUERY_PATH_ROW_STANDARD],
    "receipt_id": "<varies>",
    "receipt": {
        "receipt_id": "<varies>",
        "query_name": "vehicles_for_part",
        "parameters": {"part_number": "BP-1"},
        "execution_options": QUERY_EXECUTION_OPTIONS,
        "nodes": [
            {
                "node_id": "n1",
                "node_type": "query",
                "entity_type": None,
                "entity_id": None,
                "relationship": None,
                "detail": {
                    "query_name": "vehicles_for_part",
                    "parameters": {"part_number": "BP-1"},
                    "execution_options": QUERY_EXECUTION_OPTIONS,
                    "filter_summary": [],
                    "select": None,
                    "order_by": [],
                    "limit": None,
                    "max_paths": None,
                    "max_paths_per_result": None,
                    "include": [],
                },
                "payload_metadata": None,
                "timestamp": "<varies>",
            },
            {
                "node_id": "n2",
                "node_type": "entity_lookup",
                "entity_type": "Part",
                "entity_id": "BP-1",
                "relationship": None,
                "detail": {},
                "payload_metadata": None,
                "timestamp": "<varies>",
            },
            {
                "node_id": "n3",
                "node_type": "edge_traversal",
                "entity_type": "Vehicle",
                "entity_id": "V-2",
                "relationship": "fits",
                "detail": {
                    "from_entity_type": "Part",
                    "from_entity_id": "BP-1",
                    "edge_properties": {"verified": True},
                    "edge_key": 0,
                },
                "payload_metadata": None,
                "timestamp": "<varies>",
            },
            {
                "node_id": "n4",
                "node_type": "result",
                "entity_type": None,
                "entity_id": None,
                "relationship": None,
                "detail": {
                    "count": 1,
                    "total_results": 1,
                    "limit": None,
                    "truncated": False,
                    "limit_truncated": False,
                    "path_truncated": False,
                    "truncation_reasons": [],
                    "max_paths": None,
                    "max_paths_per_result": None,
                    "total_path_count": None,
                    "retained_path_count": None,
                    "evaluated_path_candidate_count": None,
                },
                "payload_metadata": None,
                "timestamp": "<varies>",
            },
        ],
        "edges": [
            {"from_node": "n1", "to_node": "n2", "edge_type": "consulted"},
            {"from_node": "n1", "to_node": "n3", "edge_type": "traversed"},
            {"from_node": "n3", "to_node": "n4", "edge_type": "produced"},
        ],
        "results": [QUERY_PATH_ROW_STANDARD],
        "created_at": "<varies>",
        "duration_ms": "<varies>",
        "operation_type": "query",
        "head_snapshot_id": None,
        "workflow_mode": None,
        "committed": False,
        "actor_context": None,
    },
    "total": 1,
    "limit": None,
    "offset": 0,
    "truncated": False,
    "limit_truncated": False,
    "path_truncated": False,
    "truncation_reasons": [],
    "max_paths": None,
    "max_paths_per_result": None,
    "total_path_count": None,
    "retained_path_count": None,
    "steps_executed": 1,
    "result_shape": "path",
    "dedupe": "path",
    "relationship_state": "live",
    "param_hints": {
        "entry_point": "Part",
        "required_params": ["part_number"],
        "primary_key": "part_number",
        "example_ids": ["BP-1"],
    },
    "policy_summary": {},
    "read_revision": "<varies>",
}

GET_ENTITY_STANDARD = {
    "found": True,
    "entity_type": "Part",
    "entity_id": "BP-1",
    "properties": PART_PROPERTIES,
    "metadata": {"actor_context": NORMALIZED_ACTOR_CONTEXT},
    "read_revision": "<varies>",
}


class TestStandardIsDefaultAndUnchanged:
    """profile=standard is byte-identical to omitting the parameter (a).

    Every standard surface is additionally pinned against its COMPLETE
    pre-change payload with only volatile leaf values normalized, so a drift
    that moves default and explicit-standard together still fails here.
    """

    def test_get_entity_standard_shape_is_pinned(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        default = app_client.get(f"/api/v1/{seeded_instance}/entities/Part/BP-1").json()
        explicit = app_client.get(
            f"/api/v1/{seeded_instance}/entities/Part/BP-1",
            params={"profile": "standard"},
        ).json()
        assert default == explicit
        assert _normalized(default) == GET_ENTITY_STANDARD

    def test_list_entities_standard_shape_is_pinned(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        default = app_client.get(
            f"/api/v1/{seeded_instance}/list/entities", params={"entity_type": "Part"}
        ).json()
        explicit = app_client.get(
            f"/api/v1/{seeded_instance}/list/entities",
            params={"entity_type": "Part", "profile": "standard"},
        ).json()
        assert default == explicit
        assert _normalized(default) == LIST_ENTITIES_STANDARD

    def test_list_edges_standard_shape_is_pinned(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        default = app_client.get(f"/api/v1/{seeded_instance}/list/edges").json()
        explicit = app_client.get(
            f"/api/v1/{seeded_instance}/list/edges", params={"profile": "standard"}
        ).json()
        assert default == explicit
        assert _normalized(default) == LIST_EDGES_STANDARD

    def test_sample_standard_shape_is_pinned(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        default = app_client.get(f"/api/v1/{seeded_instance}/sample/Part").json()
        explicit = app_client.get(
            f"/api/v1/{seeded_instance}/sample/Part", params={"profile": "standard"}
        ).json()
        assert default == explicit
        assert _normalized(default) == SAMPLE_STANDARD

    def test_inspect_standard_shape_is_pinned(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        default = app_client.get(f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1").json()
        explicit = app_client.get(
            f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1",
            params={"profile": "standard"},
        ).json()
        assert default == explicit
        assert _normalized(default) == INSPECT_STANDARD
        assert list(default) == list(INSPECT_STANDARD)

    def test_query_run_standard_shape_is_pinned(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        body = {"query_name": "vehicles_for_part", "params": {"part_number": "BP-1"}}
        default = app_client.post(f"/api/v1/{seeded_instance}/queries/run", json=body).json()
        explicit = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run",
            json={**body, "profile": "standard"},
        ).json()
        # Complete pin including the receipt's full key structure (volatile
        # leaves normalized only) — the receipt subtree is never blanked.
        assert _normalized(default) == QUERY_RUN_STANDARD
        assert _normalized(explicit) == QUERY_RUN_STANDARD


class TestCompactProfile:
    """Compact drops actor_context/provenance, keeps governance markers (b, c, f)."""

    def test_compact_get_entity_is_a_bounded_identity_card(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        compact = app_client.get(
            f"/api/v1/{seeded_instance}/entities/Part/BP-1",
            params={"profile": "compact"},
        ).json()
        assert _normalized(compact) == {
            "found": True,
            "entity_type": "Part",
            "entity_id": "BP-1",
            "properties": {"name": "Ceramic Brake Pads"},
            "metadata": {},
            "read_revision": "<varies>",
        }

    def test_compact_edge_keeps_identity_and_review_markers(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        edges = app_client.get(
            f"/api/v1/{seeded_instance}/list/edges", params={"profile": "compact"}
        ).json()
        # Envelope fields are never trimmed.
        assert set(edges) == {
            "items",
            "total",
            "limit",
            "offset",
            "truncated",
            "read_revision",
            "continuation_token",
        }
        edge = edges["items"][0]
        assert edge["relationship_type"] == "fits"
        assert edge["from_type"] == "Part"
        assert edge["from_id"] == "BP-1"
        assert edge["to_type"] == "Vehicle"
        assert edge["to_id"] == "V-1"
        assert isinstance(edge["edge_key"], int)
        assert edge["properties"] == {"verified": True}
        # The reviewable-state (pending) governance markers MUST survive.
        assert edge["metadata"]["assertion"]["review"]["status"] == "pending"
        assert edge["metadata"]["assertion"]["lifecycle"] == {"status": "active"}
        assert "provenance" not in edge["metadata"]
        assert "actor_context" not in json.dumps(edges)

    def test_compact_inspect_and_query_drop_actor_context(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        inspect = app_client.get(
            f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1",
            params={"profile": "compact"},
        ).json()
        assert inspect["found"] is True
        assert inspect["total_neighbors"] == 2
        assert "actor_context" not in json.dumps(inspect)
        pending_neighbor = next(
            neighbor
            for neighbor in inspect["neighbors"]
            if neighbor["metadata"]["assertion"]["review"]["status"] == "pending"
        )
        assert pending_neighbor["metadata"]["assertion"]["lifecycle"] == {"status": "active"}

        query = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run",
            json={
                "query_name": "vehicles_for_part",
                "params": {"part_number": "BP-1"},
                "profile": "compact",
            },
        ).json()
        assert query["total"] == 1
        assert "actor_context" not in json.dumps(query["items"])
        # Envelope and receipt survive untouched.
        assert query["receipt_id"] is not None
        assert query["truncated"] is False

    def test_compact_sample_items_are_bounded(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        sample = app_client.get(
            f"/api/v1/{seeded_instance}/sample/Part", params={"profile": "compact"}
        ).json()
        assert sample["entity_type"] == "Part"
        assert sample["items"][0] == {
            "entity_type": "Part",
            "entity_id": "BP-1",
            "properties": {"name": "Ceramic Brake Pads"},
            "metadata": {},
        }
