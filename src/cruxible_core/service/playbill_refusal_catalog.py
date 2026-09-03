"""Source-owned inventory of closed refusal and next-reason vocabularies."""

from __future__ import annotations

from typing import get_args

from cruxible_client.contracts import (
    PLAYBILL_HAND_EDIT_NEXT_REASONS,
    PlaybillNextReason,
    ProviderLaneUnavailableCodeV1,
)
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadReason,
    PlaybillBlockSyncReason,
)
from cruxible_client.contracts.predictions import PredictionRefusalCodeV1
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalCodeV1,
    ProcedureInternalFailureCodeV1,
    ProcedureNodeRefusalCodeV1,
    ProcedureOperationalFailureCodeV1,
    ProcedureSettlementRefusalCodeV1,
)
from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    UNDECLARED_HAND_EDIT_CHANGE,
    RepairOperationV1,
    ServedRepairV1,
    hand_edit_repair,
)
from cruxible_client.contracts.workspace_advertisement import WorkspaceAdvertisementFailureCode

CLOSED_SERVED_REFUSAL_VOCABULARIES: dict[str, frozenset[str]] = {
    "playbill_next_reason": frozenset(get_args(PlaybillNextReason)),
    "provider_lane_unavailable": frozenset(get_args(ProviderLaneUnavailableCodeV1)),
    "workspace_advertisement_failure": frozenset(get_args(WorkspaceAdvertisementFailureCode)),
    "block_sync_read_reason": frozenset(get_args(PlaybillBlockSyncReadReason)),
    "block_sync_reason": frozenset(get_args(PlaybillBlockSyncReason)),
    "procedure_admission_refusal": frozenset(get_args(ProcedureAdmissionRefusalCodeV1)),
    "procedure_node_refusal": frozenset(get_args(ProcedureNodeRefusalCodeV1)),
    "procedure_operational_failure": frozenset(get_args(ProcedureOperationalFailureCodeV1)),
    "procedure_internal_failure": frozenset(get_args(ProcedureInternalFailureCodeV1)),
    "procedure_settlement_refusal": frozenset(get_args(ProcedureSettlementRefusalCodeV1)),
    "prediction_refusal": frozenset(get_args(PredictionRefusalCodeV1)),
}

ALL_SERVED_REFUSAL_CODES = frozenset().union(*CLOSED_SERVED_REFUSAL_VOCABULARIES.values())

# Codes whose repair is one served CLI leaf. The operation is the leaf's dotted
# path, so the guardrail resolves it against the live lazy command map instead
# of trusting the string; a repair naming a command that does not exist is worse
# than no repair at all.
RUNNABLE_REFUSAL_REPAIRS: dict[str, RepairOperationV1] = {
    "line_mandate_required": RepairOperationV1(
        operation="playbill.authoring.create",
        arguments={"example": "procedure-mandate"},
    ),
    "prediction_unsettleable_rule": RepairOperationV1(operation="playbill.predict"),
    "prediction_deadline_passed": RepairOperationV1(operation="playbill.predict"),
    "settlement_evidence_mismatch": RepairOperationV1(operation="playbill.settle"),
    "block_backing_changed": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_backing_missing": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_backing_retired": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_locally_modified": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "block_sync_failed": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "projection_backing_stale": RepairOperationV1(
        operation="playbill.block.sync", arguments={"all": True}
    ),
    "projection_dirty": RepairOperationV1(operation="playbill.block.sync", arguments={"all": True}),
    "occurrence_not_due": RepairOperationV1(operation="playbill.line.run"),
    "occurrence_id_mismatch": RepairOperationV1(operation="playbill.line.run"),
    "line_identity_mismatch": RepairOperationV1(operation="playbill.line.run"),
    "document_modified": RepairOperationV1(operation="playbill.document.propose"),
}

# Everything else resolves to the truthful undeclared hand edit. The count is
# pinned by the guardrail so a new closed refusal member cannot join silently:
# adding one forces either a declared repair or an explicit re-pin here.
UNDECLARED_REFUSAL_CODE_COUNT = 170


def repair_for_refusal(code: str) -> ServedRepairV1:
    """Resolve one registered code without interpreting diagnostic prose."""

    if code not in ALL_SERVED_REFUSAL_CODES:
        raise KeyError(f"unregistered served refusal code: {code}")
    runnable = RUNNABLE_REFUSAL_REPAIRS.get(code)
    if runnable is not None:
        return runnable
    return hand_edit_repair(code)


def undeclared_refusal_codes() -> frozenset[str]:
    """Expose exactly the codes still carrying no specific declared repair."""

    return frozenset(
        code
        for code in ALL_SERVED_REFUSAL_CODES
        if code not in RUNNABLE_REFUSAL_REPAIRS and code not in DECLARED_HAND_EDIT_CHANGES
    )


def hand_edit_next_reasons() -> frozenset[str]:
    """Expose the exact client-owned positive exemption membership."""

    return frozenset(PLAYBILL_HAND_EDIT_NEXT_REASONS)


__all__ = [
    "ALL_SERVED_REFUSAL_CODES",
    "CLOSED_SERVED_REFUSAL_VOCABULARIES",
    "DECLARED_HAND_EDIT_CHANGES",
    "RUNNABLE_REFUSAL_REPAIRS",
    "UNDECLARED_HAND_EDIT_CHANGE",
    "UNDECLARED_REFUSAL_CODE_COUNT",
    "hand_edit_next_reasons",
    "repair_for_refusal",
    "undeclared_refusal_codes",
]
