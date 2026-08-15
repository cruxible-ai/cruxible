"""Analysis 7 -- capped path enumeration on the reviewer surfaces (§3.1).

The one exponential analysis, and the one no correctness check may consult.
It exists because authorising a branching definition is authorising its
BEHAVIOURS, and the step list stops describing those the moment a second path
appears.
"""

from __future__ import annotations

from typing import Any

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.procedure.analysis import build_procedure_graph, enumerate_control_paths
from cruxible_core.procedure.types import (
    MAX_PROCEDURE_ENUMERATED_PATHS,
    ProcedureDefinition,
)
from cruxible_core.service import (
    service_accept_procedure,
    service_get_procedure_details,
    service_propose_procedure,
)
from tests.test_procedures.conftest import actor

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 0}


def _definition(steps: list[dict[str, Any]], **overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "enumeration_probe",
        "steps": steps,
        "returns": "final",
        "precondition": {},
        "budget": _BUDGET,
        "graph_format": 2,
    }
    payload.update(overrides)
    if payload.get("graph_format") is None:
        payload.pop("graph_format", None)
    return ProcedureDefinition.model_validate(payload)


def _shape(step_id: str, alias: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
        "as": alias,
    }


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


def test_a_linear_definition_has_exactly_one_path() -> None:
    definition = _definition([_shape("a", "ra"), _shape("tail", "final")], graph_format=None)
    paths, truncated = enumerate_control_paths(build_procedure_graph(definition))
    assert paths == (("a", "tail"),)
    assert truncated is False


def test_both_arms_of_a_guard_are_enumerated() -> None:
    definition = _definition(
        [
            _guard("gate", on_true="hot", on_false="cold"),
            {"step": _shape("hot", "hot_out"), "next": "tail"},
            _shape("cold", "cold_out"),
            _shape("tail", "final"),
        ]
    )
    paths, truncated = enumerate_control_paths(build_procedure_graph(definition))
    assert paths == (("gate", "hot", "tail"), ("gate", "cold", "tail"))
    assert truncated is False


def test_the_enumeration_order_is_deterministic() -> None:
    """`on_true` before `on_false` before `next`, on every call."""
    definition = _definition(
        [
            _guard("gate", on_true="hot", on_false="cold"),
            {"step": _shape("hot", "hot_out"), "next": "tail"},
            _shape("cold", "cold_out"),
            _shape("tail", "final"),
        ]
    )
    graph = build_procedure_graph(definition)
    assert enumerate_control_paths(graph) == enumerate_control_paths(graph)


def test_an_abort_arm_is_not_a_path() -> None:
    """`$abort` terminates the run; it is not a successor and not an exit."""
    definition = _definition(
        [
            _guard("gate", on_true="tail"),
            _shape("tail", "final"),
        ]
    )
    paths, _ = enumerate_control_paths(build_procedure_graph(definition))
    assert paths == (("gate", "tail"),)


def _fan_out(count: int) -> ProcedureDefinition:
    """`count` independent guards in series: 2**count paths."""
    steps: list[dict[str, Any]] = []
    for index in range(count):
        steps.append(_guard(f"g{index}", on_true=f"skip{index}", on_false=f"take{index}"))
        steps.append({"step": _shape(f"take{index}", f"t{index}"), "next": f"skip{index}"})
        steps.append(_shape(f"skip{index}", f"s{index}"))
    steps.append(_shape("tail", "final"))
    return _definition(steps)


def test_the_cap_truncates_and_says_so() -> None:
    """Seven independent guards make 128 paths; the cap is 64."""
    paths, truncated = enumerate_control_paths(build_procedure_graph(_fan_out(7)))
    assert len(paths) == MAX_PROCEDURE_ENUMERATED_PATHS
    assert truncated is True


def test_an_uncapped_enumeration_is_not_silently_truncated() -> None:
    paths, truncated = enumerate_control_paths(build_procedure_graph(_fan_out(4)))
    assert len(paths) == 16
    assert truncated is False


def test_the_cap_is_never_consulted_by_a_correctness_check() -> None:
    """A definition with more paths than the display cap still compiles.

    The cap is display-only. If any refusal read it, a reviewable definition
    would become unproposable at 65 paths -- a ceiling nobody declared.
    """
    definition = _fan_out(7)
    assert definition.static_expansion().expanded_provider_calls == 0


def test_the_details_surface_carries_the_enumeration(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "enumeration_surface",
            "contract_in": "ProcedureInput",
            "graph_format": 2,
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": "$input.value", "op": "gt", "right": 0},
                    "on_true": "hot",
                    "on_false": "cold",
                    "message": "no value",
                },
                {"step": _shape("hot", "hot_out"), "next": "tail"},
                _shape("cold", "cold_out"),
                _shape("tail", "final"),
            ],
            "returns": "final",
            "precondition": {},
            "budget": _BUDGET,
            "declared_tier": "graph_write",
        }
    )
    proposed = service_propose_procedure(
        procedure_instance, definition, actor_context=actor("proposer")
    )
    service_accept_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )
    details = service_get_procedure_details(procedure_instance, proposed.procedure.procedure_id)
    assert details.control_paths is not None
    assert details.control_paths.paths == [
        ["gate", "hot", "tail"],
        ["gate", "cold", "tail"],
    ]
    assert details.control_paths.truncated is False
    assert details.control_paths.cap == MAX_PROCEDURE_ENUMERATED_PATHS
