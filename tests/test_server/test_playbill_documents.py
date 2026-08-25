"""PB-E HTTP lifecycle, custody, coordinate, and private-input guardrails."""

from __future__ import annotations

import base64
from inspect import getsource
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.playbill.proposals import ProposalAdmissionRequest
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager


def test_http_document_lifecycle_and_explanation(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, private_key_path = playbill_http
    # Simulate a daemon-process reopen: no test-only in-memory registration may
    # be required to find or verify the pinned out-of-band trust root.
    get_playbill_manager().clear()
    body_bytes = b"# Public Playbill\n\nGoverned through HTTP.\n"
    stored = client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(body_bytes).decode("ascii")},
    )
    assert stored.status_code == 200, stored.text
    body_digest = stored.json()["digest"]
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Playbill design",
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(required_tier="graph_write", approval_roles=("owner",)),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/playbill/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "design"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]

    review = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/review",
        json={"include_body": True},
    )
    assert review.status_code == 200, review.text
    assert "Governed through HTTP" in review.json()["documents"][0]["readable_diff"]
    assert review.json()["attestation_coverage"]["coverage"] == "containing_change_set"

    challenge_response = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "operator", "include_body": True},
    )
    assert challenge_response.status_code == 200, challenge_response.text
    challenge = challenge_response.json()
    assert "private_key" not in challenge_response.text
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="operator",
        private_key_path=private_key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["submitted_by"] == "operator"
    activated = client.post(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
    coordinate = activated.json()["accepted_coordinate"]

    listed = client.get(f"/api/v1/{instance_id}/playbill/documents")
    assert listed.status_code == 200, listed.text
    assert listed.json()["coordinate"] == coordinate
    read = client.get(f"/api/v1/{instance_id}/playbill/documents/document:design/body")
    assert read.status_code == 200, read.text
    assert base64.b64decode(read.json()["content_base64"]) == body_bytes
    explained = client.post(
        f"/api/v1/{instance_id}/playbill/explain",
        json={
            "subject": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "documents/design.yaml",
                "selector": {
                    "scheme": "artifact-v1",
                    "value": "",
                },
            },
            "at": coordinate,
            "detail": "evidence",
            "include_body": True,
        },
    )
    assert explained.status_code == 200, explained.text
    assert explained.json()["coordinate"] == coordinate
    assert explained.json()["attestation_coverage"]["coverage_binding"]["coverage"] == (
        "containing_change_set"
    )


def test_http_models_refuse_private_key_and_local_path_inputs(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, private_key_path = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/sha256:{'1' * 64}/approvals",
        json={"private_key_path": str(private_key_path)},
    )
    assert response.status_code == 422
    assert "private_key_path" in response.text
    source = client.post(
        f"/api/v1/{instance_id}/playbill/sources/check",
        json={"local_path": "/tmp/secret.md"},
    )
    assert source.status_code == 422
    assert "local_path" in source.text


def test_principal_display_name_is_sanitized_and_invalid_ref_is_a_typed_400(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key_path = playbill_http
    principal = PrincipalRecord(
        principal_id="reviewer",
        public_key="1" * 64,
        authority_roles=("reviewer",),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/playbill/principals/proposals",
        json={
            "principal": principal.model_dump(mode="json"),
            "proposal_name": "Add Reviewer",
        },
    )

    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["proposal"]["admission"]["target_ref"] == (
        "refs/proposals/operator/add-reviewer"
    )

    refused = client.post(
        f"/api/v1/{instance_id}/playbill/principals/proposals",
        json={
            "principal": principal.model_dump(mode="json"),
            "proposal_name": " !!! ",
        },
    )
    assert refused.status_code == 400
    assert refused.json()["error_type"] == "DataValidationError"
    assert "canonical ref characters" in refused.text


@pytest.mark.parametrize(
    "entrypoint",
    (
        playbill_api.playbill_propose_document,
        playbill_api.playbill_propose_subject,
        playbill_api.playbill_propose_claim_type,
        playbill_api.playbill_propose_claim_type_input,
        playbill_api.playbill_propose_claim,
        playbill_api.playbill_propose_claims,
        playbill_api.playbill_propose_query_definition,
    ),
)
def test_every_proposal_route_keeps_the_typed_validation_boundary(
    entrypoint: object,
) -> None:
    assert "_proposal_validation_boundary(" in getsource(entrypoint)


def test_residual_proposal_ref_validation_is_a_typed_http_400(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, _private_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    body = instance.store_document_body(b"body")
    shell = DocumentShell(
        identity="document:residual-validation",
        document_kind="design",
        title="Residual validation",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="graph_write", approval_roles=("owner",)),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )

    def residual_failure(*_args: object, **_kwargs: object) -> object:
        return ProposalAdmissionRequest(
            target_ref="not-a-ref",
            proposed_base_oid="0" * 40,
        )

    monkeypatch.setattr(playbill_api, "service_propose_playbill_document", residual_failure)

    refused = client.post(
        f"/api/v1/{instance_id}/playbill/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "Any name"},
    )

    assert refused.status_code == 400
    assert refused.json()["error_type"] == "DataValidationError"
    assert "document proposal reference is invalid" in refused.text


def test_http_permission_modes_separate_read_store_propose_approval_and_activation(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, private_key_path = playbill_http

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    assert client.get(f"/api/v1/{instance_id}/playbill/documents").status_code == 200
    denied_store = client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(b"# Tiered\n").decode("ascii")},
    )
    assert denied_store.status_code == 403

    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    stored = client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(b"# Tiered\n").decode("ascii")},
    )
    assert stored.status_code == 200, stored.text
    shell = DocumentShell(
        identity="document:tiered",
        document_kind="design",
        title="Tiered",
        media_type="text/markdown",
        body_digest=stored.json()["digest"],
        authority=DocumentAuthority(
            required_tier="graph_write",
            approval_roles=("owner",),
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/playbill/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "tiered"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]
    challenge = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "operator"},
    ).json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="operator",
        private_key_path=private_key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    denied_approval = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert denied_approval.status_code == 403

    monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
    reset_permissions()
    approved = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
