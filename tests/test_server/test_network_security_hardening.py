"""Tests for daemon network-security hardening (wi-daemon-network-security-hardening).

Covers:
- (b) #5: an unhandled sqlite IntegrityError must NOT leak the internal schema
  (table/column names) through the HTTP 500/409 body; the real detail is logged
  server-side only.
- (a) #4: browser-originated cross-origin requests are rejected via an Origin
  allowlist, while no-Origin (CLI/SDK) requests still work; the unauth-loopback
  default tier stays ADMIN, with an opt-in READ_ONLY default flag.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime import api
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import (
    PermissionMode,
    init_permissions,
)
from cruxible_core.runtime.permissions import (
    reset_permissions as runtime_reset_permissions,
)
from cruxible_core.server.app import create_app
from cruxible_core.server.config import is_origin_allowed
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from tests.test_cli.conftest import CAR_PARTS_YAML

# Real sqlite message shape: names a live table + column. This must never reach
# the client. We assert both the table and the column substring are absent.
_LEAKY_TABLE = "graph_entities"
_LEAKY_COLUMN = "graph_entities.entity_id"
_LEAKY_SQLITE_MESSAGE = f"UNIQUE constraint failed: {_LEAKY_COLUMN}"


def _make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    monkeypatch.delenv("CRUXIBLE_ORIGIN_ALLOWLIST", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()
    return TestClient(create_app(), raise_server_exceptions=raise_server_exceptions)


def _init_instance(client: TestClient, root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CAR_PARTS_YAML},
    )
    assert response.status_code == 200
    return response.json()["instance_id"]


@pytest.fixture
def capture_structlog() -> io.StringIO:
    """Capture structlog output to a buffer, restoring the stderr default after."""
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


# ── (b) #5: IntegrityError must not leak schema ──────────────────────────────


def test_integrity_error_returns_generic_message_without_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_structlog: io.StringIO,
) -> None:
    client = _make_client(tmp_path, monkeypatch, raise_server_exceptions=False)
    instance_id = _init_instance(client, tmp_path / "project")

    def _raise_integrity(*args: object, **kwargs: object) -> object:
        raise sqlite3.IntegrityError(_LEAKY_SQLITE_MESSAGE)

    monkeypatch.setattr(api, "add_entities", _raise_integrity)

    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Vehicle",
                    "entity_id": "V-1",
                    "properties": {
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                }
            ]
        },
    )

    assert response.status_code == 409
    body = response.json()
    serialized = response.text

    # The generic client message is returned...
    assert body["message"] == "database constraint violation"
    assert body["error_type"] == "ConstraintViolationError"
    # ...and the internal schema (table + column) never reaches the client.
    assert _LEAKY_TABLE not in serialized
    assert _LEAKY_COLUMN not in serialized
    assert "UNIQUE constraint failed" not in serialized

    # The real detail IS logged server-side (load-bearing: the operator can still
    # diagnose the violation from logs).
    logs = capture_structlog.getvalue()
    assert "database_integrity_error" in logs
    assert _LEAKY_COLUMN in logs


def test_generic_database_error_returns_generic_500_without_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_structlog: io.StringIO,
) -> None:
    client = _make_client(tmp_path, monkeypatch, raise_server_exceptions=False)
    instance_id = _init_instance(client, tmp_path / "project")

    leaky_sql_detail = "no such column: secret_internal_col"

    def _raise_db_error(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError(leaky_sql_detail)

    monkeypatch.setattr(api, "add_entities", _raise_db_error)

    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Vehicle",
                    "entity_id": "V-2",
                    "properties": {
                        "vehicle_id": "V-2",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                }
            ]
        },
    )

    assert response.status_code == 500
    body = response.json()
    assert body["message"] == "database error"
    assert "secret_internal_col" not in response.text

    logs = capture_structlog.getvalue()
    assert "database_error" in logs
    assert "secret_internal_col" in logs


# ── (a) #4: Origin allowlist ─────────────────────────────────────────────────


def test_no_origin_request_is_allowed_for_cli_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal CLI/SDK request (no Origin header) must still work."""
    client = _make_client(tmp_path, monkeypatch)
    response = client.post("/api/v1/validate", json={"config_yaml": CAR_PARTS_YAML})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_cross_origin_browser_request_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser request from a non-allowlisted origin is rejected with 403."""
    client = _make_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "OriginNotAllowedError"


def test_cross_origin_browser_request_is_rejected_on_mutating_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Origin gate runs before the handler, so it also covers mutating routes."""
    client = _make_client(tmp_path, monkeypatch)
    instance_id = _init_instance(client, tmp_path / "project")
    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={"entities": []},
        headers={"Origin": "https://attacker.test"},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "OriginNotAllowedError"


def test_loopback_origin_is_allowed_for_bundled_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon-served same-origin UI (loopback Origin) keeps working."""
    client = _make_client(tmp_path, monkeypatch)
    for origin in (
        "http://127.0.0.1:8100",
        "http://localhost:8100",
        "http://[::1]:8100",
    ):
        response = client.post(
            "/api/v1/validate",
            json={"config_yaml": CAR_PARTS_YAML},
            headers={"Origin": origin},
        )
        assert response.status_code == 200, origin
        assert response.json()["valid"] is True


def test_allowlisted_origin_is_permitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicitly allowlisted origin (e.g. a Cloud console) is permitted."""
    monkeypatch.setenv("CRUXIBLE_ORIGIN_ALLOWLIST", "https://console.example.com")
    client = TestClient(create_app())
    reset_permissions()
    response = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Origin": "https://console.example.com"},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_referer_fallback_rejects_cross_origin_when_origin_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Origin is absent, a cross-origin Referer is still rejected."""
    client = _make_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
        headers={"Referer": "https://evil.example.com/page"},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "OriginNotAllowedError"


def test_is_origin_allowed_unit_policy() -> None:
    """Unit-level policy: None allowed; loopback allowed; opaque/null + foreign denied."""
    assert is_origin_allowed(None) is True
    assert is_origin_allowed("http://127.0.0.1:8100") is True
    assert is_origin_allowed("http://localhost") is True
    assert is_origin_allowed("https://[::1]") is True
    assert is_origin_allowed("https://evil.example.com") is False
    # Opaque origin ("null") from a sandboxed iframe / file:// must be denied.
    assert is_origin_allowed("null") is False
    assert is_origin_allowed("") is True  # empty == absent == non-browser


# ── (a) #4: default tier stays ADMIN, with opt-in READ_ONLY ───────────────────


def test_unauth_loopback_default_mode_stays_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unauthenticated default stays ADMIN so local writes work out of the box."""
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_DEFAULT_READ_ONLY", raising=False)
    runtime_reset_permissions()
    assert init_permissions() == PermissionMode.ADMIN


def test_default_read_only_opt_in_lowers_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in flag lowers the unset-CRUXIBLE_MODE default to READ_ONLY."""
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.setenv("CRUXIBLE_DEFAULT_READ_ONLY", "true")
    runtime_reset_permissions()
    assert init_permissions() == PermissionMode.READ_ONLY


def test_explicit_mode_overrides_read_only_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit CRUXIBLE_MODE always wins over the opt-in default flag."""
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    monkeypatch.setenv("CRUXIBLE_DEFAULT_READ_ONLY", "true")
    runtime_reset_permissions()
    assert init_permissions() == PermissionMode.ADMIN
