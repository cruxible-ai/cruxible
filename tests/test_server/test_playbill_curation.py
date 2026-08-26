"""HTTP delivery for the G9a curation-list foundation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_http_curation_list_is_read_tier_and_returns_operational_head(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/curation/list",
        json={
            "tag": "playbill-curation-list-request-v1",
            "evaluation_time": "2026-08-26T16:00:00+00:00",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tag"] == "playbill-curation-list-result-v1"
    assert payload["items"] == []
    assert payload["observation_coverage"]["source_count"] == 0
    assert payload["operational_head_digest"].startswith("sha256:")


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
            {"tag": "playbill-curation-suppress-request-v1", "scope": "instance"},
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
        f"/api/v1/{instance_id}/playbill/curation/{route}",
        json={
            "item_id": "sha256:" + "1" * 64,
            "expected_latest_event_digest": "sha256:" + "2" * 64,
            "reason": "operator-reviewed mechanical facts",
            **payload,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "playbill.curation.item_not_found"
