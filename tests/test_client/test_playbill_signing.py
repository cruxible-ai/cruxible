"""PB-E client signer failures cannot fall back to server-side signing."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cruxible_client.contracts.errors import PlaybillFormatError
from tests.test_client.test_playbill_documents import COORDINATE, _client, _review


def test_client_signer_failure_stops_before_attestation_submission() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-approval-challenge-v1",
                "proposal_id": "sha256:" + "9" * 64,
                "signer_principal": {
                    "principal_id": "owner",
                    "public_key": "a" * 64,
                    "authority_roles": ["owner"],
                    "status": "active",
                },
                "signer_key_history_ref": "principals/owner.yaml@" + COORDINATE["semantic_root"],
                "statement": {
                    "tag": "playbill-attest-v1",
                    "signer_id": "owner",
                    "signing_semantic_root": COORDINATE["semantic_root"],
                    "payload_digest": "sha256:" + "7" * 64,
                },
                "review": _review(),
            },
        )

    client = _client(handler)

    def missing_signer(_statement: dict[str, Any]) -> dict[str, Any]:
        raise PlaybillFormatError("client-held signer is unavailable")

    with pytest.raises(PlaybillFormatError, match="unavailable"):
        client.approve_playbill_proposal(
            "inst_test",
            "sha256:" + "9" * 64,
            signer_id="owner",
            signer=missing_signer,
        )

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/approval-challenge")
    assert "private" not in json.dumps(json.loads(requests[0].content))
