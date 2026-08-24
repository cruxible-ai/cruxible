"""Client request parity for the deterministic Playbill next queue."""

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


def test_client_sends_explicit_time_access_and_workspace_observation() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-next-result-v1",
                "coordinate": COORDINATE,
                "evaluation_time": "2026-08-24T18:00:00.000000Z",
                "observed_domains": ["accepted_state", "workspace_floor"],
                "unobserved_domains": ["workspace_sources"],
                "items": [],
                "result_digest": "sha256:" + "5" * 64,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.next_playbill(
        "inst",
        evaluation_time="2026-08-24T18:00:00Z",
        access_profile={
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "client-next",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": True,
        },
        workspace_observation={
            "tag": "playbill-next-workspace-observation-v1",
            "floor_status": "missing",
            "installed_coordinate": None,
            "drift_observations": None,
        },
    )

    assert result.observed_domains == ["accepted_state", "workspace_floor"]
    assert captured[0].url.path == "/api/v1/inst/playbill/next"
    payload: dict[str, Any] = json.loads(captured[0].content)
    assert payload["tag"] == "playbill-next-request-v1"
    assert payload["evaluation_time"] == "2026-08-24T18:00:00Z"
    assert payload["workspace_observation"]["floor_status"] == "missing"
    assert "result_digest" not in payload
