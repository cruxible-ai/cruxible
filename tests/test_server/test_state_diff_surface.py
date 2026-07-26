"""Surface parity for ``state diff``: hidden HTTP routes, client, and CLI.

The hidden POST/GET pair rides ``wi-030-surface-commit`` for exposure, so the
routes are absent from the OpenAPI document by design; parity is proven by
driving the real ``CruxibleClient`` and the real CLI over them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.graph.types import EntityInstance
from cruxible_core.mcp import handlers
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from cruxible_core.service import service_add_entities
from cruxible_core.service.snapshots import service_create_snapshot
from tests.test_cli.conftest import CAR_PARTS_YAML


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    return TestClient(create_app())


def _seeded_instance(app_client: TestClient, tmp_path: Path) -> tuple[str, str]:
    """Init a governed instance, snapshot it, then add one entity."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    response = app_client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CAR_PARTS_YAML},
    )
    assert response.status_code == 200
    instance_id = response.json()["instance_id"]

    instance = get_manager().get(instance_id)
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-9000",
                properties={
                    "part_number": "BP-9000",
                    "name": "Track Brake Pads",
                    "category": "brakes",
                },
            )
        ],
    )
    return instance_id, snapshot.snapshot_id


def test_hidden_post_route_and_get_retrieval_round_trip(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id, snapshot_id = _seeded_instance(app_client, tmp_path)

    response = app_client.post(
        f"/api/v1/{instance_id}/state/diff",
        json={"from_coordinate": snapshot_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["added"] == 1
    assert body["artifact_complete"] is True
    assert body["receipt_id"]

    retrieval = app_client.get(f"/api/v1/{instance_id}/state/diff/artifacts/{body['diff_digest']}")
    assert retrieval.status_code == 200
    artifact = retrieval.json()
    assert artifact["diff_digest"] == body["diff_digest"]
    assert artifact["content"]["summary"] == body["summary"]


def test_routes_are_hidden_from_the_openapi_document(app_client: TestClient) -> None:
    paths = app_client.get("/openapi.json").json()["paths"]
    assert "/api/v1/{instance_id}/state/diff" not in paths
    assert "/api/v1/{instance_id}/state/diff/artifacts/{diff_digest}" not in paths


def test_client_parity_for_both_routes(app_client: TestClient, tmp_path: Path) -> None:
    instance_id, snapshot_id = _seeded_instance(app_client, tmp_path)
    # The real CLI client speaks its own httpx.Client; swap in the in-process
    # TestClient so parity is proven against the actual FastAPI routes.
    client = CruxibleClient(base_url="http://cruxible-daemon")
    client._client = app_client

    result = client.state_diff(instance_id, from_coordinate=snapshot_id)
    assert result.summary["added"] == 1
    assert result.selector["sections"] is None

    artifact = client.state_diff_artifact(instance_id, result.diff_digest)
    assert artifact.diff_digest == result.diff_digest
    assert artifact.byte_count == result.artifact_ref.byte_count


def test_unknown_artifact_digest_is_a_structured_error(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id, _snapshot_id = _seeded_instance(app_client, tmp_path)
    response = app_client.get(f"/api/v1/{instance_id}/state/diff/artifacts/sha256:{'0' * 64}")
    assert response.status_code == 400
    assert response.json()["error_type"] == "ConfigError"


def test_cli_renders_the_diff_and_reads_the_artifact_back(tmp_path: Path) -> None:
    root = tmp_path / "local"
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    snapshot = service_create_snapshot(instance).snapshot
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-9000",
                properties={
                    "part_number": "BP-9000",
                    "name": "Track Brake Pads",
                    "category": "brakes",
                },
            )
        ],
    )

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        import os

        os.chdir(root)
        human = runner.invoke(cli, ["state", "diff", snapshot.snapshot_id])
        assert human.exit_code == 0, human.output
        assert "diff digest:" in human.output
        assert "+ Part:BP-9000" in human.output

        structured = runner.invoke(cli, ["state", "diff", snapshot.snapshot_id, "--json"])
        assert structured.exit_code == 0, structured.output
        payload = json.loads(structured.output)
        assert payload["summary"]["added"] == 1

        artifact = runner.invoke(cli, ["state", "diff", "--artifact", payload["diff_digest"]])
        assert artifact.exit_code == 0, artifact.output
        assert json.loads(artifact.output)["summary"] == payload["summary"]


def test_mcp_tool_dispatches_to_both_shapes(app_client: TestClient, tmp_path: Path) -> None:
    instance_id, snapshot_id = _seeded_instance(app_client, tmp_path)
    result = handlers.handle_state_diff(instance_id, from_coordinate=snapshot_id)
    assert result.summary["added"] == 1
    artifact = handlers.handle_state_diff_artifact(instance_id, result.diff_digest)
    assert artifact.diff_digest == result.diff_digest
