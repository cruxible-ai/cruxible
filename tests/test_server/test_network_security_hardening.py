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
import json
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

# A pre-serialized JSON validate-request body, used to exercise an explicit
# Content-Type with a charset parameter without relying on the client to set it.
CAR_PARTS_YAML_JSON_BODY = json.dumps({"config_yaml": CAR_PARTS_YAML})


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


def test_no_origin_state_change_with_non_json_body_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a state-changing request with NO Origin and a non-JSON body is 403.

    Without this, a cross-site *simple-request* POST (no Origin, ``text/plain`` /
    form / multipart body) would skip the Origin gate entirely and reach a future
    raw-body route at loopback-ADMIN. The whole mutating surface binds JSON today,
    but the gate itself must not depend on that holding forever.
    """
    client = _make_client(tmp_path, monkeypatch)
    for content_type in (
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
    ):
        response = client.post(
            "/api/v1/validate",
            content=b"config_yaml: {}",
            headers={"Content-Type": content_type},
        )
        assert response.status_code == 403, content_type
        assert response.json()["error_type"] == "OriginNotAllowedError"


def test_no_origin_state_change_with_missing_content_type_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state-changing no-Origin request with NO Content-Type is also rejected."""
    client = _make_client(tmp_path, monkeypatch)
    # Send a raw body but strip the Content-Type header entirely.
    response = client.post(
        "/api/v1/validate",
        content=b"anything",
        headers={"Content-Type": ""},
    )
    assert response.status_code == 403
    assert response.json()["error_type"] == "OriginNotAllowedError"


def test_no_origin_state_change_with_json_body_passes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal CLI/SDK request (no Origin, application/json) still clears the gate.

    It must reach the handler / normal validation, i.e. NOT be blocked as a
    forbidden origin. Charset parameters on the media type must not matter.
    """
    client = _make_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/validate",
        json={"config_yaml": CAR_PARTS_YAML},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True

    # Explicit charset parameter on the JSON media type must still pass.
    response = client.post(
        "/api/v1/validate",
        content=CAR_PARTS_YAML_JSON_BODY,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_allowed_origin_with_non_json_body_passes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowed (loopback) Origin bypasses the no-Origin JSON requirement.

    The fail-closed rule applies only when no allowed Origin is present; a
    same-origin UI request with a loopback Origin must pass regardless of body
    content type (it is then rejected/accepted by normal body validation, never
    as a forbidden origin).
    """
    client = _make_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/v1/validate",
        content=b"config_yaml: {}",
        headers={
            "Origin": "http://127.0.0.1:8100",
            "Content-Type": "text/plain",
        },
    )
    # Not blocked by the Origin gate (would be 403/OriginNotAllowedError); it
    # falls through to FastAPI body validation instead.
    assert response.status_code != 403
    assert response.json().get("error_type") != "OriginNotAllowedError"


def test_no_origin_get_with_non_json_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET is not state-changing, so the no-Origin content-type rule never applies."""
    client = _make_client(tmp_path, monkeypatch)
    response = client.get("/version")
    assert response.status_code == 200
    assert "version" in response.json()


# ── (b) #5: catch-all must not leak str(exc) for unexpected exceptions ────────


def test_unhandled_exception_returns_generic_500_without_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_structlog: io.StringIO,
) -> None:
    """An unexpected non-domain exception must yield a generic 500 with no detail.

    A non-``sqlite3`` exception type whose message embeds internal detail (here a
    RuntimeError carrying SQL-shaped text) bypasses the sqlite3.* handlers and
    hits the catch-all. The client body must be generic; the real detail must be
    logged server-side.
    """
    client = _make_client(tmp_path, monkeypatch, raise_server_exceptions=False)
    instance_id = _init_instance(client, tmp_path / "project")

    leaky_detail = "no such column: secret_internal_col in graph_entities"

    def _raise_unexpected(*args: object, **kwargs: object) -> object:
        raise RuntimeError(leaky_detail)

    monkeypatch.setattr(api, "add_entities", _raise_unexpected)

    response = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Vehicle",
                    "entity_id": "V-3",
                    "properties": {
                        "vehicle_id": "V-3",
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
    assert body["message"] == "internal server error"
    assert body["error_type"] == "InternalServerError"
    # The raw exception message (and its internal detail) never reaches the client.
    assert leaky_detail not in response.text
    assert "secret_internal_col" not in response.text
    assert "RuntimeError" not in response.text

    # The real detail IS logged server-side for operator diagnosis.
    logs = capture_structlog.getvalue()
    assert "unhandled_server_error" in logs
    assert leaky_detail in logs
    assert "RuntimeError" in logs


def test_domain_error_message_still_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: a domain error keeps its intended, safe client message.

    Domain errors subclass CoreError and are served by the dedicated CoreError
    handler, so genericizing the catch-all must not affect them. ConfigError's
    detailed validation message must still reach the client verbatim.
    """
    client = _make_client(tmp_path, monkeypatch)
    response = client.post("/api/v1/validate", json={"config_yaml": "entity_types: {}\n"})
    assert response.status_code == 400
    body = response.json()
    assert body["error_type"] == "ConfigError"
    # The intended, user-facing message is preserved (not genericized away).
    assert body["message"] != "internal server error"
    assert body["errors"]  # the structured per-error detail survives too


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
