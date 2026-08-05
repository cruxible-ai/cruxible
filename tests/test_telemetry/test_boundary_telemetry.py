"""Instance-local telemetry across storage, service, HTTP, MCP, and CLI boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.cli.commands.server import server_start_cmd
from cruxible_core.cli.main import cli, handle_errors, long_running_command
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import PermissionMode, reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import (
    get_runtime_credential_store,
    reset_runtime_credential_store,
)
from cruxible_core.server.registry import reset_registry
from cruxible_core.service import service_telemetry_summary
from cruxible_core.telemetry.buffer import (
    MAX_BUFFERED_SURFACES,
    BoundaryTelemetryBuffer,
    reset_boundary_telemetry_buffers,
    telemetry_buffer,
)
from cruxible_core.telemetry.instrumentation import (
    MAX_CLI_BOUNDARY_EVENTS,
    CliBoundaryCollector,
    CliBoundaryEvent,
    record_boundary,
)
from cruxible_core.telemetry.store import SQLiteTelemetryStore
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture(autouse=True)
def reset_runtime_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_REQUIRE_SERVER", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    reset_permissions()
    reset_client_cache()
    reset_registry()
    reset_runtime_credential_store()
    reset_boundary_telemetry_buffers()
    get_manager().clear()
    yield
    get_manager().clear()
    reset_boundary_telemetry_buffers()
    reset_client_cache()
    reset_registry()
    reset_runtime_credential_store()
    reset_permissions()


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    root = tmp_path / "project"
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    return CruxibleInstance.init(root, "config.yaml")


def _counter(summary: Any, surface_name: str) -> Any:
    return next(counter for counter in summary.counters if counter.surface_name == surface_name)


def _surface_names(summary: Any) -> set[str]:
    return {counter.surface_name for counter in summary.counters}


def _break_telemetry_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every telemetry write path raise where it is really called.

    Both halves of the write path are broken: the in-memory merge that runs on
    the request/command path, and the batched SQLite write behind it. A capture
    site that only guards its innermost call would survive one and not the
    other, so tests using this fixture prove the WHOLE site is guarded.
    """

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("telemetry store unavailable")

    monkeypatch.setattr(BoundaryTelemetryBuffer, "add", explode)
    monkeypatch.setattr(SQLiteTelemetryStore, "merge_best_effort", explode)


def test_storage_aggregates_in_place_without_advancing_read_revision(
    instance: CruxibleInstance,
) -> None:
    revision = instance.get_read_revision()

    instance.record_boundary_telemetry(
        "cruxible_query",
        response_bytes=120,
        duration_ms=4.5,
        error=False,
    )
    instance.record_boundary_telemetry(
        "cruxible_query",
        response_bytes=30,
        duration_ms=7.25,
        error=True,
    )

    summary = service_telemetry_summary(instance)
    counter = _counter(summary, "cruxible_query")
    assert summary.earliest_recorded_at is not None
    assert counter.call_count == 2
    assert counter.error_count == 1
    assert counter.total_response_bytes == 150
    assert counter.total_duration_ms == 11.75
    assert counter.max_duration_ms == 7.25
    assert instance.get_read_revision() == revision

    with sqlite3.connect(instance.get_instance_dir() / "state.db") as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM boundary_telemetry").fetchone()[0]
    assert row_count == 1


def test_recording_does_no_storage_work_on_the_calling_path(
    instance: CruxibleInstance,
) -> None:
    """An observation is a dict merge: it must not open the state DB at all.

    This is the property that made HTTP capture affordable — the earlier
    per-call connect/PRAGMA/INSERT/commit/close cost about a millisecond of
    every request, on the event loop.
    """
    connects: list[Any] = []
    real_connect = sqlite3.connect

    def counting_connect(*args: Any, **kwargs: Any) -> Any:
        connects.append(args[:1])
        return real_connect(*args, **kwargs)

    buffer = telemetry_buffer(instance.get_instance_dir() / "state.db")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", counting_connect)
        for _ in range(50):
            instance.record_boundary_telemetry(
                "stats",
                response_bytes=10,
                duration_ms=1.0,
                error=False,
            )
    assert connects == []

    buffer.flush()
    assert _counter(instance.get_boundary_telemetry_summary(), "stats").call_count == 50


def test_busy_telemetry_store_drops_the_flushed_batch_without_failing(
    instance: CruxibleInstance,
) -> None:
    """A writer holding the DB costs counters, never a wait and never an error."""
    instance.get_boundary_telemetry_summary()
    buffer = telemetry_buffer(instance.get_instance_dir() / "state.db")
    instance.record_boundary_telemetry(
        "service_stats",
        response_bytes=10,
        duration_ms=1.0,
        error=False,
    )

    connection = sqlite3.connect(instance.get_instance_dir() / "state.db")
    connection.execute("BEGIN IMMEDIATE")
    try:
        buffer.flush()
    finally:
        connection.rollback()
        connection.close()

    assert service_telemetry_summary(instance).counters == []


def test_flush_before_schema_exists_drops_rather_than_migrating(tmp_path: Path) -> None:
    """The documented first-drop: the flusher never initializes the schema.

    Taking the migration lock from the background flusher could block real work
    on a DB nobody has opened normally yet, so a batch aimed at an
    uninitialized state DB is dropped instead. Reads initialize first, so no
    caller can observe the gap.
    """
    bare_db = tmp_path / "bare-state.db"
    sqlite3.connect(bare_db).close()
    buffer = BoundaryTelemetryBuffer(str(bare_db))
    buffer.add("stats", response_bytes=1, duration_ms=1.0, error=False)

    buffer.flush()

    with sqlite3.connect(bare_db) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in rows}
    assert "boundary_telemetry" not in tables


def test_buffer_caps_surfaces_and_counts_the_overflow(tmp_path: Path) -> None:
    buffer = BoundaryTelemetryBuffer(str(tmp_path / "state.db"))

    for index in range(MAX_BUFFERED_SURFACES + 25):
        buffer.add(f"surface-{index}", response_bytes=1, duration_ms=1.0, error=False)

    assert buffer.dropped_observations == 25


def test_recorder_failure_never_escapes() -> None:
    class FailingInstance:
        def record_boundary_telemetry(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("telemetry unavailable")

    record_boundary(
        FailingInstance(),
        "stats",
        response_bytes=10,
        duration_ms=1.0,
        error=False,
    )


def test_http_counts_exact_serialized_bytes_and_errors(
    tmp_path: Path,
) -> None:
    project = tmp_path / "http-project"
    project.mkdir()
    (project / "config.yaml").write_text(CAR_PARTS_YAML)
    client = TestClient(create_app())
    initialized = client.post(
        "/api/v1/instances",
        json={"root_dir": str(project), "config_yaml": CAR_PARTS_YAML},
    )
    assert initialized.status_code == 200
    instance_id = initialized.json()["instance_id"]

    stats_response = client.get(f"/api/v1/{instance_id}/stats")
    missing_response = client.get(f"/api/v1/{instance_id}/entities/TypoType/does-not-exist")
    summary_response = client.get(f"/api/v1/{instance_id}/telemetry/summary")

    assert stats_response.status_code == 200
    assert missing_response.status_code == 404
    assert summary_response.status_code == 200
    summary = summary_response.json()
    counters = {counter["surface_name"]: counter for counter in summary["counters"]}
    assert counters["stats"]["total_response_bytes"] == len(stats_response.content)
    assert counters["stats"]["error_count"] == 0
    assert counters["get_entity"]["total_response_bytes"] == len(missing_response.content)
    assert counters["get_entity"]["error_count"] == 1
    assert "telemetry_summary" not in counters
    assert summary["earliest_recorded_at"] is not None


def test_http_capture_never_resolves_an_instance_the_route_rejects(
    tmp_path: Path,
) -> None:
    """The middleware resolves exactly what ``resolve_server_instance_id`` does.

    A raw path segment is attacker-controlled. The instance manager's fallback
    would happily ``CruxibleInstance.load(Path(segment))`` a local dev instance
    off disk; the middleware must not reach it for a request the router itself
    refused.
    """
    local_root = tmp_path / "local-project"
    local_root.mkdir()
    (local_root / "config.yaml").write_text(CAR_PARTS_YAML)
    local_instance = CruxibleInstance.init(local_root, "config.yaml")
    client = TestClient(create_app())

    original = os.getcwd()
    try:
        # A bare directory name is a legal path segment, so the instance
        # manager's dev-mode fallback WOULD find this instance on disk.
        os.chdir(tmp_path)
        denied = client.get("/api/v1/local-project/stats")
    finally:
        os.chdir(original)

    assert denied.status_code == 404
    assert denied.json()["error_type"] == "InstanceNotFoundError"
    assert get_manager().list_ids() == []
    assert local_instance.get_boundary_telemetry_summary().counters == []


def test_http_capture_skips_instance_scope_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 is not the addressed instance's traffic, so it is not its counter."""
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    (allowed_root / "config.yaml").write_text(CAR_PARTS_YAML)
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "config.yaml").write_text(CAR_PARTS_YAML)

    client = TestClient(create_app())
    allowed_id = client.post(
        "/api/v1/instances",
        json={"root_dir": str(allowed_root), "config_yaml": CAR_PARTS_YAML},
    ).json()["instance_id"]
    other_id = client.post(
        "/api/v1/instances",
        json={"root_dir": str(other_root), "config_yaml": CAR_PARTS_YAML},
    ).json()["instance_id"]

    scoped = get_runtime_credential_store().create_credential(
        instance_id=allowed_id,
        label="scoped",
        permission_mode=PermissionMode.ADMIN,
        created_by="test",
    )
    monkeypatch.setenv("CRUXIBLE_SERVER_AUTH", "true")
    headers = {"Authorization": f"Bearer {scoped.token}"}

    refused = client.get(f"/api/v1/{other_id}/stats", headers=headers)

    assert refused.status_code == 403
    assert refused.json()["error_type"] == "InstanceScopeError"
    refused_summary = get_manager().get(other_id).get_boundary_telemetry_summary()
    assert "stats" not in _surface_names(refused_summary)


def test_http_counters_sum_correctly_under_concurrent_requests(
    tmp_path: Path,
) -> None:
    """Parallel requests must neither race the counters nor raise."""
    project = tmp_path / "concurrent-project"
    project.mkdir()
    (project / "config.yaml").write_text(CAR_PARTS_YAML)
    client = TestClient(create_app())
    instance_id = client.post(
        "/api/v1/instances",
        json={"root_dir": str(project), "config_yaml": CAR_PARTS_YAML},
    ).json()["instance_id"]

    request_count = 40
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(
            pool.map(
                lambda _: client.get(f"/api/v1/{instance_id}/stats").status_code,
                range(request_count),
            )
        )

    assert statuses == [200] * request_count
    counter = _counter(get_manager().get(instance_id).get_boundary_telemetry_summary(), "stats")
    assert counter.call_count == request_count
    assert counter.error_count == 0


def test_concurrent_buffer_writers_lose_no_observations(instance: CruxibleInstance) -> None:
    """The buffer lock protects the merge; a concurrent flush loses nothing."""
    buffer = telemetry_buffer(instance.get_instance_dir() / "state.db")
    instance.get_boundary_telemetry_summary()
    barrier = threading.Barrier(4)

    def record_many() -> None:
        barrier.wait()
        for _ in range(250):
            buffer.add("stats", response_bytes=2, duration_ms=1.0, error=False)

    threads = [threading.Thread(target=record_many) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    counter = _counter(instance.get_boundary_telemetry_summary(), "stats")
    assert counter.call_count == 1000
    assert counter.total_response_bytes == 2000


def test_http_request_succeeds_when_the_telemetry_store_throws(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "failopen-http"
    project.mkdir()
    (project / "config.yaml").write_text(CAR_PARTS_YAML)
    client = TestClient(create_app())
    instance_id = client.post(
        "/api/v1/instances",
        json={"root_dir": str(project), "config_yaml": CAR_PARTS_YAML},
    ).json()["instance_id"]
    _break_telemetry_store(monkeypatch)

    response = client.get(f"/api/v1/{instance_id}/stats")

    assert response.status_code == 200
    assert response.json()["entity_count"] == 0


def test_mcp_counts_the_already_serialized_text_payload(instance: CruxibleInstance) -> None:
    instance_id = "telemetry-mcp"
    get_manager().register(instance_id, instance)
    server = create_server()

    result = asyncio.run(server.call_tool("cruxible_stats", {"instance_id": instance_id}))

    content = result[0] if isinstance(result, tuple) else result
    expected_bytes = sum(len(block.text.encode("utf-8")) for block in content)
    counter = _counter(instance.get_boundary_telemetry_summary(), "cruxible_stats")
    assert counter.call_count == 1
    assert counter.error_count == 0
    assert counter.total_response_bytes == expected_bytes

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "cruxible_get_entity",
                {
                    "instance_id": instance_id,
                    "entity_type": "TypoType",
                    "entity_id": "ANY",
                },
            )
        )
    error_counter = _counter(
        instance.get_boundary_telemetry_summary(),
        "cruxible_get_entity",
    )
    assert error_counter.call_count == 1
    assert error_counter.error_count == 1
    assert error_counter.total_response_bytes > 0


def test_mcp_tool_call_succeeds_when_the_telemetry_store_throws(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "telemetry-mcp-failopen"
    get_manager().register(instance_id, instance)
    server = create_server()
    _break_telemetry_store(monkeypatch)

    result = asyncio.run(server.call_tool("cruxible_stats", {"instance_id": instance_id}))

    content = result[0] if isinstance(result, tuple) else result
    assert content


def test_mcp_capture_survives_a_result_it_cannot_measure(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A byte helper that raises must not alter the tool result.

    The helpers walk objects core does not own: exception ``__str__``, rendered
    content blocks, and a caller-supplied ``arguments`` that need not be a
    mapping. Whatever they do, the caller still gets its result.
    """
    instance_id = "telemetry-mcp-raising-helper"
    get_manager().register(instance_id, instance)
    server = create_server()

    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("cannot measure this payload")

    monkeypatch.setattr(
        "cruxible_core.mcp.server._serialized_mcp_response_bytes",
        explode,
    )

    result = asyncio.run(server.call_tool("cruxible_stats", {"instance_id": instance_id}))

    content = result[0] if isinstance(result, tuple) else result
    assert content
    assert "cruxible_stats" not in _surface_names(instance.get_boundary_telemetry_summary())


def test_cli_records_command_bytes_apart_from_per_verb_durations(
    instance: CruxibleInstance,
) -> None:
    """Bytes and wall time belong to the command; duration belongs to the verb."""
    original = os.getcwd()
    try:
        os.chdir(instance.get_root_path())
        result = CliRunner().invoke(cli, ["stats", "--json"])
    finally:
        os.chdir(original)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["entity_count"] == 0
    summary = instance.get_boundary_telemetry_summary()

    verb = _counter(summary, "service_stats")
    assert verb.call_count == 1
    assert verb.error_count == 0
    # The rendered output is the command's, not this verb's.
    assert verb.total_response_bytes == 0

    command = _counter(summary, "cli:stats")
    assert command.call_count == 1
    assert command.error_count == 0
    assert command.total_response_bytes == len(result.output.encode("utf-8"))
    # The verb ran inside the command, so it cannot have taken longer than it.
    assert verb.total_duration_ms <= command.total_duration_ms


def test_cli_command_succeeds_when_the_telemetry_store_throws(
    instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_telemetry_store(monkeypatch)
    original = os.getcwd()
    try:
        os.chdir(instance.get_root_path())
        result = CliRunner().invoke(cli, ["stats", "--json"])
    finally:
        os.chdir(original)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["entity_count"] == 0


def test_cli_collector_caps_events_and_counts_the_overflow() -> None:
    collector = CliBoundaryCollector()

    for _ in range(MAX_CLI_BOUNDARY_EVENTS + 10):
        collector.add(
            CliBoundaryEvent(
                instance=object(),
                surface_name="service_stats",
                duration_ms=1.0,
                error=False,
            )
        )

    assert len(collector.events) == MAX_CLI_BOUNDARY_EVENTS
    assert collector.dropped_events == 10


def test_server_start_opts_out_of_cli_collection_and_stream_proxies() -> None:
    """The daemon must not run inside the CLI collector.

    ``server start`` returns only as the process shuts down. Collecting inside
    it would accumulate one undrained event per served request, replay them all
    at shutdown carrying the daemon's whole wall time, double-count every
    request the HTTP middleware already counted, and leave structlog bound to a
    counting stream proxy for the process lifetime.
    """
    assert getattr(server_start_cmd.callback, "_cruxible_long_running", False) is True

    observed: dict[str, Any] = {}

    @handle_errors
    @long_running_command
    def marked_command() -> None:
        from cruxible_core.telemetry.instrumentation import _CLI_COLLECTOR

        observed["collector"] = _CLI_COLLECTOR.get()
        observed["stdout_proxied"] = type(__import__("sys").stdout).__name__ == "_CountingTextIO"

    with click.Context(click.Command("marked")):
        marked_command()

    assert observed["collector"] is None
    assert observed["stdout_proxied"] is False


def test_cli_telemetry_summary_reports_prior_calls(instance: CruxibleInstance) -> None:
    instance.record_boundary_telemetry(
        "stats",
        response_bytes=42,
        duration_ms=2.0,
        error=False,
    )
    original = os.getcwd()
    try:
        os.chdir(instance.get_root_path())
        result = CliRunner().invoke(cli, ["telemetry", "summary", "--json"])
    finally:
        os.chdir(original)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["earliest_recorded_at"] is not None
    assert payload["counters"][0]["surface_name"] == "stats"
    summary = instance.get_boundary_telemetry_summary()
    assert _counter(summary, "service_telemetry_summary").call_count == 1
    assert _counter(summary, "cli:telemetry summary").total_response_bytes == len(
        result.output.encode("utf-8")
    )
