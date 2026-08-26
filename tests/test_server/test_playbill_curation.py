"""HTTP delivery for the G9a curation-list foundation."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_http_curation_list_is_read_tier_and_returns_operational_head(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/curation/list",
        json={"tag": "playbill-curation-list-request-v1"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tag"] == "playbill-curation-list-result-v1"
    assert payload["items"] == []
    assert payload["observation_coverage"]["source_count"] == 0
    assert payload["operational_head_digest"].startswith("sha256:")
