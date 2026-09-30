"""Procedure-run reads on every surface: ``get`` cards and ``orient(section="runs")``.

The CLI, the MCP tool and the SDK each reach the real services over one world
holding a finished run and one left running; HTTP serves the same section and
refusal on a fresh instance (``tests/test_server/test_playbill_operational_get_route.py``).
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from cruxible_client.authoring.sdk import Playbill
from cruxible_client.authoring.sdk_types import RefKind
from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_client.contracts.operational_reads import PlaybillGetProcedureRunCardV1
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run
from tests.test_service.test_procedure_run_reads import run_world  # noqa: F401

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


class _ServiceClient:
    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.surfaces: list[str] = []

    def playbill_get(self, instance_id: str, *, request: PlaybillGetRequestV1) -> Any:
        self.surfaces.append(request.surface)
        return service_playbill_get(self.instance, request=request, access=_ACCESS)

    def orient_playbill(self, instance_id: str, **values: Any) -> Any:
        from datetime import datetime

        self.surfaces.append(values["surface"])
        values.pop("caller_tools", None)
        if isinstance(values.get("evaluation_time"), str):
            values["evaluation_time"] = datetime.fromisoformat(values["evaluation_time"])
        return service_playbill_orient(self.instance, **values)


def test_the_cli_lists_runs_and_reads_one(run_world, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _procedure, finished = run_world
    client = _ServiceClient(instance)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    prefix = ["--server-url", "http://server", "--instance-id", "inst"]

    listed = CliRunner().invoke(cli, [*prefix, "playbill", "orient", "--section", "runs"])
    assert listed.exit_code == 0, listed.output
    assert finished.run_id in listed.output and "running" in listed.output
    assert "cruxible playbill get ProcedureRun:" in listed.output

    read = CliRunner().invoke(cli, [*prefix, "playbill", "get", finished.run_id, "--json"])
    assert read.exit_code == 0, read.output
    body = json.loads(read.output)
    assert body["kind"] == "procedure_run" and body["card"]["status"] == "succeeded"
    assert body["card"]["receipt_digest"] == finished.receipt_digest
    assert set(client.surfaces) == {"cli"}


def test_the_mcp_tools_read_runs(run_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _procedure, finished = run_world
    client = _ServiceClient(instance)
    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr(handlers.playbill_api, "playbill_get", client.playbill_get)
    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_orient",
        lambda instance_id, **values: client.orient_playbill(instance_id, **values),
    )
    server = create_server()

    async def exercise() -> tuple[str, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            orient = await session.call_tool(
                "cruxible_playbill_orient", {"instance_id": "inst", "section": "runs"}
            )
            got = await session.call_tool(
                "cruxible_playbill_get",
                {"instance_id": "inst", "ref": f"ProcedureRun:{finished.run_id}"},
            )
            assert not orient.isError and not got.isError
            return (
                " ".join(block.text for block in orient.content if hasattr(block, "text")),
                " ".join(block.text for block in got.content if hasattr(block, "text")),
            )

    orient_text, get_text = _run(exercise())

    runs = json.loads(orient_text)["runs"]
    assert [row["status"] for row in runs] == ["running", "succeeded"]
    card = json.loads(get_text)["card"]
    assert card["run"] == finished.run_id and card["nodes_done"] == card["nodes_total"]
    assert set(client.surfaces) == {"mcp"}


def test_the_sdk_reads_a_run_card(run_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _procedure, finished = run_world
    playbill = Playbill.__new__(Playbill)
    playbill._client = _ServiceClient(instance)  # type: ignore[assignment]
    playbill._instance_id = "inst"
    playbill._read_at = lambda coordinate=None: None  # type: ignore[method-assign,assignment]
    playbill._evaluation_time = lambda: "2026-08-24T16:05:00+00:00"  # type: ignore[method-assign]
    playbill._observe_read = lambda *_a, **_k: None  # type: ignore[method-assign]

    card = playbill.get(f"ProcedureRun:{finished.run_id}")

    assert card.kind is RefKind.PROCEDURE_RUN and card.identity == finished.run_id
    assert isinstance(card.value, PlaybillGetProcedureRunCardV1)
    assert card.value.next[-1] == f'pb.get("ProcedureRun:{finished.run_id}", detail="proof")'
    runs = playbill.orient(section="runs")
    assert runs.runs is not None and len(runs.runs) == 2
