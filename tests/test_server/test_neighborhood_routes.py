"""HTTP surface tests for the bounded neighborhood inspect read.

The inspect-entity route is shape-switched: calls without neighborhood
parameters keep the legacy single-hop ``InspectEntityResult`` payload
bit-for-bit (pinned in ``test_read_profiles.py::INSPECT_STANDARD``);
providing any neighborhood parameter returns the expanded
``InspectNeighborhoodResult`` with explicit budgets and visible truncation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML

EXPANDED_KEYS = [
    "found",
    "entity_type",
    "entity_id",
    "properties",
    "metadata",
    "depth",
    "state",
    "nodes",
    "edges",
    "truncated",
    "truncation_reasons",
    "nodes_returned",
    "edges_returned",
]

LEGACY_KEYS = [
    "found",
    "entity_type",
    "entity_id",
    "properties",
    "metadata",
    "neighbors",
    "total_neighbors",
]


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
    """Car-parts instance with a two-hop graph and one pending edge.

    BP-1 -fits(live)-> V-2, BP-1 -fits(pending)-> V-1, BP-2 -fits(live)-> V-2:
    from BP-1, V-1/V-2 sit at depth 1 and BP-2 at depth 2 (via V-2).
    """
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
                {
                    "entity_type": "Part",
                    "entity_id": "BP-2",
                    "properties": {
                        "part_number": "BP-2",
                        "name": "Oil Filter",
                        "category": "engine",
                        "price": 9.99,
                    },
                },
            ]
        },
    )
    assert seeded.status_code == 200

    for edge in [
        {"from_id": "BP-1", "to_id": "V-2", "pending": False},
        {"from_id": "BP-1", "to_id": "V-1", "pending": True},
        {"from_id": "BP-2", "to_id": "V-2", "pending": False},
    ]:
        response = app_client.post(
            f"/api/v1/{instance_id}/relationships",
            json={
                "relationships": [
                    {
                        "from_type": "Part",
                        "from_id": edge["from_id"],
                        "relationship_type": "fits",
                        "to_type": "Vehicle",
                        "to_id": edge["to_id"],
                        "properties": {"verified": True},
                        "pending": edge["pending"],
                    }
                ]
            },
        )
        assert response.status_code == 200
    return instance_id


def _inspect(app_client: TestClient, instance_id: str, **params) -> dict:
    response = app_client.get(
        f"/api/v1/{instance_id}/inspect/entity/Part/BP-1",
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestExpandedShape:
    def test_depth_2_returns_nodes_and_edges_grouped_by_depth(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        payload = _inspect(app_client, seeded_instance, depth=2, state="all")
        assert list(payload) == EXPANDED_KEYS
        assert payload["found"] is True
        assert payload["depth"] == 2
        assert payload["state"] == "all"
        nodes = [(n["entity_type"], n["entity_id"], n["depth"]) for n in payload["nodes"]]
        assert nodes == [
            ("Vehicle", "V-1", 1),
            ("Vehicle", "V-2", 1),
            ("Part", "BP-2", 2),
        ]
        edges = [(e["from_id"], e["to_id"]) for e in payload["edges"]]
        assert edges == [("BP-1", "V-1"), ("BP-1", "V-2"), ("BP-2", "V-2")]
        assert payload["nodes_returned"] == 3
        assert payload["edges_returned"] == 3
        assert payload["truncated"] is False
        # Root card keeps full properties; markers survive on the pending edge.
        assert payload["properties"]["name"] == "Ceramic Brake Pads"
        pending = [e for e in payload["edges"] if e["to_id"] == "V-1"]
        assert pending[0]["metadata"]["assertion"]["review"]["status"] == "pending"

    def test_expanded_read_is_deterministic_across_calls(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        first = _inspect(app_client, seeded_instance, depth=2, state="all")
        second = _inspect(app_client, seeded_instance, depth=2, state="all")
        assert first == second

    def test_state_defaults_to_live_and_hides_pending(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        payload = _inspect(app_client, seeded_instance, depth=1)
        assert payload["state"] == "live"
        assert [n["entity_id"] for n in payload["nodes"]] == ["V-2"]

    def test_state_pending_and_reviewable_surface_the_pending_edge(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        pending = _inspect(app_client, seeded_instance, depth=1, state="pending")
        assert [n["entity_id"] for n in pending["nodes"]] == ["V-1"]
        reviewable = _inspect(app_client, seeded_instance, depth=1, state="reviewable")
        assert [n["entity_id"] for n in reviewable["nodes"]] == ["V-1", "V-2"]

    def test_single_hop_truncation_is_visible(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        """The silent legacy `[:limit]` cap reports truncation on the expanded read."""
        payload = _inspect(app_client, seeded_instance, depth=1, state="reviewable", max_nodes=1)
        assert payload["truncated"] is True
        assert payload["truncation_reasons"] == ["node_budget"]
        assert payload["nodes_returned"] == 1
        # Legacy `limit` maps to the node budget when max_nodes is omitted.
        mapped = _inspect(app_client, seeded_instance, depth=1, state="reviewable", limit=1)
        assert mapped["truncated"] is True
        assert mapped["truncation_reasons"] == ["node_budget"]

    def test_target_types_and_relationship_types_compose(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        payload = _inspect(
            app_client,
            seeded_instance,
            depth=2,
            state="all",
            relationship_types="fits",
            target_types="Vehicle",
        )
        assert [(n["entity_type"], n["entity_id"]) for n in payload["nodes"]] == [
            ("Vehicle", "V-1"),
            ("Vehicle", "V-2"),
        ]

    def test_projection_composes_with_compact_profile(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        payload = _inspect(
            app_client,
            seeded_instance,
            depth=2,
            state="all",
            projection="make",
            profile="compact",
        )
        vehicles = [n for n in payload["nodes"] if n["entity_type"] == "Vehicle"]
        assert vehicles
        for node in vehicles:
            assert node["properties"] == {"make": "Honda"}
        parts = [n for n in payload["nodes"] if n["entity_type"] == "Part"]
        # Projection names absent from an entity are simply omitted.
        assert all(node["properties"] == {} for node in parts)
        # Root is exempt from projection: compact card properties remain.
        assert payload["properties"] == {"name": "Ceramic Brake Pads"}

    def test_unknown_projection_property_is_a_config_error(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        response = app_client.get(
            f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1",
            params={"depth": 1, "projection": "no_such_property"},
        )
        assert response.status_code == 400
        assert "no_such_property" in response.json()["message"]


class TestLegacyShapeUnchanged:
    def test_default_call_has_no_expanded_keys(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        response = app_client.get(
            f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1",
        )
        assert response.status_code == 200
        assert list(response.json()) == LEGACY_KEYS

    def test_legacy_limit_call_keeps_legacy_values(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        """A legacy limited call keeps its exact shape: pre-cap total, capped rows."""
        response = app_client.get(
            f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1",
            params={"limit": 1},
        )
        assert response.status_code == 200
        payload = response.json()
        assert list(payload) == LEGACY_KEYS
        assert len(payload["neighbors"]) == 1
        assert payload["total_neighbors"] == 2


class TestBudgetValidation:
    """Hard-cap violations are typed 422s with clear messages."""

    @pytest.mark.parametrize(
        "params,fragment",
        [
            ({"depth": 5}, "depth"),
            ({"depth": 0}, "depth"),
            ({"max_nodes": 501}, "max_nodes"),
            ({"max_edges": 1001}, "max_edges"),
            ({"state": "bogus"}, "state"),
        ],
    )
    def test_out_of_range_params_are_422(
        self, app_client: TestClient, seeded_instance: str, params: dict, fragment: str
    ) -> None:
        response = app_client.get(
            f"/api/v1/{seeded_instance}/inspect/entity/Part/BP-1",
            params=params,
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error_type"] == "RequestValidationError"
        assert any(fragment in error for error in body["errors"])
