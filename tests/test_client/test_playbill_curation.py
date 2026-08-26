"""Client wire parity for G9 curation listing."""

from __future__ import annotations

import json

import httpx

from cruxible_client import CruxibleClient


def test_client_sends_explicit_workspace_observation_to_curation_list() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-curation-list-result-v1",
                "coordinate": {
                    "tag": "playbill-accepted-coordinate-v1",
                    "git_oid": "1" * 64,
                    "semantic_root": "sha256:" + "2" * 64,
                    "generation_root": "sha256:" + "3" * 64,
                    "compiler_digest": "sha256:" + "4" * 64,
                },
                "generation": 7,
                "operational_head_digest": "sha256:" + "5" * 64,
                "items": [],
                "observation_coverage": {
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 0,
                    "observed_block_count": 0,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.list_playbill_curation(
        "inst",
        workspace_observation={
            "tag": "playbill-next-workspace-observation-v1",
            "source_observations": [],
        },
    )

    assert result.generation == 7
    assert captured[0].url.path == "/api/v1/inst/playbill/curation/list"
    payload = json.loads(captured[0].content)
    assert payload["tag"] == "playbill-curation-list-request-v1"
    assert payload["workspace_observation"]["source_observations"] == []
