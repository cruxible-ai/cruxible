"""MCP registration, schema, dispatch, and permission coverage for procedures."""

from __future__ import annotations

import asyncio

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode

PROCEDURE_TOOLS = {
    "cruxible_propose_procedure",
    "cruxible_list_procedures",
    "cruxible_get_procedure",
    "cruxible_resolve_procedure",
    "cruxible_retire_procedure",
    "cruxible_run_procedure",
    "cruxible_list_procedure_runs",
}


def test_procedure_tools_are_registered_once_with_expected_schemas() -> None:
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    assert PROCEDURE_TOOLS <= set(tools)
    assert [name for name in tools if name == "cruxible_run_procedure"] == [
        "cruxible_run_procedure"
    ]
    status = tools["cruxible_list_procedures"].inputSchema["properties"]["status"]
    status_values = next(item["enum"] for item in status["anyOf"] if "enum" in item)
    assert status_values == ["pending", "live", "rejected", "retired"]
    action = tools["cruxible_resolve_procedure"].inputSchema["properties"]["action"]
    assert action["enum"] == ["promote", "reject"]
    assert set(tools["cruxible_list_procedures"].outputSchema["properties"]) == {
        "items",
        "total",
        "limit",
        "offset",
        "truncated",
        "read_revision",
        "continuation_token",
    }
    assert set(tools["cruxible_list_procedure_runs"].outputSchema["properties"]) == {
        "items",
        "total",
        "limit",
        "offset",
        "truncated",
        "read_revision",
        "continuation_token",
    }


def test_procedure_permission_map_matches_stage_c_tiers() -> None:
    assert TOOL_PERMISSIONS["cruxible_propose_procedure"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_run_procedure"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_resolve_procedure"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_retire_procedure"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_list_procedures"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_get_procedure"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_list_procedure_runs"] == PermissionMode.READ_ONLY


def test_procedure_handlers_dispatch_to_remote_client(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    envelope = contracts.ListResult(
        items=[],
        total=0,
        limit=100,
        offset=0,
        truncated=False,
        read_revision=7,
    )

    class StubClient:
        def propose_procedure(self, instance_id, **kwargs):
            calls.append(("propose", (instance_id, kwargs)))
            return {"action": "propose", "procedure": {}, "receipt_id": "RCP-1"}

        def list_procedures(self, instance_id, **kwargs):
            calls.append(("list", (instance_id, kwargs)))
            return envelope

        def get_procedure(self, instance_id, procedure_id):
            calls.append(("get", (instance_id, procedure_id)))
            return {"procedure": {"procedure_id": procedure_id}}

        def resolve_procedure(self, instance_id, procedure_id, **kwargs):
            calls.append(("resolve", (instance_id, procedure_id, kwargs)))
            return {"action": kwargs["action"], "procedure": {}, "receipt_id": "RCP-2"}

        def retire_procedure(self, instance_id, procedure_id, **kwargs):
            calls.append(("retire", (instance_id, procedure_id, kwargs)))
            return {"action": "retire", "procedure": {}, "receipt_id": "RCP-3"}

        def run_procedure(self, instance_id, procedure_id, **kwargs):
            calls.append(("run", (instance_id, procedure_id, kwargs)))
            return {"procedure": {}, "run": {}, "output": {}, "receipt": {}}

        def list_procedure_runs(self, instance_id, procedure_id, **kwargs):
            calls.append(("runs", (instance_id, procedure_id, kwargs)))
            return envelope

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    handlers.handle_propose_procedure("inst_1", {"name": "p"})
    handlers.handle_list_procedures("inst_1", status="live")
    handlers.handle_get_procedure("inst_1", "PRC-1")
    handlers.handle_resolve_procedure(
        "inst_1",
        "PRC-1",
        action="promote",
        expected_version=1,
    )
    handlers.handle_retire_procedure(
        "inst_1",
        "PRC-1",
        expected_version=2,
        reason="obsolete",
    )
    handlers.handle_run_procedure("inst_1", "PRC-1", input_payload={"value": 1})
    handlers.handle_list_procedure_runs("inst_1", "PRC-1")

    assert [name for name, _ in calls] == [
        "propose",
        "list",
        "get",
        "resolve",
        "retire",
        "run",
        "runs",
    ]
