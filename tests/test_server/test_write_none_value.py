"""A none (``{"none": null}``) reaches the daemon from every client.

``none: null`` is the whole value of a none on a one-valued field, not an unset
member. The transport sends request bodies dumped with ``exclude_none``, which
used to drop it: every client then failed with ``body.value.NoneValue.none:
Field required`` before any check. Each test here goes through the real
``CruxibleClient`` serialisation into the served app, never a stub that skips it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.write import (
    FileEvidence,
    NoneValue,
    SetChange,
    SetRequest,
    WriteOutcome,
    WriteRequest,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import (
    CLAIM_TYPES,
    KIND,
    report_evidence,
    seed_write_vocabulary,
)
from tests.test_service.test_write_inverse import INVERSE_TYPES

WI1 = f"{KIND}/wi-1"
#: ``state`` declares an inverse; ``status`` does not, so core refuses a none of it.
INVERSE = "state"
NO_INVERSE = "status"
UNDECLARED = "cruxible.write.inverse_undeclared"


@pytest.fixture
def world(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> Iterator[tuple[CruxibleClient, str, Path]]:
    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    seed_write_vocabulary(
        get_playbill_manager().get(instance_id),
        actor_id=actor,
        claim_types=(*CLAIM_TYPES, *INVERSE_TYPES),
    )
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    yield transport, instance_id, workspace


def _evidence(workspace: Path, anchor: str) -> FileEvidence:
    return FileEvidence.model_validate(report_evidence(workspace, anchor))


def _assert_none_landed(outcome: WriteOutcome, *, status: str = "accepted") -> None:
    assert outcome.status == status, outcome
    (change,) = outcome.changes
    assert change.field == INVERSE
    assert change.after is not None and change.after.object == {"none": None}  # type: ignore[union-attr]


def test_the_transport_set_carries_the_none(world: tuple[CruxibleClient, str, Path]) -> None:
    transport, instance_id, workspace = world
    request = SetRequest(
        subject=WI1,
        field=INVERSE,
        value=NoneValue(none=None),
        because="The report says it is closed.",
        evidence=_evidence(workspace, "closed"),
    )
    # The body the transport sends keeps the member that is the whole value.
    assert request.model_dump(mode="json", exclude_none=True)["value"] == {"none": None}
    _assert_none_landed(
        transport.set(instance_id, request=request.model_copy(update={"dry_run": True})),
        status="would_accept",
    )
    _assert_none_landed(transport.set(instance_id, request=request))
    refused = transport.set(
        instance_id,
        request=SetRequest(
            subject=WI1, field=NO_INVERSE, value=NoneValue(none=None), because="Closed."
        ),
    )
    assert refused.refused and refused.refusal is not None
    assert refused.refusal.code == UNDECLARED


def test_the_transport_write_batch_carries_the_none(
    world: tuple[CruxibleClient, str, Path],
) -> None:
    transport, instance_id, workspace = world
    request = WriteRequest(
        because="The report says it is closed.",
        changes=(
            SetChange(
                subject=WI1,
                field=INVERSE,
                value=NoneValue(none=None),
                evidence=_evidence(workspace, "closed"),
            ),
        ),
    )
    preview = transport.write(instance_id, request=request.model_copy(update={"dry_run": True}))
    _assert_none_landed(preview, status="would_accept")
    _assert_none_landed(transport.write(instance_id, request=request))


def test_the_sdk_set_carries_the_none(world: tuple[CruxibleClient, str, Path]) -> None:
    transport, instance_id, workspace = world
    cx = Cruxible._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )
    with pytest.raises(WriteRefusalError) as refused:
        cx.set(WI1, NO_INVERSE, NoneValue(none=None), because="Closed.")
    assert UNDECLARED in str(refused.value)
    report_evidence(workspace, "closed")
    outcome = cx.set(
        WI1,
        INVERSE,
        NoneValue(none=None),
        because="The report says it is closed.",
        evidence=FileEvidence(file="reports.md#closed"),
    )
    _assert_none_landed(outcome)


@pytest.fixture
def cli_world(
    world: tuple[CruxibleClient, str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, Path]:
    transport, instance_id, workspace = world
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    return instance_id, workspace


def _cli(instance_id: str, *args: str, input: str | None = None) -> Any:
    prefix = ["--server-url", "http://testserver", "--instance-id", instance_id]
    return CliRunner().invoke(cli, [*prefix, *args], input=input)


def test_the_cli_set_none_carries_the_none(cli_world: tuple[str, Path]) -> None:
    instance_id, workspace = cli_world
    refused = _cli(instance_id, "set", WI1, NO_INVERSE, "--none", "--because", "Closed.")
    assert refused.exit_code == 1, refused.output
    assert UNDECLARED in refused.output
    report_evidence(workspace, "closed")
    landed = _cli(
        instance_id,
        "set",
        WI1,
        INVERSE,
        "--none",
        "--evidence-file",
        "reports.md#closed",
        "--workspace-root",
        str(workspace),
        "--because",
        "The report says it is closed.",
        "--json",
    )
    assert landed.exit_code == 0, landed.output
    _assert_none_landed(WriteOutcome.model_validate(json.loads(landed.stdout)))


def test_a_write_file_none_carries_the_none(cli_world: tuple[str, Path]) -> None:
    instance_id, workspace = cli_world
    report_evidence(workspace, "closed")
    payload = f"""\
because: The report says it is closed.
changes:
  - op: set
    subject: {WI1}
    field: {INVERSE}
    value: {{none: null}}
    evidence: {{kind: file, file: "reports.md#closed"}}
"""
    for args, status in ((("--dry-run",), "would_accept"), ((), "accepted")):
        result = _cli(
            instance_id,
            "write",
            "-",
            "--workspace-root",
            str(workspace),
            "--json",
            *args,
            input=payload,
        )
        assert result.exit_code == 0, result.output
        _assert_none_landed(WriteOutcome.model_validate(json.loads(result.stdout)), status=status)


def test_the_mcp_set_carries_the_none(
    world: tuple[CruxibleClient, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, instance_id, workspace = world
    monkeypatch.setattr(handlers, "_get_client", lambda: transport)
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    refused = handlers.handle_playbill_set(
        instance_id, subject=WI1, field=NO_INVERSE, value={"none": None}, because="Closed."
    )
    assert refused.refusal is not None and refused.refusal.code == UNDECLARED
    report_evidence(workspace, "closed")
    landed = handlers.handle_playbill_set(
        instance_id,
        subject=WI1,
        field=INVERSE,
        value={"none": None},
        because="The report says it is closed.",
        evidence={"kind": "file", "file": "reports.md#closed"},
    )
    _assert_none_landed(landed)
