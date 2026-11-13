"""HTTP shapes of the reads that replace the cut surfaces."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_http_head_answers_coordinate_and_generation(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.get(f"/api/v1/{instance_id}/head")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tag"] == "playbill-head-v1" and body["instance"] == instance_id
    assert set(body) == {"tag", "instance", "coordinate", "generation"}
    pinned = client.get(f"/api/v1/{instance_id}/head", params={"at": body["coordinate"]["git_oid"]})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == body


def test_http_orient_serves_principals_and_policies_sections(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    url = f"/api/v1/{instance_id}/orient"

    principals = client.get(url, params={"section": "principals"})
    assert principals.status_code == 200, principals.text
    assert {row["principal_id"] for row in principals.json()["principals"]} >= {"daemon"}
    policies = client.get(url, params={"section": "policies"})
    assert policies.status_code == 200, policies.text
    assert any(row["policy_kind"] == "approval_policy" for row in policies.json()["policies"])
    card = client.post(f"/api/v1/{instance_id}/get", json={"ref": "ApprovalPolicy:instance"})
    assert card.status_code == 200, card.text
    assert card.json()["kind"] == "approval_policy"
