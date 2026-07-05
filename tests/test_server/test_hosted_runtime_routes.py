"""Hosted runtime route tests: bootstrap claim, runtime credentials, hosted init, scoping.

Extracted from the private cloud branch's test_routes.py during the hosted-runtime
hardening extraction; uses the same app/server fixtures as test_routes.py.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.kits.state_refs import StateCatalogEntry
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime import api
from cruxible_core.runtime.instance import CruxibleInstance
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
from cruxible_core.service.snapshots import service_backup_instance
from tests.test_cli.conftest import CAR_PARTS_YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_OPERATION_CONFIG = REPO_ROOT / "kits" / "agent-operation" / "config.yaml"
KEV_REFERENCE_KIT_DIR = REPO_ROOT / "kits" / "kev-reference"
KEV_KIT_DIR = REPO_ROOT / "kits" / "kev-triage"
KEV_PUBLIC_DATA_FILES = (
    KEV_REFERENCE_KIT_DIR / "data" / "known_exploited_vulnerabilities.csv",
    KEV_REFERENCE_KIT_DIR / "data" / "epss_kev_nvd.csv",
    KEV_REFERENCE_KIT_DIR / "data" / "nvd_kev_cves.json",
)

AUTH_MANAGED_PRINCIPAL_YAML = """
name: auth_managed_principal_test
description: Auth-managed runtime principal fixture
entity_types:
  Principal:
    auth_managed: true
    write_policy: mint_only
    properties:
      actor_id:
        type: string
        primary_key: true
      kind:
        type: string
      label:
        type: string
      credential_id:
        type: string
        optional: true
      permission_mode:
        type: string
      custom_note:
        type: string
        optional: true
relationships: []
"""


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
) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    if auth_enabled:
        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
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


def _init_auth_managed_instance(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    config_yaml: str | None = None,
) -> str:
    """Create an auth-managed instance on an auth-ENABLED daemon.

    Auth-managed configs are refused at init on an auth-off daemon, because their
    entity types materialize only from runtime-credential mints that require
    ``CRUXIBLE_SERVER_AUTH``. The bearer-gated HTTP init route has no credential to
    present on a fresh daemon (bootstrap only authorizes hosted-init/server-ops), so
    the instance is created directly as a daemon operator would, with auth enabled.
    """
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    resolved_config_yaml = (
        config_yaml if config_yaml is not None else (root / "config.yaml").read_text()
    )
    return api.init_governed(str(root), config_yaml=resolved_config_yaml).instance_id


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
    label: str | None = None,
) -> dict[str, str]:
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label=label or f"{permission_mode.name.lower()} credential",
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


def _direct_write_payload(suffix: str) -> dict[str, object]:
    return {
        "entities": [
            _valid_vehicle_entity(f"V-{suffix}"),
            {
                "entity_type": "Part",
                "entity_id": f"BP-{suffix}",
                "properties": {
                    "part_number": f"BP-{suffix}",
                    "name": f"Batch Pads {suffix}",
                    "category": "brakes",
                },
            },
        ],
        "relationships": [
            {
                "from_type": "Part",
                "from_id": f"BP-{suffix}",
                "relationship_type": "fits",
                "to_type": "Vehicle",
                "to_id": f"V-{suffix}",
                "properties": {"verified": True, "source": "batch"},
            }
        ],
    }


def _lookup_fitment(
    client: TestClient,
    instance_id: str,
    suffix: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    response = client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": f"BP-{suffix}",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": f"V-{suffix}",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


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


def test_runtime_credential_gates_entire_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label="local-reader",
        permission_mode=PermissionMode.READ_ONLY,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")

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
        headers={"Authorization": f"Bearer {created.token}"},
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
        json={"label": "governed-write-allowed"},
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


def _seed_pending_fit_edge(
    client: TestClient,
    instance_id: str,
    admin_headers: dict[str, str],
) -> None:
    """Seed a Part/Vehicle pair with a single pending (non-live) fits edge."""
    seed = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Part",
                    "entity_id": "BP-PEND",
                    "properties": {
                        "part_number": "BP-PEND",
                        "name": "Pending Pads",
                        "category": "brakes",
                    },
                },
                _valid_vehicle_entity("V-PEND"),
            ]
        },
        headers=admin_headers,
    )
    assert seed.status_code == 200
    add = client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "from_type": "Part",
                    "from_id": "BP-PEND",
                    "relationship_type": "fits",
                    "to_type": "Vehicle",
                    "to_id": "V-PEND",
                    "properties": {"verified": True},
                    "pending": True,
                }
            ]
        },
        headers=admin_headers,
    )
    assert add.status_code == 200


def _fit_review_status(
    client: TestClient,
    instance_id: str,
    headers: dict[str, str],
) -> str:
    lookup = client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": "BP-PEND",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-PEND",
        },
        headers=headers,
    )
    assert lookup.status_code == 200
    return lookup.json()["metadata"]["assertion"]["review"]["status"]


def test_governed_write_credential_approve_promotes_with_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    """Under auth, a GOVERNED_WRITE credential may promote a review edge.

    The runtime credential supplies the resolved actor identity, so the
    review-state promotion is attributed and the actor-guard (audit F3) is
    satisfied without a request-supplied actor_context.
    """
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    admin_headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
        label="seed-admin",
    )
    _seed_pending_fit_edge(client, instance_id, admin_headers)
    assert _fit_review_status(client, instance_id, admin_headers) == "pending"

    governed_headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
        label="governed-reviewer",
    )
    approve = client.post(
        f"/api/v1/{instance_id}/feedback",
        json={
            "action": "approve",
            "source": "human",
            "from_type": "Part",
            "from_id": "BP-PEND",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-PEND",
        },
        headers=governed_headers,
    )
    assert approve.status_code == 200
    assert approve.json()["applied"] is True
    assert _fit_review_status(client, instance_id, governed_headers) == "approved"


def _review_actor_context() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="usr_reviewer",
        org_id="org_1",
        operation_id="op_review",
        timestamp="2026-06-05T12:00:00Z",
    )


class TestReviewPromotionActorGuard:
    """Unit coverage for the runtime feedback-approve actor guard (audit F3)."""

    def test_auth_on_approve_without_actor_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cruxible_core.errors import AuthenticationError
        from cruxible_core.runtime.api import _require_review_promotion_actor

        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
        with pytest.raises(AuthenticationError, match="requires a resolved actor identity"):
            _require_review_promotion_actor("approve", None)

    def test_auth_on_correct_without_actor_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cruxible_core.errors import AuthenticationError
        from cruxible_core.runtime.api import _require_review_promotion_actor

        # ``correct`` also sets the review status to ``approved`` (a full peer of
        # ``approve`` for the close-gate), so it must carry a resolved actor too.
        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
        with pytest.raises(AuthenticationError, match="requires a resolved actor identity"):
            _require_review_promotion_actor("correct", None)

    @pytest.mark.parametrize("action", ["approve", "correct"])
    def test_auth_on_promotion_with_actor_allowed(
        self, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        from cruxible_core.runtime.api import _require_review_promotion_actor

        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
        # Returns without raising when a resolved actor is present.
        _require_review_promotion_actor(action, _review_actor_context())

    def test_auth_off_approve_without_actor_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cruxible_core.runtime.api import _require_review_promotion_actor

        monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
        _require_review_promotion_actor("approve", None)

    @pytest.mark.parametrize("action", ["reject", "flag"])
    def test_non_promotion_actions_never_gated(
        self, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        from cruxible_core.runtime.api import _require_review_promotion_actor

        # ``reject`` -> rejected and ``flag`` -> pending neither make a non-live edge
        # live, so even under auth they never require actor context and legitimate
        # flag/reject feedback is untouched. (``correct`` DOES promote -> gated above.)
        monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
        _require_review_promotion_actor(action, None)


def test_auth_off_approve_promotes_without_actor_context(
    app_client: TestClient,
    server_project: Path,
) -> None:
    """The actor-guard only fires under a governed (auth-on) runtime.

    With auth off there is no tier boundary or governed identity, so local
    review-state promotion stays usable without a request-supplied actor_context.
    """
    instance_id = _init_instance(app_client, server_project)
    admin_headers: dict[str, str] = {}
    _seed_pending_fit_edge(app_client, instance_id, admin_headers)

    approve = app_client.post(
        f"/api/v1/{instance_id}/feedback",
        json={
            "action": "approve",
            "source": "human",
            "from_type": "Part",
            "from_id": "BP-PEND",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-PEND",
        },
    )
    assert approve.status_code == 200
    assert approve.json()["applied"] is True
    assert _fit_review_status(app_client, instance_id, admin_headers) == "approved"


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


def test_runtime_credential_governed_write_derives_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    credential_label = "snapshot-agent"
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
        label=credential_label,
    )

    response = client.post(
        f"/api/v1/{instance_id}/snapshots",
        json={"label": "derived-actor"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["snapshot"]["snapshot_id"]


def test_runtime_credential_direct_write_derives_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    credential_label = "codex-core"
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
        label=credential_label,
    )

    response = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={"payload": _direct_write_payload("DERIVED-ACTOR")},
        headers=headers,
    )

    assert response.status_code == 200
    lookup = _lookup_fitment(client, instance_id, "DERIVED-ACTOR", headers=headers)
    actor_context = lookup["metadata"]["provenance"]["created_actor_context"]
    assert actor_context["actor_type"] == "service_account"
    assert actor_context["actor_id"] == credential_label
    assert actor_context["org_id"] == instance_id
    assert actor_context["operation_id"].startswith("op_")


def test_runtime_credential_rejects_spoofed_actor_context_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    # G3: a runtime credential must NOT be able to assert an arbitrary actor_id
    # via a request-supplied actor_context. Doing so would let a credential
    # labeled "codex-core" impersonate an authorized human reviewer and pass
    # identity-gated approval guards. The mismatch is rejected and nothing is
    # written.
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
        label="codex-core",
    )
    spoofed_actor = _actor_context(actor_id="robert", operation_id="op_control")

    response = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={
            "payload": _direct_write_payload("SPOOFED-ACTOR"),
            "actor_context": spoofed_actor,
        },
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json()["error_type"] == "AuthenticationError"
    # The spoofed write was rejected before any state changed.
    fitment = client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": "BP-SPOOFED-ACTOR",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-SPOOFED-ACTOR",
        },
        headers=headers,
    )
    assert fitment.status_code == 200
    assert fitment.json()["found"] is False


def test_runtime_credential_entities_route_rejects_spoofed_actor_context(
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
        label="codex-core",
    )

    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [_valid_vehicle_entity("V-SPOOFED-ENTITY")],
            "actor_context": _actor_context(actor_id="robert", operation_id="op_spoof_entity"),
        },
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json()["error_type"] == "AuthenticationError"
    lookup = client.get(
        f"/api/v1/{instance_id}/entities/Vehicle/V-SPOOFED-ENTITY",
        headers=headers,
    )
    assert lookup.status_code == 200
    assert lookup.json()["found"] is False


def test_runtime_credential_matching_actor_context_keeps_credential_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    # A supplied actor_context that agrees with the credential identity is
    # accepted; the credential remains authoritative for the identity fields
    # and only the correlation request_id is carried through.
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    credential_label = "codex-core"
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
        label=credential_label,
    )
    matching_actor = {
        "actor_type": "service_account",
        "actor_id": credential_label,
        "org_id": instance_id,
        "operation_id": "op_supplied",
        "timestamp": "2026-06-05T12:00:00Z",
        "request_id": "req_correlation",
    }

    response = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={
            "payload": _direct_write_payload("MATCHING-ACTOR"),
            "actor_context": matching_actor,
        },
        headers=headers,
    )

    assert response.status_code == 200
    lookup = _lookup_fitment(client, instance_id, "MATCHING-ACTOR", headers=headers)
    actor_context = lookup["metadata"]["provenance"]["created_actor_context"]
    assert actor_context["actor_type"] == "service_account"
    assert actor_context["actor_id"] == credential_label
    assert actor_context["org_id"] == instance_id
    # Identity stays credential-derived: the supplied operation_id does NOT win.
    assert actor_context["operation_id"] != "op_supplied"
    assert actor_context["operation_id"].startswith("op_")
    # The correlation request_id is preserved from the supplied context.
    assert actor_context["request_id"] == "req_correlation"


def test_runtime_credential_review_approval_is_gated_and_close_gate_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # agent-operation gates ReviewRequest approval (unlike the retired
    # project-state kit, where approval was ungated). Approval over HTTP must:
    #   * come from the authorized-reviewer actor — here the runtime credential
    #     whose label (and therefore credential-derived actor_id) is
    #     "authorized-reviewer"; a writer credential cannot approve, AND
    #   * co-write a StateNote(kind=review_note) linked via
    #     state_note_about_review_request in the same write (the batch endpoint).
    # The WorkItem close gate then remains: close is rejected until the approved
    # review exists, then allowed.
    project_root = tmp_path / "agent-operation"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(AGENT_OPERATION_CONFIG.read_text())
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_auth_managed_instance(monkeypatch, project_root)
    writer_headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
        label="codex-core",
    )
    reviewer_headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
        label="authorized-reviewer",
    )

    seed = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={
            "payload": {
                "entities": [
                    {
                        "entity_type": "WorkItem",
                        "entity_id": "wi-approval-guard",
                        "properties": {
                            "work_item_id": "wi-approval-guard",
                            "title": "Approval guard",
                            "type": "feature",
                            "status": "active",
                            "priority": "high",
                        },
                    },
                    {
                        "entity_type": "ReviewRequest",
                        "entity_id": "rr-approval-guard",
                        "properties": {
                            "review_request_id": "rr-approval-guard",
                            "title": "Approval guard review",
                            "status": "requested",
                        },
                    },
                ],
                "relationships": [
                    {
                        "from_type": "ReviewRequest",
                        "from_id": "rr-approval-guard",
                        "relationship_type": "review_request_for_work_item",
                        "to_type": "WorkItem",
                        "to_id": "wi-approval-guard",
                    }
                ],
            }
        },
        headers=writer_headers,
    )
    assert seed.status_code == 200

    close_before_approval = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "WorkItem",
                    "entity_id": "wi-approval-guard",
                    "properties": {"status": "closed"},
                }
            ]
        },
        headers=writer_headers,
    )
    assert close_before_approval.status_code == 400
    assert close_before_approval.json()["error_type"] == "DataValidationError"
    assert "work_item_closed_requires_approved_review" in close_before_approval.text

    approval_payload = {
        "payload": {
            "entities": [
                {
                    "entity_type": "ReviewRequest",
                    "entity_id": "rr-approval-guard",
                    "properties": {"status": "approved"},
                },
                {
                    "entity_type": "StateNote",
                    "entity_id": "sn-approval-guard",
                    "properties": {
                        "note_id": "sn-approval-guard",
                        "kind": "review_note",
                        "title": "Approval rationale",
                        "summary": "Approved after review.",
                        "body": "Approval guard review approved.",
                        "created_at": "2026-06-05T12:00:00Z",
                    },
                },
            ],
            "relationships": [
                {
                    "from_type": "StateNote",
                    "from_id": "sn-approval-guard",
                    "relationship_type": "state_note_about_review_request",
                    "to_type": "ReviewRequest",
                    "to_id": "rr-approval-guard",
                }
            ],
        }
    }

    # A writer credential (actor_id="codex-core") cannot approve: the authorized
    # actor guard rejects it.
    writer_approval = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json=approval_payload,
        headers=writer_headers,
    )
    assert writer_approval.status_code == 400
    assert writer_approval.json()["error_type"] == "DataValidationError"
    assert "review_request_approval_requires_authorized_actor" in writer_approval.text

    # The authorized-reviewer credential, co-writing the review_note, approves.
    approved = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json=approval_payload,
        headers=reviewer_headers,
    )
    assert approved.status_code == 200

    closed = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "WorkItem",
                    "entity_id": "wi-approval-guard",
                    "properties": {"status": "closed"},
                }
            ]
        },
        headers=writer_headers,
    )
    assert closed.status_code == 200


def test_runtime_credential_cannot_spoof_actor_to_pass_approval_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # G3 end-to-end: a "codex-core" runtime credential supplies an
    # actor_context claiming actor_id="authorized-reviewer" (the authorized
    # reviewer actor) to try to approve its own review. The credential identity
    # is authoritative, so the spoofed actor_context is rejected before reaching
    # the approval guard, and the ReviewRequest is NOT approved.
    project_root = tmp_path / "agent-operation"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(AGENT_OPERATION_CONFIG.read_text())
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_auth_managed_instance(monkeypatch, project_root)
    codex_headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
        label="codex-core",
    )

    seed = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={
            "payload": {
                "entities": [
                    {
                        "entity_type": "WorkItem",
                        "entity_id": "wi-spoof-guard",
                        "properties": {
                            "work_item_id": "wi-spoof-guard",
                            "title": "Spoof guard",
                            "type": "feature",
                            "status": "active",
                            "priority": "high",
                        },
                    },
                    {
                        "entity_type": "ReviewRequest",
                        "entity_id": "rr-spoof-guard",
                        "properties": {
                            "review_request_id": "rr-spoof-guard",
                            "title": "Spoof guard review",
                            "status": "requested",
                        },
                    },
                ],
                "relationships": [
                    {
                        "from_type": "ReviewRequest",
                        "from_id": "rr-spoof-guard",
                        "relationship_type": "review_request_for_work_item",
                        "to_type": "WorkItem",
                        "to_id": "wi-spoof-guard",
                    }
                ],
            }
        },
        headers=codex_headers,
    )
    assert seed.status_code == 200

    spoofed = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "ReviewRequest",
                    "entity_id": "rr-spoof-guard",
                    "properties": {"status": "approved"},
                }
            ],
            "actor_context": {
                "actor_type": "human_user",
                "actor_id": "authorized-reviewer",
                "org_id": instance_id,
                "operation_id": "op_spoof",
                "timestamp": "2026-06-05T12:00:00Z",
            },
        },
        headers=codex_headers,
    )
    assert spoofed.status_code == 401
    assert spoofed.json()["error_type"] == "AuthenticationError"

    review = client.get(
        f"/api/v1/{instance_id}/entities/ReviewRequest/rr-spoof-guard",
        headers=codex_headers,
    )
    assert review.status_code == 200
    assert review.json()["found"] is True
    assert review.json()["properties"]["status"] == "requested"


def test_legacy_bearer_direct_write_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)

    unauthenticated = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={"payload": _direct_write_payload("NO-AUTH")},
    )
    assert unauthenticated.status_code == 200
    no_auth_lookup = _lookup_fitment(client, instance_id, "NO-AUTH")
    assert "created_actor_context" not in no_auth_lookup["metadata"]["provenance"]

    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    legacy_headers = {"Authorization": "Bearer legacy-token"}
    legacy = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={"payload": _direct_write_payload("LEGACY-AUTH")},
        headers=legacy_headers,
    )
    assert legacy.status_code == 401
    assert legacy.json()["error_type"] == "AuthenticationError"


def test_decision_record_routes_persist_credential_derived_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    # Decision records persist the actor context, but a runtime credential is
    # authoritative for the identity: the persisted actor is derived from the
    # credential, not from a request-supplied actor_context. A matching supplied
    # context is accepted (request_id carried through); the identity fields stay
    # credential-derived.
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    credential_label = "decision-agent"
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
        label=credential_label,
    )

    created = client.post(
        f"/api/v1/{instance_id}/decision-records",
        json={"question": "Decide?"},
        headers=headers,
    )
    assert created.status_code == 200
    record = created.json()["record"]
    assert record["opened_actor_context"]["actor_type"] == "service_account"
    assert record["opened_actor_context"]["actor_id"] == credential_label
    assert record["opened_actor_context"]["org_id"] == instance_id
    fetched = client.get(
        f"/api/v1/{instance_id}/decision-records/{record['decision_record_id']}",
        headers=headers,
    )
    assert fetched.status_code == 200
    fetched_record = fetched.json()["record"]
    assert fetched_record["opened_actor_context"]["actor_id"] == credential_label
    assert fetched_record["finalized_actor_context"] is None

    finalized = client.post(
        f"/api/v1/{instance_id}/decision-records/{record['decision_record_id']}/finalize",
        json={
            "final_decision": "Ship it",
            "decision_class": "recommended",
        },
        headers=headers,
    )
    assert finalized.status_code == 200
    assert finalized.json()["record"]["finalized_actor_context"]["actor_id"] == credential_label
    fetched_final = client.get(
        f"/api/v1/{instance_id}/decision-records/{record['decision_record_id']}",
        headers=headers,
    )
    assert fetched_final.status_code == 200
    fetched_final_record = fetched_final.json()["record"]
    assert fetched_final_record["opened_actor_context"]["actor_id"] == credential_label
    assert fetched_final_record["finalized_actor_context"]["actor_id"] == credential_label


def test_runtime_credential_decision_record_rejects_spoofed_actor_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    # A runtime credential cannot relay an arbitrary actor identity onto a
    # decision record either; a mismatching actor_context is rejected.
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GOVERNED_WRITE,
        label="decision-agent",
    )

    spoofed = client.post(
        f"/api/v1/{instance_id}/decision-records",
        json={
            "question": "Decide?",
            "actor_context": _actor_context(actor_id="robert", operation_id="op_spoof"),
        },
        headers=headers,
    )
    assert spoofed.status_code == 401
    assert spoofed.json()["error_type"] == "AuthenticationError"


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


def test_runtime_bootstrap_claim_materializes_auth_managed_entity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "auth-managed-bootstrap-project"
    project.mkdir()
    (project / "config.yaml").write_text(AUTH_MANAGED_PRINCIPAL_YAML)
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_auth_managed_instance(monkeypatch, project)
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")

    response = client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )

    assert response.status_code == 200
    principal = get_manager().get(instance_id).load_graph().get_entity(
        "Principal",
        "bootstrap-admin",
    )
    assert principal is not None
    assert principal.properties["kind"] == "service_account"
    assert principal.properties["permission_mode"] == "admin"
    assert principal.metadata.actor_context is not None
    assert principal.metadata.actor_context.actor_id == "bootstrap-admin"


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


def test_shared_hosted_profile_rejects_workflow_provider_execution_with_public_error_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_config_yaml: str,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    project = tmp_path / "workflow-project"
    project.mkdir()
    (project / "config.yaml").write_text(workflow_config_yaml)
    monkeypatch.setenv("CRUXIBLE_HOSTED_SERVER_PROFILE", "shared")
    monkeypatch.setenv("CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND", "docker")
    instance_id = _init_instance(client, project, config_yaml=workflow_config_yaml)

    lock = client.post(f"/api/v1/{instance_id}/workflows/lock", json={})
    assert lock.status_code == 200

    monkeypatch.delenv("CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND", raising=False)
    response = client.post(
        f"/api/v1/{instance_id}/workflows/run",
        json={
            "workflow_name": "evaluate_promo",
            "input": {
                "sku": "SKU-123",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
            },
        },
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error_type"] == "CustomerCodeExecutionUnsupportedError"
    assert body["error_code"] == "customer_code_execution_unsupported"
    assert body["message"] == (
        "Customer code execution is not supported in this hosted runtime profile."
    )
    assert body["context"] == {}
    assert body["errors"] == []
    serialized = json.dumps(body)
    assert "tests.support" not in serialized
    assert str(project) not in serialized
    assert "docker" not in serialized


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
        "kit_refs": ["car-parts-hosted"],
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
        json={**payload, "kit_refs": [f"file://{kit_dir}"]},
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
            "kit_refs": ["missing-kit"],
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
        json={"source_type": "kit", "kit_refs": ["car-parts-hosted"]},
    )
    assert missing.status_code == 401

    response = client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_bootinit",
            "source_type": "kit",
            "kit_refs": ["car-parts-hosted"],
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
            "kit_refs": ["car-parts-hosted"],
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
            "kit_refs": ["missing-kit"],
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
            "kit_refs": ["car-parts-hosted"],
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


def test_runtime_credential_routes_materialize_auth_managed_entity_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "auth-managed-project"
    project.mkdir()
    (project / "config.yaml").write_text(AUTH_MANAGED_PRINCIPAL_YAML)
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_auth_managed_instance(monkeypatch, project)
    store = get_runtime_credential_store()
    admin = store.create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {admin.token}"}

    created = client.post(
        f"/api/v1/{instance_id}/runtime/credentials",
        json={"label": "principal-agent", "permission_mode": "read_only"},
        headers=headers,
    )

    assert created.status_code == 200
    created_payload = created.json()
    instance = get_manager().get(instance_id)
    graph = instance.load_graph()
    principal = graph.get_entity("Principal", "principal-agent")
    assert principal is not None
    assert principal.properties["kind"] == "service_account"
    assert principal.properties["label"] == "principal-agent"
    assert principal.properties["credential_id"] == created_payload["credential"]["credential_id"]
    assert principal.properties["permission_mode"] == "read_only"
    assert principal.metadata.actor_context is not None
    assert principal.metadata.actor_context.actor_id == "principal-agent"

    graph.update_entity_properties(
        "Principal",
        "principal-agent",
        {"custom_note": "preserve-me"},
    )
    instance.save_graph(graph)

    rotated = client.post(
        f"/api/v1/{instance_id}/runtime/credentials/"
        f"{created_payload['credential']['credential_id']}/rotate",
        headers=headers,
    )

    assert rotated.status_code == 200
    rotated_payload = rotated.json()
    graph = instance.load_graph()
    principals = graph.list_entities("Principal")
    assert [entity.entity_id for entity in principals] == ["principal-agent"]
    principal = graph.get_entity("Principal", "principal-agent")
    assert principal is not None
    assert principal.properties["credential_id"] == rotated_payload["credential"]["credential_id"]
    assert principal.properties["custom_note"] == "preserve-me"


def test_runtime_credential_create_materialization_failure_leaves_store_untouched(
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
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    headers = {"Authorization": f"Bearer {admin.token}"}
    before_ids = [record.credential_id for record in store.list_for_instance(instance_id)]

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(
        "cruxible_core.server.routes.runtime_credentials.materialize_auth_managed_entities",
        fail_materialization,
    )

    with pytest.raises(RuntimeError, match="materialization failed"):
        client.post(
            f"/api/v1/{instance_id}/runtime/credentials",
            json={"label": "principal-agent", "permission_mode": "read_only"},
            headers=headers,
        )

    after_ids = [record.credential_id for record in store.list_for_instance(instance_id)]
    assert after_ids == before_ids


def test_runtime_credential_rotate_materialization_failure_keeps_old_token_active(
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
    headers = {"Authorization": f"Bearer {admin.token}"}
    before_ids = [record.credential_id for record in store.list_for_instance(instance_id)]

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(
        "cruxible_core.server.routes.runtime_credentials.materialize_auth_managed_entities",
        fail_materialization,
    )

    with pytest.raises(RuntimeError, match="materialization failed"):
        client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{target.record.credential_id}/rotate",
            headers=headers,
        )

    after_ids = [record.credential_id for record in store.list_for_instance(instance_id)]
    assert after_ids == before_ids
    assert store.authenticate(target.token) is not None
    target_after = store.get(target.record.credential_id)
    assert target_after is not None
    assert target_after.revoked_at is None


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


def test_instance_scoped_credential_runs_non_daemon_routes_but_not_server_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    """An instance-scoped credential keeps its non-daemon routes but is barred from
    the daemon-wide global-metadata read (wi-server-op-routes-instance-scope)."""
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

    # Instance-bound routes still work for an instance-scoped credential.
    validate = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers=headers,
    )
    assert validate.status_code == 200
    assert validate.json()["valid"] is True

    # The daemon-wide global-metadata read is now barred: an instance-scoped
    # credential must not enumerate the shared daemon's cross-tenant state.
    info = client.get("/api/v1/server/info", headers=headers)
    assert info.status_code == 403
    info_payload = info.json()
    assert info_payload["error_type"] == "InstanceScopeError"
    assert info_payload["context"]["instance_id"] == "cruxible_server_info"
    assert info_payload["context"]["credential_scope"] == instance_id


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


# ---------------------------------------------------------------------------
# Daemon-wide server-operation scope gate (wi-server-op-routes-instance-scope)
#
# server_info / server_restart / restore_instance act on the whole shared daemon.
# An instance-scoped ADMIN must NOT drive them (cross-tenant DoS / metadata leak);
# only the unscoped runtime bootstrap operator (or auth-off local) may.
# ---------------------------------------------------------------------------

_BOOTSTRAP_SECRET = "daemon-operator-secret"


def _instance_scoped_admin_headers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_id: str,
) -> dict[str, str]:
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label="instance-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", _BOOTSTRAP_SECRET)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    return {"Authorization": f"Bearer {created.token}"}


def _operator_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", _BOOTSTRAP_SECRET)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    return {"Authorization": f"Bearer {_BOOTSTRAP_SECRET}"}


def _build_restore_artifact(tmp_path: Path, instance_id: str) -> Path:
    source_root = tmp_path / f"restore-source-{instance_id}"
    source_root.mkdir()
    (source_root / "config.yaml").write_text(CAR_PARTS_YAML)
    source_instance = CruxibleInstance.init(source_root, "config.yaml")
    artifact = tmp_path / f"{instance_id}.cruxible.zip"
    service_backup_instance(
        source_instance,
        instance_id=instance_id,
        artifact_path=artifact,
    )
    return artifact


def test_server_info_rejects_instance_scoped_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _instance_scoped_admin_headers(monkeypatch, instance_id=instance_id)

    response = client.get("/api/v1/server/info", headers=headers)

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    assert payload["context"]["instance_id"] == "cruxible_server_info"
    assert payload["context"]["credential_scope"] == instance_id


def test_server_restart_rejects_instance_scoped_admin_no_reexec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    """The cross-tenant restart is closed: an instance-scoped ADMIN cannot re-exec
    the shared daemon, and the re-exec hook is never armed."""
    from cruxible_core.server import restart as restart_module

    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _instance_scoped_admin_headers(monkeypatch, instance_id=instance_id)

    exec_called = False

    def _record() -> None:
        nonlocal exec_called
        exec_called = True

    restart_module.set_exec_self(_record)
    monkeypatch.setattr(restart_module, "_RESTART_DELAY_SECONDS", 0.0)
    try:
        response = client.post("/api/v1/server/restart", headers=headers)
    finally:
        restart_module.reset_exec_self()

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    assert payload["context"]["instance_id"] == "cruxible_server_restart"
    assert payload["context"]["credential_scope"] == instance_id
    # Critical: the daemon-wide re-exec was never scheduled.
    assert exec_called is False


def test_restore_instance_rejects_instance_scoped_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    headers = _instance_scoped_admin_headers(monkeypatch, instance_id=instance_id)
    artifact = _build_restore_artifact(tmp_path, "inst_restoredenied")
    before_count = get_registry().count_instances()

    response = client.post(
        "/api/v1/instances/restore",
        json={
            "artifact_path": str(artifact),
            "root_dir": str(tmp_path / "restored-denied"),
        },
        headers=headers,
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_type"] == "InstanceScopeError"
    assert payload["context"]["instance_id"] == "cruxible_instance_restore"
    assert payload["context"]["credential_scope"] == instance_id
    # The first-check fires before the manifest is read, so no instance is created.
    assert get_registry().count_instances() == before_count
    assert get_registry().get("inst_restoredenied") is None


def test_unscoped_operator_can_run_daemon_server_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    """The legitimate daemon operator path: the unscoped bootstrap secret authorizes
    server_info / server_restart / restore on the shared daemon under auth."""
    from cruxible_core.server import restart as restart_module

    client = _make_app_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, server_project)
    # Force auth_required so server_info reports the shared-daemon posture.
    get_runtime_credential_store().mark_auth_required("test")
    headers = _operator_headers(monkeypatch)

    info = client.get("/api/v1/server/info", headers=headers)
    assert info.status_code == 200
    assert info.json()["instance_count"] == 1
    assert info.json()["auth_enabled"] is True

    exec_called = False

    def _record() -> None:
        nonlocal exec_called
        exec_called = True

    restart_module.set_exec_self(_record)
    monkeypatch.setattr(restart_module, "_RESTART_DELAY_SECONDS", 0.0)
    try:
        restart = client.post("/api/v1/server/restart", headers=headers)
        assert restart.status_code == 200
        assert restart.json()["scheduled"] is True
    finally:
        restart_module.reset_exec_self()
    assert exec_called is True

    artifact = _build_restore_artifact(tmp_path, "inst_operatorrestore")
    restore = client.post(
        "/api/v1/instances/restore",
        json={
            "artifact_path": str(artifact),
            "root_dir": str(tmp_path / "operator-restored"),
        },
        headers=headers,
    )
    assert restore.status_code == 200
    assert restore.json()["instance_id"] == "inst_operatorrestore"
    assert get_registry().get(instance_id) is not None  # original instance still present


def test_auth_off_local_can_run_daemon_server_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_project: Path,
) -> None:
    """Single-tenant / auth-off local: no credential context, so the daemon-wide
    routes keep working with no token (no governed tier boundary locally)."""
    from cruxible_core.server import restart as restart_module

    client = _make_app_client(tmp_path, monkeypatch)
    _init_instance(client, server_project)

    info = client.get("/api/v1/server/info")
    assert info.status_code == 200
    assert info.json()["auth_enabled"] is False

    exec_called = False

    def _record() -> None:
        nonlocal exec_called
        exec_called = True

    restart_module.set_exec_self(_record)
    monkeypatch.setattr(restart_module, "_RESTART_DELAY_SECONDS", 0.0)
    try:
        restart = client.post("/api/v1/server/restart")
        assert restart.status_code == 200
        assert restart.json()["scheduled"] is True
    finally:
        restart_module.reset_exec_self()
    assert exec_called is True

    artifact = _build_restore_artifact(tmp_path, "inst_localrestore")
    restore = client.post(
        "/api/v1/instances/restore",
        json={
            "artifact_path": str(artifact),
            "root_dir": str(tmp_path / "local-restored"),
        },
    )
    assert restore.status_code == 200
    assert restore.json()["instance_id"] == "inst_localrestore"
