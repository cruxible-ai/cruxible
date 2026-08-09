"""Run-id-only recovery of the facts required for procedure calibration."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, QueryExecutionError, StaleContinuationError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.procedure.pins import receipt_pin_material
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.query.continuation import (
    compute_filter_hash,
    decode_continuation_token,
    mint_continuation_token,
    validate_continuation_token,
)
from cruxible_core.service import service_record_reading, service_run_procedure
from cruxible_core.temporal import utc_now
from tests.test_procedures.conftest import actor
from tests.test_procedures.test_execution import _accept, _stub_provider


def _two_arm_definition() -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": "log_sufficient_two_arm",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "read",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "rows",
                },
                {
                    "id": "gate",
                    "guard": {"left": "$steps.rows.value", "op": "gt", "right": 0},
                    "on_true": "result",
                    "message": "value did not pass the decision gate",
                },
                {
                    "id": "result",
                    "shape_items": {
                        "items": [{"value": "$steps.rows.value"}],
                        "fields": {"value": "$item.value"},
                    },
                    "as": "result",
                },
            ],
            "returns": "result",
            "precondition": {
                "entity_type": "Task",
                "condition": {"status": "ready"},
            },
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )


def _set_task_status(procedure_instance: CruxibleInstance, status: str) -> None:
    entity = EntityInstance(
        entity_type="Task",
        entity_id="log-task",
        properties={"status": status},
    )
    graph = procedure_instance.load_graph()
    if not graph.update_entity_properties("Task", "log-task", {"status": status}):
        graph.add_entity(entity)
    procedure_instance.save_graph_delta(graph, entities=[entity])


def _recover_run_facts(
    procedure_instance: CruxibleInstance,
    run_id: str,
) -> dict[str, Any]:
    procedure_store = procedure_instance.get_procedure_store()
    try:
        run = procedure_store.get_run(run_id)
        assert run is not None
        pins = procedure_store.list_acceptance_node_pins(run.procedure_id)
    finally:
        procedure_store.close()
    assert run.receipt_id is not None
    receipt_store = procedure_instance.get_receipt_store()
    try:
        receipt = receipt_store.get_receipt(run.receipt_id)
    finally:
        receipt_store.close()
    assert receipt is not None
    fired_store = procedure_instance.get_procedure_reading_store()
    try:
        fired = fired_store.list_run_fired_nodes(run_id)
    finally:
        fired_store.close()
    guards = [
        node.detail
        for node in receipt.nodes
        if node.node_type == "validation" and node.detail.get("kind") == "branch"
    ]
    return {"run": run, "receipt": receipt, "pins": pins, "fired": fired, "guards": guards}


def test_success_failure_and_refusal_are_log_sufficient_from_run_id(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_task_status(procedure_instance, "ready")
    procedure_id = _accept(procedure_instance, _two_arm_definition())
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload["value"])})

    before_success = procedure_instance.get_read_revision()
    success = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 1},
        actor("success-runner"),
    )
    assert procedure_instance.get_read_revision() == before_success + 2

    with pytest.raises(QueryExecutionError) as failed_exc:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": -1},
            actor("failure-runner"),
        )
    failed_run_id = getattr(failed_exc.value, "procedure_run_id")

    _set_task_status(procedure_instance, "waiting")
    with pytest.raises(ConfigError, match="precondition was unsatisfied") as refused_exc:
        service_run_procedure(
            procedure_instance,
            procedure_id,
            {"value": 1},
            actor("refusal-runner"),
        )
    refused_run_id = getattr(refused_exc.value, "procedure_run_id")

    recovered = {
        "success": _recover_run_facts(procedure_instance, success.run.run_id),
        "failure": _recover_run_facts(procedure_instance, failed_run_id),
        "refusal": _recover_run_facts(procedure_instance, refused_run_id),
    }
    assert [node.node_id for node in recovered["success"]["fired"]] == [
        "read",
        "gate",
        "result",
    ]
    assert [node.node_id for node in recovered["failure"]["fired"]] == ["read", "gate"]
    assert recovered["refusal"]["fired"] == []

    for outcome, facts in recovered.items():
        run = facts["run"]
        receipt = facts["receipt"]
        assert run.receipt_id == receipt.receipt_id
        pin_map, pin_payloads = receipt_pin_material(facts["pins"])
        assert receipt.nodes[0].detail["node_pins"] == pin_map
        assert receipt.nodes[0].detail["pin_payloads"] == pin_payloads
        for sequence, fired in enumerate(facts["fired"]):
            assert fired.sequence == sequence
            assert fired.node_local_digest.startswith("sha256:")
            assert fired.node_subtree_digest.startswith("sha256:")
        if outcome == "refusal":
            assert facts["guards"] == []
        else:
            assert [
                (guard["op"], guard["left"], guard["right"], guard["taken"])
                for guard in facts["guards"]
            ] == [
                (
                    "gt",
                    1 if outcome == "success" else -1,
                    0,
                    "on_true" if outcome == "success" else "on_false",
                )
            ]


def test_reading_advances_revision_once_and_invalidates_continuation(
    procedure_instance: CruxibleInstance,
) -> None:
    _set_task_status(procedure_instance, "ready")
    procedure_id = _accept(procedure_instance, _two_arm_definition())
    before = procedure_instance.get_read_revision()
    config_digest = "sha256:continuation-config"
    filter_hash = compute_filter_hash({"status": "live"})
    raw = mint_continuation_token(
        surface="list",
        instance_key="log-sufficiency-instance",
        config_digest=config_digest,
        read_revision=before,
        filter_hash=filter_hash,
        cursor={"offset": 1},
    )

    service_record_reading(
        procedure_instance,
        procedure_id,
        subject_grain="procedure_unit",
        grade="attestation",
        verdict="satisfied",
        observed_at=utc_now(),
        actor_context=actor("revision-reader"),
    )

    after = procedure_instance.get_read_revision()
    assert after == before + 1
    with pytest.raises(StaleContinuationError):
        validate_continuation_token(
            decode_continuation_token(raw),
            surface="list",
            instance_key="log-sufficiency-instance",
            config_digest=config_digest,
            read_revision=after,
            filter_hash=filter_hash,
        )
