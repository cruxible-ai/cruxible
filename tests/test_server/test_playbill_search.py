"""HTTP route parity for search/list/orient."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client import contracts


def test_http_search_keeps_access_profile_and_digests_server_owned(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: dict[str, object] = {}

    def search_stub(selected: str, **values: object) -> contracts.PlaybillSearchResult:
        assert selected == instance_id
        seen.update(values)
        return contracts.PlaybillSearchResult(
            mode="search",
            coordinate=contracts.PlaybillAcceptedCoordinate(
                git_oid="1" * 64,
                semantic_root="sha256:" + "2" * 64,
                generation_root="sha256:" + "3" * 64,
                compiler_digest="sha256:" + "4" * 64,
            ),
            evaluation_time="2026-08-21T14:00:00.000000Z",
            rows=[],
            selection_basis_digest="sha256:" + "5" * 64,
            truncated=False,
            result_digest="sha256:" + "6" * 64,
        )

    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_search", search_stub)
    response = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={
            "mode": "search",
            "query": "release",
            "kinds": ["claim", "procedure"],
            "statuses": ["accepted"],
            "evaluation_time": "2026-08-21T14:00:00Z",
        },
    )

    assert response.status_code == 200, response.text
    assert seen["mode"] == "search"
    assert seen["kinds"] == ("claim", "procedure")
    assert seen["statuses"] == ("accepted",)


def test_http_refuses_caller_owned_search_access_profile(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={
            "mode": "orient",
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "widened",
                "permitted_access_classes": ["restricted"],
            },
        },
    )
    assert response.status_code == 422


def test_http_search_openapi_and_runtime_reject_removed_brief_kind(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    schemas = client.app.openapi()["components"]["schemas"]
    request = schemas["PlaybillSearchRequest"]
    item_schema = request["properties"]["kinds"]["items"]
    assert item_schema["enum"] == ["claim", "procedure", "demand"]

    response = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "list", "kinds": ["brief"]},
    )
    assert response.status_code == 422


def test_http_search_reads_an_empty_kind_filter_as_every_kind(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """`kinds: []` means "no kind restriction", as `statuses: []` already does."""
    client, instance_id, _private_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "list", "kinds": [], "statuses": []},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "list"


def test_http_search_empty_kind_filter_matches_the_explicit_full_kind_list(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    implicit = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={"mode": "list", "kinds": [], "evaluation_time": "2026-08-21T14:00:00Z"},
    )
    explicit = client.post(
        f"/api/v1/{instance_id}/playbill/search",
        json={
            "mode": "list",
            "kinds": ["claim", "demand", "procedure"],
            "evaluation_time": "2026-08-21T14:00:00Z",
        },
    )

    assert implicit.status_code == 200, implicit.text
    assert explicit.status_code == 200, explicit.text
    assert implicit.json()["result_digest"] == explicit.json()["result_digest"]
