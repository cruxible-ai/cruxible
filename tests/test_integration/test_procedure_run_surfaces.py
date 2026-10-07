"""Procedure-run reads on every surface: ``get`` cards and ``orient(section="runs")``.

The CLI, the MCP tool and the SDK each reach the real services over one world
holding a finished run and one left running; HTTP serves the same section and
refusal on a fresh instance (``tests/test_server/test_playbill_operational_get_route.py``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from click.testing import CliRunner

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.authoring.sdk_types import RefKind
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.operational_reads import GetProcedureRunCard
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_head, service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run
from tests.test_service.test_procedure_run_reads import run_world  # noqa: F401

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


class _ServiceClient:
    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.surfaces: list[str] = []

    def get(self, instance_id: str, *, request: GetRequest) -> Any:
        self.surfaces.append(request.surface)
        return service_playbill_get(self.instance, request=request, access=_ACCESS)

    def head(self, instance_id: str, **_values: Any) -> Any:
        return service_playbill_head(self.instance)

    def orient(self, instance_id: str, **values: Any) -> Any:
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

    listed = CliRunner().invoke(cli, [*prefix, "orient", "--section", "runs"])
    assert listed.exit_code == 0, listed.output
    assert finished.run_id in listed.output and "running" in listed.output
    assert "cruxible get ProcedureRun:" in listed.output

    read = CliRunner().invoke(cli, [*prefix, "get", finished.run_id, "--json"])
    assert read.exit_code == 0, read.output
    body = json.loads(read.output)
    assert body["kind"] == "procedure_run" and body["card"]["status"] == "succeeded"
    assert body["card"]["receipt_digest"] == finished.receipt_digest
    assert set(client.surfaces) == {"cli"}


def test_the_mcp_tools_read_runs(run_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _procedure, finished = run_world
    client = _ServiceClient(instance)
    monkeypatch.setattr(handlers, "_get_client", lambda: client)
    server = create_server()

    async def exercise() -> tuple[str, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            orient = await session.call_tool(
                "cruxible_orient", {"instance_id": "inst", "section": "runs"}
            )
            got = await session.call_tool(
                "cruxible_get",
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
    # A connection opened without a workspace: reads serve, and orient reports
    # no floor rather than reading one.
    playbill = Cruxible._from_client(  # type: ignore[arg-type]
        _ServiceClient(instance),
        instance_id="inst",
        workspace=None,
        clock=lambda: datetime(2026, 8, 24, 16, 5, tzinfo=UTC),
    )

    card = playbill.get(f"ProcedureRun:{finished.run_id}")

    assert card.kind is RefKind.PROCEDURE_RUN and card.identity == finished.run_id
    assert isinstance(card.value, GetProcedureRunCard)
    assert card.value.next[-1] == f'cx.get("ProcedureRun:{finished.run_id}", detail="proof")'
    runs = playbill.orient(section="runs")
    assert runs.runs is not None and len(runs.runs) == 2
    assert runs.floor is None
