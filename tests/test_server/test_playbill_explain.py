"""PB-E HTTP explanation detail and coordinate refusal contracts."""

from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner


def _accept_document(client: TestClient, instance_id: str, key_path: Path) -> dict[str, str]:
    stored = client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(b"# Explain me\n").decode("ascii")},
    )
    shell = DocumentShell(
        identity="document:explain",
        document_kind="design",
        title="Explain",
        media_type="text/markdown",
        body_digest=stored.json()["digest"],
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposal = client.post(
        f"/api/v1/{instance_id}/playbill/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "explain"},
    ).json()
    proposal_id = proposal["proposal"]["admission"]["proposal_id"]
    challenge = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "reviewer"},
    ).json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=key_path,
        expected_public_key=challenge["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
    return activated.json()["accepted_coordinate"]


def test_http_explain_returns_typed_proof_deferral_and_refuses_mixed_coordinate(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, key_path = playbill_http
    coordinate = _accept_document(client, instance_id, key_path)
    request = {
        "subject": {
            "tag": "playbill-semantic-address-v1",
            "artifact_path": "documents/explain.json",
            "selector": {"scheme": "artifact-v1", "value": ""},
        },
        "at": coordinate,
        "detail": "proof",
        "include_body": False,
    }
    proof = client.post(f"/api/v1/{instance_id}/playbill/explain", json=request)
    assert proof.status_code == 200, proof.text
    assert proof.json()["tag"] == "playbill-explain-unsupported-detail-v1"
    assert proof.json()["supported_details"] == ["summary", "evidence"]

    request["detail"] = "summary"
    request["at"] = {**coordinate, "semantic_root": "sha256:" + "9" * 64}
    mixed = client.post(f"/api/v1/{instance_id}/playbill/explain", json=request)
    assert mixed.status_code == 400, mixed.text
    assert "mixed" in mixed.text
