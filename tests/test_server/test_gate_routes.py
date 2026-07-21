"""In-process HTTP coverage for daemon-owned gate evaluation receipts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

GATE_CONFIG_YAML = """\
version: "1.0"
name: gate_route_test
entity_types:
  ReviewRequest:
    properties:
      review_request_id: {type: string, primary_key: true}
      status: {type: string, enum: [requested, approved]}
      change_head: {type: string}
relationships: []
gates:
  merge-review:
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
"""

PIN = "a" * 40


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    try:
        yield TestClient(create_app())
    finally:
        get_manager().clear()
        reset_client_cache()
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()


@pytest.fixture
def gate_instance_id(app_client: TestClient, tmp_path: Path) -> str:
    root = tmp_path / "workspace"
    root.mkdir()
    initialized = app_client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": GATE_CONFIG_YAML},
    )
    assert initialized.status_code == 200, initialized.text
    instance_id = initialized.json()["instance_id"]
    seeded = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "ReviewRequest",
                    "entity_id": "RR-1",
                    "properties": {
                        "review_request_id": "RR-1",
                        "status": "approved",
                        "change_head": PIN,
                    },
                }
            ]
        },
    )
    assert seeded.status_code == 200, seeded.text
    return str(instance_id)


def test_gate_check_endpoint_returns_daemon_evaluation_and_receipt(
    app_client: TestClient,
    gate_instance_id: str,
) -> None:
    response = app_client.post(
        f"/api/v1/{gate_instance_id}/gates/merge-review/check",
        json={"candidates": [PIN]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "gate_name": "merge-review",
        "kind": "git-pre-push",
        "candidates": [PIN],
        "candidate_outcomes": [
            {
                "candidate": PIN,
                "satisfied": True,
                "satisfying_entity_ids": ["RR-1"],
            }
        ],
        "verdict": "satisfied",
        "reason": None,
        "instance_id": gate_instance_id,
        "read_revision": payload["read_revision"],
        "receipt_id": payload["receipt_id"],
    }
    receipt_response = app_client.get(
        f"/api/v1/{gate_instance_id}/receipts/{payload['receipt_id']}"
    )
    assert receipt_response.status_code == 200
    receipt = receipt_response.json()
    assert receipt["operation_type"] == "gate_evaluation"
    assert receipt["parameters"]["instance_id"] == gate_instance_id
    assert receipt["parameters"]["read_revision"] == payload["read_revision"]


def test_gate_check_endpoint_receipts_reported_adapter_refusal(
    app_client: TestClient,
    gate_instance_id: str,
) -> None:
    response = app_client.post(
        f"/api/v1/{gate_instance_id}/gates/merge-review/check",
        json={"candidates": [], "error_reason": "malformed pre-push stdin"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["verdict"] == "error"
    assert payload["reason"] == "malformed pre-push stdin"
    receipt = app_client.get(f"/api/v1/{gate_instance_id}/receipts/{payload['receipt_id']}").json()
    assert receipt["parameters"] == {
        "instance_id": gate_instance_id,
        "read_revision": payload["read_revision"],
        "gate_name": "merge-review",
        "kind": "git-pre-push",
        "candidates": [],
        "candidate_outcomes": [],
        "verdict": "error",
        "reason": "malformed pre-push stdin",
    }
