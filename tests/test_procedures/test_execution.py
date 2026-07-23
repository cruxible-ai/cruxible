"""Procedure execution, precondition, repeat, run-record, and drift coverage."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    ConfigError,
    ProcedureRepeatExhaustedError,
    QueryExecutionError,
)
from cruxible_core.graph.types import EntityInstance
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureRun
from cruxible_core.receipt.types import Receipt
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.service import (
    service_lock,
    service_promote_procedure,
    service_propose_procedure,
    service_run_procedure,
)
from tests.test_procedures.conftest import actor, provider_definition


def _promote(
    instance: CruxibleInstance,
    definition: ProcedureDefinition,
) -> str:
    proposed = service_propose_procedure(
        instance,
        definition,
        actor_context=actor("proposer"),
    )
    promoted = service_promote_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )
    return promoted.procedure.procedure_id


def _run(instance: CruxibleInstance, run_id: str) -> ProcedureRun:
    store = instance.get_procedure_store()
    try:
        run = store.get_run(run_id)
        assert run is not None
        return run
    finally:
        store.close()


def _receipt(instance: CruxibleInstance, receipt_id: str) -> Receipt:
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
        assert receipt is not None
        return receipt
    finally:
        store.close()


def _stub_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    def resolve_provider(*args: Any, **kwargs: Any) -> Callable[..., dict[str, Any]]:
        def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
            return provider(payload)

        return invoke

    monkeypatch.setattr("cruxible_core.workflow.io.resolve_provider", resolve_provider)


def _add_task(instance: CruxibleInstance, task_id: str, status: str) -> None:
    task = EntityInstance(
        entity_type="Task",
        entity_id=task_id,
        properties={"status": status},
    )
    graph = instance.load_graph()
    graph.add_entity(task)
    instance.save_graph_delta(graph, entities=[task])


def _repeat_definition(
    name: str,
    *,
    max_attempts: int = 3,
    nested_steps: list[dict[str, Any]] | None = None,
) -> ProcedureDefinition:
    repeat_steps = nested_steps or [
        {
            "id": "invoke",
            "provider": "exported_action",
            "input": {"value": "$input.value"},
            "as": "result",
        }
    ]
    provider_steps = sum("provider" in step for step in repeat_steps)
    return ProcedureDefinition.model_validate(
        {
            "name": name,
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": max_attempts,
                        "until": {
                            "left": "$steps.result.value",
                            "op": "eq",
                            "right": 3,
                            "message": "value reached three",
                        },
                        "steps": repeat_steps,
                    },
                    "as": "attempt",
                }
            ],
            "returns": "attempt",
            "precondition": {},
            "budget": {
                "wall_clock_s": 30,
                "max_provider_calls": max_attempts * provider_steps,
            },
            "declared_tier": "graph_write",
        }
    )


def test_precondition_refusal_finalizes_started_run_and_receipts_revision(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = provider_definition("requires_ready_task").model_copy(
        update={"precondition": {"status": "ready"}}
    )
    procedure_id = _promote(procedure_instance, definition)
    _stub_provider(monkeypatch, lambda payload: payload)

    with pytest.raises(ConfigError, match="precondition was unsatisfied") as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 1},
            actor("runner"),
        )

    run = _run(procedure_instance, getattr(exc_info.value, "procedure_run_id"))
    assert run.status == "finalized"
    assert run.verdict == "refused"
    assert run.receipt_id is not None
    receipt = _receipt(procedure_instance, run.receipt_id)
    precondition_nodes = [
        node
        for node in receipt.nodes
        if node.node_type == "validation" and node.detail.get("kind") == "procedure_precondition"
    ]
    assert len(precondition_nodes) == 1
    assert precondition_nodes[0].detail["read_revision"] == procedure_instance.get_read_revision()
    assert precondition_nodes[0].detail["satisfying_entity_ids"] == []
    assert receipt.nodes[0].detail["verdict"] == "refused"


def test_satisfied_precondition_records_satisfier_entity_ids(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_task(procedure_instance, "task-ready", "ready")
    _add_task(procedure_instance, "task-waiting", "waiting")
    definition = provider_definition("ready_task_action").model_copy(
        update={"precondition": {"status": "ready"}}
    )
    procedure_id = _promote(procedure_instance, definition)
    _stub_provider(monkeypatch, lambda payload: payload)

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 7},
        actor("runner"),
    )

    assert result.output == {"value": 7}
    assert result.receipt.nodes[0].detail["precondition"]["satisfying_entity_ids"] == ["task-ready"]
    lookups = [node.entity_id for node in result.receipt.nodes if node.node_type == "entity_lookup"]
    assert lookups == ["task-ready"]


def test_started_run_exists_before_precondition_evaluation(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = provider_definition("started_before_precondition").model_copy(
        update={"precondition": {"status": "ready"}}
    )
    procedure_id = _promote(procedure_instance, definition)
    observed: list[ProcedureRun] = []

    def inspect_started(*args: Any, **kwargs: Any) -> list[tuple[str, str]]:
        store = procedure_instance.get_procedure_store()
        try:
            runs = store.list_runs(procedure_id=procedure_id)
            assert len(runs) == 1
            observed.append(runs[0])
        finally:
            store.close()
        return []

    monkeypatch.setattr(
        "cruxible_core.service.procedures._procedure_precondition_satisfiers",
        inspect_started,
    )

    with pytest.raises(ConfigError, match="precondition was unsatisfied"):
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 1},
            actor("runner"),
        )

    assert observed[0].status == "started"
    assert observed[0].verdict is None
    assert observed[0].receipt_id is None


def test_provider_runs_after_precondition_transaction_closes(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        provider_definition("short_authorization_transaction"),
    )

    def write_from_another_connection(payload: dict[str, Any]) -> dict[str, Any]:
        connection = sqlite3.connect(
            procedure_instance.get_instance_dir() / "state.db",
            timeout=0.1,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO instance_state(key, value_json, updated_at) "
                "VALUES (?, ?, ?)",
                ("provider_probe", json.dumps("writable"), "2026-07-22T12:00:00Z"),
            )
            time.sleep(0.02)
            connection.commit()
        finally:
            connection.close()
        return payload

    _stub_provider(monkeypatch, write_from_another_connection)

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 9},
        actor("runner"),
    )

    assert result.run.verdict == "succeeded"
    assert result.output == {"value": 9}


def test_repeat_until_satisfaction_uses_final_attempt_outputs_and_attempt_count(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        _repeat_definition("repeat_until_satisfied"),
    )
    values = iter([1, 2, 3])
    _stub_provider(monkeypatch, lambda payload: {"value": next(values)})

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 0},
        actor("runner"),
    )

    assert result.output == {
        "result": {"value": 3},
        "attempt_count": 3,
    }
    repeat_node = next(
        node
        for node in result.receipt.nodes
        if node.node_type == "plan_step" and node.detail["kind"] == "repeat"
    )
    assert repeat_node.detail["attempt_count"] == 3
    nested_attempts = [
        node.detail["attempt_count"]
        for node in result.receipt.nodes
        if node.node_type == "plan_step" and node.detail.get("repeat_step_id") == "retry"
    ]
    assert nested_attempts == [1, 2, 3]


def test_repeat_exhaustion_finalizes_failed_run_with_annotation(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        _repeat_definition("repeat_exhausted", max_attempts=2),
    )
    _stub_provider(monkeypatch, lambda payload: {"value": 1})

    with pytest.raises(ProcedureRepeatExhaustedError) as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 0},
            actor("runner"),
        )

    run = _run(procedure_instance, getattr(exc_info.value, "procedure_run_id"))
    assert run.verdict == "failed"
    assert run.budget_spent.provider_calls == 2
    assert run.receipt_id is not None
    receipt = _receipt(procedure_instance, run.receipt_id)
    assert receipt.nodes[0].detail["repeat_exhausted"] is True


def test_repeat_nested_assert_aborts_without_another_attempt(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested_steps = [
        {
            "id": "invoke",
            "provider": "exported_action",
            "input": {"value": "$input.value"},
            "as": "result",
        },
        {
            "id": "invariant",
            "assert": {
                "left": "$steps.result.value",
                "op": "gte",
                "right": 2,
                "message": "invariant failed",
            },
        },
    ]
    procedure_id = _promote(
        procedure_instance,
        _repeat_definition(
            "repeat_invariant_abort",
            nested_steps=nested_steps,
        ),
    )
    calls = 0

    def fail_invariant(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"value": 1}

    _stub_provider(monkeypatch, fail_invariant)

    with pytest.raises(QueryExecutionError, match="invariant failed") as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 0},
            actor("runner"),
        )

    assert calls == 1
    assert _run(procedure_instance, getattr(exc_info.value, "procedure_run_id")).verdict == "failed"


def test_repeat_attempt_aliases_are_rebuilt_from_current_attempt(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested_steps = [
        {
            "id": "first",
            "provider": "exported_action",
            "input": {"value": "$input.value"},
            "as": "current",
        },
        {
            "id": "second",
            "provider": "exported_action",
            "input": {"value": "$steps.current.value"},
            "as": "result",
        },
    ]
    definition = _repeat_definition(
        "repeat_alias_isolation",
        max_attempts=2,
        nested_steps=nested_steps,
    )
    procedure_id = _promote(procedure_instance, definition)
    provider_inputs: list[int] = []
    outputs = iter([{"value": 1}, {"value": 1}, {"value": 3}, {"value": 3}])

    def current_attempt(payload: dict[str, Any]) -> dict[str, Any]:
        provider_inputs.append(payload["value"])
        return next(outputs)

    _stub_provider(monkeypatch, current_attempt)

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 0},
        actor("runner"),
    )

    assert provider_inputs == [0, 1, 0, 3]
    assert result.output == {
        "current": {"value": 3},
        "result": {"value": 3},
        "attempt_count": 2,
    }


def test_run_fails_closed_when_live_provider_is_deexported(
    procedure_instance: CruxibleInstance,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        provider_definition("deexported_after_promotion"),
    )
    config = procedure_instance.load_config()
    config.providers["exported_action"].procedure_access = "disabled"
    procedure_instance.save_config(config)
    service_lock(procedure_instance)

    with pytest.raises(ConfigError, match="not exported to procedures") as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 1},
            actor("runner"),
        )

    run = _run(procedure_instance, getattr(exc_info.value, "procedure_run_id"))
    assert run.verdict == "refused"
    assert run.budget_spent.provider_calls == 0
    assert run.receipt_id is not None
    receipt = _receipt(procedure_instance, run.receipt_id)
    assert receipt.nodes[0].detail["executed_against"]["config_digest"] is not None


def test_run_fails_closed_when_live_provider_is_removed(
    procedure_instance: CruxibleInstance,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        provider_definition("missing_after_promotion"),
    )
    config = procedure_instance.load_config()
    del config.providers["exported_action"]
    procedure_instance.save_config(config)
    service_lock(procedure_instance)

    with pytest.raises(ConfigError, match="unknown provider 'exported_action'") as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 1},
            actor("runner"),
        )

    run = _run(procedure_instance, getattr(exc_info.value, "procedure_run_id"))
    assert run.status == "finalized"
    assert run.verdict == "refused"
    assert run.budget_spent.provider_calls == 0


def test_run_rederives_and_enforces_effective_tier(
    procedure_instance: CruxibleInstance,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        provider_definition("tier_checked_at_run"),
    )

    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(ConfigError, match="requires tier 'graph_write'") as exc_info:
            service_run_procedure(
                procedure_instance,
                procedure_id,
                {"value": 1},
                actor("runner"),
            )

    run = _run(procedure_instance, getattr(exc_info.value, "procedure_run_id"))
    assert run.verdict == "refused"
    assert run.budget_spent.provider_calls == 0


def test_input_contract_is_validated_before_provider_execution(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        provider_definition("input_contract_checked"),
    )
    called = False

    def should_not_run(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return payload

    _stub_provider(monkeypatch, should_not_run)

    with pytest.raises(ConfigError, match="input failed contract") as exc_info:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": "not-an-int"},
            actor("runner"),
        )

    assert called is False
    assert (
        _run(
            procedure_instance,
            getattr(exc_info.value, "procedure_run_id"),
        ).verdict
        == "refused"
    )


def test_run_receipt_records_promoted_and_drifted_execution_digests(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procedure_id = _promote(
        procedure_instance,
        provider_definition("drift_visible"),
    )
    config = procedure_instance.load_config()
    config.providers["exported_action"].config["timeout_s"] = 4
    procedure_instance.save_config(config)
    service_lock(procedure_instance)
    _stub_provider(monkeypatch, lambda payload: payload)

    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 5},
        actor("runner"),
    )

    root = result.receipt.nodes[0].detail
    assert root["promoted_against"]["config_digest"] != root["executed_against"]["config_digest"]
    assert root["promoted_against"]["lock_digest"] != root["executed_against"]["lock_digest"]
    assert result.receipt.operation_type == "procedure"
    assert root["procedure_id"] == procedure_id
    assert root["definition_digest"] == result.procedure.definition_digest
