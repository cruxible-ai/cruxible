"""Receipted procedure lifecycle, attribution, and digest-pinning tests."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import (
    ProcedureRecord,
    compute_procedure_definition_digest,
)
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    service_accept_procedure,
    service_get_procedure,
    service_lock,
    service_propose_procedure,
    service_reject_procedure,
    service_retire_procedure,
)
from cruxible_core.workflow.compiler import (
    compute_lock_config_digest,
    compute_lock_digest,
    load_lock,
    resolve_lock_path,
)
from tests.test_procedures.conftest import actor, provider_definition


def _receipt(instance: CruxibleInstance, receipt_id: str) -> Receipt:
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
        assert receipt is not None
        return receipt
    finally:
        store.close()


def test_propose_and_accept_pin_definition_config_and_lock_digests(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition()
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    assert proposed.procedure.status == "pending"
    assert proposed.procedure.version == 1
    assert proposed.procedure.definition_digest == compute_procedure_definition_digest(definition)
    assert proposed.receipt_id is not None
    proposal_receipt = _receipt(procedure_instance, proposed.receipt_id)
    assert proposal_receipt.operation_type == "procedure_transition"
    assert proposal_receipt.committed is True

    accepted = service_accept_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )

    config = procedure_instance.load_config()
    lock = load_lock(resolve_lock_path(procedure_instance))
    assert accepted.procedure.status == "live"
    assert accepted.procedure.version == 2
    assert accepted.procedure.definition_digest == proposed.procedure.definition_digest
    assert accepted.procedure.acceptance_config_digest == compute_lock_config_digest(config)
    assert accepted.procedure.acceptance_lock_digest == compute_lock_digest(lock)
    assert accepted.procedure.resolved_actor_context == actor("reviewer")
    assert accepted.receipt_id is not None
    assert _receipt(procedure_instance, accepted.receipt_id).committed is True


def test_proposal_refuses_unknown_precondition_entity_type_with_receipt(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition(
        "unknown_precondition_type",
        precondition={
            "entity_type": "UnknownType",
            "condition": {"status": "ready"},
        },
    )

    with pytest.raises(
        ConfigError,
        match="precondition references unknown entity type 'UnknownType'",
    ) as exc_info:
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.committed is False
    assert any(
        "UnknownType" in str(node.detail.get("reason", ""))
        for node in receipt.nodes
        if node.node_type == "validation"
    )


def test_acceptance_refuses_precondition_entity_type_removed_after_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition(
        "removed_precondition_type",
        precondition={
            "entity_type": "Task",
            "condition": {"status": "ready"},
        },
    )
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )
    config = procedure_instance.load_config()
    del config.entity_types["Task"]
    procedure_instance.save_config(config)

    with pytest.raises(
        ConfigError,
        match="precondition references unknown entity type 'Task'",
    ) as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.committed is False
    assert any(
        "Task" in str(node.detail.get("reason", ""))
        for node in receipt.nodes
        if node.node_type == "validation"
    )
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_acceptance_refuses_same_actor_and_receipts_the_refusal(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition(),
        actor_context=actor("same-person", "op-propose"),
    )

    with pytest.raises(ConfigError, match="independent from the proposer") as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("same-person", "op-review"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.operation_type == "procedure_transition"
    assert receipt.committed is False
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_missing_proposer_or_reviewer_attribution_is_refused_and_receipted(
    procedure_instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="proposer actor context is required") as propose_exc:
        service_propose_procedure(
            procedure_instance,
            provider_definition("missing_proposer"),
            actor_context=None,
        )
    assert propose_exc.value.mutation_receipt_id is not None

    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("missing_reviewer"),
        actor_context=actor("proposer"),
    )
    with pytest.raises(ConfigError, match="reviewer actor context is required") as review_exc:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=None,
        )
    assert review_exc.value.mutation_receipt_id is not None

    definition = provider_definition("persisted_null_proposer")
    malformed = ProcedureRecord(
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition),
        proposed_actor_context=None,
    )
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_procedure(malformed)
    with pytest.raises(ConfigError, match="proposer actor context is missing/null") as null_exc:
        service_accept_procedure(
            procedure_instance,
            malformed.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )
    assert null_exc.value.mutation_receipt_id is not None


def test_version_conflict_refuses_without_transition(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition(),
        actor_context=actor("proposer"),
    )

    with pytest.raises(ConfigError, match="expected version 2, found 1") as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=2,
            actor_context=actor("reviewer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    assert service_get_procedure(procedure_instance, proposed.procedure.procedure_id).version == 1


def test_reject_and_retire_require_reasons(
    procedure_instance: CruxibleInstance,
) -> None:
    rejected_candidate = service_propose_procedure(
        procedure_instance,
        provider_definition("reject_me"),
        actor_context=actor("proposer-a"),
    )
    with pytest.raises(ConfigError, match="reject requires a non-empty reason"):
        service_reject_procedure(
            procedure_instance,
            rejected_candidate.procedure.procedure_id,
            expected_version=1,
            reason="  ",
            actor_context=actor("reviewer-a"),
        )
    rejected = service_reject_procedure(
        procedure_instance,
        rejected_candidate.procedure.procedure_id,
        expected_version=1,
        reason="unsafe composition",
        actor_context=actor("reviewer-a"),
    )
    assert rejected.procedure.status == "rejected"
    assert rejected.procedure.version == 2
    assert rejected.procedure.reason == "unsafe composition"

    live_candidate = service_propose_procedure(
        procedure_instance,
        provider_definition("retire_me"),
        actor_context=actor("proposer-b"),
    )
    live = service_accept_procedure(
        procedure_instance,
        live_candidate.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-b"),
    )
    with pytest.raises(ConfigError, match="retire requires a non-empty reason"):
        service_retire_procedure(
            procedure_instance,
            live.procedure.procedure_id,
            expected_version=2,
            reason="",
            actor_context=actor("retirer"),
        )
    retired = service_retire_procedure(
        procedure_instance,
        live.procedure.procedure_id,
        expected_version=2,
        reason="action is obsolete",
        actor_context=actor("retirer"),
    )
    assert retired.procedure.status == "retired"
    assert retired.procedure.version == 3
    assert retired.procedure.reason == "action is obsolete"
    assert retired.procedure.retired_actor_context == actor("retirer")


def test_live_change_is_new_superseding_proposal_and_acceptance_retires_old(
    procedure_instance: CruxibleInstance,
) -> None:
    first_pending = service_propose_procedure(
        procedure_instance,
        provider_definition("versioned_action"),
        actor_context=actor("first-proposer"),
    )
    first_live = service_accept_procedure(
        procedure_instance,
        first_pending.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("first-reviewer"),
    )
    changed = provider_definition("versioned_action").model_copy(
        update={"description": "A separately reviewed revision"}
    )
    second_pending = service_propose_procedure(
        procedure_instance,
        changed,
        actor_context=actor("second-proposer"),
        supersedes_procedure_id=first_live.procedure.procedure_id,
    )

    assert second_pending.procedure.procedure_id != first_live.procedure.procedure_id
    assert second_pending.procedure.supersedes_procedure_id == first_live.procedure.procedure_id
    assert (
        service_get_procedure(procedure_instance, first_live.procedure.procedure_id).status
        == "live"
    )

    second_live = service_accept_procedure(
        procedure_instance,
        second_pending.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("second-reviewer"),
    )

    retired_first = service_get_procedure(procedure_instance, first_live.procedure.procedure_id)
    assert second_live.procedure.status == "live"
    assert retired_first.status == "retired"
    assert retired_first.version == 3
    assert retired_first.reason == (
        f"superseded by procedure '{second_live.procedure.procedure_id}'"
    )


def test_provider_export_and_declared_tier_are_revalidated_at_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    disabled = provider_definition("disabled_provider").model_copy(deep=True)
    assert disabled.steps[0].provider == "exported_action"  # type: ignore[union-attr]
    disabled.steps[0].provider = "disabled_action"  # type: ignore[union-attr]
    with pytest.raises(ConfigError, match="not exported to procedures"):
        service_propose_procedure(
            procedure_instance,
            disabled,
            actor_context=actor("proposer"),
        )

    low_tier = provider_definition("low_tier").model_copy(
        update={"declared_tier": "governed_write"}
    )
    with pytest.raises(ConfigError, match="below its effective provider tier"):
        service_propose_procedure(
            procedure_instance,
            low_tier,
            actor_context=actor("proposer"),
        )


def test_acceptance_recompiles_and_refuses_a_provider_deexported_after_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("drifted_provider"),
        actor_context=actor("proposer"),
    )
    config = procedure_instance.load_config()
    config.providers["exported_action"].procedure_access = "disabled"
    procedure_instance.save_config(config)
    service_lock(procedure_instance)

    with pytest.raises(ConfigError, match="not exported to procedures") as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_proposer_may_reject_own_proposal_as_withdrawal(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("withdraw_me"),
        actor_context=actor("proposer"),
    )
    withdrawn = service_reject_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        reason="withdrawing my own proposal",
        actor_context=actor("proposer"),
    )
    assert withdrawn.procedure.status == "rejected"
    assert withdrawn.procedure.reason == "withdrawing my own proposal"


def test_acceptance_refuses_second_live_procedure_with_same_name(
    procedure_instance: CruxibleInstance,
) -> None:
    first = service_propose_procedure(
        procedure_instance,
        provider_definition("unique_name"),
        actor_context=actor("proposer-a"),
    )
    service_accept_procedure(
        procedure_instance,
        first.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-a"),
    )
    second = service_propose_procedure(
        procedure_instance,
        provider_definition("unique_name"),
        actor_context=actor("proposer-b"),
    )
    with pytest.raises(ConfigError, match="one live version per name") as exc_info:
        service_accept_procedure(
            procedure_instance,
            second.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer-b"),
        )
    assert exc_info.value.mutation_receipt_id is not None
    assert (
        service_get_procedure(procedure_instance, second.procedure.procedure_id).status == "pending"
    )


def test_supersede_race_second_acceptance_refused(
    procedure_instance: CruxibleInstance,
) -> None:
    v1 = service_propose_procedure(
        procedure_instance,
        provider_definition("raced_name"),
        actor_context=actor("proposer-a"),
    )
    v1_live = service_accept_procedure(
        procedure_instance,
        v1.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-a"),
    )
    v2a = service_propose_procedure(
        procedure_instance,
        provider_definition("raced_name"),
        actor_context=actor("proposer-b"),
        supersedes_procedure_id=v1_live.procedure.procedure_id,
    )
    v2b = service_propose_procedure(
        procedure_instance,
        provider_definition("raced_name"),
        actor_context=actor("proposer-c"),
        supersedes_procedure_id=v1_live.procedure.procedure_id,
    )
    service_accept_procedure(
        procedure_instance,
        v2a.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-b"),
    )
    with pytest.raises(ConfigError, match="one live version per name"):
        service_accept_procedure(
            procedure_instance,
            v2b.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer-c"),
        )
    live_rows = [
        record
        for record in (
            service_get_procedure(procedure_instance, pid)
            for pid in (
                v1_live.procedure.procedure_id,
                v2a.procedure.procedure_id,
                v2b.procedure.procedure_id,
            )
        )
        if record.status == "live"
    ]
    assert len(live_rows) == 1
    assert live_rows[0].procedure_id == v2a.procedure.procedure_id
