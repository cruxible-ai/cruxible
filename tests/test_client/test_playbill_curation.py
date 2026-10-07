"""Client wire parity for G9 curation listing."""

from __future__ import annotations

import json

import httpx
import pytest

from cruxible_client import CruxibleClient


def test_client_curation_list_sends_no_clock_and_no_scan() -> None:
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
                "detection": {
                    "state": "behind",
                    "trigger": "live",
                    "detected_through_generation": 6,
                    "detected_at": "2026-08-26T16:00:00Z",
                },
                "detector_coverage": [],
                "inactive_detectors": [
                    {
                        "pattern_kind": "playbill.curation.dead_vocabulary.v1",
                        "reason": "consumption_receipts_off",
                    }
                ],
                "observation_coverage": None,
                "result_digest": "sha256:" + "6" * 64,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[assignment]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    result = client.list_curation(
        "inst",
        access_profile={
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "test-curation",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": True,
        },
    )

    assert result.generation == 7
    assert result.detection.state == "behind"
    assert result.inactive_detectors[0].reason == "consumption_receipts_off"
    assert captured[0].url.path == "/api/v1/inst/curation/list"
    payload = json.loads(captured[0].content)
    assert payload == {
        "tag": "playbill-curation-list-request-v1",
        "access_profile": payload["access_profile"],
    }
    assert payload["access_profile"]["profile_id"] == "test-curation"


@pytest.mark.parametrize(
    ("method", "path", "extra", "expected_tag"),
    (
        (
            "overrule_curation",
            "/api/v1/inst/curation/overrule",
            {},
            "playbill-curation-overrule-request-v1",
        ),
        (
            "accept_fixed_curation",
            "/api/v1/inst/curation/accept-fixed",
            {"accepted_generation": 4},
            "playbill-curation-accept-fixed-request-v1",
        ),
        (
            "suppress_curation",
            "/api/v1/inst/curation/suppress",
            {"scope": "lineage", "until_generation": 12},
            "playbill-curation-suppress-request-v1",
        ),
        (
            "unsuppress_curation",
            "/api/v1/inst/curation/unsuppress",
            {},
            "playbill-curation-unsuppress-request-v1",
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
