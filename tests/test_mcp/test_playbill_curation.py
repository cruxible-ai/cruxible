"""MCP curation tools are thin delegates; the list is a pure read."""

from __future__ import annotations

import pytest

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.service.discovery.next import NextWorkspaceObservationInvalid
from tests.support.mcp_daemon import bind_mcp_daemon


def _action_result(item_id: str) -> contracts.CurationActionResult:
    return contracts.CurationActionResult(
        coordinate=contracts.AcceptedCoordinate(
            git_oid="1" * 64,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        generation=7,
        operational_head_digest="sha256:" + "5" * 64,
        item={"item_id": item_id, "status": "resolved"},
    )


def test_mcp_curation_list_is_one_thin_read_delegate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    def stub(instance_id: str, *, request: dict[str, object]):  # type: ignore[no-untyped-def]
        seen["instance_id"] = instance_id
        seen["request"] = request
        return contracts.CurationListResult(
            coordinate=contracts.AcceptedCoordinate(
                git_oid="1" * 64,
                semantic_root="sha256:" + "2" * 64,
                generation_root="sha256:" + "3" * 64,
                compiler_digest="sha256:" + "4" * 64,
            ),
            generation=0,
            operational_head_digest="sha256:" + "5" * 64,
            detection=contracts.CurationDetection(state="never_run", trigger="live"),
            detector_coverage=[],
            result_digest="sha256:" + "6" * 64,
        )

    bind_mcp_daemon(monkeypatch, instances=["inst_mcp"])
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_curation_list", stub)

    result = handlers.handle_playbill_curation_list("inst_mcp", access_profile=None)

    assert result.items == []
    assert seen["instance_id"] == "inst_mcp"
    request = seen["request"]
    assert isinstance(request, dict)
    assert request["tag"] == "playbill-curation-list-request-v1"
    assert request["access_profile"] == {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "mcp-curation",
        "permitted_access_classes": ["instance", "public"],
        "disclose_restricted_existence": True,
    }
    assert "workspace_observation" not in request
    assert "evaluation_time" not in request
    assert request["limit"] == contracts.CURATION_LIST_DEFAULT_LIMIT
    assert request.get("cursor") is None


def test_curation_observe_refuses_a_raw_source_observation_with_a_typed_error() -> None:
    from cruxible_core.service.discovery.curation import (
        validate_playbill_curation_observe_request,
    )

    raw = {
        "tag": "playbill-next-workspace-observation-v1",
        "source_observations": [
            {
                "source_id": "corpus.runbook",
                "document_id": "runbook",
                "observed_source_digest": "sha256:" + "1" * 64,
            }
        ],
    }

    with pytest.raises(NextWorkspaceObservationInvalid):
        validate_playbill_curation_observe_request({"workspace_observation": raw})


def test_mcp_curation_lifecycle_actions_are_thin_delegates(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[str, dict[str, object]]] = []

    def stub(operation: str):  # type: ignore[no-untyped-def]
        def invoke(
            instance_id: str, *, request: dict[str, object]
        ) -> contracts.CurationActionResult:
            assert instance_id == "inst_mcp"
            seen.append((operation, request))
            return _action_result(str(request["item_id"]))

        return invoke

    bind_mcp_daemon(monkeypatch, instances=["inst_mcp"])
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_curation_overrule", stub("overrule")
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_curation_accept_fixed",
        stub("accept_fixed"),
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_curation_suppress", stub("suppress")
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_curation_unsuppress", stub("unsuppress")
    )
    item_id = "sha256:" + "1" * 64
    latest = "sha256:" + "2" * 64
    reason = "operator-reviewed mechanical facts"
    handlers.handle_playbill_curation_overrule(
        "inst_mcp",
        item_id=item_id,
        expected_latest_event_digest=latest,
        reason=reason,
        attribution_refs=[],
    )
    handlers.handle_playbill_curation_accept_fixed(
        "inst_mcp",
        item_id=item_id,
        expected_latest_event_digest=latest,
        reason=reason,
        accepted_proposal_id=None,
        accepted_changeset_digest=None,
        accepted_generation=4,
        attribution_refs=[],
    )
    handlers.handle_playbill_curation_suppress(
        "inst_mcp",
        item_id=item_id,
        expected_latest_event_digest=latest,
        reason=reason,
        scope="lineage",
        until_generation=9,
        attribution_refs=[],
    )
    handlers.handle_playbill_curation_unsuppress(
        "inst_mcp",
        item_id=item_id,
        expected_latest_event_digest=latest,
        reason=reason,
        suppression_event_id=None,
        attribution_refs=[],
    )

    assert [operation for operation, _request in seen] == [
        "overrule",
        "accept_fixed",
        "suppress",
        "unsuppress",
    ]
    assert seen[1][1]["accepted_generation"] == 4
    assert seen[2][1]["scope"] == "lineage"
