"""Client transport parity for attributed Claim retirement."""

from __future__ import annotations

import json

import httpx

from cruxible_client import CruxibleClient


def _coordinate() -> dict[str, str]:
    return {
        "tag": "playbill-accepted-coordinate-v1",
        "git_oid": "1" * 40,
        "semantic_root": "sha256:" + "2" * 64,
        "generation_root": "sha256:" + "3" * 64,
        "compiler_digest": "sha256:" + "4" * 64,
    }


def test_client_posts_the_typed_retirement_wire_and_parses_preflight() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-claim-retire-preflight-v1",
                "operation_digest": "sha256:" + "5" * 64,
                "coordinate": _coordinate(),
                "root_identity": {
                    "kind": "Claim",
                    "name": "CLM-0123456789abcdef0123456789abcdef",
                },
                "root_predecessor_digest": "sha256:" + "6" * 64,
                "reason": "was-wrong",
                "effective_until": None,
                "required_dependents": [],
                "diagnostics": [],
                "submit_ready": True,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible",
        transport=httpx.MockTransport(handler),
    )
    request = {
        "tag": "playbill-claim-retire-request-v1",
        "mode": "preflight",
        "claim_ref": "Claim:CLM-0123456789abcdef0123456789abcdef",
        "reason": "was-wrong",
        "effective_until": None,
        "expected_coordinate": _coordinate(),
        "dependents": [],
    }

    result = client.retire_playbill_claim(
        "inst",
        "CLM-0123456789abcdef0123456789abcdef",
        request=request,
    )

    assert result.submit_ready is True
    assert captured[0].url.path == (
        "/api/v1/inst/playbill/claims/CLM-0123456789abcdef0123456789abcdef/retire"
    )
    assert json.loads(captured[0].content) == request
