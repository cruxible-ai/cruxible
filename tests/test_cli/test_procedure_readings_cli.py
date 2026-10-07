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
