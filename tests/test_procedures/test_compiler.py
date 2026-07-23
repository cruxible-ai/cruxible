"""Shared compiler entry-point coverage for state-held procedure bodies."""

from __future__ import annotations

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import compile_procedure_definition


def test_compile_procedure_forces_utility_and_preserves_definition_time_input_refs(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "retry_exported_action",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "seed",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "seed",
                },
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 3,
                        "until": {
                            "left": "$steps.result.value",
                            "op": "eq",
                            "right": 1,
                            "message": "result is ready",
                        },
                        "steps": [
                            {
                                "id": "invoke",
                                "provider": "exported_action",
                                "input": {"value": "$steps.seed.value"},
                                "as": "result",
                            }
                        ],
                    },
                    "as": "attempt",
                },
            ],
            "returns": "attempt",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 4},
            "declared_tier": "graph_write",
        }
    )

    plan = compile_procedure_definition(procedure_instance, definition)

    assert plan.workflow == definition.name
    assert plan.workflow_type == "utility"
    assert plan.input_payload == {}
    assert plan.steps[1].kind == "repeat"
    assert plan.steps[1].repeat_max_attempts == 3
    assert plan.steps[1].repeat_until_spec == definition.steps[1].repeat.until  # type: ignore[union-attr]
    assert plan.steps[1].repeat_steps[0].kind == "provider"
    assert plan.steps[1].repeat_steps[0].input_preview == {"value": "$steps.seed.value"}
