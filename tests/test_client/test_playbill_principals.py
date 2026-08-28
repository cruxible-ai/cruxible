"""Principal onboarding reuses the frozen public-record proposal route."""

from __future__ import annotations

import json

import httpx

from cruxible_client import CruxibleClient


def test_client_proposes_principal_public_record_on_existing_route() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-proposal-inspection-v1",
                "proposal": {"proposal_id": "sha256:" + "5" * 64},
                "accepted_coordinate": {
                    "tag": "playbill-accepted-coordinate-v1",
                    "git_oid": "1" * 64,
                    "semantic_root": "sha256:" + "2" * 64,
                    "generation_root": "sha256:" + "3" * 64,
                    "compiler_digest": "sha256:" + "4" * 64,
                },
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    principal = {
        "tag": "playbill-principal-v1",
        "principal_id": "reviewer",
        "algorithm": "ed25519-v1",
        "public_key": "a" * 64,
        "kind": "ordinary",
        "status": "active",
    }

    result = client.propose_playbill_principal_change(
        "inst_principals",
        principal=principal,
        proposal_name="register-reviewer",
    )

    assert result.proposal["proposal_id"] == "sha256:" + "5" * 64
    assert captured[0].url.path == "/api/v1/inst_principals/playbill/principals/proposals"
    assert json.loads(captured[0].content) == {
        "principal": principal,
        "proposal_name": "register-reviewer",
    }
