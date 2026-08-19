"""The control graph: successors, R1/R2/R3/R15, and linear degeneracy."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.procedure.analysis import build_procedure_graph, procedure_node_kind
from cruxible_core.procedure.types import ProcedureDefinition

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 5}


def _definition(steps: list[dict[str, Any]], **overrides: Any) -> ProcedureDefinition:
    payload: dict[str, Any] = {
        "name": "graph_probe",
        "steps": steps,
        "returns": steps[0].get("as", steps[0].get("id", "rows")),
        "precondition": {},
        "budget": _BUDGET,
    }
    payload.update(overrides)
    return ProcedureDefinition.model_validate(payload)


def _provider(step_id: str, alias: str) -> dict[str, Any]:
    return {"id": step_id, "provider": "scorer", "input": {}, "as": alias}


def test_a_linear_definition_degenerates_to_the_path_graph() -> None:
    definition = _definition([_provider("a", "ra"), _provider("b", "rb"), _provider("c", "rc")])
    graph = build_procedure_graph(definition)
    assert graph.successors == {"a": ("b",), "b": ("c",), "c": ()}
    assert graph.entry_id == "a"


def test_linear_availability_is_the_prior_alias_walk() -> None:
    """The regression obligation, stated as an equality rather than a promise.

    The right-hand side used to be read off the workflow compiler's
    ``_prior_step_aliases_by_index``. That donor left in PC-F, so the walk is
    now written out: on a path graph, MUST-availability degenerates to exactly
    the aliases produced by strictly earlier steps.
    """
    definition = _definition([_provider("a", "ra"), _provider("b", "rb"), _provider("c", "rc")])
    graph = build_procedure_graph(definition)
    assert [graph.available_aliases[node_id] for node_id in graph.node_ids] == [
        frozenset(),
        frozenset({"ra"}),
        frozenset({"ra", "rb"}),
    ]


def _branching_definition(**overrides: Any) -> ProcedureDefinition:
    return _definition(
        [
            _provider("read", "rows"),
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "on_true": "hot",
                "on_false": "cold",
                "message": "no rows",
            },
            {"step": _provider("hot", "decision"), "next": "tail"},
            _provider("cold", "decision"),
            _provider("tail", "final"),
        ],
        graph_format=2,
        returns="final",
        **overrides,
    )


def test_a_guard_has_two_successors_and_both_arms_converge() -> None:
    graph = build_procedure_graph(_branching_definition())
    assert graph.successors["gate"] == ("hot", "cold")
    assert graph.successors["hot"] == ("tail",)
    assert graph.successors["cold"] == ("tail",)
    assert graph.kinds["gate"] == "guard"


def test_an_alias_produced_on_one_arm_only_is_not_available_after_the_join() -> None:
    """MUST-dataflow, not MAY.

    Both arms produce `decision`, so it survives the join. Nothing either arm
    produced privately would.
    """
    graph = build_procedure_graph(_branching_definition())
    assert "decision" in graph.available_aliases["tail"]
    assert graph.available_aliases["gate"] == frozenset({"rows"})


def test_an_alias_produced_on_only_one_arm_does_not_survive_the_join() -> None:
    definition = _definition(
        [
            _provider("read", "rows"),
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "on_true": "hot",
                "on_false": "cold",
                "message": "no rows",
            },
            {"step": _provider("hot", "hot_only"), "next": "tail"},
            _provider("cold", "cold_only"),
            _provider("tail", "final"),
        ],
        graph_format=2,
        returns="final",
    )
    graph = build_procedure_graph(definition)
    assert graph.available_aliases["tail"] == frozenset({"rows"})


def test_r1_a_control_edge_to_an_unknown_step_is_refused() -> None:
    definition = _definition(
        [
            {"step": _provider("read", "rows"), "next": "nowhere"},
            _provider("tail", "final"),
        ],
        graph_format=2,
    )
    with pytest.raises(ConfigError, match="targets 'nowhere', which is not a step"):
        build_procedure_graph(definition)


def test_r2_a_back_edge_is_refused() -> None:
    definition = _definition(
        [
            _provider("read", "rows"),
            {"step": _provider("middle", "mid"), "next": "read"},
            _provider("tail", "final"),
        ],
        graph_format=2,
    )
    with pytest.raises(ConfigError, match="at or before it in the step list"):
        build_procedure_graph(definition)


def test_r3_an_unreachable_step_is_refused() -> None:
    definition = _definition(
        [
            {"step": _provider("read", "rows"), "next": "tail"},
            _provider("orphan", "never"),
            _provider("tail", "final"),
        ],
        graph_format=2,
        returns="final",
    )
    with pytest.raises(ConfigError, match=r"\['orphan'\] are unreachable"):
        build_procedure_graph(definition)


def test_r15_two_producers_of_one_alias_on_the_same_path_are_refused() -> None:
    definition = _definition(
        [_provider("a", "rows"), _provider("b", "rows")],
        graph_format=2,
        returns="rows",
    )
    with pytest.raises(ConfigError, match="already produced on at least one path"):
        build_procedure_graph(definition)


def test_r15_uses_may_reachability_not_must_availability() -> None:
    """The collision a MUST-availability check calls legal.

    The true arm produces `x` and the false arm produces `y`, so the
    intersection at the join drops BOTH -- and a node after the join that also
    produces `x` looks clean, while the true path plainly carries two
    producers of it and the second silently overwrites the first. Duplicate
    production is a MAY question; reference validity is a MUST question. They
    are not the same analysis.
    """
    definition = _definition(
        [
            _provider("read", "rows"),
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "on_true": "hot",
                "on_false": "cold",
                "message": "no rows",
            },
            {"step": _provider("hot", "decision"), "next": "tail"},
            _provider("cold", "other"),
            _provider("tail", "decision"),
        ],
        graph_format=2,
        returns="decision",
    )
    with pytest.raises(ConfigError, match="already produced on at least one path"):
        build_procedure_graph(definition)


def test_reference_validity_still_uses_must_availability() -> None:
    """The two analyses are kept apart, not merged.

    `decision` is produced on both arms, so it IS available after the join;
    `other` is produced on one, so it is not. May-reachability would have said
    both were fine.
    """
    definition = _definition(
        [
            _provider("read", "rows"),
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "on_true": "hot",
                "on_false": "cold",
                "message": "no rows",
            },
            {"step": _provider("hot", "decision"), "next": "tail"},
            _provider("cold", "decision"),
            _provider("tail", "final"),
        ],
        graph_format=2,
        returns="final",
    )
    graph = build_procedure_graph(definition)
    assert graph.available_aliases["tail"] == frozenset({"rows", "decision"})
    assert graph.reachable_aliases["tail"] == frozenset({"rows", "decision"})


def test_r15_does_not_retroactively_refuse_a_format_v1_definition() -> None:
    """v1 identity is frozen, and that includes what v1 accepted.

    A stored v1 definition with a duplicate alias compiles today. Refusing it
    now would be a behaviour change on shipped instances dressed as a new
    analysis; convergence onto the stricter rule happens through the governed
    re-acceptance sweep instead.
    """
    definition = _definition([_provider("a", "rows"), _provider("b", "rows")], returns="rows")
    graph = build_procedure_graph(definition)
    assert graph.produced_alias == {"a": "rows", "b": "rows"}


def test_a_guard_without_an_on_false_arm_aborts() -> None:
    definition = _definition(
        [
            _provider("read", "rows"),
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "message": "no rows",
            },
            _provider("tail", "final"),
        ],
        graph_format=2,
        returns="final",
    )
    graph = build_procedure_graph(definition)
    assert graph.edges["gate"] == {"on_false": "$abort", "on_true": "tail"}
    assert graph.successors["gate"] == ("tail",)


def test_node_kinds_read_through_the_wrapper() -> None:
    definition = _branching_definition()
    assert procedure_node_kind(definition.steps[2]) == "provider"
    assert procedure_node_kind(definition.steps[1]) == "guard"
