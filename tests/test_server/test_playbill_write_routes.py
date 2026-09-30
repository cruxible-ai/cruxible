"""POST /playbill/set, /retire and /write: outcomes over HTTP, refusals included."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import KIND, seed_write_vocabulary

WI1 = f"{KIND}/wi-1"


def _seed(client: TestClient, instance_id: str) -> str:
    actor = client.get(f"/api/v1/{instance_id}/playbill/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    return actor


def test_set_retire_and_write_answer_outcomes(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}/playbill"

    first = client.post(
        f"{base}/set",
        json={"subject": WI1, "field": "status", "value": "ready", "because": "Checked."},
    )
    assert first.status_code == 200, first.text
    outcome = first.json()
    assert outcome["status"] == "accepted"
    claim = outcome["changes"][0]["claim"]
    assert outcome["changes"][0]["verdict"] == "supported"

    links = client.post(
        f"{base}/write",
        json={
            "because": "Linked.",
            "changes": [
                {"op": "add", "subject": WI1, "field": "governs", "value": f"{KIND}/wi-2"},
                {"op": "add", "subject": WI1, "field": "governs", "value": f"{KIND}/wi-3"},
            ],
        },
    )
    assert links.status_code == 200 and links.json()["status"] == "accepted", links.text

    retired = client.post(f"{base}/retire", json={"target": claim, "because": "Withdrawn."})
    assert retired.status_code == 200 and retired.json()["status"] == "accepted", retired.text
    assert retired.json()["changes"][0]["claim"] == claim


def test_expect_travels_on_every_write_route(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}/playbill"
    post = lambda route, body: client.post(f"{base}/{route}", json=body).json()  # noqa: E731

    first = post("set", {"subject": WI1, "field": "status", "value": "ready", "because": "x"})
    assert first["status"] == "accepted", first
    stale = post(
        "set",
        {"subject": WI1, "field": "status", "value": "done", "because": "x", "expect": "blocked"},
    )
    assert stale["refusal"]["code"] == "playbill.write.slot_changed", stale
    assert stale["refusal"]["field_path"] == "changes[0].expect"
    added = post(
        "write",
        {
            "because": "x",
            "changes": [
                {
                    "op": "add",
                    "subject": WI1,
                    "field": "governs",
                    "value": f"{KIND}/wi-2",
                    "expect_absent": True,
                },
                {
                    "op": "set",
                    "subject": WI1,
                    "field": "status",
                    "value": "done",
                    "expect": "ready",
                },
            ],
        },
    )
    assert added["status"] == "accepted", added
    retired = post(
        "retire",
        {
            "target": {"subject": WI1, "field": "governs"},
            "because": "x",
            "expect": [f"{KIND}/wi-2"],
        },
    )
    assert retired["status"] == "accepted", retired


def test_the_write_route_takes_a_default_subject(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}/playbill"
    written = client.post(
        f"{base}/write",
        json={
            "because": "x",
            "subject": WI1,
            "changes": [
                {"op": "set", "field": "status", "value": "ready"},
                {"op": "add", "field": "governs", "value": f"{KIND}/wi-2"},
            ],
        },
    ).json()
    assert written["status"] == "accepted", written
    assert {item["subject"] for item in written["changes"]} == {WI1}
    orphan = client.post(
        f"{base}/write",
        json={"because": "x", "changes": [{"op": "set", "field": "status", "value": "done"}]},
    ).json()
    assert orphan["refusal"]["code"] == "playbill.write.subject_required", orphan


def test_a_refused_write_is_an_outcome_and_a_malformed_one_is_a_422(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}/playbill"

    refused = client.post(
        f"{base}/set",
        json={"subject": WI1, "field": "status", "value": "dne", "because": "x", "dry_run": True},
    )
    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert body["status"] == "would_refuse"
    assert body["refusal"]["code"] == "playbill.write.value_not_member"
    assert "blocked, done, ready" in body["refusal"]["message"]

    malformed = client.post(f"{base}/write", json={"because": "x", "changes": []})
    assert malformed.status_code == 422


def test_the_dedicated_claim_retire_route_is_gone(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/claims/CLM-{'0' * 32}/retire",
        json={"mode": "preflight"},
    )
    assert response.status_code in {404, 405}
