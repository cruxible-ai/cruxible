"""Client wire parity for G9 curation listing."""

from __future__ import annotations

import json

import httpx
import pytest

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
                "evaluation_time": "2026-08-26T16:00:00+00:00",
                "operational_head_digest": "sha256:" + "5" * 64,
                "items": [],
                "detector_coverage": [],
                "observation_coverage": {
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 0,
                    "observed_block_count": 0,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
                "result_digest": "sha256:" + "6" * 64,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[assignment]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.list_playbill_curation(
        "inst",
        evaluation_time="2026-08-26T16:00:00+00:00",
        workspace_observation={
            "tag": "playbill-next-workspace-observation-v1",
            "source_observations": [],
        },
    )

    assert result.generation == 7
    assert captured[0].url.path == "/api/v1/inst/playbill/curation/list"
    payload = json.loads(captured[0].content)
    assert payload["tag"] == "playbill-curation-list-request-v1"
    assert payload["evaluation_time"] == "2026-08-26T16:00:00+00:00"
    assert payload["workspace_observation"]["source_observations"] == []


@pytest.mark.parametrize(
    ("method", "path", "extra", "expected_tag"),
    (
        (
            "overrule_playbill_curation",
            "/api/v1/inst/playbill/curation/overrule",
            {},
            "playbill-curation-overrule-request-v1",
        ),
        (
            "accept_fixed_playbill_curation",
            "/api/v1/inst/playbill/curation/accept-fixed",
            {
                "accepted_proposal_id": "sha256:" + "3" * 64,
                "accepted_changeset_digest": "sha256:" + "4" * 64,
            },
            "playbill-curation-accept-fixed-request-v1",
        ),
        (
            "suppress_playbill_curation",
            "/api/v1/inst/playbill/curation/suppress",
            {"scope": "pattern", "until_generation": 12},
            "playbill-curation-suppress-request-v1",
        ),
    ),
)
def test_client_curation_lifecycle_routes_are_typed(
    method: str,
    path: str,
    extra: dict[str, object],
    expected_tag: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-curation-action-result-v1",
                "coordinate": {
                    "tag": "playbill-accepted-coordinate-v1",
                    "git_oid": "1" * 64,
                    "semantic_root": "sha256:" + "2" * 64,
                    "generation_root": "sha256:" + "3" * 64,
                    "compiler_digest": "sha256:" + "4" * 64,
                },
                "generation": 7,
                "operational_head_digest": "sha256:" + "5" * 64,
                "item": {"item_id": "sha256:" + "1" * 64, "status": "resolved"},
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[assignment]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    kwargs = {
        "item_id": "sha256:" + "1" * 64,
        "expected_latest_event_digest": "sha256:" + "2" * 64,
        "reason": "operator-reviewed mechanical facts",
        **extra,
    }
    result = getattr(client, method)("inst", **kwargs)

    assert result.generation == 7
    assert captured[0].url.path == path
    payload = json.loads(captured[0].content)
    assert payload["tag"] == expected_tag
    assert payload["item_id"] == "sha256:" + "1" * 64
