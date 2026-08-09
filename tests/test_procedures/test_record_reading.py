"""Procedure-reading service and the contract-grade Goodhart boundary."""

from __future__ import annotations

from typing import Literal, cast

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ContractGradeRefusedError
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureRecord
from cruxible_core.service import (
    service_accept_procedure,
    service_get_procedure,
    service_list_resolution_contracts,
    service_propose_procedure,
    service_record_reading,
)
from cruxible_core.temporal import utc_now
from tests.test_procedures.conftest import actor


def _measurement(name: str, **coordinates: str) -> dict[str, object]:
    return {
        "name": name,
        "granularity": "arm" if coordinates else "procedure_unit",
        **coordinates,
        "measurement": {
            "kind": "attestation",
            "relationship_type": "blocks",
            "from_type": "Task",
            "from_id": "T-1",
            "to_type": "Incident",
            "to_id": "I-1",
        },
        "check_after_days": 0,
        "expires_after_days": 30,
    }


def _accept(
    procedure_instance: CruxibleInstance,
    definition: ProcedureDefinition,
) -> ProcedureRecord:
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("reading-proposer"),
    ).procedure
    return service_accept_procedure(
        procedure_instance,
        proposed.procedure_id,
        expected_version=1,
        actor_context=actor("reading-reviewer"),
    ).procedure


def _unit_definition() -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": "reading_goodhart_unit",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "result",
                    "shape_items": {
                        "items": [{"value": "$input.value"}],
                        "fields": {"value": "$item.value"},
                    },
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 10, "max_provider_calls": 0},
            "measurements": [_measurement("unit_quality")],
        }
    )


def test_goodhart_refusal_is_typed_and_writes_no_reading(
    procedure_instance: CruxibleInstance,
) -> None:
    procedure = _accept(procedure_instance, _unit_definition())
    listed = service_list_resolution_contracts(
        procedure_instance,
        entity_type="cruxible.Procedure",
        entity_id=procedure.procedure_id,
    )
    contract_id = listed.items[0].contract.contract_id
    store = procedure_instance.get_procedure_reading_store()
    try:
        before = store.list_readings(procedure_id=procedure.procedure_id)
    finally:
        store.close()

    with pytest.raises(ContractGradeRefusedError) as exc_info:
        service_record_reading(
            procedure_instance,
            procedure.procedure_id,
            subject_grain="procedure_unit",
            grade="contract",
            measurement_name="undeclared_measurement",
            contract_id=contract_id,
            verdict="satisfied",
            observed_at=utc_now(),
            actor_context=actor("reading-recorder"),
        )

    assert exc_info.value.error_code == "contract_grade_refused"
    assert exc_info.value.mutation_receipt_id is not None
    store = procedure_instance.get_procedure_reading_store()
    try:
        assert store.list_readings(procedure_id=procedure.procedure_id) == before
    finally:
        store.close()


def test_two_converging_arms_are_distinct_contract_grade_subjects(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": "converging_arm_readings",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": "$input.value", "op": "gt", "right": 0},
                    "on_true": "tail",
                    "on_false": "tail",
                    "message": "select outcome arm",
                },
                {
                    "id": "tail",
                    "shape_items": {
                        "items": [{"value": 1}],
                        "fields": {"value": "$item.value"},
                    },
                    "as": "result",
                },
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 10, "max_provider_calls": 0},
            "measurements": [
                _measurement("true_arm", node_id="tail", from_node_id="gate", arm_label="on_true"),
                _measurement(
                    "false_arm", node_id="tail", from_node_id="gate", arm_label="on_false"
                ),
            ],
        }
    )
    procedure = _accept(procedure_instance, definition)
    contracts = service_list_resolution_contracts(
        procedure_instance,
        entity_type="cruxible.Procedure",
        entity_id=procedure.procedure_id,
    )
    contract_by_name = {
        item.contract.idempotency_key.rsplit(":", 1)[-1]: item.contract.contract_id
        for item in contracts.items
        if item.contract.idempotency_key is not None
    }

    readings = [
        service_record_reading(
            procedure_instance,
            procedure.procedure_id,
            subject_grain="arm",
            node_id="tail",
            from_node_id="gate",
            arm_label=cast(Literal["on_true", "on_false"], arm_label),
            grade="contract",
            measurement_name=name,
            contract_id=contract_by_name[name],
            verdict="satisfied",
            observed_at=utc_now(),
            actor_context=actor(f"{name}-recorder"),
        )
        for name, arm_label in (
            ("true_arm", "on_true"),
            ("false_arm", "on_false"),
        )
    ]

    assert readings[0].node_local_digest == readings[1].node_local_digest
    assert readings[0].from_node_local_digest == readings[1].from_node_local_digest
    assert readings[0].arm_label != readings[1].arm_label
    linked = service_get_procedure(procedure_instance, procedure.procedure_id).track_record
    assert linked.linked_outcomes is not None
    assert linked.linked_outcomes.model_dump(mode="json") == {
        "contract_grade": {
            "readings": 2,
            "satisfied": 2,
            "contradicted": 0,
            "indeterminate": 0,
        },
        "attestation_grade": {
            "readings": 0,
            "satisfied": 0,
            "contradicted": 0,
            "indeterminate": 0,
        },
    }
