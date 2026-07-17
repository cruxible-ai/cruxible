"""HTTP surface tests for read_revision + continuation tokens.

Covers the resumable-read contract: opaque revision-bound tokens on list,
query catalog, and bounded neighborhood inspect; typed 409 on stale replay
(state mutated between pages); 422 on malformed or re-bound tokens; and the
explicit-truncation invariant (total > 0 with an empty page is never silent).
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


def _create_instance(app_client: TestClient, root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    response = app_client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CAR_PARTS_YAML},
    )
    assert response.status_code == 200, response.text
    return response.json()["instance_id"]


def _seed_vehicles(app_client: TestClient, instance_id: str, count: int) -> None:
    entities = [
        {
            "entity_type": "Vehicle",
            "entity_id": f"V-{index:03d}",
            "properties": {
                "vehicle_id": f"V-{index:03d}",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        }
        for index in range(count)
    ]
    response = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": entities},
    )
    assert response.status_code == 200, response.text


def _seed_star_graph(app_client: TestClient, instance_id: str, spokes: int) -> None:
    """One Part hub with `spokes` Vehicle neighbors (all live edges)."""
    entities = [
        {
            "entity_type": "Part",
            "entity_id": "HUB-1",
            "properties": {"part_number": "HUB-1", "name": "Hub", "category": "brakes"},
        }
    ] + [
        {
            "entity_type": "Vehicle",
            "entity_id": f"V-{index:03d}",
            "properties": {
                "vehicle_id": f"V-{index:03d}",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        }
        for index in range(spokes)
    ]
    response = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": entities},
    )
    assert response.status_code == 200, response.text
    relationships = [
        {
            "from_type": "Part",
            "from_id": "HUB-1",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": f"V-{index:03d}",
            "properties": {"verified": True},
        }
        for index in range(spokes)
    ]
    response = app_client.post(
        f"/api/v1/{instance_id}/relationships",
        json={"relationships": relationships},
    )
    assert response.status_code == 200, response.text


def _mutate(app_client: TestClient, instance_id: str) -> None:
    response = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Part",
                    "entity_id": "MUT-1",
                    "properties": {"part_number": "MUT-1", "name": "Mut", "category": "brakes"},
                }
            ]
        },
    )
    assert response.status_code == 200, response.text


def _list_vehicles(app_client: TestClient, instance_id: str, **params) -> dict:
    response = app_client.get(
        f"/api/v1/{instance_id}/list/entities",
        params={"entity_type": "Vehicle", **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestListContinuation:
    def test_round_trip_pages_are_disjoint_and_ordered(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_vehicles(app_client, instance_id, 7)

        page1 = _list_vehicles(app_client, instance_id, limit=3)
        assert page1["total"] == 7
        assert page1["truncated"] is True
        assert isinstance(page1["read_revision"], int)
        token = page1["continuation_token"]
        assert token

        page2 = _list_vehicles(app_client, instance_id, limit=3, continuation=token)
        ids1 = [item["entity_id"] for item in page1["items"]]
        ids2 = [item["entity_id"] for item in page2["items"]]
        assert len(ids1) == 3 and len(ids2) == 3
        assert set(ids1).isdisjoint(ids2)
        assert ids1 + ids2 == sorted(ids1 + ids2)
        assert page2["offset"] == 3
        assert page2["read_revision"] == page1["read_revision"]

        # Walk to the end: last page has no token.
        page3 = _list_vehicles(
            app_client, instance_id, limit=3, continuation=page2["continuation_token"]
        )
        assert [item["entity_id"] for item in page3["items"]] == sorted(
            set(f"V-{i:03d}" for i in range(7)) - set(ids1 + ids2)
        )
        assert page3["truncated"] is False
        assert page3["continuation_token"] is None
        assert ids1 + ids2 + [i["entity_id"] for i in page3["items"]] == sorted(
            f"V-{i:03d}" for i in range(7)
        )

    def test_stale_token_after_mutation_is_409_typed(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_vehicles(app_client, instance_id, 5)
        token = _list_vehicles(app_client, instance_id, limit=2)["continuation_token"]

        _mutate(app_client, instance_id)

        response = app_client.get(
            f"/api/v1/{instance_id}/list/entities",
            params={"entity_type": "Vehicle", "limit": 2, "continuation": token},
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error_type"] == "StaleContinuationError"
        assert body["error_code"] == "stale_continuation"
        assert "restart" in body["message"].lower()
        assert isinstance(body["context"]["current_read_revision"], int)

    def test_malformed_token_is_422(self, app_client: TestClient, tmp_path: Path) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_vehicles(app_client, instance_id, 2)
        response = app_client.get(
            f"/api/v1/{instance_id}/list/entities",
            params={"entity_type": "Vehicle", "continuation": "not-a-token!!"},
        )
        assert response.status_code == 422
        assert response.json()["error_type"] == "InvalidContinuationError"

    def test_token_from_another_instance_is_rejected(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        first = _create_instance(app_client, tmp_path / "p1")
        second = _create_instance(app_client, tmp_path / "p2")
        _seed_vehicles(app_client, first, 5)
        _seed_vehicles(app_client, second, 5)
        token = _list_vehicles(app_client, first, limit=2)["continuation_token"]

        response = app_client.get(
            f"/api/v1/{second}/list/entities",
            params={"entity_type": "Vehicle", "limit": 2, "continuation": token},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error_type"] == "InvalidContinuationError"
        assert "different instance" in body["message"]

    def test_token_with_different_filters_is_rejected(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_vehicles(app_client, instance_id, 5)
        token = _list_vehicles(app_client, instance_id, limit=2)["continuation_token"]

        response = app_client.get(
            f"/api/v1/{instance_id}/list/edges",
            params={"limit": 2, "continuation": token},
        )
        assert response.status_code == 422
        assert response.json()["error_type"] == "InvalidContinuationError"


class TestReceiptContinuation:
    """Receipts are audit rows: inserting one does NOT bump read_revision, so
    receipt continuation must be keyset-based (resume strictly older than the
    last-seen receipt), never offset-based."""

    @staticmethod
    def _run_query(app_client: TestClient, instance_id: str) -> None:
        response = app_client.post(
            f"/api/v1/{instance_id}/queries/run",
            json={"query_name": "vehicles_for_part", "params": {"part_number": "HUB-1"}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["receipt_id"]

    @staticmethod
    def _list_receipts(app_client: TestClient, instance_id: str, **params) -> dict:
        response = app_client.get(
            f"/api/v1/{instance_id}/list/receipts",
            params=params,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_page2_is_stable_when_a_receipt_is_inserted_between_pages(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_star_graph(app_client, instance_id, spokes=2)
        for _ in range(6):
            self._run_query(app_client, instance_id)

        # Baseline: every receipt that exists before pagination starts
        # (6 query receipts + the seeding mutation receipts), newest first.
        baseline = self._list_receipts(app_client, instance_id, limit=50)
        assert baseline["truncated"] is False
        baseline_ids = [item["receipt_id"] for item in baseline["items"]]
        assert len(baseline_ids) >= 6

        page1 = self._list_receipts(app_client, instance_id, limit=2)
        assert page1["truncated"] is True
        token = page1["continuation_token"]
        assert token
        ids1 = [item["receipt_id"] for item in page1["items"]]

        # Control: resume WITHOUT any insertion in between.
        control = self._list_receipts(app_client, instance_id, limit=2, continuation=token)
        control_ids = [item["receipt_id"] for item in control["items"]]
        assert len(control_ids) == 2
        assert set(ids1).isdisjoint(control_ids)

        # Insert a new (query) receipt between pages. This does not bump
        # read_revision, so the token stays valid — page 2 must still be
        # identical to the no-insertion control: no duplicate, no skip.
        self._run_query(app_client, instance_id)
        page2 = self._list_receipts(app_client, instance_id, limit=2, continuation=token)
        assert [item["receipt_id"] for item in page2["items"]] == control_ids
        assert set(ids1).isdisjoint(item["receipt_id"] for item in page2["items"])

        # Walking to the end returns exactly the receipts that existed when
        # page 1 was read, in order — the mid-scan insert never appears, and
        # nothing is skipped.
        collected = list(ids1)
        payload = page2
        collected.extend(item["receipt_id"] for item in payload["items"])
        while payload["truncated"]:
            token = payload["continuation_token"]
            assert token
            payload = self._list_receipts(app_client, instance_id, limit=2, continuation=token)
            page_ids = [item["receipt_id"] for item in payload["items"]]
            assert set(collected).isdisjoint(page_ids)
            collected.extend(page_ids)
        assert payload["continuation_token"] is None
        assert collected == baseline_ids
        # The post-token receipt shows up only on a fresh (restarted) read.
        fresh = self._list_receipts(app_client, instance_id, limit=50)
        assert len(fresh["items"]) == len(baseline_ids) + 1

    def test_receipt_token_is_still_revision_bound(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_star_graph(app_client, instance_id, spokes=2)
        for _ in range(3):
            self._run_query(app_client, instance_id)
        token = self._list_receipts(app_client, instance_id, limit=1)["continuation_token"]
        assert token

        _mutate(app_client, instance_id)  # graph mutation DOES bump read_revision

        response = app_client.get(
            f"/api/v1/{instance_id}/list/receipts",
            params={"limit": 1, "continuation": token},
        )
        assert response.status_code == 409
        assert response.json()["error_type"] == "StaleContinuationError"


class TestQueryCatalogContinuation:
    def test_round_trip(self, app_client: TestClient, tmp_path: Path) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        page1 = app_client.get(f"/api/v1/{instance_id}/queries", params={"limit": 1}).json()
        assert page1["truncated"] is True
        token = page1["continuation_token"]
        assert token

        page2 = app_client.get(
            f"/api/v1/{instance_id}/queries",
            params={"limit": 5, "continuation": token},
        ).json()
        names1 = [item["name"] for item in page1["items"]]
        names2 = [item["name"] for item in page2["items"]]
        assert set(names1).isdisjoint(names2)
        assert page2["offset"] == 1

    def test_detail_mismatch_is_rejected(self, app_client: TestClient, tmp_path: Path) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        token = app_client.get(f"/api/v1/{instance_id}/queries", params={"limit": 1}).json()[
            "continuation_token"
        ]
        response = app_client.get(
            f"/api/v1/{instance_id}/queries",
            params={"limit": 1, "detail": "full", "continuation": token},
        )
        assert response.status_code == 422
        assert response.json()["error_type"] == "InvalidContinuationError"


class TestNeighborhoodContinuation:
    def test_budget_truncated_bfs_resumes_to_exact_untruncated_set(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_star_graph(app_client, instance_id, spokes=9)

        untruncated = app_client.get(
            f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
            params={"depth": 1},
        ).json()
        assert untruncated["truncated"] is False
        full_nodes = {(n["entity_type"], n["entity_id"]) for n in untruncated["nodes"]}
        full_edges = {(e["from_id"], e["to_id"], e["edge_key"]) for e in untruncated["edges"]}
        assert len(full_nodes) == 9

        collected_nodes: set[tuple[str, str]] = set()
        collected_edges: set[tuple[str, str, int]] = set()
        params: dict = {"depth": 1, "max_nodes": 4, "max_edges": 4}
        token: str | None = None
        pages = 0
        while True:
            request_params = dict(params)
            if token is not None:
                request_params["continuation"] = token
            payload = app_client.get(
                f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
                params=request_params,
            ).json()
            pages += 1
            page_nodes = {(n["entity_type"], n["entity_id"]) for n in payload["nodes"]}
            page_edges = {(e["from_id"], e["to_id"], e["edge_key"]) for e in payload["edges"]}
            # Pages never repeat items already returned.
            assert collected_nodes.isdisjoint(page_nodes)
            assert collected_edges.isdisjoint(page_edges)
            collected_nodes |= page_nodes
            collected_edges |= page_edges
            token = payload["continuation_token"]
            if not payload["truncated"]:
                assert token is None
                break
            assert token is not None
            assert pages < 10  # guard against a non-terminating loop

        assert pages >= 3
        assert collected_nodes == full_nodes
        assert collected_edges == full_edges

    def test_stale_neighborhood_token_is_409(self, app_client: TestClient, tmp_path: Path) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_star_graph(app_client, instance_id, spokes=6)
        payload = app_client.get(
            f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
            params={"depth": 1, "max_nodes": 2},
        ).json()
        token = payload["continuation_token"]
        assert token

        _mutate(app_client, instance_id)

        response = app_client.get(
            f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
            params={"depth": 1, "max_nodes": 2, "continuation": token},
        )
        assert response.status_code == 409
        assert response.json()["error_type"] == "StaleContinuationError"

    def test_continuation_without_neighborhood_params_is_422(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_star_graph(app_client, instance_id, spokes=3)
        payload = app_client.get(
            f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
            params={"depth": 1, "max_nodes": 2},
        ).json()
        response = app_client.get(
            f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
            params={"continuation": payload["continuation_token"]},
        )
        assert response.status_code == 422
        assert response.json()["error_type"] == "InvalidContinuationError"

    def test_depth_only_truncation_is_not_resumable(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        # Two-hop chain: HUB-1 -> V-000 and a second part on V-000.
        _seed_star_graph(app_client, instance_id, spokes=1)
        response = app_client.post(
            f"/api/v1/{instance_id}/entities",
            json={
                "entities": [
                    {
                        "entity_type": "Part",
                        "entity_id": "BP-2",
                        "properties": {
                            "part_number": "BP-2",
                            "name": "Second",
                            "category": "brakes",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 200
        response = app_client.post(
            f"/api/v1/{instance_id}/relationships",
            json={
                "relationships": [
                    {
                        "from_type": "Part",
                        "from_id": "BP-2",
                        "relationship_type": "fits",
                        "to_type": "Vehicle",
                        "to_id": "V-000",
                        "properties": {"verified": True},
                    }
                ]
            },
        )
        assert response.status_code == 200

        payload = app_client.get(
            f"/api/v1/{instance_id}/inspect/entity/Part/HUB-1",
            params={"depth": 1},
        ).json()
        assert payload["truncated"] is True
        assert payload["truncation_reasons"] == ["depth"]
        # Depth-horizon truncation is a different read, not a resumable page.
        assert payload["continuation_token"] is None


class TestExplicitTruncationInvariant:
    def test_sample_reports_true_total_and_truncated(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_vehicles(app_client, instance_id, 8)
        payload = app_client.get(
            f"/api/v1/{instance_id}/sample/Vehicle", params={"limit": 3}
        ).json()
        assert len(payload["items"]) == 3
        assert payload["total"] == 8  # TRUE stored count, not the sampled count
        assert payload["truncated"] is True
        assert isinstance(payload["read_revision"], int)

    def test_sample_covering_the_type_is_not_truncated(
        self, app_client: TestClient, tmp_path: Path
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_vehicles(app_client, instance_id, 2)
        payload = app_client.get(
            f"/api/v1/{instance_id}/sample/Vehicle", params={"limit": 5}
        ).json()
        assert payload["total"] == 2
        assert payload["truncated"] is False

    @pytest.mark.parametrize("resource", ["entities", "edges"])
    def test_offset_beyond_end_is_never_silent(
        self, app_client: TestClient, tmp_path: Path, resource: str
    ) -> None:
        instance_id = _create_instance(app_client, tmp_path / "p1")
        _seed_star_graph(app_client, instance_id, spokes=3)
        params: dict = {"offset": 100}
        if resource == "entities":
            params["entity_type"] = "Vehicle"
        payload = app_client.get(f"/api/v1/{instance_id}/list/{resource}", params=params).json()
        assert payload["items"] == []
        assert payload["total"] > 0
        # Invariant: total > 0 with an empty page must be explicit truncation.
        assert payload["truncated"] is True
        # But there is nothing to resume from beyond the end.
        assert payload["continuation_token"] is None
