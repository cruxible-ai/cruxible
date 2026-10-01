"""A floor export runs off the event loop, and its log line says how long it took.

`export_floor` was `async def` around a fully synchronous export, so a 3-15 s
export on real state stalled every other request on the daemon. The export here
is replaced by one that blocks until the test releases it. While it is blocked,
cheap requests must still be answered: if the export held the loop, they
could only finish after the export gave up waiting, and the export would
report that it timed out instead of being released. No wall-clock assertion is
made; the timeout only bounds the failure path.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from cruxible_client import contracts
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.server.routes import playbill as playbill_routes

_FAILURE_BOUND_SECONDS = 30.0
_DIGEST = "sha256:" + "0" * 64


@pytest.fixture
def request_log_buffer() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
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
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    # One portal for the client's lifetime, so requests from two threads share
    # one event loop, as they do in the daemon.
    with TestClient(create_app()) as test_client:
        yield test_client


def _runtime_request_events(buffer: io.StringIO) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in buffer.getvalue().splitlines():
        if line.startswith("{"):
            payload = json.loads(line)
            if payload.get("event") == "runtime_request":
                events.append(payload)
    return events


def test_a_slow_floor_export_does_not_block_a_concurrent_cheap_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    released_by_test: list[bool] = []

    def blocking_export(instance_id: str, **_: object) -> contracts.PlaybillFloorExport:
        entered.set()
        released_by_test.append(release.wait(_FAILURE_BOUND_SECONDS))
        coordinate = contracts.PlaybillAcceptedCoordinate(
            git_oid="0" * 40,
            semantic_root=_DIGEST,
            generation_root=_DIGEST,
            compiler_digest=_DIGEST,
        )
        return contracts.PlaybillFloorExport(
            tag="playbill-floor-export-v4", coordinate=coordinate, manifest={}, files=[]
        )

    monkeypatch.setattr(playbill_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(playbill_api, "playbill_export_floor", blocking_export)

    export_status: list[int] = []

    def export() -> None:
        response = client.post("/api/v1/inst_floor_slow/playbill/floor/export", json={})
        export_status.append(response.status_code)

    exporter = threading.Thread(target=export)
    exporter.start()
    try:
        assert entered.wait(_FAILURE_BOUND_SECONDS), "the export never reached its handler"
        # Unlogged and answered on the loop itself.
        health = client.get("/health")
        # Logged, and answered from the threadpool beside the export.
        info = client.get("/api/v1/server/info")
    finally:
        release.set()
        exporter.join(_FAILURE_BOUND_SECONDS)

    assert health.status_code == 200
    assert info.status_code == 200, info.text
    assert released_by_test == [True], (
        "the cheap request was answered only after the export stopped waiting, "
        "so the export was holding the event loop"
    )
    assert export_status == [200]

    events = {event["route"]: event for event in _runtime_request_events(request_log_buffer)}
    floor = events["/api/v1/{instance_id}/playbill/floor/export"]
    cheap = events["/api/v1/server/info"]
    assert isinstance(floor["duration_ms"], float)
    assert isinstance(cheap["duration_ms"], float)
    # The cheap request arrived after the export did and was logged before the
    # export was released, so the export's measured span contains it.
    assert floor["duration_ms"] >= cheap["duration_ms"]
