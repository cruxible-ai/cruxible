"""HTTP orient: one GET, values without nulls, coded refusals."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_http_orient_answers_the_map_rendered_for_the_requested_surface(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http

    response = client.get(f"/api/v1/{instance_id}/playbill/orient", params={"surface": "mcp"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tag"] == "playbill-orient-v1" and body["instance"] == instance_id
    assert body["kinds"] == [] and body["truncated"] is False
    assert set(body["artifacts"]) == {"claim_types", "procedures", "documents", "queries"}
    assert body["you"]["actor"] is not None
    # Optional parts that do not apply are absent, never null.
    assert "kind_detail" not in body and "next_cursor" not in body
    assert None not in body.values()
    assert all(line.startswith("cruxible_playbill_") for line in body["next"])

    pinned = client.get(
        f"/api/v1/{instance_id}/playbill/orient",
        params={"at": body["coordinate"]["git_oid"], "section": "documents"},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["coordinate"] == body["coordinate"]
    assert pinned.json()["documents"] == []


def test_http_orient_refusals_are_coded(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    url = f"/api/v1/{instance_id}/playbill/orient"

    missing = client.get(url, params={"kind": "project.nothing"})
    assert missing.status_code == 404, missing.text
    assert missing.json()["error_code"] == "playbill.orient.kind_not_found"
    assert missing.json()["context"]["kind"] == "project.nothing"
    assert missing.json()["repair"]["operation"] == "playbill.orient"

    both = client.get(url, params={"kind": "project.nothing", "section": "queries"})
    assert both.status_code == 400, both.text
    assert both.json()["error_code"] == "playbill.orient.request_invalid"

    stale = client.get(url, params={"section": "queries", "cursor": "not-a-cursor"})
    assert stale.status_code == 400, stale.text
    assert stale.json()["error_code"] == "playbill.list.cursor_mismatch"

    assert client.get(url, params={"section": "nothing"}).status_code == 422
