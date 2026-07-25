"""Hidden HTTP parity and local-operator attribution for outcome contracts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_outcome_contracts.conftest import UNGUARDED_CONFIG

CHECK_AT = "2026-07-24T12:00:00Z"
EXPIRES_AT = "2026-08-24T12:00:00Z"
MEASUREMENT: dict[str, Any] = {
    "kind": "query",
    "query_name": "healthy_services",
    "params": {},
    "expect": {"min_count": 1},
}


@pytest.fixture
def outcome_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    with TestClient(create_app()) as client:
        yield client
    get_manager().clear()
    reset_registry()


def _init(client: TestClient, root: Path) -> str:
    root.mkdir()
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": UNGUARDED_CONFIG},
    )
    assert response.status_code == 200, response.text
    return cast(str, response.json()["instance_id"])


def _seed_subject(client: TestClient, instance_id: str) -> None:
    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Service",
                    "entity_id": "svc-1",
                    "properties": {"service_id": "svc-1", "health": "healthy"},
                },
                {
                    "entity_type": "Decision",
                    "entity_id": "dd-1",
                    "properties": {
                        "decision_id": "dd-1",
                        "status": "proposed",
                        "outcome_tracking": "required",
                        "title": "Adopt the thing",
                    },
                },
            ]
        },
    )
    assert response.status_code == 200, response.text


def _activate(client: TestClient, instance_id: str, contract_id: str) -> None:
    """Stand in for the acceptance write path (no guard on this config)."""
    from cruxible_core.resolution_contracts.types import ContractActivation

    instance = get_manager().get(instance_id)
    with instance.write_transaction() as uow:
        uow.resolution_contracts.save_activation(
            ContractActivation(
                contract_id=contract_id,
                acceptance_receipt_id="RCP-http-acceptance",
                subject_content_digest="sha256:http-accepted",
            )
        )


def test_hidden_outcome_routes_cover_open_resolve_dispose_list_and_queue(
    outcome_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(outcome_client, tmp_path / "workspace")
    _seed_subject(outcome_client, instance_id)

    opened = outcome_client.post(
        f"/api/v1/{instance_id}/outcome-contracts/open",
        json={
            "entity_type": "Decision",
            "entity_id": "dd-1",
            "description": "Service stays healthy",
            "check_at": CHECK_AT,
            "expires_at": EXPIRES_AT,
            "measurement": MEASUREMENT,
        },
    )
    assert opened.status_code == 200, opened.text
    payload = opened.json()
    # Auth-off local-operator minting: the daemon attributes the act itself.
    assert payload["contract"]["actor_context"]["actor_id"] == "operator"
    contract_id = payload["contract"]["contract_id"]

    listed = outcome_client.get(f"/api/v1/{instance_id}/outcome-contracts")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["status"] == "prepared"

    # Prepared contracts demand no attention.
    queued = outcome_client.get(f"/api/v1/{instance_id}/outcome-contracts/queue")
    assert queued.status_code == 200, queued.text
    assert queued.json()["total"] == 0

    _activate(outcome_client, instance_id, contract_id)
    queued = outcome_client.get(f"/api/v1/{instance_id}/outcome-contracts/queue")
    assert queued.json()["total"] == 1

    query = outcome_client.post(
        f"/api/v1/{instance_id}/queries/run",
        json={"query_name": "healthy_services", "params": {}},
    )
    assert query.status_code == 200, query.text
    receipt_id = query.json()["receipt_id"]

    resolved = outcome_client.post(
        f"/api/v1/{instance_id}/outcome-contracts/{contract_id}/resolve",
        json={
            "verdict": "satisfied",
            "observed_at": "2026-07-25T12:00:00Z",
            "evidence_refs": [{"source": "test", "source_record_id": "record-http"}],
            "resolving_query_receipt_id": receipt_id,
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolution"]["actor_context"]["actor_id"] == "operator"
    resolution_id = resolved.json()["resolution"]["resolution_id"]

    assert outcome_client.get(f"/api/v1/{instance_id}/outcome-contracts/queue").json()["total"] == 0

    disposed = outcome_client.post(
        f"/api/v1/{instance_id}/outcome-resolutions/{resolution_id}/dispose",
        json={"verdict": "overturned", "note": "re-measure at the right window"},
    )
    assert disposed.status_code == 200, disposed.text
    assert disposed.json()["disposition"]["reviewer_actor_context"]["actor_id"] == "operator"
    # An overturn re-opens the contract, so it is owed a check again.
    assert outcome_client.get(f"/api/v1/{instance_id}/outcome-contracts/queue").json()["total"] == 1


def test_outcome_routes_are_hidden_from_frozen_openapi() -> None:
    spec = create_app().openapi()
    assert all("/outcome-contracts" not in path for path in spec["paths"])
    assert all("/outcome-resolutions" not in path for path in spec["paths"])


def test_daemon_refusal_parity_for_an_absent_subject(
    outcome_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(outcome_client, tmp_path / "workspace")
    refused = outcome_client.post(
        f"/api/v1/{instance_id}/outcome-contracts/open",
        json={
            "entity_type": "Decision",
            "entity_id": "dd-absent",
            "description": "Service stays healthy",
            "check_at": CHECK_AT,
            "expires_at": EXPIRES_AT,
            "measurement": MEASUREMENT,
        },
    )
    assert 400 <= refused.status_code < 500, refused.text
    assert "does not exist" in refused.text


def test_daemon_refusal_parity_for_a_prepared_contract(
    outcome_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(outcome_client, tmp_path / "workspace")
    _seed_subject(outcome_client, instance_id)
    opened = outcome_client.post(
        f"/api/v1/{instance_id}/outcome-contracts/open",
        json={
            "entity_type": "Decision",
            "entity_id": "dd-1",
            "description": "Service stays healthy",
            "check_at": CHECK_AT,
            "expires_at": EXPIRES_AT,
            "measurement": MEASUREMENT,
        },
    )
    assert opened.status_code == 200, opened.text
    refused = outcome_client.post(
        f"/api/v1/{instance_id}/outcome-contracts/{opened.json()['contract']['contract_id']}/resolve",
        json={"verdict": "indeterminate", "observed_at": "2026-07-25T12:00:00Z"},
    )
    assert 400 <= refused.status_code < 500, refused.text
    assert "never activated" in refused.text
