"""Instance-local telemetry across storage, service, HTTP, MCP, and CLI boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.cli.main import cli
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.service import service_telemetry_summary
from cruxible_core.telemetry.instrumentation import record_boundary
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
    get_manager().clear()
    yield
    get_manager().clear()
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


def test_busy_telemetry_store_drops_observation_without_failing(
    instance: CruxibleInstance,
) -> None:
    instance.get_boundary_telemetry_summary()
    connection = sqlite3.connect(instance.get_instance_dir() / "state.db")
    connection.execute("BEGIN IMMEDIATE")
    try:
        instance.record_boundary_telemetry(
            "service_stats",
            response_bytes=10,
            duration_ms=1.0,
            error=False,
        )
    finally:
        connection.rollback()
        connection.close()

    assert service_telemetry_summary(instance).counters == []


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


def test_cli_counts_emitted_json_under_the_invoked_service_verb(
    instance: CruxibleInstance,
) -> None:
    original = os.getcwd()
    try:
        os.chdir(instance.get_root_path())
        result = CliRunner().invoke(cli, ["stats", "--json"])
    finally:
        os.chdir(original)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["entity_count"] == 0
    counter = _counter(instance.get_boundary_telemetry_summary(), "service_stats")
    assert counter.call_count == 1
    assert counter.error_count == 0
    assert counter.total_response_bytes == len(result.output.encode("utf-8"))


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
    assert (
        _counter(
            instance.get_boundary_telemetry_summary(),
            "service_telemetry_summary",
        ).call_count
        == 1
    )
