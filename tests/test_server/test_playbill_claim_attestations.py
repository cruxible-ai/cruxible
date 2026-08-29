"""HTTP Claim-attestation append shares the typed service contract and actor context."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.playbill.claim_attestation_store import ClaimAttestationStoreError
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.errors import error_to_response
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.playbill_claim_attestations import service_append_claim_attestation
from tests.test_playbill.test_claim_attestation_service import _request
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world


@pytest.fixture
def attestation_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    world = tmp_path / "world"
    world.mkdir()
    instance, claim_id, owner = _accepted_claim_world(world)
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    get_registry().create_governed_instance_with_id(instance.descriptor.instance_id)
    get_playbill_manager().register(instance.descriptor.instance_id, instance)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda: "owner")
    with TestClient(create_app()) as client:
        yield client, instance, claim_id, owner
    get_playbill_manager().clear()
    reset_registry()
    reset_permissions()


def test_http_append_uses_authenticated_actor_and_shared_wire(
    attestation_http,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    client, instance, claim_id, owner = attestation_http
    request = _request(instance, owner, claim_id, tmp_path)

    response = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/claim-attestations",
        json=request.model_dump(mode="json"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["tag"] == "playbill-claim-attestation-append-result-v1"
    assert len(instance.claim_attestation_evidence_store().events()) == 1


def test_http_actor_relay_and_malformed_request_are_typed(
    attestation_http,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance, claim_id, owner = attestation_http
    request = _request(instance, owner, claim_id, tmp_path)
    monkeypatch.setattr(playbill_api, "_actor_id", lambda: "reviewer")
    relay = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/claim-attestations",
        json=request.model_dump(mode="json"),
    )
    assert relay.status_code == 400
    assert relay.json()["error_code"] == "playbill.claim_attestation.actor_signer_mismatch"

    malformed = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/claim-attestations",
        json={"tag": "playbill-claim-attestation-append-request-v1"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["error_code"] == "playbill.claim_attestation.request_invalid"
    assert malformed.json()["errors"][0].startswith("body.attestation:")


def test_http_read_only_refuses_attestation_append(
    attestation_http,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance, claim_id, owner = attestation_http
    request = _request(instance, owner, claim_id, tmp_path)
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()

    response = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/claim-attestations",
        json=request.model_dump(mode="json"),
    )

    assert response.status_code == 403
    assert response.json()["context"]["required_mode"] == "GOVERNED_WRITE"
    assert instance.claim_attestation_evidence_store().events() == ()


def test_http_admin_recovery_rolls_forward_and_clears_poison(
    attestation_http,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    client, instance, claim_id, owner = attestation_http
    request = _request(instance, owner, claim_id, tmp_path)
    store = instance.claim_attestation_evidence_store()

    def crash(boundary: str) -> None:
        if boundary == "after_step2":
            raise RuntimeError(boundary)

    store.crash_hook = crash
    with pytest.raises(RuntimeError, match="after_step2"):
        service_append_claim_attestation(
            instance,
            request=request,
            actor_id="owner",
        )
    with pytest.raises(ClaimAttestationStoreError, match="claim-attestation recover"):
        store.head()
    store.crash_hook = None

    response = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/claim-attestations/recover"
    )

    assert response.status_code == 204, response.text
    assert len(store.events()) == 1


def test_http_recovery_is_admin_only(
    attestation_http,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance, _claim_id, _owner = attestation_http
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()

    response = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/claim-attestations/recover"
    )

    assert response.status_code == 403
    assert response.json()["context"]["required_mode"] == "ADMIN"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("attestation_head_unknown", 400),
        ("idempotency_payload_mismatch", 400),
        ("store_corrupt", 500),
        ("store_poisoned", 500),
        ("recovery_ambiguous", 500),
    ],
)
def test_claim_attestation_store_error_http_mapping_is_typed(
    code: str,
    status: int,
) -> None:
    actual, response = error_to_response(
        ClaimAttestationStoreError(
            f"playbill.claim_attestation.{code}",
            "mapped refusal",
        )
    )
    assert actual == status
    assert response.error_code == f"playbill.claim_attestation.{code}"


def test_http_next_maps_unknown_attestation_head_to_typed_400(
    attestation_http,  # type: ignore[no-untyped-def]
) -> None:
    client, instance, _claim_id, _owner = attestation_http
    response = client.post(
        f"/api/v1/{instance.descriptor.instance_id}/playbill/next",
        json={
            "tag": "playbill-next-request-v2",
            "evaluation_time": "2026-08-28T18:00:00Z",
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "attestation-store-error-test",
                "permitted_access_classes": ["instance", "public"],
                "disclose_restricted_existence": True,
            },
            "at_attestation_head_digest": "sha256:" + "9" * 64,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == ("playbill.claim_attestation.attestation_head_unknown")
