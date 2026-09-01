"""PB-E client coordinate-bound explanation transport."""

from __future__ import annotations

import json

import httpx

from cruxible_client import contracts
from tests.test_client.test_playbill_documents import COORDINATE, _client


def test_client_explain_transmits_only_subject_coordinate_and_read_options() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "tag": "playbill-explain-unsupported-detail-v1",
                "subject": captured[0]["subject"],
                "coordinate": COORDINATE,
                "requested_detail": "proof",
                "code": "playbill.explain.detail_unsupported",
                "message": "proof retrieval is deferred",
                "supported_details": ["summary", "evidence"],
            },
        )

    client = _client(handler)
    result = client.explain_playbill_subject(
        "inst_test",
        subject={
            "tag": "playbill-semantic-address-v1",
            "artifact_path": "documents/design.json",
            "selector": {"scheme": "artifact-v1", "value": ""},
        },
        at=contracts.PlaybillAcceptedCoordinate.model_validate(COORDINATE),
        detail="proof",
    )

    assert isinstance(result, contracts.PlaybillExplainUnsupportedDetail)
    assert captured[0]["at"] == COORDINATE
    serialized = json.dumps(captured[0])
    assert "private_key" not in serialized
    assert "local_path" not in serialized
    assert "proposal" not in serialized
