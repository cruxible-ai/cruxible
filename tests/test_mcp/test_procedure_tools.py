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
    "cruxible_withdraw_procedure",
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
    assert status_values == ["pending", "live", "rejected", "retired", "withdrawn"]
    action = tools["cruxible_resolve_procedure"].inputSchema["properties"]["action"]
    assert action["enum"] == ["accept", "reject"]
    # Withdraw is its own verb, not a resolve action: it is the author's own
    # retraction rather than a reviewer verdict, so its reason stays optional.
    withdraw_schema = tools["cruxible_withdraw_procedure"].inputSchema
    assert set(withdraw_schema["required"]) == {
        "instance_id",
        "procedure_id",
        "expected_version",
    }
    assert "reason" in withdraw_schema["properties"]
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
    assert "track records" in tools["cruxible_list_procedures"].description
    assert "track record" in tools["cruxible_get_procedure"].description


def test_procedure_permission_map_matches_stage_c_tiers() -> None:
    assert TOOL_PERMISSIONS["cruxible_propose_procedure"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_run_procedure"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_resolve_procedure"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_retire_procedure"] == PermissionMode.GRAPH_WRITE
    # Withdrawing YOUR OWN proposal is the retraction half of proposing it, so
    # it sits at the proposing tier; the service refuses a non-author below
    # GRAPH_WRITE inside the receipted transition.
    assert TOOL_PERMISSIONS["cruxible_withdraw_procedure"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_list_procedures"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_get_procedure"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_list_procedure_runs"] == PermissionMode.READ_ONLY


def test_procedure_handlers_dispatch_to_remote_client(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    envelope = contracts.ListResult(
        items=[
            {
                "procedure_id": "PRC-1",
                "track_record": {
                    "runs": 8,
                    "succeeded": 0,
                    "failed": 0,
                    "refused": 8,
                    "last_succeeded_at": None,
                    "top_refusal_reason": None,
                    "linked_outcomes": None,
                },
            }
        ],
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
            return {"procedure": envelope.items[0]}

        def resolve_procedure(self, instance_id, procedure_id, **kwargs):
            calls.append(("resolve", (instance_id, procedure_id, kwargs)))
            return {"action": kwargs["action"], "procedure": {}, "receipt_id": "RCP-2"}

        def withdraw_procedure(self, instance_id, procedure_id, **kwargs):
            calls.append(("withdraw", (instance_id, procedure_id, kwargs)))
            return {"action": "withdraw", "procedure": {}, "receipt_id": "RCP-4"}

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
    listed = handlers.handle_list_procedures("inst_1", status="live")
    shown = handlers.handle_get_procedure("inst_1", "PRC-1")
    handlers.handle_resolve_procedure(
        "inst_1",
        "PRC-1",
        action="accept",
        expected_version=1,
    )
    handlers.handle_withdraw_procedure("inst_1", "PRC-1", expected_version=1)
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
        "withdraw",
        "retire",
        "run",
        "runs",
    ]
    assert listed.items[0]["track_record"]["refused"] == 8
    assert shown["procedure"]["track_record"]["linked_outcomes"] is None
    withdraw_call = next(payload for name, payload in calls if name == "withdraw")
    assert withdraw_call[2] == {"expected_version": 1, "reason": None}
