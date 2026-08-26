"""MCP curation list delegates one explicit observation unchanged."""

from __future__ import annotations

from cruxible_client import contracts
from cruxible_core.mcp import handlers


def test_mcp_curation_list_is_one_thin_read_delegate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}
    observation = {
        "tag": "playbill-next-workspace-observation-v1",
        "source_observations": [],
    }

    def stub(instance_id: str, *, request: dict[str, object]):  # type: ignore[no-untyped-def]
        seen["instance_id"] = instance_id
        seen["request"] = request
        return contracts.PlaybillCurationListResult(
            coordinate=contracts.PlaybillAcceptedCoordinate(
                git_oid="1" * 64,
                semantic_root="sha256:" + "2" * 64,
                generation_root="sha256:" + "3" * 64,
                compiler_digest="sha256:" + "4" * 64,
            ),
            generation=0,
            operational_head_digest="sha256:" + "5" * 64,
            items=[],
            observation_coverage={
                "tag": "playbill-curation-observation-coverage-v1",
                "source_count": 0,
                "observed_block_count": 0,
                "omitted_source_count": 0,
                "omissions": [],
            },
        )

    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_curation_list", stub)

    result = handlers.handle_playbill_curation_list(
        "inst",
        workspace_observation=observation,
    )

    assert result.items == []
    assert seen == {
        "instance_id": "inst",
        "request": {
            "tag": "playbill-curation-list-request-v1",
            "workspace_observation": observation,
        },
    }
