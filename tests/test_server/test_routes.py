"""Tests for FastAPI server routes."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.errors import ConstraintViolationError, InstanceNotFoundError
from cruxible_core.kits.state_refs import StateCatalogEntry
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.config import get_server_state_dir
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.server.routes import resolve_server_instance_id
from tests.test_cli.conftest import CAR_PARTS_YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_KIT_DIR = REPO_ROOT / "kits" / "kev-triage"


def _write_overlay_kit_manifest(
    kit_dir: Path,
    kit_id: str,
    *,
    target_state: str = "car-parts",
) -> None:
    (kit_dir / "cruxible-kit.yaml").write_text(
        "\n".join(
            [
                "schema_version: cruxible.kit.v1",
                f"kit_id: {kit_id}",
                "version: 0.2.0",
                "role: overlay",
                f"target_state: {target_state}",
                "entry_config: config.yaml",
                "provider_paths: []",
                "copy_paths: []",
                "requires_extras: []",
            ]
        )
        + "\n"
    )
    (kit_dir / "cruxible.lock.yaml").write_text(
        "version: '1'\nconfig_digest: test\nartifacts: {}\nproviders: {}\n"
    )


@pytest.fixture
def server_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    return root


@pytest.fixture
def workflow_server_project(tmp_path: Path, proposal_workflow_config_yaml: str) -> Path:
    root = tmp_path / "workflow-project"
    root.mkdir()
    (root / "config.yaml").write_text(proposal_workflow_config_yaml)
    return root


@pytest.fixture
def vehicles_csv(server_project: Path) -> Path:
    csv_path = server_project / "vehicles.csv"
    csv_path.write_text(
        "vehicle_id,year,make,model\n"
        "V-2024-CIVIC-EX,2024,Honda,Civic\n"
        "V-2024-ACCORD-SPORT,2024,Honda,Accord\n"
    )
    return csv_path


@pytest.fixture
def parts_csv(server_project: Path) -> Path:
    csv_path = server_project / "parts.csv"
    csv_path.write_text(
        "part_number,name,category,price\n"
        "BP-1001,Ceramic Brake Pads,brakes,49.99\n"
        "BP-1002,Performance Brake Pads,brakes,89.99\n"
    )
    return csv_path


@pytest.fixture
def fitments_csv(server_project: Path) -> Path:
    csv_path = server_project / "fitments.csv"
    csv_path.write_text(
        "part_number,vehicle_id,verified,source\n"
        "BP-1001,V-2024-CIVIC-EX,true,catalog\n"
        "BP-1001,V-2024-ACCORD-SPORT,true,catalog\n"
        "BP-1002,V-2024-CIVIC-EX,true,user_report\n"
    )
    return csv_path


def _make_app_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_enabled: bool = False,
    token: str | None = None,
) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    if auth_enabled:
        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
        assert token is not None
        monkeypatch.setenv("CRUXIBLE_SERVER_TOKEN", token)
    else:
        monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
        monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    return TestClient(create_app())


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _make_app_client(tmp_path, monkeypatch)


def _init_instance(
    client: TestClient,
    root: Path,
    *,
    config_yaml: str | None = None,
    kit: str | None = None,
) -> str:
    resolved_config_yaml = (
        config_yaml if config_yaml is not None else (root / "config.yaml").read_text()
    )
    payload = {"root_dir": str(root)}
    if kit is not None:
        payload["kit"] = kit
    else:
        payload["config_yaml"] = resolved_config_yaml
    response = client.post(
        "/api/v1/instances",
        json=payload,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["instance_id"] != str(root)
    return payload["instance_id"]


def _seed_car_parts_state(client: TestClient, instance_id: str) -> None:
    entities = [
        {
            "entity_type": "Vehicle",
            "entity_id": "V-2024-CIVIC-EX",
            "properties": {
                "vehicle_id": "V-2024-CIVIC-EX",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        },
        {
            "entity_type": "Vehicle",
            "entity_id": "V-2024-ACCORD-SPORT",
            "properties": {
                "vehicle_id": "V-2024-ACCORD-SPORT",
                "year": 2024,
                "make": "Honda",
                "model": "Accord",
            },
        },
        {
            "entity_type": "Part",
            "entity_id": "BP-1001",
            "properties": {
                "part_number": "BP-1001",
                "name": "Ceramic Brake Pads",
                "category": "brakes",
                "price": 49.99,
            },
        },
        {
            "entity_type": "Part",
            "entity_id": "BP-1002",
            "properties": {
                "part_number": "BP-1002",
                "name": "Performance Brake Pads",
                "category": "brakes",
                "price": 89.99,
            },
        },
    ]
    relationships = [
        {
            "from_type": "Part",
            "from_id": "BP-1001",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-CIVIC-EX",
            "properties": {"verified": True, "source": "catalog"},
        },
        {
            "from_type": "Part",
            "from_id": "BP-1001",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-ACCORD-SPORT",
            "properties": {"verified": True, "source": "catalog"},
        },
        {
            "from_type": "Part",
            "from_id": "BP-1002",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-CIVIC-EX",
            "properties": {"verified": True, "source": "user_report"},
        },
    ]
    response = client.post(f"/api/v1/{instance_id}/entities", json={"entities": entities})
    assert response.status_code == 200
    response = client.post(
        f"/api/v1/{instance_id}/relationships",
        json={"relationships": relationships},
    )
    assert response.status_code == 200


def test_health_endpoint_returns_ok(app_client: TestClient):
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_request_validation_errors_use_error_response_envelope(app_client: TestClient):
    # Non-integer offset trips FastAPI param validation; the body must be the
    # ErrorResponse envelope, not FastAPI's native {detail: [...]} shape.
    response = app_client.get("/api/v1/inst-missing/traces", params={"offset": "abc"})
    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body["error_type"] == "RequestValidationError"
    assert body["message"] == "Request validation failed"
    assert any("offset" in error for error in body["errors"])


def test_server_info_endpoint_returns_live_metadata(
    app_client: TestClient,
    server_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_REQUIRE_SERVER", "1")
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    reset_permissions()
    _init_instance(app_client, server_project)

    response = app_client.get("/api/v1/server/info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["server_required"] is True
    assert payload["version"]
    assert payload["state_dir"] == str(get_server_state_dir())
    assert payload["instance_count"] == 1


def test_daemon_auth_defaults_to_disabled_for_local_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    response = client.post("/api/v1/validate", json={"config_yaml": CAR_PARTS_YAML})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_optional_server_token_gates_entire_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch, auth_enabled=True, token="local-secret")

    missing = client.post("/api/v1/validate", json={"config_yaml": CAR_PARTS_YAML})
    assert missing.status_code == 401

    wrong = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert wrong.status_code == 401

    allowed = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Authorization": "Bearer local-secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["valid"] is True


def test_init_then_seed_then_query_round_trip(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    response = app_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["receipt_id"]
    assert payload["param_hints"]["primary_key"] == "vehicle_id"

    evaluate = app_client.post(f"/api/v1/{instance_id}/evaluate", json={})
    assert evaluate.status_code == 200
    assert "quality_summary" in evaluate.json()

    lint = app_client.post(f"/api/v1/{instance_id}/lint", json={})
    assert lint.status_code == 200
    lint_payload = lint.json()
    assert lint_payload["config_name"] == "car_parts_compatibility"
    assert isinstance(lint_payload["has_issues"], bool)
    assert "summary" in lint_payload
    assert "evaluation" in lint_payload


def test_view_route_runs_named_query_with_string_params(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    run = app_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
        },
    )
    view = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX"},
    )

    assert view.status_code == 200
    payload = view.json()
    assert payload["total"] == 2
    assert payload["offset"] == 0
    assert payload["items"] == run.json()["items"]
    assert payload["receipt_id"]

    receipt = app_client.get(f"/api/v1/{instance_id}/receipts/{payload['receipt_id']}")
    assert receipt.status_code == 200


def test_view_route_windows_results_deterministically(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    full = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX"},
    ).json()
    first = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX", "limit": 1, "offset": 0},
    ).json()
    second = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX", "limit": 1, "offset": 1},
    ).json()

    assert full["total"] == 2
    assert [first["items"][0], second["items"][0]] == full["items"]
    assert first["offset"] == 0
    assert first["truncated"] is True
    assert second["offset"] == 1
    assert second["truncated"] is False
    assert first["total"] == second["total"] == 2

    beyond = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX", "limit": 1, "offset": 10},
    ).json()
    assert beyond["items"] == []
    assert beyond["truncated"] is False
    assert beyond["total"] == 2


def test_view_route_rejects_unknown_query_with_error_envelope(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)

    response = app_client.get(f"/api/v1/{instance_id}/views/no_such_query")

    assert response.status_code == 404
    body = response.json()
    assert body["error_type"] == "QueryNotFoundError"
    assert "no_such_query" in body["message"]


def test_view_route_validates_reserved_pagination_params(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)

    bad_offset = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX", "offset": -1},
    )
    assert bad_offset.status_code == 422
    assert bad_offset.json()["error_type"] == "RequestValidationError"

    bad_limit = app_client.get(
        f"/api/v1/{instance_id}/views/parts_for_vehicle",
        params={"vehicle_id": "V-2024-CIVIC-EX", "limit": 0},
    )
    assert bad_limit.status_code == 422
    assert bad_limit.json()["error_type"] == "RequestValidationError"


def test_query_run_route_accepts_offset(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    second_page = app_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
            "limit": 1,
            "offset": 1,
        },
    )
    assert second_page.status_code == 200
    payload = second_page.json()
    assert payload["offset"] == 1
    assert payload["total"] == 2
    assert len(payload["items"]) == 1
    assert payload["truncated"] is False


def test_inline_query_route_executes_without_persisting_config(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    response = app_client.post(
        f"/api/v1/{instance_id}/queries/run-inline",
        json={
            "definition": {
                "name": "brake_parts",
                "mode": "collection",
                "returns": "Part",
                "result_shape": "entity",
                "where": {"result.properties.category": {"eq": "brakes"}},
            },
            "params": {},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["receipt_id"]
    assert payload["limit"] == 50

    queries = app_client.get(f"/api/v1/{instance_id}/queries")
    assert queries.status_code == 200
    assert "brake_parts" not in [query["name"] for query in queries.json()["items"]]


def test_inline_query_route_rejects_malformed_definition(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/queries/run-inline",
        json={
            "definition": {
                "name": "broken",
                "mode": "collection",
                "result_shape": "entity",
            },
            "params": {},
        },
    )

    assert response.status_code == 422


def test_inline_query_route_rejects_stringified_budget_caps(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/queries/run-inline",
        json={
            "definition": {
                "name": "too_many_paths",
                "mode": "traversal",
                "entry_point": "Vehicle",
                "traversal": [
                    {
                        "relationship": "fits",
                        "direction": "incoming",
                    }
                ],
                "returns": "Part",
                "result_shape": "path",
                "limit": "501",
                "max_paths": "5001",
                "max_paths_per_result": "101",
            },
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
        },
    )

    assert response.status_code == 400
    assert response.json()["message"] == "inline query limit must be <= 500"


def test_decision_record_routes_and_query_context_round_trip(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    created = app_client.post(
        f"/api/v1/{instance_id}/decision-records",
        json={
            "question": "Should we investigate vehicle impact?",
            "subject_type": "Vehicle",
            "subject_id": "V-2024-CIVIC-EX",
            "opened_by": "agent",
        },
    )
    assert created.status_code == 200
    decision_record_id = created.json()["record"]["decision_record_id"]

    fetched = app_client.get(f"/api/v1/{instance_id}/decision-records/{decision_record_id}")
    assert fetched.status_code == 200
    assert fetched.json()["record"]["question"] == "Should we investigate vehicle impact?"

    listed = app_client.get(
        f"/api/v1/{instance_id}/decision-records",
        params={"status": "open", "subject_type": "Vehicle"},
    )
    assert listed.status_code == 200
    assert [record["decision_record_id"] for record in listed.json()["items"]] == [
        decision_record_id
    ]

    query = app_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
            "decision_record_id": decision_record_id,
        },
    )
    assert query.status_code == 200

    events = app_client.get(
        f"/api/v1/{instance_id}/decision-records/events",
        params={"decision_record_id": decision_record_id},
    )
    assert events.status_code == 200
    event_payload = events.json()["items"]
    assert len(event_payload) == 1
    assert event_payload[0]["command"] == "query:parts_for_vehicle"
    assert event_payload[0]["receipt_id"] == query.json()["receipt_id"]
    assert event_payload[0]["surface"] == "http"

    finalized = app_client.post(
        f"/api/v1/{instance_id}/decision-records/{decision_record_id}/finalize",
        json={
            "final_decision": "Investigate affected vehicle parts",
            "decision_class": "recommended",
            "rationale": "Query returned impacted parts.",
        },
    )
    assert finalized.status_code == 200
    assert finalized.json()["record"]["status"] == "finalized"

    abandoned_record = app_client.post(
        f"/api/v1/{instance_id}/decision-records",
        json={"question": "Superseded question"},
    )
    abandoned_id = abandoned_record.json()["record"]["decision_record_id"]
    abandoned = app_client.post(
        f"/api/v1/{instance_id}/decision-records/{abandoned_id}/abandon",
        json={"reason": "Superseded"},
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["record"]["status"] == "abandoned"


def test_stats_and_inspect_routes_return_expected_shapes(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    stats = app_client.get(f"/api/v1/{instance_id}/stats")
    assert stats.status_code == 200
    stats_payload = stats.json()
    assert stats_payload["entity_count"] == 4
    assert stats_payload["edge_count"] == 3
    assert stats_payload["entity_counts"]["Vehicle"] == 2
    assert stats_payload["status_counts"] == {}

    inspect = app_client.get(f"/api/v1/{instance_id}/inspect/entity/Vehicle/V-2024-CIVIC-EX")
    assert inspect.status_code == 200
    inspect_payload = inspect.json()
    assert inspect_payload["found"] is True
    assert inspect_payload["metadata"] == {}
    assert inspect_payload["total_neighbors"] == 2
    assert inspect_payload["neighbors"][0]["relationship_type"] == "fits"
    assert inspect_payload["neighbors"][0]["metadata"]["provenance"]["source"] == "http_api"

    ontology = app_client.get(
        f"/api/v1/{instance_id}/inspect/ontology",
        params={"limit": 25},
    )
    assert ontology.status_code == 200
    ontology_payload = ontology.json()
    assert ontology_payload["view"] == "ontology"
    assert ontology_payload["payload"]["entity_count"] == 2
    assert ontology_payload["payload"]["relationship_count"] == 2


def test_query_discovery_routes_return_expected_shapes(
    app_client: TestClient,
    server_project: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)

    listed = app_client.get(f"/api/v1/{instance_id}/queries")
    assert listed.status_code == 200
    listed_payload = listed.json()
    assert listed_payload["items"]
    assert listed_payload["items"][0]["name"]
    assert listed_payload["items"][0]["mode"] in {"collection", "traversal"}

    described = app_client.get(f"/api/v1/{instance_id}/queries/parts_for_vehicle")
    assert described.status_code == 200
    described_payload = described.json()
    assert described_payload["name"] == "parts_for_vehicle"
    assert described_payload["mode"] == "traversal"
    assert described_payload["entry_point"] == "Vehicle"
    assert described_payload["required_params"] == ["vehicle_id"]


def test_trace_routes_return_trace_payloads(
    app_client: TestClient,
    server_project: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = ExecutionTrace(
        trace_id="TRC-route-001",
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
        provider_ref="tests.support.workflow_test_providers.provider",
        runtime="python",
        deterministic=True,
        side_effects=False,
        input_payload={"input": True},
        output_payload={"rows": 3},
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0.0,
    )
    with get_manager().get(instance_id).write_transaction() as uow:
        uow.receipts.save_trace(trace)

    fetched = app_client.get(f"/api/v1/{instance_id}/traces/{trace.trace_id}")
    listed = app_client.get(f"/api/v1/{instance_id}/traces", params={"workflow_name": "wf"})
    missing = app_client.get(f"/api/v1/{instance_id}/traces/TRC-missing")

    assert fetched.status_code == 200
    assert fetched.json()["output_payload"]["rows"] == 3
    assert listed.status_code == 200
    assert listed.json()["items"][0]["trace_id"] == trace.trace_id
    assert missing.status_code == 404
    assert missing.json()["error_type"] == "TraceNotFoundError"

    large_payload = {"body": "x" * 40000}
    large_trace = ExecutionTrace(
        trace_id="TRC-route-large",
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
    with get_manager().get(instance_id).write_transaction() as uow:
        uow.receipts.save_trace(large_trace)

    preview = app_client.get(f"/api/v1/{instance_id}/traces/{large_trace.trace_id}")

    assert preview.status_code == 200
    assert preview.json()["input_payload"] != large_payload
    assert preview.json()["input_payload_metadata"]["retention"] == "preview"
    assert preview.json()["input_payload_metadata"]["stored_inline"] is False


def test_workflow_run_route_rejects_proposal_workflows(
    app_client: TestClient,
    workflow_server_project: Path,
) -> None:
    instance_id = _init_instance(
        app_client,
        workflow_server_project,
        config_yaml=(workflow_server_project / "config.yaml").read_text(),
    )
    lock_response = app_client.post(f"/api/v1/{instance_id}/workflows/lock", json={})
    assert lock_response.status_code == 200

    response = app_client.post(
        f"/api/v1/{instance_id}/workflows/run",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )

    assert response.status_code == 400
    payload = response.json()
    assert "produces a governed proposal" in payload["message"]
    assert "cruxible propose --workflow propose_campaign_recommendations" in payload["message"]


def test_reload_config_route_updates_instance_path(
    app_client: TestClient,
    server_project: Path,
    tmp_path: Path,
):
    instance_id = _init_instance(app_client, server_project)
    new_config = tmp_path / "alt-config.yaml"
    new_config.write_text(CAR_PARTS_YAML.replace("car_parts_compatibility", "alt_name"))

    response = app_client.post(
        f"/api/v1/{instance_id}/config/reload",
        json={"config_yaml": new_config.read_text()},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["updated"] is True
    assert str(tmp_path / "alt-config.yaml") not in payload["config_path"]
    assert payload["config_path"].endswith("/.cruxible/configs/active.yaml")


def test_server_init_creates_daemon_owned_governed_instance(
    app_client: TestClient,
    server_project: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)
    record = get_registry().get(instance_id)
    assert record is not None
    assert record.location != str(server_project)
    expected_root = get_server_state_dir() / "instances" / instance_id
    assert Path(record.location) == expected_root

    instance = get_manager().get(instance_id)
    assert isinstance(instance, CruxibleInstance)
    assert instance.is_governed_mode()
    assert instance.get_root_path() == Path(record.location)
    assert instance.get_config_path() == (expected_root / ".cruxible" / "configs" / "active.yaml")
    assert instance.load_config().name == "car_parts_compatibility"


def test_source_artifact_relative_path_resolves_from_workspace_root(
    app_client: TestClient,
    server_project: Path,
) -> None:
    docs_dir = server_project / "docs"
    docs_dir.mkdir()
    evidence_path = docs_dir / "evidence.md"
    evidence_path.write_text("# Evidence\n\nWorkspace-local source text.\n")
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/source-artifacts/register",
        json={
            "source_path": "docs/evidence.md",
            "source_retention": "manifest_only",
            "label": "workspace evidence",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["original_uri"] == "docs/evidence.md"
    assert payload["chunks"]

    instance = get_manager().get(instance_id)
    store = instance.get_source_artifact_store()
    try:
        artifact = store.get_artifact(payload["source_artifact_id"])
    finally:
        store.close()
    assert artifact is not None
    assert artifact.local_path == str(evidence_path.resolve())
    assert Path(artifact.local_path).is_file()

    paragraph_chunk = next(
        chunk for chunk in payload["chunks"] if chunk["block_selector"] == "paragraph:1"
    )
    dereferenced = app_client.post(
        f"/api/v1/{instance_id}/source-evidence/dereference",
        json={
            "source_artifact_id": payload["source_artifact_id"],
            "chunk_id": paragraph_chunk["chunk_id"],
        },
    )

    assert dereferenced.status_code == 200
    dereferenced_payload = dereferenced.json()
    assert dereferenced_payload["status"] == "available"
    assert dereferenced_payload["body_origin"] == "local_path"
    assert dereferenced_payload["body"] == "Workspace-local source text."


@pytest.mark.parametrize(
    "payload",
    [
        {"source_artifact_id": "SRC-1"},
        {"source_artifact_id": "SRC-1", "chunk_id": ""},
        {"source_artifact_id": "SRC-1", "heading_path": ["Evidence"]},
        {
            "source_artifact_id": "SRC-1",
            "heading_path": [],
            "block_selector": "paragraph:1",
        },
        {
            "source_artifact_id": "SRC-1",
            "heading_path": ["Evidence"],
            "block_selector": "",
        },
        {"source_artifact_id": "", "chunk_id": "chunk-1"},
    ],
)
def test_source_artifact_dereference_rejects_incomplete_locators(
    app_client: TestClient,
    server_project: Path,
    payload: dict[str, object],
) -> None:
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/source-evidence/dereference",
        json=payload,
    )

    assert response.status_code == 422


def test_source_artifact_relative_path_preserves_original_uri(
    app_client: TestClient,
    server_project: Path,
) -> None:
    docs_dir = server_project / "docs"
    docs_dir.mkdir()
    evidence_path = docs_dir / "evidence.md"
    evidence_path.write_text("# Evidence\n\nWorkspace-local source text.\n")
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/source-artifacts/register",
        json={
            "source_path": "docs/evidence.md",
            "source_retention": "manifest_only",
            "original_uri": "https://example.test/evidence.md",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["original_uri"] == "https://example.test/evidence.md"

    instance = get_manager().get(instance_id)
    store = instance.get_source_artifact_store()
    try:
        artifact = store.get_artifact(payload["source_artifact_id"])
    finally:
        store.close()
    assert artifact is not None
    assert artifact.local_path == str(evidence_path.resolve())
    assert artifact.original_uri == "https://example.test/evidence.md"


def test_source_artifact_relative_path_cannot_escape_workspace(
    app_client: TestClient,
    server_project: Path,
    tmp_path: Path,
) -> None:
    outside_path = tmp_path / "outside.md"
    outside_path.write_text("# Outside\n\nShould not be registered.\n")
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/source-artifacts/register",
        json={
            "source_path": "../outside.md",
            "source_retention": "manifest_only",
        },
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error_type"] == "ConfigError"
    assert "source_path must stay within the registered workspace" in payload["message"]

    instance = get_manager().get(instance_id)
    with sqlite3.connect(instance.get_instance_dir() / "state.db") as conn:
        artifact_count = conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM source_artifact_chunks").fetchone()[0]
        archive_count = conn.execute("SELECT COUNT(*) FROM source_artifact_archives").fetchone()[0]
    assert artifact_count == 0
    assert chunk_count == 0
    assert archive_count == 0


def test_source_artifact_group_propose_rejects_malformed_source_evidence(
    app_client: TestClient,
    server_project: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/groups/propose",
        json={
            "relationship_type": "fits",
            "members": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1001",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-CIVIC-EX",
                    "relationship_type": "fits",
                    "source_evidence": [{"source_artifact_id": "SRC-1"}],
                }
            ],
        },
    )

    assert response.status_code == 422


def test_list_groups_status_filter_rejects_suppressed(
    app_client: TestClient,
    server_project: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)

    accepted = app_client.get(
        f"/api/v1/{instance_id}/groups",
        params={"status": "pending_review"},
    )
    rejected = app_client.get(
        f"/api/v1/{instance_id}/groups",
        params={"status": "suppressed"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["total"] == 0
    assert rejected.status_code == 422


def test_server_init_rejects_uploaded_config_with_unmaterialized_kit_refs(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    project = tmp_path / "plain-project"
    project.mkdir()
    config_yaml = (
        "version: '1.0'\n"
        "name: kit_ref_demo\n"
        "entity_types:\n"
        "  Demo:\n"
        "    properties:\n"
        "      demo_id: {type: string, primary_key: true}\n"
        "relationships: []\n"
        "contracts:\n"
        "  EmptyInput:\n"
        "    fields: {}\n"
        "providers:\n"
        "  p:\n"
        "    kind: function\n"
        "    contract_in: EmptyInput\n"
        "    contract_out: EmptyInput\n"
        "    ref: kit://providers/main.py::run\n"
        "    version: 1.0.0\n"
    )

    response = app_client.post(
        "/api/v1/instances",
        json={"root_dir": str(project), "config_yaml": config_yaml},
    )

    assert response.status_code == 400
    assert "Uploaded config contains kit:// provider refs" in response.json()["message"]


def test_repeated_init_returns_same_opaque_id(app_client: TestClient, server_project: Path):
    first = _init_instance(app_client, server_project)
    second = app_client.post("/api/v1/instances", json={"root_dir": str(server_project)}).json()
    assert second["instance_id"] == first
    assert second["status"] == "loaded"


def test_add_entity_returns_contract_shape(app_client: TestClient, server_project: Path):
    instance_id = _init_instance(app_client, server_project)
    response = app_client.post(
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
                    "metadata": {"source": "route-test"},
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["entities_added"] == 1
    lookup = app_client.get(f"/api/v1/{instance_id}/entities/Vehicle/V-1")
    assert lookup.status_code == 200
    assert lookup.json()["metadata"] == {"source": "route-test"}


def test_state_publish_overlay_and_status_routes(
    app_client: TestClient,
    server_project: Path,
    tmp_path: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)
    release_dir = tmp_path / "releases" / "current"

    publish = app_client.post(
        f"/api/v1/{instance_id}/state/publish",
        json={
            "transport_ref": f"file://{release_dir}",
            "state_id": "car-parts",
            "release_id": "v1.0.0",
            "compatibility": "data_only",
        },
    )
    assert publish.status_code == 200
    assert publish.json()["manifest"]["release_id"] == "v1.0.0"

    overlay_root = tmp_path / "cloned-model"
    overlay = app_client.post(
        "/api/v1/states/overlays",
        json={
            "transport_ref": f"file://{release_dir}",
            "root_dir": str(overlay_root),
        },
    )
    assert overlay.status_code == 200
    overlay_instance_id = overlay.json()["instance_id"]
    assert overlay_instance_id != str(overlay_root)

    status = app_client.get(f"/api/v1/{overlay_instance_id}/state/status")
    assert status.status_code == 200
    assert status.json()["upstream"]["state_id"] == "car-parts"
    assert status.json()["upstream"]["release_id"] == "v1.0.0"


def test_create_state_overlay_route_accepts_state_ref(
    app_client: TestClient,
    server_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = _init_instance(app_client, server_project)
    releases_dir = tmp_path / "releases"
    version_dir = releases_dir / "v1.0.0"
    latest_dir = releases_dir / "current"
    kit_dir = tmp_path / "kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(
        "\n".join(
            [
                'version: "1.0"',
                "name: car_parts_overlay",
                "extends: base-kit.yaml",
                "entity_types: {}",
                "relationships: []",
            ]
        )
        + "\n"
    )
    (kit_dir / "providers.py").write_text("KIT = True\n")
    _write_overlay_kit_manifest(kit_dir, "car-parts-overlay")
    publish = app_client.post(
        f"/api/v1/{instance_id}/state/publish",
        json={
            "transport_ref": f"file://{version_dir}",
            "state_id": "car-parts",
            "release_id": "v1.0.0",
            "compatibility": "data_only",
        },
    )
    assert publish.status_code == 200
    shutil.copytree(version_dir, latest_dir)
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "car-parts": StateCatalogEntry(
                alias="car-parts",
                base_transport_ref=f"file://{releases_dir}",
                latest_release="current",
                default_kit="car-parts-overlay",
            )
        },
    )
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"car-parts-overlay": f"file://{kit_dir}"},
    )

    overlay_root = tmp_path / "cloned-alias-model"
    overlay = app_client.post(
        "/api/v1/states/overlays",
        json={
            "state_ref": "car-parts",
            "root_dir": str(overlay_root),
        },
    )
    assert overlay.status_code == 200
    overlay_instance_id = overlay.json()["instance_id"]

    status = app_client.get(f"/api/v1/{overlay_instance_id}/state/status")
    assert status.status_code == 200
    assert status.json()["upstream"]["requested_source_ref"] == "car-parts"
    assert status.json()["upstream"]["requested_transport_ref"] == f"file://{latest_dir}"
    assert status.json()["upstream"]["transport_ref"] == f"file://{latest_dir}"
    record = get_registry().get(overlay_instance_id)
    assert record is not None
    assert (Path(record.location) / "providers.py").exists()


def test_create_state_overlay_route_requires_exactly_one_source(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    response = app_client.post(
        "/api/v1/states/overlays",
        json={
            "transport_ref": "file:///tmp/release",
            "state_ref": "car-parts",
            "root_dir": str(tmp_path / "overlay"),
        },
    )
    assert response.status_code == 422


def test_create_state_overlay_route_rejects_kit_and_no_kit(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    response = app_client.post(
        "/api/v1/states/overlays",
        json={
            "state_ref": "car-parts",
            "kit": "car-parts-overlay",
            "no_kit": True,
            "root_dir": str(tmp_path / "overlay"),
        },
    )
    assert response.status_code == 422


def test_permission_denied_returns_structured_403(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
):
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    admin_client = TestClient(create_app())
    instance_id = _init_instance(admin_client, server_project)

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    reset_client_cache()
    get_manager().clear()
    client = TestClient(create_app())
    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": [{"entity_type": "Vehicle", "entity_id": "V-1", "properties": {}}]},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "PermissionDeniedError"


def test_workflow_lock_requires_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_server_project: Path,
):
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    admin_client = TestClient(create_app())
    instance_id = _init_instance(admin_client, workflow_server_project)

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    reset_client_cache()
    get_manager().clear()
    client = TestClient(create_app())
    response = client.post(f"/api/v1/{instance_id}/workflows/lock")

    assert response.status_code == 403
    assert response.json()["error_type"] == "PermissionDeniedError"


def test_data_validation_error_returns_400_with_errors(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    response = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "UnknownEntity",
                    "entity_id": "V-1",
                    "properties": {"vehicle_id": "V-1"},
                }
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["error_type"] == "DataValidationError"
    assert response.json()["errors"]


def test_constraint_violation_returns_422_with_context(
    app_client: TestClient,
    server_project: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    instance_id = _init_instance(app_client, server_project)

    def raise_constraint(*_args, **_kwargs):
        raise ConstraintViolationError("constraint failed", violations=["mismatch"])

    monkeypatch.setattr(
        "cruxible_core.runtime.api.evaluate",
        raise_constraint,
    )
    response = app_client.post(f"/api/v1/{instance_id}/evaluate", json={})
    assert response.status_code == 422
    assert response.json()["context"]["violations"] == ["mismatch"]


def test_server_restart_can_reload_existing_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
):
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    client1 = TestClient(create_app())
    instance_id = _init_instance(client1, server_project)
    _seed_car_parts_state(client1, instance_id)

    get_manager().clear()
    reset_registry()
    client2 = TestClient(create_app())
    response = client2.get(f"/api/v1/{instance_id}/sample/Vehicle", params={"limit": 5})
    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_add_relationship_stamps_http_api_provenance(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    for entity in [
        {
            "entity_type": "Part",
            "entity_id": "BP-1",
            "properties": {
                "part_number": "BP-1",
                "name": "Brake Pad",
                "category": "brakes",
                "price": 49.99,
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
    ]:
        response = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": [entity]})
        assert response.status_code == 200

    response = app_client.post(
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
                    "evidence_refs": [
                        {
                            "source": "roadmap_doc",
                            "source_record_id": "section-p0",
                        }
                    ],
                    "evidence_rationale": "Accepted direct source-backed assertion.",
                }
            ]
        },
    )
    assert response.status_code == 200

    lookup = app_client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": "BP-1",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-1",
        },
    )
    assert lookup.status_code == 200
    metadata = lookup.json()["metadata"]
    assert metadata["provenance"]["source"] == "http_api"
    assert metadata["provenance"]["source_ref"] == "add_relationship"
    assert metadata["evidence"]["rationale"] == "Accepted direct source-backed assertion."
    assert metadata["evidence"]["evidence_refs"] == [
        {
            "source": "roadmap_doc",
            "source_record_id": "section-p0",
        }
    ]


def test_add_relationship_rejects_malformed_evidence_ref(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)

    response = app_client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "evidence_refs": [{"source": "roadmap_doc"}],
                }
            ]
        },
    )

    assert response.status_code in {400, 422}


def test_batch_direct_write_route_dry_run_and_apply(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    payload = {
        "entities": [
            {
                "entity_type": "Vehicle",
                "entity_id": "V-BATCH",
                "properties": {
                    "vehicle_id": "V-BATCH",
                    "year": 2026,
                    "make": "Honda",
                    "model": "Pilot",
                },
            },
            {
                "entity_type": "Part",
                "entity_id": "BP-BATCH",
                "properties": {
                    "part_number": "BP-BATCH",
                    "name": "Batch Pads",
                    "category": "brakes",
                },
            },
        ],
        "relationships": [
            {
                "from_type": "Part",
                "from_id": "BP-BATCH",
                "relationship_type": "fits",
                "to_type": "Vehicle",
                "to_id": "V-BATCH",
                "properties": {"verified": True, "source": "batch"},
                "shared_evidence_keys": ["doc"],
                "evidence_rationale": "Batch payload establishes the fitment.",
            }
        ],
        "shared_evidence": {
            "doc": {
                "evidence_refs": [{"source": "roadmap_doc", "source_record_id": "batch-section"}]
            }
        },
    }

    dry_run = app_client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={"payload": payload, "dry_run": True},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["valid"] is True
    missing = app_client.get(f"/api/v1/{instance_id}/inspect/entity/Vehicle/V-BATCH")
    assert missing.status_code == 200
    assert missing.json()["found"] is False

    applied = app_client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={"payload": payload, "dry_run": False},
    )
    assert applied.status_code == 200
    applied_payload = applied.json()
    assert applied_payload["entities_added"] == 2
    assert applied_payload["relationships_added"] == 1
    assert applied_payload["receipt_id"]

    lookup = app_client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": "BP-BATCH",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-BATCH",
        },
    )
    assert lookup.status_code == 200
    metadata = lookup.json()["metadata"]
    assert metadata["provenance"]["source_ref"] == "batch_direct_write"
    assert metadata["evidence"]["evidence_refs"][0]["source"] == "roadmap_doc"


def test_feedback_batch_route(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    for entity in [
        {
            "entity_type": "Part",
            "entity_id": "BP-1",
            "properties": {
                "part_number": "BP-1",
                "name": "Pads",
                "category": "brakes",
                "price": 49.99,
            },
        },
        {
            "entity_type": "Part",
            "entity_id": "BP-2",
            "properties": {
                "part_number": "BP-2",
                "name": "Rotor",
                "category": "brakes",
                "price": 19.99,
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
    ]:
        response = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": [entity]})
        assert response.status_code == 200

    response = app_client.post(
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
                },
                {
                    "from_type": "Part",
                    "from_id": "BP-2",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True},
                },
            ]
        },
    )
    assert response.status_code == 200

    query = app_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={"query_name": "parts_for_vehicle", "params": {"vehicle_id": "V-1"}},
    )
    assert query.status_code == 200
    receipt_id = query.json()["receipt_id"]

    from_query = app_client.post(
        f"/api/v1/{instance_id}/feedback/from-query",
        json={
            "receipt_id": receipt_id,
            "result_index": 0,
            "action": "approve",
            "source": "human",
            "reason_code": "route_review",
            "scope_hints": {"route": "feedback-from-query"},
        },
    )
    assert from_query.status_code == 200
    assert from_query.json()["applied"] is True

    batch = app_client.post(
        f"/api/v1/{instance_id}/feedback/batch",
        json={
            "source": "human",
            "items": [
                {
                    "receipt_id": receipt_id,
                    "action": "approve",
                    "target": {
                        "from_type": "Part",
                        "from_id": "BP-1",
                        "relationship_type": "fits",
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
                        "relationship_type": "fits",
                        "to_type": "Vehicle",
                        "to_id": "V-1",
                    },
                },
            ],
        },
    )
    assert batch.status_code == 200
    payload = batch.json()
    assert payload["total"] == 2
    assert payload["applied_count"] == 2
    assert payload["receipt_id"]


def test_workflow_propose_snapshot_and_overlay_round_trip(
    app_client: TestClient,
    workflow_server_project: Path,
):
    instance_id = _init_instance(app_client, workflow_server_project)

    for entity in [
        {
            "entity_type": "Campaign",
            "entity_id": "CMP-1",
            "properties": {"campaign_id": "CMP-1", "region": "north"},
        },
        {
            "entity_type": "Product",
            "entity_id": "SKU-123",
            "properties": {"sku": "SKU-123", "category": "beverages"},
        },
        {
            "entity_type": "Product",
            "entity_id": "SKU-456",
            "properties": {"sku": "SKU-456", "category": "beverages"},
        },
    ]:
        response = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": [entity]})
        assert response.status_code == 200

    lock = app_client.post(f"/api/v1/{instance_id}/workflows/lock")
    assert lock.status_code == 200

    propose = app_client.post(
        f"/api/v1/{instance_id}/workflows/propose",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )
    assert propose.status_code == 200
    group_id = propose.json()["group_id"]

    group_get = app_client.get(f"/api/v1/{instance_id}/groups/{group_id}")
    assert group_get.status_code == 200
    group_payload = group_get.json()
    assert group_payload["bucket_status"]["pending_group_id"] == group_id
    assert group_payload["member_review"]

    resolve = app_client.post(
        f"/api/v1/{instance_id}/groups/{group_id}/resolve",
        json={
            "action": "approve",
            "resolved_by": "human",
            "rationale": "looks good",
            "expected_pending_version": 1,
        },
    )
    assert resolve.status_code == 200
    assert resolve.json()["edges_created"] == 2

    list_edges = app_client.get(
        f"/api/v1/{instance_id}/list/edges",
        params={"relationship_type": "recommended_for"},
    )
    assert list_edges.status_code == 200
    edges = list_edges.json()["items"]
    assert len(edges) == 2
    assert all(edge["metadata"]["provenance"]["source"] == "group_resolve" for edge in edges)
    assert all(
        edge["metadata"]["provenance"]["source_ref"] == f"group:{group_id}" for edge in edges
    )
    lineage = app_client.get(
        f"/api/v1/{instance_id}/relationships/lineage",
        params={
            "from_type": "Campaign",
            "from_id": "CMP-1",
            "relationship_type": "recommended_for",
            "to_type": "Product",
            "to_id": "SKU-123",
        },
    )
    assert lineage.status_code == 200
    lineage_payload = lineage.json()
    assert lineage_payload["group"]["group_id"] == group_id
    assert "assertion" not in lineage_payload
    assert (
        lineage_payload["relationship"]["metadata"]["assertion"]["review"]["status"] == "approved"
    )
    assert lineage_payload["source_trace_ids"]

    snapshot = app_client.post(f"/api/v1/{instance_id}/snapshots", json={"label": "baseline"})
    assert snapshot.status_code == 200
    snapshot_id = snapshot.json()["snapshot"]["snapshot_id"]

    listed = app_client.get(f"/api/v1/{instance_id}/snapshots")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["snapshot_id"] == snapshot_id

    clone_root = workflow_server_project.parent / "cloned-server-project"
    clone = app_client.post(
        f"/api/v1/{instance_id}/snapshots/clone",
        json={"snapshot_id": snapshot_id, "root_dir": str(clone_root)},
    )
    assert clone.status_code == 200
    assert clone.json()["snapshot"]["snapshot_id"] == snapshot_id
    clone_instance_id = clone.json()["instance_id"]
    assert clone_instance_id != instance_id

    clone_list = app_client.get(
        f"/api/v1/{clone_instance_id}/list/edges",
        params={"relationship_type": "recommended_for"},
    )
    assert clone_list.status_code == 200
    assert clone_list.json()["total"] == 2


def test_workflow_routes_lock_plan_run_and_test(
    app_client: TestClient,
    workflow_server_project: Path,
):
    instance_id = _init_instance(app_client, workflow_server_project)

    for entity in [
        {
            "entity_type": "Campaign",
            "entity_id": "CMP-1",
            "properties": {"campaign_id": "CMP-1", "region": "north"},
        },
        {
            "entity_type": "Product",
            "entity_id": "SKU-123",
            "properties": {"sku": "SKU-123", "category": "beverages"},
        },
        {
            "entity_type": "Product",
            "entity_id": "SKU-456",
            "properties": {"sku": "SKU-456", "category": "beverages"},
        },
    ]:
        response = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": [entity]})
        assert response.status_code == 200

    lock = app_client.post(f"/api/v1/{instance_id}/workflows/lock")
    assert lock.status_code == 200

    plan = app_client.post(
        f"/api/v1/{instance_id}/workflows/plan",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )
    assert plan.status_code == 200
    assert plan.json()["plan"]["workflow"] == "propose_campaign_recommendations"

    run = app_client.post(
        f"/api/v1/{instance_id}/workflows/run",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )
    assert run.status_code == 400
    assert "produces a governed proposal" in run.json()["message"]

    propose = app_client.post(
        f"/api/v1/{instance_id}/workflows/propose",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )
    assert propose.status_code == 200
    assert propose.json()["receipt_id"].startswith("RCP-")
    assert propose.json()["output"]["members"]

    test = app_client.post(f"/api/v1/{instance_id}/workflows/test", json={"name": None})
    assert test.status_code == 200
    assert test.json()["failed"] == 0


def test_workflow_run_route_appends_decision_record_event(
    app_client: TestClient,
    tmp_path: Path,
    workflow_config_yaml: str,
):
    project = tmp_path / "workflow-run-project"
    project.mkdir()
    (project / "config.yaml").write_text(workflow_config_yaml)
    instance_id = _init_instance(app_client, project)

    entity = {
        "entity_type": "Product",
        "entity_id": "SKU-123",
        "properties": {
            "sku": "SKU-123",
            "category": "soda",
            "base_margin": 0.2,
        },
    }
    response = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": [entity]})
    assert response.status_code == 200

    lock = app_client.post(f"/api/v1/{instance_id}/workflows/lock")
    assert lock.status_code == 200

    created = app_client.post(
        f"/api/v1/{instance_id}/decision-records",
        json={"question": "Should the promo run?", "opened_by": "agent"},
    )
    assert created.status_code == 200
    decision_record_id = created.json()["record"]["decision_record_id"]

    run = app_client.post(
        f"/api/v1/{instance_id}/workflows/run",
        json={
            "workflow_name": "evaluate_promo",
            "input": {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
            "decision_record_id": decision_record_id,
        },
    )
    assert run.status_code == 200
    assert run.json()["receipt_id"].startswith("RCP-")

    events = app_client.get(
        f"/api/v1/{instance_id}/decision-records/events",
        params={"decision_record_id": decision_record_id},
    )
    assert events.status_code == 200
    assert len(events.json()["items"]) == 1
    assert events.json()["items"][0]["command"] == "workflow_run:evaluate_promo"
    assert events.json()["items"][0]["receipt_id"] == run.json()["receipt_id"]
    assert events.json()["items"][0]["surface"] == "http"


def test_workflow_propose_route_refreshes_same_signature_tuple_group(
    app_client: TestClient,
    workflow_server_project: Path,
    proposal_workflow_config_yaml: str,
):
    tuple_config_yaml = proposal_workflow_config_yaml.replace(
        "    proposal_policy:\n",
        "    proposal_identity: relationship_tuple\n    proposal_policy:\n",
        1,
    )
    instance_id = _init_instance(
        app_client,
        workflow_server_project,
        config_yaml=tuple_config_yaml,
    )

    for entity in [
        {
            "entity_type": "Campaign",
            "entity_id": "CMP-1",
            "properties": {"campaign_id": "CMP-1", "region": "north"},
        },
        {
            "entity_type": "Product",
            "entity_id": "SKU-123",
            "properties": {"sku": "SKU-123", "category": "beverages"},
        },
        {
            "entity_type": "Product",
            "entity_id": "SKU-456",
            "properties": {"sku": "SKU-456", "category": "beverages"},
        },
    ]:
        response = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": [entity]})
        assert response.status_code == 200

    lock = app_client.post(f"/api/v1/{instance_id}/workflows/lock")
    assert lock.status_code == 200, lock.text

    first = app_client.post(
        f"/api/v1/{instance_id}/workflows/propose",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )
    assert first.status_code == 200, first.text
    first_group_id = first.json()["group_id"]
    assert first_group_id

    update_campaign = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Campaign",
                    "entity_id": "CMP-1",
                    "properties": {"campaign_id": "CMP-1", "region": "south"},
                }
            ]
        },
    )
    assert update_campaign.status_code == 200, update_campaign.text

    second = app_client.post(
        f"/api/v1/{instance_id}/workflows/propose",
        json={
            "workflow_name": "propose_campaign_recommendations",
            "input": {"campaign_id": "CMP-1"},
        },
    )
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["group_id"] == first_group_id
    assert payload["group_status"] == "pending_review"
    assert payload["suppressed"] is False
    assert payload["suppressed_members"] == []


def test_workflow_apply_route_commits_canonical_snapshot(
    app_client: TestClient,
    canonical_workflow_project: Path,
):
    instance_id = _init_instance(app_client, canonical_workflow_project)

    lock = app_client.post(f"/api/v1/{instance_id}/workflows/lock")
    assert lock.status_code == 200

    preview = app_client.post(
        f"/api/v1/{instance_id}/workflows/run",
        json={"workflow_name": "build_reference", "input": {}},
    )
    assert preview.status_code == 200
    preview_json = preview.json()
    assert preview_json["mode"] == "preview"

    apply = app_client.post(
        f"/api/v1/{instance_id}/workflows/apply",
        json={
            "workflow_name": "build_reference",
            "input": {},
            "expected_apply_digest": preview_json["apply_digest"],
            "expected_head_snapshot_id": preview_json["head_snapshot_id"],
        },
    )
    assert apply.status_code == 200
    assert apply.json()["committed_snapshot_id"].startswith("snap_")


def test_server_routes_reject_unknown_instance_ids(
    app_client: TestClient,
    server_project: Path,
):
    _init_instance(app_client, server_project)

    response = app_client.post(
        "/api/v1/inst_missing/queries/run",
        json={"query_name": "parts_for_vehicle", "params": {"vehicle_id": "V-1"}},
    )

    assert response.status_code == 404
    assert response.json()["error_type"] == "InstanceNotFoundError"


def test_resolve_server_instance_id_rejects_raw_filesystem_paths(
    app_client: TestClient,
    server_project: Path,
):
    _init_instance(app_client, server_project)

    with pytest.raises(InstanceNotFoundError):
        resolve_server_instance_id(str(server_project))


def _run_canonical_workflow(client: TestClient, instance_id: str, workflow_name: str) -> None:
    preview = client.post(
        f"/api/v1/{instance_id}/workflows/run",
        json={"workflow_name": workflow_name, "input": {}},
    )
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["mode"] == "preview"

    apply = client.post(
        f"/api/v1/{instance_id}/workflows/apply",
        json={
            "workflow_name": workflow_name,
            "input": {},
            "expected_apply_digest": preview_payload["apply_digest"],
            "expected_head_snapshot_id": preview_payload["head_snapshot_id"],
        },
    )
    assert apply.status_code == 200
    assert apply.json()["committed_snapshot_id"]


def _approve_workflow_group(client: TestClient, instance_id: str, workflow_name: str) -> None:
    propose = client.post(
        f"/api/v1/{instance_id}/workflows/propose",
        json={"workflow_name": workflow_name, "input": {}},
    )
    assert propose.status_code == 200
    group_id = propose.json()["group_id"]
    assert group_id

    resolve = client.post(
        f"/api/v1/{instance_id}/groups/{group_id}/resolve",
        json={
            "action": "approve",
            "resolved_by": "human",
            "rationale": "smoke test",
            "expected_pending_version": 1,
        },
    )
    assert resolve.status_code == 200


def test_local_daemon_kev_smoke_runs_workflows_and_query(
    app_client: TestClient,
) -> None:
    instance_id = _init_instance(
        app_client,
        KEV_KIT_DIR,
        config_yaml=(KEV_KIT_DIR / "config.yaml").read_text(),
    )

    lock = app_client.post(f"/api/v1/{instance_id}/workflows/lock")
    assert lock.status_code == 200

    _run_canonical_workflow(app_client, instance_id, "build_public_kev_reference")
    _run_canonical_workflow(app_client, instance_id, "build_local_state")

    for workflow_name in [
        "propose_asset_products",
        "propose_asset_exposure",
    ]:
        _approve_workflow_group(app_client, instance_id, workflow_name)

    exposure_edges = app_client.get(
        f"/api/v1/{instance_id}/list/edges",
        params={"relationship_type": "asset_vulnerability_posture", "limit": 5},
    )
    assert exposure_edges.status_code == 200
    edge = exposure_edges.json()["items"][0]

    query = app_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={
            "query_name": "vulnerability_asset_context",
            "params": {"cve_id": edge["to_id"]},
        },
    )
    assert query.status_code == 200
    query_payload = query.json()
    assert query_payload["total"] > 0
    assert query_payload["receipt_id"]


def test_relationship_route_respects_dry_run(
    app_client: TestClient,
    server_project: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)
    entities = [
        {
            "entity_type": "Part",
            "entity_id": "BP-DRY-RUN",
            "properties": {
                "part_number": "BP-DRY-RUN",
                "name": "Dry Run Brake Pads",
                "category": "brakes",
                "price": 1.0,
            },
        },
        {
            "entity_type": "Vehicle",
            "entity_id": "V-DRY-RUN",
            "properties": {
                "vehicle_id": "V-DRY-RUN",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        },
    ]
    seeded = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": entities})
    assert seeded.status_code == 200

    relationship = {
        "from_type": "Part",
        "from_id": "BP-DRY-RUN",
        "relationship_type": "fits",
        "to_type": "Vehicle",
        "to_id": "V-DRY-RUN",
        "properties": {"verified": True, "source": "dry_run"},
    }
    dry_run = app_client.post(
        f"/api/v1/{instance_id}/relationships",
        json={"relationships": [relationship], "dry_run": True},
    )
    assert dry_run.status_code == 200
    assert dry_run.json() == {"added": 1, "updated": 0, "receipt_id": None}

    edges = app_client.get(
        f"/api/v1/{instance_id}/list/edges",
        params={"relationship_type": "fits"},
    )
    assert edges.status_code == 200
    assert edges.json()["total"] == 0


def test_entity_list_pagination_is_deterministic_and_honest(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    entities = [
        {
            "entity_type": "Vehicle",
            "entity_id": f"V-PAGE-{i:02d}",
            "properties": {
                "vehicle_id": f"V-PAGE-{i:02d}",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        }
        for i in range(5)
    ]
    seeded = app_client.post(f"/api/v1/{instance_id}/entities", json={"entities": entities})
    assert seeded.status_code == 200

    page1 = app_client.get(
        f"/api/v1/{instance_id}/list/entities",
        params={"entity_type": "Vehicle", "limit": 2, "offset": 0},
    ).json()
    page2 = app_client.get(
        f"/api/v1/{instance_id}/list/entities",
        params={"entity_type": "Vehicle", "limit": 2, "offset": 2},
    ).json()

    assert page1["total"] == 5
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert page1["truncated"] is True
    assert page2["offset"] == 2
    ids1 = [item["entity_id"] for item in page1["items"]]
    ids2 = [item["entity_id"] for item in page2["items"]]
    assert len(ids1) == 2 and len(ids2) == 2
    assert not set(ids1) & set(ids2)
    assert ids1 == sorted(ids1)

    tail = app_client.get(
        f"/api/v1/{instance_id}/list/entities",
        params={"entity_type": "Vehicle", "limit": 2, "offset": 4},
    ).json()
    assert len(tail["items"]) == 1
    assert tail["truncated"] is False


def test_decision_record_pagination_orders_and_counts(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    for i in range(4):
        created = app_client.post(
            f"/api/v1/{instance_id}/decision-records",
            json={"question": f"Question {i}?"},
        )
        assert created.status_code == 200

    page1 = app_client.get(
        f"/api/v1/{instance_id}/decision-records",
        params={"limit": 3, "offset": 0},
    ).json()
    page2 = app_client.get(
        f"/api/v1/{instance_id}/decision-records",
        params={"limit": 3, "offset": 3},
    ).json()

    assert page1["total"] == 4
    assert page1["truncated"] is True
    assert len(page1["items"]) == 3
    assert len(page2["items"]) == 1
    assert page2["truncated"] is False
    ids1 = {record["decision_record_id"] for record in page1["items"]}
    ids2 = {record["decision_record_id"] for record in page2["items"]}
    assert not ids1 & ids2


def test_snapshot_and_query_discovery_pagination(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    for label in ("first", "second", "third"):
        created = app_client.post(
            f"/api/v1/{instance_id}/snapshots",
            json={"label": label},
        )
        assert created.status_code == 200

    snaps = app_client.get(
        f"/api/v1/{instance_id}/snapshots",
        params={"limit": 2, "offset": 0},
    ).json()
    assert snaps["total"] >= 3
    assert len(snaps["items"]) == 2
    assert snaps["truncated"] is True

    queries = app_client.get(
        f"/api/v1/{instance_id}/queries",
        params={"limit": 1, "offset": 0},
    ).json()
    assert queries["total"] >= 1
    assert len(queries["items"]) == 1
    names = [item["name"] for item in queries["items"]]
    assert names == sorted(names)


def test_relationship_dry_run_validates_without_persisting(
    app_client: TestClient,
    server_project: Path,
):
    instance_id = _init_instance(app_client, server_project)
    _seed_car_parts_state(app_client, instance_id)

    response = app_client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-1002",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-2024-ACCORD-SPORT",
                    "properties": {"verified": True},
                }
            ],
            "dry_run": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["added"] == 1
    assert payload["updated"] == 0
    assert payload["receipt_id"] is None

    lookup = app_client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": "BP-1002",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-ACCORD-SPORT",
        },
    )
    assert lookup.json()["found"] is False
