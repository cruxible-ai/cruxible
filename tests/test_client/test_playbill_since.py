"""Client request parity for accepted ChangeSet history."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cruxible_client import CruxibleClient, contracts
from cruxible_client.contracts.errors import PlaybillSinceRequestInvalid

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


def test_client_refuses_an_invalid_request_before_transport() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(PlaybillSinceRequestInvalid) as raised:
        client.since_playbill(
            "inst",
            generation=3,
            access_profile=PROFILE,
            max_rows=1001,
        )

    assert raised.value.error_code == "playbill.since.request_invalid"
    assert raised.value.field_path == "$.max_rows"
    assert calls == []


def test_client_reconstructs_the_typed_http_refusal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_type": "PlaybillSinceRequestInvalid",
                "message": (
                    "playbill.since.request_invalid: request field $.cursor is invalid: "
                    "Input should be a valid dictionary or instance of PlaybillSinceCursor"
                ),
                "error_code": "playbill.since.request_invalid",
                "errors": [],
                "context": {"field_path": "$.cursor"},
                "mutation_receipt_id": None,
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(PlaybillSinceRequestInvalid) as raised:
        client.since_playbill(
            "inst",
            generation=3,
            access_profile=PROFILE,
        )

    assert raised.value.error_code == "playbill.since.request_invalid"
    assert raised.value.field_path == "$.cursor"


def test_client_since_rejects_non_mapping_access_profile_with_typed_refusal() -> None:
    client = CruxibleClient(base_url="https://since.invalid", token="crt_x")
    with pytest.raises(PlaybillSinceRequestInvalid) as excinfo:
        client.since_playbill("inst_x", generation=0, access_profile=["not", "a", "mapping"])  # type: ignore[arg-type]
    assert excinfo.value.field_path.startswith("$.access_profile")
