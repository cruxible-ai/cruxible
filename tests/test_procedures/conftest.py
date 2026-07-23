"""Shared fixtures for procedure definition and lifecycle tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import service_lock

CONFIG_YAML = """\
version: "1.0"
name: procedure_stage_a

entity_types:
  Task:
    properties:
      task_id:
        type: string
        primary_key: true
      status:
        type: string
  Incident:
    properties:
      incident_id:
        type: string
        primary_key: true
      status:
        type: string

relationships: []

contracts:
  ProcedureInput:
    fields:
      value:
        type: int
  ProcedureOutput:
    fields:
      value:
        type: int

providers:
  exported_action:
    kind: tool
    contract_in: ProcedureInput
    contract_out: ProcedureOutput
    ref: https://example.invalid/action
    version: "1.0"
    runtime: http_json
    procedure_access: graph_write
    config:
      timeout_s: 5
  disabled_action:
    kind: tool
    contract_in: ProcedureInput
    contract_out: ProcedureOutput
    ref: https://example.invalid/disabled
    version: "1.0"
    runtime: http_json
"""


@pytest.fixture
def procedure_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    service_lock(instance)
    return instance


def actor(actor_id: str, operation_id: str | None = None) -> GovernedActorContext:
    """Build stable attributed identities with distinct operation metadata."""
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org-procedures",
        operation_id=operation_id or f"op-{actor_id}",
        timestamp=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
    )


def provider_definition(
    name: str = "restart_task",
    *,
    precondition: dict[str, object] | None = None,
) -> ProcedureDefinition:
    """Return a valid definition that exercises provider export/tier checks."""
    return ProcedureDefinition.model_validate(
        {
            "name": name,
            "description": "Restart one task through an exported action",
            "contract_in": "ProcedureInput",
            "contract_out": "ProcedureOutput",
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {} if precondition is None else precondition,
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )
