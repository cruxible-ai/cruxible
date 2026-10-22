"""cruxible_capture_erase: full profile, graph_write, and the daemon's typed request."""

import asyncio
from types import SimpleNamespace

from cruxible_client.contracts.capture_erasure import CaptureEraseRequest
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode, reset_permissions


def test_the_tool_takes_the_capture_the_reason_and_change_control(monkeypatch):
    reset_permissions()
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv("CRUXIBLE_MODE", "graph_write")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    tool = tools["cruxible_capture_erase"]
    assert set(tool.inputSchema["required"]) == {"capture", "because"}
    assert {"dry_run", "at"} <= set(tool.inputSchema["properties"])
    assert TOOL_PERMISSIONS[tool.name] == PermissionMode.GRAPH_WRITE
    assert tool.outputSchema is not None
    reset_permissions()


def test_the_tool_is_absent_below_graph_write_and_from_the_default_profile(monkeypatch):
    for mode, profile in (("governed_write", "full"), ("admin", "default")):
        reset_permissions()
        monkeypatch.setenv("CRUXIBLE_MODE", mode)
        monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)
        names = {tool.name for tool in asyncio.run(create_server().list_tools())}
        assert "cruxible_capture_erase" not in names
    reset_permissions()


def test_the_handler_sends_the_typed_request(monkeypatch):
    captured = {}
    request = CaptureEraseRequest(capture_digest="CAP-0123456789ab", because="Rotated.")
    sentinel = object()

    def erase(instance_id, sent):
        captured.update(instance=instance_id, request=sent)
        return sentinel

    def call(fn, *, operation_name):
        captured["operation"] = operation_name
        return fn(SimpleNamespace(erase_capture=erase))

    monkeypatch.setattr(handlers, "_daemon_call", call)
    assert handlers.handle_playbill_erase_capture("inst", request) is sentinel
    assert captured == {
        "instance": "inst",
        "request": request,
        "operation": "cruxible_capture_erase",
    }
