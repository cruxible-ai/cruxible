"""MCP adapter laws for the unified search verb."""

from __future__ import annotations

from cruxible_client import contracts
from cruxible_core.mcp import handlers


def test_mcp_search_sorts_filters_and_keeps_access_server_owned(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    def search_stub(instance_id: str, **values: object) -> contracts.PlaybillSearchResult:
        seen.update(values)
        return contracts.PlaybillSearchResult(
            mode="list",
            coordinate=contracts.PlaybillAcceptedCoordinate(
                git_oid="1" * 64,
                semantic_root="sha256:" + "2" * 64,
                generation_root="sha256:" + "3" * 64,
                compiler_digest="sha256:" + "4" * 64,
            ),
            evaluation_time="2026-08-21T14:00:00.000000Z",
            rows=[],
            selection_basis_digest="sha256:" + "5" * 64,
            truncated=False,
            result_digest="sha256:" + "6" * 64,
        )

    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_search", search_stub)
    result = handlers.handle_playbill_search(
        "inst",
        mode="list",
        query=None,
        kinds=["procedure", "claim", "claim"],
        subject=None,
        statuses=["retired", "accepted"],
        cursor=None,
        evaluation_time="2026-08-21T14:00:00Z",
        budgets=None,
    )

    assert result.mode == "list"
    assert seen["kinds"] == ("claim", "procedure")
    assert seen["statuses"] == ("accepted", "retired")
