"""HTTP graph-layout tests: rows stay bit-identical, graph is opt-in.

Freeze-guard intent: the `layout` body param is ADDITIVE. Requests that do not
ask for a layout get exactly the pre-layout payload — asserted byte-for-byte
against `layout="rows"` and by the absence of any graph key — and
`layout="graph"` changes ONLY the item representation: envelope, truncation,
relationship visibility, policy summary, and receipt fields pass through
verbatim.
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
from tests.test_server.test_read_profiles import _normalized

GRAPH_ONLY_KEYS = {"layout", "nodes", "edges", "results", "paths"}

INLINE_PATH_DEFINITION = {
    "name": "vehicles_for_part_paths",
    "mode": "traversal",
    "entry_point": "Part",
    "traversal": [{"relationship": "fits", "direction": "outgoing", "alias": "fit"}],
    "returns": "list[Vehicle]",
    "result_shape": "path",
    "dedupe": "path",
}


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

    for vehicle in ("V-1", "V-2"):
        added = app_client.post(
            f"/api/v1/{instance_id}/relationships",
            json={
                "relationships": [
                    {
                        "from_type": "Part",
                        "from_id": "BP-1",
                        "relationship_type": "fits",
                        "to_type": "Vehicle",
                        "to_id": vehicle,
                        "properties": {"verified": True},
                    }
                ]
            },
        )
        assert added.status_code == 200
    return instance_id


ENVELOPE_KEYS = (
    "total",
    "limit",
    "offset",
    "truncated",
    "limit_truncated",
    "path_truncated",
    "truncation_reasons",
    "max_paths",
    "max_paths_per_result",
    "total_path_count",
    "retained_path_count",
    "steps_executed",
    "result_shape",
    "dedupe",
    "relationship_state",
    "param_hints",
    "policy_summary",
    "read_revision",
)


class TestRowsLayoutIsUnchanged:
    def test_omitted_layout_is_byte_identical_to_explicit_rows(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        body = {"query_name": "vehicles_for_part", "params": {"part_number": "BP-1"}}
        omitted = app_client.post(f"/api/v1/{seeded_instance}/queries/run", json=body)
        explicit = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run",
            json={**body, "layout": "rows"},
        )
        assert omitted.status_code == 200
        # Byte-identity modulo the per-execution receipt volatiles
        # (receipt_id/timestamp/operation ids differ across two executions).
        assert _normalized(omitted.json()) == _normalized(explicit.json())
        payload = omitted.json()
        assert "items" in payload
        assert GRAPH_ONLY_KEYS.isdisjoint(payload)

    def test_inline_omitted_layout_is_byte_identical_to_explicit_rows(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        body = {"definition": INLINE_PATH_DEFINITION, "params": {"part_number": "BP-1"}}
        omitted = app_client.post(f"/api/v1/{seeded_instance}/queries/run-inline", json=body)
        explicit = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run-inline",
            json={**body, "layout": "rows"},
        )
        assert omitted.status_code == 200
        assert _normalized(omitted.json()) == _normalized(explicit.json())
        assert GRAPH_ONLY_KEYS.isdisjoint(omitted.json())


class TestGraphLayout:
    def test_envelope_and_receipt_pass_through_verbatim(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        body = {"definition": INLINE_PATH_DEFINITION, "params": {"part_number": "BP-1"}}
        rows = app_client.post(f"/api/v1/{seeded_instance}/queries/run-inline", json=body).json()
        graph = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run-inline",
            json={**body, "layout": "graph"},
        ).json()

        assert graph["layout"] == "graph"
        assert set(graph) == set(rows) - {"items", "receipt"} | GRAPH_ONLY_KEYS | {"receipt"}
        for key in ENVELOPE_KEYS:
            assert graph[key] == rows[key], key
        # receipt_id is per-execution: present in both, values differ.
        assert graph["receipt_id"] is not None
        assert rows["receipt_id"] is not None
        # The inline receipt document itself is verbatim passthrough
        # (per-execution ids/timestamps normalized, structure pinned).
        assert _normalized(graph["receipt"]) == _normalized(rows["receipt"])

        # Graph sections carry each entity/edge once with ordered references.
        assert [node["entity_id"] for node in graph["nodes"]] == ["BP-1", "V-1", "V-2"]
        assert len(graph["edges"]) == 2
        assert graph["results"] == [
            {"entry": 0, "result": 1, "paths": [0], "includes": {}},
            {"entry": 0, "result": 2, "paths": [1], "includes": {}},
        ]
        assert graph["paths"] == [
            [{"edge": 0, "alias": "fit"}],
            [{"edge": 1, "alias": "fit"}],
        ]
        # Edge cards are physical: the step alias lives on the path refs.
        assert all("alias" not in edge for edge in graph["edges"])

    def test_named_query_graph_layout(self, app_client: TestClient, seeded_instance: str) -> None:
        body = {
            "query_name": "vehicles_for_part",
            "params": {"part_number": "BP-1"},
            "layout": "graph",
        }
        graph = app_client.post(f"/api/v1/{seeded_instance}/queries/run", json=body).json()
        rows = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run",
            json={"query_name": "vehicles_for_part", "params": {"part_number": "BP-1"}},
        ).json()
        assert graph["layout"] == "graph"
        assert graph["total"] == rows["total"]
        assert graph["result_shape"] == rows["result_shape"]
        assert graph["receipt_id"] is not None
        assert len(graph["results"]) == rows["total"]

    def test_truncated_query_envelope_passes_through(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        body = {
            "definition": INLINE_PATH_DEFINITION,
            "params": {"part_number": "BP-1"},
            "limit": 1,
        }
        rows = app_client.post(f"/api/v1/{seeded_instance}/queries/run-inline", json=body).json()
        graph = app_client.post(
            f"/api/v1/{seeded_instance}/queries/run-inline",
            json={**body, "layout": "graph"},
        ).json()
        assert rows["truncated"] is True
        assert graph["truncated"] is True
        assert graph["limit"] == rows["limit"] == 1
        assert graph["total"] == rows["total"] == 2
        # A limited read omits the inline receipt in BOTH layouts.
        assert rows["receipt"] is None
        assert graph["receipt"] is None
        assert graph["receipt_id"] is not None
        # Only the retained window is normalized.
        assert len(graph["results"]) == 1
        assert len(graph["paths"]) == 1

    def test_graph_layout_composes_with_compact_profile(
        self, app_client: TestClient, seeded_instance: str
    ) -> None:
        body = {
            "definition": INLINE_PATH_DEFINITION,
            "params": {"part_number": "BP-1"},
            "profile": "compact",
            "layout": "graph",
        }
        graph = app_client.post(f"/api/v1/{seeded_instance}/queries/run-inline", json=body).json()
        assert graph["layout"] == "graph"
        # Compact node cards are bounded identity cards.
        part = next(node for node in graph["nodes"] if node["entity_id"] == "BP-1")
        assert part["properties"] == {"name": "Ceramic Brake Pads"}
        assert "actor_context" not in str(graph["nodes"])
        # Edge properties (the assertion payload) survive compact.
        assert all(edge["properties"] == {"verified": True} for edge in graph["edges"])
