"""Analysis 5 -- R9, connective-aware guard unsatisfiability (§3.5).

The refusal is deliberately SHALLOW, and the shallowness is the design. It
narrows a domain only through conjunctive dominating comparisons; `any_of`
contributes nothing because a disjunction narrows nothing, and `not_of`
contributes nothing because negating a range over an open domain is not a
range. What the fragment cannot decide is NOT refused -- fail-open on the
analysis, fail-closed on the semantics.

v1's "intersect dominating comparisons" had neither carve-out and would have
hard-refused valid definitions; each is pinned below by a case that must still
compile.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.analysis import build_procedure_graph, true_arm_dominators
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import service_lock
from cruxible_core.service.procedures import compile_procedure_definition

_CONFIG_YAML = """\
version: "1.0"
name: procedure_vacuity

entity_types:
  Task:
    properties:
      task_id:
        type: string
        primary_key: true

relationships: []

enums:
  Severity:
    values: [low, medium, high]

contracts:
  VacuityInput:
    fields:
      value:
        type: int
      severity:
        type: string
        enum_ref: Severity

providers:
  exported_action:
    kind: tool
    contract_in: cruxible.JsonObject
    contract_out: cruxible.JsonObject
    ref: https://example.invalid/action
    version: "1.0"
    runtime: http_json
    procedure_access: graph_write
    config:
      timeout_s: 5
"""


@pytest.fixture
def vacuity_instance(tmp_path: Any) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    service_lock(instance)
    return instance


def _definition(steps: list[dict[str, Any]], **overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "vacuity_probe",
        "contract_in": "VacuityInput",
        "steps": steps,
        "returns": "final",
        "precondition": {},
        "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
        "declared_tier": "graph_write",
        "graph_format": 2,
    }
    payload.update(overrides)
    return ProcedureDefinition.model_validate(payload)


def _tail() -> dict[str, Any]:
    return {
        "id": "tail",
        "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
        "as": "final",
    }


def _guard(step_id: str, guard: dict[str, Any], **edges: Any) -> dict[str, Any]:
    return {"id": step_id, "guard": guard, "message": f"{step_id} failed", **edges}


def _compile(instance: CruxibleInstance, definition: ProcedureDefinition) -> None:
    compile_procedure_definition(instance, definition)


def test_a_self_contradictory_all_of_is_refused(vacuity_instance: CruxibleInstance) -> None:
    definition = _definition(
        [
            _guard(
                "gate",
                {
                    "all_of": [
                        {"left": "$input.value", "op": "gte", "right": 10},
                        {"left": "$input.value", "op": "lt", "right": 5},
                    ]
                },
                on_true="tail",
                on_false="tail",
            ),
            _tail(),
        ]
    )
    with pytest.raises(ConfigError, match="statically unsatisfiable"):
        _compile(vacuity_instance, definition)


def test_a_dominating_true_arm_narrows_a_downstream_guard(
    vacuity_instance: CruxibleInstance,
) -> None:
    """The interval is intersected ACROSS nodes, which is the whole point.

    Neither guard is contradictory alone. On every path reaching the second,
    the first one's predicate held.
    """
    definition = _definition(
        [
            _guard("high", {"left": "$input.value", "op": "gt", "right": 100}, on_true="low"),
            _guard("low", {"left": "$input.value", "op": "lt", "right": 10}, on_true="tail"),
            _tail(),
        ]
    )
    with pytest.raises(ConfigError, match="guard step 'low' is statically unsatisfiable"):
        _compile(vacuity_instance, definition)


def test_the_refusal_cites_the_comparisons_and_the_nodes_they_came_from(
    vacuity_instance: CruxibleInstance,
) -> None:
    definition = _definition(
        [
            _guard("high", {"left": "$input.value", "op": "gt", "right": 100}, on_true="low"),
            _guard("low", {"left": "$input.value", "op": "lt", "right": 10}, on_true="tail"),
            _tail(),
        ]
    )
    with pytest.raises(ConfigError) as exc_info:
        _compile(vacuity_instance, definition)
    message = str(exc_info.value)
    assert "$input.value > 100 (at 'high')" in message
    assert "$input.value < 10 (at 'low')" in message


def test_a_false_arm_contributes_nothing(vacuity_instance: CruxibleInstance) -> None:
    """The false arm asserts a NEGATION, and negations are outside the fragment.

    `low` is reached only when `value > 100` is FALSE, so `value < 10` is
    perfectly satisfiable there. A checker that narrowed on the negated arm
    would still be sound here -- but the same reasoning applied to `not_of` is
    not, and the fragment is one rule, not two.
    """
    definition = _definition(
        [
            _guard(
                "high",
                {"left": "$input.value", "op": "gt", "right": 100},
                on_true="tail",
                on_false="low",
            ),
            _guard("low", {"left": "$input.value", "op": "lt", "right": 10}, on_true="tail"),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_any_of_contributes_nothing(vacuity_instance: CruxibleInstance) -> None:
    """A disjunction narrows nothing, so it can never make anything empty."""
    definition = _definition(
        [
            _guard(
                "gate",
                {
                    "any_of": [
                        {"left": "$input.value", "op": "gte", "right": 10},
                        {"left": "$input.value", "op": "lt", "right": 5},
                    ]
                },
                on_true="tail",
                on_false="tail",
            ),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_a_dominating_any_of_does_not_narrow_downstream(
    vacuity_instance: CruxibleInstance,
) -> None:
    definition = _definition(
        [
            _guard(
                "wide",
                {
                    "any_of": [
                        {"left": "$input.value", "op": "gt", "right": 100},
                        {"left": "$input.value", "op": "lt", "right": 0},
                    ]
                },
                on_true="low",
            ),
            _guard("low", {"left": "$input.value", "op": "lt", "right": 10}, on_true="tail"),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_not_of_contributes_nothing(vacuity_instance: CruxibleInstance) -> None:
    """Negating a range over an open domain is not a range."""
    definition = _definition(
        [
            _guard("high", {"left": "$input.value", "op": "gt", "right": 100}, on_true="gate"),
            _guard(
                "gate",
                {"not_of": {"left": "$input.value", "op": "gt", "right": 100}},
                on_true="tail",
                on_false="tail",
            ),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_two_different_equalities_on_one_reference_are_refused(
    vacuity_instance: CruxibleInstance,
) -> None:
    definition = _definition(
        [
            _guard(
                "first", {"left": "$input.severity", "op": "eq", "right": "high"}, on_true="second"
            ),
            _guard(
                "second", {"left": "$input.severity", "op": "eq", "right": "low"}, on_true="tail"
            ),
            _tail(),
        ]
    )
    with pytest.raises(ConfigError, match="guard step 'second' is statically unsatisfiable"):
        _compile(vacuity_instance, definition)


def test_a_value_outside_the_declared_enum_is_refused(
    vacuity_instance: CruxibleInstance,
) -> None:
    """The "declared enum sets" half of §3.5.

    `severity` is enum_ref'd to Severity, which has no `critical`. No caller
    can send one, so the arm can never be taken -- knowable at authoring time.
    """
    definition = _definition(
        [
            _guard(
                "gate",
                {"left": "$input.severity", "op": "eq", "right": "critical"},
                on_true="tail",
                on_false="tail",
            ),
            _tail(),
        ]
    )
    with pytest.raises(ConfigError, match="statically unsatisfiable"):
        _compile(vacuity_instance, definition)


def test_a_value_inside_the_declared_enum_compiles(
    vacuity_instance: CruxibleInstance,
) -> None:
    definition = _definition(
        [
            _guard(
                "gate",
                {"left": "$input.severity", "op": "eq", "right": "high"},
                on_true="tail",
                on_false="tail",
            ),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_different_references_are_never_intersected(
    vacuity_instance: CruxibleInstance,
) -> None:
    """Two names, two domains. Merging them would invent a contradiction.

    `count(rows, items)` needs a producer of `rows` before the guard reads it,
    so the shape-items step leads.
    """
    definition = _definition(
        [
            {
                "id": "rows",
                "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
                "as": "rows",
            },
            _guard("high", {"left": "$input.value", "op": "gt", "right": 100}, on_true="low"),
            _guard(
                "low",
                {"left": "count(rows, items)", "op": "lt", "right": 10},
                on_true="tail",
            ),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_a_constant_false_comparison_is_refused(vacuity_instance: CruxibleInstance) -> None:
    """The degenerate empty domain: no reference at all, and already decided."""
    definition = _definition(
        [
            _guard("gate", {"left": 1, "op": "eq", "right": 2}, on_true="tail", on_false="tail"),
            _tail(),
        ]
    )
    with pytest.raises(ConfigError, match="constantly false"):
        _compile(vacuity_instance, definition)


def test_a_constant_true_comparison_still_compiles(
    vacuity_instance: CruxibleInstance,
) -> None:
    """Vacuously TRUE is a specificity smell, not an unsatisfiable predicate."""
    definition = _definition(
        [
            _guard("gate", {"left": 1, "op": "eq", "right": 1}, on_true="tail", on_false="tail"),
            _tail(),
        ]
    )
    _compile(vacuity_instance, definition)


def test_converging_arms_imply_nothing_downstream() -> None:
    """A guard whose arms rejoin immediately asserts nothing at the join.

    Control arrives there under the predicate AND under its negation, so
    treating the predicate as implied would refuse a definition that runs.
    """
    definition = _definition(
        [
            _guard(
                "gate",
                {"left": "$input.value", "op": "gt", "right": 100},
                on_true="tail",
                on_false="tail",
            ),
            _tail(),
        ]
    )
    graph = build_procedure_graph(definition)
    assert true_arm_dominators(graph)["tail"] == ()


def test_a_guard_with_an_abort_arm_dominates_everything_after_it() -> None:
    """No false arm means no continuation: downstream runs only if it held."""
    definition = _definition(
        [
            _guard("gate", {"left": "$input.value", "op": "gt", "right": 100}),
            _tail(),
        ]
    )
    graph = build_procedure_graph(definition)
    assert true_arm_dominators(graph)["tail"] == ("gate",)
