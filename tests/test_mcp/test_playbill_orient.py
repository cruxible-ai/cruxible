"""MCP orient: typed parameters, daemon dispatch, tool-call rendering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client import contracts
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _answer() -> contracts.OrientResult:
    return contracts.OrientResult(
        instance="inst",
        coordinate=AcceptedCoordinate.model_validate(COORDINATE.model_dump(mode="json")),
        generation=4,
        accepted_at=datetime(2026, 9, 1, tzinfo=UTC),
        evaluation_time=datetime(2026, 9, 2, tzinfo=UTC),
        kinds=(),
        next=("cruxible_next()",),
    )


def test_mcp_orient_renders_for_mcp_and_forwards_its_inputs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, Any] = {}

    def orient_stub(instance_id: str, **values: Any) -> contracts.OrientResult:
        seen.update(values, instance_id=instance_id)
        return _answer()

    monkeypatch.setattr(handlers, "_get_client", lambda: SimpleNamespace(orient=orient_stub))

    result = handlers.handle_playbill_orient(
        "inst",
        section="queries",
        limit=5,
        at=COORDINATE,
        evaluation_time="2026-09-02T00:00:00Z",
    )

    assert result.generation == 4
    assert seen["instance_id"] == "inst"
    assert seen["surface"] == "mcp"
    assert seen["section"] == "queries" and seen["limit"] == 5
    assert seen["at"] == COORDINATE
    assert seen["evaluation_time"] == "2026-09-02T00:00:00Z"

    handlers.handle_playbill_orient("inst", at="1" * 64)
    assert seen["at"] == "1" * 64


def test_orient_tool_declares_every_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "default")
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

    schema = tools["cruxible_orient"].inputSchema

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
        "principals",
        "policies",
    ]
    # `at` is a git OID (or unique prefix) or a generation number, never a free-form dict.
    assert {member.get("type") for member in schema["properties"]["at"]["anyOf"]} == {
        "string",
        "integer",
        "null",
    }


@pytest.mark.parametrize("tools", [(), ("cruxible_orient", "cruxible_prediction_settle")])
def test_mcp_orient_forwards_the_advertised_tools(
    monkeypatch: pytest.MonkeyPatch, tools: tuple[str, ...]
) -> None:
    seen: dict[str, Any] = {}

    def orient_stub(instance_id: str, **values: Any) -> contracts.OrientResult:
        seen.update(values)
        return _answer()

    monkeypatch.setattr(handlers, "_get_client", lambda: SimpleNamespace(orient=orient_stub))
    monkeypatch.setattr("cruxible_core.mcp.curation.session_tool_names", lambda: set(tools))
    handlers.handle_playbill_orient("inst")
    assert seen["caller_tools"] == tuple(sorted(tools))
    assert seen["surface"] == "mcp"


def test_mcp_orient_reports_the_mcp_workspace_floor(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    floor = tmp_path / ".cruxible/floor"
    floor.mkdir(parents=True)
    (floor / "manifest.json").write_text(
        json.dumps({"coordinate": {"git_oid": "9" * 64}, "generation": 1}), encoding="utf-8"
    )
    monkeypatch.setattr(
        handlers, "_get_client", lambda: SimpleNamespace(orient=lambda *_a, **_k: _answer())
    )
    monkeypatch.setattr(handlers, "optional_mcp_git_workspace_root", lambda: tmp_path)

    result = handlers.handle_playbill_orient("inst")

    assert result.floor == contracts.OrientFloor(at="9" * 64, generations_behind=3)
