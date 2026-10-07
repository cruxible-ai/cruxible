"""MCP curation list delegates one explicit observation unchanged."""

from __future__ import annotations

import pytest

from cruxible_client import contracts
from cruxible_client.errors import CoreError
from cruxible_core.mcp import handlers
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
    observation = {
        "tag": "playbill-next-workspace-observation-v1",
        "source_observations": [],
    }

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
            evaluation_time="2026-08-26T16:00:00+00:00",
            operational_head_digest="sha256:" + "5" * 64,
            items=[],
            detector_coverage=[],
            observation_coverage={
                "tag": "playbill-curation-observation-coverage-v1",
                "source_count": 0,
                "observed_block_count": 0,
                "omitted_source_count": 0,
                "omissions": [],
            },
            result_digest="sha256:" + "6" * 64,
        )

    bind_mcp_daemon(monkeypatch, instances=["inst_mcp"])
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_curation_list", stub)

    result = handlers.handle_playbill_curation_list(
        "inst_mcp",
        evaluation_time="2026-08-26T16:00:00+00:00",
        access_profile=None,
        workspace_observation=observation,
    )

    assert result.items == []
    assert seen["instance_id"] == "inst_mcp"
    request = seen["request"]
    assert isinstance(request, dict)
    assert request["tag"] == "playbill-curation-list-request-v1"
    assert request["evaluation_time"] == "2026-08-26T16:00:00Z"
    assert request["access_profile"] == {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "mcp-curation",
        "permitted_access_classes": ["instance", "public"],
        "disclose_restricted_existence": True,
    }
    assert request["workspace_observation"] == observation
    assert request["limit"] == contracts.CURATION_LIST_DEFAULT_LIMIT
    assert request.get("cursor") is None


def test_mcp_curation_list_rejects_raw_source_observation_with_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind_mcp_daemon(monkeypatch, instances=["inst_mcp"])
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

    with pytest.raises(CoreError, match="cruxible.next.workspace_observation_invalid"):
        handlers.handle_playbill_curation_list(
            "inst_mcp",
            evaluation_time="2026-08-26T16:00:00+00:00",
            access_profile=None,
            workspace_observation=raw,
        )


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
        accepted_proposal_id="sha256:" + "3" * 64,
        accepted_changeset_digest="sha256:" + "4" * 64,
        attribution_refs=[],
    )
    handlers.handle_playbill_curation_suppress(
        "inst_mcp",
        item_id=item_id,
        expected_latest_event_digest=latest,
        reason=reason,
        scope="pattern",
        until_generation=9,
        attribution_refs=[],
    )

    assert [operation for operation, _request in seen] == [
        "overrule",
        "accept_fixed",
        "suppress",
    ]
    assert seen[2][1]["scope"] == "pattern"
