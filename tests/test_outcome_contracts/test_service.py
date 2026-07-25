"""D1-D5 service semantics: declaration, one-shot answers, queues."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.service import (
    service_attest,
    service_dispose_resolution,
    service_list_resolution_contracts,
    service_open_resolution_contract,
    service_outcome_queue,
    service_query,
    service_resolve_outcome,
)
from cruxible_core.service.mutation_guards import record_contract_activations
from cruxible_core.temporal import utc_now
from tests.test_outcome_contracts.conftest import (
    CHECK_AT,
    EXPIRES_AT,
    FUTURE_CHECK_AT,
    actor,
    add_decision,
    attestation_measurement,
    evidence,
    query_measurement,
)


def _open(
    instance: CruxibleInstance,
    *,
    decision_id: str = "dd-1",
    measurement: dict[str, Any] | None = None,
    check_at: Any = CHECK_AT,
    expires_at: Any = EXPIRES_AT,
    idempotency_key: str | None = None,
    opener: str = "proposer",
):
    return service_open_resolution_contract(
        instance,
        entity_type="Decision",
        entity_id=decision_id,
        description="Service stays healthy after the change",
        check_at=check_at,
        expires_at=expires_at,
        measurement=measurement or query_measurement(),
        actor_context=actor(opener),
        idempotency_key=idempotency_key,
    )


def _activate(instance: CruxibleInstance, contract_id: str) -> None:
    """Stand in for the acceptance write path's in-transaction activation."""
    from cruxible_core.resolution_contracts.types import ContractActivation

    with instance.write_transaction() as uow:
        uow.resolution_contracts.save_activation(
            ContractActivation(
                contract_id=contract_id,
                acceptance_receipt_id="RCP-test-acceptance",
                subject_content_digest="sha256:test-accepted-content",
            )
        )


# ---------------------------------------------------------------------------
# D1/D2 open
# ---------------------------------------------------------------------------


def test_open_records_the_declaration_and_pins_the_query_digest(contract_instance) -> None:
    add_decision(contract_instance)
    result = _open(contract_instance)

    contract = result.contract
    assert contract.entity_type == "Decision"
    assert contract.entity_id == "dd-1"
    assert contract.receipt_id is not None
    measurement = contract.declaration.measurement
    assert measurement.kind == "query"
    assert measurement.query_definition_digest is not None
    assert measurement.query_definition_digest.startswith("sha256:")


def test_open_refuses_an_absent_subject(contract_instance) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        _open(contract_instance, decision_id="dd-missing")


def test_open_refuses_check_at_at_or_after_expiry(contract_instance) -> None:
    add_decision(contract_instance)
    with pytest.raises(ConfigError, match="check_at must be strictly before expires_at"):
        _open(contract_instance, check_at=EXPIRES_AT, expires_at=EXPIRES_AT)


def test_open_refuses_an_unknown_query_measurement(contract_instance) -> None:
    add_decision(contract_instance)
    with pytest.raises(ConfigError, match="not .*defined in named_queries"):
        _open(contract_instance, measurement=query_measurement(query_name="nope"))


def test_open_refuses_query_params_missing_the_entry_point_key(contract_instance) -> None:
    add_decision(contract_instance)
    with pytest.raises(ConfigError, match="must supply 'service_id'"):
        _open(
            contract_instance,
            measurement=query_measurement(query_name="service_controls", params={}),
        )


def test_open_refuses_an_expectation_that_constrains_nothing(contract_instance) -> None:
    add_decision(contract_instance)
    with pytest.raises(ConfigError, match="expect requires min_count, max_count, or condition"):
        _open(contract_instance, measurement=query_measurement(expect={}))


def test_multiple_open_contracts_per_subject_are_legal(contract_instance) -> None:
    add_decision(contract_instance)
    first = _open(contract_instance)
    second = _open(contract_instance, measurement=attestation_measurement())

    assert first.contract.contract_id != second.contract.contract_id
    listed = service_list_resolution_contracts(
        contract_instance, entity_type="Decision", entity_id="dd-1"
    )
    assert listed.total == 2


def test_idempotent_open_replays_without_a_second_contract(contract_instance) -> None:
    add_decision(contract_instance)
    first = _open(contract_instance, idempotency_key="retry-1")
    replay = _open(contract_instance, idempotency_key="retry-1")

    assert replay.idempotent_replay is True
    assert replay.contract.contract_id == first.contract.contract_id
    assert replay.receipt_id == first.contract.receipt_id
    assert service_list_resolution_contracts(contract_instance).total == 1


def test_divergent_idempotent_replay_refuses(contract_instance) -> None:
    add_decision(contract_instance)
    _open(contract_instance, idempotency_key="retry-1")
    with pytest.raises(ConfigError, match="diverges from the original contract"):
        _open(
            contract_instance,
            idempotency_key="retry-1",
            measurement=query_measurement(expect={"min_count": 5}),
        )


def test_open_requires_an_actor(contract_instance) -> None:
    add_decision(contract_instance)
    with pytest.raises(ConfigError, match="actor context is required"):
        service_open_resolution_contract(
            contract_instance,
            entity_type="Decision",
            entity_id="dd-1",
            description="x",
            check_at=CHECK_AT,
            expires_at=EXPIRES_AT,
            measurement=query_measurement(),
            actor_context=None,
        )


def test_open_never_mutates_the_subject(contract_instance) -> None:
    subject = add_decision(contract_instance)
    before = dict(subject.properties)
    _open(contract_instance)
    after = contract_instance.load_graph().get_entity("Decision", "dd-1")
    assert after is not None
    assert dict(after.properties) == before


# ---------------------------------------------------------------------------
# D4 resolutions
# ---------------------------------------------------------------------------


def _resolving_query_receipt(instance: CruxibleInstance) -> str:
    result = service_query(instance, "healthy_services", {})
    assert result.receipt_id is not None
    return result.receipt_id


def test_query_receipts_carry_the_read_revision_stamp(contract_instance) -> None:
    """Prerequisite fix: the receipt alone proves the observed revision."""
    result = service_query(contract_instance, "healthy_services", {})
    assert result.receipt is not None
    assert result.receipt.read_revision == contract_instance.get_read_revision()


def test_resolve_refuses_a_prepared_contract(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    with pytest.raises(ConfigError, match="never activated"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="indeterminate",
            observed_at=utc_now(),
            actor_context=actor("checker"),
        )


def test_satisfied_resolution_validates_the_resolving_receipt(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)

    result = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("healthy")],
        actor_context=actor("checker"),
        resolving_query_receipt_id=receipt_id,
    )
    assert result.resolution.sequence == 1
    assert result.resolution.resolving_query_receipt_id == receipt_id
    assert result.resolution.receipt_id is not None


def test_satisfied_requires_observed_at_at_or_after_check_at(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance, check_at=FUTURE_CHECK_AT).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)

    with pytest.raises(ConfigError, match="at or after the declared check_at"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="satisfied",
            observed_at=utc_now(),
            evidence_refs=[evidence("early")],
            actor_context=actor("checker"),
            resolving_query_receipt_id=receipt_id,
        )


def test_contradicted_may_be_observed_before_check_at_but_needs_a_note(
    contract_instance,
) -> None:
    add_decision(contract_instance)
    contract = _open(
        contract_instance,
        check_at=FUTURE_CHECK_AT,
        measurement=query_measurement(expect={"min_count": 5}),
    ).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)

    with pytest.raises(ConfigError, match="requires a note"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="contradicted",
            observed_at=utc_now(),
            evidence_refs=[evidence("bad")],
            actor_context=actor("checker"),
            resolving_query_receipt_id=receipt_id,
        )

    result = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="contradicted",
        observed_at=utc_now(),
        evidence_refs=[evidence("bad")],
        actor_context=actor("checker"),
        note="only one healthy service, we promised five",
        resolving_query_receipt_id=receipt_id,
    )
    assert result.resolution.verdict == "contradicted"


def test_satisfied_refuses_when_the_receipt_contradicts_the_expectation(
    contract_instance,
) -> None:
    add_decision(contract_instance)
    contract = _open(
        contract_instance, measurement=query_measurement(expect={"min_count": 5})
    ).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)

    with pytest.raises(ConfigError, match="does not satisfy the declared expectation"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="satisfied",
            observed_at=utc_now(),
            evidence_refs=[evidence("healthy")],
            actor_context=actor("checker"),
            resolving_query_receipt_id=receipt_id,
        )


def test_contradicted_refuses_when_the_receipt_satisfies_the_expectation(
    contract_instance,
) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)

    with pytest.raises(ConfigError, match="satisfies the declared expectation"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="contradicted",
            observed_at=utc_now(),
            evidence_refs=[evidence("x")],
            actor_context=actor("checker"),
            note="claiming failure against a passing receipt",
            resolving_query_receipt_id=receipt_id,
        )


def test_resolve_refuses_a_receipt_for_a_different_query(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    other = service_query(contract_instance, "service_controls", {"service_id": "svc-1"})
    assert other.receipt_id is not None

    with pytest.raises(ConfigError, match="not the declared"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="satisfied",
            observed_at=utc_now(),
            evidence_refs=[evidence("wrong")],
            actor_context=actor("checker"),
            resolving_query_receipt_id=other.receipt_id,
        )


def test_satisfied_requires_a_resolving_receipt_on_a_query_measurement(
    contract_instance,
) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)

    with pytest.raises(ConfigError, match="requires resolving_query_receipt_id"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="satisfied",
            observed_at=utc_now(),
            evidence_refs=[evidence("none")],
            actor_context=actor("checker"),
        )


def test_query_definition_drift_allows_only_indeterminate(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)

    config = contract_instance.load_config()
    config.named_queries["healthy_services"].description = "redefined after the promise"
    contract_instance.save_config(config)

    with pytest.raises(ConfigError, match="definition digest drift"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="satisfied",
            observed_at=utc_now(),
            evidence_refs=[evidence("drift")],
            actor_context=actor("checker"),
            resolving_query_receipt_id=receipt_id,
        )

    result = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="indeterminate",
        observed_at=utc_now(),
        actor_context=actor("checker"),
        note="the measurement query changed under us",
    )
    assert result.resolution.verdict == "indeterminate"


def test_attestation_measurement_validates_stance_and_content_digest(
    contract_instance,
) -> None:
    from cruxible_core.graph.types import RelationshipInstance
    from cruxible_core.service import service_add_relationships

    service_add_relationships(
        contract_instance,
        [
            RelationshipInstance(
                relationship_type="protected_by",
                from_type="Service",
                from_id="svc-1",
                to_type="Control",
                to_id="ctl-1",
                properties={"severity": "high"},
            )
        ],
        "test",
        "outcome-fixture",
        actor_context=actor("claim-writer"),
    )
    add_decision(contract_instance)
    contract = _open(contract_instance, measurement=attestation_measurement()).contract
    _activate(contract_instance, contract.contract_id)

    support = service_attest(
        contract_instance,
        relationship_type="protected_by",
        from_type="Service",
        from_id="svc-1",
        to_type="Control",
        to_id="ctl-1",
        stance="support",
        evidence_refs=[evidence("att")],
        observed_at=utc_now(),
        actor_context=actor("observer"),
    )
    attestation_id = support.attestation.attestation_id

    with pytest.raises(ConfigError, match="needs 'contradict'"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="contradicted",
            observed_at=utc_now(),
            evidence_refs=[evidence("att")],
            actor_context=actor("checker"),
            note="mismatched stance",
            resolving_attestation_ids=[attestation_id],
        )

    result = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("att")],
        actor_context=actor("checker"),
        resolving_attestation_ids=[attestation_id],
    )
    assert result.resolution.resolving_attestation_ids == [attestation_id]


def test_second_resolution_refuses_until_a_reviewer_overturns(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)
    first = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("first")],
        actor_context=actor("checker"),
        resolving_query_receipt_id=receipt_id,
    ).resolution

    with pytest.raises(ConfigError, match="already carries a standing"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="indeterminate",
            observed_at=utc_now(),
            actor_context=actor("checker"),
        )

    service_dispose_resolution(
        contract_instance,
        first.resolution_id,
        verdict="overturned",
        actor_context=actor("reviewer"),
        note="the receipt measured the wrong window",
    )
    second = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="indeterminate",
        observed_at=utc_now(),
        actor_context=actor("checker"),
        note="re-measured, inconclusive",
    ).resolution
    assert second.sequence == 2


def test_upheld_disposition_does_not_reopen(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)
    first = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("first")],
        actor_context=actor("checker"),
        resolving_query_receipt_id=receipt_id,
    ).resolution
    service_dispose_resolution(
        contract_instance,
        first.resolution_id,
        verdict="upheld",
        actor_context=actor("reviewer"),
    )
    with pytest.raises(ConfigError, match="already carries a standing"):
        service_resolve_outcome(
            contract_instance,
            contract.contract_id,
            verdict="indeterminate",
            observed_at=utc_now(),
            actor_context=actor("checker"),
        )


def test_second_disposition_on_one_resolution_refuses(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)
    first = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("first")],
        actor_context=actor("checker"),
        resolving_query_receipt_id=receipt_id,
    ).resolution
    service_dispose_resolution(
        contract_instance,
        first.resolution_id,
        verdict="upheld",
        actor_context=actor("reviewer"),
    )
    with pytest.raises(ConfigError, match="already carries a reviewer disposition"):
        service_dispose_resolution(
            contract_instance,
            first.resolution_id,
            verdict="overturned",
            actor_context=actor("reviewer"),
        )


def test_resolution_never_mutates_the_subject(contract_instance) -> None:
    subject = add_decision(contract_instance)
    before = dict(subject.properties)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)
    service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("first")],
        actor_context=actor("checker"),
        resolving_query_receipt_id=receipt_id,
    )
    after = contract_instance.load_graph().get_entity("Decision", "dd-1")
    assert after is not None
    assert dict(after.properties) == before


# ---------------------------------------------------------------------------
# D5 queues
# ---------------------------------------------------------------------------


def test_due_queue_lists_only_activated_contracts(contract_instance) -> None:
    add_decision(contract_instance)
    prepared = _open(contract_instance).contract
    activated = _open(contract_instance, measurement=attestation_measurement()).contract
    _activate(contract_instance, activated.contract_id)

    due = service_outcome_queue(contract_instance, queue="due")
    ids = [entry.contract_id for entry in due.items]
    assert ids == [activated.contract_id]
    assert prepared.contract_id not in ids


def test_due_queue_excludes_contracts_whose_check_at_has_not_arrived(
    contract_instance,
) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance, check_at=FUTURE_CHECK_AT).contract
    _activate(contract_instance, contract.contract_id)
    assert service_outcome_queue(contract_instance, queue="due").total == 0


def test_overdue_queue_is_the_past_expiry_subset(contract_instance) -> None:
    add_decision(contract_instance)
    expired = _open(
        contract_instance,
        check_at=CHECK_AT - timedelta(days=10),
        expires_at=CHECK_AT - timedelta(days=1),
    ).contract
    _activate(contract_instance, expired.contract_id)
    live = _open(contract_instance, measurement=attestation_measurement()).contract
    _activate(contract_instance, live.contract_id)

    overdue = service_outcome_queue(contract_instance, queue="overdue")
    assert [entry.contract_id for entry in overdue.items] == [expired.contract_id]

    due = service_outcome_queue(contract_instance, queue="due")
    due_by_id = {entry.contract_id: entry for entry in due.items}
    assert due_by_id[expired.contract_id].overdue is True
    assert due_by_id[live.contract_id].overdue is False


def test_queues_drop_dead_subjects(contract_instance) -> None:
    from cruxible_core.graph.assertion_state import EntityLifecycleState
    from cruxible_core.graph.types import EntityInstance, EntityMetadata

    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    assert service_outcome_queue(contract_instance, queue="due").total == 1

    graph = contract_instance.load_graph()
    subject = graph.get_entity("Decision", "dd-1")
    assert subject is not None
    retired = EntityInstance(
        entity_type="Decision",
        entity_id="dd-1",
        properties=dict(subject.properties),
        metadata=EntityMetadata(lifecycle=EntityLifecycleState(status="retired")),
    )
    contract_instance.save_graph_delta(graph, entities=[retired])
    contract_instance.invalidate_graph_cache()

    assert service_outcome_queue(contract_instance, queue="due").total == 0


def test_resolved_contract_leaves_the_due_queue(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)
    service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="satisfied",
        observed_at=utc_now(),
        evidence_refs=[evidence("done")],
        actor_context=actor("checker"),
        resolving_query_receipt_id=receipt_id,
    )
    assert service_outcome_queue(contract_instance, queue="due").total == 0


def test_contradicted_queue_drains_only_on_a_reviewer_disposition(
    contract_instance,
) -> None:
    add_decision(contract_instance)
    contract = _open(
        contract_instance, measurement=query_measurement(expect={"min_count": 5})
    ).contract
    _activate(contract_instance, contract.contract_id)
    receipt_id = _resolving_query_receipt(contract_instance)
    resolution = service_resolve_outcome(
        contract_instance,
        contract.contract_id,
        verdict="contradicted",
        observed_at=utc_now(),
        evidence_refs=[evidence("bad")],
        actor_context=actor("checker"),
        note="we promised five healthy services and got one",
        resolving_query_receipt_id=receipt_id,
    ).resolution

    queue = service_outcome_queue(contract_instance, queue="contradicted")
    assert [entry.contract_id for entry in queue.items] == [contract.contract_id]

    service_dispose_resolution(
        contract_instance,
        resolution.resolution_id,
        verdict="upheld",
        actor_context=actor("reviewer"),
        note="accepted, opening remediation",
    )
    assert service_outcome_queue(contract_instance, queue="contradicted").total == 0


def test_list_marks_status_activation_and_content_drift(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract

    prepared = service_list_resolution_contracts(contract_instance).items[0]
    assert prepared.status == "prepared"
    assert prepared.activation is None
    assert prepared.subject_present is True
    assert prepared.subject_content_drifted is False

    _activate(contract_instance, contract.contract_id)
    add_decision(contract_instance, title="edited after the promise")

    opened = service_list_resolution_contracts(contract_instance).items[0]
    assert opened.status == "open"
    assert opened.activation is not None
    assert opened.subject_content_drifted is True


def test_list_status_filter_and_page_validation(contract_instance) -> None:
    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    _activate(contract_instance, contract.contract_id)
    _open(contract_instance, measurement=attestation_measurement())

    assert len(service_list_resolution_contracts(contract_instance, status="open").items) == 1
    assert len(service_list_resolution_contracts(contract_instance, status="prepared").items) == 1
    with pytest.raises(ConfigError):
        service_list_resolution_contracts(contract_instance, limit=0)


def test_record_contract_activations_writes_the_acceptance_join(contract_instance) -> None:
    from cruxible_core.service.mutation_guards import ContractActivationIntent

    add_decision(contract_instance)
    contract = _open(contract_instance).contract
    with contract_instance.write_transaction() as uow:
        record_contract_activations(
            uow.resolution_contracts,
            [
                ContractActivationIntent(
                    contract_id=contract.contract_id,
                    guard_name="g",
                    entity_type="Decision",
                    entity_id="dd-1",
                    accepted_content_digest="sha256:accepted",
                )
            ],
            acceptance_receipt_id="RCP-acceptance",
        )
    item = service_list_resolution_contracts(contract_instance).items[0]
    assert item.activation is not None
    assert item.activation.acceptance_receipt_id == "RCP-acceptance"
    assert item.activation.subject_content_digest == "sha256:accepted"
