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
  Vulnerability:
    properties:
      cve_id:
        type: string
        primary_key: true
      status:
        type: string
  Response:
    properties:
      response_id:
        type: string
        primary_key: true
      decision:
        type: string

relationships:
  - name: blocks
    from: Task
    to: Incident
    properties:
      reason:
        type: string
        optional: true
  - name: recommended_response
    from: Vulnerability
    to: Response
    properties:
      rationale:
        type: string
        optional: true

enums:
  Severity:
    values: [low, medium, high]

contracts:
  ProcedureInput:
    fields:
      value:
        type: int
  ProcedureOutput:
    fields:
      value:
        type: int
  OpenProcedureInput:
    fields:
      value:
        type: int
    allow_extra: true
  KevTriageInput:
    fields:
      cve_id:
        type: string
      exposed:
        type: bool
      critical:
        type: bool

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


def bridge_definition(
    name: str = "land_blockers",
    *,
    rows: list[dict[str, object]] | None = None,
    relationship_type: str = "blocks",
    edges_from: str = "$steps.rows.items",
    proposal_scope: object = "blocker-triage",
) -> ProcedureDefinition:
    """Return a provider-free definition that lands shaped candidate rows."""
    return ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": name,
            "description": "Land shaped rows as one governed proposal",
            "steps": [
                {
                    "id": "rows",
                    "shape_items": {
                        "items": rows
                        if rows is not None
                        else [
                            {
                                "from_type": "Task",
                                "from_id": "T-1",
                                "to_type": "Incident",
                                "to_id": "I-1",
                                "properties": {"reason": "shares a deploy window"},
                            }
                        ],
                        "include_input": True,
                    },
                    "as": "rows",
                },
                {
                    "id": "land",
                    "propose_group_from": {
                        "relationship_type": relationship_type,
                        "edges_from": edges_from,
                        "proposal_scope": proposal_scope,
                        "thesis_text": "tasks blocking open incidents",
                    },
                    "as": "proposal",
                },
            ],
            "returns": "proposal",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
            "declared_tier": "governed_write",
        }
    )


def kev_three_arm_definition() -> ProcedureDefinition:
    """Worked KEV triage: three decisions converge on one terminal bridge."""

    def decision_row(response_id: str, rationale: str) -> list[dict[str, object]]:
        return [
            {
                "from_type": "Vulnerability",
                "from_id": "CVE-2026-0001",
                "to_type": "Response",
                "to_id": response_id,
                "properties": {"rationale": rationale},
            }
        ]

    return ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": "kev_three_arm_triage",
            "description": "Patch exposed KEV, otherwise mitigate critical or accept risk",
            "contract_in": "KevTriageInput",
            "steps": [
                {
                    "id": "exposed_guard",
                    "guard": {"left": "$input.exposed", "op": "eq", "right": True},
                    "on_true": "patch",
                    "on_false": "critical_guard",
                    "message": "select exposure arm",
                },
                {
                    "step": {
                        "id": "patch",
                        "shape_items": {
                            "items": decision_row("patch", "internet-exposed KEV"),
                            "include_input": True,
                        },
                        "as": "decision",
                    },
                    "next": "land_decision",
                },
                {
                    "id": "critical_guard",
                    "guard": {"left": "$input.critical", "op": "eq", "right": True},
                    "on_true": "mitigate",
                    "on_false": "accept",
                    "message": "select criticality arm",
                },
                {
                    "step": {
                        "id": "mitigate",
                        "shape_items": {
                            "items": decision_row("mitigate", "critical internal KEV"),
                            "include_input": True,
                        },
                        "as": "decision",
                    },
                    "next": "land_decision",
                },
                {
                    "id": "accept",
                    "shape_items": {
                        "items": decision_row("accept", "documented residual risk"),
                        "include_input": True,
                    },
                    "as": "decision",
                },
                {
                    "id": "land_decision",
                    "propose_group_from": {
                        "relationship_type": "recommended_response",
                        "edges_from": "$steps.decision.items",
                        "proposal_scope": "$input.cve_id",
                        "thesis_text": "KEV triage decision",
                    },
                    "as": "proposal",
                },
            ],
            "returns": "proposal",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
            "declared_tier": "governed_write",
        }
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
