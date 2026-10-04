"""Operational ``get`` on every surface: CLI text, the MCP tool, and the SDK.

Each surface reaches the real ``get`` service over one real world -- an armed
Line that admitted a run and then stopped -- so the cards, their rendered
``next`` calls and the refusals are what a caller of that surface sees.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.authoring.sdk_types import RefKind
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineEvaluateRequest
from cruxible_client.contracts.operational_reads import GetLineCard
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_evaluate_line,
    service_stop_line_arm,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_mcp.test_playbill_protocol_curation import _protocol_session, _run
from tests.test_procedures.test_line_arming import _armed_world
from tests.test_procedures.test_line_dispatch import _active_segment
from tests.test_procedures.test_line_triggers import capture
from tests.test_procedures.test_procedure_run_surface import _actor

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    instance, line, procedure, start = _armed_world(tmp_path_factory.mktemp("surfaces"))
    capture(instance, procedure, at=start + timedelta(seconds=1))
    later = start + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=start, until=later),
        actor=_actor(instance),
        now=later,
    )
    service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(limit=1),
        actor=_actor(instance),
        now=later + timedelta(seconds=1),
        caller_rung=3,
    )
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="credential_revoked",
        detail="revoked",
        actor=_actor(instance),
        now=later + timedelta(seconds=2),
    )
    return instance, line


class _ServiceClient:
    """A transport whose get is the real service over one instance."""

    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.requests: list[GetRequest] = []

    def get(self, instance_id: str, *, request: GetRequest) -> Any:
        self.requests.append(request)
        return service_playbill_get(self.instance, request=request, access=_ACCESS)


def test_the_cli_prints_a_line_card_as_values(world, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    instance, line = world
    client = _ServiceClient(instance)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    prefix = ["--server-url", "http://server", "--instance-id", "inst"]

    text = CliRunner().invoke(cli, [*prefix, "playbill", "get", f"Line:{line.identity.name}"])

    assert text.exit_code == 0, text.output
    assert f"line: {line.identity.qualified}" in text.output
    assert "trigger: capture_landing" in text.output
    assert "credential_revoked" in text.output
    assert f"next: cruxible playbill get {line.procedure.target.qualified}" in text.output
    assert client.requests[-1].surface == "cli"

    as_json = CliRunner().invoke(
        cli, [*prefix, "playbill", "get", "Mandate:served-line-mandate", "--json"]
    )
    assert as_json.exit_code == 0, as_json.output
    body = json.loads(as_json.output)
    assert body["kind"] == "mandate" and body["card"]["grants"] == "propose"

    missing = CliRunner().invoke(cli, [*prefix, "playbill", "get", "Line:nope"])
    assert missing.exit_code != 0
    assert "playbill.get.ref_not_found" in missing.output


def test_the_mcp_tool_answers_a_line_card_by_its_identity_digest(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.procedures.line_specs import line_identity_digest

    instance, line = world
    client = _ServiceClient(instance)
    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr(handlers.playbill_api, "playbill_get", client.get)
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_playbill_get",
                {"instance_id": "inst", "ref": line_identity_digest(line.identity)},
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, output = _run(exercise())

    assert not is_error, output
    body = json.loads(output)
    assert body["kind"] == "line" and body["card"]["line"] == line.identity.qualified
    assert body["card"]["arms"][0]["state"] == "stopped"
    assert all(step.startswith("cruxible_playbill_get(") for step in body["card"]["next"])
    assert client.requests[-1].surface == "mcp"


def test_the_sdk_returns_typed_operational_cards(world, tmp_path) -> None:  # type: ignore[no-untyped-def]
    instance, line = world
    playbill = Cruxible.__new__(Cruxible)
    client = _ServiceClient(instance)
    playbill._client = client  # type: ignore[assignment]
    playbill._instance_id = "inst"
    playbill._read_at = lambda coordinate=None: None  # type: ignore[method-assign,assignment]
    playbill._evaluation_time = lambda: "2026-08-24T16:05:00+00:00"  # type: ignore[method-assign]
    playbill._observe_read = lambda *_a, **_k: None  # type: ignore[method-assign]

    card = playbill.get(line.identity.qualified)

    assert card.kind is RefKind.LINE and card.identity == line.identity.name
    assert isinstance(card.value, GetLineCard)
    assert card.value.next[0] == f'cx.get("{line.procedure.target.qualified}")'
    mandate = playbill.get("ProcedureMandate:served-line-mandate")
    assert mandate.kind is RefKind.MANDATE
    assert client.requests[-1].surface == "sdk"


def _orient_client(instance: Any) -> Any:
    from datetime import datetime

    from cruxible_core.service.discovery.orient import service_playbill_orient

    class _Orient(_ServiceClient):
        def head(self, instance_id: str, **_values: Any) -> Any:
            from cruxible_core.service.discovery.orient import service_playbill_head

            return service_playbill_head(self.instance)

        def orient(self, instance_id: str, **values: Any) -> Any:
            self.surfaces = [*getattr(self, "surfaces", []), values["surface"]]
            values.pop("caller_tools", None)
            if isinstance(values.get("evaluation_time"), str):
                values["evaluation_time"] = datetime.fromisoformat(values["evaluation_time"])
            return service_playbill_orient(self.instance, **values)

    return _Orient(instance)


def test_the_cli_pages_the_operational_sections(world, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    instance, line = world
    client = _orient_client(instance)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    prefix = ["--server-url", "http://server", "--instance-id", "inst"]

    lines = CliRunner().invoke(cli, [*prefix, "playbill", "orient", "--section", "lines"])
    assert lines.exit_code == 0, lines.output
    assert line.identity.qualified in lines.output and "stopped" in lines.output
    assert f"cruxible playbill get {line.identity.qualified}" in lines.output

    mandates = CliRunner().invoke(
        cli, [*prefix, "playbill", "orient", "--section", "mandates", "--json"]
    )
    assert mandates.exit_code == 0, mandates.output
    assert json.loads(mandates.output)["mandates"][0]["grants"] == "propose"

    whole = CliRunner().invoke(cli, [*prefix, "playbill", "orient"])
    assert whole.exit_code == 0, whole.output
    assert "lines=1" in whole.output and "mandates=1" in whole.output


def test_the_mcp_orient_tool_pages_lines(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    instance, line = world
    client = _orient_client(instance)
    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr(
        handlers.playbill_api,
        "playbill_orient",
        lambda instance_id, **values: client.orient(instance_id, **values),
    )
    server = create_server()

    async def exercise() -> str:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_playbill_orient", {"instance_id": "inst", "section": "lines"}
            )
            assert not result.isError
            return " ".join(block.text for block in result.content if hasattr(block, "text"))

    body = json.loads(_run(exercise()))

    assert body["lines"][0]["line"] == line.identity.qualified
    assert body["next"][0] == f'cruxible_playbill_get(ref="{line.identity.qualified}")'


def test_the_sdk_orients_by_operational_section(world) -> None:  # type: ignore[no-untyped-def]
    instance, line = world
    playbill = Cruxible._from_client(  # type: ignore[arg-type]
        _orient_client(instance),
        instance_id="inst",
        workspace=None,
        clock=lambda: datetime(2026, 8, 24, 16, 5, tzinfo=UTC),
    )

    answer = playbill.orient(section="lines")
    assert answer.floor is None

    assert answer.lines is not None and answer.lines[0].line == line.identity.qualified
    assert answer.next[0] == f'cx.get("{line.identity.qualified}")'
