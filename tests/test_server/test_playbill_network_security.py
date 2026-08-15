"""Network-security laws on the surviving Playbill host surface."""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry


@pytest.fixture
def secure_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    monkeypatch.delenv("CRUXIBLE_ORIGIN_ALLOWLIST", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def capture_structlog() -> io.StringIO:
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


@pytest.mark.parametrize(
    ("error", "status", "public_message", "log_event"),
    [
        (
            sqlite3.IntegrityError("UNIQUE constraint failed: secret_table.secret_column"),
            409,
            "database constraint violation",
            "database_integrity_error",
        ),
        (
            sqlite3.OperationalError("no such column: secret_internal_column"),
            500,
            "database error",
            "database_error",
        ),
    ],
)
def test_database_errors_do_not_leak_schema(
    secure_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capture_structlog: io.StringIO,
    error: sqlite3.DatabaseError,
    status: int,
    public_message: str,
    log_event: str,
) -> None:
    def raise_database_error(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(
        "cruxible_core.runtime.host_api.create_playbill_host",
        raise_database_error,
    )
    response = secure_client.post("/api/v1/runtime/instances", json={})

    assert response.status_code == status
    assert response.json()["message"] == public_message
    assert "secret_" not in response.text
    logs = capture_structlog.getvalue()
    assert log_event in logs
    assert "secret_" in logs


def test_cross_origin_browser_requests_are_rejected_before_playbill_handlers(
    secure_client: TestClient,
) -> None:
    response = secure_client.post(
        "/api/v1/runtime/instances",
        json={},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "OriginNotAllowedError"


def test_no_origin_json_cli_request_remains_allowed(secure_client: TestClient) -> None:
    response = secure_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_json_cli"},
    )
    assert response.status_code == 200, response.text


def test_no_origin_non_json_mutation_is_rejected(secure_client: TestClient) -> None:
    response = secure_client.post(
        "/api/v1/runtime/instances",
        content="{}",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "OriginNotAllowedError"


def test_allowlisted_origin_passes_the_origin_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_ORIGIN_ALLOWLIST", "https://console.example.com")
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "allowlisted-state"))
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_allowlisted"},
        headers={"Origin": "https://console.example.com"},
    )
    assert response.status_code == 200, response.text
