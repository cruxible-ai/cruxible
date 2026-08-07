"""Read-only HTTP surface over the binding ledger."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from cruxible_core.bindings.types import ProviderDescriptor, SlotInterface
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from cruxible_core.service.bindings import (
    service_create_slot_binding,
    service_rebind_slot,
)
from tests.test_bindings.conftest import MINIMAL_CONFIG_YAML

INSTALL = "inst-prod-1"

SLOT = SlotInterface(
    slot_name="summarize",
    contract_in="doc.v1",
    contract_out="summary.v1",
)
PROVIDER = ProviderDescriptor(
    provider_name="summarizer-core",
    contract_in="doc.v1",
    contract_out="summary.v1",
    billing_mode="included",
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_client_cache()
    get_manager().clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_manager().clear()
    reset_registry()


def _init(client: TestClient, root: Path) -> str:
    root.mkdir()
    response = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": MINIMAL_CONFIG_YAML},
    )
    assert response.status_code == 200, response.text
    return cast(str, response.json()["instance_id"])


def _seed(instance_id: str, actor: GovernedActorContext) -> str:
    """Write the ledger through the service; the HTTP surface is read-only."""
    instance = get_manager().get(instance_id)
    created = service_create_slot_binding(
        instance,
        install_id=INSTALL,
        slot=SLOT,
        provider=PROVIDER,
        actor_context=actor,
    )
    service_rebind_slot(
        instance,
        install_id=INSTALL,
        slot=SLOT,
        provider=PROVIDER.model_copy(update={"provider_name": "summarizer-fast"}),
        note="cost tuning",
        actor_context=actor,
    )
    return created.binding.binding_id


def test_list_route_returns_the_standard_envelope(
    client: TestClient, tmp_path: Path, actor: GovernedActorContext
) -> None:
    instance_id = _init(client, tmp_path / "proj")
    _seed(instance_id, actor)

    response = client.get(f"/api/v1/{instance_id}/slot-bindings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["truncated"] is False
    assert body["read_revision"] is not None
    assert body["items"][0]["provider_name"] == "summarizer-fast"
    assert body["items"][0]["revision"] == 2
    assert body["items"][0]["contract_in"] == "doc.v1"


def test_list_route_filters_by_install_and_status(
    client: TestClient, tmp_path: Path, actor: GovernedActorContext
) -> None:
    instance_id = _init(client, tmp_path / "proj")
    _seed(instance_id, actor)

    hit = client.get(
        f"/api/v1/{instance_id}/slot-bindings",
        params={"install_id": INSTALL, "status": "active"},
    )
    assert hit.json()["total"] == 1

    miss = client.get(
        f"/api/v1/{instance_id}/slot-bindings",
        params={"install_id": "inst-nowhere"},
    )
    assert miss.json()["total"] == 0
    assert miss.json()["items"] == []


def test_history_route_returns_every_revision_oldest_first(
    client: TestClient, tmp_path: Path, actor: GovernedActorContext
) -> None:
    instance_id = _init(client, tmp_path / "proj")
    binding_id = _seed(instance_id, actor)

    response = client.get(f"/api/v1/{instance_id}/slot-bindings/{binding_id}/history")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["binding_id"] == binding_id
    assert [row["revision"] for row in body["items"]] == [1, 2]
    assert [row["change_kind"] for row in body["items"]] == ["bind", "rebind"]
    assert body["items"][1]["note"] == "cost tuning"


def test_history_route_404s_on_an_unknown_binding(client: TestClient, tmp_path: Path) -> None:
    instance_id = _init(client, tmp_path / "proj")
    response = client.get(f"/api/v1/{instance_id}/slot-bindings/bnd_missing/history")
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["error_type"] == "BindingNotFoundError"
    assert body["error_code"] == "binding_not_found"
    assert body["context"]["binding_id"] == "bnd_missing"


def test_list_route_rejects_an_unknown_status(client: TestClient, tmp_path: Path) -> None:
    instance_id = _init(client, tmp_path / "proj")
    response = client.get(f"/api/v1/{instance_id}/slot-bindings", params={"status": "paused"})
    assert response.status_code == 400, response.text
    assert "active, retired" in response.json()["message"]
