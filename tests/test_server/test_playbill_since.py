"""HTTP parity for the frozen Playbill since request."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import contracts


def test_http_since_delegates_the_exact_request(
    playbill_http: tuple[TestClient, str, Path], monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: dict[str, object] = {}
    coordinate = contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    values = {
        "coordinate": coordinate.model_dump(mode="json"),
        "generation": 4,
        "rows": [],
        "next_cursor": None,
        "truncated": False,
    }
    result = contracts.PlaybillSinceResult.model_validate(
        {
            **values,
            "result_digest": contracts._since_digest(  # type: ignore[attr-defined]
                "playbill-since-result-v1", values
            ),
        }
    )

    def stub(selected: str, *, request: contracts.PlaybillSinceRequest):
        assert selected == instance_id
        seen.update(request.model_dump(mode="json"))
        return result

    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_since", stub)
    response = client.post(
        f"/api/v1/{instance_id}/playbill/since",
        json={
            "tag": "playbill-since-request-v1",
            "generation": 2,
            "at": None,
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "http-since",
                "permitted_access_classes": ["instance", "public"],
                "disclose_restricted_existence": True,
            },
            "max_rows": 10,
            "max_bytes": 4096,
            "cursor": None,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == result.model_dump(mode="json")
    assert seen["generation"] == 2
    assert seen["max_rows"] == 10
    assert seen["max_bytes"] == 4096


def test_http_since_refuses_oversized_limits(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/since",
        json={
            "generation": 0,
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "http-since",
                "permitted_access_classes": ["instance"],
                "disclose_restricted_existence": False,
            },
            "max_rows": 1001,
        },
    )
    assert response.status_code == 400
    assert response.json() == {
        "error_type": "PlaybillSinceRequestInvalid",
        "message": (
            "playbill.since.request_invalid: request field $.max_rows is invalid: "
            "Input should be less than or equal to 1000"
        ),
        "error_code": "playbill.since.request_invalid",
        "errors": ["body.max_rows: Input should be less than or equal to 1000"],
        "context": {"field_path": "$.max_rows"},
        "mutation_receipt_id": None,
    }


VALID_PROFILE = {
    "tag": "playbill-coverage-access-profile-v1",
    "profile_id": "http-since",
    "permitted_access_classes": ["instance"],
    "disclose_restricted_existence": False,
}


def test_http_since_reports_every_invalid_field(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/since",
        json={
            "generation": -1,
            "access_profile": VALID_PROFILE,
            "max_rows": 1001,
            "max_bytes": 0,
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error_code"] == "playbill.since.request_invalid"
    assert payload["context"]["field_path"] == "$.generation"
    assert len(payload["errors"]) == 3
    assert any("max_rows" in item for item in payload["errors"])
    assert any("max_bytes" in item for item in payload["errors"])


def test_http_since_unknown_generation_is_a_typed_400(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/since",
        json={"generation": 999999, "access_profile": VALID_PROFILE},
    )
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error_type"] == "PlaybillSinceGenerationUnknown"
    assert payload["error_code"] == "playbill.since.generation_unknown"


@pytest.mark.parametrize(
    ("body", "expected_path_prefix"),
    (
        ({"generation": 0, "access_profile": VALID_PROFILE, "extra": 1}, "$.extra"),
        (
            {
                "tag": "playbill-wrong-tag",
                "generation": 0,
                "access_profile": VALID_PROFILE,
            },
            "$.tag",
        ),
        (
            {"generation": 0, "access_profile": VALID_PROFILE, "cursor": "garbage"},
            "$.cursor",
        ),
        ({"generation": 0, "access_profile": {"bogus": True}}, "$.access_profile"),
        ({"access_profile": VALID_PROFILE}, "$.generation"),
        (
            {
                "generation": 0,
                "access_profile": VALID_PROFILE,
                "at": {"git_oid": "zz"},
            },
            "$.at.",
        ),
    ),
)
def test_http_since_adversarial_bodies_are_typed_400(
    playbill_http: tuple[TestClient, str, Path],
    body: dict,
    expected_path_prefix: str,
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(f"/api/v1/{instance_id}/playbill/since", json=body)
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error_code"] == "playbill.since.request_invalid"
    assert payload["context"]["field_path"].startswith(expected_path_prefix)
    assert "Traceback" not in response.text


@pytest.mark.parametrize("body", ([1, 2], "string-body", None))
def test_http_since_non_object_bodies_are_typed_400(
    playbill_http: tuple[TestClient, str, Path],
    body: object,
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(f"/api/v1/{instance_id}/playbill/since", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "playbill.since.request_invalid"
