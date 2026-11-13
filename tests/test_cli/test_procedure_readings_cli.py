"""CLI `procedure readings` reaches every field its request model has."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import cruxible_core.cli.commands.playbill as playbill_commands
from cruxible_core.cli.main import cli


def test_readings_take_grain_evaluation_time_and_at_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_server_call(call, *, command_name: str):  # type: ignore[no-untyped-def]
        client = SimpleNamespace(
            list_procedure_readings=lambda instance_id, name, *, request: (
                captured.update(name=name, request=request)
                or SimpleNamespace(contracts=(), readings=(), cursor=None)
            )
        )
        return call(client, "inst")

    monkeypatch.setattr(playbill_commands, "_server_call", fake_server_call)
    at = {
        "git_oid": "a" * 40,
        "semantic_root": "sha256:" + "1" * 64,
        "generation_root": "sha256:" + "2" * 64,
        "compiler_digest": "sha256:" + "3" * 64,
    }
    result = CliRunner().invoke(
        cli,
        [
            "procedure",
            "readings",
            "triage",
            "--subject-grain",
            "node",
            "--evaluation-time",
            "2026-08-24T15:00:00Z",
            "--at",
            "-",
        ],
        input=json.dumps(at),
    )

    assert result.exit_code == 0, result.output
    request = captured["request"]
    assert captured["name"] == "triage"
    assert request.subject_grain == "node"
    assert request.evaluation_time.isoformat() == "2026-08-24T15:00:00+00:00"
    assert request.at.git_oid == "a" * 40


def test_dispose_passes_the_typed_judgment_and_prints_the_result(monkeypatch):
    from cruxible_core.cli.commands import _common

    targets = []
    monkeypatch.setattr(_common, "_echo_write_target", lambda mode, kwargs: targets.append(mode))
    from datetime import UTC, datetime

    from cruxible_client.contracts.procedures.dispositions import (
        ProcedureResolutionDispositionResult,
        ResolutionDispositionSummary,
    )

    captured = {}
    resolution_id = "RSR-" + "a" * 32
    result = ProcedureResolutionDispositionResult(
        disposition=ResolutionDispositionSummary(
            disposition_id="RSD-" + "b" * 32,
            resolution_id=resolution_id,
            sequence=1,
            verdict="overturned",
            reviewer_id="reviewer",
            recorded_at=datetime.now(UTC),
            reason="Missing evidence.",
        ),
        standing_resolution_id=None,
        written_now=True,
    )

    def dispose(instance_id, name, *, request):
        captured.update(name=name, request=request)
        return result

    monkeypatch.setattr(
        playbill_commands,
        "_server_call",
        lambda call, **kwargs: call(SimpleNamespace(dispose_procedure_resolution=dispose), "inst"),
    )
    output = CliRunner().invoke(
        cli,
        [
            "procedure",
            "dispose",
            "triage",
            resolution_id,
            "--verdict",
            "overturned",
            "--reason",
            "Missing evidence.",
        ],
    )
    assert output.exit_code == 0, output.output
    assert "overturned by reviewer" in output.output and "Standing answer: none" in output.output
    assert captured["request"].reason == "Missing evidence." and captured["name"] == "triage"
    assert targets == ["active"]
    refused = CliRunner().invoke(
        cli,
        ["procedure", "dispose", "triage", resolution_id, "--verdict", "upheld", "--reason", " "],
    )
    assert refused.exit_code != 0 and "reason" in refused.output
