"""A direct ClaimType propose lands when policy and the caller allow it (one landing rule)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_server.conftest import _playbill_http

_MINIMAL = {
    "predicate": "catalog.part.price",
    "allowed_subject_kinds": ["catalog.part"],
    "object_kind": "literal",
    "literal_schema": {"type": "integer"},
    "cardinality": "one",
}


def _propose(client: TestClient, instance_id: str, **extra: object) -> dict[str, object]:
    response = client.post(
        f"/api/v1/{instance_id}/claim-types/proposals",
        json={
            "tag": "playbill-claim-type-input-propose-request-v1",
            "input": _MINIMAL,
            "proposal_name": "part-price",
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_minimal_claim_type_lands_in_the_propose_call(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http

    body = _propose(client, instance_id)

    outcome = body["proposal"]["outcome"]
    assert outcome["status"] == "landed", outcome
    assert body["lint"]["warnings"] == []
    read = client.post(
        f"/api/v1/{instance_id}/query",
        json={
            "kind": "ClaimType",
            "where": [{"field": "name", "eq": "catalog.part.price"}],
            "select": ["roles"],
        },
    )
    assert read.status_code == 200, read.text
    assert [(row["predicate"], row["roles"]) for row in read.json()["rows"]] == [
        ("catalog.part.price", ["normative"])
    ]


def test_accept_never_keeps_the_proposal(playbill_http: tuple[TestClient, str, Path]) -> None:
    client, instance_id, _key = playbill_http

    outcome = _propose(client, instance_id, accept="never")["proposal"]["outcome"]

    assert (outcome["status"], outcome["reason"]) == ("proposed", "accept_never")
    assert outcome["next"].startswith("cruxible proposal activate ")


def test_a_preview_never_lands(playbill_http: tuple[TestClient, str, Path]) -> None:
    client, instance_id, _key = playbill_http

    body = _propose(client, instance_id, dry_run=True)

    assert body["proposal"]["status"] == "would_propose"
    assert "outcome" not in body["proposal"]


def test_an_independent_approval_instance_stops_at_the_approver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = _playbill_http(tmp_path, monkeypatch, require_independent_approval=True)
    client, instance_id, _key = next(opened)
    try:
        outcome = _propose(client, instance_id)["proposal"]["outcome"]
    finally:
        for _ in opened:
            pass

    assert (outcome["status"], outcome["reason"]) == (
        "proposed",
        "independent_approval_required",
    )


def test_adopting_a_builtin_the_instance_already_holds_is_a_typed_refusal(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/claim-types/proposals",
        json={
            "tag": "cruxible-builtin-claim-type-propose-request-v1",
            "builtin": "semantic.alias",
            "proposal_name": "adopt-alias",
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "cruxible.claim_type.builtin_already_adopted"
    assert "cruxible get ClaimType:semantic.alias" in response.json()["message"]
