"""Client and HTTP request parity for composed ClaimType migration."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cruxible_client import CruxibleClient


def _result() -> dict[str, Any]:
    return {
        "tag": "playbill-claim-type-migration-result-v1",
        "operation_digest": "sha256:" + "1" * 64,
        "dependents": [{"claim_id": "CLM-" + "2" * 32, "disposition": "retire"}],
        "proposal": {
            "tag": "playbill-proposal-inspection-v1",
            "proposal": {},
            "accepted_coordinate": {
                "tag": "playbill-accepted-coordinate-v1",
                "git_oid": "3" * 64,
                "semantic_root": "sha256:" + "4" * 64,
                "generation_root": "sha256:" + "5" * 64,
                "compiler_digest": "sha256:" + "6" * 64,
            },
        },
    }


def test_client_posts_the_frozen_migration_request_unchanged() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_result())

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    request = {
        "tag": "playbill-claim-type-migration-request-v1",
        "successor": {"predicate": "project.work_item.status"},
        "dependents": [],
    }

    result = client.migrate_playbill_claim_type("inst", request=request)

    assert result.operation_digest == "sha256:" + "1" * 64
    assert captured[0].url.path == "/api/v1/inst/playbill/claim-types/migrations"
    assert json.loads(captured[0].content) == request
