"""The MCP claim values tool reaches the same read as the SDK's World.values."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cruxible_client.contracts.claim_reads import ClaimValuesRequestV1
from cruxible_core.errors import ConfigError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import PERMISSION_REQUIREMENTS, PermissionMode


def test_claim_values_is_a_read_only_tool_with_a_kind_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    schema = tools["cruxible_playbill_claim_values"].inputSchema
    assert set(schema["required"]) == {"subject_kind", "predicates"}
    assert {"subject_ids", "evaluation_time"} <= set(schema["properties"])
    assert PERMISSION_REQUIREMENTS["cruxible_playbill_claim_values"] is PermissionMode.READ_ONLY


def test_claim_values_handler_builds_the_kind_or_path_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ClaimValuesRequestV1] = []

    def local(instance_id: str, *, request: ClaimValuesRequestV1) -> Any:
        assert instance_id == "inst_values"
        captured.append(request)
        return "result"

    monkeypatch.setattr(handlers.playbill_api, "playbill_read_claim_values", local)
    monkeypatch.setattr(
        handlers, "_dispatch_remote_or_local", lambda _remote, local, **_kw: local()
    )

    handlers.handle_playbill_claim_values(
        "inst_values", subject_kind="project.work_item", predicates=["project.work_item.status"]
    )
    handlers.handle_playbill_claim_values(
        "inst_values",
        subject_kind="project.work_item",
        predicates=["project.work_item.status"],
        subject_ids=["wi-42"],
        evaluation_time="2026-09-01T00:00:00Z",
    )

    assert captured[0].subject_kind == "project.work_item"
    assert captured[1].subject_paths == ("subjects/project.work_item/wi-42.json",)
    assert captured[1].evaluation_time is not None
    with pytest.raises(ConfigError, match="Invalid claim values selection"):
        handlers.handle_playbill_claim_values(
            "inst_values", subject_kind="Not A Kind", predicates=[], subject_ids=["wi-42"]
        )
