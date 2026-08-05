"""Procedure/provider schema validation and static-expansion tests."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from cruxible_core.config.schema import ProviderSchema
from cruxible_core.procedure.types import ProcedureDefinition


def _definition(**updates: object) -> ProcedureDefinition:
    payload: dict[str, object] = {
        "name": "bounded_action",
        "steps": [
            {
                "id": "present",
                "assert_exists": {"ref": "$input.value"},
            }
        ],
        "returns": "present",
        "precondition": {},
        "budget": {"wall_clock_s": 10, "max_provider_calls": 0},
    }
    payload.update(updates)
    return ProcedureDefinition.model_validate(payload)


def _provider_step(index: int) -> dict[str, object]:
    return {
        "id": f"provider_{index}",
        "provider": "action",
        "input": {},
        "as": f"provider_{index}",
    }


def _assert_step(index: int) -> dict[str, object]:
    return {
        "id": f"assert_{index}",
        "assert_exists": {"ref": "$input.value"},
    }


def test_provider_access_defaults_disabled() -> None:
    provider = ProviderSchema(
        contract_in="cruxible.EmptyInput",
        contract_out="cruxible.EmptyInput",
        ref="tests.support.workflow_test_providers.margin_calculator",
        version="1",
    )

    assert provider.procedure_access == "disabled"
    assert "procedure_access" not in provider.model_dump(mode="json")


def test_python_provider_cannot_be_exported_to_procedures() -> None:
    with pytest.raises(ValidationError, match="in-process Python providers"):
        ProviderSchema(
            contract_in="cruxible.EmptyInput",
            contract_out="cruxible.EmptyInput",
            ref="tests.support.workflow_test_providers.margin_calculator",
            version="1",
            runtime="python",
            procedure_access="governed_write",
        )


@pytest.mark.parametrize("runtime", ["http_json", "command"])
def test_timeout_enforced_provider_transports_can_be_exported(
    runtime: Literal["http_json", "command"],
) -> None:
    ref = "https://example.invalid/action" if runtime == "http_json" else "/usr/bin/true"
    provider = ProviderSchema(
        contract_in="cruxible.EmptyInput",
        contract_out="cruxible.EmptyInput",
        ref=ref,
        version="1",
        runtime=runtime,
        procedure_access="admin",
    )

    assert provider.procedure_access == "admin"


def test_type_is_not_part_of_procedure_body() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _definition(type="utility")


def test_empty_precondition_is_explicitly_allowed() -> None:
    assert _definition().precondition.model_dump(exclude_none=True) == {}


def test_precondition_refuses_reserved_query_key_like_gate_conditions() -> None:
    with pytest.raises(
        ValidationError,
        match="reserved for a future named-query condition variant",
    ):
        _definition(
            precondition={
                "entity_type": "Task",
                "condition": {"query": "eligible_tasks"},
            }
        )


def test_precondition_refuses_legacy_flat_property_shape() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _definition(precondition={"status": "ready"})


def test_precondition_requires_named_entity_type_with_condition() -> None:
    with pytest.raises(
        ValidationError,
        match="entity_type must be non-empty when condition is present",
    ):
        _definition(precondition={"condition": {"status": "ready"}})

    with pytest.raises(
        ValidationError,
        match="entity_type must be non-empty when condition is present",
    ):
        _definition(
            precondition={
                "entity_type": " ",
                "condition": {"status": "ready"},
            }
        )


def test_precondition_requires_non_empty_condition_with_entity_type() -> None:
    with pytest.raises(
        ValidationError,
        match="condition must declare at least one property=value pair",
    ):
        _definition(precondition={"entity_type": "Task"})

    with pytest.raises(
        ValidationError,
        match="condition must declare at least one property=value pair",
    ):
        _definition(precondition={"entity_type": "Task", "condition": {}})


def test_build_write_workflow_kinds_are_refused() -> None:
    with pytest.raises(ValidationError, match="disallowed kinds.*make_entities"):
        _definition(
            steps=[
                {
                    "id": "build",
                    "make_entities": {
                        "items": "$input.items",
                        "entity_type": "Task",
                        "entity_id": "$item.task_id",
                    },
                    "as": "built",
                }
            ]
        )


def test_repeat_requires_max_attempts_and_until() -> None:
    with pytest.raises(ValidationError, match="max_attempts"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [_provider_step(1)],
                    },
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 1},
        )

    with pytest.raises(ValidationError, match="until"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {"max_attempts": 2, "steps": [_provider_step(1)]},
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 2},
        )


def test_repeat_attempts_are_capped_at_25() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 25"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 26,
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [_provider_step(1)],
                    },
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 26},
        )


def test_repeat_until_may_only_reference_current_attempt_step_outputs() -> None:
    with pytest.raises(ValidationError, match="only current-attempt"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {
                            "left": "$input.value",
                            "op": "eq",
                            "right": 1,
                            "message": "done",
                        },
                        "steps": [_provider_step(1)],
                    },
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 2},
        )

    with pytest.raises(ValidationError, match="does not name a current-attempt"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {
                            "left": "$steps.missing.value",
                            "op": "eq",
                            "right": 1,
                            "message": "done",
                        },
                        "steps": [_provider_step(1)],
                    },
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 2},
        )


def test_repeat_nested_steps_exclude_queries_and_nested_repeat() -> None:
    with pytest.raises(ValidationError, match="repeat nested steps may only use"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [
                            {
                                "id": "read",
                                "query": "anything",
                                "as": "read",
                            }
                        ],
                    },
                    "as": "attempt",
                }
            ]
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _definition(
            steps=[
                {
                    "id": "outer",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [
                            {
                                "id": "inner",
                                "repeat": {
                                    "max_attempts": 2,
                                    "until": {
                                        "left": 1,
                                        "op": "eq",
                                        "right": 1,
                                        "message": "done",
                                    },
                                    "steps": [_provider_step(1)],
                                },
                                "as": "inner",
                            }
                        ],
                    },
                    "as": "outer",
                }
            ]
        )


def test_static_expansion_counts_repeat_provider_calls() -> None:
    definition = _definition(
        steps=[
            _provider_step(0),
            {
                "id": "retry",
                "repeat": {
                    "max_attempts": 4,
                    "until": {
                        "left": "$steps.provider_1.value",
                        "op": "eq",
                        "right": 1,
                        "message": "done",
                    },
                    "steps": [_provider_step(1), _assert_step(2)],
                },
                "as": "attempt",
            },
        ],
        budget={"wall_clock_s": 10, "max_provider_calls": 5},
    )

    assert definition.static_expansion().model_dump() == {
        "total_steps": 4,
        "expanded_steps": 10,
        "expanded_provider_calls": 5,
    }


def test_budget_refusal_carries_all_computed_numbers() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 4,
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [_provider_step(1)],
                    },
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 3},
        )

    message = str(exc_info.value)
    assert "total_steps=2" in message
    assert "expanded_steps=5" in message
    assert "expanded_provider_calls=4" in message
    assert "declared max_provider_calls=3" in message


def test_global_expanded_step_ceiling_is_enforced_with_counts() -> None:
    with pytest.raises(ValidationError, match="expanded_steps=501"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 25,
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [_assert_step(index) for index in range(20)],
                    },
                    "as": "attempt",
                }
            ]
        )


def test_global_expanded_provider_ceiling_is_enforced_with_counts() -> None:
    with pytest.raises(ValidationError, match="expanded_provider_calls=275"):
        _definition(
            steps=[
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 25,
                        "until": {"left": 1, "op": "eq", "right": 1, "message": "done"},
                        "steps": [_provider_step(index) for index in range(11)],
                    },
                    "as": "attempt",
                }
            ],
            budget={"wall_clock_s": 10, "max_provider_calls": 275},
        )


def test_global_stored_step_ceiling_is_enforced_with_counts() -> None:
    with pytest.raises(ValidationError, match="total_steps=101"):
        _definition(steps=[_assert_step(index) for index in range(101)])


def test_wall_clock_budget_is_required_and_capped() -> None:
    with pytest.raises(ValidationError, match="wall_clock_s"):
        _definition(budget={"max_provider_calls": 0})
    with pytest.raises(ValidationError, match="less than or equal to 600"):
        _definition(budget={"wall_clock_s": 601, "max_provider_calls": 0})
