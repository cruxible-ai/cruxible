"""Client request parity for accepted ChangeSet history."""

from __future__ import annotations

import json
from typing import Any

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
    "profile_id": "client-since",
    "permitted_access_classes": ["instance", "public"],
    "disclose_restricted_existence": True,
}


def test_client_sends_the_frozen_since_request() -> None:
    captured: list[httpx.Request] = []
    values: dict[str, Any] = {
        "coordinate": COORDINATE.model_dump(mode="json"),
        "generation": 7,
        "rows": [],
        "next_cursor": None,
        "truncated": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-since-result-v1",
                **values,
                "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                    "playbill-since-result-v1", values
                ),
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.since_playbill(
        "inst",
        generation=3,
        at=COORDINATE,
        access_profile=PROFILE,
        max_rows=9,
        max_bytes=12_345,
    )

    assert result.generation == 7
    assert captured[0].url.path == "/api/v1/inst/playbill/since"
    payload = json.loads(captured[0].content)
    assert payload == {
        "tag": "playbill-since-request-v1",
        "generation": 3,
        "at": COORDINATE.model_dump(mode="json"),
        "access_profile": PROFILE,
        "max_rows": 9,
        "max_bytes": 12_345,
        "cursor": None,
    }
