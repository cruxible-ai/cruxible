"""Parse-time refusals for the graph node kinds (R16, R17) and their shapes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureFlowStepSchema,
    ProcedureGuardStepSchema,
    unwrap_procedure_step,
)

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 2}


def _definition(steps: list[dict[str, Any]], **overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "graph_node_probe",
        "steps": steps,
        "returns": "rows",
        "precondition": {},
        "budget": _BUDGET,
        "graph_format": 2,
    }
    payload.update(overrides)
    return ProcedureDefinition.model_validate(payload)


def test_a_guard_node_parses_with_two_labelled_successors() -> None:
    definition = _definition(
        [
            {"id": "read", "provider": "scorer", "input": {}, "as": "rows"},
            {
                "id": "gate",
                "guard": {"left": "$steps.rows.count", "op": "gt", "right": 0},
                "on_true": "build",
                "on_false": "$abort",
                "message": "no rows",
            },
            {"id": "build", "provider": "scorer", "input": {}, "as": "built"},
        ]
    )
    guard = definition.steps[1]
    assert isinstance(guard, ProcedureGuardStepSchema)
    assert (guard.on_true, guard.on_false) == ("build", "$abort")


def test_a_flow_wrapper_takes_its_identity_and_alias_from_the_wrapped_step() -> None:
    definition = _definition(
        [
            {
                "step": {"id": "read", "provider": "scorer", "input": {}, "as": "rows"},
                "next": "tail",
            },
            {"id": "tail", "provider": "scorer", "input": {}, "as": "tail_rows"},
        ]
    )
    wrapper = definition.steps[0]
    assert isinstance(wrapper, ProcedureFlowStepSchema)
    assert wrapper.id == "read"
    assert wrapper.as_ == "rows"
    assert unwrap_procedure_step(wrapper) is wrapper.step
    # The alias is visible to definition-level analyses that read step aliases.
    assert definition.referenced_providers() == {"scorer"}


@pytest.mark.parametrize(
    "smuggled",
    [
        {"apply_all": {"entities_from": ["rows"]}},
        {
            "propose_relationship_group": {
                "relationship_type": "R",
                "candidates_from": "rows",
                "signals_from": ["rows"],
            }
        },
        {
            "make_entities": {
                "entity_type": "Task",
                "items": "$steps.rows",
                "entity_id": "$item.id",
            }
        },
        {"apply_entities": {"entities_from": "rows"}},
    ],
    ids=["apply_all", "propose_relationship_group", "make_entities", "apply_entities"],
)
def test_r16_a_wrapper_cannot_smuggle_an_excluded_kind_past_the_whitelist(
    smuggled: dict[str, Any],
) -> None:
    """The refusal is at PARSE, by type -- not a downstream compile check.

    The top-level whitelist tests ``isinstance(step, WorkflowStepSchema)``, and
    a wrapped step is not one, so without the typed inner slot the wrapper is a
    hole straight through the procedure step subset into the write kinds.
    """
    with pytest.raises(ValidationError, match="disallowed kind"):
        _definition(
            [
                {"step": {"id": "smuggle", **smuggled, "as": "rows"}, "next": "tail"},
                {"id": "tail", "provider": "scorer", "input": {}, "as": "tail_rows"},
            ]
        )


def test_r16_the_wrapper_still_admits_the_ruled_subset() -> None:
    definition = _definition(
        [
            {"step": {"id": "read", "query": "named_query", "as": "rows"}, "next": "tail"},
            {"id": "tail", "provider": "scorer", "input": {}, "as": "tail_rows"},
        ]
    )
    assert definition.steps[0].id == "read"


@pytest.mark.parametrize(
    "nested",
    [
        {"id": "gate", "guard": {"left": 1, "op": "eq", "right": 1}, "message": "m"},
        {"step": {"id": "wrapped", "provider": "scorer", "input": {}}, "next": "x"},
        {"id": "attempt", "provider": "scorer", "input": {}, "as": "attempt", "next": "x"},
    ],
    ids=["guard", "wrapper", "control-edge"],
)
def test_r17_a_repeat_body_refuses_graph_nodes_and_wrappers(nested: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="repeat bodies may not contain graph nodes"):
        _definition(
            [
                {
                    "id": "loop",
                    "as": "rows",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {
                            "left": "$steps.attempt.done",
                            "op": "eq",
                            "right": True,
                            "message": "not done",
                        },
                        "steps": [nested],
                    },
                }
            ]
        )


def test_a_guard_publishes_no_alias_so_evidence_outputs_cannot_name_it() -> None:
    with pytest.raises(ValidationError, match="evidence_outputs references unknown step aliases"):
        _definition(
            [
                {"id": "read", "provider": "scorer", "input": {}, "as": "rows"},
                {
                    "id": "gate",
                    "guard": {"left": "$steps.rows.count", "op": "gt", "right": 0},
                    "message": "no rows",
                },
            ],
            evidence_outputs=["gate"],
        )


def test_a_guard_costs_one_step_and_no_provider_call() -> None:
    definition = _definition(
        [
            {"id": "read", "provider": "scorer", "input": {}, "as": "rows"},
            {
                "id": "gate",
                "guard": {"left": "$steps.rows.count", "op": "gt", "right": 0},
                "message": "no rows",
            },
        ]
    )
    expansion = definition.static_expansion()
    assert (expansion.total_steps, expansion.expanded_provider_calls) == (2, 1)


def test_an_undeclared_guard_node_is_refused_by_the_discriminator() -> None:
    """R13, reached through a real construct rather than a test-only registration."""
    with pytest.raises(Exception, match="does not declare 'graph_format: 2'"):
        ProcedureDefinition.model_validate(
            {
                "name": "undeclared_graph",
                "steps": [
                    {"id": "read", "provider": "scorer", "input": {}, "as": "rows"},
                    {
                        "id": "gate",
                        "guard": {"left": "$steps.rows.count", "op": "gt", "right": 0},
                        "message": "no rows",
                    },
                ],
                "returns": "rows",
                "precondition": {},
                "budget": _BUDGET,
            }
        )
