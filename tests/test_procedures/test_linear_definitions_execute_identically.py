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

_RUN_IDENTITY_KEYS = {
    # Minted or advanced ONCE PER RUN by construction, in both executors alike:
    # ids, clocks, and the monotonic read revision. Two sequential runs cannot
    # agree on these and no executor change could make them.
    "receipt_id",
    "created_at",
    "duration_ms",
    "timestamp",
    "read_revision",
    "trace_id",
    "head_snapshot_id",
}
_PLACEHOLDER = "<run-identity>"
"""Run-identity values are REPLACED, not deleted.

Deleting a key hides the difference between "this field is per-run" and "this
field vanished". The oracle has to fail when the successor walk stops emitting
something the flat loop emitted, so every key survives the normalization and
only its value is neutralized."""


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


def _normalize_run_identity(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                _PLACEHOLDER
                if key in _RUN_IDENTITY_KEYS
                else _normalize_query_receipt_ids(key, item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_run_identity(item) for item in value]
    return value


def _normalize_query_receipt_ids(key: str, value: Any) -> Any:
    """Per-execution measurements, collapsed to the part that is comparable.

    Query receipt ids are minted per execution; their COUNT is not, and how
    many query receipts a run produced is exactly the kind of thing an
    executor change could alter. Spent wall-clock is a measurement of this
    run's elapsed time; the provider-call count beside it is not, so only the
    clock is neutralized.
    """
    if key == "query_receipt_ids" and isinstance(value, list):
        return f"<{len(value)} query receipts>"
    if key == "spent" and isinstance(value, dict):
        return {**_normalize_run_identity(value), "wall_clock_s": _PLACEHOLDER}
    return _normalize_run_identity(value)


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
    result = _normalize_run_identity(dumped)
    assert isinstance(result, dict)
    # The root detail is COMPARED, not dropped: it carries the accepted/executed
    # digests, the precondition, the budget, the verdict and the pin material,
    # and every one of those is a thing an executor change could disturb. Only
    # the per-run coordinates inside it are normalized, by the same walk.
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
    "query-then-assert": [
        {
            "id": "read",
            "query": {
                "mode": "collection",
                "returns": "Task",
                "result_shape": "entity",
                "limit": 10,
            },
            "as": "rows",
        },
        {
            "id": "guard_count",
            "assert_count": {
                "step": "rows",
                "count": "returned_results",
                "op": "gte",
                "value": 0,
                "message": "read must succeed",
            },
        },
        {
            "id": "assemble",
            "shape_items": {"items": [{"value": 1}], "fields": {"value": "$item.value"}},
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

    # A query step's own output embeds the receipt id of the read that produced
    # it, which is minted per execution; the same normalization applies.
    assert _normalize_run_identity(walked.step_outputs) == _normalize_run_identity(
        flat.step_outputs
    )
    assert _normalize_run_identity(walked.output) == _normalize_run_identity(flat.output)
    # Read metadata is derived from what the run READ, not from run identity,
    # so it must match exactly. It was previously not compared at all.
    assert _read_metadata(procedure_instance, walked) == _read_metadata(procedure_instance, flat)
    assert _receipt_modulo_ids(
        procedure_instance, walked.run.receipt_id or ""
    ) == _receipt_modulo_ids(procedure_instance, flat.run.receipt_id or "")


def _read_metadata(instance: CruxibleInstance, result: Any) -> Any:
    """The aggregated read metadata, with per-execution ids neutralized.

    Everything else in it -- which steps read, their counts, every truncation
    flag and reason -- is derived from what the run READ and must match
    exactly. It was previously not compared at all.
    """
    receipt = _receipt(instance, result.run.receipt_id or "")
    for node in receipt.nodes:
        if "read_metadata" in node.detail:
            return _normalize_run_identity(node.detail["read_metadata"])
    return {}


def test_the_oracle_compares_the_root_detail_rather_than_discarding_it(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard on the guard.

    An oracle that strips the root detail cannot see a change to the pin
    material, the accepted/executed digests or the verdict -- so this asserts
    the normalized receipt still carries them.
    """
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    definition = ProcedureDefinition.model_validate(
        {
            "name": "oracle_selfcheck",
            "contract_in": "ProcedureInput",
            "steps": _LINEAR_SHAPES["single-provider"],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 5},
            "declared_tier": "graph_write",
        }
    )
    procedure_id = _accept(procedure_instance, definition)
    result = service_run_procedure(
        procedure_instance, procedure_id, input_payload={"value": 1}, actor_context=None
    )
    normalized = _receipt_modulo_ids(procedure_instance, result.run.receipt_id or "")
    root_detail = normalized["nodes"][0]["detail"]
    for key in ("node_pins", "pin_payloads", "accepted_against", "executed_against", "verdict"):
        assert key in root_detail
    # Run-identity keys survive as placeholders, so a field that DISAPPEARS is
    # still a failure rather than a silent match.
    assert normalized["read_revision"] == _PLACEHOLDER
