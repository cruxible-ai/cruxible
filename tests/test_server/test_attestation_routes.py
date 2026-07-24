"""Hidden HTTP parity and local-operator attribution for attestations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_attestations.conftest import CONFIG_YAML


@pytest.fixture
def attestation_client(
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
        json={"root_dir": str(root), "config_yaml": CONFIG_YAML},
    )
    assert response.status_code == 200, response.text
    return cast(str, response.json()["instance_id"])


def _seed_live_claim(client: TestClient, instance_id: str) -> None:
    entities = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Service",
                    "entity_id": "svc-1",
                    "properties": {"service_id": "svc-1"},
                },
                {
                    "entity_type": "Control",
                    "entity_id": "ctl-1",
                    "properties": {"control_id": "ctl-1"},
                },
            ]
        },
    )
    assert entities.status_code == 200, entities.text
    relationship = client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "relationship_type": "protected_by",
                    "from_type": "Service",
                    "from_id": "svc-1",
                    "to_type": "Control",
                    "to_id": "ctl-1",
                    "properties": {"severity": "high"},
                }
            ]
        },
    )
    assert relationship.status_code == 200, relationship.text


def test_hidden_attestation_routes_cover_record_queue_list_and_resolve(
    attestation_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(attestation_client, tmp_path / "workspace")
    _seed_live_claim(attestation_client, instance_id)
    recorded = attestation_client.post(
        f"/api/v1/{instance_id}/attestations/record",
        json={
            "relationship_type": "protected_by",
            "from_type": "Service",
            "from_id": "svc-1",
            "to_type": "Control",
            "to_id": "ctl-1",
            "stance": "contradict",
            "observed_at": "2020-01-01T00:00:00Z",
            "evidence_refs": [{"source": "test", "source_record_id": "record-http"}],
        },
    )
    assert recorded.status_code == 200, recorded.text
    payload = recorded.json()
    assert payload["attestation"]["actor_context"]["actor_id"] == "operator"
    attestation_id = payload["attestation"]["attestation_id"]

    listed = attestation_client.get(f"/api/v1/{instance_id}/attestations")
    queued = attestation_client.get(f"/api/v1/{instance_id}/attestations/queue")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["attestation"]["attestation_id"] == attestation_id
    assert queued.status_code == 200, queued.text
    assert queued.json()["items"][0]["open_contradict_count"] == 1

    resolved = attestation_client.post(
        f"/api/v1/{instance_id}/attestations/{attestation_id}/resolve",
        json={"verdict": "upheld"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["disposition"]["reviewer_actor_context"]["actor_id"] == "operator"
    assert attestation_client.get(f"/api/v1/{instance_id}/attestations/queue").json()["total"] == 0


def test_attestation_routes_are_hidden_from_frozen_openapi() -> None:
    spec = create_app().openapi()
    assert all("/attestations" not in path for path in spec["paths"])


def test_daemon_refusal_parity_contradict_on_absent_claim(
    attestation_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(attestation_client, tmp_path / "workspace")
    refused = attestation_client.post(
        f"/api/v1/{instance_id}/attestations/record",
        json={
            "relationship_type": "protected_by",
            "from_type": "Service",
            "from_id": "svc-absent",
            "to_type": "Control",
            "to_id": "ctl-absent",
            "stance": "contradict",
            "observed_at": "2020-01-01T00:00:00Z",
            "evidence_refs": [{"source": "test", "source_record_id": "record-absent"}],
        },
    )
    assert 400 <= refused.status_code < 500, refused.text
    assert "only support" in refused.text
