"""Client request-tag and response-model parity for ergonomic authoring."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cruxible_client import CruxibleClient

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}
INTENT_ID = "AIT-" + "5" * 32


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
