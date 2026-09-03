from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_core.runtime.playbill_manager import get_playbill_manager


def test_http_seed_route_is_idempotent_after_init_generation_one(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _private_key = playbill_http
    instance = get_playbill_manager().get(instance_id)
    assert instance.accepted_history()[-1].sequence == 1

    response = http.post(f"/api/v1/{instance_id}/playbill/providers/seed", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "already_current"
    assert body["materialization_source"] == "local"
    assert body["accepted_coordinate"]["git_oid"]
    assert instance.accepted_history()[-1].sequence == 1
