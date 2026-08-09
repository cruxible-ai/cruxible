"""R5–R8 procedure bridge and branch-target refusals."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import WorkflowStepSchema
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.analysis import build_procedure_graph
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureGuardStepSchema,
)
from cruxible_core.service import service_propose_procedure
from tests.test_procedures.conftest import actor, bridge_definition


def test_r5_bridge_must_be_terminal_on_its_path(
    procedure_instance: CruxibleInstance,
) -> None:
    payload = bridge_definition().model_dump(mode="python", by_alias=True, exclude_none=True)
    payload["steps"].append(
        {
            "id": "after",
            "shape_items": {"items": [], "include_input": True},
            "as": "after",
        }
    )
    definition = ProcedureDefinition.model_validate(payload)

    with pytest.raises(ConfigError, match="R5:.*must be terminal"):
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )


def test_r7_definition_allows_at_most_one_bridge() -> None:
    payload = bridge_definition().model_dump(mode="python", by_alias=True, exclude_none=True)
    duplicate = dict(payload["steps"][-1])
    duplicate["id"] = "land_again"
    duplicate["as"] = "proposal_again"
    payload["steps"].append(duplicate)

    with pytest.raises(ValidationError, match="R7:.*at most one"):
        ProcedureDefinition.model_validate(payload)


def test_r8_never_branch_targetable_kind_is_refused_at_parse() -> None:
    payload = bridge_definition().model_dump(mode="python", by_alias=True, exclude_none=True)
    payload["steps"] = [
        {
            "id": "apply",
            "apply_all": {"entities_from": ["built"], "relationships_from": []},
            "as": "result",
        }
    ]
    payload["returns"] = "result"

    with pytest.raises(ValidationError, match="R8 NEVER_BRANCH_TARGETABLE.*apply_all"):
        ProcedureDefinition.model_validate(payload)


def test_r6_control_edge_refuses_non_targetable_kind_defensively() -> None:
    guard = ProcedureGuardStepSchema.model_validate(
        {
            "id": "gate",
            "guard": {"left": 1, "op": "eq", "right": 1},
            "on_true": "apply",
            "on_false": "$abort",
            "message": "stop",
        }
    )
    apply = WorkflowStepSchema.model_validate(
        {
            "id": "apply",
            "apply_all": {"entities_from": ["built"], "relationships_from": []},
            "as": "result",
        }
    )
    definition = ProcedureDefinition.model_construct(steps=[guard, apply])

    with pytest.raises(ConfigError, match="R6:.*non-targetable kind 'apply_all'"):
        build_procedure_graph(definition)
