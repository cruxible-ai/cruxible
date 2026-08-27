"""HTTP parity for the frozen Playbill since request."""

from __future__ import annotations

from pathlib import Path

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
        "errors": [],
        "context": {"field_path": "$.max_rows"},
        "mutation_receipt_id": None,
    }
