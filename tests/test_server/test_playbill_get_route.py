"""POST /playbill/get serves the get read and its coded refusals."""

from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.documents import DocumentAuthority, DocumentLifecycle, DocumentShell
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner

_BODY = b"# Design\n\nServed through get.\n"


def _accept_document(client: TestClient, instance_id: str, key: Path) -> None:
    stored = client.post(
        f"/api/v1/{instance_id}/playbill/bodies",
        json={"content_base64": base64.b64encode(_BODY).decode("ascii")},
    )
    assert stored.status_code == 200, stored.text
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Cruxible design",
        media_type="text/markdown",
        body_digest=stored.json()["digest"],
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = client.post(
        f"/api/v1/{instance_id}/playbill/documents/proposals",
        json={"shell": shell.model_dump(mode="json"), "proposal_name": "design"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposal"]["admission"]["proposal_id"]
    challenge = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "reviewer", "include_body": True},
    ).json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=key,
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


def test_get_serves_cards_details_and_coded_refusals(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, key = playbill_http
    _accept_document(client, instance_id, key)
    url = f"/api/v1/{instance_id}/playbill/get"

    card = client.post(url, json={"ref": "Document:design"})
    body = client.post(
        url, json={"ref": "document:design", "detail": "body", "range": {"start": 0, "end": 8}}
    )
    missing = client.post(url, json={"ref": "Document:desig"})
    unsupported = client.post(url, json={"ref": "Document:design", "detail": "evidence"})
    why = client.post(url, json={"ref": "Document:design", "detail": "why"})
    malformed = client.post(url, json={"ref": "Document:design", "range": {"start": 0, "end": 8}})

    assert card.status_code == 200, card.text
    payload = card.json()
    assert payload["kind"] == "document" and payload["ref"] == "Document:design"
    assert payload["card"]["title"] == "Cruxible design"
    assert payload["card"]["size"] == len(_BODY)
    # Absent sections are omitted, not null.
    assert "evidence" not in payload and "proof" not in payload
    # A summary names its coordinate compactly; the full one only when asked.
    assert set(payload["coordinate"]) == {"git_oid", "generation"}
    assert len(payload["coordinate"]["git_oid"]) == 12
    assert "accepted_coordinate" not in payload and "truncated" not in payload
    pinned = client.post(url, json={"ref": "Document:design", "full_coordinate": True}).json()
    proof = client.post(url, json={"ref": "Document:design", "detail": "proof"}).json()
    assert pinned["accepted_coordinate"] == proof["accepted_coordinate"]
    assert pinned["accepted_coordinate"]["git_oid"].startswith(payload["coordinate"]["git_oid"])
    assert body.status_code == 200, body.text
    assert body.json()["body"]["text"] == _BODY[:8].decode()

    assert missing.status_code == 404, missing.text
    refusal = missing.json()
    assert refusal["error_code"] == "cruxible.get.ref_not_found"
    assert refusal["context"]["candidates"] == ["Document:design"]
    assert refusal["repair"] == {
        "operation": "cruxible.get",
        "arguments": {"ref": "Document:design"},
    }
    assert "nearest: Document:design" in refusal["message"]

    # A Document explains why it is accepted; with body permission (the
    # default admin tier here) its source maps onto its body.
    assert why.status_code == 200, why.text
    assert why.json()["why"]["source_mapping"] is not None
    assert unsupported.status_code == 400, unsupported.text
    assert unsupported.json()["error_code"] == "cruxible.get.detail_unsupported"
    assert unsupported.json()["context"]["allowed"] == [
        "summary",
        "why",
        "history",
        "proof",
        "body",
    ]
    assert malformed.status_code == 422, malformed.text
    assert any("range applies only to detail" in item for item in malformed.json()["errors"])
