"""HTTP boundary tests for the immutable daemon capability ceiling."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.errors import ConfigError
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import init_permissions, reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service import service_init
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def daemon_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Build daemon-owned state without using a permission-gated HTTP bootstrap."""
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    registered = get_registry().create_governed_instance(workspace_root=workspace_root)
    instance = service_init(
        Path(registered.record.location),
        config_yaml=CAR_PARTS_YAML,
        instance_mode=CruxibleInstance.GOVERNED_MODE,
    ).instance
    get_manager().register(registered.record.instance_id, instance)

    try:
        yield registered.record.instance_id
    finally:
        get_manager().clear()
        reset_client_cache()
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()


def _test_client_at_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    ceiling: str,
) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_MODE", ceiling)
    init_permissions()
    return TestClient(create_app())


def _vehicle(entity_id: str) -> dict[str, object]:
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


@pytest.mark.parametrize(
    ("ceiling", "expected_operation", "expected_required"),
    [
        ("read_only", "cruxible_create_snapshot", "GOVERNED_WRITE"),
        ("governed_write", "cruxible_add_entity", "GRAPH_WRITE"),
        ("graph_write", "cruxible_reload_config", "ADMIN"),
    ],
)
def test_each_tier_boundary_allows_at_ceiling_and_refuses_above_it(
    daemon_instance: str,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: str,
    expected_operation: str,
    expected_required: str,
) -> None:
    client = _test_client_at_ceiling(monkeypatch, ceiling)

    if ceiling == "read_only":
        allowed = client.get(f"/api/v1/{daemon_instance}/schema")
        denied = client.post(
            f"/api/v1/{daemon_instance}/snapshots",
            json={"label": "above-read-only"},
        )
    elif ceiling == "governed_write":
        allowed = client.post(
            f"/api/v1/{daemon_instance}/snapshots",
            json={"label": "at-governed-write"},
        )
        denied = client.post(
            f"/api/v1/{daemon_instance}/entities",
            json={"entities": [_vehicle("V-ABOVE-GOVERNED")]},
        )
    else:
        allowed = client.post(
            f"/api/v1/{daemon_instance}/entities",
            json={"entities": [_vehicle("V-AT-GRAPH")]},
        )
        denied = client.post(
            f"/api/v1/{daemon_instance}/config/reload",
            json={"config_yaml": CAR_PARTS_YAML},
        )

    assert allowed.status_code == 200, allowed.text
    assert denied.status_code == 403
    payload = denied.json()
    assert payload["error_type"] == "PermissionDeniedError"
    assert payload["context"] == {
        "tool_name": expected_operation,
        "current_mode": ceiling.upper(),
        "required_mode": expected_required,
        "ceiling_mode": ceiling.upper(),
    }
    assert expected_operation in payload["message"]
    assert expected_required in payload["message"]
    assert ceiling.upper() in payload["message"]
    assert "capability ceiling" in payload["message"]


def test_admin_ceiling_allows_admin_operation(
    daemon_instance: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _test_client_at_ceiling(monkeypatch, "admin")

    response = client.post(
        f"/api/v1/{daemon_instance}/config/reload",
        json={"config_yaml": CAR_PARTS_YAML},
    )

    assert response.status_code == 200, response.text


def test_admin_bearer_token_is_clamped_and_cannot_mint_above_ceiling(
    daemon_instance: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_runtime_credential_store()
    admin = store.create_credential(
        instance_id=daemon_instance,
        label="found-admin",
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    client = _test_client_at_ceiling(monkeypatch, "governed_write")
    headers = {"Authorization": f"Bearer {admin.token}"}

    at_ceiling = client.post(
        f"/api/v1/{daemon_instance}/snapshots",
        json={"label": "admin-token-clamped"},
        headers=headers,
    )
    above_ceiling = client.post(
        f"/api/v1/{daemon_instance}/entities",
        json={"entities": [_vehicle("V-ADMIN-TOKEN-DENIED")]},
        headers=headers,
    )
    workflow_apply = client.post(
        f"/api/v1/{daemon_instance}/workflows/apply",
        json={
            "workflow_name": "cannot-run-above-ceiling",
            "expected_apply_digest": "not-reached",
        },
        headers=headers,
    )
    mint = client.post(
        f"/api/v1/{daemon_instance}/runtime/credentials",
        json={"label": "attempted-admin", "permission_mode": "admin"},
        headers=headers,
    )

    assert at_ceiling.status_code == 200, at_ceiling.text
    assert above_ceiling.status_code == 403
    assert above_ceiling.json()["context"]["ceiling_mode"] == "GOVERNED_WRITE"
    assert workflow_apply.status_code == 403
    assert workflow_apply.json()["context"] == {
        "tool_name": "cruxible_apply_workflow",
        "current_mode": "GOVERNED_WRITE",
        "required_mode": "GRAPH_WRITE",
        "ceiling_mode": "GOVERNED_WRITE",
    }
    assert mint.status_code == 403
    assert mint.json()["context"] == {
        "tool_name": "cruxible_runtime_credentials",
        "current_mode": "GOVERNED_WRITE",
        "required_mode": "ADMIN",
        "ceiling_mode": "GOVERNED_WRITE",
    }
    assert [record.label for record in store.list_for_instance(daemon_instance)] == ["found-admin"]


def test_bootstrap_claim_cannot_mint_admin_above_ceiling(
    daemon_instance: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    client = _test_client_at_ceiling(monkeypatch, "graph_write")

    response = client.post(
        f"/api/v1/{daemon_instance}/runtime/bootstrap/claim",
        json={"bootstrap_secret": "bootstrap-secret"},
    )

    assert response.status_code == 403
    assert response.json()["context"] == {
        "tool_name": "cruxible_runtime_credentials",
        "current_mode": "GRAPH_WRITE",
        "required_mode": "ADMIN",
        "ceiling_mode": "GRAPH_WRITE",
    }
    assert get_runtime_credential_store().list_for_instance(daemon_instance) == []


def test_config_reload_and_environment_changes_cannot_alter_frozen_ceiling(
    daemon_instance: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _test_client_at_ceiling(monkeypatch, "graph_write")
    before = client.get("/health")
    assert before.status_code == 200
    assert before.json()["capability_ceiling"] == "graph_write"

    # A different value appearing in os.environ after initialization models a
    # runtime mutation attempt. Reinitialization refuses it, and HTTP continues
    # to observe the frozen value.
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    with pytest.raises(ConfigError, match="immutable after permission initialization"):
        init_permissions()

    reload_response = client.post(
        f"/api/v1/{daemon_instance}/config/reload",
        json={"config_yaml": CAR_PARTS_YAML},
    )
    after = client.get("/health")

    assert reload_response.status_code == 403
    assert reload_response.json()["context"]["ceiling_mode"] == "GRAPH_WRITE"
    assert after.status_code == 200
    assert after.json()["capability_ceiling"] == "graph_write"


def test_health_discloses_effective_capability_ceiling(
    daemon_instance: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _test_client_at_ceiling(monkeypatch, "read_only")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["capability_ceiling"] == "read_only"
