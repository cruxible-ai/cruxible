"""HTTP delivery for the G9a curation-list foundation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_core.curation.review_operational import (
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalStoreError,
)


def test_http_curation_list_is_read_tier_and_returns_operational_head(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/curation/list",
        json={
            "tag": "playbill-curation-list-request-v1",
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "test-curation",
                "permitted_access_classes": ["instance", "public"],
                "disclose_restricted_existence": True,
            },
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tag"] == "playbill-curation-list-result-v1"
    assert payload["items"] == []
    # Detection is opt-in (G1): a fresh instance seeds no curation-detect Trigger
    # and has no recorded run or scan.
    assert payload["detection"] == {
        "state": "never_run",
        "trigger": "missing",
        "detected_through_generation": None,
        "detected_at": None,
    }
    assert payload["observation_coverage"] is None
    assert payload["operational_head_digest"].startswith("sha256:")


def test_http_block_observe_records_pages_and_refuses_an_empty_batch_typed(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    page = {
        "tag": "playbill-declared-block-page-observation-v1",
        "source_id": "corpus.notes",
        "observed_source_digest": "sha256:" + "1" * 64,
        "byte_length": 12,
    }
    recorded = client.post(
        f"/api/v1/{instance_id}/blocks/observe",
        json={"tag": "playbill-block-observe-request-v1", "pages": [page]},
    )
    assert recorded.status_code == 200, recorded.text
    payload = recorded.json()
    assert payload["tag"] == "playbill-block-observe-result-v1"
    assert payload["status"] == "recorded"
    # An evidence-only page names no Document, so its blocks cannot be tied to one.
    assert payload["observation_coverage"]["omissions"] == [
        {"reason": "block_subject_unresolved", "count": 1}
    ]

    empty = client.post(
        f"/api/v1/{instance_id}/blocks/observe",
        json={"tag": "playbill-block-observe-request-v1", "pages": []},
    )
    assert empty.status_code == 400, empty.text
    assert empty.json()["error_code"] == "cruxible.block.observation_invalid"
    retired = client.post(f"/api/v1/{instance_id}/curation/observe", json={})
    assert retired.status_code in {404, 405}, retired.text


@pytest.mark.parametrize(
    ("route", "payload"),
    (
        (
            "overrule",
            {
                "tag": "playbill-curation-overrule-request-v1",
                "item_id": "not-a-digest",
                "expected_latest_event_digest": "sha256:" + "2" * 64,
                "reason": "operator-reviewed mechanical facts",
            },
        ),
        (
            "accept-fixed",
            {
                "tag": "playbill-curation-accept-fixed-request-v1",
                "item_id": "not-a-digest",
                "expected_latest_event_digest": "sha256:" + "2" * 64,
                "reason": "operator-reviewed mechanical facts",
                "accepted_proposal_id": "sha256:" + "3" * 64,
                "accepted_changeset_digest": "sha256:" + "4" * 64,
            },
        ),
        (
            "suppress",
            {
                "tag": "playbill-curation-suppress-request-v1",
                "item_id": "not-a-digest",
                "expected_latest_event_digest": "sha256:" + "2" * 64,
                "reason": "operator-reviewed mechanical facts",
                "scope": "lineage",
            },
        ),
    ),
)
def test_http_curation_lifecycle_validation_is_a_typed_refusal(
    playbill_http: tuple[TestClient, str, Path],
    route: str,
    payload: dict[str, object],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/curation/{route}",
        json=payload,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "cruxible.curation.refused"


@pytest.mark.parametrize(
    ("route", "payload"),
    (
        (
            "overrule",
            {"tag": "playbill-curation-overrule-request-v1"},
        ),
        (
            "accept-fixed",
            {
                "tag": "playbill-curation-accept-fixed-request-v1",
                "accepted_proposal_id": "sha256:" + "3" * 64,
                "accepted_changeset_digest": "sha256:" + "4" * 64,
            },
        ),
        (
            "suppress",
            {"tag": "playbill-curation-suppress-request-v1", "scope": "lineage"},
        ),
        (
            "unsuppress",
            {"tag": "playbill-curation-unsuppress-request-v1"},
        ),
    ),
)
def test_http_curation_lifecycle_routes_deliver_typed_domain_refusals(
    playbill_http: tuple[TestClient, str, Path],
    route: str,
    payload: dict[str, object],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/curation/{route}",
        json={
            "item_id": "sha256:" + "1" * 64,
            "expected_latest_event_digest": "sha256:" + "2" * 64,
            "reason": "operator-reviewed mechanical facts",
            **payload,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "cruxible.curation.item_not_found"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    (
        (
            ReviewOperationalStoreError("corrupt operational event"),
            400,
            "cruxible.curation.operational_store_invalid",
        ),
        (
            ReviewOperationalConcurrentChangeError(),
            409,
            "cruxible.curation.concurrent_change",
        ),
    ),
)
def test_http_curation_maps_both_operational_store_statuses(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
) -> None:
    from cruxible_core.runtime import playbill_api

    client, instance_id, _private_key = playbill_http

    def refuse(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(playbill_api, "playbill_curation_list", refuse)
    response = client.post(
        f"/api/v1/{instance_id}/curation/list",
        json={
            "tag": "playbill-curation-list-request-v1",
            "access_profile": {
                "tag": "playbill-coverage-access-profile-v1",
                "profile_id": "test-curation",
                "permitted_access_classes": ["instance", "public"],
                "disclose_restricted_existence": True,
            },
        },
    )

    assert response.status_code == status, response.text
    assert response.json()["error_code"] == code
