"""Acceptance-time procedure contracts and reserved-subject classification."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    ConfigError,
    MalformedReservedSubjectError,
    ReservedSubjectError,
    UnknownReservedSubjectError,
)
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import (
    service_accept_procedure,
    service_list_resolution_contracts,
    service_lock,
    service_open_resolution_contract,
    service_outcome_queue,
    service_propose_procedure,
)
from cruxible_core.temporal import utc_now
from tests.test_procedures.conftest import CONFIG_YAML, actor


@pytest.fixture
def procedure_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    service_lock(instance)
    return instance


def _measured_definition() -> ProcedureDefinition:
    return ProcedureDefinition.model_validate(
        {
            "graph_format": 2,
            "name": "measured_unit",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
            "measurements": [
                {
                    "name": "unit_quality",
                    "granularity": "procedure_unit",
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
            ],
        }
    )


def test_acceptance_opens_activates_and_queues_procedure_contract(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        _measured_definition(),
        actor_context=actor("proposer"),
    )
    accepted = service_accept_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )

    listed = service_list_resolution_contracts(
        procedure_instance,
        entity_type="cruxible.Procedure",
        entity_id=accepted.procedure.procedure_id,
    )
    assert listed.total == 1
    item = listed.items[0]
    assert item.status == "open"
    assert item.activation is not None
    assert item.contract.subject_content_digest == accepted.procedure.definition_digest
    assert item.activation.subject_content_digest == accepted.procedure.definition_digest
    assert item.subject_present is True
    assert item.subject_content_drifted is False

    due = service_outcome_queue(procedure_instance, queue="due")
    assert [entry.contract_id for entry in due.items] == [item.contract.contract_id]


def test_public_open_refuses_live_reserved_subject_before_lookup_or_declaration(
    procedure_instance: CruxibleInstance,
) -> None:
    now = utc_now()
    with pytest.raises(ReservedSubjectError) as exc_info:
        service_open_resolution_contract(
            procedure_instance,
            entity_type="cruxible.Procedure",
            entity_id="PRC-does-not-exist",
            description="",
            check_at=now + timedelta(days=2),
            expires_at=now + timedelta(days=1),
            measurement={"kind": "not-a-measurement"},
            actor_context=actor("public-opener"),
            idempotency_key="reserved-precedence",
        )

    error = exc_info.value
    assert error.error_code == "reserved_subject"
    assert error.mutation_receipt_id is not None
    receipt_store = procedure_instance.get_receipt_store()
    try:
        receipt = receipt_store.get_receipt(error.mutation_receipt_id)
    finally:
        receipt_store.close()
    assert receipt is not None
    assert receipt.committed is False
    assert any(node.detail.get("reason_code") == "reserved_subject" for node in receipt.nodes)


@pytest.mark.parametrize(
    ("entity_type", "error_type", "error_code"),
    [
        ("cruxible.Procedures", UnknownReservedSubjectError, "unknown_reserved_subject"),
        ("cruxible", MalformedReservedSubjectError, "malformed_reserved_subject"),
        ("cruxible.", MalformedReservedSubjectError, "malformed_reserved_subject"),
        (
            "cruxible.Procedure.extra",
            MalformedReservedSubjectError,
            "malformed_reserved_subject",
        ),
    ],
)
def test_reserved_classifier_has_typed_symmetric_refusals(
    procedure_instance: CruxibleInstance,
    entity_type: str,
    error_type: type[ConfigError],
    error_code: str,
) -> None:
    now = utc_now()
    with pytest.raises(error_type) as exc_info:
        service_open_resolution_contract(
            procedure_instance,
            entity_type=entity_type,
            entity_id="subject",
            description="invalid later",
            check_at=now,
            expires_at=now,
            measurement={},
            actor_context=actor("public-opener"),
        )
    assert getattr(exc_info.value, "error_code") == error_code
