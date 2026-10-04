"""POST /set, /retire and /write: outcomes over HTTP, refusals included."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import (
    KIND,
    REPORTS,
    cited_captures,
    report_evidence,
    seed_write_vocabulary,
)

WI1 = f"{KIND}/wi-1"


def _seed(client: TestClient, instance_id: str) -> str:
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    return actor


def test_set_retire_and_write_answer_outcomes(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}"

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
    base = f"/api/v1/{instance_id}"
    post = lambda route, body: client.post(f"{base}/{route}", json=body).json()  # noqa: E731

    first = post("set", {"subject": WI1, "field": "status", "value": "ready", "because": "x"})
    assert first["status"] == "accepted", first
    stale = post(
        "set",
        {"subject": WI1, "field": "status", "value": "done", "because": "x", "expect": "blocked"},
    )
    assert stale["refusal"]["code"] == "cruxible.write.slot_changed", stale
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
    base = f"/api/v1/{instance_id}"
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
    assert orphan["refusal"]["code"] == "cruxible.write.subject_required", orphan


def test_capture_handles_and_contract_evidence_over_http(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}"
    evidence = report_evidence(tmp_path, "Count: 3")
    first = client.post(
        f"{base}/set",
        json={
            "subject": WI1,
            "field": "measured",
            "value": 3,
            "because": "x",
            "evidence": evidence,
        },
    ).json()
    assert first["status"] == "accepted", first
    instance = get_playbill_manager().get(instance_id)
    (digest,) = cited_captures(instance, first["changes"][0]["claim"])
    handle = "CAP-" + digest.removeprefix("sha256:")[:12]
    cited = client.post(
        f"{base}/set",
        json={
            "subject": f"{KIND}/wi-2",
            "field": "measured",
            "value": 3,
            "because": "x",
            "evidence": {"kind": "capture", "capture": handle},
        },
    ).json()
    assert cited["changes"][0]["capture"] == handle, cited
    by_contract = client.post(
        f"{base}/write",
        json={
            "because": "x",
            "subject": WI1,
            "changes": [
                {
                    "op": "add",
                    "field": "labels",
                    "value": "counted",
                    "evidence": {"kind": "contract", "contract": REPORTS.identity.name},
                }
            ],
        },
    ).json()
    assert by_contract["changes"][0]["capture"] == handle, by_contract
    short = client.post(
        f"{base}/set",
        json={
            "subject": WI1,
            "field": "measured",
            "value": 3,
            "because": "x",
            "evidence": {"kind": "capture", "capture": "CAP-abc"},
        },
    )
    assert short.status_code == 422


def test_a_refused_write_is_an_outcome_and_a_malformed_one_is_a_422(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    _seed(client, instance_id)
    base = f"/api/v1/{instance_id}"

    refused = client.post(
        f"{base}/set",
        json={"subject": WI1, "field": "status", "value": "dne", "because": "x", "dry_run": True},
    )
    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert body["status"] == "would_refuse"
    assert body["refusal"]["code"] == "cruxible.write.value_not_member"
    assert "blocked, done, ready" in body["refusal"]["message"]

    malformed = client.post(f"{base}/write", json={"because": "x", "changes": []})
    assert malformed.status_code == 422


def test_the_dedicated_claim_retire_route_is_gone(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/claims/CLM-{'0' * 32}/retire",
        json={"mode": "preflight"},
    )
    assert response.status_code in {404, 405}


def test_the_openapi_outcome_declares_each_warning_variant(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, _instance_id, _key = playbill_http
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    outcome = schemas["WriteOutcome"]
    items = outcome["properties"]["warnings"]["items"]
    assert items["discriminator"]["propertyName"] == "code"
    verdict = schemas["VerdictNotSupportedWarning"]
    newer = schemas["NewerCaptureNotCitableWarning"]
    assert "verdict" in verdict["required"] and "capture" not in verdict["properties"]
    assert "capture" in newer["required"] and "verdict" not in newer["properties"]
    assert "WriteWarning" not in schemas


def test_a_cold_write_preview_opens_its_instance_behind_the_guards(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """F-005: the actor is resolved and the instance opened behind the preview's guards.

    A dry-run set on an instance the daemon has not opened yet writes nothing;
    one whose open would first repair a derived file refuses by name. That
    holds for a principal claim too, whose check opens the instance.
    """

    from cruxible_core.server.auth import PRINCIPAL_ID_HEADER
    from tests.support.store_snapshot import assert_writes_nothing

    client, instance_id, _key = playbill_http
    actor = _seed(client, instance_id)
    base = f"/api/v1/{instance_id}"
    body = {"subject": WI1, "field": "status", "value": "ready", "because": "x", "dry_run": True}
    manager = get_playbill_manager()

    def settle() -> None:
        # One ordinary cold open first: it writes the replay checkpoint a quiet
        # daemon would already hold, so the preview's own open repairs nothing.
        manager.clear()
        client.get(f"{base}/head")
        manager.consumer_runner.close()
        manager.get(instance_id).settled_workspace_advertisement()
        manager.flush_replay_checkpoints()

    for headers in ({}, {PRINCIPAL_ID_HEADER: actor}):
        settle()
        manager.clear()
        cold = assert_writes_nothing(
            [tmp_path], lambda: client.post(f"{base}/set", json=body, headers=headers)
        )
        assert cold.status_code == 200 and cold.json()["status"] == "would_accept", cold.text

        serving = manager.get(instance_id).root / "projections" / "serving.json"
        settle()
        serving.unlink()
        manager.clear()
        refused = assert_writes_nothing(
            [tmp_path], lambda: client.post(f"{base}/set", json=body, headers=headers)
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error_code"] == "cruxible.preview.recovery_pending"
        assert not serving.exists()
        client.get(f"{base}/head")  # an ordinary read repairs it
        assert serving.exists()
