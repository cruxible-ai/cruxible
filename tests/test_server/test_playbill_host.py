"""DP-0B tests for the schema-free daemon host and credential boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry, reset_registry


@pytest.fixture
def host_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    return TestClient(create_app())


def test_host_allocation_is_idempotent_and_creates_no_semantic_state(
    host_client: TestClient,
) -> None:
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_dp0b_host"},
    )
    assert created.status_code == 200, created.text
    assert created.json() == {
        "instance_id": "inst_dp0b_host",
        "status": "created",
    }

    record = get_registry().get("inst_dp0b_host")
    assert record is not None
    assert record.backend == GOVERNED_DAEMON_BACKEND
    assert record.workspace_root is None
    assert not Path(record.location).exists()

    repeated = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_dp0b_host"},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "already_exists"
    assert not Path(record.location).exists()


def test_transport_credentials_do_not_initialize_playbill_or_a_legacy_graph(
    host_client: TestClient,
) -> None:
    created = host_client.post("/api/v1/runtime/instances", json={})
    instance_id = created.json()["instance_id"]
    record = get_registry().get(instance_id)
    assert record is not None

    credential = host_client.post(
        f"/api/v1/{instance_id}/runtime/credentials",
        json={"label": "automation", "permission_mode": "governed_write"},
    )
    assert credential.status_code == 200, credential.text
    assert credential.json()["credential"]["instance_id"] == instance_id
    assert not Path(record.location).exists()

    uninitialized = host_client.get(f"/api/v1/{instance_id}/playbill/documents")
    assert uninitialized.status_code == 409
    assert "not initialized" in uninitialized.text
    assert not Path(record.location).exists()


def test_playbill_bootstrap_is_the_first_semantic_write(
    host_client: TestClient,
    tmp_path: Path,
) -> None:
    created = host_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_dp0b_bootstrap"},
    )
    instance_id = created.json()["instance_id"]
    record = get_registry().get(instance_id)
    assert record is not None
    managed_root = Path(record.location) / ".cruxible" / "playbill-v1"
    assert not managed_root.exists()

    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        authority_roles=("owner",),
        forbidden_roots=(managed_root,),
    )
    initialized = host_client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
    )
    assert initialized.status_code == 200, initialized.text
    assert initialized.json()["instance_id"] == instance_id
    assert managed_root.is_dir()
    assert not (Path(record.location) / ".cruxible" / "state.db").exists()


def test_authenticated_bootstrap_binds_owner_to_credential_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "authenticated-server-state"
    bootstrap_secret = "one-time-bootstrap-secret"
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", bootstrap_secret)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    client = TestClient(create_app())
    bootstrap_headers = {"Authorization": f"Bearer {bootstrap_secret}"}

    allocated = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_authenticated_bootstrap"},
        headers=bootstrap_headers,
    )
    assert allocated.status_code == 200, allocated.text
    instance_id = allocated.json()["instance_id"]

    claimed = client.post(
        f"/api/v1/{instance_id}/runtime/bootstrap/claim",
        json={"bootstrap_secret": bootstrap_secret},
        headers=bootstrap_headers,
    )
    assert claimed.status_code == 200, claimed.text
    admin_headers = {"Authorization": f"Bearer {claimed.json()['token']}"}

    record = get_registry().get(instance_id)
    assert record is not None
    managed_root = Path(record.location) / ".cruxible" / "playbill-v1"
    owner = generate_client_principal_key(
        tmp_path / "authenticated-owner-custody",
        principal_id="bootstrap-admin",
        authority_roles=("owner",),
        forbidden_roots=(managed_root,),
    )
    initialized = client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={"principals": [owner.principal.model_dump(mode="json")]},
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text
    assert managed_root.is_dir()
    assert not (Path(record.location) / ".cruxible" / "state.db").exists()
