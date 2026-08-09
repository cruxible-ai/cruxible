"""CLI dispatch coverage for procedure dry-runs."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.procedure.types import ProcedureRun


def test_procedure_run_dry_run_is_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def run_procedure(self, instance_id: str, procedure_id: str, **kwargs):
            captured.update({"instance_id": instance_id, "procedure_id": procedure_id, **kwargs})
            run = ProcedureRun(
                procedure_id=procedure_id,
                definition_digest="definition-digest",
                status="finalized",
                verdict="succeeded",
            )
            return {
                "run": run.model_dump(mode="json"),
                "output": {"group_status": "would_propose"},
                "receipt": {},
                "dry_run": True,
            }

    def fake_dispatch(remote, local, **kwargs):
        return remote(StubClient(), "inst_1")

    monkeypatch.setattr(
        "cruxible_core.cli.commands.procedures._dispatch_cli_instance",
        fake_dispatch,
    )
    result = CliRunner().invoke(
        cli,
        [
            "procedure",
            "run",
            "PRC-1",
            "--input",
            '{"value": 1}',
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dry_run"] is True
    assert captured == {
        "instance_id": "inst_1",
        "procedure_id": "PRC-1",
        "input_payload": {"value": 1},
        "dry_run": True,
    }
