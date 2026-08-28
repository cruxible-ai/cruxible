"""Tests for structured runtime request logs."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import Request
from fastapi.testclient import TestClient

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server import request_logging as request_logging_module
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.server.request_logging import (
    _RotatingFileLogSink,
    configure_request_logging,
    log_runtime_request,
)


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
    get_playbill_manager().clear()
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


def _create_host(client: TestClient, instance_id: str) -> str:
    response = client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": instance_id},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["instance_id"])


def _runtime_credential_headers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance_id: str,
    permission_mode: PermissionMode,
) -> tuple[dict[str, str], str]:
    created = get_runtime_credential_store().create_credential(
        instance_id=instance_id,
        label=f"{permission_mode.name.lower()}_credential",
        permission_mode=permission_mode,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    return {"Authorization": f"Bearer {created.token}"}, created.record.credential_id


def test_successful_runtime_request_logs_principal_and_instance(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _create_host(app_client, "inst_request_log_success")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
    )
    _clear_buffer(request_log_buffer)

    response = app_client.get(
        f"/api/v1/{instance_id}/runtime/credentials",
        headers=headers,
    )

    assert response.status_code == 200
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["event"] == "runtime_request"
    assert event["method"] == "GET"
    assert event["route"] == "/api/v1/{instance_id}/runtime/credentials"
    assert event["status"] == 200
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == "admin_credential"
    assert event["credential_type"] == "runtime_credential"
    assert event["role"] == "admin"
    assert event["instance_scope"] == instance_id
    assert event["instance_id"] == instance_id
    assert "operation_id" not in event


def test_denied_runtime_request_logs_status_and_error_type(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _create_host(app_client, "inst_request_log_denied")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.READ_ONLY,
    )
    _clear_buffer(request_log_buffer)

    response = app_client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": ""},
        headers=headers,
    )

    assert response.status_code == 403
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["event"] == "runtime_request"
    assert event["method"] == "POST"
    assert event["route"] == "/api/v1/{instance_id}/playbill/bodies"
    assert event["status"] == 403
    assert event["error_type"] == "PermissionDeniedError"
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == "read_only_credential"
    assert event["credential_type"] == "runtime_credential"
    assert event["instance_id"] == instance_id


def test_playbill_write_logs_credential_actor_and_operation(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _create_host(app_client, "inst_request_log_actor")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
    )
    managed_root = Path(get_registry().get(instance_id).location) / ".cruxible" / "playbill-v1"
    owner = generate_client_principal_key(
        tmp_path / "request-log-owner-custody",
        principal_id="admin_credential",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "request-log-reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    _clear_buffer(request_log_buffer)

    response = app_client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ]
        },
        headers=headers,
    )

    assert response.status_code == 200
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == "admin_credential"
    assert str(event["operation_id"]).startswith("op_")


def test_activation_receipt_and_request_log_name_the_credential_actor(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    instance_id = _create_host(app_client, "inst_request_log_activation")
    headers, credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
    )
    managed_root = Path(get_registry().get(instance_id).location) / ".cruxible" / "playbill-v1"
    owner = generate_client_principal_key(
        tmp_path / "request-log-activation-owner",
        principal_id="admin_credential",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "request-log-activation-reviewer",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed_root,),
    )
    initialized = app_client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={
            "principals": [
                owner.principal.model_dump(mode="json"),
                reviewer.principal.model_dump(mode="json"),
            ]
        },
        headers=headers,
    )
    assert initialized.status_code == 200, initialized.text
    stored = app_client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(b"activation actor\n").decode("ascii")},
        headers=headers,
    )
    assert stored.status_code == 200, stored.text
    shell = DocumentShell(
        identity="document:activation-actor",
        document_kind="design",
        title="Activation actor",
        media_type="text/plain",
        body_digest=stored.json()["digest"],
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = app_client.post(
        f"/api/v1/{instance_id}/playbill/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "activation-actor"},
        headers=headers,
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]

    _clear_buffer(request_log_buffer)
    activated = app_client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate",
        headers=headers,
    )

    assert activated.status_code == 200, activated.text
    assert activated.json()["activated_by"] == "admin_credential"
    event = _runtime_request_events(request_log_buffer)[-1]
    assert event["route"] == "/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate"
    assert event["principal_id"] == credential_id
    assert event["principal_label"] == "admin_credential"
    assert str(event["operation_id"]).startswith("op_")


def test_empty_playbill_init_reaches_the_typed_bootstrap_refusal(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = _create_host(app_client, "inst_empty_principals")
    headers, _credential_id = _runtime_credential_headers(
        monkeypatch,
        instance_id=instance_id,
        permission_mode=PermissionMode.ADMIN,
    )

    response = app_client.post(
        f"/api/v1/{instance_id}/playbill/init",
        json={"principals": []},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error_type"] == "PlaybillBootstrapError"


def test_bootstrap_secret_runtime_request_log_does_not_include_secret(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    monkeypatch.setenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", "bootstrap-secret")
    _clear_buffer(request_log_buffer)

    response = app_client.post(
        "/api/v1/runtime/instances",
        json={"instance_id": "inst_requestlog_bootstrap"},
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


def _make_request(path: str = "/api/v1/health") -> Request:
    """Build a minimal ASGI Request sufficient for log field extraction."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "path_params": {},
        "state": {},
    }
    return Request(scope)


def test_configure_request_logging_writes_to_default_durable_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    state_dir = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(state_dir))
    monkeypatch.delenv("CRUXIBLE_SERVER_LOG_PATH", raising=False)

    log_path = configure_request_logging()
    log_runtime_request(
        _make_request("/api/v1/test-log"),
        status=204,
        auth_context=None,
    )

    assert log_path == (state_dir / "logs" / "server.log").resolve()
    payload = json.loads(log_path.read_text().splitlines()[-1])
    assert payload["event"] == "runtime_request"
    assert payload["method"] == "GET"
    assert payload["route"] == "/api/v1/test-log"
    assert payload["status"] == 204
    assert payload["principal_id"] == "anonymous"


def test_rotating_file_log_sink_rotates_when_limit_is_exceeded(tmp_path: Path) -> None:
    log_path = tmp_path / "server.log"
    sink = _RotatingFileLogSink(log_path, max_bytes=20, backup_count=1)
    try:
        sink.write("first line\n")
        sink.flush()
        sink.write("second line exceeds\n")
        sink.flush()
    finally:
        sink.close()

    assert (tmp_path / "server.log.1").read_text() == "first line\n"
    assert log_path.read_text() == "second line exceeds\n"


def test_log_runtime_request_warns_once_when_durable_sink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request_log_buffer: io.StringIO,
) -> None:
    bad_log_path = tmp_path / "server.log"
    bad_log_path.mkdir()
    monkeypatch.setattr(request_logging_module, "_request_log_failure_warned", False)

    configure_request_logging(log_path=bad_log_path)
    log_runtime_request(_make_request(), status=200, auth_context=None)
    log_runtime_request(_make_request(), status=200, auth_context=None)

    stderr = capsys.readouterr().err
    assert stderr.count("Cruxible request log sink failed") == 1
    assert "runtime request logs may be dropped" in stderr


def test_log_runtime_request_swallows_broken_pipe_from_dead_sink(
    request_log_buffer: io.StringIO,
) -> None:
    """A dead log sink (EPIPE) must never propagate into request handling.

    Reproduces the daemon wedge: the request logger writes to an inherited
    pipe whose read end has closed, so the write raises BrokenPipeError. The
    ``request_log_buffer`` fixture restores a healthy sink on teardown.
    """
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # closing the reader makes every write raise EPIPE
    dead_writer = os.fdopen(write_fd, "w", buffering=1)
    try:
        # Sanity-check, on a throwaway handle to the same dead pipe, that a
        # write really does raise EPIPE — so this test exercises the guard
        # rather than a silently-healthy sink. Kept off ``dead_writer`` so no
        # buffered bytes linger to re-raise during teardown's flush/close.
        probe_fd = os.dup(write_fd)
        with pytest.raises(OSError):
            os.write(probe_fd, b"probe\n")
        os.close(probe_fd)

        # Configure the request logger like configure_request_logging(), but
        # point the sink at the broken pipe instead of stderr.
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.add_log_level,
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(file=dead_writer),
            cache_logger_on_first_use=False,
        )

        # The actual assertion: emitting a runtime request log must not raise,
        # even though the underlying sink is dead.
        log_runtime_request(
            _make_request(),
            status=200,
            auth_context=None,
        )
    finally:
        # The sink is broken, so flushing buffered bytes on close raises EPIPE;
        # that is expected here and unrelated to request handling.
        try:
            dead_writer.close()
        except OSError:
            pass


def test_configure_request_logging_is_callable() -> None:
    """configure_request_logging stays importable and runs without error."""
    configure_request_logging()
