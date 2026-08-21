"""Client request and response parity for headless Playbill search."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cruxible_client import CruxibleClient

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}


def test_client_sends_only_search_inputs_and_parses_the_frozen_result() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-search-result-v1",
                "mode": "search",
                "coordinate": COORDINATE,
                "evaluation_time": "2026-08-21T14:00:00.000000Z",
                "rows": [],
                "orientation": None,
                "selection_basis_digest": "sha256:" + "5" * 64,
                "next_cursor": None,
                "truncated": False,
                "result_digest": "sha256:" + "6" * 64,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.search_playbill(
        "inst",
        mode="search",
        query="release",
        kinds=("procedure", "brief", "brief"),
        evaluation_time="2026-08-21T14:00:00Z",
    )

    assert result.mode == "search"
    assert captured[0].url.path == "/api/v1/inst/playbill/search"
    payload: dict[str, Any] = json.loads(captured[0].content)
    assert payload["query"] == "release"
    assert payload["kinds"] == ["brief", "procedure"]
    assert "access_profile" not in payload
    assert "selection_basis_digest" not in payload
