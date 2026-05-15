"""Tests for FastAPI server routes."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.errors import ConstraintViolationError, InstanceNotFoundError
from cruxible_core.kits.world_refs import WorldCatalogEntry
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
    target_world: str = "car-parts",
) -> None:
    (kit_dir / "cruxible-kit.yaml").write_text(
        "\n".join(
            [
                "schema_version: cruxible.kit.v1",
                f"kit_id: {kit_id}",
                "version: 0.2.0",
                "role: overlay",
                f"target_world: {target_world}",
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
            "relationship": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-CIVIC-EX",
            "properties": {"verified": True, "source": "catalog"},
        },
        {
            "from_type": "Part",
            "from_id": "BP-1001",
            "relationship": "fits",
            "to_type": "Vehicle",
            "to_id": "V-2024-ACCORD-SPORT",
            "properties": {"verified": True, "source": "catalog"},
        },
        {
            "from_type": "Part",
            "from_id": "BP-1002",
            "relationship": "fits",
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


def test_server_info_endpoint_returns_live_metadata(
    app_client: TestClient,
    server_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_AGENT_MODE", "1")
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    reset_permissions()
    _init_instance(app_client, server_project)

    response = app_client.get("/api/v1/server/info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_mode"] is True
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
        f"/api/v1/{instance_id}/query",
        json={
            "query_name": "parts_for_vehicle",
            "params": {"vehicle_id": "V-2024-CIVIC-EX"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_results"] == 2
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
    assert [record["decision_record_id"] for record in listed.json()["records"]] == [
        decision_record_id
    ]

    query = app_client.post(
        f"/api/v1/{instance_id}/query",
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
    event_payload = events.json()["events"]
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

    inspect = app_client.get(
        f"/api/v1/{instance_id}/inspect/entity/Vehicle/V-2024-CIVIC-EX"
    )
    assert inspect.status_code == 200
    inspect_payload = inspect.json()
    assert inspect_payload["found"] is True
    assert inspect_payload["total_neighbors"] == 2
    assert inspect_payload["neighbors"][0]["relationship_type"] == "fits"

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
    assert listed_payload["queries"]
    assert listed_payload["queries"][0]["name"]

    described = app_client.get(f"/api/v1/{instance_id}/queries/parts_for_vehicle")
    assert described.status_code == 200
    described_payload = described.json()
    assert described_payload["name"] == "parts_for_vehicle"
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
    store = get_manager().get(instance_id).get_receipt_store()
    try:
        store.save_trace(trace)
    finally:
        store.close()

    fetched = app_client.get(f"/api/v1/{instance_id}/traces/{trace.trace_id}")
    listed = app_client.get(f"/api/v1/{instance_id}/traces", params={"workflow_name": "wf"})
    missing = app_client.get(f"/api/v1/{instance_id}/traces/TRC-missing")

    assert fetched.status_code == 200
    assert fetched.json()["output_payload"]["rows"] == 3
    assert listed.status_code == 200
    assert listed.json()["traces"][0]["trace_id"] == trace.trace_id
    assert missing.status_code == 404
    assert missing.json()["error_type"] == "TraceNotFoundError"


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
    assert instance.get_config_path() == (
        expected_root / ".cruxible" / "configs" / "active.yaml"
    )
    assert instance.load_config().name == "car_parts_compatibility"


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
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["entities_added"] == 1


def test_world_publish_overlay_and_status_routes(
    app_client: TestClient,
    server_project: Path,
    tmp_path: Path,
) -> None:
    instance_id = _init_instance(app_client, server_project)
    release_dir = tmp_path / "releases" / "current"

    publish = app_client.post(
        f"/api/v1/{instance_id}/world/publish",
        json={
            "transport_ref": f"file://{release_dir}",
            "world_id": "car-parts",
            "release_id": "v1.0.0",
            "compatibility": "data_only",
        },
    )
    assert publish.status_code == 200
    assert publish.json()["manifest"]["release_id"] == "v1.0.0"

    overlay_root = tmp_path / "cloned-model"
    overlay = app_client.post(
        "/api/v1/worlds/overlays",
        json={
            "transport_ref": f"file://{release_dir}",
            "root_dir": str(overlay_root),
        },
    )
    assert overlay.status_code == 200
    overlay_instance_id = overlay.json()["instance_id"]
    assert overlay_instance_id != str(overlay_root)

    status = app_client.get(f"/api/v1/{overlay_instance_id}/world/status")
    assert status.status_code == 200
    assert status.json()["upstream"]["world_id"] == "car-parts"
    assert status.json()["upstream"]["release_id"] == "v1.0.0"


def test_create_world_overlay_route_accepts_world_ref(
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
                "kind: world_model",
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
        f"/api/v1/{instance_id}/world/publish",
        json={
            "transport_ref": f"file://{version_dir}",
            "world_id": "car-parts",
            "release_id": "v1.0.0",
            "compatibility": "data_only",
        },
    )
    assert publish.status_code == 200
    shutil.copytree(version_dir, latest_dir)
    monkeypatch.setattr(
        "cruxible_core.kits.world_refs.get_world_catalog",
        lambda: {
            "car-parts": WorldCatalogEntry(
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
        "/api/v1/worlds/overlays",
        json={
            "world_ref": "car-parts",
            "root_dir": str(overlay_root),
        },
    )
    assert overlay.status_code == 200
    overlay_instance_id = overlay.json()["instance_id"]

    status = app_client.get(f"/api/v1/{overlay_instance_id}/world/status")
    assert status.status_code == 200
    assert status.json()["upstream"]["requested_source_ref"] == "car-parts"
    assert status.json()["upstream"]["requested_transport_ref"] == f"file://{latest_dir}"
    assert status.json()["upstream"]["transport_ref"] == f"file://{latest_dir}"
    record = get_registry().get(overlay_instance_id)
    assert record is not None
    assert (Path(record.location) / "providers.py").exists()


def test_create_world_overlay_route_requires_exactly_one_source(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    response = app_client.post(
        "/api/v1/worlds/overlays",
        json={
            "transport_ref": "file:///tmp/release",
            "world_ref": "car-parts",
            "root_dir": str(tmp_path / "overlay"),
        },
    )
    assert response.status_code == 422


def test_create_world_overlay_route_rejects_kit_and_no_kit(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    response = app_client.post(
        "/api/v1/worlds/overlays",
        json={
            "world_ref": "car-parts",
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
    assert response.json()["count"] == 2


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
                    "relationship": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True},
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
    props = lookup.json()["properties"]
    assert props["_provenance"]["source"] == "http_api"
    assert props["_provenance"]["source_ref"] == "cruxible_add_relationship"


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
                    "relationship": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True},
                },
                {
                    "from_type": "Part",
                    "from_id": "BP-2",
                    "relationship": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-1",
                    "properties": {"verified": True},
                },
            ]
        },
    )
    assert response.status_code == 200

    query = app_client.post(
        f"/api/v1/{instance_id}/query",
        json={"query_name": "parts_for_vehicle", "params": {"vehicle_id": "V-1"}},
    )
    assert query.status_code == 200
    receipt_id = query.json()["receipt_id"]

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
    assert all(edge["properties"]["_provenance"]["source"] == "group_resolve" for edge in edges)
    assert all(
        edge["properties"]["_provenance"]["source_ref"] == f"group:{group_id}" for edge in edges
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
    assert lineage_payload["source_trace_ids"]

    snapshot = app_client.post(f"/api/v1/{instance_id}/snapshots", json={"label": "baseline"})
    assert snapshot.status_code == 200
    snapshot_id = snapshot.json()["snapshot"]["snapshot_id"]

    listed = app_client.get(f"/api/v1/{instance_id}/snapshots")
    assert listed.status_code == 200
    assert listed.json()["snapshots"][0]["snapshot_id"] == snapshot_id

    clone_root = workflow_server_project.parent / "cloned-server-project"
    clone = app_client.post(
        f"/api/v1/{instance_id}/clone",
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
    assert len(events.json()["events"]) == 1
    assert events.json()["events"][0]["command"] == "workflow_run:evaluate_promo"
    assert events.json()["events"][0]["receipt_id"] == run.json()["receipt_id"]
    assert events.json()["events"][0]["surface"] == "http"


def test_workflow_propose_route_returns_suppressed_members(
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
    assert payload["group_id"] is None
    assert payload["suppressed"] is True
    assert len(payload["suppressed_members"]) == 2
    sku_123 = next(
        item for item in payload["suppressed_members"] if item["to_id"] == "SKU-123"
    )
    assert sku_123["relationship_type"] == "recommended_for"
    assert sku_123["reason"] == "pending_proposal"
    assert sku_123["existing_group_id"] == first_group_id
    assert sku_123["existing_group_status"] == "pending_review"


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
        "/api/v1/inst_missing/query",
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
        params={"relationship_type": "asset_exposed_to_vulnerability", "limit": 5},
    )
    assert exposure_edges.status_code == 200
    edge = exposure_edges.json()["items"][0]

    query = app_client.post(
        f"/api/v1/{instance_id}/query",
        json={
            "query_name": "exposed_assets_for_vulnerability",
            "params": {"cve_id": edge["to_id"]},
        },
    )
    assert query.status_code == 200
    query_payload = query.json()
    assert query_payload["total_results"] > 0
    assert query_payload["receipt_id"]
