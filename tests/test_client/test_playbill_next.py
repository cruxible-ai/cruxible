"""Client request parity for the deterministic Playbill next queue."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cruxible_client import CruxibleClient, contracts

COORDINATE = {
    "tag": "playbill-accepted-coordinate-v1",
    "git_oid": "1" * 64,
    "semantic_root": "sha256:" + "2" * 64,
    "generation_root": "sha256:" + "3" * 64,
    "compiler_digest": "sha256:" + "4" * 64,
}
HEALTHY_STATUS = {
    "tag": "playbill-next-status-v1",
    "blocking": False,
    "instance": {"tag": "playbill-next-health-v1", "state": "active"},
    "floor": {"tag": "playbill-next-health-v1", "state": "current"},
    "ledger_mirror": {"tag": "playbill-next-health-v1", "state": "not_configured"},
    "provider_lane": {"tag": "playbill-next-health-v1", "state": "available"},
    "procedure_catalog": {"tag": "playbill-next-health-v1", "state": "not_observed"},
    "held": 0,
}
HAND_EDIT = {
    "operation": "hand_edit",
    "target": "Claim:c",
    "required_change": "revise_into_distinct_qualifiers",
    "arguments": {},
    "command": None,
}


def _client(handler: Any) -> CruxibleClient:
    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    return client


def _item(item_id: str, **values: Any) -> dict[str, Any]:
    return {
        "tag": "playbill-next-item-v1",
        "item_id": item_id,
        "severity": "warning",
        "reason": "claim_conflicted",
        "subject_identity": "Claim:c",
        "related_identities": [],
        "detail": {},
        "repair": HAND_EDIT,
        **values,
    }


def test_client_sends_explicit_time_access_and_workspace_observation() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-next-result-v1",
                "coordinate": COORDINATE,
                "evaluation_time": "2026-08-24T18:00:00.000000Z",
                "observed_domains": ["accepted_state", "workspace_floor"],
                "unobserved_domains": ["workspace_sources", "workspace_projections"],
                "status": HEALTHY_STATUS,
                "items": [],
                "result_digest": "sha256:" + "5" * 64,
            },
        )

    client = _client(handler)
    result = client.next_playbill(
        "inst",
        evaluation_time="2026-08-24T18:00:00Z",
        access_profile={
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "client-next",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": True,
        },
        workspace_observation={
            "tag": "playbill-next-workspace-observation-v1",
            "floor_status": "missing",
            "installed_coordinate": None,
            "drift_observations": None,
        },
    )

    assert result.observed_domains == ["accepted_state", "workspace_floor"]
    assert captured[0].url.path == "/api/v1/inst/playbill/next"
    payload: dict[str, Any] = json.loads(captured[0].content)
    assert payload["tag"] == "playbill-next-request-v2"
    assert "at_attestation_head_digest" not in payload
    assert payload["evaluation_time"] == "2026-08-24T18:00:00Z"
    assert payload["workspace_observation"]["floor_status"] == "missing"
    assert "result_digest" not in payload


def test_client_parses_v2_delta_removal_classification() -> None:
    removed_id = "sha256:" + "6" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["since_result_digest"] == "sha256:" + "5" * 64
        return httpx.Response(
            200,
            json={
                "tag": "playbill-next-result-v2",
                "coordinate": COORDINATE,
                "evaluation_time": "2026-08-24T18:00:00.000000Z",
                "observed_domains": ["accepted_state", "workspace_floor"],
                "unobserved_domains": ["workspace_sources", "workspace_projections"],
                "status": HEALTHY_STATUS,
                "items": [_item(removed_id)],
                "result_digest": "sha256:" + "7" * 64,
                "delta_since": "sha256:" + "5" * 64,
                "attestation_head_digest": "sha256:" + "8" * 64,
                "removed_item_ids": [removed_id],
            },
        )

    client = _client(handler)

    result = client.next_playbill(
        "inst",
        evaluation_time="2026-08-24T18:00:00Z",
        access_profile={
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "client-next",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": True,
        },
        since_result_digest="sha256:" + "5" * 64,
    )

    assert result.removed_item_ids == [removed_id]


def test_client_parses_typed_rows_findings_and_status() -> None:
    item_id = "sha256:" + "6" * 64
    runnable = {
        "operation": "playbill.block.sync",
        "target": "docs/runbook.md",
        "required_change": "resync_projection",
        "arguments": {"all": True},
        "command": "cruxible playbill block sync --all",
    }
    body = {
        "tag": "playbill-next-result-v2",
        "coordinate": COORDINATE,
        "evaluation_time": "2026-08-24T18:00:00.000000Z",
        "observed_domains": ["accepted_state", "workspace_floor"],
        "unobserved_domains": ["workspace_sources", "workspace_projections"],
        "status": HEALTHY_STATUS
        | {
            "held": 2,
            "floor": {
                "tag": "playbill-next-health-v1",
                "state": "stale",
                "detail": {"reported_status": "stale"},
                "repair": {
                    "operation": "playbill.floor.export",
                    "target": "inst",
                    "required_change": "replace_installed_floor",
                    "arguments": {},
                    "command": "cruxible playbill floor export",
                },
            },
        },
        "items": [
            _item(
                item_id,
                severity="repair",
                reason="projection_dirty",
                subject_identity="Block:docs/runbook.md#b",
                related_identities=["Claim:a"],
                detail={"block_id": "b"},
                repair=runnable,
                findings=[
                    {
                        "tag": "playbill-next-finding-v1",
                        "severity": "warning",
                        "reason": "projection_backing_stale",
                        "subject_identity": "Block:docs/runbook.md#b",
                        "detail": {"claim": "Claim:a"},
                        "repair": runnable,
                    }
                ],
            )
        ],
        "result_digest": "sha256:" + "7" * 64,
        "attestation_head_digest": "sha256:" + "8" * 64,
    }
    result = _client(lambda _request: httpx.Response(200, json=body)).next_playbill(
        "inst",
        evaluation_time="2026-08-24T18:00:00Z",
        access_profile={
            "tag": "playbill-coverage-access-profile-v1",
            "profile_id": "client-next",
            "permitted_access_classes": ["instance", "public"],
            "disclose_restricted_existence": True,
        },
    )

    (row,) = result.items
    assert isinstance(row, contracts.PlaybillNextItem)
    assert (row.severity, row.reason, row.related_identities) == (
        "repair",
        "projection_dirty",
        ["Claim:a"],
    )
    assert row.repair.command == "cruxible playbill block sync --all"
    assert row.repair.arguments == {"all": True}
    (finding,) = row.findings
    assert finding.reason == "projection_backing_stale"
    assert finding.detail == {"claim": "Claim:a"}
    assert result.status.held == 2
    assert result.status.floor.state == "stale"
    assert result.status.floor.repair is not None
    assert result.status.floor.repair.operation == "playbill.floor.export"
    assert result.status.instance.repair is None
    # A row standing alone keeps its bytes: empty findings stay off the wire.
    assert "findings" not in _item(item_id)
    lone = contracts.PlaybillNextItem.model_validate(_item(item_id))
    assert "findings" not in lone.model_dump(mode="json")
    assert result.model_dump(mode="json")["items"][0]["findings"][0]["reason"] == (
        "projection_backing_stale"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "not_a_reason"},
        {"severity": "urgent"},
        {"repair": HAND_EDIT | {"operation": "playbill.unknown"}},
        {"item_id": "not-a-digest"},
        {"unexpected": True},
    ],
)
def test_client_refuses_an_untyped_row(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        contracts.PlaybillNextItem.model_validate(_item("sha256:" + "6" * 64) | change)


def test_client_status_requires_every_facet() -> None:
    status = dict(HEALTHY_STATUS)
    status.pop("procedure_catalog")
    with pytest.raises(ValidationError):
        contracts.PlaybillNextStatus.model_validate(status)
