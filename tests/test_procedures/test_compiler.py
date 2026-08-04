"""Shared compiler entry-point coverage for state-held procedure bodies."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
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


def test_compile_procedure_rejects_reference_expression_as_return_alias(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "invalid_return_reference",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "load_transactions",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "transactions",
                }
            ],
            "returns": "$steps.transactions.result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )

    with pytest.raises(
        ConfigError,
        match=("returns alias '\\$steps.transactions.result' not produced by any output step"),
    ):
        compile_procedure_definition(procedure_instance, definition)


def test_compile_procedure_rejects_return_from_non_output_guard_step(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "invalid_guard_return",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "input_present",
                    "assert_exists": {"ref": "$input.value"},
                }
            ],
            "returns": "input_present",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
        }
    )

    with pytest.raises(
        ConfigError,
        match="returns alias 'input_present' not produced by any output step",
    ):
        compile_procedure_definition(procedure_instance, definition)
