"""Pending-lift dedupe for the supervised procedure migration."""

from __future__ import annotations

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.procedure.migration import lift_v1_procedure_definition
from cruxible_core.procedure.types import ProcedureRecord
from cruxible_core.service import (
    service_accept_procedure,
    service_propose_procedure,
    service_scan_pending_procedure_lift,
)
from tests.test_procedures.conftest import actor, provider_definition


def _live_v1(instance: InstanceProtocol, name: str) -> ProcedureRecord:
    proposed = service_propose_procedure(
        instance,
        provider_definition(name),
        actor_context=actor("original-author"),
    )
    return service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=proposed.procedure.version,
        actor_context=actor("original-reviewer"),
    ).procedure


def test_matching_pending_lift_is_carried_forward(
    procedure_instance: InstanceProtocol,
) -> None:
    predecessor = _live_v1(procedure_instance, "pending_lift_dedupe")
    lifted = lift_v1_procedure_definition(predecessor.definition)
    pending = service_propose_procedure(
        procedure_instance,
        lifted,
        supersedes_procedure_id=predecessor.procedure_id,
        actor_context=actor("migration-proposer"),
    ).procedure

    scan = service_scan_pending_procedure_lift(
        procedure_instance,
        predecessor,
        lifted,
    )

    assert scan.disposition == "matching"
    assert scan.pending is not None
    assert scan.pending.procedure_id == pending.procedure_id
    assert scan.conflicts == ()


def test_non_matching_pending_proposal_is_refused_and_named(
    procedure_instance: InstanceProtocol,
) -> None:
    predecessor = _live_v1(procedure_instance, "pending_human_revision")
    lifted = lift_v1_procedure_definition(predecessor.definition)
    unrelated = lifted.model_copy(
        update={"description": "A human-authored change already under review"}
    )
    pending = service_propose_procedure(
        procedure_instance,
        unrelated,
        supersedes_procedure_id=predecessor.procedure_id,
        actor_context=actor("human-author"),
    ).procedure

    scan = service_scan_pending_procedure_lift(
        procedure_instance,
        predecessor,
        lifted,
    )

    assert scan.disposition == "refused"
    assert scan.pending is None
    assert scan.conflicts == (pending,)
    assert scan.reason is not None
    assert "pending_human_revision" in scan.reason
    assert pending.procedure_id in scan.reason
