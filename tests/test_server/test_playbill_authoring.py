"""HTTP route parity for the frozen AuthoringIntent verbs."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client import contracts

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32


def test_http_compile_and_submit_keep_the_frozen_request_boundary(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, instance_id, _private_key = playbill_http
    seen: list[tuple[str, object]] = []

    def compile_stub(selected: str, *, payload: object, intent_id: str | None = None):
        seen.append((selected, payload))
        assert intent_id is None
        return contracts.PlaybillAuthoringPreflightResult(
            verdict="refused",
            certificate={"certificate_digest": "sha256:" + "6" * 64},
            frontier={"diagnostics": [{"code": "example"}]},
        )

    def submit_stub(selected: str, intent_id: str):
        seen.append((selected, intent_id))
        status = contracts.PlaybillCandidateStatus(
            state="draft",
            current_accepted_coordinate=COORDINATE,
        )
        return contracts.PlaybillAuthoringSubmitResult(
            intent={"intent_id": intent_id},
            status=status,
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_compile", compile_stub
    )
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_authoring_submit", submit_stub)
    payload = {
        "tag": "playbill-claim-authoring-payload-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": {
                "tag": "playbill-semantic-address-v1",
                "artifact_path": "subjects/work_item/wi-42.yaml",
                "selector": {"scheme": "artifact-v1", "value": ""},
            },
            "predicate": "work.status",
            "qualifier": None,
            "object": {"kind": "literal", "value": "ready"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Observed ready.",
        "source": {
            "tag": "playbill-self-source-body-v1",
            "content_base64": "cmVhZHk=",
        },
        "citation_role": None,
        "claim_ref": None,
        "existing_claim_dispositions": [],
        "insertion_target": None,
    }
    compiled = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/compile",
        json={
            "tag": "playbill-authoring-intent-compile-request-v1",
            "payload": payload,
            "intent_id": None,
        },
    )
    submitted = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents/{INTENT_ID}/submit",
        json={"tag": "playbill-authoring-intent-submit-request-v1"},
    )

    assert compiled.status_code == 200, compiled.text
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["tag"] == "playbill-authoring-submit-result-v1"
    assert seen[0][0] == instance_id
    assert seen[1] == (instance_id, INTENT_ID)


def test_http_refuses_digest_and_base_smuggling_in_request_models(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _private_key = playbill_http
    response = client.post(
        f"/api/v1/{instance_id}/playbill/authoring/intents",
        json={
            "tag": "playbill-authoring-intent-create-request-v1",
            "payload": {
                "tag": "playbill-claim-authoring-payload-v1",
                "claim_id": "CLM-" + "0" * 32,
                "base": COORDINATE.model_dump(mode="json"),
            },
        },
    )
    assert response.status_code == 422
    assert "claim_id" in response.text or "statement" in response.text
