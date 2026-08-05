"""Author-withdrawal of a pending procedure proposal (wi-033)."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, ProcedureWithdrawalRefusedError
from cruxible_core.procedure.types import ProcedureRecord, compute_procedure_definition_digest
from cruxible_core.receipt.types import Receipt
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.service import (
    service_accept_procedure,
    service_get_procedure,
    service_list_procedures,
    service_propose_procedure,
    service_withdraw_procedure,
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


def _validation_details(receipt: Receipt) -> list[dict[str, object]]:
    return [node.detail for node in receipt.nodes if node.node_type == "validation"]


def test_author_withdraws_own_pending_proposal_with_a_committed_receipt(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("withdraw_me"),
        actor_context=actor("proposer"),
    )

    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        withdrawn = service_withdraw_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            reason="changed my mind about the composition",
            actor_context=actor("proposer", "op-withdraw"),
        )

    assert withdrawn.action == "withdraw"
    assert withdrawn.procedure.status == "withdrawn"
    assert withdrawn.procedure.version == 2
    assert withdrawn.procedure.reason == "changed my mind about the composition"
    assert withdrawn.procedure.resolved_actor_context == actor("proposer", "op-withdraw")
    assert withdrawn.receipt_id is not None

    receipt = _receipt(procedure_instance, withdrawn.receipt_id)
    assert receipt.operation_type == "procedure_transition"
    assert receipt.committed is True
    assert any(
        detail.get("action") == "withdraw" and detail.get("withdrawn_by") == "author"
        for detail in _validation_details(receipt)
    )


def test_withdrawal_reason_is_optional(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("no_reason_needed"),
        actor_context=actor("proposer"),
    )

    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        withdrawn = service_withdraw_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("proposer"),
        )

    assert withdrawn.procedure.status == "withdrawn"
    assert withdrawn.procedure.reason is None


def test_reviewer_may_withdraw_another_actors_pending_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("reviewer_withdraws"),
        actor_context=actor("proposer"),
    )

    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        withdrawn = service_withdraw_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            reason="stale proposal, author unreachable",
            actor_context=actor("reviewer"),
        )

    assert withdrawn.procedure.status == "withdrawn"
    assert withdrawn.procedure.resolved_actor_context == actor("reviewer")
    assert withdrawn.receipt_id is not None
    assert any(
        detail.get("withdrawn_by") == "reviewer"
        for detail in _validation_details(_receipt(procedure_instance, withdrawn.receipt_id))
    )


def test_non_author_below_reviewer_tier_is_refused_and_receipted(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("not_yours"),
        actor_context=actor("proposer"),
    )

    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(ProcedureWithdrawalRefusedError) as exc_info:
            service_withdraw_procedure(
                procedure_instance,
                proposed.procedure.procedure_id,
                expected_version=1,
                actor_context=actor("bystander"),
            )

    message = str(exc_info.value)
    assert "may be withdrawn only by its proposing author" in message
    assert "actor 'proposer' in org 'org-procedures'" in message
    assert "reviewer holding GRAPH_WRITE" in message
    assert "actor 'bystander' in org 'org-procedures' is neither" in message
    assert exc_info.value.required_mode == "GRAPH_WRITE"
    assert exc_info.value.current_mode == "GOVERNED_WRITE"

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.committed is False
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_unattributed_proposal_is_withdrawable_only_at_reviewer_tier(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition("null_author")
    orphan = ProcedureRecord(
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition),
        proposed_actor_context=None,
    )
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_procedure(orphan)

    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(ProcedureWithdrawalRefusedError, match="an unattributed author"):
            service_withdraw_procedure(
                procedure_instance,
                orphan.procedure_id,
                expected_version=1,
                actor_context=actor("someone"),
            )

    with request_permission_scope(PermissionMode.GRAPH_WRITE):
        withdrawn = service_withdraw_procedure(
            procedure_instance,
            orphan.procedure_id,
            expected_version=1,
            actor_context=actor("someone"),
        )
    assert withdrawn.procedure.status == "withdrawn"


def test_withdrawing_a_non_pending_procedure_names_the_actual_status(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("already_live"),
        actor_context=actor("proposer"),
    )
    accepted = service_accept_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )

    with pytest.raises(ConfigError, match="must be pending; found 'live'") as exc_info:
        service_withdraw_procedure(
            procedure_instance,
            accepted.procedure.procedure_id,
            expected_version=2,
            actor_context=actor("proposer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    assert _receipt(procedure_instance, exc_info.value.mutation_receipt_id).committed is False
    assert (
        service_get_procedure(procedure_instance, accepted.procedure.procedure_id).status == "live"
    )


def test_withdrawing_an_already_withdrawn_procedure_names_that_status(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("withdraw_twice"),
        actor_context=actor("proposer"),
    )
    service_withdraw_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("proposer"),
    )

    with pytest.raises(ConfigError, match="must be pending; found 'withdrawn'"):
        service_withdraw_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=2,
            actor_context=actor("proposer"),
        )


def test_withdrawal_requires_expected_version_and_attribution(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("guarded_withdraw"),
        actor_context=actor("proposer"),
    )

    with pytest.raises(ConfigError, match="withdrawing author actor context is required"):
        service_withdraw_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=None,
        )
    with pytest.raises(ConfigError, match="requires expected_version"):
        service_withdraw_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=None,
            actor_context=actor("proposer"),
        )
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_withdrawn_name_is_immediately_reusable_and_acceptable(
    procedure_instance: CruxibleInstance,
) -> None:
    first = service_propose_procedure(
        procedure_instance,
        provider_definition("reusable_name"),
        actor_context=actor("proposer"),
    )
    service_withdraw_procedure(
        procedure_instance,
        first.procedure.procedure_id,
        expected_version=1,
        reason="wrong budget",
        actor_context=actor("proposer"),
    )

    second = service_propose_procedure(
        procedure_instance,
        provider_definition("reusable_name"),
        actor_context=actor("proposer"),
    )
    live = service_accept_procedure(
        procedure_instance,
        second.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )

    assert live.procedure.status == "live"
    assert second.procedure.procedure_id != first.procedure.procedure_id
    withdrawn_rows = service_list_procedures(
        procedure_instance,
        name="reusable_name",
        status="withdrawn",
    )
    assert [row.procedure_id for row in withdrawn_rows.items] == [first.procedure.procedure_id]


def test_supersede_of_a_pending_proposal_points_at_the_withdraw_verb(
    procedure_instance: CruxibleInstance,
) -> None:
    pending = service_propose_procedure(
        procedure_instance,
        provider_definition("supersede_pending"),
        actor_context=actor("proposer"),
    )

    with pytest.raises(ConfigError) as exc_info:
        service_propose_procedure(
            procedure_instance,
            provider_definition("supersede_pending"),
            actor_context=actor("proposer"),
            supersedes_procedure_id=pending.procedure.procedure_id,
        )

    assert str(exc_info.value).startswith(
        f"superseded procedure '{pending.procedure.procedure_id}' must be live; "
        "found 'pending'; the author may withdraw the pending proposal and re-propose"
    )
