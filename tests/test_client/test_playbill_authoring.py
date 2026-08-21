"""Client request-tag and response-model parity for ergonomic authoring."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cruxible_client import CruxibleClient
from cruxible_client.playbill_briefs import prepare_playbill_brief

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}
INTENT_ID = "AIT-" + "5" * 32
OBSERVATION = {
    "tag": "playbill-insertion-confirmation-observation-v1",
    "expectation_id": "sha256:" + "6" * 64,
    "source_id": "repo.work-items",
    "coordinate": {
        "kind": "observed_digest",
        "source_content_digest": "sha256:" + "7" * 64,
        "source_byte_length": 5,
    },
    "observed_content_digest": "sha256:" + "7" * 64,
    "selected_start_byte": 0,
    "selected_end_byte": 5,
    "selected_bytes_digest": "sha256:" + "8" * 64,
    "observed_occurrence_count": 1,
}


def _client(handler: Any) -> CruxibleClient:
    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    return client


def _status() -> dict[str, Any]:
    return {
        "tag": "playbill-candidate-status-v1",
        "state": "draft",
        "proposal_id": None,
        "candidate_digest": None,
        "current_accepted_coordinate": COORDINATE,
        "path_to_acceptance": [],
        "accepted_generation": None,
    }


def test_client_speaks_frozen_compile_and_submit_requests() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/compile"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-authoring-preflight-result-v1",
                    "verdict": "refused",
                    "certificate": {"certificate_digest": "sha256:" + "6" * 64},
                    "frontier": {"diagnostics": [{"code": "example"}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-submit-result-v1",
                "intent": {"intent_id": INTENT_ID},
                "status": _status(),
            },
        )

    client = _client(handler)
    payload = {"tag": "playbill-claim-authoring-payload-v1", "example": "payload"}
    compiled = client.compile_playbill_authoring("inst", payload=payload)
    submitted = client.submit_playbill_authoring_intent("inst", INTENT_ID)

    assert compiled.verdict == "refused"
    assert submitted.status.state == "draft"
    assert json.loads(captured[0].content) == {
        "tag": "playbill-authoring-intent-compile-request-v1",
        "payload": payload,
        "intent_id": None,
    }
    assert json.loads(captured[1].content) == {"tag": "playbill-authoring-intent-submit-request-v1"}
    assert all(key not in captured[0].content.decode() for key in ("base", "claim_id"))


def test_client_get_resume_list_and_status_are_path_only_reads() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=_status())
        if request.url.path.endswith("/authoring/intents"):
            return httpx.Response(
                200,
                json={"tag": "playbill-authoring-intent-list-v1", "intents": []},
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-authoring-intent-view-v1",
                "intent": {"intent_id": INTENT_ID},
            },
        )

    client = _client(handler)
    client.get_playbill_authoring_intent("inst", INTENT_ID)
    client.resume_playbill_authoring_intent("inst", INTENT_ID)
    client.list_pending_playbill_authoring_intents("inst")
    status = client.playbill_authoring_intent_status("inst", INTENT_ID)

    assert status.state == "draft"
    assert [item.method for item in captured] == ["GET", "GET", "GET", "GET"]
    assert all(not item.content for item in captured)


def test_client_speaks_frozen_insertion_confirm_and_abandon_requests() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/confirm"):
            return httpx.Response(
                200,
                json={
                    "tag": "playbill-insertion-confirm-result-v1",
                    "outcome": "stale_target",
                    "intent": {"intent_id": INTENT_ID},
                    "expectation": {"expectation_id": OBSERVATION["expectation_id"]},
                    "successor_status": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "tag": "playbill-insertion-abandon-result-v1",
                "intent": {"intent_id": INTENT_ID},
                "expectation": {"state": "abandoned"},
            },
        )

    client = _client(handler)
    confirmed = client.confirm_playbill_authoring_insertion(
        "inst",
        INTENT_ID,
        observation=OBSERVATION,
    )
    abandoned = client.abandon_playbill_authoring_insertion("inst", INTENT_ID)

    assert confirmed.outcome == "stale_target"
    assert abandoned.expectation["state"] == "abandoned"
    assert json.loads(captured[0].content) == {
        "tag": "playbill-insertion-confirm-request-v1",
        "observation": OBSERVATION,
    }
    assert json.loads(captured[1].content) == {"tag": "playbill-insertion-abandon-request-v1"}


def test_prepare_brief_remains_an_ordinary_claim_payload() -> None:
    subject = {
        "tag": "playbill-semantic-address-v1",
        "artifact_path": "subjects/work_item/wi-42.yaml",
        "selector": {"scheme": "artifact-v1", "value": ""},
    }

    payload = prepare_playbill_brief(
        subject=subject,
        purpose="How should this be released?",
        kind="guidance",
        prose="Use the release checklist.",
        rationale="Keep the release guidance governed.",
    )

    assert payload["tag"] == "playbill-claim-authoring-payload-v1"
    assert payload["statement"]["predicate"] == "knowledge.brief"
    assert payload["statement"]["qualifier"] is None
    assert payload["statement"]["object"]["value"]["tag"] == ("playbill-knowledge-brief-value-v1")
