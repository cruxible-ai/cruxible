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
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import service_run_procedure
from cruxible_core.workflow import executor as executor_module
from cruxible_core.workflow.execution_context import WorkflowExecutionContext
from cruxible_core.workflow.step_handlers import PROCEDURE_STEP_HANDLER_REGISTRY
from tests.test_procedures.test_execution import _accept, _receipt, _stub_provider

_NORMALIZED_RECEIPT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        # Receipt envelope: minted or advanced once per run by construction.
        ("receipt_id",),
        ("created_at",),
        ("duration_ms",),
        ("read_revision",),
        ("head_snapshot_id",),
        ("nodes", "*", "timestamp"),
        # Per-step provider trace, minted per invocation.
        ("nodes", "*", "detail", "trace_id"),
        # A query plan step records the id of the read receipt it produced.
        ("nodes", "*", "detail", "receipt_id"),
        # The precondition records the revision it evaluated at, both as a
        # bare validation node and nested in the root detail's summary.
        ("nodes", "*", "detail", "read_revision"),
        ("nodes", "*", "detail", "precondition", "read_revision"),
        # A measurement of THIS run's elapsed time. `provider_calls` beside it
        # is not a measurement and stays compared.
        ("nodes", "*", "detail", "budget", "spent", "wall_clock_s"),
        # Per-query receipt ids, at every place the aggregate republishes them.
        ("nodes", "*", "detail", "read_metadata", "query_receipt_ids"),
        ("nodes", "*", "detail", "read_metadata", "read_steps", "*", "metadata", "receipt_id"),
    }
)
"""EXACT PATHS, not key names.

A key-name-global rule reaches into user data: a step output or a returned
object with a field called `read_revision` or `timestamp` would be neutralized
too, and two runs producing genuinely different outputs would compare equal --
which is the one thing this oracle exists to detect. Every entry below names
where in the RECEIPT the value lives; `*` matches a list index.
"""

_PLACEHOLDER = "<run-identity>"
"""Run-identity values are REPLACED, not deleted.

Deleting a key hides the difference between "this field is per-run" and "this
field vanished". The oracle has to fail when the successor walk stops emitting
something the flat loop emitted, so every key survives normalization and only
its value is neutralized."""


def _strip_query_receipt_ids(step_outputs: dict[str, Any]) -> dict[str, Any]:
    """Drop the ONE per-execution value a query step publishes in its output.

    A query step's output carries `receipt_id`, the id of the read receipt that
    produced it, minted per execution. It is removed at that exact path -- one
    key, one level, in a step output that is otherwise compared byte for byte,
    including every count, every truncation flag and every result row.
    """
    return {
        alias: (
            {key: item for key, item in value.items() if key != "receipt_id"}
            if isinstance(value, dict) and "receipt_id" in value and "results" in value
            else value
        )
        for alias, value in step_outputs.items()
    }


def _path_is_normalized(path: tuple[str, ...]) -> bool:
    return any(
        len(pattern) == len(path)
        and all(part == "*" or part == actual for part, actual in zip(pattern, path))
        for pattern in _NORMALIZED_RECEIPT_PATHS
    )


def _normalize_receipt(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Neutralize run identity at known receipt paths, and nowhere else."""
    if _path_is_normalized(path):
        if path[-1] == "query_receipt_ids" and isinstance(value, list):
            # Ids are minted per execution; the COUNT is the property an
            # executor change could alter, so it is what survives.
            return f"<{len(value)} query receipts>"
        return _PLACEHOLDER
    if isinstance(value, dict):
        return {key: _normalize_receipt(item, (*path, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_receipt(item, (*path, "*")) for item in value]
    return value


def _flat_loop(context: WorkflowExecutionContext) -> None:
    """The pre-change executor loop, verbatim, as the oracle."""
    for compiled_step in context.plan.steps:
        context.check_procedure_wall_clock()
        PROCEDURE_STEP_HANDLER_REGISTRY.execute(context, compiled_step)


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
    result = _normalize_receipt(dumped)
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

    # EXACT equality. Step outputs and the returned output are user data and
    # get no normalization at all -- T3 promises they are identical, and
    # neutralizing anything inside them would let two genuinely different
    # outputs pass.
    assert _strip_query_receipt_ids(walked.step_outputs) == _strip_query_receipt_ids(
        flat.step_outputs
    )
    assert walked.output == flat.output
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
    for index, node in enumerate(receipt.nodes):
        if "read_metadata" in node.detail:
            normalized = _normalize_receipt(
                receipt.model_dump(mode="json")["nodes"][index]["detail"]["read_metadata"],
                ("nodes", "*", "detail", "read_metadata"),
            )
            return normalized
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


def test_the_oracle_does_not_neutralize_user_data() -> None:
    """The defect a key-name-global normalization introduces.

    Two runs whose OUTPUT differs only at a field called `read_revision` are
    two runs that produced different answers. An oracle that neutralizes the
    field by name compares them equal and reports T3 satisfied -- which is the
    exact opposite of what T3 asserts.
    """
    walked = {"result": {"read_revision": 1, "timestamp": "A", "value": 7}}
    flat = {"result": {"read_revision": 999, "timestamp": "B", "value": 7}}
    assert _strip_query_receipt_ids(walked) != _strip_query_receipt_ids(flat)

    # And inside the receipt, user data rides in `results` -- also untouched.
    receipt = {"results": [{"output": {"read_revision": 1}}], "nodes": [], "edges": []}
    other = {"results": [{"output": {"read_revision": 999}}], "nodes": [], "edges": []}
    assert _normalize_receipt(receipt) != _normalize_receipt(other)


def test_the_oracle_still_neutralizes_run_identity_at_its_own_paths() -> None:
    receipt = {
        "receipt_id": "RCP-a",
        "read_revision": 4,
        "nodes": [{"detail": {"trace_id": "TRC-a", "read_revision": 4}}],
    }
    other = {
        "receipt_id": "RCP-b",
        "read_revision": 6,
        "nodes": [{"detail": {"trace_id": "TRC-b", "read_revision": 6}}],
    }
    assert _normalize_receipt(receipt) == _normalize_receipt(other)
    assert _normalize_receipt(receipt)["nodes"][0]["detail"]["trace_id"] == _PLACEHOLDER
