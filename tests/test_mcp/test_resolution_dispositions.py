"""Resolution review is typed, governed, and delegates to the daemon SDK."""

import asyncio
from types import SimpleNamespace

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode


def test_resolution_disposition_tool_schema_and_governed_tier(monkeypatch):
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    tool = tools["cruxible_procedure_dispose"]
    assert set(tool.inputSchema["required"]) == {"name", "request"}
    request = tool.inputSchema["$defs"]["ProcedureResolutionDispositionRequest"]
    assert set(request["required"]) == {"resolution_id", "verdict", "reason"}
    assert request["properties"]["verdict"]["enum"] == ["upheld", "overturned"]
    assert TOOL_PERMISSIONS[tool.name] == PermissionMode.GOVERNED_WRITE
    assert tool.outputSchema is not None


def test_resolution_disposition_handler_uses_the_typed_transport(monkeypatch):
    captured = {}
    request = contracts.ProcedureResolutionDispositionRequest(
        resolution_id="RSR-" + "a" * 32, verdict="overturned", reason="Evidence differs."
    )
    sentinel = object()

    def dispose(instance_id, name, *, request):
        captured.update(instance=instance_id, name=name, request=request)
        return sentinel

    def call(fn, *, operation_name):
        captured["operation"] = operation_name
        return fn(SimpleNamespace(dispose_procedure_resolution=dispose))

    monkeypatch.setattr(handlers, "_daemon_call", call)
    assert handlers.handle_playbill_procedure_dispose("inst", "triage", request) is sentinel
    assert captured == {
        "instance": "inst",
        "name": "triage",
        "request": request,
        "operation": "cruxible_procedure_dispose",
    }


def test_review_hidden_in_read_only_and_default_profiles(monkeypatch):
    for mode, profile in (("read_only", "full"), ("admin", "default")):
        monkeypatch.setenv("CRUXIBLE_MODE", mode)
        monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)
        names = {tool.name for tool in asyncio.run(create_server().list_tools())}
        assert "cruxible_procedure_dispose" not in names


def test_runtime_review_requires_authenticated_attribution(monkeypatch):
    import pytest

    from cruxible_core.errors import AuthenticationError
    from cruxible_core.runtime import playbill_api

    calls = []
    monkeypatch.setattr(playbill_api, "check_permission", lambda tool, **kwargs: calls.append(tool))
    monkeypatch.setattr(playbill_api, "_write_actor_context", lambda instance_id: None)
    with pytest.raises(AuthenticationError):
        playbill_api.playbill_procedure_dispose(
            "inst",
            "triage",
            request=contracts.ProcedureResolutionDispositionRequest(
                resolution_id="RSR-" + "a" * 32, verdict="upheld", reason="Reviewed."
            ),
        )
    assert calls == ["cruxible_procedure_dispose"]
