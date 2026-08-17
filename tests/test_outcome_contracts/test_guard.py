"""D3 enforcement: the first store-backed entity-guard condition."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service.mutations import (
    service_add_entities,
    service_batch_direct_write,
)
from cruxible_core.service.resolution_contracts import (
    service_list_resolution_contracts,
    service_open_resolution_contract,
)
from cruxible_core.service.types import BatchDirectWriteInput, EntityWriteInput
from tests.test_outcome_contracts.conftest import (
    CHECK_AT,
    EXPIRES_AT,
    actor,
    add_decision,
    query_measurement,
)


def _open(
    instance: CruxibleInstance,
    decision_id: str = "dd-1",
    *,
    check_at: Any = CHECK_AT,
    expires_at: Any = EXPIRES_AT,
):
    return service_open_resolution_contract(
        instance,
        entity_type="Decision",
        entity_id=decision_id,
        description="Service stays healthy after the change",
        check_at=check_at,
        expires_at=expires_at,
        measurement=query_measurement(),
        actor_context=actor("proposer"),
    ).contract


def _accept(
    instance: CruxibleInstance,
    decision_id: str = "dd-1",
    *,
    outcome_tracking: str = "required",
    title: str = "Adopt the thing",
) -> None:
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Decision",
                entity_id=decision_id,
                properties={
                    "decision_id": decision_id,
                    "status": "accepted",
                    "outcome_tracking": outcome_tracking,
                    "title": title,
                },
            )
        ],
        actor_context=actor("reviewer"),
    )


def test_acceptance_refuses_without_a_contract(guarded_instance) -> None:
    add_decision(guarded_instance)
    with pytest.raises(DataValidationError, match="no eligible resolution contract"):
        _accept(guarded_instance)


def test_acceptance_passes_and_activates_with_an_open_contract(guarded_instance) -> None:
    add_decision(guarded_instance)
    contract = _open(guarded_instance)
    _accept(guarded_instance)

    stored = guarded_instance.load_graph().get_entity("Decision", "dd-1")
    assert stored is not None
    assert stored.properties["status"] == "accepted"

    item = service_list_resolution_contracts(guarded_instance).items[0]
    assert item.contract.contract_id == contract.contract_id
    assert item.status == "open"
    assert item.activation is not None
    assert item.activation.acceptance_receipt_id is not None
    # The activation pins what was ACCEPTED, which differs from what the
    # contract committed to (the pre-transition content).
    assert item.activation.subject_content_digest != contract.subject_content_digest


def test_create_with_accepted_value_refuses_with_a_teaching_message(guarded_instance) -> None:
    with pytest.raises(DataValidationError) as excinfo:
        _accept(guarded_instance, "dd-new")
    message = str(excinfo.value.errors)
    assert "cannot be set at create time" in message
    assert "propose the record first" in message.lower()
    assert guarded_instance.load_graph().get_entity("Decision", "dd-new") is None


def test_opted_out_subjects_are_unaffected(guarded_instance) -> None:
    add_decision(guarded_instance, "dd-opt", outcome_tracking="not_applicable")
    _accept(guarded_instance, "dd-opt", outcome_tracking="not_applicable")

    stored = guarded_instance.load_graph().get_entity("Decision", "dd-opt")
    assert stored is not None
    assert stored.properties["status"] == "accepted"
    assert service_list_resolution_contracts(guarded_instance).total == 0


def test_an_expired_contract_does_not_satisfy_the_guard(guarded_instance) -> None:
    add_decision(guarded_instance)
    _open(
        guarded_instance,
        check_at=CHECK_AT - timedelta(days=3),
        expires_at=CHECK_AT - timedelta(hours=1),
    )
    with pytest.raises(DataValidationError, match="no eligible resolution contract"):
        _accept(guarded_instance)


def test_an_already_activated_contract_cannot_be_reused(guarded_instance) -> None:
    add_decision(guarded_instance)
    _open(guarded_instance)
    _accept(guarded_instance)

    # Demote and re-accept: the consumed contract must not satisfy the guard a
    # second time (the reusable-bypass hole).
    service_add_entities(
        guarded_instance,
        [
            EntityInstance(
                entity_type="Decision",
                entity_id="dd-1",
                properties={
                    "decision_id": "dd-1",
                    "status": "proposed",
                    "outcome_tracking": "required",
                    "title": "Adopt the thing",
                },
            )
        ],
        actor_context=actor("reviewer"),
    )
    with pytest.raises(DataValidationError, match="no eligible resolution contract"):
        _accept(guarded_instance)


def test_editing_the_subject_after_opening_invalidates_the_contract(guarded_instance) -> None:
    add_decision(guarded_instance)
    _open(guarded_instance)
    add_decision(guarded_instance, title="edited after the promise")
    with pytest.raises(DataValidationError, match="no eligible resolution contract"):
        _accept(guarded_instance, title="edited after the promise")


def test_acceptance_that_co_edits_content_refuses(guarded_instance) -> None:
    """Content binding: the transition may change the guarded property, nothing else.

    Eligibility is keyed on the PRE-write content, so a write that accepts and
    rewrites the decision in one go would otherwise ratify a commitment made
    about different content.
    """
    add_decision(guarded_instance)
    _open(guarded_instance)

    with pytest.raises(DataValidationError) as excinfo:
        _accept(guarded_instance, title="rewritten while accepting")
    message = str(excinfo.value.errors)
    assert "content the contract never committed to" in message
    assert "Re-open a contract" in message

    stored = guarded_instance.load_graph().get_entity("Decision", "dd-1")
    assert stored is not None
    assert stored.properties["status"] == "proposed"
    assert service_list_resolution_contracts(guarded_instance).items[0].activation is None

    # The taught flow works: accept alone, then edit in a separate write.
    _accept(guarded_instance)
    assert service_list_resolution_contracts(guarded_instance).items[0].activation is not None


def test_a_missing_adoption_property_fails_closed(legacy_guarded_instance) -> None:
    """A record predating the adoption property must not slip out of scope.

    ``where: {candidate.properties.outcome_tracking: {eq: required}}`` does not
    match an entity that carries no such property. Skipping the guard there
    would let every pre-migration decision accept with no contract — the
    silence is exactly the failure mode, so it refuses instead.
    """
    add_decision(legacy_guarded_instance, outcome_tracking=None)

    with pytest.raises(DataValidationError) as excinfo:
        service_add_entities(
            legacy_guarded_instance,
            [
                EntityInstance(
                    entity_type="Decision",
                    entity_id="dd-1",
                    properties={
                        "decision_id": "dd-1",
                        "status": "accepted",
                        "title": "Adopt the thing",
                    },
                )
            ],
            actor_context=actor("reviewer"),
        )
    message = str(excinfo.value.errors)
    assert "'outcome_tracking' is not set" in message
    assert "not_applicable" in message

    stored = legacy_guarded_instance.load_graph().get_entity("Decision", "dd-1")
    assert stored is not None
    assert stored.properties["status"] == "proposed"


def test_an_explicit_not_applicable_still_accepts(legacy_guarded_instance) -> None:
    """Fail-closed applies to ABSENCE, not to the recorded opt-out."""
    add_decision(legacy_guarded_instance, outcome_tracking="not_applicable")
    _accept(legacy_guarded_instance, outcome_tracking="not_applicable")

    stored = legacy_guarded_instance.load_graph().get_entity("Decision", "dd-1")
    assert stored is not None
    assert stored.properties["status"] == "accepted"


def test_one_contract_satisfies_only_one_acceptance_in_a_batch(guarded_instance) -> None:
    add_decision(guarded_instance, "dd-1")
    add_decision(guarded_instance, "dd-2")
    _open(guarded_instance, "dd-1")

    payload = BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type="Decision",
                entity_id="dd-1",
                properties={
                    "decision_id": "dd-1",
                    "status": "accepted",
                    "outcome_tracking": "required",
                    "title": "Adopt the thing",
                },
            ),
            EntityWriteInput(
                entity_type="Decision",
                entity_id="dd-2",
                properties={
                    "decision_id": "dd-2",
                    "status": "accepted",
                    "outcome_tracking": "required",
                    "title": "Adopt the thing",
                },
            ),
        ]
    )
    with pytest.raises(DataValidationError, match="no eligible resolution contract"):
        service_batch_direct_write(
            guarded_instance,
            payload,
            actor_context=actor("reviewer"),
        )


def test_batch_acceptance_activates_the_consumed_contract(guarded_instance) -> None:
    add_decision(guarded_instance)
    contract = _open(guarded_instance)
    payload = BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type="Decision",
                entity_id="dd-1",
                properties={
                    "decision_id": "dd-1",
                    "status": "accepted",
                    "outcome_tracking": "required",
                    "title": "Adopt the thing",
                },
            )
        ]
    )
    result = service_batch_direct_write(
        guarded_instance,
        payload,
        actor_context=actor("reviewer"),
    )
    assert result.valid is True
    item = service_list_resolution_contracts(guarded_instance).items[0]
    assert item.activation is not None
    assert item.activation.acceptance_receipt_id == result.receipt_id
    assert item.contract.contract_id == contract.contract_id


def test_batch_dry_run_consumes_nothing(guarded_instance) -> None:
    add_decision(guarded_instance)
    _open(guarded_instance)
    payload = BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type="Decision",
                entity_id="dd-1",
                properties={
                    "decision_id": "dd-1",
                    "status": "accepted",
                    "outcome_tracking": "required",
                    "title": "Adopt the thing",
                },
            )
        ]
    )
    preview = service_batch_direct_write(
        guarded_instance,
        payload,
        dry_run=True,
        actor_context=actor("reviewer"),
    )
    assert preview.valid is True
    assert service_list_resolution_contracts(guarded_instance).items[0].activation is None


def test_the_refusal_receipt_records_the_guard_coordinates(guarded_instance) -> None:
    add_decision(guarded_instance)
    with pytest.raises(DataValidationError) as excinfo:
        _accept(guarded_instance)
    receipt_id = excinfo.value.mutation_receipt_id
    assert receipt_id is not None
    store = guarded_instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    refusals = [
        node
        for node in receipt.nodes
        if node.node_type == "validation" and node.detail.get("guard_error")
    ]
    assert refusals
    assert refusals[0].detail["guard_name"] == "decision_acceptance_needs_contract"
    assert refusals[0].entity_id == "dd-1"


def test_a_guard_evaluated_without_a_contract_store_fails_closed(tmp_path: Path) -> None:
    """Fail-closed fallback: no store handle means no pass, ever."""
    from copy import deepcopy

    from cruxible_core.config.loader import load_config
    from cruxible_core.graph.entity_graph import EntityGraph
    from cruxible_core.graph.operations import apply_entity, validate_entity
    from cruxible_core.service.mutation_guards import mutation_guard_errors
    from tests.test_outcome_contracts.conftest import GUARDED_CONFIG

    config_path = tmp_path / "config.yaml"
    config_path.write_text(GUARDED_CONFIG)
    config = load_config(config_path)

    current = EntityGraph()
    current.add_entity(
        EntityInstance(
            entity_type="Decision",
            entity_id="dd-1",
            properties={
                "decision_id": "dd-1",
                "status": "proposed",
                "outcome_tracking": "required",
            },
        )
    )
    validated = validate_entity(
        config,
        current,
        "Decision",
        "dd-1",
        {
            "decision_id": "dd-1",
            "status": "accepted",
            "outcome_tracking": "required",
        },
    )
    proposed = EntityGraph.from_dict(deepcopy(current.to_dict()))
    apply_entity(proposed, validated, config=config, source="test")

    errors = mutation_guard_errors(
        config,
        current_graph=current,
        proposed_graph=proposed,
        entities=[validated],
        resolution_contract_store=None,
    )
    assert any("enforcement is unavailable" in error for error in errors)
