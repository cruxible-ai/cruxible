"""HTTP shapes of the reads that replace the cut surfaces."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_http_head_answers_coordinate_and_generation(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.get(f"/api/v1/{instance_id}/playbill/head")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tag"] == "playbill-head-v1" and body["instance"] == instance_id
    assert set(body) == {"tag", "instance", "coordinate", "generation"}
    pinned = client.get(
        f"/api/v1/{instance_id}/playbill/head", params={"at": body["coordinate"]["git_oid"]}
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == body
