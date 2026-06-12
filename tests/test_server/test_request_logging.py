"""Tests for structured runtime request logs."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def request_log_buffer() -> io.StringIO:
    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )
    yield buffer
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    return TestClient(create_app())


def _runtime_request_events(buffer: io.StringIO) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in buffer.getvalue().splitlines():
        if not line.startswith("{"):
            continue
        payload = json.loads(line)
        if payload.get("event") == "runtime_request":
            events.append(payload)
    return events


def _clear_buffer(buffer: io.StringIO) -> None:
    buffer.seek(0)
    buffer.truncate(0)


def _init_instance(client: TestClient, root: Path) -> str:
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CAR_PARTS_YAML},
    )
    assert response.status_code == 200
    return str(response.json()["instance_id"])


def _runtime_credential_headers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_id: str,
    permission_mode: PermissionMode,
) -> tuple[dict[str, str], str]:
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label=f"{permission_mode.name.lower()} credential",
        permission_mode=permission_mode,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    return {"Authorization": f"Bearer {created.token}"}, created.record.credential_id


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


def test_successful_runtime_request_logs_principal_and_instance(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _init_instance(app_client, tmp_path / "project")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.READ_ONLY,
    )
    _clear_buffer(request_log_buffer)

    response = app_client.get(f"/api/v1/{instance_id}/schema", headers=headers)

    assert response.status_code == 200
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["event"] == "runtime_request"
    assert event["method"] == "GET"
    assert event["route"] == "/api/v1/{instance_id}/schema"
    assert event["status"] == 200
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == "read_only credential"
    assert event["credential_type"] == "runtime_credential"
    assert event["role"] == "read_only"
    assert event["instance_scope"] == instance_id
    assert event["instance_id"] == instance_id
    assert "operation_id" not in event


def test_denied_runtime_request_logs_status_and_error_type(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _init_instance(app_client, tmp_path / "project")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.READ_ONLY,
    )
    _clear_buffer(request_log_buffer)

    response = app_client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": []},
        headers=headers,
    )

    assert response.status_code == 403
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["event"] == "runtime_request"
    assert event["method"] == "POST"
    assert event["route"] == "/api/v1/{instance_id}/entities"
    assert event["status"] == 403
    assert event["error_type"] == "PermissionDeniedError"
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == "read_only credential"
    assert event["credential_type"] == "runtime_credential"
    assert event["instance_id"] == instance_id


def test_derived_actor_context_logs_same_principal_and_operation_as_provenance(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _init_instance(app_client, tmp_path / "project")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.GRAPH_WRITE,
    )
    payload = {
        "entities": [
            {
                "entity_type": "Vehicle",
                "entity_id": "V-LOG-ACTOR",
                "properties": {
                    "vehicle_id": "V-LOG-ACTOR",
                    "year": 2026,
                    "make": "Honda",
                    "model": "Civic",
                },
            },
            {
                "entity_type": "Part",
                "entity_id": "BP-LOG-ACTOR",
                "properties": {
                    "part_number": "BP-LOG-ACTOR",
                    "name": "Log Actor Pads",
                    "category": "brakes",
                },
            },
        ],
        "relationships": [
            {
                "from_type": "Part",
                "from_id": "BP-LOG-ACTOR",
                "relationship_type": "fits",
                "to_type": "Vehicle",
                "to_id": "V-LOG-ACTOR",
            }
        ],
    }
    _clear_buffer(request_log_buffer)

    response = app_client.post(
        f"/api/v1/{instance_id}/direct-writes/batch",
        json={"payload": payload},
        headers=headers,
    )

    assert response.status_code == 200
    lookup = app_client.get(
        f"/api/v1/{instance_id}/relationships/lookup",
        params={
            "from_type": "Part",
            "from_id": "BP-LOG-ACTOR",
            "relationship_type": "fits",
            "to_type": "Vehicle",
            "to_id": "V-LOG-ACTOR",
        },
        headers=headers,
    )
    assert lookup.status_code == 200
    actor_context = lookup.json()["metadata"]["provenance"]["created_actor_context"]
    event = _runtime_request_events(request_log_buffer)[0]
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == actor_context["actor_id"]
    assert event["operation_id"] == actor_context["operation_id"]


def test_bootstrap_secret_runtime_request_log_does_not_include_secret(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    kit_dir = tmp_path / "standalone-kit"
    kit_dir.mkdir()
    (kit_dir / "config.yaml").write_text(CAR_PARTS_YAML)
    _write_standalone_kit_manifest(kit_dir, "car-parts-hosted")
    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"car-parts-hosted": f"file://{kit_dir}"},
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    _clear_buffer(request_log_buffer)

    response = app_client.post(
        "/api/v1/runtime/instances",
        json={
            "instance_id": "inst_requestlog",
            "source_type": "kit",
            "kit_ref": "car-parts-hosted",
        },
        headers={"Authorization": "Bearer bootstrap-secret"},
    )

    assert response.status_code == 200
    output = request_log_buffer.getvalue()
    assert "bootstrap-secret" not in output
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["route"] == "/api/v1/runtime/instances"
    assert event["status"] == 200
    assert event["principal_id"] == "runtime_bootstrap"
    assert event["principal_label"] == "runtime_bootstrap"
    assert event["credential_type"] == "runtime_bootstrap"
