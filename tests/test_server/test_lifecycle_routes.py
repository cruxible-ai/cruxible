"""Hidden daemon parity and auth-off attribution for lifecycle verbs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_attestations.conftest import CONFIG_YAML


@pytest.fixture
def lifecycle_client(
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


def _init_and_seed(client: TestClient, root: Path) -> tuple[str, dict[tuple[str, str], str]]:
    root.mkdir()
    initialized = client.post(
        "/api/v1/instances",
        json={"root_dir": str(root), "config_yaml": CONFIG_YAML},
    )
    assert initialized.status_code == 200, initialized.text
    instance_id = cast(str, initialized.json()["instance_id"])
    entities = client.post(
        f"/api/v1/{instance_id}/entities",
        json={
            "entities": [
                {
                    "entity_type": "Service",
                    "entity_id": service_id,
                    "properties": {"service_id": service_id},
                }
                for service_id in ("svc-1", "svc-2")
            ]
            + [
                {
                    "entity_type": "Control",
                    "entity_id": control_id,
                    "properties": {"control_id": control_id},
                }
                for control_id in ("ctl-1", "ctl-2")
            ]
        },
    )
    assert entities.status_code == 200, entities.text
    relationships = client.post(
        f"/api/v1/{instance_id}/relationships",
        json={
            "relationships": [
                {
                    "relationship_type": "protected_by",
                    "from_type": "Service",
                    "from_id": from_id,
                    "to_type": "Control",
                    "to_id": to_id,
                    "properties": {"severity": "high"},
                }
                for from_id, to_id in (
                    ("svc-1", "ctl-1"),
                    ("svc-1", "ctl-2"),
                    ("svc-2", "ctl-1"),
                )
            ]
        },
    )
    assert relationships.status_code == 200, relationships.text
    listed = client.get(
        f"/api/v1/{instance_id}/list/edges",
        params={"relationship_type": "protected_by"},
    )
    assert listed.status_code == 200, listed.text
    claim_ids = {
        (item["from_id"], item["to_id"]): item["claim_id"]
        for item in cast(list[dict[str, Any]], listed.json()["items"])
    }
    return instance_id, claim_ids


def test_hidden_lifecycle_routes_cover_all_verbs_and_mint_local_operator(
    lifecycle_client: TestClient,
    tmp_path: Path,
) -> None:
    instance_id, claims = _init_and_seed(lifecycle_client, tmp_path / "workspace")
    superseded = lifecycle_client.post(
        f"/api/v1/{instance_id}/claims/{claims[('svc-1', 'ctl-1')]}/supersede",
        json={
            "successor_claim_id": claims[("svc-1", "ctl-2")],
            "reason": "control replacement",
            "evidence_ref": {"source": "test", "source_record_id": "supersede-http"},
        },
    )
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["claim"]["metadata"]["assertion"]["lifecycle"]["closed_by"] == (
        "operator"
    )
    assert superseded.json()["receipt_id"]

    retracted = lifecycle_client.post(
        f"/api/v1/{instance_id}/claims/{claims[('svc-2', 'ctl-1')]}/retract",
        json={"reason": "claim withdrawn"},
    )
    assert retracted.status_code == 200, retracted.text
    assert retracted.json()["claim"]["metadata"]["assertion"]["lifecycle"]["status"] == (
        "retracted"
    )

    entity_superseded = lifecycle_client.post(
        f"/api/v1/{instance_id}/entities/Control/ctl-1/supersede",
        json={
            "successor_entity_type": "Control",
            "successor_entity_id": "ctl-2",
            "reason": "control renamed",
        },
    )
    assert entity_superseded.status_code == 200, entity_superseded.text
    assert entity_superseded.json()["entity"]["metadata"]["lifecycle"]["status"] == ("superseded")

    retired = lifecycle_client.post(
        f"/api/v1/{instance_id}/entities/Service/svc-2/retire",
        json={"reason": "service removed"},
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["stranded_live_edge_count"] == 0


def test_lifecycle_routes_are_hidden_from_frozen_openapi() -> None:
    paths = create_app().openapi()["paths"]
    assert all("/claims/{claim_id}/supersede" not in path for path in paths)
    assert all("/claims/{claim_id}/retract" not in path for path in paths)
    assert all("/{entity_id}/supersede" not in path for path in paths)
    assert all("/{entity_id}/retire" not in path for path in paths)


def test_http_lifecycle_routes_refuse_an_empty_reason_and_change_nothing(
    lifecycle_client: TestClient,
    tmp_path: Path,
) -> None:
    """A blank reason is refused over HTTP, on both kinds, with no state change.

    The required reason is a governance property, not client-side politeness:
    a settled transition with no reason is the corpus starving itself. Pin that
    the daemon enforces it (whitespace does not satisfy it) and that the refused
    call leaves the subject exactly as it found it.
    """
    instance_id, claims = _init_and_seed(lifecycle_client, tmp_path / "workspace")

    refused_claim = lifecycle_client.post(
        f"/api/v1/{instance_id}/claims/{claims[('svc-2', 'ctl-1')]}/retract",
        json={"reason": "   "},
    )
    assert refused_claim.status_code == 400, refused_claim.text
    assert "requires a non-empty reason" in refused_claim.json()["message"]

    refused_entity = lifecycle_client.post(
        f"/api/v1/{instance_id}/entities/Service/svc-2/retire",
        json={"reason": ""},
    )
    assert refused_entity.status_code == 400, refused_entity.text
    assert "requires a non-empty reason" in refused_entity.json()["message"]

    # Nothing moved: the claim is still active and the entity still live.
    edges = lifecycle_client.get(
        f"/api/v1/{instance_id}/list/edges",
        params={"relationship_state": "all"},
    ).json()
    claim_row = next(
        item for item in edges["items"] if item["claim_id"] == claims[("svc-2", "ctl-1")]
    )
    assert claim_row["metadata"]["assertion"]["lifecycle"]["status"] == "active"

    entity = lifecycle_client.get(f"/api/v1/{instance_id}/entities/Service/svc-2").json()
    lifecycle = entity["metadata"].get("lifecycle")
    assert lifecycle is None or lifecycle["status"] == "live"
