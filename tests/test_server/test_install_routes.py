"""HTTP surface for the install ledger: reads only, in phase 1."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from cruxible_core.service import (
    service_advance_install_phase,
    service_create_install,
    service_record_owned_object,
)
from tests.test_installs.conftest import MINIMAL_CONFIG, actor


@pytest.fixture
def install_client(
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


def _init(client: TestClient, root: Path) -> str:
    root.mkdir()
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": MINIMAL_CONFIG},
    )
    assert response.status_code == 200, response.text
    return cast(str, response.json()["instance_id"])


def _seed(instance_id: str) -> str:
    """Seed one install through the service layer (there is no write route)."""
    instance = get_manager().get(instance_id)
    record = service_create_install(
        instance,
        artifact_kind="blueprint",
        artifact_id="kev-triage",
        artifact_version="1.0.0",
        artifact_digest="sha256:blueprint-a",
        actor_context=actor(),
        install_id="inst-seeded",
    )
    service_record_owned_object(
        instance,
        record.install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        installed_digest="sha256:q1",
        actor_context=actor(),
    )
    for phase in ("pending_acceptance", "active"):
        service_advance_install_phase(
            instance,
            record.install_id,
            to_phase=phase,
            actor_context=actor(),  # type: ignore[arg-type]
        )
    return record.install_id


def test_list_installs_returns_the_standard_envelope(
    install_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(install_client, tmp_path / "inst")
    _seed(instance_id)

    response = install_client.get(f"/api/v1/{instance_id}/installs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["offset"] == 0
    assert body["truncated"] is False
    assert body["items"][0]["install_id"] == "inst-seeded"
    assert body["items"][0]["phase"] == "active"


def test_list_installs_filters_by_phase(
    install_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(install_client, tmp_path / "inst")
    _seed(instance_id)

    active = install_client.get(f"/api/v1/{instance_id}/installs", params={"phase": "active"})
    assert active.json()["total"] == 1

    failed = install_client.get(f"/api/v1/{instance_id}/installs", params={"phase": "failed"})
    assert failed.json()["total"] == 0
    assert failed.json()["items"] == []


def test_an_unknown_phase_filter_is_refused_not_silently_empty(
    install_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(install_client, tmp_path / "inst")
    _seed(instance_id)

    response = install_client.get(f"/api/v1/{instance_id}/installs", params={"phase": "instaled"})
    assert response.status_code == 400
    assert "unknown install phase" in response.json()["message"]


def test_install_detail_carries_owned_objects_and_phase_history(
    install_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(install_client, tmp_path / "inst")
    install_id = _seed(instance_id)

    response = install_client.get(f"/api/v1/{instance_id}/installs/{install_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["install"]["artifact"]["artifact_id"] == "kev-triage"
    assert [item["object_name"] for item in body["owned_objects"]] == ["pub.kev.queue"]
    assert [event["to_phase"] for event in body["phase_history"]] == [
        "preparing",
        "pending_acceptance",
        "active",
    ]


def test_unknown_install_returns_the_404_error_envelope(
    install_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id = _init(install_client, tmp_path / "inst")

    response = install_client.get(f"/api/v1/{instance_id}/installs/inst-missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error_type"] == "InstallNotFoundError"
    assert body["context"]["install_id"] == "inst-missing"


def test_the_install_surface_exposes_no_write_routes(install_client: TestClient) -> None:
    """Phase 1 ships reads only; writes stay behind the (unbuilt) installer."""
    paths = install_client.app.openapi()["paths"]  # type: ignore[attr-defined]
    install_paths = {path: entry for path, entry in paths.items() if "/installs" in path}
    assert install_paths, "install routes are not registered"
    assert all(set(entry) == {"get"} for entry in install_paths.values()), install_paths
