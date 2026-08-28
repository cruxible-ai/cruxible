"""PB-E client transport and signer-callback parity tests."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from cruxible_client import CruxibleClient
from cruxible_client.contracts.errors import ProposalActivationRequestInvalid

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}


def _client(handler: Any) -> CruxibleClient:
    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    return client


def _review() -> dict[str, Any]:
    candidate = {
        "tag": "playbill-validated-candidate-v1",
        "candidate": {
            "tag": "playbill-candidate-v1",
            "parent_semantic_root": COORDINATE["semantic_root"],
            "candidate_manifest_root": "sha256:" + "5" * 64,
            "semantic_diff_digest": "sha256:" + "6" * 64,
            "scope": ["documents/design.yaml"],
            "timestamp": "2026-08-13T12:00:00.000000Z",
        },
        "candidate_digest": "sha256:" + "7" * 64,
        "required_tier": "graph_write",
        "approval_requirements": [],
        "activation_policy": "snapshot",
        "closure_paths": ["documents/design.yaml"],
        "members": [],
        "law_digests": {"document-v1": "sha256:" + "8" * 64},
        "compiler_digest": COORDINATE["compiler_digest"],
    }
    return {
        "tag": "playbill-proposal-review-v1",
        "coordinate_kind": "provisional",
        "proposal_id": "sha256:" + "9" * 64,
        "candidate": candidate,
        "candidate_digest": candidate["candidate_digest"],
        "parent_semantic_root": COORDINATE["semantic_root"],
        "settlement_base": COORDINATE,
        "base_oid": COORDINATE["git_oid"],
        "complete_members": [],
        "members": [],
        "governance": {},
        "provenance": {},
        "attestation_coverage": {"coverage": "containing_change_set"},
        "documents": [],
        "redactions": [],
    }


def test_client_preserves_typed_malformed_activation_refusal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_type": "ProposalActivationRequestInvalid",
                "message": (
                    "playbill.proposal.activation_request_invalid: proposal_id must be "
                    "a canonical sha256 digest"
                ),
                "error_code": "playbill.proposal.activation_request_invalid",
                "errors": [],
                "context": {},
            },
        )

    client = _client(handler)

    with pytest.raises(ProposalActivationRequestInvalid, match="canonical sha256"):
        client.activate_playbill_proposal("inst_test", "bogus-no-prefix")


def test_client_signer_callback_submits_only_public_attestation() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        requests.append(payload)
        if request.url.path.endswith("/approval-challenge"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-approval-challenge-v1",
                    "proposal_id": "sha256:" + "9" * 64,
                    "signer_principal": {
                        "principal_id": "owner",
                        "public_key": "a" * 64,
                        "kind": "ordinary",
                        "status": "active",
                    },
                    "signer_key_history_ref": "principals/owner.yaml@"
                    + COORDINATE["semantic_root"],
                    "statement": {
                        "tag": "playbill-attest-v1",
                        "signer_id": "owner",
                        "signing_semantic_root": COORDINATE["semantic_root"],
                        "payload_digest": "sha256:" + "7" * 64,
                    },
                    "review": _review(),
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-approval-receipt-v1",
                "proposal_id": "sha256:" + "9" * 64,
                "candidate_digest": "sha256:" + "7" * 64,
                "signer_id": "owner",
                "submitted_by": "relay",
                "signing_semantic_root": COORDINATE["semantic_root"],
                "attestation_digest": "sha256:" + "b" * 64,
                "key_history_ref": "principals/owner.yaml@" + COORDINATE["semantic_root"],
            },
        )

    client = _client(handler)
    seen_statement: dict[str, Any] = {}

    def signer(statement: dict[str, Any]) -> dict[str, Any]:
        seen_statement.update(statement)
        return {**statement, "sig": "c" * 128}

    receipt = client.approve_playbill_proposal(
        "inst_test",
        "sha256:" + "9" * 64,
        signer_id="owner",
        signer=signer,
        include_body=True,
    )
    assert seen_statement["payload_digest"] == "sha256:" + "7" * 64
    assert receipt.signer_id == "owner"
    assert requests[0] == {"signer_id": "owner", "include_body": True}
    assert requests[1] == {"attestation": {**seen_statement, "sig": "c" * 128}}
    assert "private" not in json.dumps(requests)


def test_client_encodes_body_bytes_and_exact_coordinate_params() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "digest": "sha256:" + "d" * 64,
                    "present": True,
                    "byte_length": 6,
                    "redacted": False,
                },
            )
        return httpx.Response(
            200,
            json={"tag": "playbill-document-list-v1", "coordinate": COORDINATE, "documents": []},
        )

    client = _client(handler)
    client.store_playbill_body("inst_test", b"bytes\n")
    listed = client.list_playbill_documents("inst_test", at=COORDINATE)
    assert (
        json.loads(captured[0].content)["content_base64"] == base64.b64encode(b"bytes\n").decode()
    )
    assert dict(captured[1].url.params) == {
        "git_oid": COORDINATE["git_oid"],
        "semantic_root": COORDINATE["semantic_root"],
        "generation_root": COORDINATE["generation_root"],
        "compiler_digest": COORDINATE["compiler_digest"],
    }
    assert listed.coordinate.git_oid == COORDINATE["git_oid"]
