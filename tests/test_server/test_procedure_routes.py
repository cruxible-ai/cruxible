"""Internal HTTP parity routes used by procedure CLI/MCP server mode."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_procedures.conftest import CONFIG_YAML, actor


@pytest.fixture
def app_client(
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


def _init_procedure_instance(client: TestClient, root: Path) -> str:
    root.mkdir()
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CONFIG_YAML},
    )
    assert response.status_code == 200, response.text
    instance_id = response.json()["instance_id"]
    locked = client.post(f"/api/v1/{instance_id}/workflows/lock", json={})
    assert locked.status_code == 200, locked.text
    return instance_id


def _definition() -> dict[str, object]:
    return {
        "name": "http_procedure",
        "contract_in": "ProcedureInput",
        "steps": [
            {
                "id": "shape",
                "shape_items": {
                    "items": [{"value": "$input.value"}],
                    "fields": {"value": "$item.value"},
                },
                "as": "result",
            }
        ],
        "returns": "result",
        "precondition": {},
        "budget": {"wall_clock_s": 10, "max_provider_calls": 0},
    }


def test_procedure_routes_cover_lifecycle_run_and_read_envelopes(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init_procedure_instance(app_client, tmp_path / "workspace")
    proposed = app_client.post(
        f"/api/v1/{instance_id}/procedures/propose",
        json={
            "definition": _definition(),
            "actor_context": actor("http-proposer").model_dump(mode="json"),
        },
    )
    assert proposed.status_code == 200, proposed.text
    procedure_id = proposed.json()["procedure"]["procedure_id"]

    listed = app_client.get(
        f"/api/v1/{instance_id}/procedures",
        params={"status": "pending"},
    )
    shown = app_client.get(f"/api/v1/{instance_id}/procedures/{procedure_id}")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["procedure_id"] == procedure_id
    assert listed.json()["read_revision"] is not None
    assert shown.status_code == 200, shown.text
    assert shown.json()["procedure"]["procedure_id"] == procedure_id

    promoted = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/resolve",
        json={
            "action": "promote",
            "expected_version": 1,
            "actor_context": actor("http-reviewer").model_dump(mode="json"),
        },
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["procedure"]["status"] == "live"

    executed = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/run",
        json={
            "input_payload": {"value": 1},
            "actor_context": actor("http-runner").model_dump(mode="json"),
        },
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["run"]["status"] == "finalized"
    assert executed.json()["run"]["verdict"] == "succeeded"

    runs = app_client.get(f"/api/v1/{instance_id}/procedures/{procedure_id}/runs")
    assert runs.status_code == 200, runs.text
    assert runs.json()["items"][0]["run_id"] == executed.json()["run"]["run_id"]
    assert runs.json()["read_revision"] is not None

    retired = app_client.post(
        f"/api/v1/{instance_id}/procedures/{procedure_id}/retire",
        json={
            "expected_version": 2,
            "reason": "superseded operationally",
            "actor_context": actor("http-retirer").model_dump(mode="json"),
        },
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["procedure"]["status"] == "retired"


def test_internal_procedure_transport_routes_do_not_expand_frozen_public_openapi() -> None:
    spec = create_app().openapi()
    assert all("/procedures" not in path for path in spec["paths"])
