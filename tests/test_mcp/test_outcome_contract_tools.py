"""MCP registration, permission, and dispatch coverage for outcome contracts."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

import asyncio

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.mcp.tool_prompts import tool_description
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode

OUTCOME_TOOLS = {
    "cruxible_open_outcome_contract",
    "cruxible_resolve_outcome",
    "cruxible_list_outcome_contracts",
    "cruxible_outcome_due",
    "cruxible_dispose_outcome_resolution",
}


def test_outcome_tools_are_registered_with_prompt_descriptions_and_schemas() -> None:
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    assert OUTCOME_TOOLS <= set(tools)
    for name in OUTCOME_TOOLS:
        assert tools[name].description == tool_description(name)
    verdict = tools["cruxible_resolve_outcome"].inputSchema["properties"]["verdict"]
    assert verdict["enum"] == ["satisfied", "contradicted", "indeterminate"]
    queue = tools["cruxible_outcome_due"].inputSchema["properties"]["queue"]
    assert queue["enum"] == ["due", "overdue", "contradicted"]
    disposition = tools["cruxible_dispose_outcome_resolution"].inputSchema["properties"]["verdict"]
    assert disposition["enum"] == ["upheld", "overturned"]


def test_outcome_permission_map_matches_the_declared_tiers() -> None:
    assert TOOL_PERMISSIONS["cruxible_open_outcome_contract"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_resolve_outcome"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_dispose_outcome_resolution"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_list_outcome_contracts"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_outcome_due"] == PermissionMode.READ_ONLY


def test_outcome_handlers_dispatch_to_remote_client(monkeypatch) -> None:
    calls: list[str] = []
    envelope = contracts.ListResult(
        items=[],
        total=0,
        limit=100,
        offset=0,
        truncated=False,
        read_revision=3,
    )

    class StubClient:
        def open_outcome_contract(self, instance_id, **kwargs):
            calls.append("open")
            return contracts.OutcomeContractResult(contract={})

        def resolve_outcome(self, instance_id, contract_id, **kwargs):
            calls.append("resolve")
            return contracts.OutcomeResolutionResult(resolution={})

        def dispose_outcome_resolution(self, instance_id, resolution_id, **kwargs):
            calls.append("dispose")
            return contracts.OutcomeDispositionResult(disposition={})

        def list_outcome_contracts(self, instance_id, **kwargs):
            calls.append("list")
            return envelope

        def outcome_due(self, instance_id, **kwargs):
            calls.append("due")
            return envelope

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    handlers.handle_open_outcome_contract(
        "inst-1",
        entity_type="Decision",
        entity_id="dd-1",
        description="stays healthy",
        check_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        measurement={"kind": "query", "query_name": "q", "expect": {"min_count": 1}},
    )
    handlers.handle_resolve_outcome(
        "inst-1",
        "RSC-1",
        verdict="indeterminate",
        observed_at="2026-08-02T00:00:00Z",
    )
    handlers.handle_dispose_outcome_resolution("inst-1", "RSR-1", verdict="overturned")
    handlers.handle_list_outcome_contracts("inst-1", entity_type="Decision")
    handlers.handle_outcome_due("inst-1", queue="overdue")
    assert calls == ["open", "resolve", "dispose", "list", "due"]
