"""MCP orient: typed parameters, dual-mode dispatch, tool-call rendering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from cruxible_client import contracts
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _answer() -> contracts.PlaybillOrientResultV1:
    return contracts.PlaybillOrientResultV1(
        instance="inst",
        coordinate=AcceptedCoordinate.model_validate(COORDINATE.model_dump(mode="json")),
        generation=4,
        accepted_at=datetime(2026, 9, 1, tzinfo=UTC),
        evaluation_time=datetime(2026, 9, 2, tzinfo=UTC),
        kinds=(),
        next=("cruxible_playbill_next()",),
    )


def test_local_mcp_orient_renders_for_mcp_and_passes_typed_inputs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, Any] = {}

    def orient_stub(instance_id: str, **values: Any) -> contracts.PlaybillOrientResultV1:
        seen.update(values, instance_id=instance_id)
        return _answer()

    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_orient", orient_stub)

    result = handlers.handle_playbill_orient(
        "inst",
        section="queries",
        limit=5,
        at=COORDINATE,
        evaluation_time="2026-09-02T00:00:00Z",
    )

    assert result.generation == 4
    assert seen["surface"] == "mcp"
    assert seen["section"] == "queries" and seen["limit"] == 5
    assert seen["at"] == AcceptedCoordinate.model_validate(COORDINATE.model_dump(mode="json"))
    assert seen["evaluation_time"] == datetime(2026, 9, 2, tzinfo=UTC)

    handlers.handle_playbill_orient("inst", at="1" * 64)
    assert seen["at"] == "1" * 64


def test_orient_tool_declares_every_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "default")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    schema = tools["cruxible_playbill_orient"].inputSchema

    assert set(schema["properties"]) == {
        "instance_id",
        "kind",
        "section",
        "limit",
        "cursor",
        "at",
        "evaluation_time",
    }
    assert schema.get("required", []) == []
    section = schema["properties"]["section"]["anyOf"][0]
    assert section["enum"] == [
        "documents",
        "procedures",
        "claim_types",
        "queries",
        "interfaces",
        "runs",
        "running",
        "lines",
        "captures",
        "capture_contracts",
        "predictions",
        "mandates",
    ]
    # `at` is a Git OID string or a declared coordinate object, never a free-form dict.
    coordinate = schema["$defs"]["PlaybillAcceptedCoordinate"]
    assert coordinate["additionalProperties"] is False
    assert set(coordinate["properties"]) >= {"git_oid", "semantic_root"}


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("tools", [(), ("cruxible_playbill_orient", "cruxible_playbill_settle")])
def test_mcp_orient_forwards_the_advertised_tools(
    monkeypatch: pytest.MonkeyPatch, remote: bool, tools: tuple[str, ...]
) -> None:
    seen: dict[str, Any] = {}

    def orient_stub(instance_id: str, **values: Any) -> contracts.PlaybillOrientResultV1:
        seen.update(values)
        return _answer()

    from types import SimpleNamespace

    monkeypatch.setattr(
        handlers,
        "_get_client",
        lambda: SimpleNamespace(orient_playbill=orient_stub) if remote else None,
    )
    monkeypatch.setattr(handlers.playbill_api, "playbill_orient", orient_stub)
    monkeypatch.setattr("cruxible_core.mcp.curation.session_tool_names", lambda: set(tools))
    handlers.handle_playbill_orient("inst")
    assert seen["caller_tools"] == tuple(sorted(tools))
    assert seen["surface"] == "mcp"
