"""An instance-scoped caller sees its Line arms stall or stop, without daemon scope.

Audit item 7: the consumer registry answered only daemon-scope callers
(``server_info``), and orient accepted ``consumers_running`` and
``provider_lane`` and ignored them. The Line consumer's health is instance
state, so ``next``'s consumers facet counts the arms by state, orient's
attention names the stopped or stalled Lines, and orient says when nothing on
this host runs the consumer loop or the provider lane is down.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.procedures.line_dispatch import service_stop_line_arm
from tests.test_procedures.test_line_arming import _armed_world
from tests.test_procedures.test_line_dispatch import _active_segment
from tests.test_procedures.test_procedure_run_surface import _actor

_PROFILE = {"profile_id": "arms", "permitted_access_classes": ["instance", "public"]}


@pytest.fixture
def stopped(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, line, _procedure, start = _armed_world(tmp_path)
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="credential_revoked",
        detail="revoked",
        actor=_actor(instance),
        now=start + timedelta(seconds=1),
    )
    return instance, line, start + timedelta(seconds=2)


def test_next_counts_line_arms_in_the_consumers_facet(stopped) -> None:  # type: ignore[no-untyped-def]
    instance, _line, when = stopped

    result = service_playbill_next(
        instance,
        request=NextRequest(evaluation_time=when, access_profile=_PROFILE),
        caller_rung=1,
    )

    assert result.status.consumers.detail["line_enablements"] == {
        "running": 0,
        "stalled": 0,
        "stopped": 1,
    }


def test_orient_attention_names_a_stopped_arm_for_an_instance_caller(stopped) -> None:  # type: ignore[no-untyped-def]
    instance, line, when = stopped

    answer = service_playbill_orient(instance, evaluation_time=when, caller_rung=1)

    assert answer.attention is not None and answer.attention.enablements is not None
    arms = answer.attention.enablements
    assert (arms.running, arms.stalled, arms.stopped) == (0, 0, 1)
    assert arms.needs_attention == (f"{line.identity.qualified} stopped (credential_revoked)",)
    # Nothing is armed and running, so no consumer-loop note is owed.
    assert not any("consumer loop" in note for note in answer.attention.notes)


def test_orient_says_when_armed_lines_have_no_consumer_loop_or_lane(tmp_path: Path) -> None:
    instance, _line, _procedure, start = _armed_world(tmp_path)
    lane_down = contracts.ProviderLaneStatus(
        state="unavailable", code="provider_runtime_recovery_failed", detail="recovery failed"
    )

    idle = service_playbill_orient(
        instance,
        evaluation_time=start + timedelta(seconds=1),
        consumers_running=False,
        provider_lane=lane_down,
    )
    served = service_playbill_orient(
        instance,
        evaluation_time=start + timedelta(seconds=1),
        consumers_running=True,
    )

    assert idle.attention is not None and served.attention is not None
    assert idle.attention.enablements is not None and idle.attention.enablements.running == 1
    assert any("consumer loop is not running" in note for note in idle.attention.notes)
    assert any("provider lane is unavailable" in note for note in idle.attention.notes)
    assert not any("consumer loop" in note for note in served.attention.notes)


def test_an_instance_without_lines_reports_no_arms(tmp_path: Path) -> None:
    from tests.test_procedures.test_procedure_run_surface import _world

    instance, _owner, _procedure = _world(tmp_path)

    answer = service_playbill_orient(instance)

    assert answer.attention is not None and answer.attention.enablements is None
    assert "enablements" not in answer.attention.model_dump(mode="json")


class _Stub:
    def __init__(self, instance: Any) -> None:
        self.instance = instance

    def orient(self, instance_id: str, **values: Any) -> Any:
        from datetime import datetime

        values.pop("caller_tools", None)
        if isinstance(values.get("evaluation_time"), str):
            values["evaluation_time"] = datetime.fromisoformat(values["evaluation_time"])
        return service_playbill_orient(self.instance, **values)

    def next(self, instance_id: str, **values: Any) -> Any:
        request = NextRequest.model_validate(
            {
                "evaluation_time": values["evaluation_time"],
                "access_profile": values["access_profile"],
            }
        )
        result = service_playbill_next(self.instance, request=request, caller_rung=1)
        return contracts.NextResult.model_validate(result.model_dump(mode="json"))


def test_the_cli_shows_stopped_arms_in_orient_and_next(stopped, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    instance, line, when = stopped
    stub = _Stub(instance)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: stub)
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_next_workspace", lambda _root: {}
    )
    prefix = ["--server-url", "http://server", "--instance-id", "inst"]
    stamp = when.isoformat()

    orient = CliRunner().invoke(cli, [*prefix, "orient", "--evaluation-time", stamp])
    assert orient.exit_code == 0, orient.output
    assert "Line enablements: running=0 stalled=0 stopped=1" in orient.output
    assert f"{line.identity.qualified} stopped (credential_revoked)" in orient.output

    queue = CliRunner().invoke(cli, [*prefix, "next", "--evaluation-time", stamp])
    assert queue.exit_code == 0, queue.output
    assert "Status: line enablements stalled=0 stopped=1" in queue.output
    assert "consumer_stalled" in queue.output


def test_runtime_orient_passes_the_lane_and_consumer_loop_through(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The HTTP route and MCP tool both reach the runtime facade, which now forwards both."""

    from cruxible_core.runtime import playbill_api

    seen: dict[str, Any] = {}
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_a, **_k: None)
    monkeypatch.setattr(playbill_api, "playbill_whoami", lambda _id: None)

    class _Manager:
        consumer_runner = type("R", (), {"running": True})()

        def provider_runtime_operator(self):  # type: ignore[no-untyped-def]
            return type("L", (), {"lane_status": lambda self: ("available", None, None)})()

        def get(self, _id):  # type: ignore[no-untyped-def]
            return "instance"

    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: _Manager())
    monkeypatch.setattr(
        playbill_api, "service_playbill_orient", lambda instance, **values: seen.update(values)
    )

    playbill_api.playbill_orient("inst")

    assert seen["consumers_running"] is True
    assert seen["provider_lane"].state == "available"


def test_the_mcp_orient_tool_carries_the_arms(stopped, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import json

    from cruxible_core.mcp import handlers
    from cruxible_core.mcp.server import create_server
    from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run

    instance, line, when = stopped
    stub = _Stub(instance)
    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_orient",
        lambda instance_id, **values: stub.orient(instance_id, **values),
    )
    server = create_server()

    async def exercise() -> str:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_orient",
                {"instance_id": "inst", "evaluation_time": when.isoformat()},
            )
            assert not result.isError
            return " ".join(block.text for block in result.content if hasattr(block, "text"))

    arms = json.loads(_run(exercise()))["attention"]["enablements"]

    assert arms["stopped"] == 1
    assert arms["needs_attention"] == [f"{line.identity.qualified} stopped (credential_revoked)"]


def test_the_sdk_reads_the_arms_off_orient(stopped) -> None:  # type: ignore[no-untyped-def]
    from cruxible_client.authoring.sdk import Cruxible

    instance, _line, when = stopped
    playbill = Cruxible.__new__(Cruxible)
    playbill._client = _Stub(instance)  # type: ignore[assignment]
    playbill._instance_id = "inst"
    # A connection without a workspace: orient then reports no floor.
    playbill._workspace = None
    playbill._read_at = lambda coordinate=None: None  # type: ignore[method-assign,assignment]
    playbill._evaluation_time = lambda: when.isoformat()  # type: ignore[method-assign]
    playbill._observe_read = lambda *_a, **_k: None  # type: ignore[method-assign]

    answer = playbill.orient()

    assert answer.attention is not None and answer.attention.enablements is not None
    assert answer.attention.enablements.stopped == 1
