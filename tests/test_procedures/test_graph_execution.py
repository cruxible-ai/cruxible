"""Branch execution: the successor walk, arm selection, abort, and the unwrap."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import service_run_procedure
from cruxible_core.service.procedures import compile_procedure_definition
from tests.test_procedures.test_execution import _accept, _receipt, _stub_provider

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 5}


def _definition(name: str, steps: list[dict[str, Any]], returns: str) -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "name": name,
            "contract_in": "ProcedureInput",
            "steps": steps,
            "returns": returns,
            "precondition": {},
            "budget": _BUDGET,
            "declared_tier": "graph_write",
            "graph_format": 2,
        }
    )


def _branching(name: str) -> ProcedureDefinition:
    return _definition(
        name,
        [
            {
                "id": "read",
                "provider": "exported_action",
                "input": {"value": "$input.value"},
                "as": "rows",
            },
            {
                "id": "gate",
                "guard": {"left": "$steps.rows.value", "op": "gte", "right": 10},
                "on_true": "hot",
                "on_false": "cold",
                "message": "value below threshold",
            },
            {
                "step": {
                    "id": "hot",
                    "shape_items": {"items": [{"arm": "hot"}], "fields": {"arm": "$item.arm"}},
                    "as": "decision",
                },
                "next": "tail",
            },
            {
                "id": "cold",
                "shape_items": {"items": [{"arm": "cold"}], "fields": {"arm": "$item.arm"}},
                "as": "decision",
            },
            {
                "id": "tail",
                "shape_items": {"items": "$steps.decision.items", "fields": {"arm": "$item.arm"}},
                "as": "result",
            },
        ],
        returns="result",
    )


@pytest.mark.parametrize(
    ("value", "expected_arm"),
    [(50, "hot"), (1, "cold")],
    ids=["true-arm", "false-arm"],
)
def test_a_run_takes_one_arm_and_skips_the_other(
    value: int,
    expected_arm: str,
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    procedure_id = _accept(procedure_instance, _branching(f"branch_{expected_arm}"))
    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        input_payload={"value": value},
        actor_context=None,
    )
    assert result.output["items"][0]["arm"] == expected_arm
    # The unvisited arm produced nothing at all -- the walk did not run it.
    receipt = _receipt(procedure_instance, result.run.receipt_id or "")
    executed_ids = {
        node.detail.get("step_id") for node in receipt.nodes if node.node_type == "plan_step"
    }
    assert expected_arm in executed_ids
    assert ("cold" if expected_arm == "hot" else "hot") not in executed_ids


def test_the_receipt_records_every_operand_and_the_arm_taken(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log sufficiency: a branch nobody can explain is not auditable."""
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    procedure_id = _accept(procedure_instance, _branching("branch_receipt"))
    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        input_payload={"value": 50},
        actor_context=None,
    )
    receipt = _receipt(procedure_instance, result.run.receipt_id or "")
    guard_nodes = [
        node
        for node in receipt.nodes
        if node.node_type == "plan_step" and node.detail.get("guard") == "guard"
    ]
    assert len(guard_nodes) == 1
    detail = guard_nodes[0].detail
    assert detail["arm"] == "on_true"
    assert detail["target"] == "hot"
    assert detail["comparisons"] == [{"left": 50, "op": "gte", "right": 10, "passed": True}]


def test_a_false_arm_that_aborts_fails_the_run_with_the_guard_message(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    definition = _definition(
        "branch_abort",
        [
            {
                "id": "read",
                "provider": "exported_action",
                "input": {"value": "$input.value"},
                "as": "rows",
            },
            {
                "id": "gate",
                "guard": {"left": "$steps.rows.value", "op": "gte", "right": 10},
                "on_false": "$abort",
                "message": "refused: value below threshold",
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"ok": True}], "fields": {"ok": "$item.ok"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    procedure_id = _accept(procedure_instance, definition)
    with pytest.raises(QueryExecutionError, match="refused: value below threshold"):
        service_run_procedure(
            procedure_instance,
            procedure_id,
            input_payload={"value": 1},
            actor_context=None,
        )


def test_short_circuit_free_connectives_record_every_comparison(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    definition = _definition(
        "branch_all_of",
        [
            {
                "id": "read",
                "provider": "exported_action",
                "input": {"value": "$input.value"},
                "as": "rows",
            },
            {
                "id": "gate",
                # A SATISFIABLE conjunction that the run's input still fails on
                # its first member. It used to be `>= 10 and <= 0`, which R9
                # now refuses at compile as statically unsatisfiable -- and
                # correctly: this test is about what the receipt records, not
                # about admitting a branch that can never be taken.
                "guard": {
                    "all_of": [
                        {"left": "$steps.rows.value", "op": "gte", "right": 10},
                        {"left": "$steps.rows.value", "op": "lte", "right": 100},
                    ]
                },
                "on_false": "tail",
                "on_true": "tail",
                "message": "outside the band",
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"ok": True}], "fields": {"ok": "$item.ok"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    procedure_id = _accept(procedure_instance, definition)
    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        input_payload={"value": 1},
        actor_context=None,
    )
    receipt = _receipt(procedure_instance, result.run.receipt_id or "")
    guard = next(node for node in receipt.nodes if node.detail.get("guard") == "guard")
    # The first comparison already decided `all_of`; the second is evaluated and
    # recorded anyway, because the receipt has to say what the run saw.
    assert len(guard.detail["comparisons"]) == 2


def test_a_flow_wrapped_step_compiles_like_the_bare_step_plus_its_edge(
    procedure_instance: CruxibleInstance,
) -> None:
    """The AB6 test obligation: the gap that would otherwise compile to nothing.

    The compile loop reads shared fields directly off each step. A wrapper has
    only `step` and `next`, so without the unwrap every one of those reads
    misses and the step compiles to nothing at all.
    """
    bare = _definition(
        "wrapper_bare",
        [
            {
                "id": "read",
                "provider": "exported_action",
                "input": {"value": "$input.value"},
                "as": "rows",
            },
            {
                "id": "tail",
                "shape_items": {"items": "$steps.rows.items", "fields": {"v": "$item.v"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    wrapped = _definition(
        "wrapper_wrapped",
        [
            {
                "step": {
                    "id": "read",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "rows",
                },
                "next": "tail",
            },
            {
                "id": "tail",
                "shape_items": {"items": "$steps.rows.items", "fields": {"v": "$item.v"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    bare_plan = compile_procedure_definition(procedure_instance, bare)
    wrapped_plan = compile_procedure_definition(procedure_instance, wrapped)
    bare_step = bare_plan.steps[0].model_dump(mode="json")
    wrapped_step = wrapped_plan.steps[0].model_dump(mode="json")
    assert wrapped_step.pop("next_step_id") == "tail"
    assert "next_step_id" not in bare_step
    assert wrapped_step == bare_step
    # And the wrapped step's alias is visible to the later reference.
    assert wrapped_plan.steps[1].shape_items_spec is not None


def test_a_compiled_linear_plan_carries_no_graph_fields(
    procedure_instance: CruxibleInstance,
) -> None:
    """Compiled plans of linear procedures are byte-unchanged."""
    definition = ProcedureDefinition.model_validate(
        {
            "name": "linear_plan",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "r",
                }
            ],
            "returns": "r",
            "precondition": {},
            "budget": _BUDGET,
            "declared_tier": "graph_write",
        }
    )
    plan = compile_procedure_definition(procedure_instance, definition)
    dumped = plan.steps[0].model_dump(mode="json")
    for field in (
        "guard_spec",
        "guard_message",
        "on_true_step_id",
        "on_false_step_id",
        "next_step_id",
    ):
        assert field not in dumped


def test_a_governed_parameter_operand_is_refused_at_compile(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = _definition(
        "param_guard",
        [
            {
                "id": "read",
                "provider": "exported_action",
                "input": {"value": "$input.value"},
                "as": "rows",
            },
            {
                "id": "gate",
                "guard": {
                    "left": "$steps.rows.value",
                    "op": "gte",
                    "right": "@param:kev_threshold",
                },
                "on_false": "$abort",
                "message": "below threshold",
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"ok": True}], "fields": {"ok": "$item.ok"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    with pytest.raises(ConfigError, match="Governed scalar parameters are not available"):
        compile_procedure_definition(procedure_instance, definition)


@pytest.mark.parametrize(
    ("operand", "message"),
    [
        ("count(missing, items)", "reads step alias 'missing'"),
        ("truncated(missing)", "reads step alias 'missing'"),
        ("$steps.missing.value", "reads step alias 'missing'"),
        ("exists($steps.missing)", "reads step alias 'missing'"),
    ],
)
def test_a_guard_reading_an_unproduced_alias_is_refused_at_compile(
    operand: str,
    message: str,
    procedure_instance: CruxibleInstance,
) -> None:
    """A first-node guard reading an alias nothing produced used to compile
    clean, accept clean, and fail at RUN -- the outcome acceptance exists to
    rule out."""
    definition = _definition(
        "unbound_guard",
        [
            {
                "id": "gate",
                "guard": {"left": operand, "op": "eq", "right": 0},
                "on_false": "$abort",
                "message": "unbound",
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"ok": True}], "fields": {"ok": "$item.ok"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    with pytest.raises(ConfigError, match=message):
        compile_procedure_definition(procedure_instance, definition)


def test_a_guard_reading_an_alias_produced_on_only_one_arm_is_refused(
    procedure_instance: CruxibleInstance,
) -> None:
    """MUST-availability is the right question for a guard operand.

    Reading an alias only one incoming arm produced would fail at run time on
    the arm that did not.
    """
    definition = _definition(
        "one_armed_alias",
        [
            {"id": "read", "provider": "exported_action", "input": {}, "as": "rows"},
            {
                "id": "split",
                "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                "on_true": "hot",
                "on_false": "join",
                "message": "split",
            },
            {
                "step": {
                    "id": "hot",
                    "provider": "exported_action",
                    "input": {},
                    "as": "hot_only",
                },
                "next": "join",
            },
            {
                "id": "join",
                "guard": {"left": "count(hot_only, items)", "op": "gt", "right": 0},
                "on_false": "$abort",
                "message": "reads a one-armed alias",
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"ok": True}], "fields": {"ok": "$item.ok"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    with pytest.raises(ConfigError, match="not produced on every path reaching it"):
        compile_procedure_definition(procedure_instance, definition)


def test_a_guard_reading_a_properly_bound_alias_compiles(
    procedure_instance: CruxibleInstance,
) -> None:
    """The binding is a boundary, not a wall."""
    definition = _definition(
        "bound_guard",
        [
            {"id": "read", "provider": "exported_action", "input": {}, "as": "rows"},
            {
                "id": "gate",
                "guard": {"left": "count(rows, items)", "op": "gte", "right": 0},
                "on_false": "$abort",
                "message": "bound",
            },
            {
                "id": "tail",
                "shape_items": {"items": [{"ok": True}], "fields": {"ok": "$item.ok"}},
                "as": "result",
            },
        ],
        returns="result",
    )
    assert compile_procedure_definition(procedure_instance, definition).steps[1].kind == "guard"
