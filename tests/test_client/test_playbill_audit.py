"""Client wire parity for the G9 audit patrol."""

from __future__ import annotations

import json

import httpx

from cruxible_client import CruxibleClient, contracts

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
PROFILE = {
    "tag": "playbill-coverage-access-profile-v1",
    "profile_id": "client-audit",
    "permitted_access_classes": ["instance", "public"],
    "disclose_restricted_existence": True,
}


def test_client_sends_the_frozen_audit_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-audit-result-v1",
                "coordinate": COORDINATE.model_dump(mode="json"),
                "generation": 7,
                "evaluation_time": "2026-08-26T18:00:00+00:00",
                "operational_input_head_digest": "sha256:" + "5" * 64,
                "audited_through_generation": 7,
                "rows": [],
                "coverage": {
                    "tag": "playbill-audit-coverage-v1",
                    "access_permitted": True,
                    "declared_scope": {
                        "tag": "playbill-audit-scope-v1",
                        "claim_type_identities": ["ClaimType:status"],
                        "subject_kinds": ["work_item"],
                    },
                    "covered_claims": [],
                    "candidate_claim_count": 0,
                    "returned_claim_count": 0,
                    "omitted_claim_count": 0,
                    "omission_reasons": [],
                },
                "next_cursor": None,
                "result_digest": "sha256:" + "6" * 64,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.audit_playbill(
        "inst",
        evaluation_time="2026-08-26T18:00:00+00:00",
        access_profile=PROFILE,
        at=COORDINATE,
        claim_type_identities=("ClaimType:status",),
        subject_kinds=("work_item",),
        max_rows=9,
        max_bytes=4096,
    )

    assert result.audited_through_generation == 7
    assert captured[0].url.path == "/api/v1/inst/playbill/audit"
    payload = json.loads(captured[0].content)
    assert payload == {
        "tag": "playbill-audit-request-v1",
        "at": COORDINATE.model_dump(mode="json"),
        "evaluation_time": "2026-08-26T18:00:00+00:00",
        "access_profile": PROFILE,
        "scope": {
            "tag": "playbill-audit-scope-v1",
            "claim_type_identities": ["ClaimType:status"],
            "subject_kinds": ["work_item"],
        },
        "budget": {
            "tag": "playbill-audit-budget-v1",
            "max_rows": 9,
            "max_bytes": 4096,
        },
    }
