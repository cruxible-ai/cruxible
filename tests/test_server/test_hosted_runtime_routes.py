"""Hosted runtime route tests: bootstrap claim, runtime credentials, hosted init, scoping.

Extracted from the private cloud branch's test_routes.py during the hosted-runtime
hardening extraction; uses the same app/server fixtures as test_routes.py.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.kits.state_refs import StateCatalogEntry
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.app import create_app
from cruxible_core.server.auth import EFFECTIVE_PERMISSION_MODE_HEADER
from cruxible_core.server.config import get_server_state_dir
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_REFERENCE_KIT_DIR = REPO_ROOT / "kits" / "kev-reference"
KEV_KIT_DIR = REPO_ROOT / "kits" / "kev-triage"
KEV_PUBLIC_DATA_FILES = (
    KEV_REFERENCE_KIT_DIR / "data" / "known_exploited_vulnerabilities.csv",
    KEV_REFERENCE_KIT_DIR / "data" / "epss_kev_nvd.csv",
    KEV_REFERENCE_KIT_DIR / "data" / "nvd_kev_cves.json",
)


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


def _write_standalone_kit_manifest(kit_dir: Path, kit_id: str) -> None:
    (kit_dir / "cruxible-kit.yaml").write_text(
        "\n".join(
            [
                "schema_version: cruxible.kit.v1",
                f"kit_id: {kit_id}",
                "version: 0.2.0",
                "role: standalone",
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
        if token is not None:
            monkeypatch.setenv("CRUXIBLE_SERVER_TOKEN", token)
        else:
            monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    else:
        monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
        monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
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


def _runtime_credential_headers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_id: str,
    permission_mode: PermissionMode,
) -> dict[str, str]:
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label=f"{permission_mode.name.lower()} credential",
        permission_mode=permission_mode,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    return {"Authorization": f"Bearer {created.token}"}


def _valid_vehicle_entity(entity_id: str) -> dict[str, object]:
    return {
        "entity_type": "Vehicle",
        "entity_id": entity_id,
        "properties": {
            "vehicle_id": entity_id,
            "year": 2026,
            "make": "Honda",
            "model": "Civic",
        },
    }


def _actor_context(*, actor_id: str = "usr_1", operation_id: str = "op_1") -> dict[str, str]:
    return {
        "actor_type": "human_user",
        "actor_id": actor_id,
        "org_id": "org_1",
        "operation_id": operation_id,
        "timestamp": "2026-06-05T12:00:00Z",
        "request_id": "req_1",
    }


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


def test_product_bearer_token_cannot_authenticate_directly_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch, auth_enabled=True)

    response = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Authorization": "Bearer cruxible_cloud_public_key"},
    )

    assert response.status_code == 401
    assert response.json()["error_type"] == "AuthenticationError"


def test_runtime_credential_is_scoped_to_one_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_a = _init_instance(client, server_project)

    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / "config.yaml").write_text(CAR_PARTS_YAML)
    instance_b = _init_instance(client, other_root)

    created = get_runtime_credential_store().create_credential(
        instance_id=instance_a,
        label="instance-a-reader",
        permission_mode=PermissionMode.READ_ONLY,
        created_by="test",
    )

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {created.token}"}

    allowed = client.get(f"/api/v1/{instance_a}/schema", headers=headers)
    assert allowed.status_code == 200

    denied = client.get(f"/api/v1/{instance_b}/schema", headers=headers)
    assert denied.status_code == 403
    denied_payload = denied.json()
    assert denied_payload["error_type"] == "InstanceScopeError"
    assert denied_payload["context"]["instance_id"] == instance_b
    assert denied_payload["context"]["credential_scope"] == instance_a


def test_runtime_credential_scope_blocks_other_instance_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_a = _init_instance(client, server_project)

    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / "config.yaml").write_text(CAR_PARTS_YAML)
    instance_b = _init_instance(client, other_root)

    created = get_runtime_credential_store().create_credential(
        instance_id=instance_a,
        label="instance-a-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {created.token}"}

    reload_own = client.post(
        "/api/v1/instances",
        json={"root_dir": str(server_project)},
        headers=headers,
    )
    assert reload_own.status_code == 200
    assert reload_own.json()["instance_id"] == instance_a

    reload_other = client.post(
        "/api/v1/instances",
        json={"root_dir": str(other_root)},
        headers=headers,
    )
    assert reload_other.status_code == 403
    reload_payload = reload_other.json()
    assert reload_payload["error_type"] == "InstanceScopeError"
    assert reload_payload["context"]["instance_id"] == instance_b
    assert reload_payload["context"]["credential_scope"] == instance_a

    new_root = tmp_path / "new-project"
    new_root.mkdir()
    (new_root / "config.yaml").write_text(CAR_PARTS_YAML)
    create_other = client.post(
        "/api/v1/instances",
        json={"root_dir": str(new_root), "config_yaml": CAR_PARTS_YAML},
        headers=headers,
    )
    assert create_other.status_code == 403
    create_payload = create_other.json()
    assert create_payload["error_type"] == "InstanceScopeError"
    assert create_payload["context"]["instance_id"] == "new_instance"
    assert create_payload["context"]["credential_scope"] == instance_a
    assert get_registry().count_instances() == 2


def test_runtime_credential_lifecycle_uses_permission_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label="instance-a-reader",
        permission_mode=PermissionMode.READ_ONLY,
        created_by="test",
    )

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)

    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(server_project)},
        headers={"Authorization": f"Bearer {created.token}"},
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_type"] == "PermissionDeniedError"
    assert payload["context"]["tool_name"] == "cruxible_governed_instance_lifecycle"
    assert payload["context"]["current_mode"] == "READ_ONLY"
    assert payload["context"]["required_mode"] == "ADMIN"


def test_read_only_runtime_credential_can_read_but_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.READ_ONLY,
    )

    read = client.get(f"/api/v1/{instance_id}/schema", headers=headers)
    assert read.status_code == 200

    governed_write = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "read-only-denied"},
        headers=headers,
    )
    assert governed_write.status_code == 403
    assert governed_write.json()["context"]["required_mode"] == "GOVERNED_WRITE"

    graph_write = client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": [_valid_vehicle_entity("V-RO-DENIED")]},
        headers=headers,
    )
    assert graph_write.status_code == 403
    assert graph_write.json()["context"]["required_mode"] == "GRAPH_WRITE"

    admin = client.post(
        "/api/v1/instances",
        json={"root_dir": str(server_project)},
        headers=headers,
    )
    assert admin.status_code == 403
    assert admin.json()["context"]["required_mode"] == "ADMIN"


def test_governed_write_runtime_credential_cannot_graph_write_or_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )

    read = client.get(f"/api/v1/{instance_id}/schema", headers=headers)
    assert read.status_code == 200

    governed_write = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "governed-write-allowed", "actor_context": _actor_context()},
        headers=headers,
    )
    assert governed_write.status_code == 200
    assert governed_write.json()["snapshot"]["snapshot_id"]

    graph_write = client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": [_valid_vehicle_entity("V-GW-DENIED")]},
        headers=headers,
    )
    assert graph_write.status_code == 403
    assert graph_write.json()["context"]["required_mode"] == "GRAPH_WRITE"

    admin = client.post(
        "/api/v1/instances",
        json={"root_dir": str(server_project)},
        headers=headers,
    )
    assert admin.status_code == 403
    assert admin.json()["context"]["required_mode"] == "ADMIN"


def test_runtime_credential_effective_permission_header_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = {
        **_runtime_credential_headers(
            monkeypatch,
            instance_id=instance_id,
            permission_mode=PermissionMode.ADMIN,
        ),
        EFFECTIVE_PERMISSION_MODE_HEADER: "read_only",
    }

    read = client.get(f"/api/v1/{instance_id}/schema", headers=headers)
    assert read.status_code == 200

    governed_write = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "denied", "actor_context": _actor_context()},
        headers=headers,
    )
    assert governed_write.status_code == 403
    assert governed_write.json()["context"]["current_mode"] == "READ_ONLY"
    assert governed_write.json()["context"]["required_mode"] == "GOVERNED_WRITE"


def test_runtime_credential_effective_permission_header_cannot_escalate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = {
        **_runtime_credential_headers(
            monkeypatch,
            instance_id=instance_id,
            permission_mode=PermissionMode.READ_ONLY,
        ),
        EFFECTIVE_PERMISSION_MODE_HEADER: "admin",
    }

    response = client.get(f"/api/v1/{instance_id}/schema", headers=headers)

    assert response.status_code == 401
    assert response.json()["error_type"] == "AuthenticationError"


def test_effective_permission_header_is_rejected_without_runtime_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)

    response = client.get(
        f"/api/v1/{instance_id}/schema",
        headers={EFFECTIVE_PERMISSION_MODE_HEADER: "read_only"},
    )

    assert response.status_code == 401
    assert response.json()["error_type"] == "AuthenticationError"


def test_runtime_credential_governed_write_requires_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )

    missing = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "missing-actor"},
        headers=headers,
    )
    assert missing.status_code == 400
    assert missing.json()["error_type"] == "ConfigError"
    assert missing.json()["message"] == "hosted governed actor context is required"

    supplied = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "with-actor", "actor_context": _actor_context()},
        headers=headers,
    )
    assert supplied.status_code == 200
    assert supplied.json()["snapshot"]["snapshot_id"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/v1/{instance_id}/decision-records", {"question": "Decide?"}),
        (
            "post",
            "/api/v1/{instance_id}/decision-records/DR-missing/finalize",
            {
                "final_decision": "Ship it",
                "decision_class": "recommended",
            },
        ),
        (
            "post",
            "/api/v1/{instance_id}/decision-records/DR-missing/abandon",
            {"reason": "superseded"},
        ),
        ("post", "/api/v1/{instance_id}/workflows/test", {"name": None}),
        (
            "post",
            "/api/v1/{instance_id}/constraints",
            {
                "name": "requires_year",
                "rule": "entity.properties.year is not None",
            },
        ),
        (
            "post",
            "/api/v1/{instance_id}/decision-policies",
            {
                "name": "review-fits",
                "applies_to": "query",
                "relationship_type": "fits",
                "effect": "require_review",
            },
        ),
        (
            "post",
            "/api/v1/{instance_id}/state/pull/apply",
            {"expected_apply_digest": "sha256:test"},
        ),
    ],
)
def test_runtime_credential_governed_write_routes_require_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
    method: str,
    path: str,
    payload: dict[str, object],
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )

    response = getattr(client, method)(
        path.format(instance_id=instance_id),
        json=payload,
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error_type"] == "ConfigError"
    assert response.json()["message"] == "hosted governed actor context is required"


def test_decision_record_routes_persist_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )

    opened_actor = _actor_context(actor_id="usr_open", operation_id="op_open")
    created = client.post(
        f"/api/v1/{instance_id}/decision-records",
        json={"question": "Decide?", "actor_context": opened_actor},
        headers=headers,
    )
    assert created.status_code == 200
    record = created.json()["record"]
    assert record["opened_actor_context"]["actor_id"] == "usr_open"
    fetched = client.get(
        f"/api/v1/{instance_id}/decision-records/{record['decision_record_id']}",
        headers=headers,
    )
    assert fetched.status_code == 200
    fetched_record = fetched.json()["record"]
    assert fetched_record["opened_actor_context"]["actor_id"] == "usr_open"
    assert fetched_record["opened_actor_context"]["operation_id"] == "op_open"
    assert fetched_record["finalized_actor_context"] is None

    finalized_actor = _actor_context(actor_id="usr_finalize", operation_id="op_finalize")
    finalized = client.post(
        f"/api/v1/{instance_id}/decision-records/{record['decision_record_id']}/finalize",
        json={
            "final_decision": "Ship it",
            "decision_class": "recommended",
            "actor_context": finalized_actor,
        },
        headers=headers,
    )
    assert finalized.status_code == 200
    assert finalized.json()["record"]["finalized_actor_context"]["actor_id"] == "usr_finalize"
    fetched_final = client.get(
        f"/api/v1/{instance_id}/decision-records/{record['decision_record_id']}",
        headers=headers,
    )
    assert fetched_final.status_code == 200
    fetched_final_record = fetched_final.json()["record"]
    assert fetched_final_record["opened_actor_context"]["actor_id"] == "usr_open"
    assert fetched_final_record["opened_actor_context"]["operation_id"] == "op_open"
    assert fetched_final_record["finalized_actor_context"]["actor_id"] == "usr_finalize"
    assert fetched_final_record["finalized_actor_context"]["operation_id"] == "op_finalize"


def test_actor_context_extra_fields_are_rejected_by_http_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )
    actor_context = {**_actor_context(), "unexpected": "nope"}

    response = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "extra-actor-field", "actor_context": actor_context},
        headers=headers,
    )

    assert response.status_code == 422
    assert "unexpected" in response.text


def test_graph_write_runtime_credential_cannot_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
    )

    graph_write = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [_valid_vehicle_entity("V-GRAPH-ALLOWED")],
            "actor_context": _actor_context(),
        },
        headers=headers,
    )
    assert graph_write.status_code == 200
    assert graph_write.json()["entities_added"] == 1

    admin = client.post(
        "/api/v1/instances",
        json={"root_dir": str(server_project)},
        headers=headers,
    )
    assert admin.status_code == 403
    assert admin.json()["context"]["required_mode"] == "ADMIN"


def test_admin_runtime_credential_can_manage_scoped_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
    )

    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(server_project)},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["instance_id"] == instance_id


def test_runtime_credential_permission_scope_overrides_global_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
    )
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()

    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [_valid_vehicle_entity("V-REQUEST-SCOPE")],
            "actor_context": _actor_context(),
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["entities_added"] == 1


def test_runtime_bootstrap_claim_returns_admin_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")

    response = client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"credential_id", "instance_id", "permission_mode", "token"}
    assert payload["instance_id"] == instance_id
    assert payload["permission_mode"] == "admin"
    assert payload["token"].startswith(f"crt_{payload['credential_id']}_")

    stored = get_runtime_credential_store().get(payload["credential_id"])
    assert stored is not None
    assert stored.instance_id == instance_id
    assert stored.permission_mode is PermissionMode.ADMIN
    assert stored.token_hash != payload["token"]

    reused = client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )
    assert reused.status_code == 401
    assert reused.json()["error_type"] == "AuthenticationError"


def test_runtime_bootstrap_claim_invalid_secret_fails_without_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")

    response = client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json()["error_type"] == "AuthenticationError"
    assert get_runtime_credential_store().list_for_instance(instance_id) == []


def test_runtime_bootstrap_claim_token_is_scoped_to_target_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_a = _init_instance(client, server_project)

    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / "config.yaml").write_text(CAR_PARTS_YAML)
    instance_b = _init_instance(client, other_root)

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")

    response = client.post(
        f"/api/v1/{instance_a}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['token']}"}

    allowed = client.get(f"/api/v1/{instance_a}/schema", headers=headers)
    assert allowed.status_code == 200

    denied = client.get(f"/api/v1/{instance_b}/schema", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error_type"] == "InstanceScopeError"


def test_hosted_instance_init_from_kit_is_idempotent_and_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    kit_dir = tmp_path / "standalone-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(CAR_PARTS_YAML)
    _write_standalone_kit_manifest(kit_dir, "car-parts-hosted")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"car-parts-hosted": f"file://{kit_dir}"},
    )
    payload = {
        "instance_id": "inst_hostedkit",
        "source_type": "kit",
        "kit_ref": "car-parts-hosted",
    }

    created = client.post("/api/v1/runtime/instances", json=payload)
    assert created.status_code == 200
    assert created.json()["instance_id"] == "inst_hostedkit"
    assert created.json()["status"] == "initialized"
    assert created.json()["source_ref"] == "car-parts-hosted"

    repeated = client.post("/api/v1/runtime/instances", json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "already_initialized"

    changed = client.post(
        "/api/v1/runtime/instances",
        json={**payload, "kit_ref": f"file://{kit_dir}"},
    )
    assert changed.status_code == 400
    assert "different material" in changed.json()["message"]

    reset_registry()
    get_manager().clear()
    record = get_registry().get("inst_hostedkit")
    assert record is not None
    loaded = get_manager().get("inst_hostedkit")
    assert loaded.load_config().name == "car_parts_compatibility"


def test_hosted_instance_init_rejects_invalid_kit_without_registry_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    before_count = get_registry().count_instances()
    instance_id = "inst_badkit"

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": instance_id,
            "source_type": "kit",
            "kit_ref": "missing-kit",
        },
    )

    assert response.status_code == 400
    assert response.json()["error_type"] == "ConfigError"
    assert get_registry().count_instances() == before_count
    assert get_registry().get(instance_id) is None
    assert not (get_server_state_dir() / "instances" / instance_id).exists()


def test_hosted_instance_init_from_reference_transport_without_overlay(
    tmp_path: Path,
    server_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    source_instance_id = _init_instance(client, server_project)
    release_dir = tmp_path / "releases" / "v1.0.0"
    publish = client.post(
        f"/api/v1/{source_instance_id}/state/publish",
        json={
            "transport_ref": f"file://{release_dir}",
            "state_id": "car-parts",
            "release_id": "v1.0.0",
            "compatibility": "data_only",
        },
    )
    assert publish.status_code == 200

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_reference",
            "source_type": "reference_model",
            "transport_ref": f"file://{release_dir}",
            "no_overlay_kit": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["instance_id"] == "inst_reference"
    assert payload["source_ref"] == f"file://{release_dir}"
    assert payload["resolved_source_ref"] == f"file://{release_dir}"
    assert payload["manifest"]["release_id"] == "v1.0.0"

    status = client.get("/api/v1/inst_reference/state/status")
    assert status.status_code == 200
    assert status.json()["upstream"]["state_id"] == "car-parts"


def test_hosted_instance_init_from_state_ref_and_overlay_kit(
    tmp_path: Path,
    server_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    source_instance_id = _init_instance(client, server_project)
    releases_dir = tmp_path / "releases"
    version_dir = releases_dir / "v1.0.0"
    latest_dir = releases_dir / "current"
    kit_dir = tmp_path / "overlay-kit"
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
    publish = client.post(
        f"/api/v1/{source_instance_id}/state/publish",
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
            )
        },
    )
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"car-parts-overlay": f"file://{kit_dir}"},
    )

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_refkit",
            "source_type": "reference_model",
            "state_ref": "car-parts",
            "overlay_kit_ref": "car-parts-overlay",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_ref"] == "car-parts"
    assert payload["overlay_kit_ref"] == "car-parts-overlay"
    assert payload["resolved_source_ref"] == f"file://{latest_dir}"
    record = get_registry().get("inst_refkit")
    assert record is not None
    assert (Path(record.location) / "providers.py").exists()


def test_hosted_instance_init_accepts_unclaimed_bootstrap_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch, auth_enabled=True)
    kit_dir = tmp_path / "standalone-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(CAR_PARTS_YAML)
    _write_standalone_kit_manifest(kit_dir, "car-parts-hosted")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"car-parts-hosted": f"file://{kit_dir}"},
    )
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")

    missing = client.post(
        "/api/v1/runtime/instances",
        json={"source_type": "kit", "kit_ref": "car-parts-hosted"},
    )
    assert missing.status_code == 401

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_bootinit",
            "source_type": "kit",
            "kit_ref": "car-parts-hosted",
        },
        headers={"Authorization": "Bearer bootstrap-secret"},
    )
    assert response.status_code == 200

    claimed = client.post(
        "/api/v1/inst_bootinit/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )
    assert claimed.status_code == 200

    second = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_bootinit2",
            "source_type": "kit",
            "kit_ref": "car-parts-hosted",
        },
        headers={"Authorization": "Bearer bootstrap-secret"},
    )
    assert second.status_code == 401


def test_non_admin_runtime_credential_cannot_init_hosted_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
    )

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_deniedhosted",
            "source_type": "kit",
            "kit_ref": "missing-kit",
        },
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["error_type"] == "PermissionDeniedError"
    assert response.json()["context"]["tool_name"] == "cruxible_hosted_instance_init"


def test_scoped_admin_runtime_credential_cannot_init_new_hosted_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    kit_dir = tmp_path / "standalone-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(CAR_PARTS_YAML)
    _write_standalone_kit_manifest(kit_dir, "car-parts-hosted")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"car-parts-hosted": f"file://{kit_dir}"},
    )
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
    )
    denied_instance_id = "inst_deniedhostedadmin"
    before_count = get_registry().count_instances()

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": denied_instance_id,
            "source_type": "kit",
            "kit_ref": "car-parts-hosted",
        },
        headers=headers,
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    assert payload["context"]["instance_id"] == denied_instance_id
    assert payload["context"]["credential_scope"] == instance_id
    assert get_registry().count_instances() == before_count
    assert get_registry().get(denied_instance_id) is None
    assert not (get_server_state_dir() / "instances" / denied_instance_id).exists()


def test_admin_runtime_credential_can_create_and_list_runtime_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    admin = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {admin.token}"}

    for mode in ["read_only", "governed_write", "graph_write", "admin"]:
        response = client.post(
            f"/api/v1/{instance_id}/runtime/credentials",
            json={"label": f"{mode}-dispatch", "permission_mode": mode},
            headers=headers,
        )
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"credential", "token"}
        assert payload["token"].startswith(f"crt_{payload['credential']['credential_id']}_")
        credential = payload["credential"]
        assert credential["instance_id"] == instance_id
        assert credential["permission_mode"] == mode
        assert credential["created_by"] == admin.record.credential_id
        authenticated = get_runtime_credential_store().authenticate(payload["token"])
        assert authenticated is not None
        assert authenticated.permission_mode is PermissionMode[mode.upper()]

    listed = client.get(
        f"/api/v1/{instance_id}/runtime/credentials",
        headers=headers,
    )
    assert listed.status_code == 200
    credentials = listed.json()["credentials"]
    assert {credential["permission_mode"] for credential in credentials} >= {
        "read_only",
        "governed_write",
        "graph_write",
        "admin",
    }
    for credential in credentials:
        assert "token" not in credential
        assert "token_hash" not in credential


def test_runtime_credential_revoke_immediately_blocks_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    store = get_runtime_credential_store()
    admin = store.create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    target = store.create_credential(
        instance_id=instance_id,
        label="target-reader",
        permission_mode=PermissionMode.READ_ONLY,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    admin_headers = {"Authorization": f"Bearer {admin.token}"}
    target_headers = {"Authorization": f"Bearer {target.token}"}

    before = client.get(f"/api/v1/{instance_id}/schema", headers=target_headers)
    assert before.status_code == 200

    revoked = client.post(
        f"/api/v1/{instance_id}/runtime/credentials/{target.record.credential_id}/revoke",
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["credential"]["revoked_at"] is not None
    assert store.authenticate(target.token) is None

    after = client.get(f"/api/v1/{instance_id}/schema", headers=target_headers)
    assert after.status_code == 401
    assert after.json()["error_type"] == "AuthenticationError"


def test_runtime_credential_rotate_invalidates_old_and_returns_new_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    store = get_runtime_credential_store()
    admin = store.create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    target = store.create_credential(
        instance_id=instance_id,
        label="target-reader",
        permission_mode=PermissionMode.READ_ONLY,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    admin_headers = {"Authorization": f"Bearer {admin.token}"}
    old_headers = {"Authorization": f"Bearer {target.token}"}

    before = client.get(f"/api/v1/{instance_id}/schema", headers=old_headers)
    assert before.status_code == 200

    rotated = client.post(
        f"/api/v1/{instance_id}/runtime/credentials/{target.record.credential_id}/rotate",
        headers=admin_headers,
    )
    assert rotated.status_code == 200
    payload = rotated.json()
    assert set(payload) == {"credential", "token"}
    assert payload["credential"]["credential_id"] != target.record.credential_id
    assert payload["credential"]["permission_mode"] == "read_only"
    assert payload["token"].startswith(f"crt_{payload['credential']['credential_id']}_")
    assert store.authenticate(target.token) is None

    old_after = client.get(f"/api/v1/{instance_id}/schema", headers=old_headers)
    assert old_after.status_code == 401

    new_headers = {"Authorization": f"Bearer {payload['token']}"}
    new_after = client.get(f"/api/v1/{instance_id}/schema", headers=new_headers)
    assert new_after.status_code == 200


@pytest.mark.parametrize(
    "permission_mode",
    [
        PermissionMode.READ_ONLY,
        PermissionMode.GOVERNED_WRITE,
        PermissionMode.GRAPH_WRITE,
    ],
)
def test_non_admin_runtime_credential_cannot_manage_runtime_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
    permission_mode: PermissionMode,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    store = get_runtime_credential_store()
    admin = store.create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    non_admin = store.create_credential(
        instance_id=instance_id,
        label=f"{permission_mode.name.lower()}-credential",
        permission_mode=permission_mode,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {non_admin.token}"}

    responses = [
        client.post(
            f"/api/v1/{instance_id}/runtime/credentials",
            json={"label": "denied", "permission_mode": "read_only"},
            headers=headers,
        ),
        client.get(f"/api/v1/{instance_id}/runtime/credentials", headers=headers),
        client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{admin.record.credential_id}/revoke",
            headers=headers,
        ),
        client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{admin.record.credential_id}/rotate",
            headers=headers,
        ),
    ]

    for response in responses:
        assert response.status_code == 403
        payload = response.json()
        assert payload["error_type"] == "PermissionDeniedError"
        assert payload["context"]["tool_name"] == "cruxible_runtime_credentials"
        assert payload["context"]["current_mode"] == permission_mode.name
        assert payload["context"]["required_mode"] == "ADMIN"


def test_runtime_credential_management_is_instance_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_a = _init_instance(client, server_project)

    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / "config.yaml").write_text(CAR_PARTS_YAML)
    instance_b = _init_instance(client, other_root)

    admin = get_runtime_credential_store().create_credential(
        instance_id=instance_a,
        label="instance-a-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {admin.token}"}

    response = client.get(f"/api/v1/{instance_b}/runtime/credentials", headers=headers)

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    assert payload["context"]["instance_id"] == instance_b
    assert payload["context"]["credential_scope"] == instance_a


def test_runtime_credential_scope_allows_global_read_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label="instance-a-reader",
        permission_mode=PermissionMode.READ_ONLY,
        created_by="test",
    )

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {created.token}"}

    validate = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers=headers,
    )
    assert validate.status_code == 200
    assert validate.json()["valid"] is True

    info = client.get("/api/v1/server/info", headers=headers)
    assert info.status_code == 200
    assert info.json()["instance_count"] == 1


def test_runtime_credential_plaintext_is_only_returned_on_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)

    store = get_runtime_credential_store()
    created = store.create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )

    stored = store.get(created.record.credential_id)
    assert stored is not None
    assert not hasattr(stored, "token")
    assert stored.token_hash == created.record.token_hash
    assert stored.token_hash != created.token
    assert len(stored.token_hash) == 64

    listed = store.list_for_instance(instance_id)
    assert [record.credential_id for record in listed] == [created.record.credential_id]
    assert all(not hasattr(record, "token") for record in listed)

    with sqlite3.connect(get_server_state_dir() / "runtime_credentials.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM runtime_credentials WHERE credential_id = ?",
            (created.record.credential_id,),
        ).fetchone()

    assert row is not None
    persisted_values = [str(row[key]) for key in row.keys() if row[key] is not None]
    token_secret = created.token.rsplit("_", 1)[-1]
    assert created.token not in persisted_values
    assert token_secret not in persisted_values
    assert row["token_hash"] == created.record.token_hash
    assert row["token_hash"] != created.token
