"""MCP registration, permission, and dispatch coverage for attestations."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

import asyncio

from cruxible_client import contracts
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.mcp.tool_prompts import tool_description
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode

ATTESTATION_TOOLS = {
    "cruxible_attest",
    "cruxible_list_attestations",
    "cruxible_attestation_queue",
    "cruxible_resolve_attestation",
}


def test_attestation_tools_are_registered_with_prompt_descriptions_and_schemas() -> None:
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    assert ATTESTATION_TOOLS <= set(tools)
    for name in ATTESTATION_TOOLS:
        assert tools[name].description == tool_description(name)
    stance = tools["cruxible_attest"].inputSchema["properties"]["stance"]
    assert stance["enum"] == ["support", "contradict", "unsure"]
    verdict = tools["cruxible_resolve_attestation"].inputSchema["properties"]["verdict"]
    assert verdict["enum"] == ["upheld", "corrected", "invalidated"]


def test_attestation_permission_map_matches_stage_d_tiers() -> None:
    assert TOOL_PERMISSIONS["cruxible_attest"] == PermissionMode.GOVERNED_WRITE
    assert TOOL_PERMISSIONS["cruxible_resolve_attestation"] == PermissionMode.GRAPH_WRITE
    assert TOOL_PERMISSIONS["cruxible_list_attestations"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_attestation_queue"] == PermissionMode.READ_ONLY


def test_attestation_handlers_dispatch_to_remote_client(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    envelope = contracts.ListResult(
        items=[],
        total=0,
        limit=100,
        offset=0,
        truncated=False,
        read_revision=3,
    )

    class StubClient:
        def attest(self, instance_id, **kwargs):
            calls.append(("attest", (instance_id, kwargs)))
            return contracts.AttestationRecordResult(attestation={})

        def list_attestations(self, instance_id, **kwargs):
            calls.append(("list", (instance_id, kwargs)))
            return envelope

        def attestation_queue(self, instance_id, **kwargs):
            calls.append(("queue", (instance_id, kwargs)))
            return envelope

        def resolve_attestation(self, instance_id, attestation_id, **kwargs):
            calls.append(("resolve", (instance_id, attestation_id, kwargs)))
            return contracts.AttestationDispositionResult(disposition={})

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    handlers.handle_attest(
        "inst-1",
        relationship_type="protected_by",
        from_type="Service",
        from_id="svc-1",
        to_type="Control",
        to_id="ctl-1",
        stance="support",
        observed_at="2026-07-24T11:00:00Z",
        evidence_refs=[],
    )
    handlers.handle_list_attestations("inst-1", stance="unsure")
    handlers.handle_attestation_queue("inst-1")
    handlers.handle_resolve_attestation("inst-1", "ATT-1", verdict="upheld")
    assert [name for name, _ in calls] == ["attest", "list", "queue", "resolve"]
