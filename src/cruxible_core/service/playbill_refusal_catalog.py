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
from cruxible_client.contracts.repairs import RepairOperationV1, ServedRepairV1, hand_edit_repair
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


def repair_for_refusal(code: str) -> ServedRepairV1:
    """Resolve one registered code without interpreting diagnostic prose."""

    if code not in ALL_SERVED_REFUSAL_CODES:
        raise KeyError(f"unregistered served refusal code: {code}")
    if code == "prediction_unsettleable_rule":
        return RepairOperationV1(operation="playbill.predict", arguments={"rule": "equality"})
    if code == "prediction_deadline_passed":
        return RepairOperationV1(
            operation="playbill.predict", arguments={"replace_prediction": "current"}
        )
    if code == "settlement_evidence_mismatch":
        return RepairOperationV1(
            operation="playbill.settle", arguments={"prediction_id": "required"}
        )
    return hand_edit_repair(code)


def hand_edit_next_reasons() -> frozenset[str]:
    """Expose the exact client-owned positive exemption membership."""

    return frozenset(PLAYBILL_HAND_EDIT_NEXT_REASONS)


__all__ = [
    "ALL_SERVED_REFUSAL_CODES",
    "CLOSED_SERVED_REFUSAL_VOCABULARIES",
    "hand_edit_next_reasons",
    "repair_for_refusal",
]
