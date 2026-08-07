"""T3 -- the batch's soul.

Linearity is not a special case in the executor; it is the case where the
successor function degenerates. That claim is TESTED here, not asserted:

1. over every frozen corpus definition, the successor walk's visit ORDER is
   shown equal to the flat list order the old loop used; and
2. over executable definitions, the run is performed twice -- once through the
   shipped successor walk and once through the pre-change flat loop, carried
   here verbatim as the oracle -- and the two are compared on ``step_outputs``,
   on the returned output, and on the receipt modulo ids and timings.

If either half fails, a v1 procedure on a shipped instance no longer executes
the way it did.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.procedure.analysis import build_procedure_graph
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import service_run_procedure
from cruxible_core.workflow import executor as executor_module
from cruxible_core.workflow.execution_context import WorkflowExecutionContext
from cruxible_core.workflow.step_handlers import PROCEDURE_STEP_HANDLER_REGISTRY
from tests.test_procedures.test_definition_digest_corpus import ENTRIES, IDS
from tests.test_procedures.test_execution import _accept, _receipt, _stub_provider

_VOLATILE_RECEIPT_KEYS = {
    # Minted or advanced per run, in both executors alike.
    "receipt_id",
    "created_at",
    "duration_ms",
    "timestamp",
    "read_revision",
    "trace_id",
    "head_snapshot_id",
    "query_receipt_ids",
}


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_the_successor_walk_visits_the_flat_list_order(entry: dict[str, Any]) -> None:
    definition = ProcedureDefinition.model_validate(entry["normalized_dump_v032"])
    graph = build_procedure_graph(definition)
    walked: list[str] = []
    current: str | None = graph.entry_id
    while current is not None:
        walked.append(current)
        successors = graph.successors_of(current)
        current = successors[0] if successors else None
    assert walked == list(graph.node_ids)


def _flat_loop(context: WorkflowExecutionContext) -> None:
    """The pre-change executor loop, verbatim, as the oracle."""
    for compiled_step in context.plan.steps:
        context.check_procedure_wall_clock()
        PROCEDURE_STEP_HANDLER_REGISTRY.execute(context, compiled_step)


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in _VOLATILE_RECEIPT_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _receipt_modulo_ids(instance: CruxibleInstance, receipt_id: str) -> dict[str, Any]:
    receipt = _receipt(instance, receipt_id)
    dumped = receipt.model_dump(mode="json")
    # Node ids and the edges over them are minted per run; the SHAPE of the DAG
    # and every recorded detail are what must match.
    node_index = {node["node_id"]: position for position, node in enumerate(dumped["nodes"])}
    dumped["nodes"] = [
        {key: item for key, item in node.items() if key != "node_id"} for node in dumped["nodes"]
    ]
    dumped["edges"] = [
        {
            "from_node": node_index[edge["from_node"]],
            "to_node": node_index[edge["to_node"]],
            "edge_type": edge["edge_type"],
        }
        for edge in dumped["edges"]
    ]
    result = _strip_volatile(dumped)
    assert isinstance(result, dict)
    # Run ids and receipt ids leak into the root detail; drop the whole run
    # coordinate rather than guess at which key carries it.
    result["nodes"][0].pop("detail", None)
    return result


_LINEAR_SHAPES: dict[str, list[dict[str, Any]]] = {
    "single-provider": [
        {"id": "invoke", "provider": "exported_action", "input": {"value": 1}, "as": "result"},
    ],
    "provider-then-assert": [
        {"id": "invoke", "provider": "exported_action", "input": {"value": 1}, "as": "result"},
        {
            "id": "check",
            "assert": {
                "left": "$steps.result.value",
                "op": "gte",
                "right": 0,
                "message": "value must be non-negative",
            },
        },
    ],
    "provider-chain-with-shape": [
        {"id": "invoke", "provider": "exported_action", "input": {"value": 1}, "as": "first"},
        {"id": "again", "provider": "exported_action", "input": {"value": 2}, "as": "second"},
        {
            "id": "assemble",
            "shape_items": {
                "items": [{"value": 1}],
                "fields": {"value": "$item.value"},
            },
            "as": "result",
        },
    ],
    "repeat": [
        {
            "id": "retry",
            "as": "result",
            "repeat": {
                "max_attempts": 2,
                "until": {
                    "left": "$steps.attempt.value",
                    "op": "gte",
                    "right": 0,
                    "message": "not settled",
                },
                "steps": [
                    {
                        "id": "attempt",
                        "provider": "exported_action",
                        "input": {"value": 1},
                        "as": "attempt",
                    }
                ],
            },
        },
    ],
}


@pytest.mark.parametrize("shape", sorted(_LINEAR_SHAPES), ids=sorted(_LINEAR_SHAPES))
def test_t3_a_linear_definition_runs_identically_through_both_executors(
    shape: str,
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    definition = ProcedureDefinition.model_validate(
        {
            "name": f"linear_{shape.replace('-', '_')}",
            "contract_in": "ProcedureInput",
            "steps": _LINEAR_SHAPES[shape],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 5},
            "declared_tier": "graph_write",
        }
    )
    procedure_id = _accept(procedure_instance, definition)

    walked = service_run_procedure(
        procedure_instance,
        procedure_id,
        input_payload={"value": 1},
        actor_context=None,
    )
    monkeypatch.setattr(executor_module, "_walk_procedure_successors", _flat_loop)
    flat = service_run_procedure(
        procedure_instance,
        procedure_id,
        input_payload={"value": 1},
        actor_context=None,
    )

    assert walked.step_outputs == flat.step_outputs
    assert walked.output == flat.output
    assert _receipt_modulo_ids(
        procedure_instance, walked.run.receipt_id or ""
    ) == _receipt_modulo_ids(procedure_instance, flat.run.receipt_id or "")
