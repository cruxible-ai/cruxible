"""PB-E HTTP lifecycle, custody, coordinate, and private-input guardrails."""

from __future__ import annotations

import base64
from inspect import getsource
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import (
    query_claims_by_type_example,
)
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
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
    body_bytes = b"# Public Cruxible\n\nGoverned through HTTP.\n"
    stored = client.post(
        f"/api/v1/{instance_id}/bodies",
        json={"content_base64": base64.b64encode(body_bytes).decode("ascii")},
    )
    assert stored.status_code == 200, stored.text
    body_digest = stored.json()["digest"]
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Cruxible design",
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "design"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]

    review = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/review",
        json={"include_body": True},
    )
    assert review.status_code == 200, review.text
    assert "Governed through HTTP" in review.json()["documents"][0]["readable_diff"]
    assert review.json()["attestation_coverage"]["coverage"] == "containing_change_set"

    challenge_response = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "reviewer", "include_body": True},
    )
    assert challenge_response.status_code == 200, challenge_response.text
    challenge = challenge_response.json()
    assert "private_key" not in challenge_response.text
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=private_key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["submitted_by"] == "operator"
    activated = client.post(f"/api/v1/{instance_id}/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["activated_by"] == "operator"
    coordinate = activated.json()["accepted_coordinate"]

    listed = client.get(f"/api/v1/{instance_id}/orient", params={"section": "documents"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["coordinate"] == coordinate
    assert [row["name"] for row in listed.json()["documents"]] == ["design"]
    read = client.post(
        f"/api/v1/{instance_id}/get", json={"ref": "Document:design", "detail": "body"}
    )
    assert read.status_code == 200, read.text
    assert read.json()["body"]["text"] == body_bytes.decode()
    assert read.json()["body"]["body_digest"] == body_digest
    why = client.post(
        f"/api/v1/{instance_id}/get",
        json={"ref": "Document:design", "detail": "why", "at": coordinate["git_oid"]},
    )
    assert why.status_code == 200, why.text
    explained = why.json()["why"]
    assert explained["coordinate"] == coordinate
    assert explained["attestation_coverage"]["coverage_binding"]["coverage"] == (
        "containing_change_set"
    )


def test_http_activation_refuses_a_malformed_proposal_id_as_typed_400(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _ = playbill_http

    response = client.post(f"/api/v1/{instance_id}/proposals/bogus-no-prefix/activate")

    assert response.status_code == 400
    assert response.json()["error_type"] == "ProposalActivationRequestInvalid"
    assert response.json()["error_code"] == ("cruxible.proposal.activation_request_invalid")


def test_http_models_refuse_private_key_and_local_path_inputs(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, private_key_path = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/proposals/sha256:{'1' * 64}/approvals",
        json={"private_key_path": str(private_key_path)},
    )
    assert response.status_code == 422
    assert "private_key_path" in response.text
    source = client.post(
        f"/api/v1/{instance_id}/sources/check",
        json={"local_path": "/" + "tmp/secret.md"},
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
        kind="ordinary",
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/principals/proposals",
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
        f"/api/v1/{instance_id}/principals/proposals",
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
        playbill_api.playbill_propose_claim_type,
        playbill_api.playbill_propose_claim_type_input,
    ),
)
def test_every_proposal_route_keeps_the_typed_validation_boundary(
    entrypoint: object,
) -> None:
    assert "_proposal_validation_boundary(" in getsource(entrypoint)


def test_policy_read_is_a_real_http_behavior(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key_path = playbill_http

    policies = client.get(f"/api/v1/{instance_id}/orient", params={"section": "policies"})
    assert policies.status_code == 200, policies.text
    rows = policies.json()["policies"]
    assert [item["policy_kind"] for item in rows] == [
        "approval_policy",
        "procedure_runtime_policy",
        "trigger_schedule",
        "trigger_schedule",
        "trigger_schedule",
    ]
    assert {item["declaring_artifact_identity"]: item["policy"] for item in rows[2:]} == {
        "Trigger:evidence-sweep": {"kind": "cadence", "interval_seconds": 86400},
        "Trigger:floor-refresh": {"kind": "generation_accepted"},
        "Trigger:prediction-anchor-retry": {"kind": "cadence", "interval_seconds": 3600},
    }
    for item in rows[2:]:
        assert item["placement"] == "embedded"
        assert item["declaring_artifact_kind"] == "Trigger"
        assert item["field_path"] == "/schedule"


def test_friendly_change_set_duplicate_is_a_typed_http_400(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key_path = playbill_http
    query = query_claims_by_type_example().model_dump(mode="json")

    response = client.post(
        f"/api/v1/{instance_id}/authoring/compile",
        json={
            "tag": "playbill-authoring-input-compile-request-v1",
            "input": {"kind": "change_set", "members": [query, query]},
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "AuthoringInputError"
    assert response.json()["error_code"] == ("cruxible.authoring.change_set_duplicate_identity")


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
        authority=DocumentAuthority(required_tier="graph_write"),
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
        f"/api/v1/{instance_id}/documents/proposals",
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
    assert (
        client.get(f"/api/v1/{instance_id}/orient", params={"section": "documents"}).status_code
        == 200
    )
    denied_store = client.post(
        f"/api/v1/{instance_id}/bodies",
        json={"content_base64": base64.b64encode(b"# Tiered\n").decode("ascii")},
    )
    assert denied_store.status_code == 403

    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    reset_permissions()
    stored = client.post(
        f"/api/v1/{instance_id}/bodies",
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
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "tiered"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]
    challenge = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "reviewer"},
    ).json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=private_key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    denied_approval = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert denied_approval.status_code == 403

    monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
    reset_permissions()
    approved = client.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text


@pytest.mark.parametrize(
    "selector",
    (
        "sha256:121448",  # a prefix shorter than the resolvable minimum
        "121448",  # a bare hex prefix without the digest tag
        "bogus-no-prefix",
        "sha256:" + "0" * 64,  # well formed, but no such proposal
    ),
)
def test_http_review_refuses_an_unknown_proposal_id_as_typed_404(
    playbill_http: tuple[TestClient, str, Path],
    selector: str,
) -> None:
    client, instance_id, _ = playbill_http

    reviewed = client.post(
        f"/api/v1/{instance_id}/proposals/{selector}/review",
        json={"include_body": False},
    )

    for response in (reviewed,):
        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error_code"] == "cruxible.proposal_not_found"
        assert body["context"]["selector"] == selector
        assert body["repair"]["operation"] == "cruxible.proposal.list"


def test_sdk_get_reads_one_proposal_by_id_without_paging_the_list(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    from cruxible_client import Cruxible, CruxibleClient

    client, instance_id, _ = playbill_http
    stored = client.post(
        f"/api/v1/{instance_id}/bodies",
        json={"content_base64": base64.b64encode(b"# Status\n").decode("ascii")},
    )
    shell = DocumentShell(
        identity="document:status",
        document_kind="design",
        title="Status probe",
        media_type="text/markdown",
        body_digest=stored.json()["digest"],
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "status"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]

    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = client  # type: ignore[assignment]

    def no_listing(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("get paged the proposal list")

    transport.list_proposals = no_listing  # type: ignore[method-assign]
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=tmp_path)

    card = pb.get(proposal_id).value

    assert card.proposal == proposal_id  # type: ignore[union-attr]
    assert card.status == "open"  # type: ignore[union-attr]
    assert card.verdict == "candidate"  # type: ignore[union-attr]

    # The by-ID status route is cut: get is the one proposal read.
    gone = client.get(f"/api/v1/{instance_id}/proposals/{proposal_id}/status")
    assert gone.status_code in {404, 405}, gone.text
