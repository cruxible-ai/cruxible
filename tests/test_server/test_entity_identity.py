"""HTTP coverage for config-declared entity identity keys."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

IDENTITY_CONFIG = """\
version: '1.0'
name: declared_identity_keys
entity_types:
  HintedAccount:
    identity_hint: [name, family]
    properties:
      account_id: {type: string, primary_key: true}
      name: {type: string}
      family: {type: string}
  UniqueAccount:
    unique_by: [name, family]
    properties:
      account_id: {type: string, primary_key: true}
      name: {type: string}
      family: {type: string}
      note: {type: string}
  PatternAccount:
    id_pattern: '^pattern_[a-z0-9_]+$'
    properties:
      account_id: {type: string, primary_key: true}
      name: {type: string}
relationships: []
"""


@pytest.fixture
def identity_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, str]:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(project), "config_yaml": IDENTITY_CONFIG},
    )
    assert response.status_code == 200, response.text
    return client, response.json()["instance_id"]


def _add_entity(
    client: TestClient,
    instance_id: str,
    entity_type: str,
    entity_id: str,
    properties: dict[str, str],
) -> Response:
    return client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "properties": properties,
                }
            ]
        },
    )


def _warning(incoming_id: str, existing_id: str) -> dict[str, object]:
    return {
        "entity_type": "HintedAccount",
        "entity_id": incoming_id,
        "similar_existing_entity": {
            "entity_id": existing_id,
            "matched_properties": ["name", "family"],
        },
    }


def test_identity_hint_surfaces_on_add_entity_and_batch(
    identity_client: tuple[TestClient, str],
) -> None:
    client, instance_id = identity_client
    first = _add_entity(
        client,
        instance_id,
        "HintedAccount",
        "product_bluest_account",
        {"name": "Bluest Account", "family": "Checking"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["warnings"] == []

    duplicate = _add_entity(
        client,
        instance_id,
        "HintedAccount",
        "checking_bluest_account",
        {"name": "  BLUEST,   ACCOUNT! ", "family": "checking"},
    )
    assert duplicate.status_code == 200, duplicate.text
    duplicate_body = duplicate.json()
    assert duplicate_body["entities_added"] == 1
    assert duplicate_body["warnings"] == [
        _warning("checking_bluest_account", "product_bluest_account")
    ]
    receipt = client.get(f"/api/v1/{instance_id}/receipts/{duplicate_body['receipt_id']}")
    assert receipt.status_code == 200, receipt.text
    entity_write = next(
        node
        for node in receipt.json()["nodes"]
        if node["node_type"] == "entity_write" and node["entity_id"] == "checking_bluest_account"
    )
    assert entity_write["detail"]["similar_existing_entity"] == {
        "entity_id": "product_bluest_account",
        "matched_properties": ["name", "family"],
    }

    batch = client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={
            "payload": {
                "entities": [
                    {
                        "entity_type": "HintedAccount",
                        "entity_id": "third_bluest_account",
                        "properties": {
                            "name": "bluest account",
                            "family": "CHECKING",
                        },
                    }
                ]
            }
        },
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["entities_added"] == 1
    assert batch.json()["warnings"] == [_warning("third_bluest_account", "checking_bluest_account")]


def test_unique_by_rejects_create_and_identity_update(
    identity_client: tuple[TestClient, str],
) -> None:
    client, instance_id = identity_client
    for entity_id, properties in (
        ("unique_bluest", {"name": "Bluest Account", "family": "Checking"}),
        ("unique_green", {"name": "Green Account", "family": "Checking"}),
    ):
        response = _add_entity(
            client,
            instance_id,
            "UniqueAccount",
            entity_id,
            properties,
        )
        assert response.status_code == 200, response.text

    duplicate = _add_entity(
        client,
        instance_id,
        "UniqueAccount",
        "unique_duplicate",
        {"name": "BLUEST, ACCOUNT!", "family": " checking "},
    )
    assert duplicate.status_code == 400
    duplicate_body = duplicate.json()
    assert duplicate_body["error_type"] == "DataValidationError"
    assert "violates unique_by [name, family]" in duplicate_body["errors"][0]
    assert "existing entity_id 'unique_bluest'" in duplicate_body["errors"][0]

    non_identity_update = _add_entity(
        client,
        instance_id,
        "UniqueAccount",
        "unique_green",
        {"note": "safe edit"},
    )
    assert non_identity_update.status_code == 200, non_identity_update.text

    conflicting_update = _add_entity(
        client,
        instance_id,
        "UniqueAccount",
        "unique_green",
        {"name": "bluest account"},
    )
    assert conflicting_update.status_code == 400
    update_body = conflicting_update.json()
    assert update_body["error_type"] == "DataValidationError"
    assert "UniqueAccount:unique_green" in update_body["errors"][0]
    assert "existing entity_id 'unique_bluest'" in update_body["errors"][0]


def test_id_pattern_rejection_names_pattern(
    identity_client: tuple[TestClient, str],
) -> None:
    client, instance_id = identity_client
    response = _add_entity(
        client,
        instance_id,
        "PatternAccount",
        "BAD-ID",
        {"name": "Bad id"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error_type"] == "DataValidationError"
    assert "id_pattern '^pattern_[a-z0-9_]+$'" in body["errors"][0]
