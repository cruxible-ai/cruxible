"""Analysis 3 -- the worst-case budget as a longest path (§3.3), and R11.

The change this file pins is one word: the expanded counts are a MAX over
control paths, not a SUM over the body. Under branching a sum charges one
execution for work no execution does, so the three-arm shape the whole graph
feature exists to express would be refused by the very ceiling that is meant
to bound it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.procedure.analysis import worst_case_expansion
from cruxible_core.procedure.types import (
    MAX_PROCEDURE_BRANCH_NODES,
    ProcedureDefinition,
)

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 5}


def _definition(steps: list[dict[str, Any]], **overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "budget_probe",
        "steps": steps,
        "returns": "final",
        "precondition": {},
        "budget": _BUDGET,
        "graph_format": 2,
    }
    payload.update(overrides)
    if payload.get("graph_format") is None:
        # Format v1 is spelled by ABSENCE, key included.
        payload.pop("graph_format", None)
    return ProcedureDefinition.model_validate(payload)


def _provider(step_id: str, alias: str, *, next_id: str | None = None) -> dict[str, Any]:
    step = {"id": step_id, "provider": "scorer", "input": {}, "as": alias}
    return step if next_id is None else {"step": step, "next": next_id}


def _guard(step_id: str, *, on_true: str, on_false: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": step_id,
        "guard": {"left": "$input.value", "op": "gt", "right": 0},
        "on_true": on_true,
        "message": f"{step_id} failed",
    }
    if on_false is not None:
        node["on_false"] = on_false
    return node


def _three_arm_definition(**overrides: Any) -> ProcedureDefinition:
    """One guard chain fanning out to three mutually exclusive provider arms."""
    return _definition(
        [
            _guard("triage", on_true="hot", on_false="second"),
            _guard("second", on_true="warm", on_false="cold"),
            _provider("hot", "hot_call", next_id="tail"),
            _provider("warm", "warm_call", next_id="tail"),
            _provider("cold", "cold_call"),
            {
                "id": "tail",
                "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
                "as": "final",
            },
        ],
        **overrides,
    )


def test_three_mutually_exclusive_arms_cost_one_arm_not_three() -> None:
    expansion = _three_arm_definition().static_expansion()
    assert expansion.expanded_provider_calls == 1
    # The stored body still counts every arm: total_steps is not a path
    # property and does not become a max.
    assert expansion.total_steps == 6


def test_the_witness_path_names_one_real_execution() -> None:
    expansion = _three_arm_definition().static_expansion()
    assert expansion.expanded_provider_calls_path == ("triage", "hot", "tail")
    # And already NOT the same path: the longest arm runs one more guard than
    # the arm that spends the most provider calls.
    assert expansion.expanded_steps_path == ("triage", "second", "warm", "tail")


def test_a_budget_the_sum_would_have_refused_is_accepted() -> None:
    """The regression the sum caused, stated as the shape it broke.

    Three arms, one provider call each, a budget of one call: every execution
    makes exactly one call and the definition is correct. Summing charges it
    three and refuses it.
    """
    definition = _three_arm_definition(budget={"wall_clock_s": 30, "max_provider_calls": 1})
    assert definition.static_expansion().expanded_provider_calls == 1


def test_the_two_maxima_are_maxima_of_different_weightings() -> None:
    """The heaviest path and the longest path are not the same path.

    One arm runs a five-attempt repeat with no provider in it; the other makes
    a single provider call. Reporting one path's node list beside the other
    path's count would describe an execution that does not exist.
    """
    definition = _definition(
        [
            _guard("gate", on_true="loop", on_false="call"),
            _provider("call", "called", next_id="tail"),
            {
                "id": "loop",
                "as": "looped",
                "repeat": {
                    "max_attempts": 5,
                    "until": {
                        "left": "$steps.probe.value",
                        "op": "gte",
                        "right": 0,
                        "message": "settled",
                    },
                    "steps": [
                        {
                            "id": "probe",
                            "shape_items": {
                                "items": [{"value": 1}],
                                "fields": {"value": "$item.value"},
                            },
                            "as": "probe",
                        },
                        {
                            "id": "check",
                            "assert": {
                                "left": "$steps.probe.value",
                                "op": "gte",
                                "right": 0,
                                "message": "non-negative",
                            },
                        },
                    ],
                },
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
                "as": "final",
            },
        ]
    )
    expansion = definition.static_expansion()
    assert expansion.expanded_steps == 13
    assert expansion.expanded_steps_path == ("gate", "loop", "tail")
    assert expansion.expanded_provider_calls == 1
    assert expansion.expanded_provider_calls_path == ("gate", "call", "tail")


def test_an_aborting_arm_carries_no_downstream_weight() -> None:
    """`$abort` terminates; nothing accumulates past it."""
    definition = _definition(
        [
            _guard("gate", on_true="call", on_false="$abort"),
            _provider("call", "called"),
            {
                "id": "tail",
                "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
                "as": "final",
            },
        ]
    )
    expansion = definition.static_expansion()
    assert expansion.expanded_provider_calls == 1
    assert expansion.expanded_provider_calls_path == ("gate", "call", "tail")


def test_a_linear_definition_maximum_equals_the_sum() -> None:
    """Linearity is where the max and the sum agree, by construction.

    The corpus-wide statement of this is T4; this is its smallest witness.
    """
    definition = _definition(
        [
            _provider("a", "ra"),
            _provider("b", "rb"),
            {
                "id": "tail",
                "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
                "as": "final",
            },
        ],
        graph_format=None,
    )
    expansion = definition.static_expansion()
    assert expansion.expanded_provider_calls == 2
    assert expansion.expanded_steps == 3
    assert expansion.expanded_provider_calls_path == ("a", "b", "tail")


def test_a_definition_with_a_back_edge_falls_back_to_the_sum() -> None:
    """No topological order, so no maximum -- and the sum over-approximates it.

    The compiler refuses the back edge (R2) a moment later. Until then the
    ceilings must not be made LOOSER by a graph the analysis cannot walk, so
    the fallback is the sum rather than a partial DP result.
    """
    definition = _definition(
        [
            _provider("a", "ra"),
            _provider("b", "rb", next_id="a"),
            {
                "id": "tail",
                "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
                "as": "final",
            },
        ]
    )
    expansion = worst_case_expansion(definition.steps)
    assert expansion.expanded_provider_calls.count == 2
    assert expansion.expanded_provider_calls.path == ("a", "b", "tail")


def _guard_chain(count: int) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        _guard(f"g{index}", on_true=f"g{index + 1}" if index + 1 < count else "tail")
        for index in range(count)
    ]
    steps.append(
        {
            "id": "tail",
            "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
            "as": "final",
        }
    )
    return steps


def test_r11_the_branch_node_ceiling_is_enforced() -> None:
    with pytest.raises(ValidationError, match="branch-node ceiling is 12"):
        _definition(_guard_chain(MAX_PROCEDURE_BRANCH_NODES + 1))


def test_r11_admits_a_definition_at_the_ceiling() -> None:
    definition = _definition(_guard_chain(MAX_PROCEDURE_BRANCH_NODES))
    assert definition.static_expansion().expanded_provider_calls == 0


def test_the_expansion_refusal_names_the_path_that_blew_the_ceiling() -> None:
    """R12's message gains the witness (§3.3).

    A bare `expanded_provider_calls=2` on a branching definition leaves the
    author to guess which arm spends it.
    """
    with pytest.raises(ValidationError) as exc_info:
        _definition(
            [
                _guard("gate", on_true="hot", on_false="cold"),
                _provider("hot", "hot_call", next_id="tail"),
                _provider("cold", "cold_call"),
                {
                    "id": "tail",
                    "provider": "scorer",
                    "input": {},
                    "as": "final",
                },
            ],
            budget={"wall_clock_s": 30, "max_provider_calls": 1},
        )
    message = str(exc_info.value)
    assert "expanded_provider_calls=2 on path gate -> hot -> tail" in message
    assert "declared max_provider_calls=1" in message
