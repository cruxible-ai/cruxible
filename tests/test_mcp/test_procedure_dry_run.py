"""MCP schema and dispatch coverage for procedure dry-runs."""

from __future__ import annotations

import asyncio

from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server


def test_run_procedure_schema_exposes_dry_run() -> None:
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    schema = tools["cruxible_run_procedure"].inputSchema
    assert schema["properties"]["dry_run"] == {
        "default": False,
        "title": "Dry Run",
        "type": "boolean",
    }
    assert "dry_run" not in schema["required"]


def test_run_procedure_handler_forwards_dry_run(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def run_procedure(self, instance_id: str, procedure_id: str, **kwargs):
            captured.update({"instance_id": instance_id, "procedure_id": procedure_id, **kwargs})
            return {"dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    result = handlers.handle_run_procedure(
        "inst_1",
        "PRC-1",
        input_payload={"value": 1},
        dry_run=True,
    )

    assert result == {"dry_run": True}
    assert captured == {
        "instance_id": "inst_1",
        "procedure_id": "PRC-1",
        "input_payload": {"value": 1},
        "dry_run": True,
    }
